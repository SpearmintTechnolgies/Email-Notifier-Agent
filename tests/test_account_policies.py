"""Per-account mailbox policies.

* ``gmail`` shares its inbox with the Sales-Agent: read, classify, cache and
  notify only - never move, copy, flag, delete, create folders or send.
* ``altcoininformer`` supports priority-folder organization, but it stays OFF
  until the owner explicitly approves production activation.
* Sending is off by default everywhere and every email needs explicit approval.
* Dashboards must show real classification results even though no priority
  folder exists (read-only accounts, not-yet-approved accounts, test mode).

The Sales-Agent finds replies/bounces with ``(in:inbox OR in:spam) -from:me
newer_than:30d`` (outreach/gmail.py), so anything that moves a Gmail message
out of INBOX would hide it from the Sales-Agent. The shared-inbox tests below
therefore assert the Gmail INBOX is byte-for-byte what it was.
"""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

import mailops.bot.app as app_mod
import mailops.himalaya_client as hc
from mailops.bot.app import group_callback_allowed
from mailops.config import load_settings, save_settings
from mailops.dashboard import collect_bucket, collect_counts, format_dashboard
from mailops.db import get_message_locations, init_db, save_draft
from mailops.himalaya_client import Himalaya, RunResult
from mailops.policy import (
    can_organize,
    can_send,
    denial,
    organizes_physically,
    policy_for,
)
from mailops.sync_worker import process_account, rescan_account
from mailops.taxonomy import bootstrap_all, ensure_taxonomy
from tests.conftest import remember_all

GMAIL = "gmail"
ALT = "altcoininformer"
GMAIL_ADDR = "growthpilotforyou@gmail.com"
ALT_ADDR = "info@altcoininformer.com"

# subjects the default rules classify as P0 / P1 / P3
CRITICAL = "security alert breach"
IMPORTANT = "how much to advertise"
NOISE = "newsletter digest"


def _run(coro):
    return asyncio.run(coro)


def _set_policies(policies: dict) -> None:
    s = load_settings()
    s["account_policies"] = policies
    save_settings(s)


@pytest.fixture()
def prod_profile(tmp_profile):
    """The real account layout with NO policy overrides (deny by default)."""
    (tmp_profile / "himalaya" / "accounts.json").write_text(
        json.dumps(
            [
                {"id": GMAIL, "email": GMAIL_ADDR, "display_name": "growthpilotforyou", "default": True},
                {"id": ALT, "email": ALT_ADDR, "display_name": "Altcoin Informer"},
            ]
        ),
        encoding="utf-8",
    )
    _set_policies({})
    init_db()
    return tmp_profile


def _seed(him, account, subjects=(CRITICAL, IMPORTANT, NOISE), start=1):
    for i, subj in enumerate(subjects, start=start):
        him.add_message(account, "INBOX", str(i), subject=subj, sender=f"s{i}@ext.example")


def _inbox_snapshot(him, account):
    return {
        mb: {mid: dict(env) for mid, env in msgs.items()}
        for mb, msgs in him.store.get(account, {}).items()
    }


# --------------------------------------------------------------------------
# 1. policy resolution
# --------------------------------------------------------------------------
def test_gmail_is_locked_read_only_and_cannot_be_unlocked(prod_profile):
    p = policy_for(GMAIL)
    assert p.locked and not p.organize and not p.send
    # Even an explicit settings override cannot unlock the shared inbox.
    _set_policies({GMAIL: {"organize": True, "send": True}})
    p = policy_for(GMAIL)
    assert p.locked and not p.organize and not p.send
    assert not can_organize(GMAIL) and not can_send(GMAIL)


@pytest.mark.parametrize("domain", ["gmail.com", "googlemail.com", "GMAIL.COM"])
def test_any_gmail_address_is_locked_by_default(prod_profile, domain):
    (prod_profile / "himalaya" / "accounts.json").write_text(
        json.dumps([{"id": "second", "email": f"someone@{domain}"}]), encoding="utf-8"
    )
    _set_policies({"second": {"organize": True, "send": True}})
    assert policy_for("second").locked
    assert not can_organize("second") and not can_send("second")


def test_altcoin_supports_organization_but_is_off_by_default(prod_profile):
    p = policy_for(ALT)
    assert not p.locked
    assert p.organize is False and p.send is False
    assert denial(ALT, "move") and denial(ALT, "copy") and denial(ALT, "flag_add")
    assert denial(ALT, "create_mailbox") and denial(ALT, "reply_send")


def test_explicit_activation_enables_only_what_was_approved(prod_profile):
    _set_policies({ALT: {"organize": True}})
    assert can_organize(ALT) and not can_send(ALT)
    assert denial(ALT, "move") is None
    assert denial(ALT, "reply_send") is not None
    _set_policies({ALT: {"send": True}})
    assert can_send(ALT) and not can_organize(ALT)


@pytest.mark.parametrize("bad", ["yes", "true", 1, "1", None, [], {}])
def test_only_the_json_literal_true_enables_a_capability(prod_profile, bad):
    _set_policies({ALT: {"organize": bad, "send": bad}})
    assert not can_organize(ALT) and not can_send(ALT)


def test_unreadable_policy_settings_deny(prod_profile):
    (prod_profile.parent / "data" / "settings.json").write_text("{not json", encoding="utf-8")
    assert not can_organize(ALT) and not can_send(ALT)
    assert policy_for(GMAIL).locked


def test_test_mode_means_nothing_is_organized_physically(prod_profile, monkeypatch):
    _set_policies({ALT: {"organize": True}})
    assert organizes_physically(ALT) is False  # MAILOPS_TEST_MODE=1 from conftest
    monkeypatch.delenv("MAILOPS_TEST_MODE")
    assert organizes_physically(ALT) is True
    assert organizes_physically(GMAIL) is False


# --------------------------------------------------------------------------
# 2. the Himalaya client enforces policy even if a caller forgets to
# --------------------------------------------------------------------------
@pytest.fixture()
def recorded_runs(monkeypatch):
    calls: list[list[str]] = []

    def fake_run(self, args, timeout=60, input_text=None):
        calls.append(list(args))
        return RunResult(0, "", "")

    monkeypatch.setattr(hc.Himalaya, "run", fake_run)
    return calls


@pytest.mark.parametrize("account", [GMAIL, ALT])
def test_client_refuses_every_mutation_for_denied_accounts(prod_profile, monkeypatch, recorded_runs, account):
    monkeypatch.delenv("MAILOPS_TEST_MODE")  # production: nothing but policy stands in the way
    him = Himalaya()
    results = [
        him.move(account, "1", "INBOX.P0-Critical", "INBOX"),
        him.copy(account, "1", "INBOX.Tag.Support", "INBOX"),
        him.flag_add(account, "1"),
        him.flag_remove(account, "1"),
        him.create_mailbox(account, "INBOX.P0-Critical"),
        him.reply_send(account, "1", "hi", "INBOX", approved=True),
        him.send_raw(account, "raw", approved=True),
    ]
    assert all(not r.ok and r.stderr.startswith("policy:") for r in results)
    assert recorded_runs == []  # no himalaya process was ever started


def test_client_allows_what_policy_allows_and_still_requires_approval(prod_profile, monkeypatch, recorded_runs):
    monkeypatch.delenv("MAILOPS_TEST_MODE")
    _set_policies({ALT: {"organize": True, "send": True}})
    him = Himalaya()
    assert him.move(ALT, "1", "INBOX.P0-Critical", "INBOX").ok
    assert him.create_mailbox(ALT, "INBOX.P0-Critical").ok
    n = len(recorded_runs)
    # send enabled but NOT approved -> refused, nothing sent
    r = him.reply_send(ALT, "1", "hi", "INBOX")
    assert not r.ok and "explicit approval" in r.stderr
    assert not him.send_raw(ALT, "raw").ok
    assert len(recorded_runs) == n
    # approved + enabled -> goes through
    assert him.reply_send(ALT, "1", "hi", "INBOX", approved=True).ok
    assert len(recorded_runs) == n + 1


def test_sends_are_off_by_default_even_for_an_approved_call(prod_profile, monkeypatch, recorded_runs):
    monkeypatch.delenv("MAILOPS_TEST_MODE")
    _set_policies({ALT: {"organize": True}})  # organization approved, sending not
    r = Himalaya().reply_send(ALT, "1", "hi", "INBOX", approved=True)
    assert not r.ok and "sending is disabled" in r.stderr
    assert recorded_runs == []


@pytest.mark.parametrize(
    "args",
    [
        ["-a", GMAIL, "message", "move", "--from", "INBOX", "--to", "INBOX.P0-Critical", "5"],
        ["-a", GMAIL, "message", "copy", "--from", "INBOX", "--to", "INBOX.Tag.Ops", "5"],
        ["-a", GMAIL, "message", "delete", "5"],
        ["-a", GMAIL, "flag", "add", "-m", "INBOX", "--flag", "flagged", "5"],
        ["-a", GMAIL, "flag", "remove", "-m", "INBOX", "--flag", "seen", "5"],
        ["-a", GMAIL, "imap", "create", "INBOX.P0-Critical"],
        ["-a", GMAIL, "imap", "move", "-m", "INBOX", "5", "INBOX.P1-Important"],
        ["-a", GMAIL, "mailbox", "delete", "INBOX.Waiting"],
        ["-a", GMAIL, "message", "send"],
        ["-a", GMAIL, "message", "reply", "-m", "INBOX", "5", "--send"],
        ["-a", ALT, "message", "move", "--from", "INBOX", "--to", "X", "5"],
        ["-a", ALT, "message", "send"],
    ],
)
def test_raw_himalaya_argv_for_denied_accounts_is_refused(prod_profile, args):
    assert hc._raw_args_denial(args) is not None


@pytest.mark.parametrize(
    "args",
    [
        ["-a", GMAIL, "envelope", "list", "-m", "INBOX", "-s", "30", "-p", "1", "--json"],
        ["-a", GMAIL, "message", "read", "-m", "INBOX", "--json", "5"],
        ["-a", GMAIL, "mailbox", "list", "--json"],
        ["-a", GMAIL, "account", "check"],
        ["-a", ALT, "account", "check"],
    ],
)
def test_raw_himalaya_reads_stay_allowed(prod_profile, args):
    assert hc._raw_args_denial(args) is None


def test_raw_argv_for_an_enabled_account_is_allowed(prod_profile):
    _set_policies({ALT: {"organize": True}})
    assert hc._raw_args_denial(["-a", ALT, "message", "move", "--from", "INBOX", "--to", "X", "5"]) is None
    assert hc._raw_args_denial(["-a", ALT, "message", "send"]) is not None  # send still off


def test_reading_mail_never_marks_it_seen(prod_profile, recorded_runs):
    """`message read` only touches flags when given --seen (which would change
    the Gmail inbox the Sales-Agent also reads)."""
    him = Himalaya()
    him.read_message(GMAIL, "5", "INBOX")
    him.read_message_json(GMAIL, "5", "INBOX")
    him.read_card(GMAIL, "5", "INBOX")
    him.envelopes(GMAIL, "INBOX")
    assert recorded_runs
    for args in recorded_runs:
        assert "--seen" not in args
        assert hc._raw_args_denial(args) is None


# --------------------------------------------------------------------------
# 3. sync pipeline: shared Gmail inbox is left exactly as it was
# --------------------------------------------------------------------------
@pytest.mark.parametrize("test_mode", [True, False], ids=["test-mode", "production"])
def test_gmail_sync_classifies_and_notifies_without_touching_the_inbox(prod_profile, fake_him, monkeypatch, test_mode):
    if not test_mode:
        monkeypatch.delenv("MAILOPS_TEST_MODE")
    _seed(fake_him, GMAIL)
    before = _inbox_snapshot(fake_him, GMAIL)
    alerts: list[dict] = []

    async def notify(payload):
        alerts.append(payload)

    processed = _run(process_account(fake_him, GMAIL, GMAIL_ADDR, notify))

    # classified, cached, notified ...
    assert len(processed) == 3
    locs = get_message_locations(GMAIL)
    assert locs["1"]["mailbox"] == "INBOX.P0-Critical"
    assert locs["2"]["mailbox"] == "INBOX.P1-Important"
    # Post-digest-redesign: no per-mail alert fires automatically; the
    # actionable rows land in the 15-minute digest queue instead. We
    # still want to prove the policy guarantee that the physical mailbox
    # is untouched, so we check digest_pending contents directly.
    from mailops.db import list_undelivered_digest_rows

    digest_rows = list_undelivered_digest_rows(
        account_ids=[GMAIL], max_priority=1, include_seeded=True
    )
    assert alerts == []
    assert {r["msg_id"] for r in digest_rows} == {"1", "2"}
    # ... and the digest rows reference the real INBOX, not a priority folder
    assert {r["mailbox"] for r in digest_rows} == {"INBOX"}
    # ... but no mailbox operation of any kind reached the client
    assert fake_him.mutations == []
    assert _inbox_snapshot(fake_him, GMAIL) == before
    assert fake_him.flags == {} and fake_him.created == [] and fake_him.sent == []


def test_gmail_rescan_never_moves_mail(prod_profile, fake_him, monkeypatch):
    monkeypatch.delenv("MAILOPS_TEST_MODE")
    _seed(fake_him, GMAIL)
    before = _inbox_snapshot(fake_him, GMAIL)
    items = _run(rescan_account(fake_him, GMAIL, GMAIL_ADDR))
    assert len(items) == 3
    assert fake_him.mutations == []
    assert _inbox_snapshot(fake_him, GMAIL) == before
    assert get_message_locations(GMAIL)["1"]["mailbox"] == "INBOX.P0-Critical"


def test_unapproved_altcoin_behaves_like_read_only(prod_profile, fake_him, monkeypatch):
    monkeypatch.delenv("MAILOPS_TEST_MODE")  # production, but organization not approved
    _seed(fake_him, ALT)
    before = _inbox_snapshot(fake_him, ALT)
    _run(process_account(fake_him, ALT, ALT_ADDR, None))
    assert fake_him.mutations == []
    assert _inbox_snapshot(fake_him, ALT) == before
    assert get_message_locations(ALT)["1"]["mailbox"] == "INBOX.P0-Critical"


def test_approved_altcoin_is_organized_into_priority_folders(prod_profile, fake_him, monkeypatch):
    """Positive control: once explicitly activated, Altcoin Informer IS organized
    (and the checks above would have noticed)."""
    monkeypatch.delenv("MAILOPS_TEST_MODE")
    _set_policies({ALT: {"organize": True}})
    _seed(fake_him, ALT)
    _run(process_account(fake_him, ALT, ALT_ADDR, None))
    assert any(m[0] == "move" for m in fake_him.mutations)
    assert [e["id"] for e in fake_him.envelopes(ALT, "INBOX.P0-Critical")] == ["1"]
    assert fake_him.envelopes(ALT, "INBOX") == [] or "1" not in {e["id"] for e in fake_him.envelopes(ALT, "INBOX")}


def test_approved_altcoin_in_test_mode_still_never_moves(prod_profile, fake_him, monkeypatch):
    _set_policies({ALT: {"organize": True}})  # test mode stays ON (conftest)
    real = Himalaya()  # conftest forbids launching himalaya: only guards run
    assert real.move(ALT, "1", "INBOX.P0-Critical", "INBOX").stdout.startswith("test-mode")
    assert real.create_mailbox(ALT, "INBOX.P0-Critical").stdout.startswith("test-mode")


def test_taxonomy_creates_no_folders_for_read_only_accounts(prod_profile, fake_him, monkeypatch):
    monkeypatch.delenv("MAILOPS_TEST_MODE")
    reports = ensure_taxonomy(him=fake_him)
    assert {r.account_id for r in reports} == {ALT}
    assert GMAIL not in {r.account_id for r in reports}
    assert all(r.skipped and not r.created for r in reports)
    assert fake_him.created == [] and fake_him.mutations == []


def test_taxonomy_bootstraps_only_the_approved_account(prod_profile, fake_him, monkeypatch):
    monkeypatch.delenv("MAILOPS_TEST_MODE")
    _set_policies({ALT: {"organize": True}})
    reports = {r.account_id: r for r in ensure_taxonomy(him=fake_him)}
    assert GMAIL not in reports
    assert not reports[ALT].skipped and reports[ALT].created
    assert all(m[1] == ALT for m in fake_him.mutations)
    assert GMAIL not in fake_him.store


def test_bootstrap_report_says_why_folders_were_skipped(prod_profile, monkeypatch):
    import mailops.taxonomy as tax

    monkeypatch.setattr(tax, "Himalaya", lambda: SimpleNamespace(mailbox_list=lambda a: [], create_mailbox=None))
    out = bootstrap_all()
    assert f"[{GMAIL}]" not in out
    assert f"[{ALT}]" in out and "skipped (no folders created)" in out


def test_real_himalaya_pipeline_issues_no_mutating_command_for_gmail(prod_profile, monkeypatch):
    """End to end with the REAL client in production mode: the exact himalaya
    commands that would run against Gmail contain no move/copy/flag/delete/
    folder-create/send - only reads."""
    monkeypatch.delenv("MAILOPS_TEST_MODE")
    envelopes = [
        {"id": str(i), "subject": s, "from": [{"email": f"s{i}@ext.example"}]}
        for i, s in enumerate((CRITICAL, IMPORTANT, NOISE), start=1)
    ]
    calls: list[list[str]] = []

    def fake_run(self, args, timeout=60, input_text=None):
        calls.append(list(args))
        if "envelope" in args:
            box = args[args.index("-m") + 1]
            page = int(args[args.index("-p") + 1])
            rows = envelopes if (box == "INBOX" and page == 1) else []
            return RunResult(0, json.dumps(rows), "")
        if "mailbox" in args:
            return RunResult(0, json.dumps([{"name": "INBOX"}]), "")
        return RunResult(0, "", "")

    monkeypatch.setattr(hc.Himalaya, "run", fake_run)
    seen_alerts: list[dict] = []

    async def notify(payload):
        seen_alerts.append(payload)

    processed = _run(process_account(Himalaya(), GMAIL, GMAIL_ADDR, notify))
    # Post-digest-redesign no per-mail alert fires - we assert that the
    # digest queue was populated instead, which is what the invariant
    # 'Gmail is read-only, nothing mutating runs' really protects.
    from mailops.db import list_undelivered_digest_rows

    assert len(processed) == 3
    assert seen_alerts == []
    assert list_undelivered_digest_rows(
        account_ids=[GMAIL], max_priority=1, include_seeded=True
    )
    assert calls  # it did read
    for args in calls:
        assert hc._raw_args_denial(args) is None, args
        words = [a for a in args if not a.startswith("-")]
        assert not (set(zip(words, words[1:])) & (hc._ORGANIZE_CMDS | hc._SEND_CMDS)), args
        assert "--seen" not in args


def test_real_himalaya_pipeline_positive_control_for_approved_altcoin(prod_profile, monkeypatch):
    """Same harness: an approved account's moves DO show up, so the Gmail
    assertion above is meaningful."""
    monkeypatch.delenv("MAILOPS_TEST_MODE")
    _set_policies({ALT: {"organize": True}})
    envelopes = [{"id": "1", "subject": CRITICAL, "from": [{"email": "a@ext.example"}]}]
    calls: list[list[str]] = []

    def fake_run(self, args, timeout=60, input_text=None):
        calls.append(list(args))
        if "envelope" in args:
            box = args[args.index("-m") + 1]
            return RunResult(0, json.dumps(envelopes if box == "INBOX" else []), "")
        if "mailbox" in args:
            return RunResult(0, json.dumps([{"name": "INBOX"}]), "")
        return RunResult(0, "", "")

    monkeypatch.setattr(hc.Himalaya, "run", fake_run)
    _run(process_account(Himalaya(), ALT, ALT_ADDR, None))
    pairs = {p for args in calls for p in zip([a for a in args if not a.startswith("-")], [a for a in args if not a.startswith("-")][1:])}
    assert ("message", "move") in pairs


# --------------------------------------------------------------------------
# 4. dashboards show real classification results (no priority folders exist)
# --------------------------------------------------------------------------
@pytest.mark.parametrize("test_mode", [True, False], ids=["test-mode", "production"])
def test_dashboard_counts_without_any_priority_folders(prod_profile, fake_him, monkeypatch, test_mode):
    if not test_mode:
        monkeypatch.delenv("MAILOPS_TEST_MODE")
    _seed(fake_him, GMAIL)
    _seed(fake_him, ALT, subjects=(IMPORTANT, IMPORTANT, NOISE))
    _run(process_account(fake_him, GMAIL, GMAIL_ADDR, None))
    _run(process_account(fake_him, ALT, ALT_ADDR, None))
    assert fake_him.mailbox_list(GMAIL) == ["INBOX"]  # no priority folder anywhere
    assert fake_him.mailbox_list(ALT) == ["INBOX"]

    counts = {c.account_id: c for c in collect_counts(force=True)}
    assert GMAIL not in counts
    a = counts[ALT]
    assert (a.p0, a.p1, a.p2, a.p3) == (0, 2, 0, 1)

    # Gmail was classified in the DB but is excluded from every dashboard surface.
    crit_gmail = collect_bucket(0, GMAIL, force=True)
    assert crit_gmail == []
    imp_all = collect_bucket(1, None, force=True)
    assert {i["account_id"] for i in imp_all} == {ALT}
    assert {i["mailbox"] for i in imp_all} == {"INBOX"}
    assert [(i["msg_id"], i["mailbox"], i["ref"]) for i in imp_all] == [
        ("1", "INBOX", f"{ALT}|INBOX|1"),
        ("2", "INBOX", f"{ALT}|INBOX|2"),
    ]

    for group in (False, True):
        text = format_dashboard(list(counts.values()), group=group)
        assert "gmail" not in text.lower() and GMAIL_ADDR not in text
        assert ALT_ADDR in text and "Altcoin Informer" in text
        assert "Critical 0 · Important 2 · Normal 0 · Noise 1" in text
        assert "read-only (organization off until approved)" in text
        assert "sending off" in text


def test_same_message_id_in_two_accounts_is_counted_per_account(prod_profile, fake_him):
    coin = "coinography"
    rows = json.loads((prod_profile / "himalaya" / "accounts.json").read_text(encoding="utf-8"))
    rows.append({"id": coin, "email": "info@coinography.com", "display_name": "Coinography"})
    (prod_profile / "himalaya" / "accounts.json").write_text(json.dumps(rows), encoding="utf-8")
    _seed(fake_him, coin, subjects=(CRITICAL,))
    _seed(fake_him, ALT, subjects=(NOISE,))  # also id "1"
    _run(process_account(fake_him, coin, "info@coinography.com", None))
    _run(process_account(fake_him, ALT, ALT_ADDR, None))
    counts = {c.account_id: c for c in collect_counts(force=True)}
    assert GMAIL not in counts
    assert (counts[coin].p0, counts[coin].p3) == (1, 0)
    assert (counts[ALT].p0, counts[ALT].p3) == (0, 1)


def test_unclassified_inbox_mail_is_not_counted_and_new_mail_appears(prod_profile, fake_him):
    _seed(fake_him, ALT, subjects=(CRITICAL,))
    assert collect_counts(force=True)[0].p0 == 0  # not classified yet
    _run(process_account(fake_him, ALT, ALT_ADDR, None))
    assert collect_counts(force=True)[0].p0 == 1


def test_dashboard_survives_mailbox_listing_failure(prod_profile, fake_him, monkeypatch):
    _seed(fake_him, ALT, subjects=(CRITICAL,))
    _run(process_account(fake_him, ALT, ALT_ADDR, None))

    def boom(account):
        raise RuntimeError("imap down")

    monkeypatch.setattr(fake_him, "mailbox_list", boom)
    assert collect_counts(force=True)[0].p0 == 1


def test_activated_altcoin_uses_real_folders_but_keeps_earlier_classification(prod_profile, fake_him, monkeypatch):
    """Approve organization later: mail classified while read-only (still in
    INBOX) stays visible, and newly organized mail is counted once."""
    monkeypatch.delenv("MAILOPS_TEST_MODE")
    _seed(fake_him, ALT, subjects=(CRITICAL,))
    _run(process_account(fake_him, ALT, ALT_ADDR, None))  # read-only phase
    assert collect_counts(force=True)[0].p0 == 1
    _set_policies({ALT: {"organize": True}})
    _seed(fake_him, ALT, subjects=(IMPORTANT,), start=2)
    _run(process_account(fake_him, ALT, ALT_ADDR, None))  # organizing phase
    c = {c.account_id: c for c in collect_counts(force=True)}[ALT]
    assert (c.p0, c.p1) == (1, 1)


# --------------------------------------------------------------------------
# 5. Telegram actions follow the same policy
# --------------------------------------------------------------------------
class _Msg:
    def __init__(self):
        self.sent: list[str] = []

    async def reply_text(self, text, reply_markup=None):
        self.sent.append(text)


class _Query:
    def __init__(self, data):
        self.data = data
        self.edits: list[str] = []
        self.markups: list = []
        self.answers: list[tuple] = []
        self.message = _Msg()

    async def answer(self, text=None, show_alert=False):
        self.answers.append((text, show_alert))

    async def edit_message_text(self, text, reply_markup=None):
        self.edits.append(text)
        self.markups.append(reply_markup)

    async def edit_message_reply_markup(self, reply_markup=None):
        self.edits.append("")

    @property
    def said(self) -> str:
        return " | ".join([t or "" for t, _ in self.answers] + self.message.sent + self.edits)


def _press(data, user_id=111):
    q = _Query(data)
    update = SimpleNamespace(
        callback_query=q,
        effective_user=SimpleNamespace(id=user_id),
        effective_message=q.message,
    )
    _run(app_mod.on_callback(update, SimpleNamespace(user_data={})))
    return q


@pytest.fixture()
def bot(prod_profile, fake_him, monkeypatch):
    monkeypatch.setattr(app_mod, "Himalaya", lambda: fake_him)
    app_mod._MAILBOX_NAMES.clear()
    _seed(fake_him, GMAIL, subjects=(IMPORTANT,))
    _seed(fake_him, ALT, subjects=(IMPORTANT,))
    remember_all(fake_him)
    return fake_him


@pytest.mark.parametrize(
    "data",
    [
        f"setprio:{GMAIL}|INBOX|1|0",
        f"settag:{GMAIL}|INBOX|1|support",
        f"done:{GMAIL}|INBOX|1",
        f"mute:{GMAIL}|INBOX|1",
        f"setprio:{ALT}|INBOX|1|0",
        f"settag:{ALT}|INBOX|1|support",
        f"done:{ALT}|INBOX|1",
        f"mute:{ALT}|INBOX|1",
    ],
)
def test_bot_never_reorganizes_read_only_accounts(bot, monkeypatch, data):
    monkeypatch.delenv("MAILOPS_TEST_MODE")
    before = _inbox_snapshot(bot, data.split(":")[1].split("|")[0])
    q = _press(data)
    assert bot.mutations == []
    assert _inbox_snapshot(bot, data.split(":")[1].split("|")[0]) == before
    account = data.split(":")[1].split("|")[0]
    if account == GMAIL:
        assert "no longer manages" in q.said
        return
    if not data.startswith("mute:"):
        assert "read-only" in q.said and "Nothing in the mailbox was changed" in q.said
    else:
        assert "not moved" in q.said


def test_bot_organizes_an_approved_account(bot, monkeypatch):
    monkeypatch.delenv("MAILOPS_TEST_MODE")
    _set_policies({ALT: {"organize": True}})
    _press(f"setprio:{ALT}|INBOX|1|0")
    assert [m[:2] for m in bot.mutations] == [("move", ALT)]


def _draft(account, msg_id="1"):
    save_draft(111, account, "INBOX", msg_id, "Thanks for reaching out.")


@pytest.mark.parametrize("account", [GMAIL, ALT])
def test_bot_does_not_send_when_sending_is_off(bot, monkeypatch, account):
    monkeypatch.delenv("MAILOPS_TEST_MODE")
    _draft(account)
    q = _press(f"send:{account}|INBOX|1")
    assert bot.sent == [] and bot.mutations == []
    if account == GMAIL:
        assert "no longer manages" in q.said
    else:
        assert "Sending is disabled" in q.said and "Nothing was sent" in q.said


def test_gmail_cannot_send_even_if_settings_try_to_enable_it(bot, monkeypatch):
    monkeypatch.delenv("MAILOPS_TEST_MODE")
    _set_policies({GMAIL: {"organize": True, "send": True}})
    _draft(GMAIL)
    q = _press(f"send:{GMAIL}|INBOX|1")
    assert bot.sent == [] and "no longer manages" in q.said


def test_send_needs_a_draft_for_exactly_that_account(bot, monkeypatch):
    monkeypatch.delenv("MAILOPS_TEST_MODE")
    _set_policies({ALT: {"send": True}})
    _draft(GMAIL, "1")  # the user reviewed a Gmail draft ...
    q = _press(f"send:{ALT}|INBOX|1")  # ... and a stale/forged button names Altcoin
    assert bot.sent == []
    assert "different account" in q.said
    q = _press(f"send:{ALT}|INBOX|9")  # wrong message
    assert bot.sent == []


def _final_send(q):
    for markup in reversed(q.markups):
        if not markup:
            continue
        for row in markup.inline_keyboard:
            for btn in row:
                data = btn.callback_data or ""
                if data.startswith("go:"):
                    return data
    raise AssertionError("final send button was not shown")


def test_approved_send_goes_out_once_without_touching_the_mailbox(bot, monkeypatch):
    monkeypatch.delenv("MAILOPS_TEST_MODE")
    _set_policies({ALT: {"send": True}})  # sending approved, organization still off
    _draft(ALT)
    preview = _press(f"send:{ALT}|INBOX|1")
    assert bot.sent == [] and bot.mutations == []
    assert "Final confirmation" in preview.said
    assert f"From: {ALT_ADDR}" in preview.said
    assert "To: s1@ext.example" in preview.said
    assert "Thanks for reaching out." in preview.said
    q = _press(_final_send(preview))
    assert [(s["account"], s["from"]) for s in bot.sent] == [(ALT, ALT_ADDR)]
    assert bot.mutations == []  # not filed to Waiting: organization is off
    assert "no mailbox changes" in q.said
    _press(_final_send(preview))
    assert len(bot.sent) == 1


def test_send_is_blocked_in_test_mode_even_when_enabled(bot):
    _set_policies({ALT: {"organize": True, "send": True}})
    _draft(ALT)
    q = _press(f"send:{ALT}|INBOX|1")
    assert bot.sent == [] and "TEST MODE is ON" in q.said


def test_unauthorized_user_cannot_trigger_a_send(bot, monkeypatch):
    monkeypatch.delenv("MAILOPS_TEST_MODE")
    _set_policies({ALT: {"send": True}})
    _draft(ALT)
    _press(f"send:{ALT}|INBOX|1", user_id=999)
    assert bot.sent == []


@pytest.mark.parametrize(
    "data",
        [
            "send:a|INBOX|1",
            "go:abcdef0123456789abcdef0123456789",
            "nogo:abcdef0123456789abcdef0123456789",
            "setprio:a|INBOX|1|0",
            "settag:a|INBOX|1|ops",
            "done:a|INBOX|1",
            "mute:a|INBOX|1",
            "aidraft:a|INBOX|1",
        ],
)
def test_groups_can_never_trigger_mailbox_changes_or_sends(data):
    assert not group_callback_allowed(data)


def test_stale_virtual_refs_resolve_to_the_real_mailbox(bot):
    app_mod._MAILBOX_NAMES.clear()
    # INBOX.P0-Critical does not exist on a read-only inbox: old dashboards put it in refs
    assert app_mod._parse_ref(f"{ALT}|INBOX.P0-Critical|1") == (ALT, "INBOX", "1")
    assert app_mod._parse_ref(f"{ALT}|INBOX|1") == (ALT, "INBOX", "1")
    # a real folder that exists is left alone
    bot.add_message(ALT, "INBOX.P1-Important", "7", subject="x", sender="a@b.co")
    app_mod._MAILBOX_NAMES.clear()
    assert app_mod._parse_ref(f"{ALT}|INBOX.P1-Important|7") == (ALT, "INBOX.P1-Important", "7")


# --------------------------------------------------------------------------
# 6. CLI
# --------------------------------------------------------------------------
def test_policy_command_reports_effective_policy(prod_profile, capsys):
    from mailops.main import main

    assert main(["policy"]) == 0
    out = capsys.readouterr().out
    assert f"{GMAIL}:" not in out
    assert f"{ALT}: organize=NO send=NO" in out
    assert "test mode: ON" in out
