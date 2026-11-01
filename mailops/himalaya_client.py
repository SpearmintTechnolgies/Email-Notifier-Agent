from __future__ import annotations

import json
import logging
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import paths
from .paths import himalaya_bin
from .policy import SEND_OPS, denial
from .safe_mode import guard_mutation

log = logging.getLogger("mailops.himalaya")


def _blocked(
    op: str, account: str, detail: str, skipped_text: str, *, approved: bool = False
) -> "RunResult | None":
    """Last line of defence for every mailbox mutation and send.

    Returns a RunResult when the operation must NOT run, else None:
    1. account policy denies it       -> failure (code 1), nothing runs
    2. a send without explicit approval -> failure (code 1), nothing runs
    3. test mode                      -> logged no-op that reports success
    """
    why = denial(account, op)
    if why:
        log.warning("[POLICY] blocked %s", why)
        return RunResult(1, "", f"policy: {why}")
    if op in SEND_OPS and not approved:
        log.warning("[POLICY] blocked %s for '%s': explicit approval required", op, account)
        return RunResult(1, "", f"policy: {op} requires explicit approval for every email")
    if guard_mutation(op, detail):
        return RunResult(0, skipped_text, "")
    return None


# (command, subcommand) pairs that change a mailbox or send mail.
_ORGANIZE_CMDS = {
    ("message", "move"),
    ("message", "copy"),
    ("message", "delete"),
    ("message", "save"),
    ("message", "add"),
    ("flag", "add"),
    ("flag", "remove"),
    ("flag", "set"),
    ("imap", "create"),
    ("imap", "move"),
    ("imap", "delete"),
    ("imap", "expunge"),
    ("imap", "purge"),
    ("mailbox", "create"),
    ("mailbox", "delete"),
    ("mailbox", "expunge"),
    ("mailbox", "purge"),
}
_SEND_CMDS = {
    ("message", "send"),
    ("message", "reply"),
    ("message", "forward"),
    ("message", "write"),
    ("message", "mailto"),
}


def _raw_args_denial(args: list[str]) -> str | None:
    """Policy check for a raw himalaya argv (``-a <account> <cmd> <sub> ...``)."""
    try:
        account = args[args.index("-a") + 1] if "-a" in args else ""
    except IndexError:
        account = ""
    if not account:
        return None
    words = [a for a in args if not a.startswith("-")]
    # words: [<account>, <cmd>, <sub>, ...] (flag values like mailbox names
    # can appear too, so match any adjacent pair rather than fixed positions)
    pairs = set(zip(words, words[1:]))
    if pairs & _ORGANIZE_CMDS:
        return denial(account, "move")
    if pairs & _SEND_CMDS:
        return denial(account, "send_raw")
    return None


@dataclass
class RunResult:
    code: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.code == 0


class MailboxUnavailable(RuntimeError):
    """The mailbox could not be reached — NOT the same as "it is empty".

    Raised by :meth:`Himalaya.envelopes_checked`. Added after a live
    incident in which ``mail.coinography.com:993`` became unreachable and
    every layer above silently treated the failure as an empty inbox:

      * ``envelopes()`` returns ``[]`` when the CLI fails, so
      * ``sync_worker.process_account`` hit its "inbox is empty" early
        return and finished *successfully*, so
      * ``sync_once`` called ``record_sync_success`` and the health
        subsystem never opened an incident, so
      * no alert was raised while 32 of 39 mailboxes went 28+ hours
        without syncing, and
      * the dashboard reported "0 emails" for mail that was really there.

    Carrying the failure as an exception is what lets each caller choose:
    the sync worker records a health failure, and the cache keeps the counts
    it already had instead of overwriting them with zeroes.
    """

    def __init__(self, account: str, mailbox: str, detail: str) -> None:
        super().__init__(f"{account}/{mailbox}: {detail}")
        self.account = account
        self.mailbox = mailbox
        self.detail = detail


#: Fragments that mean "the server/credentials are the problem", as opposed
#: to "that folder does not exist here" (which is a legitimate empty result).
_TRANSPORT_MARKERS = (
    "connect",
    "connection",
    "timed out",
    "timeout",
    "os error 10060",
    "os error 10061",
    "dns",
    "resolve",
    "certificate",
    "tls",
    "ssl",
    "authentication",
    "authenticate",
    "login",
    "password",
    "credential",
    "permission denied",
    "himalaya not found",
    "no such host",
    "network",
    "refused",
    "unreachable",
)


def is_transport_failure(result: "RunResult") -> bool:
    """True when a failed Himalaya run indicates a server/auth problem.

    A missing folder is NOT a transport failure: several accounts simply do
    not have the priority folders yet, and that must keep reading as "no
    mail here" rather than raising.
    """
    if result.ok:
        return False
    if result.code in (124, 127):  # our own timeout / binary missing
        return True
    blob = f"{result.stderr}\n{result.stdout}".lower()
    if "does not exist" in blob or "no such mailbox" in blob or "not found: mailbox" in blob:
        return False
    return any(marker in blob for marker in _TRANSPORT_MARKERS)


class Himalaya:
    """Windows-safe Himalaya v2 wrapper (absolute -c, cwd resolved per call).

    The config dir is created on demand and the working directory always
    exists — we never assume a repo-local himalaya/ directory. Paths are
    read dynamically from mailops.paths so tests can monkeypatch them.
    """

    def __init__(self) -> None:
        self.bin = himalaya_bin()

    @staticmethod
    def _config_dir() -> Path:
        paths.HIMALAYA_DIR.mkdir(parents=True, exist_ok=True)
        return paths.HIMALAYA_DIR

    def _proc_env(self) -> dict[str, str]:
        env = os.environ.copy()
        env["HOME"] = str(paths.PROFILE)
        env["XDG_CONFIG_HOME"] = str(paths.HIMALAYA_DIR.parent)
        bin_dir = str(Path(self.bin).parent)
        env["PATH"] = bin_dir + os.pathsep + env.get("PATH", "")
        # Add repo root to PYTHONPATH so child processes (like token.command) can import mailops
        env["PYTHONPATH"] = str(paths.MAILOPS_ROOT) + (os.pathsep + env["PYTHONPATH"] if "PYTHONPATH" in env else "")
        return env

    def run(self, args: list[str], timeout: int = 60, input_text: str | None = None) -> RunResult:
        # Backstop: even a raw run([...]) can't mutate/send for a denied account.
        why = _raw_args_denial(args)
        if why:
            log.warning("[POLICY] blocked raw himalaya call: %s", why)
            return RunResult(1, "", f"policy: {why}")
        config_dir = self._config_dir()
        env = self._proc_env()
        cmd = [self.bin, "-c", "./config.toml", *args]
        try:
            proc = subprocess.run(
                cmd,
                input=input_text,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                env=env,
                cwd=str(config_dir),
            )
            return RunResult(proc.returncode, proc.stdout or "", proc.stderr or "")
        except FileNotFoundError:
            return RunResult(127, "", "himalaya not found")
        except subprocess.TimeoutExpired:
            return RunResult(124, "", "himalaya timed out")

    def account_ids(self) -> list[str]:
        from .config import load_accounts

        return [a.id for a in load_accounts()]

    def mailbox_list(self, account: str) -> list[str]:
        r = self.run(["-a", account, "mailbox", "list", "--json"])
        if not r.ok:
            # fallback imap list
            r2 = self.run(["-a", account, "imap", "list", "--json"])
            if not r2.ok:
                return []
            try:
                data = json.loads(r2.stdout)
            except json.JSONDecodeError:
                return []
            names: list[str] = []
            rows = data if isinstance(data, list) else data.get("mailboxes") or data.get("list") or []
            for row in rows:
                if isinstance(row, dict):
                    n = row.get("name") or row.get("mailbox") or row.get("id")
                    if n:
                        names.append(str(n))
                elif isinstance(row, str):
                    names.append(row)
            return names
        try:
            data = json.loads(r.stdout)
        except json.JSONDecodeError:
            return []
        names = []
        rows = data if isinstance(data, list) else data.get("mailboxes") or []
        for row in rows:
            if isinstance(row, dict):
                n = row.get("name") or row.get("id")
                if n:
                    names.append(str(n))
        return names

    def create_mailbox(self, account: str, name: str) -> RunResult:
        blocked = _blocked(
            "create_mailbox", account, f"{account} {name}", "test-mode: skipped create"
        )
        if blocked:
            return blocked
        return self.run(["-a", account, "imap", "create", name], timeout=45)

    def envelopes(
        self, account: str, mailbox: str = "INBOX", page_size: int = 25, page: int = 1
    ) -> list[dict[str, Any]]:
        """Envelope list, or ``[]`` on any failure.

        Kept lenient for callers that genuinely want "best effort, never
        raise". Callers that must distinguish an unreachable server from an
        empty folder should use :meth:`envelopes_checked`.
        """
        try:
            return self.envelopes_checked(account, mailbox, page_size, page)
        except MailboxUnavailable:
            return []

    def envelopes_checked(
        self, account: str, mailbox: str = "INBOX", page_size: int = 25, page: int = 1
    ) -> list[dict[str, Any]]:
        """Envelope list, raising :class:`MailboxUnavailable` on a transport
        or authentication failure.

        An empty list from this method genuinely means "this folder has no
        mail", which is what lets the sync worker and the cache tell a dead
        server apart from an empty inbox.
        """
        r = self.run(
            [
                "-a",
                account,
                "envelope",
                "list",
                "-m",
                mailbox,
                "-s",
                str(page_size),
                "-p",
                str(page),
                "--json",
            ]
        )
        if not r.ok:
            if is_transport_failure(r):
                detail = (r.stderr or r.stdout or "").strip().splitlines()
                raise MailboxUnavailable(
                    account, mailbox, detail[0] if detail else f"exit {r.code}"
                )
            return []
        try:
            data = json.loads(r.stdout)
        except json.JSONDecodeError:
            return []
        rows = data.get("envelopes") if isinstance(data, dict) else data
        return rows if isinstance(rows, list) else []

    def uid_validity(self, account: str, mailbox: str = "INBOX") -> str:
        """The folder's IMAP UIDVALIDITY, or '' when it cannot be determined.

        Himalaya v2 does not expose UIDVALIDITY in any JSON output we can
        rely on (``envelope list --json`` carries only ``id``), so this
        returns '' today and ``mailops.identity`` falls back to comparing the
        RFC 5322 Message-ID / subject / sender / date — which is what
        actually protects against opening the wrong email.

        The hook exists so that the moment a UIDVALIDITY source is available
        (a Himalaya flag, or a direct IMAP probe), stored references become
        epoch-scoped without touching the call sites. Tests inject a fake
        client that DOES report it, which is how the renumbering path is
        covered.
        """
        return ""

    def read_message(self, account: str, msg_id: str, mailbox: str = "INBOX") -> str:
        r = self.run(["-a", account, "message", "read", "-m", mailbox, str(msg_id)], timeout=60)
        return r.stdout if r.ok else (r.stderr or "Failed to read message")

    def read_message_json(self, account: str, msg_id: str, mailbox: str = "INBOX") -> str:
        r = self.run(
            ["-a", account, "message", "read", "-m", mailbox, "--json", str(msg_id)],
            timeout=60,
        )
        return r.stdout if r.ok else ""

    def read_card(self, account: str, msg_id: str, mailbox: str = "INBOX"):
        from .mail_format import MailCard, parse_message_json, strip_raw_headers, clean_body

        raw_json = self.read_message_json(account, msg_id, mailbox)
        card = parse_message_json(raw_json) if raw_json else None
        if card is not None:
            return card
        raw = self.read_message(account, msg_id, mailbox)
        body = strip_raw_headers(raw)
        return MailCard(
            from_name="",
            from_email="",
            subject="(open for details)",
            date="",
            body=clean_body(body) or raw[:2000],
        )

    def move(
        self, account: str, msg_id: str, to_mailbox: str, from_mailbox: str = "INBOX"
    ) -> RunResult:
        blocked = _blocked(
            "move",
            account,
            f"{account} {from_mailbox}/{msg_id} -> {to_mailbox}",
            "test-mode: skipped move",
        )
        if blocked:
            return blocked
        # Prefer shared API; fall back to imap MOVE with clearer errors
        r = self.run(
            [
                "-a",
                account,
                "message",
                "move",
                "--from",
                from_mailbox,
                "--to",
                to_mailbox,
                str(msg_id),
            ]
        )
        if r.ok:
            return r
        r2 = self.run(
            [
                "-a",
                account,
                "imap",
                "move",
                "-m",
                from_mailbox,
                str(msg_id),
                to_mailbox,
            ]
        )
        if r2.ok:
            return r2
        return RunResult(
            r2.code or r.code,
            r.stdout + r2.stdout,
            (r.stderr or "") + " | " + (r2.stderr or ""),
        )

    def copy(
        self, account: str, msg_id: str, to_mailbox: str, from_mailbox: str = "INBOX"
    ) -> RunResult:
        blocked = _blocked(
            "copy",
            account,
            f"{account} {from_mailbox}/{msg_id} -> {to_mailbox}",
            "test-mode: skipped copy",
        )
        if blocked:
            return blocked
        return self.run(
            [
                "-a",
                account,
                "message",
                "copy",
                "--from",
                from_mailbox,
                "--to",
                to_mailbox,
                str(msg_id),
            ]
        )

    def flag_add(
        self, account: str, msg_id: str, flag: str = "flagged", mailbox: str = "INBOX"
    ) -> RunResult:
        blocked = _blocked(
            "flag_add",
            account,
            f"{account} {mailbox}/{msg_id} +{flag}",
            "test-mode: skipped flag add",
        )
        if blocked:
            return blocked
        return self.run(
            ["-a", account, "flag", "add", "-m", mailbox, "--flag", flag, str(msg_id)]
        )

    def flag_remove(
        self, account: str, msg_id: str, flag: str = "flagged", mailbox: str = "INBOX"
    ) -> RunResult:
        blocked = _blocked(
            "flag_remove",
            account,
            f"{account} {mailbox}/{msg_id} -{flag}",
            "test-mode: skipped flag remove",
        )
        if blocked:
            return blocked
        return self.run(
            ["-a", account, "flag", "remove", "-m", mailbox, "--flag", flag, str(msg_id)]
        )

    def reply_send(
        self,
        account: str,
        msg_id: str,
        body: str,
        mailbox: str,
        from_addr: str | None = None,
        *,
        save_mailbox: str = "INBOX.Sent",
        approved: bool = False,
    ) -> RunResult:
        """Reply via Himalaya. Body goes through a temp file (Windows cmdline-safe).

        Refused unless the account's send policy is on AND the caller passes
        ``approved=True`` (the user's explicit approval of THIS message).
        """
        import tempfile

        blocked = _blocked(
            "reply_send",
            account,
            f"{account} {mailbox}/{msg_id}",
            "test-mode: send blocked (not sent)",
            approved=approved,
        )
        if blocked:
            return blocked
        tmp: Path | None = None
        try:
            fd, tmp_name = tempfile.mkstemp(prefix="mailops-reply-", suffix=".txt")
            os.close(fd)
            tmp = Path(tmp_name)
            tmp.write_text(body or "", encoding="utf-8", newline="\n")
            args = [
                "-a",
                account,
                "message",
                "reply",
                "-m",
                mailbox,
                str(msg_id),
                "--body-file",
                str(tmp),
                "--send",
            ]
            if save_mailbox:
                args.extend(["--save", save_mailbox])
            if from_addr:
                args.extend(["--from", from_addr])
            return self.run(args, timeout=90)
        finally:
            if tmp is not None:
                try:
                    tmp.unlink(missing_ok=True)
                except OSError:
                    pass

    def send_raw(
        self,
        account: str,
        raw_message: str,
        save_mailbox: str | None = "Sent",
        *,
        approved: bool = False,
    ) -> RunResult:
        blocked = _blocked(
            "send_raw",
            account,
            f"{account}",
            "test-mode: send blocked (not sent)",
            approved=approved,
        )
        if blocked:
            return blocked
        args = ["-a", account, "message", "send"]
        if save_mailbox:
            args.extend(["--save", save_mailbox])
        return self.run(args, timeout=90, input_text=raw_message)
