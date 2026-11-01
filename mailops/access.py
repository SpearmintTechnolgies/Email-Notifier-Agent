"""Telegram user/group access control with persisted roles.

The Video Agent pattern (numeric IDs, owner bootstrap from env, JSON/SQLite
allowlists, private-chat admin UI) is reused here, with Mailops-specific
roles and a stricter group rule: a whitelisted group never authorizes its
members. Each sender must be an authorized user AND the chat must be
allowlisted.

Source of truth after first launch is ``data/ops.db``. ``TELEGRAM_ALLOWED_USERS``,
``TELEGRAM_HOME_CHANNEL`` and optional ``TELEGRAM_OWNER_ID`` only seed the store
and remain the private-chat notification destinations. Admins change access
from the bot; they do not need to edit ``.env``.
"""
from __future__ import annotations

import json
import logging
import secrets
import time
from dataclasses import dataclass
from typing import Any

from .config import load_dotenv, load_runtime, parse_id_list
from .db import db, init_db

log = logging.getLogger("mailops.access")

ROLES = ("owner", "operator", "viewer")
ROLE_RANK = {"viewer": 1, "operator": 2, "owner": 3}

P_VIEW = "view"
P_READ = "read"
P_DRAFT = "draft"
P_SEND = "send"
P_ORGANIZE = "organize"
P_SETTINGS = "settings"
P_ADMIN = "admin"

ACTION_TTL_S = 15 * 60
AWAITING_TTL_S = 10 * 60
AUDIT_KEEP = 500

_BOOTSTRAP_KEY = "bootstrap_done"


class AccessDenied(PermissionError):
    """The actor is not allowed to perform this access-management action."""


class LastOwnerError(ValueError):
    """Refusing to remove or demote the last owner (or the bootstrap owner)."""


class ReplayError(ValueError):
    """An admin confirmation token was missing, expired, or already used."""


class InvalidTargetError(ValueError):
    """The supplied Telegram id is the wrong shape (user vs group)."""


@dataclass(frozen=True)
class AccessUser:
    user_id: int
    role: str
    can_send: bool
    is_bootstrap: bool
    name: str = ""
    username: str = ""
    added_by: int | None = None
    added_at: float = 0.0

    @property
    def perms(self) -> frozenset[str]:
        return perms_for(self.role, self.can_send)


@dataclass(frozen=True)
class AccessGroup:
    chat_id: int
    title: str = ""
    added_by: int | None = None
    added_at: float = 0.0


@dataclass(frozen=True)
class AuditRow:
    actor_id: int
    action: str
    target_type: str
    target_id: int | None
    detail: str
    created_at: float


def perms_for(role: str | None, can_send: bool = False) -> frozenset[str]:
    if role == "owner":
        return frozenset(
            {P_VIEW, P_READ, P_DRAFT, P_SEND, P_ORGANIZE, P_SETTINGS, P_ADMIN}
        )
    if role == "operator":
        granted = {P_VIEW, P_READ, P_DRAFT, P_ORGANIZE, P_SETTINGS}
        if can_send:
            granted.add(P_SEND)
        return frozenset(granted)
    if role == "viewer":
        return frozenset({P_VIEW})
    return frozenset()


def ensure() -> None:
    """Create tables and seed from trusted env if the store is empty."""
    init_db()
    _bootstrap()


def _meta(conn, key: str) -> str | None:
    row = conn.execute("SELECT value FROM access_meta WHERE key=?", (key,)).fetchone()
    return None if row is None else str(row["value"])


def _set_meta(conn, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO access_meta(key, value) VALUES(?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )


def _user_row(conn, user_id: int) -> AccessUser | None:
    row = conn.execute(
        "SELECT * FROM access_users WHERE user_id=?", (int(user_id),)
    ).fetchone()
    return None if row is None else _user_from(row)


def _user_from(row: Any) -> AccessUser:
    return AccessUser(
        user_id=int(row["user_id"]),
        role=str(row["role"]),
        can_send=bool(row["can_send"]),
        is_bootstrap=bool(row["is_bootstrap"]),
        name=str(row["name"] or ""),
        username=str(row["username"] or ""),
        added_by=None if row["added_by"] is None else int(row["added_by"]),
        added_at=float(row["added_at"] or 0),
    )


def _group_from(row: Any) -> AccessGroup:
    return AccessGroup(
        chat_id=int(row["chat_id"]),
        title=str(row["title"] or ""),
        added_by=None if row["added_by"] is None else int(row["added_by"]),
        added_at=float(row["added_at"] or 0),
    )


def _owner_count(conn) -> int:
    return int(
        conn.execute(
            "SELECT COUNT(*) AS n FROM access_users WHERE role='owner'"
        ).fetchone()["n"]
    )


def _audit(
    conn,
    actor_id: int,
    action: str,
    *,
    target_type: str = "",
    target_id: int | None = None,
    detail: str = "",
) -> None:
    conn.execute(
        "INSERT INTO access_audit(actor_id, action, target_type, target_id, detail, created_at) "
        "VALUES(?,?,?,?,?,?)",
        (int(actor_id), action, target_type, target_id, detail[:500], time.time()),
    )
    conn.execute(
        "DELETE FROM access_audit WHERE id NOT IN "
        "(SELECT id FROM (SELECT id FROM access_audit ORDER BY id DESC LIMIT ?))",
        (AUDIT_KEEP,),
    )


def _require_admin(conn, actor_id: int) -> AccessUser:
    actor = _user_row(conn, actor_id)
    if actor is None or actor.role != "owner":
        raise AccessDenied("Owner permission required")
    return actor


def _bootstrap_owner_id() -> int | None:
    env = load_dotenv()
    owner_ids, _ = parse_id_list(env.get("TELEGRAM_OWNER_ID"))
    owners = [u for u in owner_ids if u > 0]
    if owners:
        return owners[0]
    try:
        runtime = load_runtime()
    except RuntimeError:
        return None
    return runtime.allowed_users[0] if runtime.allowed_users else None


def _bootstrap() -> None:
    """Seed users/groups from env once. Re-insert the bootstrap owner if none remain."""
    try:
        runtime = load_runtime()
    except RuntimeError:
        return
    owner_id = _bootstrap_owner_id()
    now = time.time()
    with db() as conn:
        seeded = _meta(conn, _BOOTSTRAP_KEY) == "1"
        user_count = int(
            conn.execute("SELECT COUNT(*) AS n FROM access_users").fetchone()["n"]
        )
        if not seeded and user_count == 0:
            if owner_id is not None:
                conn.execute(
                    "INSERT INTO access_users(user_id, role, can_send, is_bootstrap, "
                    "name, username, added_by, added_at) VALUES(?,?,?,?,?,?,?,?)",
                    (int(owner_id), "owner", 1, 1, "", "", None, now),
                )
                _audit(
                    conn,
                    int(owner_id),
                    "bootstrap_owner",
                    target_type="user",
                    target_id=int(owner_id),
                )
            for uid in runtime.allowed_users:
                if owner_id is not None and int(uid) == int(owner_id):
                    continue
                conn.execute(
                    "INSERT OR IGNORE INTO access_users(user_id, role, can_send, is_bootstrap, "
                    "name, username, added_by, added_at) VALUES(?,?,?,?,?,?,?,?)",
                    (int(uid), "operator", 1, 0, "", "", owner_id, now),
                )
                _audit(
                    conn,
                    int(owner_id or uid),
                    "bootstrap_user",
                    target_type="user",
                    target_id=int(uid),
                    detail="role=operator can_send=1",
                )
            for gid in runtime.home_groups:
                conn.execute(
                    "INSERT OR IGNORE INTO access_groups(chat_id, title, added_by, added_at) "
                    "VALUES(?,?,?,?)",
                    (int(gid), "", owner_id, now),
                )
                _audit(
                    conn,
                    int(owner_id or 0),
                    "bootstrap_group",
                    target_type="group",
                    target_id=int(gid),
                )
            _set_meta(conn, _BOOTSTRAP_KEY, "1")
            log.info(
                "access store seeded owner=%s users=%s groups=%s",
                owner_id,
                runtime.allowed_users,
                runtime.home_groups,
            )
            return
        if _owner_count(conn) == 0 and owner_id is not None:
            conn.execute(
                "INSERT INTO access_users(user_id, role, can_send, is_bootstrap, "
                "name, username, added_by, added_at) VALUES(?,?,?,?,?,?,?,?) "
                "ON CONFLICT(user_id) DO UPDATE SET role='owner', is_bootstrap=1, can_send=1",
                (int(owner_id), "owner", 1, 1, "", "", None, now),
            )
            _audit(
                conn,
                int(owner_id),
                "restore_bootstrap_owner",
                target_type="user",
                target_id=int(owner_id),
            )
            log.warning("restored bootstrap owner %s (no owners remained)", owner_id)


def get_user(user_id: int | None) -> AccessUser | None:
    if user_id is None:
        return None
    ensure()
    with db() as conn:
        return _user_row(conn, int(user_id))


def role_of(user_id: int | None) -> str | None:
    user = get_user(user_id)
    return None if user is None else user.role


def is_owner(user_id: int | None) -> bool:
    return role_of(user_id) == "owner"


def is_authorized_user(user_id: int | None) -> bool:
    return get_user(user_id) is not None


def can(user_id: int | None, perm: str) -> bool:
    user = get_user(user_id)
    return bool(user and perm in user.perms)


def group_allowed(chat_id: int | None) -> bool:
    if chat_id is None:
        return False
    ensure()
    with db() as conn:
        row = conn.execute(
            "SELECT 1 FROM access_groups WHERE chat_id=?", (int(chat_id),)
        ).fetchone()
        return row is not None


def users() -> list[AccessUser]:
    ensure()
    with db() as conn:
        rows = conn.execute(
            "SELECT * FROM access_users ORDER BY role DESC, user_id"
        ).fetchall()
        return [_user_from(r) for r in rows]


def groups() -> list[AccessGroup]:
    ensure()
    with db() as conn:
        rows = conn.execute(
            "SELECT * FROM access_groups ORDER BY chat_id"
        ).fetchall()
        return [_group_from(r) for r in rows]


def group_ids() -> list[int]:
    return [g.chat_id for g in groups()]


def owner_ids() -> list[int]:
    return [u.user_id for u in users() if u.role == "owner"]


def bootstrap_owner_id() -> int | None:
    ensure()
    with db() as conn:
        row = conn.execute(
            "SELECT user_id FROM access_users WHERE is_bootstrap=1 ORDER BY user_id LIMIT 1"
        ).fetchone()
        return None if row is None else int(row["user_id"])


def authorized(
    user_id: int | None,
    chat_id: int | None,
    chat_type: str | None,
    *,
    allow_unconfigured_group: bool = False,
) -> bool:
    """True when this sender may use the bot in this chat.

    Private: the user must be in the store. Groups: the user must be in the
    store AND the group must be allowlisted (unless this is a /chatid-style
    helper). Whitelisting a group never authorizes strangers in it.
    """
    if user_id is None or not is_authorized_user(user_id):
        return False
    if chat_type in ("group", "supergroup"):
        if allow_unconfigured_group:
            return True
        return group_allowed(chat_id)
    return True


def describe_perms(user_id: int | None) -> str:
    user = get_user(user_id)
    if user is None:
        return "No access."
    labels = {
        P_VIEW: "view dashboards and summaries",
        P_READ: "open email bodies",
        P_DRAFT: "generate and edit drafts",
        P_SEND: "approve sending",
        P_ORGANIZE: "file / tag / mute mail",
        P_SETTINGS: "edit rules, purposes and settings",
        P_ADMIN: "manage users and groups",
    }
    lines = [
        f"User {user.user_id}",
        f"Role: {user.role}",
        f"Send approval: {'yes' if P_SEND in user.perms else 'no'}",
    ]
    if user.is_bootstrap:
        lines.append("Bootstrap owner: yes (cannot be removed)")
    lines.append("Permissions:")
    for perm, label in labels.items():
        mark = "yes" if perm in user.perms else "no"
        lines.append(f"  {label}: {mark}")
    return "\n".join(lines)


def add_user(
    actor_id: int,
    user_id: int,
    role: str = "operator",
    *,
    can_send: bool = False,
    name: str = "",
    username: str = "",
) -> AccessUser:
    if role not in ROLES:
        raise InvalidTargetError("Invalid role")
    user_id = int(user_id)
    if user_id <= 0:
        raise InvalidTargetError("User id must be a positive Telegram user id")
    ensure()
    with db() as conn:
        _require_admin(conn, actor_id)
        if role == "owner" and _user_row(conn, actor_id).role != "owner":
            raise AccessDenied("Only an owner can grant ownership")
        existing = _user_row(conn, user_id)
        if existing is not None:
            return existing
        send_flag = 1 if (role == "owner" or can_send) else 0
        conn.execute(
            "INSERT INTO access_users(user_id, role, can_send, is_bootstrap, "
            "name, username, added_by, added_at) VALUES(?,?,?,?,?,?,?,?)",
            (
                user_id,
                role,
                send_flag,
                0,
                name[:80],
                username[:80],
                int(actor_id),
                time.time(),
            ),
        )
        _audit(
            conn,
            actor_id,
            "add_user",
            target_type="user",
            target_id=user_id,
            detail=f"role={role} can_send={send_flag}",
        )
        log.info("access add_user actor=%s target=%s role=%s", actor_id, user_id, role)
        return _user_row(conn, user_id)


def _apply_role_change(
    conn,
    actor_id: int,
    existing: AccessUser,
    role: str,
    send_flag: int,
    name: str,
    username: str,
    *,
    source: str = "",
) -> None:
    if existing.role == "owner" and role != "owner":
        _guard_owner_change(conn, existing)
    conn.execute(
        "UPDATE access_users SET role=?, can_send=?, name=?, username=? WHERE user_id=?",
        (
            role,
            send_flag if role != "owner" else 1,
            name[:80] or existing.name,
            username[:80] or existing.username,
            existing.user_id,
        ),
    )
    extra = f" source={source}" if source else ""
    _audit(
        conn,
        actor_id,
        "set_role",
        target_type="user",
        target_id=existing.user_id,
        detail=f"role={role} can_send={1 if role == 'owner' else send_flag}{extra}",
    )
    log.info(
        "access set_role actor=%s target=%s role=%s",
        actor_id,
        existing.user_id,
        role,
    )


def _guard_owner_change(conn, existing: AccessUser) -> None:
    if existing.is_bootstrap:
        raise LastOwnerError("Cannot demote the bootstrap owner")
    if _owner_count(conn) <= 1:
        raise LastOwnerError("Cannot demote the last owner")


def remove_user(actor_id: int, user_id: int) -> bool:
    user_id = int(user_id)
    ensure()
    with db() as conn:
        _require_admin(conn, actor_id)
        existing = _user_row(conn, user_id)
        if existing is None:
            return False
        if existing.is_bootstrap:
            raise LastOwnerError("Cannot remove the bootstrap owner")
        if existing.role == "owner" and _owner_count(conn) <= 1:
            raise LastOwnerError("Cannot remove the last owner")
        conn.execute("DELETE FROM access_users WHERE user_id=?", (user_id,))
        conn.execute("DELETE FROM access_awaiting WHERE user_id=?", (user_id,))
        _audit(conn, actor_id, "remove_user", target_type="user", target_id=user_id)
        log.info("access remove_user actor=%s target=%s", actor_id, user_id)
    # Deep-link revocation runs on a fresh connection AFTER the removal is
    # committed, so a crash here cannot roll the removal back.
    try:
        from . import group_links

        group_links.revoke_user(user_id)
    except Exception:  # noqa: BLE001 - revocation must not break the removal
        log.debug("group deep-link revocation failed", exc_info=True)
    return True


def set_role(
    actor_id: int, user_id: int, role: str, *, source: str = ""
) -> AccessUser:
    if role not in ROLES:
        raise InvalidTargetError("Invalid role")
    ensure()
    with db() as conn:
        _require_admin(conn, actor_id)
        existing = _user_row(conn, int(user_id))
        if existing is None:
            raise InvalidTargetError("Unknown user")
        send_flag = 1 if existing.can_send or role == "owner" else 0
        _apply_role_change(
            conn,
            actor_id,
            existing,
            role,
            send_flag,
            existing.name,
            existing.username,
            source=source,
        )
        return _user_row(conn, int(user_id))


def set_can_send(actor_id: int, user_id: int, enabled: bool) -> AccessUser:
    ensure()
    with db() as conn:
        _require_admin(conn, actor_id)
        existing = _user_row(conn, int(user_id))
        if existing is None:
            raise InvalidTargetError("Unknown user")
        if existing.role == "owner":
            return existing  # owners always may approve sending
        if existing.role == "viewer":
            raise AccessDenied("Viewers cannot be granted send approval")
        conn.execute(
            "UPDATE access_users SET can_send=? WHERE user_id=?",
            (1 if enabled else 0, int(user_id)),
        )
        _audit(
            conn,
            actor_id,
            "grant_send" if enabled else "revoke_send",
            target_type="user",
            target_id=int(user_id),
        )
        log.info(
            "access set_can_send actor=%s target=%s enabled=%s",
            actor_id,
            user_id,
            enabled,
        )
        return _user_row(conn, int(user_id))


def add_group(actor_id: int, chat_id: int, title: str = "") -> AccessGroup:
    chat_id = int(chat_id)
    if chat_id >= 0:
        raise InvalidTargetError("Group id must be negative")
    ensure()
    with db() as conn:
        _require_admin(conn, actor_id)
        now = time.time()
        conn.execute(
            "INSERT INTO access_groups(chat_id, title, added_by, added_at) VALUES(?,?,?,?) "
            "ON CONFLICT(chat_id) DO UPDATE SET title=excluded.title",
            (chat_id, (title or "")[:200], int(actor_id), now),
        )
        _audit(
            conn,
            actor_id,
            "add_group",
            target_type="group",
            target_id=chat_id,
            detail=(title or "")[:200],
        )
        log.info("access add_group actor=%s chat=%s", actor_id, chat_id)
        row = conn.execute(
            "SELECT * FROM access_groups WHERE chat_id=?", (chat_id,)
        ).fetchone()
        return _group_from(row)


def remove_group(actor_id: int, chat_id: int) -> bool:
    chat_id = int(chat_id)
    ensure()
    with db() as conn:
        _require_admin(conn, actor_id)
        cur = conn.execute("DELETE FROM access_groups WHERE chat_id=?", (chat_id,))
        if cur.rowcount:
            _audit(conn, actor_id, "remove_group", target_type="group", target_id=chat_id)
            log.info("access remove_group actor=%s chat=%s", actor_id, chat_id)
            return True
        return False


def audit_log(limit: int = 40) -> list[AuditRow]:
    ensure()
    with db() as conn:
        rows = conn.execute(
            "SELECT actor_id, action, target_type, target_id, detail, created_at "
            "FROM access_audit ORDER BY id DESC LIMIT ?",
            (int(limit),),
        ).fetchall()
        return [
            AuditRow(
                actor_id=int(r["actor_id"]),
                action=str(r["action"]),
                target_type=str(r["target_type"] or ""),
                target_id=None if r["target_id"] is None else int(r["target_id"]),
                detail=str(r["detail"] or ""),
                created_at=float(r["created_at"] or 0),
            )
            for r in rows
        ]


def issue_action(actor_id: int, action: str, payload: dict[str, Any]) -> str:
    """Create a single-use admin confirmation token (replay protection)."""
    ensure()
    token = secrets.token_hex(8)
    now = time.time()
    with db() as conn:
        _require_admin(conn, actor_id)
        conn.execute(
            "DELETE FROM access_actions WHERE created_at < ? OR used_at IS NOT NULL",
            (now - ACTION_TTL_S,),
        )
        conn.execute(
            "INSERT INTO access_actions(token, actor_id, action, payload, created_at) "
            "VALUES(?,?,?,?,?)",
            (token, int(actor_id), action, json.dumps(payload), now),
        )
    return token


def consume_action(actor_id: int, token: str, expected: str | None = None) -> dict[str, Any]:
    """Mark a confirmation token used. Raises ReplayError if it cannot be spent."""
    ensure()
    now = time.time()
    with db() as conn:
        _require_admin(conn, actor_id)
        row = conn.execute(
            "SELECT token, actor_id, action, payload, created_at, used_at "
            "FROM access_actions WHERE token=?",
            (token,),
        ).fetchone()
        if row is None:
            raise ReplayError("Unknown or expired action")
        if int(row["actor_id"]) != int(actor_id):
            raise ReplayError("This action belongs to a different administrator")
        if row["used_at"] is not None:
            raise ReplayError("This action was already used")
        if now - float(row["created_at"]) > ACTION_TTL_S:
            raise ReplayError("This action expired")
        if expected and str(row["action"]) != expected:
            raise ReplayError("This action does not match")
        cur = conn.execute(
            "UPDATE access_actions SET used_at=? WHERE token=? AND used_at IS NULL",
            (now, token),
        )
        if cur.rowcount != 1:
            raise ReplayError("This action was already used")
        try:
            payload = json.loads(row["payload"] or "{}")
        except ValueError:
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        payload["_action"] = str(row["action"])
        return payload


def set_awaiting(user_id: int, mode: str, payload: dict[str, Any] | None = None) -> None:
    ensure()
    with db() as conn:
        conn.execute(
            "INSERT INTO access_awaiting(user_id, mode, payload, created_at) VALUES(?,?,?,?) "
            "ON CONFLICT(user_id) DO UPDATE SET mode=excluded.mode, payload=excluded.payload, "
            "created_at=excluded.created_at",
            (int(user_id), mode, json.dumps(payload or {}), time.time()),
        )


def get_awaiting(user_id: int) -> dict[str, Any] | None:
    ensure()
    with db() as conn:
        row = conn.execute(
            "SELECT mode, payload, created_at FROM access_awaiting WHERE user_id=?",
            (int(user_id),),
        ).fetchone()
        if row is None:
            return None
        if time.time() - float(row["created_at"]) > AWAITING_TTL_S:
            conn.execute("DELETE FROM access_awaiting WHERE user_id=?", (int(user_id),))
            return None
        try:
            payload = json.loads(row["payload"] or "{}")
        except ValueError:
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        return {"mode": str(row["mode"]), **payload}


def clear_awaiting(user_id: int) -> None:
    ensure()
    with db() as conn:
        conn.execute("DELETE FROM access_awaiting WHERE user_id=?", (int(user_id),))


def effective_notify_targets() -> list[int]:
    """Private env destinations plus currently allowlisted groups."""
    ensure()
    try:
        runtime = load_runtime()
    except RuntimeError:
        return []
    private = [c for c in runtime.home_channels if c > 0]
    out: list[int] = []
    for cid in private + group_ids():
        if cid not in out:
            out.append(cid)
    return out or list(runtime.notify_targets)
