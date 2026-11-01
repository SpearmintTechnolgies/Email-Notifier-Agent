"""Verify the new regression tests FAIL against the pre-fix behaviour.

A regression test that passes either way is worthless. This temporarily
restores each original defect, runs the tests that are supposed to catch it,
and reports whether they did. Nothing is left modified.
"""
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(".").resolve()
PY = str(ROOT / ".venv" / "Scripts" / "python.exe")

# (label, file, old snippet, reintroduced-defect snippet, tests that must fail)
DEFECTS = [
    (
        "double-answer (second answerCallbackQuery for the same query)",
        "mailops/bot/callbacks.py",
        """        if self.answered:
            await self.post(text)
            return AckResult(acknowledged=True, sent=False, category=self._outcome)
        return await self.alert(text)""",
        """        # pre-fix behaviour: always answer again, then also post
        try:
            await self._q.answer(text[:200], show_alert=True)
        except Exception:
            pass
        await self.post(text)
        return AckResult(acknowledged=True, sent=True, category=self._outcome)""",
        ["test_same_query_is_never_answered_twice_by_two_layers"],
    ),
    (
        "BadRequest misclassified as NetworkError",
        "mailops/bot/callbacks.py",
        """    if isinstance(exc, RetryAfter):
        return RATE_LIMITED""",
        """    if isinstance(exc, NetworkError):
        return NETWORK
    if isinstance(exc, RetryAfter):
        return RATE_LIMITED""",
        ["test_expired_error_is_classified_as_expiry_not_network",
         "test_error_handler_separates_expiry_from_network"],
    ),
    (
        "no watchdog: query acknowledged only after IMAP finishes",
        "mailops/bot/app.py",
        """    ack.arm()""",
        """    pass  # watchdog disabled (pre-fix: ack only after the work)""",
        ["test_slow_imap_still_acknowledges_callback_promptly"],
    ),
    (
        "blocking IMAP on the event loop",
        "mailops/bot/app.py",
        """    return await asyncio.to_thread(
        collect_bucket, priority, account_id, force=force
    )""",
        """    return collect_bucket(priority, account_id, force=force)""",
        ["test_slow_imap_does_not_block_other_users_callbacks",
         "test_slow_imap_still_acknowledges_callback_promptly"],
    ),
    (
        "positional refs resolved from per-user state",
        "mailops/bot/app.py",
        """    return nav.resolve(
        getattr(chat, "id", None),
        getattr(message, "message_id", None) if message is not None else None,
        payload,
        fallback_refs=list(context.user_data.get("page_refs") or []),
    )""",
        """    pos = nav.parse_position(payload)
    refs = list(context.user_data.get("page_refs") or [])
    if pos is not None and 1 <= pos.index <= len(refs):
        return nav.Resolution(ref=refs[pos.index - 1])
    return nav.Resolution(problem=nav.EMPTY)""",
        ["test_two_owners_sharing_one_message_open_the_same_email",
         "test_stale_keyboard_tap_does_not_open_a_different_mailbox"],
    ),
    (
        "stale reference dead-ends instead of relocating",
        "mailops/bot/app.py",
        """    identity = recall(account_id, mailbox, msg_id)
    relocated = None
    if identity is not None:
        relocated = await asyncio.to_thread(
            _relocate, him, identity, mailbox
        )""",
        """    identity = recall(account_id, mailbox, msg_id)
    relocated = None""",
        ["test_message_moved_between_listing_and_opening_is_relocated"],
    ),
    (
        "reply target missing: response lost",
        "mailops/bot/callbacks.py",
        """    chat = getattr(message, "chat", None)
    bot = getattr(message, "get_bot", None)
    chat_id = getattr(chat, "id", None)
    if chat_id is None or bot is None:
        log.warning("reply target missing and no chat to fall back to")
        return None""",
        """    return None
    chat = getattr(message, "chat", None)
    bot = getattr(message, "get_bot", None)
    chat_id = getattr(chat, "id", None)
    if chat_id is None or bot is None:
        return None""",
        ["test_deleted_message_reply_falls_back_to_standalone",
         "test_deleted_message_callback_still_informs_the_user"],
    ),
    (
        "UIDVALIDITY change ignored",
        "mailops/identity.py",
        """    current_validity = uid_validity_of(him, account_id, mailbox)
    if base.uid_validity and current_validity and base.uid_validity != current_validity:""",
        """    current_validity = uid_validity_of(him, account_id, mailbox)
    if False:""",
        ["test_uidvalidity_change_invalidates_stored_references",
         "test_uidvalidity_change_does_not_open_the_wrong_email"],
    ),
]

backups = {}
results = []
try:
    for label, path, good, bad, tests in DEFECTS:
        p = ROOT / path
        if path not in backups:
            backups[path] = p.read_text(encoding="utf-8")
        src = p.read_text(encoding="utf-8")
        if good not in src:
            results.append((label, "SNIPPET-NOT-FOUND", tests))
            continue
        p.write_text(src.replace(good, bad, 1), encoding="utf-8")
        proc = subprocess.run(
            [PY, "-m", "pytest", "tests/test_callback_reliability.py",
             "-q", "--no-header", "-x", "-k", " or ".join(tests)],
            capture_output=True, text=True,
        )
        failed = proc.returncode != 0
        results.append((label, "CAUGHT" if failed else "MISSED", tests))
        # restore before the next defect
        p.write_text(backups[path], encoding="utf-8")
finally:
    for path, original in backups.items():
        (ROOT / path).write_text(original, encoding="utf-8")

print()
print("=" * 72)
print("DEFECT REINTRODUCTION CHECK (does each test catch its original bug?)")
print("=" * 72)
for label, status, tests in results:
    mark = {"CAUGHT": "PASS", "MISSED": "FAIL", "SNIPPET-NOT-FOUND": "SKIP"}[status]
    print(f"[{mark}] {label}")
    if status != "CAUGHT":
        print(f"        tests: {tests}")
missed = [r for r in results if r[1] != "CAUGHT"]
print()
print(f"{len(results) - len(missed)}/{len(results)} defects are caught by the new tests")
sys.exit(1 if missed else 0)
