"""CSV-based bulk mailbox onboarding for Mailops.

Public surface (all submodules are safe to import without side effects):

* ``csv_parse`` - strict CSV reader with hard limits, formula/injection
  rejection, and a redacted preview.
* ``presets``   - resolver that merges per-row overrides with the saved
  ``mailbox_presets`` and the two built-in presets (hostinger, siteground).
* ``jobs``      - SQLite-backed job queue (bulk_jobs + bulk_rows), DPAPI
  sealed credentials, atomic row state transitions.
* ``validator`` - bounded-concurrency worker that drives jobs through
  the existing ``mailops.webmail_onboarding.add_account`` path (atomic
  registration + rollback, no test emails, no auth retries).
* ``service``   - high-level orchestration used by the Telegram UI
  (ingest a document, build the preview, confirm, pause/resume/cancel,
  retry failed rows, generate the final report).

Nothing in this package calls Telegram, writes to the live log, or touches a
real mailbox at import time. The Telegram glue lives in
``mailops.bot.bulk_mailboxes``.
"""
