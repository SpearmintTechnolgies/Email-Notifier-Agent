from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# Tests must never touch a real mailbox.
os.environ["MAILOPS_TEST_MODE"] = "1"

# Secrets vault: force the soft (Fernet) path in tests so Windows DPAPI is
# never invoked from unit tests. The key material lives under tmp (set per
# test via tmp_profile) so no key leaks between runs.
os.environ.setdefault("MAILOPS_FORCE_SOFT_VAULT", "1")

# Tests must never append to the live data/mailops.log: logging is redirected
# to a throw-away file for the whole session (and mailops.paths.log_file()
# additionally refuses the live log under pytest).
import hashlib
import tempfile

TEST_LOG_DIR = Path(tempfile.mkdtemp(prefix="mailops-tests-log-"))
os.environ["MAILOPS_LOG_FILE"] = str(TEST_LOG_DIR / "mailops.log")

LIVE_LOG = ROOT / "data" / "mailops.log"
LIVE_LOG_SIZE_AT_START = LIVE_LOG.stat().st_size if LIVE_LOG.exists() else 0

# The live operational database. Tests must never write to it; see
# tests/test_zz_isolation.py.
LIVE_OPS_DB = ROOT / "data" / "ops.db"


def read_live_cache_snapshot() -> str | None:
    """The dashboard count snapshot stored in the LIVE ops.db, or None.

    Read-only connection, so merely checking cannot create or lock the file.
    Returns None when there is no live db or no snapshot row.
    """
    import sqlite3

    if not LIVE_OPS_DB.exists():
        return None
    try:
        conn = sqlite3.connect(f"file:{LIVE_OPS_DB}?mode=ro", uri=True)
        try:
            row = conn.execute(
                "SELECT value FROM digest_state WHERE key='cache_counts_v1'"
            ).fetchone()
        finally:
            conn.close()
    except Exception:  # noqa: BLE001 - table may not exist yet
        return None
    return None if row is None else str(row[0])


def _protected_files() -> list[Path]:
    """Real credentials, tokens, registry, settings and knowledge bases."""
    found: list[Path] = [ROOT / ".env", ROOT / "himalaya" / "config.toml",
                         ROOT / "himalaya" / "accounts.json", ROOT / "data" / "settings.json"]
    for folder in ("secrets", "tokens", "data/kb"):
        base = ROOT / folder
        if base.exists():
            found.extend(p for p in base.rglob("*") if p.is_file())
    return sorted(p for p in found if p.exists())


def snapshot_protected() -> dict[str, str]:
    """path -> content hash (+ mtime: even a byte-identical rewrite of a secret
    or token is a violation; settings.json is hash-only because the live bot
    may legitimately save it)."""
    out: dict[str, str] = {}
    for p in _protected_files():
        digest = hashlib.sha256(p.read_bytes()).hexdigest()
        out[str(p)] = digest if p.name == "settings.json" else f"{digest}@{p.stat().st_mtime_ns}"
    return out


# Taken when the test session starts; tests/test_zz_isolation.py compares it.
PROTECTED_AT_START = snapshot_protected()

# Baseline of the live dashboard snapshot, compared at session end.
LIVE_CACHE_SNAPSHOT_AT_START = read_live_cache_snapshot()


@pytest.fixture(autouse=True)
def _no_real_himalaya(monkeypatch):
    """Hard isolation: any attempt to launch the real himalaya binary in a
    unit test is a failure. FakeHimalaya doubles and the test-mode guards
    (which return before subprocess) are unaffected."""
    import mailops.himalaya_client as hc

    def _blocked(self, *args, **kwargs):
        raise AssertionError(
            "unit tests must not launch real Himalaya — inject FakeHimalaya"
        )

    monkeypatch.setattr(hc.Himalaya, "run", _blocked)


@pytest.fixture(autouse=True)
def _reset_mailbox_awaiting():
    """Clear the mailbox AWAITING singleton before AND after every test.
    This global lives inside ``mailops.bot.mailboxes`` and otherwise
    leaks between tests when a test sets it directly for shortcut setup.

    This is defense-in-depth: ``_setup`` in test_mailbox_onboarding also
    clears it, but tests in other modules (e.g. test_telegram_group) do
    not call ``_setup`` and would still observe leftover state."""
    from mailops.bot import mailboxes as _mb
    _mb.AWAITING.clear()
    yield
    _mb.AWAITING.clear()


@pytest.fixture()
def tmp_profile(tmp_path, monkeypatch):
    """Redirect all mailops state (env, himalaya config, data dir) to tmp."""
    import mailops.paths as paths
    import mailops.config as config

    # Isolate from any real environment / cross-test leakage.
    for var in (
        "TELEGRAM_BOT_TOKEN",
        "TELEGRAM_ALLOWED_USERS",
        "TELEGRAM_HOME_CHANNEL",
        "TELEGRAM_OWNER_ID",
        "OPENAI_API_KEY",
        "OPENAI_BASE_URL",
        "OPENAI_MODEL",
        "GROQ_API_KEY",
        "GOOGLE_CLIENT_ID",
        "GOOGLE_CLIENT_SECRET",
    ):
        monkeypatch.delenv(var, raising=False)

    profile = tmp_path / "profile"
    himalaya_dir = profile / "himalaya"
    data_dir = tmp_path / "data"
    himalaya_dir.mkdir(parents=True)
    data_dir.mkdir(parents=True)

    accounts = [
        {"id": "sales", "email": "sales@example.com", "display_name": "Sales", "default": True},
        {"id": "info", "email": "info@example.com", "display_name": "Info"},
    ]
    (himalaya_dir / "accounts.json").write_text(json.dumps(accounts), encoding="utf-8")
    (himalaya_dir / "config.toml").write_text("", encoding="utf-8")
    # Policy is deny-by-default (mailops/policy.py). The generic test accounts
    # opt in so legacy tests keep exercising the organize/send code paths;
    # policy tests override this explicitly (tests/test_account_policies.py).
    (data_dir / "settings.json").write_text(
        json.dumps(
            {
                "account_policies": {
                    "sales": {"organize": True, "send": True},
                    "info": {"organize": True, "send": True},
                }
            }
        ),
        encoding="utf-8",
    )
    (profile / ".env").write_text(
        "TELEGRAM_BOT_TOKEN=test-token\n"
        "TELEGRAM_ALLOWED_USERS=111\n"
        "OPENAI_API_KEY=\n"
        "OPENAI_BASE_URL=https://api.openai.com/v1\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(paths, "PROFILE", profile)
    monkeypatch.setattr(paths, "HIMALAYA_DIR", himalaya_dir)
    monkeypatch.setattr(paths, "CONFIG_TOML", himalaya_dir / "config.toml")
    monkeypatch.setattr(paths, "ACCOUNTS_JSON", himalaya_dir / "accounts.json")
    monkeypatch.setattr(paths, "ENV_FILE", profile / ".env")
    monkeypatch.setattr(paths, "DATA_DIR", data_dir)
    monkeypatch.setattr(paths, "OPS_DB", data_dir / "ops.db")
    monkeypatch.setattr(paths, "SETTINGS_JSON", data_dir / "settings.json")

    # config.py binds these names at import time
    monkeypatch.setattr(config, "ACCOUNTS_JSON", himalaya_dir / "accounts.json")
    monkeypatch.setattr(config, "ENV_FILE", profile / ".env")
    monkeypatch.setattr(config, "SETTINGS_JSON", data_dir / "settings.json")
    monkeypatch.setattr(config, "DATA_DIR", data_dir)

    import mailops.db as db_mod
    monkeypatch.setattr(db_mod, "DATA_DIR", data_dir)
    monkeypatch.setattr(db_mod, "OPS_DB", data_dir / "ops.db")

    import mailops.reply_kb as kb_mod
    monkeypatch.setattr(kb_mod, "DATA_DIR", data_dir)
    monkeypatch.setattr(kb_mod, "KB_DIR", data_dir / "kb")

    import mailops.gmail_oauth as goa
    monkeypatch.setattr(goa, "ACCOUNTS_JSON", himalaya_dir / "accounts.json")
    monkeypatch.setattr(goa, "CONFIG_TOML", himalaya_dir / "config.toml")
    monkeypatch.setattr(goa, "HIMALAYA_DIR", himalaya_dir)

    # Secret isolation: OAuth tokens and webmail passwords go to the temporary
    # profile, never to the real tokens/ and secrets/ directories.
    monkeypatch.setattr(paths, "TOKEN_DIR", profile / "tokens")
    monkeypatch.setattr(paths, "SECRETS_DIR", profile / "secrets")
    monkeypatch.setattr(goa, "TOKEN_DIR", profile / "tokens")

    import mailops.singleton as singleton_mod
    monkeypatch.setattr(singleton_mod, "LOCK_PATH", data_dir / "mailops.pid")

    # Per-test Fernet master key so no key is shared between tests and no
    # key hits ~/.config.
    monkeypatch.setenv("MAILOPS_KEY_DIR", str(tmp_path / "keys"))

    return profile


class FakeHimalaya:
    """In-memory mailbox double with the Himalaya client interface."""

    def __init__(self):
        # {account: {mailbox: {msg_id: envelope}}}
        self.store: dict[str, dict[str, dict[str, dict]]] = {}
        self.sent: list[dict] = []
        self.flags: dict[tuple, set] = {}
        self.fail_moves = False
        self.fail_run: dict[str, Any] = {}
        self.created: list[str] = []
        # Every mailbox-mutating call that reached the client, in order:
        # (op, account, ...). Read-only accounts must leave this empty.
        self.mutations: list[tuple] = []
        # Seconds of simulated IMAP latency per envelope listing / body read.
        # Used by the slow-IMAP regression tests to prove a callback is still
        # acknowledged promptly while the mailbox is slow.
        self.latency = 0.0
        #: Account ids whose server should behave as unreachable.
        self.unreachable: set[str] = set()
        # {(account, mailbox): "<uidvalidity>"} - '' / missing = unknown,
        # which is what the real Himalaya client reports today.
        self.validity: dict[tuple[str, str], str] = {}

    def uid_validity(self, account, mailbox="INBOX"):
        return self.validity.get((account, mailbox), "")

    def add_message(
        self,
        account,
        mailbox,
        msg_id,
        subject="",
        sender="",
        uid=None,
        message_id=None,
        date="",
    ):
        mid = str(msg_id)
        self.store.setdefault(account, {}).setdefault(mailbox, {})[mid] = {
            "id": mid,
            "subject": subject,
            "from": [{"email": sender}],
            "uid": uid if uid is not None else msg_id,
            "message-id": message_id if message_id is not None else f"<{mid}@mailops.test>",
            "date": date or "Mon, 01 Jan 2024 00:00:00 +0000",
        }

    # --- interface used by production code ---
    def envelopes(self, account, mailbox="INBOX", page_size=25, page=1):
        """Lenient listing: mirrors the real client by swallowing an outage."""
        from mailops.himalaya_client import MailboxUnavailable

        try:
            return self.envelopes_checked(account, mailbox, page_size, page)
        except MailboxUnavailable:
            return []

    def envelopes_checked(self, account, mailbox="INBOX", page_size=25, page=1):
        """Strict listing: raises when the account's server is 'down'.

        Set ``fake.unreachable = {"coinography"}`` to simulate the live
        incident where an IMAP host stopped answering and every layer above
        mistook the failure for an empty inbox.
        """
        from mailops.himalaya_client import MailboxUnavailable

        self._stall()
        if account in self.unreachable:
            raise MailboxUnavailable(
                account, mailbox, "connect mail.example.com:993 (os error 10060)"
            )
        msgs = list(self.store.get(account, {}).get(mailbox, {}).values())
        start = (page - 1) * page_size
        return msgs[start : start + page_size]

    def _stall(self) -> None:
        """Block like a real IMAP round trip through a Himalaya subprocess.

        Deliberately ``time.sleep`` (not ``asyncio.sleep``): the production
        client is synchronous, so this reproduces the event-loop stall that
        let callback queries expire before they were ever answered.
        """
        if self.latency:
            import time as _time

            _time.sleep(self.latency)

    def read_message(self, account, msg_id, mailbox="INBOX"):
        self._stall()
        env = self.store.get(account, {}).get(mailbox, {}).get(str(msg_id))
        return f"From: {env['from'][0]['email']}\nSubject: {env['subject']}\n\nbody text" if env else ""

    def read_card(self, account, msg_id, mailbox="INBOX"):
        from mailops.mail_format import MailCard

        self._stall()
        env = self.store.get(account, {}).get(mailbox, {}).get(str(msg_id))
        if not env:
            return MailCard("", "", "(missing)", "", "")
        return MailCard("", env["from"][0]["email"], env["subject"], "", "body text")

    def mailbox_list(self, account):
        if account in self.unreachable:
            return []
        return list(self.store.get(account, {}).keys())

    def create_mailbox(self, account, name):
        from mailops.himalaya_client import RunResult

        self.mutations.append(("create_mailbox", account, name))
        self.store.setdefault(account, {}).setdefault(name, {})
        self.created.append(name)
        return RunResult(0, "", "")

    def move(self, account, msg_id, to_mailbox, from_mailbox="INBOX"):
        from mailops.himalaya_client import RunResult

        self.mutations.append(("move", account, str(msg_id), from_mailbox, to_mailbox))
        if self.fail_moves:
            return RunResult(1, "", "simulated move failure")
        src = self.store.get(account, {}).get(from_mailbox, {})
        env = src.pop(str(msg_id), None)
        if env is None:
            return RunResult(1, "", "message not found")
        self.store[account].setdefault(to_mailbox, {})[str(msg_id)] = env
        return RunResult(0, "", "")

    def copy(self, account, msg_id, to_mailbox, from_mailbox="INBOX"):
        from mailops.himalaya_client import RunResult

        self.mutations.append(("copy", account, str(msg_id), from_mailbox, to_mailbox))
        env = self.store.get(account, {}).get(from_mailbox, {}).get(str(msg_id))
        if env is None:
            return RunResult(1, "", "message not found")
        self.store[account].setdefault(to_mailbox, {})[str(msg_id)] = env
        return RunResult(0, "", "")

    def flag_add(self, account, msg_id, flag="flagged", mailbox="INBOX"):
        from mailops.himalaya_client import RunResult

        self.mutations.append(("flag_add", account, str(msg_id), mailbox, flag))
        self.flags.setdefault((account, mailbox, str(msg_id)), set()).add(flag)
        return RunResult(0, "", "")

    def flag_remove(self, account, msg_id, flag="flagged", mailbox="INBOX"):
        from mailops.himalaya_client import RunResult

        self.mutations.append(("flag_remove", account, str(msg_id), mailbox, flag))
        self.flags.setdefault((account, mailbox, str(msg_id)), set()).discard(flag)
        return RunResult(0, "", "")

    def run(self, args, timeout=60, input_text=None):
        """Minimal double for the raw Himalaya CLI call used by
        ``mailops.health`` health probes. Tests opt into a simulated
        failure via ``fail_run[account_id] = Exception(...)`` (raised) or
        ``RunResult(...)`` (returned as a non-ok result)."""
        from mailops.himalaya_client import RunResult

        account = None
        if "-a" in args:
            account = args[args.index("-a") + 1]
        failure = self.fail_run.get(account) if account else None
        if isinstance(failure, BaseException):
            raise failure
        if isinstance(failure, RunResult):
            return failure
        return RunResult(0, "{\"envelopes\": []}", "")

    def reply_send(
        self, account, msg_id, body, mailbox, from_addr=None, *, save_mailbox="INBOX.Sent", approved=False
    ):
        from mailops.himalaya_client import RunResult

        if not approved:  # mirror the real client: no send without explicit approval
            return RunResult(1, "", "policy: reply_send requires explicit approval for every email")
        self.sent.append(
            {"account": account, "msg_id": msg_id, "body": body, "from": from_addr}
        )
        return RunResult(0, "Message successfully saved and sent", "")


def record_identity(him, account, mailbox, msg_id):
    """Persist the FakeHimalaya envelope as the recorded message identity."""
    from mailops.identity import identity_from_envelope, remember

    env = him.store[account][mailbox][str(msg_id)]
    remember(
        identity_from_envelope(
            account, mailbox, env, him.uid_validity(account, mailbox)
        )
    )
    return env


def remember_all(him):
    for account, boxes in him.store.items():
        for mailbox, msgs in boxes.items():
            for msg_id in msgs:
                record_identity(him, account, mailbox, msg_id)


@pytest.fixture(autouse=True)
def _reset_callback_and_nav_state():
    """Clear the process-wide callback/navigation ledgers between tests.

    ``mailops.bot.callbacks`` remembers which callback query ids have been
    answered (to make the answer exactly-once) and ``mailops.bot.nav`` keeps
    per-message list snapshots. Both are intentionally process-global in the
    running bot, so they must be reset per test or doubles that reuse query
    / message ids would leak state across tests.
    """
    from mailops.bot import callbacks as _cbq
    from mailops.bot import nav as _nav
    from mailops import cache as _cache

    _cbq.reset_state()
    _nav.clear()
    _cache.clear_abort()
    try:
        yield
    finally:
        _cbq.reset_state()
        _nav.clear()
        _cache.clear_abort()


@pytest.fixture()
def fake_him():
    from mailops import cache as cache_mod

    him = FakeHimalaya()
    cache_mod.set_provider(him)  # dashboard cache refresh uses the fake too
    try:
        yield him
    finally:
        cache_mod.set_provider(None)
