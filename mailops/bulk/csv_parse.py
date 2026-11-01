"""Strict CSV parsing for bulk mailbox onboarding.

Design rules (threat model)
---------------------------
* The CSV may contain mailbox credentials.
* The uploaded file arrives through Telegram's authenticated Bot API.
* We never log raw CSV content, raw rows, raw passwords, or any download URL.
* The parser is defensive by design: it refuses files that could tip
  downstream tools into executing something unexpected.

Rejections
----------
We refuse (``CsvError``) when:
* file exceeds the configured byte or row cap;
* any line exceeds ``MAX_LINE_BYTES`` (defence against pathological inputs);
* required header fields are missing;
* unknown header columns appear (strict allow-list);
* a header or any CELL contains an ``=``, ``@``, ``+``, ``-``, ``\\t``, ``\\r``
  or ``|`` **leading character** when the cell was clearly intended as a value
  (Excel formula injection in the exported report);
* a path-traversal-looking value sneaks into ``account_id`` or ``provider``;
* the file carries a UTF-16/UTF-32 BOM (we only accept UTF-8 / ASCII;
  mixed encodings routinely break downstream TOML emission);
* email, port, host or username fails the shape checks the existing
  ``webmail_onboarding.validate_spec`` already imposes.

Nothing in here opens a network socket. The server-side validation
(``mailops.bulk.validator``) handles that.
"""
from __future__ import annotations

import csv
import io
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Iterator

from .. import webmail_onboarding as wm

log = logging.getLogger("mailops.bulk.csv")

# ------------------------------------------------------------------ limits

# Defaults ONLY; the service layer reads env vars and may tighten these.
MAX_ROWS = 1000
MAX_FILE_BYTES = 2 * 1024 * 1024    # 2 MB
MAX_LINE_BYTES = 4 * 1024           # 4 KB - a credential line is tiny

# Header fields the parser understands. Required subset is ``REQUIRED``.
REQUIRED = frozenset({"email", "password"})
OPTIONAL = frozenset(
    {
        "account_id",
        "display_name",
        "provider",
        "imap_host",
        "imap_port",
        "imap_username",
        "smtp_host",
        "smtp_port",
        "smtp_username",
    }
)
ALLOWED_HEADERS = REQUIRED | OPTIONAL

# Legacy / common synonyms get a soft rename rather than an outright reject;
# the CEO is going to export this from Excel, Google Sheets, Mailchimp or a
# hand-written note, and the header spelling varies.
HEADER_ALIASES: dict[str, str] = {
    "e-mail": "email",
    "mail": "email",
    "login": "email",
    "pass": "password",
    "pwd": "password",
    "mail_password": "password",
    "mailbox_password": "password",
    "name": "display_name",
    "label": "display_name",
    "id": "account_id",
    "account": "account_id",
    "host": "imap_host",
    "imap": "imap_host",
    "incoming": "imap_host",
    "imap_server": "imap_host",
    "incoming_server": "imap_host",
    "outgoing": "smtp_host",
    "smtp": "smtp_host",
    "smtp_server": "smtp_host",
    "outgoing_server": "smtp_host",
    "imap_user": "imap_username",
    "smtp_user": "smtp_username",
}

# A leading character that could trip Excel formula injection (CVE-2014-6362
# and a long tail of CSV-import exploits). The parser rejects on INPUT (the
# admin would have to retype the row anyway) and the reporter escapes on
# OUTPUT (defence in depth). ``@`` is deliberately NOT in this set because
# email addresses start with the local-part, not with ``@``.
_INJECT_LEAD = ("=", "+", "-", "|", "\t", "\r", "@")

# Keep ``account_id`` and ``provider`` tightly constrained: both are used
# to build filesystem paths (``secrets/<id>.imap.enc``) and TOML section
# names (``[accounts.<id>.imap]``). Nothing with path separators, dots,
# whitespace or shell metacharacters is accepted.
_SAFE_IDENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_\-]{0,31}$")
_SAFE_PROVIDER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_\-]{0,31}$")


class CsvError(ValueError):
    """A bulk CSV was rejected. Message is safe to show to the admin."""


@dataclass
class ParsedRow:
    """One parsed CSV row - credentials stay here only in memory."""

    row_no: int                   # 1-based CSV row number (header = row 0)
    email: str = ""
    password: str = ""            # NEVER persisted, NEVER logged
    account_id: str = ""          # may be empty - derived later
    display_name: str = ""
    provider: str = ""            # preset name, resolved later
    imap_host: str = ""
    imap_port: int = 0            # 0 => fall back to preset / default
    imap_username: str = ""
    smtp_host: str = ""
    smtp_port: int = 0
    smtp_username: str = ""
    problems: list[str] = field(default_factory=list)

    def safe_summary(self) -> dict[str, Any]:
        """Password-free dict used for previews, reports and audit."""
        return {
            "row_no": self.row_no,
            "email": self.email,
            "account_id": self.account_id,
            "display_name": self.display_name,
            "provider": self.provider,
            "imap_host": self.imap_host,
            "imap_port": self.imap_port or None,
            "imap_username": self.imap_username,
            "smtp_host": self.smtp_host,
            "smtp_port": self.smtp_port or None,
            "smtp_username": self.smtp_username,
        }


@dataclass
class ParsedCsv:
    rows: list[ParsedRow] = field(default_factory=list)
    invalid: list[ParsedRow] = field(default_factory=list)
    duplicates: list[tuple[int, str, str]] = field(default_factory=list)
    # (row_no, kind, value) where kind in {"email","account_id"}; value is the
    # already-safe email/id, never a secret.
    provider_counts: dict[str, int] = field(default_factory=dict)

    @property
    def total(self) -> int:
        return len(self.rows) + len(self.invalid)

    def preview_summary(self) -> dict[str, Any]:
        """Password-free summary used for the Telegram preview card."""
        return {
            "total": self.total,
            "valid": len(self.rows),
            "invalid": len(self.invalid),
            "duplicates": len(self.duplicates),
            "providers": dict(self.provider_counts),
            "sample_rows": [r.safe_summary() for r in self.rows[:5]],
            "invalid_sample": [
                {"row_no": r.row_no, "email": r.email, "problems": r.problems[:3]}
                for r in self.invalid[:5]
            ],
        }


# ---------------------------------------------------------------- helpers


def _reject_bom(data: bytes) -> None:
    # UTF-8 BOM is harmless; UTF-16/UTF-32 BOMs mean the file was saved with
    # a different encoding and will trip csv.reader.
    if data.startswith(b"\xff\xfe\x00\x00") or data.startswith(b"\x00\x00\xfe\xff"):
        raise CsvError("CSV uses a UTF-32 encoding; re-save as plain UTF-8 and retry.")
    if data.startswith(b"\xff\xfe") or data.startswith(b"\xfe\xff"):
        raise CsvError("CSV uses a UTF-16 encoding; re-save as plain UTF-8 and retry.")


def _strip_utf8_bom(data: bytes) -> bytes:
    return data[3:] if data.startswith(b"\xef\xbb\xbf") else data


def _normalise_header(raw: str) -> str:
    key = re.sub(r"\s+", "_", (raw or "").strip().lower()).strip("_")
    # Try both hyphen- and underscore-folded forms when matching aliases
    # so headers like 'e-mail', 'imap-user', 'account-id' all resolve.
    alias = HEADER_ALIASES.get(key)
    if alias is not None:
        return alias
    key = key.replace("-", "_")
    return HEADER_ALIASES.get(key, key)


def _check_cell_safety(column: str, value: str) -> None:
    """Reject spreadsheet-formula or control-prefix injections in a value.

    We only check the leading character: a value *containing* a ``-`` or
    ``@`` is fine (``info@example.com``, ``mail-cluster.hostinger.com``).
    The forbidden prefixes are the ones Excel/Sheets interpret as a formula
    or an escape sequence.
    """
    if not value:
        return
    lead = value[0]
    if lead in _INJECT_LEAD and column != "email":
        raise CsvError(
            f"Row refuses to parse: cell for '{column}' starts with the "
            f"character {lead!r} which is reserved to prevent spreadsheet "
            f"formula injection. Prefix the value with a space or remove it."
        )
    # Email: the local-part can legitimately start with letters/digits only;
    # a leading ``=`` or ``+`` here is a bulk-import attack dressed up as an
    # address.
    if column == "email" and lead in ("=", "+", "|", "\t", "\r"):
        raise CsvError(
            "Email cell starts with a formula/control character; refusing to parse."
        )


def _check_safe_ident(column: str, value: str, pattern: re.Pattern[str]) -> None:
    if value and not pattern.match(value):
        raise CsvError(
            f"'{column}' value {value!r} must be 1-32 chars of letters, "
            "digits, '-' or '_' (and start alphanumeric)."
        )


def _port(column: str, raw: str) -> int:
    raw = (raw or "").strip()
    if not raw:
        return 0
    if not raw.isdigit():
        raise CsvError(f"'{column}' must be a plain number between 1 and 65535.")
    n = int(raw)
    if not 1 <= n <= 65535:
        raise CsvError(f"'{column}' must be between 1 and 65535 (got {n}).")
    return n


# ---------------------------------------------------------------- public


def parse_bytes(
    data: bytes,
    *,
    max_rows: int = MAX_ROWS,
    max_file_bytes: int = MAX_FILE_BYTES,
    max_line_bytes: int = MAX_LINE_BYTES,
) -> ParsedCsv:
    """Parse ``data`` into a ``ParsedCsv``.

    Raises ``CsvError`` on anything the loader should refuse WITHOUT
    showing partial progress to the admin (missing headers, truly
    malformed files, size cap exceeded). Per-row validation problems
    land in ``ParsedCsv.invalid`` so the admin can see them in the
    preview without a global failure.
    """
    if not isinstance(data, (bytes, bytearray)):
        raise CsvError("Expected the uploaded document as bytes.")
    if len(data) == 0:
        raise CsvError("The uploaded file is empty.")
    if len(data) > max_file_bytes:
        raise CsvError(
            f"CSV is too large ({len(data)} bytes). "
            f"Hard cap is {max_file_bytes} bytes."
        )
    _reject_bom(data)
    data = _strip_utf8_bom(data)
    # Reject NUL bytes and lone CR streams: the former hints at a binary
    # file (xlsx, zip, pdf) renamed to .csv; the latter is a classic
    # macOS-9 line ending that csv.reader fails to split.
    if b"\x00" in data:
        raise CsvError(
            "CSV contains NUL bytes; this is probably not a plain-text "
            "CSV (did you upload an .xlsx or .zip)? Export as CSV and retry."
        )
    # Decode strictly: any non-UTF-8 byte sequence is rejected rather than
    # replaced, so a smuggled Latin-1 password cannot land in the vault
    # under a different byte representation than the one at use time.
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise CsvError(
            "CSV is not valid UTF-8. Re-save with 'UTF-8' encoding "
            "(not 'UTF-8 with BOM', not 'ANSI')."
        ) from exc
    for raw_line in text.splitlines():
        if len(raw_line.encode("utf-8")) > max_line_bytes:
            raise CsvError(
                f"CSV has a line longer than {max_line_bytes} bytes; "
                "a credential row never needs that much data."
            )
    # Normalise CRLF/CR -> LF so csv.Sniffer / reader behave consistently.
    text = text.replace("\r\n", "\n").replace("\r", "\n")

    buf = io.StringIO(text)
    try:
        reader = csv.reader(buf)
        try:
            header = next(reader)
        except StopIteration as exc:
            raise CsvError("CSV has no header row.") from exc
    except csv.Error as exc:
        raise CsvError(f"CSV is malformed: {exc}") from exc

    norm_header = [_normalise_header(h) for h in header]
    unknown = [h for h in norm_header if h and h not in ALLOWED_HEADERS]
    if unknown:
        # Fail-closed: an unknown column could carry a secret the admin
        # thinks we'll handle (e.g. "api_token"). Rejecting is safer than
        # silently dropping it.
        raise CsvError(
            "Unknown CSV header column(s): "
            + ", ".join(sorted(set(unknown)))
            + ". Supported: " + ", ".join(sorted(ALLOWED_HEADERS))
            + "."
        )
    missing = REQUIRED - set(norm_header)
    if missing:
        raise CsvError(
            "CSV header is missing required column(s): " + ", ".join(sorted(missing))
        )
    if len(set(norm_header)) != len([h for h in norm_header if h]):
        raise CsvError("CSV header has duplicate column names.")

    out = ParsedCsv()
    seen_emails: dict[str, int] = {}
    seen_account_ids: dict[str, int] = {}

    for i, raw_row in enumerate(reader, start=1):
        if i > max_rows:
            raise CsvError(
                f"CSV has more than {max_rows} data rows; split the file."
            )
        if not raw_row or all(not str(c).strip() for c in raw_row):
            continue  # tolerate blank padding lines
        if len(raw_row) > len(norm_header):
            # Too many columns on one row almost always means the admin
            # forgot to quote a value that contains a comma. Refuse rather
            # than silently truncate.
            raise CsvError(
                f"Row {i} has {len(raw_row)} columns but the header has "
                f"{len(norm_header)}. Quote values that contain commas."
            )
        values = list(raw_row) + [""] * (len(norm_header) - len(raw_row))
        row: dict[str, str] = {}
        for col, raw_val in zip(norm_header, values):
            if not col:
                continue
            v = (raw_val or "").strip()
            row[col] = v

        try:
            parsed = _parse_single(i, row)
        except CsvError as exc:
            placeholder = ParsedRow(row_no=i, email=row.get("email", "")[:120])
            placeholder.problems.append(str(exc))
            out.invalid.append(placeholder)
            continue

        # Within-CSV dup detection (both email and derived/supplied id).
        email_key = parsed.email.lower()
        if email_key in seen_emails:
            out.duplicates.append((parsed.row_no, "email", email_key))
            parsed.problems.append(
                f"Duplicate of row {seen_emails[email_key]} in this CSV."
            )
            out.invalid.append(parsed)
            continue
        seen_emails[email_key] = parsed.row_no

        if parsed.account_id:
            aid_key = parsed.account_id.lower()
            if aid_key in seen_account_ids:
                out.duplicates.append((parsed.row_no, "account_id", aid_key))
                parsed.problems.append(
                    f"Duplicate account id of row {seen_account_ids[aid_key]}."
                )
                out.invalid.append(parsed)
                continue
            seen_account_ids[aid_key] = parsed.row_no

        provider_label = parsed.provider or "custom"
        out.provider_counts[provider_label] = out.provider_counts.get(provider_label, 0) + 1
        out.rows.append(parsed)

    if out.total == 0:
        raise CsvError("CSV has a header but no data rows.")
    return out


def _parse_single(row_no: int, row: dict[str, str]) -> ParsedRow:
    for col, val in row.items():
        _check_cell_safety(col, val)

    email = row.get("email", "")
    password = row.get("password", "")
    if not email:
        raise CsvError("Email is required.")
    if not wm._EMAIL_RE.match(email):
        raise CsvError(f"Email {email!r} looks invalid.")
    if not password:
        raise CsvError("Password is required.")
    if len(password) > 1024:
        raise CsvError("Password column exceeds 1024 bytes; probably not a password.")
    # Reject obviously-fake placeholders so the admin notices before auth.
    if password.lower() in ("password", "changeme", "123456", "test", "fixme"):
        raise CsvError("Password column looks like a placeholder; refusing to validate.")

    account_id = row.get("account_id", "").lower()
    _check_safe_ident("account_id", account_id, _SAFE_IDENT_RE)
    provider = row.get("provider", "").lower()
    _check_safe_ident("provider", provider, _SAFE_PROVIDER_RE)

    imap_host = row.get("imap_host", "").lower()
    smtp_host = row.get("smtp_host", "").lower()
    for label, host in (("imap_host", imap_host), ("smtp_host", smtp_host)):
        if host:
            portal_problem = wm.reject_webmail_portal_url(host, label=label)
            if portal_problem:
                raise CsvError(portal_problem)
            if not wm._HOST_RE.match(host):
                raise CsvError(
                    f"{label} {host!r} must be a plain hostname like mail.example.com."
                )

    imap_port = _port("imap_port", row.get("imap_port", ""))
    smtp_port = _port("smtp_port", row.get("smtp_port", ""))

    for label, user in (("imap_username", row.get("imap_username", "")),
                         ("smtp_username", row.get("smtp_username", ""))):
        if user and not wm._USER_RE.match(user):
            raise CsvError(f"{label} has unsupported characters.")

    display_name = row.get("display_name", "")
    if display_name and not wm._TEXT_RE.match(display_name):
        raise CsvError("display_name must not contain quotes, backslashes or control characters.")

    return ParsedRow(
        row_no=row_no,
        email=email,
        password=password,
        account_id=account_id,
        display_name=display_name,
        provider=provider,
        imap_host=imap_host,
        imap_port=imap_port,
        imap_username=row.get("imap_username", ""),
        smtp_host=smtp_host,
        smtp_port=smtp_port,
        smtp_username=row.get("smtp_username", ""),
    )


# ---------------------------------------------------------------- CSV template


CSV_TEMPLATE = (
    "email,password,display_name,provider,account_id,"
    "imap_host,imap_port,imap_username,smtp_host,smtp_port,smtp_username\n"
    "# Required: email,password. Optional columns may be left blank.\n"
    "# 'provider' names a server preset (hostinger, siteground, or any saved\n"
    "# custom preset). Explicit host/port columns always override the preset.\n"
    "# Account IDs are derived from the email local-part if omitted.\n"
    "# NEVER commit this file. The bot deletes the upload after ingest.\n"
    "info@example.com,REPLACE-ME,Example Mailbox,hostinger,,,,,,,\n"
    "hello@siteground-site.com,REPLACE-ME,SiteGround Mailbox,siteground,,,,,,,\n"
    "alpha@custom-domain.com,REPLACE-ME,Custom Mailbox,,alpha,"
    "mail.custom-domain.com,993,alpha@custom-domain.com,"
    "mail.custom-domain.com,465,alpha@custom-domain.com\n"
)


def render_template() -> bytes:
    """CSV template, UTF-8. Safe to send as a Telegram document."""
    return CSV_TEMPLATE.encode("utf-8")


# ---------------------------------------------------------------- safe report escaping


def report_escape(value: str) -> str:
    """Prevent formula injection in exported reports.

    Prepending a single quote keeps the value readable in Excel/Sheets
    while refusing to let it be interpreted as a formula.
    """
    if not value:
        return value
    if value[0] in _INJECT_LEAD:
        return "'" + value
    return value
