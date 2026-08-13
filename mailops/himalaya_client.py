from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from typing import Any

from .paths import HIMALAYA_DIR, PROFILE, himalaya_bin


@dataclass
class RunResult:
    code: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.code == 0


class Himalaya:
    """Windows-safe Himalaya v2 wrapper (relative -c from config dir)."""

    def __init__(self) -> None:
        self.bin = himalaya_bin()
        self.cwd = str(HIMALAYA_DIR)

    def run(self, args: list[str], timeout: int = 60) -> RunResult:
        env = os.environ.copy()
        env["HOME"] = str(PROFILE / "home")
        env["XDG_CONFIG_HOME"] = str(PROFILE / "home" / ".config")
        bin_dir = str(__import__("pathlib").Path(os.environ.get("LOCALAPPDATA", "")) / "hermes" / "bin")
        env["PATH"] = bin_dir + os.pathsep + env.get("PATH", "")
        cmd = [self.bin, "-c", "./config.toml", *args]
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                env=env,
                cwd=self.cwd,
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
        return self.run(["-a", account, "imap", "create", name], timeout=45)

    def envelopes(
        self, account: str, mailbox: str = "INBOX", page_size: int = 25, page: int = 1
    ) -> list[dict[str, Any]]:
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
            return []
        try:
            data = json.loads(r.stdout)
        except json.JSONDecodeError:
            return []
        rows = data.get("envelopes") if isinstance(data, dict) else data
        return rows if isinstance(rows, list) else []

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
        return self.run(
            ["-a", account, "flag", "add", "-m", mailbox, "--flag", flag, str(msg_id)]
        )

    def flag_remove(
        self, account: str, msg_id: str, flag: str = "flagged", mailbox: str = "INBOX"
    ) -> RunResult:
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
    ) -> RunResult:
        """Reply via Himalaya. Body goes through a temp file (Windows cmdline-safe)."""
        import tempfile
        from pathlib import Path

        tmp: Path | None = None
        try:
            fd, tmp_name = tempfile.mkstemp(prefix="mailops-reply-", suffix=".txt")
            tmp = Path(tmp_name)
            with open(fd, "w", encoding="utf-8", newline="\n") as f:
                f.write(body or "")
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

    def send_raw(self, account: str, raw_message: str, save_mailbox: str | None = "Sent") -> RunResult:
        args = ["-a", account, "message", "send"]
        if save_mailbox:
            args.extend(["--save", save_mailbox])
        env = os.environ.copy()
        env["HOME"] = str(PROFILE / "home")
        env["XDG_CONFIG_HOME"] = str(PROFILE / "home" / ".config")
        bin_dir = str(__import__("pathlib").Path(os.environ.get("LOCALAPPDATA", "")) / "hermes" / "bin")
        env["PATH"] = bin_dir + os.pathsep + env.get("PATH", "")
        cmd = [self.bin, "-c", "./config.toml", *args]
        try:
            proc = subprocess.run(
                cmd,
                input=raw_message,
                capture_output=True,
                text=True,
                timeout=90,
                env=env,
                cwd=self.cwd,
            )
            return RunResult(proc.returncode, proc.stdout or "", proc.stderr or "")
        except Exception as exc:  # noqa: BLE001
            return RunResult(1, "", str(exc))
