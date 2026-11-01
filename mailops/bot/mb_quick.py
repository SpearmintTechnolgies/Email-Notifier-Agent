"""One-message "Quick Add Email" input parsing + server presets.

The group-first mailbox wizard asks for 8-9 fields in sequence. A busy admin
(e.g. the CEO) wants to paste ONE message with all the non-secret details
and move straight to the confirmation card.

This module is PURE and side-effect free (apart from the preset store):

* ``parse_quick_add_text(text)`` parses a free-form message into a dict of
  fields suitable for ``onboarding_session.set_fields`` plus optional
  overrides (account_id, imap_user, smtp_user). It REFUSES anything that
  looks like a password: group chat is not a credentials channel.

* ``derive_account_id(email, taken)`` turns an email local-part into a safe
  unique account id matching the webmail_onboarding ``_ID_RE`` pattern.

* ``presets_load`` / ``presets_save`` / ``presets_list`` / ``preset_delete``
  persist named server presets in ``settings.json`` so that an admin running
  the same SiteGround / Hostinger server for several brands can onboard a
  new mailbox with only an email address and a display name.

No Telegram, no DB, no files other than settings.json: the Telegram glue
lives in ``mailops.bot.mailboxes`` and consumes the parsed dict.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Any

from .. import config, webmail_onboarding as wm


# Reserve a dedicated AWAITING sentinel so handle_text can route a group
# message to this parser without colliding with the step-by-step wizard.
QUICK_ADD_FIELD = "__quick_add__"


# ----------------------------------------------------------- field aliases
#
# The CEO is going to paste "IMAP:", "imap host", "imap-host", "incoming
# server", etc. Normalise aggressively on parse. Each alias maps to the
# canonical session field name used by the existing onboarding wizard.
_FIELD_ALIASES: dict[str, str] = {
    "email": "email",
    "email address": "email",
    "e-mail": "email",
    "address": "email",
    "name": "display_name",
    "display name": "display_name",
    "display-name": "display_name",
    "label": "display_name",
    "imap": "imap_host",
    "imap host": "imap_host",
    "imap server": "imap_host",
    "incoming": "imap_host",
    "incoming host": "imap_host",
    "incoming server": "imap_host",
    "imap port": "imap_port",
    "imap-port": "imap_port",
    "incoming port": "imap_port",
    "smtp": "smtp_host",
    "smtp host": "smtp_host",
    "smtp server": "smtp_host",
    "outgoing": "smtp_host",
    "outgoing host": "smtp_host",
    "outgoing server": "smtp_host",
    "smtp port": "smtp_port",
    "smtp-port": "smtp_port",
    "outgoing port": "smtp_port",
    "imap user": "imap_user",
    "imap username": "imap_user",
    "imap-user": "imap_user",
    "smtp user": "smtp_user",
    "smtp username": "smtp_user",
    "smtp-user": "smtp_user",
    "account": "account_id",
    "account id": "account_id",
    "account-id": "account_id",
    "id": "account_id",
}

# Keys that are credentials: receiving ANY of these in a group message
# must abort the parse rather than silently drop them. The abort text is
# returned to the admin so they understand why. We DO NOT echo the value.
_FORBIDDEN_KEYS: frozenset[str] = frozenset(
    {
        "password",
        "pass",
        "pwd",
        "secret",
        "token",
        "api key",
        "api-key",
        "apikey",
        "imap password",
        "imap-password",
        "imap pass",
        "smtp password",
        "smtp-password",
        "smtp pass",
    }
)


# One canonical set of required fields. Presets can satisfy the server
# fields (host + port); email and display_name are always required so a
# preset alone cannot auto-onboard a mailbox.
_REQUIRED_WITHOUT_PRESET: tuple[str, ...] = (
    "email",
    "display_name",
    "imap_host",
    "imap_port",
    "smtp_host",
    "smtp_port",
)
_REQUIRED_WITH_PRESET: tuple[str, ...] = ("email", "display_name")


class QuickAddError(ValueError):
    """Parsing or validation failed; message is admin-safe (no secrets)."""


@dataclass
class QuickAddResult:
    """Parsed, normalised fields plus a few metadata flags.

    ``fields`` is ready to pass straight to ``onboarding_session.set_fields``.
    ``account_id_overridden`` is True iff the admin typed an ``account id:``
    line (we only generate one otherwise). ``save_preset_suggested`` is set
    when the admin did not use a preset and the hosts look worth saving.
    """

    fields: dict[str, Any] = field(default_factory=dict)
    account_id_overridden: bool = False
    save_preset_suggested: bool = False
    preset_used: str = ""


# --------------------------------------------------------------------------- parsing


_LINE_RE = re.compile(r"^\s*([A-Za-z][A-Za-z \-_]{0,30})\s*[:=]\s*(.+?)\s*$")


def _normalise_key(raw: str) -> str:
    k = re.sub(r"\s+", " ", raw.strip().lower()).replace("_", " ")
    return k.rstrip("- ")


def _parse_pairs(text: str) -> list[tuple[str, str]]:
    """Return the ordered list of (raw_key, raw_value) pairs we can find.

    Blank lines and lines that do not look like ``key: value`` are
    ignored, so the admin can prefix the message with "please onboard"
    or paste a provider welcome email verbatim; irrelevant lines are
    discarded without poisoning the parse. We do NOT raise on unknown
    keys here; that is done in ``parse_quick_add_text`` once we know
    whether a preset will satisfy them.
    """
    out: list[tuple[str, str]] = []
    for raw_line in (text or "").splitlines():
        m = _LINE_RE.match(raw_line)
        if not m:
            continue
        out.append((m.group(1), m.group(2)))
    return out


def _coerce_port(label: str, raw: str) -> int:
    stripped = raw.strip()
    if not stripped.isdigit():
        raise QuickAddError(
            f"{label} port must be a plain number between 1 and 65535 (got '{stripped[:16]}')."
        )
    n = int(stripped)
    if not 1 <= n <= 65535:
        raise QuickAddError(f"{label} port must be between 1 and 65535 (got {n}).")
    return n


def derive_account_id(email: str, taken: set[str]) -> str:
    """Turn ``info@alien.fi`` into a safe unique account id.

    Rules (match ``webmail_onboarding._ID_RE``: ``^[a-z0-9][a-z0-9_-]{0,31}$``):
      * use the local part if alphanumeric;
      * else prepend the domain's first label when the local part is too
        generic (``info``, ``admin``, ``contact``, ``hello``, ``support``,
        ``sales``, ``team``, ``office``);
      * strip anything outside ``[a-z0-9_-]``;
      * uniquify by appending ``2``, ``3`` ... when the id is already in
        ``taken`` (which should include registered account ids AND any
        pending onboarding session's account_id).
    """
    local, _, domain = (email or "").strip().lower().partition("@")
    first_label = domain.partition(".")[0] if domain else ""
    generic = {
        "info", "admin", "administrator", "contact", "hello",
        "support", "sales", "team", "office", "noreply", "no-reply",
    }
    if local and local not in generic:
        base = local
    elif first_label:
        base = first_label
    else:
        base = local or "mailbox"
    base = re.sub(r"[^a-z0-9_-]+", "-", base).strip("-_") or "mailbox"
    # Must start alphanumeric.
    if not base[0].isalnum():
        base = "m" + base
    base = base[:32]
    if base not in taken:
        return base
    # Need to make room for the suffix without blowing past 32 chars.
    for n in range(2, 1000):
        suffix = str(n)
        candidate = (base[: 32 - len(suffix)] + suffix)
        if candidate not in taken:
            return candidate
    raise QuickAddError("could not generate a unique account id")


def parse_quick_add_text(
    text: str,
    *,
    preset: dict[str, Any] | None = None,
    taken_ids: set[str] | None = None,
) -> QuickAddResult:
    """Parse one Telegram group message into a mailbox-wizard field set.

    ``preset`` (when non-empty) supplies imap_host, imap_port, smtp_host,
    smtp_port (and optionally imap_user / smtp_user); any of those keys
    the admin ALSO types overrides the preset.

    Raises ``QuickAddError`` with an admin-safe message. We never echo
    unknown values back, so an accidentally-pasted password line does
    not land in the error string.
    """
    if not (text or "").strip():
        raise QuickAddError(
            "Empty message. Paste the mailbox details, one 'Key: value' per line."
        )
    pairs = _parse_pairs(text)
    if not pairs:
        raise QuickAddError(
            "No 'Key: value' lines found. Example:\n"
            "Email: info@example.com\nName: Example\nIMAP: mail.example.com\n"
            "IMAP Port: 993\nSMTP: mail.example.com\nSMTP Port: 465"
        )

    taken = set(taken_ids or set())
    gathered: dict[str, Any] = {}
    account_id_overridden = False
    unknown: list[str] = []

    for raw_key, raw_value in pairs:
        key = _normalise_key(raw_key)
        # Credentials in a group message — abort cleanly WITHOUT echoing
        # the value anywhere. The caller surfaces this with an alert.
        if key in _FORBIDDEN_KEYS or key.endswith(" password") or key.endswith(" pass"):
            raise QuickAddError(
                "Passwords are NEVER accepted in a group message. "
                "Remove the password line and resend. Password capture happens "
                "privately in your DM with the bot after you confirm."
            )
        canonical = _FIELD_ALIASES.get(key)
        if canonical is None:
            unknown.append(key)
            continue
        value = raw_value.strip()
        if canonical in ("imap_port", "smtp_port"):
            gathered[canonical] = _coerce_port("IMAP" if canonical == "imap_port" else "SMTP", value)
        elif canonical in ("imap_host", "smtp_host"):
            gathered[canonical] = value.lower()
        elif canonical == "email":
            gathered[canonical] = value
        elif canonical == "account_id":
            # Account id override: validated downstream by webmail_onboarding.
            gathered[canonical] = value.lower()
            account_id_overridden = True
        else:
            gathered[canonical] = value

    # Fold preset in UNDER user input: user overrides preset for every key.
    merged: dict[str, Any] = {}
    if preset:
        for k in ("imap_host", "imap_port", "smtp_host", "smtp_port", "imap_user", "smtp_user"):
            if k in preset and preset[k] not in (None, ""):
                merged[k] = preset[k]
    merged.update(gathered)

    # Required-fields check depends on whether the preset covered servers.
    required = _REQUIRED_WITH_PRESET if preset else _REQUIRED_WITHOUT_PRESET
    missing = [k for k in required if not str(merged.get(k, "")).strip()]
    if missing:
        pretty = ", ".join(
            {
                "email": "Email",
                "display_name": "Name",
                "imap_host": "IMAP",
                "imap_port": "IMAP Port",
                "smtp_host": "SMTP",
                "smtp_port": "SMTP Port",
            }.get(m, m)
            for m in missing
        )
        raise QuickAddError(
            f"Missing required field(s): {pretty}. "
            "Add them and resend the whole message."
        )

    # Validate email shape here so a totally malformed address does not
    # make it as far as building a WebmailSpec (which would ultimately
    # still refuse). This gives a better error message to the admin.
    email_val = str(merged.get("email", "")).strip()
    if not wm._EMAIL_RE.match(email_val):
        raise QuickAddError(
            "That email address looks invalid. Use the format "
            "name@example.com."
        )

    # Validate hostnames and ports now so review-screen input is clean.
    for label, hkey in (("IMAP", "imap_host"), ("SMTP", "smtp_host")):
        host = str(merged.get(hkey, "")).strip()
        if not wm._HOST_RE.match(host):
            raise QuickAddError(
                f"{label} host must be a plain hostname like mail.example.com "
                "(no scheme, no port, no path)."
            )

    # Default imap_user / smtp_user to the email so the overrides-only path
    # described in the UX works without extra fields.
    if not merged.get("imap_user"):
        merged["imap_user"] = email_val
    if not merged.get("smtp_user"):
        merged["smtp_user"] = merged["imap_user"]

    # Account id: derive if the admin did not override it.
    if not merged.get("account_id"):
        merged["account_id"] = derive_account_id(email_val, taken)

    # Normalise display_name — this fills in the review-card nicely even
    # when the admin typed it. Blank values are rejected above.
    merged["display_name"] = str(merged.get("display_name", "")).strip()

    # ``unknown`` keys that are not credentials are silently ignored — the
    # admin may paste a provider welcome email with extra context. We do
    # NOT surface the ignored values back because they could contain a
    # password typed with a non-standard key like "mailbox pass".
    #
    # ``save_preset_suggested`` is used purely as a UX hint on the review
    # screen; the admin always has to opt in.
    save_preset_suggested = (not preset) and bool(
        merged.get("imap_host") and merged.get("smtp_host")
    )

    return QuickAddResult(
        fields={
            "account_id": merged["account_id"],
            "email": email_val,
            "display_name": merged["display_name"],
            "imap_host": merged["imap_host"],
            "imap_port": int(merged["imap_port"]),
            "imap_user": merged["imap_user"],
            "smtp_host": merged["smtp_host"],
            "smtp_port": int(merged["smtp_port"]),
            "smtp_user": merged["smtp_user"],
        },
        account_id_overridden=account_id_overridden,
        save_preset_suggested=save_preset_suggested,
    )


# --------------------------------------------------------------------------- presets

# Named server presets live in settings.json under ``mailbox_presets``. The
# shape is strict: dict[name, {imap_host, imap_port, smtp_host, smtp_port,
# imap_user?, smtp_user?}]. Anything else is ignored to keep forward-compat
# with hand-edited files.

_PRESET_KEYS: tuple[str, ...] = (
    "imap_host",
    "imap_port",
    "smtp_host",
    "smtp_port",
    "imap_user",
    "smtp_user",
)
# Optional metadata, additive and backward-compatible: presets saved before
# this metadata existed simply lack these keys and are treated as
# provider='custom', verified=True (an admin-saved preset implies it worked
# at least once), with no domain tags and the default username pattern
# (the full email address). Nothing here is required for a preset to load.
_PRESET_META_KEYS: tuple[str, ...] = (
    "provider",
    "verified",
    "verified_at",
    "username_pattern",
    "domains",
)
_PRESET_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _\-.]{0,31}$")
_KNOWN_PROVIDERS = frozenset({"hostinger", "siteground", "custom"})


def _clean_preset_meta(blob: dict[str, Any]) -> dict[str, Any]:
    """Extract + validate the optional metadata keys. Never raises -
    malformed metadata is just dropped, the core host/port fields still
    load fine (backward compatibility with hand-edited settings.json)."""
    meta: dict[str, Any] = {}
    provider = blob.get("provider")
    if isinstance(provider, str) and provider.strip().lower() in _KNOWN_PROVIDERS:
        meta["provider"] = provider.strip().lower()
    else:
        meta["provider"] = "custom"
    meta["verified"] = bool(blob.get("verified", True))
    try:
        meta["verified_at"] = float(blob.get("verified_at") or 0)
    except (TypeError, ValueError):
        meta["verified_at"] = 0.0
    up = blob.get("username_pattern")
    if isinstance(up, str) and up.strip():
        meta["username_pattern"] = up.strip()[:120]
    else:
        meta["username_pattern"] = ""
    domains = blob.get("domains")
    if isinstance(domains, list):
        clean_domains = []
        for d in domains:
            if isinstance(d, str) and d.strip():
                clean_domains.append(d.strip().lower())
        meta["domains"] = clean_domains[:20]
    else:
        meta["domains"] = []
    return meta


def _settings_presets(data: dict[str, Any]) -> dict[str, dict[str, Any]]:
    raw = data.get("mailbox_presets") or {}
    if not isinstance(raw, dict):
        return {}
    out: dict[str, dict[str, Any]] = {}
    for name, blob in raw.items():
        if not isinstance(name, str) or not _PRESET_NAME_RE.match(name):
            continue
        if not isinstance(blob, dict):
            continue
        cleaned: dict[str, Any] = {}
        for k in _PRESET_KEYS:
            v = blob.get(k)
            if k in ("imap_port", "smtp_port"):
                try:
                    v = int(v)
                except (TypeError, ValueError):
                    continue
                if not 1 <= v <= 65535:
                    continue
                cleaned[k] = v
            elif isinstance(v, str) and v.strip():
                cleaned[k] = v.strip().lower() if k.endswith("_host") else v.strip()
        if {"imap_host", "imap_port", "smtp_host", "smtp_port"} <= cleaned.keys():
            cleaned.update(_clean_preset_meta(blob))
            out[name] = cleaned
    return out


def presets_load() -> dict[str, dict[str, Any]]:
    """Return the dict of persisted presets. Robust to malformed entries."""
    return _settings_presets(config.load_settings())


def presets_list() -> list[tuple[str, dict[str, Any]]]:
    """Return (name, blob) pairs in a stable order (alphabetical)."""
    return sorted(presets_load().items(), key=lambda kv: kv[0].lower())


def preset_get(name: str) -> dict[str, Any] | None:
    return presets_load().get(name)


def preset_save(name: str, blob: dict[str, Any]) -> dict[str, Any]:
    """Create or overwrite a preset. Returns the cleaned stored blob.

    Only ``imap_host``, ``imap_port``, ``smtp_host``, ``smtp_port`` are
    required; ``imap_user`` and ``smtp_user`` are optional overrides.
    Optional metadata (``provider``, ``username_pattern``, ``domains``) may
    also be supplied - unknown/invalid metadata is silently normalised,
    never rejected, so this stays backward compatible with every existing
    call site. Raises ``QuickAddError`` with an admin-safe message on bad
    core input (host/port shape).
    """
    if not isinstance(name, str) or not _PRESET_NAME_RE.match(name):
        raise QuickAddError(
            "Preset name must be 1-32 chars of letters, digits, space, "
            "'-', '_' or '.'."
        )
    cleaned: dict[str, Any] = {}
    for k in ("imap_host", "smtp_host"):
        v = str(blob.get(k, "")).strip().lower()
        portal_problem = wm.reject_webmail_portal_url(v, label=k.replace("_", " "))
        if portal_problem:
            raise QuickAddError(portal_problem)
        if not wm._HOST_RE.match(v):
            raise QuickAddError(f"Preset {k.replace('_', ' ')} is not a valid hostname.")
        cleaned[k] = v
    for k in ("imap_port", "smtp_port"):
        try:
            v = int(blob.get(k))
        except (TypeError, ValueError):
            raise QuickAddError(f"Preset {k.replace('_', ' ')} must be a number.")
        if not 1 <= v <= 65535:
            raise QuickAddError(f"Preset {k.replace('_', ' ')} is out of range.")
        cleaned[k] = v
    for k in ("imap_user", "smtp_user"):
        v = blob.get(k)
        if isinstance(v, str) and v.strip():
            cleaned[k] = v.strip()
    meta = dict(blob.get("_meta") or {})
    # Allow metadata to also be passed flat on ``blob`` (used by the
    # Manage Presets edit screen) in addition to a nested ``_meta`` dict.
    for mk in _PRESET_META_KEYS:
        if mk in blob and mk not in meta:
            meta[mk] = blob[mk]
    if "verified_at" not in meta:
        meta["verified_at"] = time.time()
    cleaned.update(_clean_preset_meta(meta))
    settings = config.load_settings()
    presets = dict(_settings_presets(settings))
    presets[name] = cleaned
    settings["mailbox_presets"] = presets
    config.save_settings(settings)
    return cleaned


def preset_set_metadata(name: str, **meta_patch: Any) -> dict[str, Any]:
    """Update ONLY the metadata of an existing preset (provider, domains,
    username_pattern, verified flag). Host/port/username fields are left
    untouched. Raises ``QuickAddError`` if the preset does not exist."""
    existing = preset_get(name)
    if existing is None:
        raise QuickAddError(f"Preset '{name}' does not exist.")
    merged = dict(existing)
    merged.update(meta_patch)
    return preset_save(name, merged)


def associated_mailboxes(name: str) -> list[str]:
    """Account ids of currently-registered mailboxes whose IMAP host
    matches this preset's host - best-effort, read-only, used only to show
    "N mailboxes use this preset" on the Manage Presets view screen.
    Never raises; returns [] on any lookup problem."""
    blob = preset_get(name)
    if not blob:
        return []
    host = str(blob.get("imap_host") or "").strip().lower()
    if not host:
        return []
    try:
        from .. import mailbox_admin
        from .. import paths

        if not paths.CONFIG_TOML.exists():
            return []
        text = paths.CONFIG_TOML.read_text(encoding="utf-8-sig")
        out = []
        for m in mailbox_admin.list_mailboxes():
            block_re = re.compile(
                r"\[accounts\." + re.escape(m.account_id) + r"\.imap\]\s*server\s*=\s*\"([^\"]+)\""
            )
            match = block_re.search(text)
            if match and match.group(1).lower().startswith(host + ":"):
                out.append(m.account_id)
        return out
    except Exception:  # noqa: BLE001 - advisory only
        return []


def preset_delete(name: str) -> bool:
    """Remove ``name``. Returns True if a preset was removed."""
    settings = config.load_settings()
    presets = dict(_settings_presets(settings))
    if name not in presets:
        return False
    presets.pop(name, None)
    settings["mailbox_presets"] = presets
    config.save_settings(settings)
    return True
