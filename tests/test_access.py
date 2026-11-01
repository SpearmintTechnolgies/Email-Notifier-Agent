"""Access store: roles, bootstrap, persistence, last-owner, replay tokens."""
from __future__ import annotations

import pytest

from mailops import access
from mailops.db import init_db
from tests.test_telegram_group import GROUP, OTHER_GROUP, STRANGER, USER_A, USER_B, set_env


def _seed(profile, *, allowed=f"{USER_A},{USER_B}", home=f"{USER_A},{GROUP}", owner=""):
    extra = f"TELEGRAM_OWNER_ID={owner}\n" if owner else ""
    (profile / ".env").write_text(
        "TELEGRAM_BOT_TOKEN=test-token\n"
        f"TELEGRAM_ALLOWED_USERS={allowed}\n"
        f"TELEGRAM_HOME_CHANNEL={home}\n"
        + extra,
        encoding="utf-8",
    )
    init_db()
    access.ensure()


def test_bootstrap_seeds_env_users_and_group(tmp_profile):
    _seed(tmp_profile)
    assert access.role_of(USER_A) == "owner"
    assert access.get_user(USER_A).is_bootstrap
    assert access.role_of(USER_B) == "operator"
    assert access.can(USER_B, access.P_SEND)
    assert access.group_allowed(GROUP)
    assert not access.group_allowed(OTHER_GROUP)
    assert not access.is_authorized_user(STRANGER)


def test_owner_id_env_wins_over_first_allowed_user(tmp_profile):
    _seed(tmp_profile, owner=str(USER_B))
    assert access.role_of(USER_B) == "owner"
    assert access.get_user(USER_B).is_bootstrap
    assert access.role_of(USER_A) == "operator"


def test_empty_allowlist_fails_closed(tmp_profile):
    _seed(tmp_profile, allowed="", home=f"{GROUP}")
    assert access.users() == []
    assert not access.authorized(STRANGER, STRANGER, "private")
    assert not access.authorized(USER_A, GROUP, "group")


def test_group_whitelist_does_not_authorize_members(tmp_profile):
    _seed(tmp_profile)
    assert access.authorized(USER_A, GROUP, "group")
    assert not access.authorized(STRANGER, GROUP, "group")
    assert not access.authorized(USER_A, OTHER_GROUP, "group")
    assert access.authorized(USER_A, OTHER_GROUP, "group", allow_unconfigured_group=True)


def test_add_and_remove_user_and_group(tmp_profile):
    _seed(tmp_profile)
    added = access.add_user(USER_A, STRANGER, "viewer")
    assert added.role == "viewer"
    assert access.can(STRANGER, access.P_VIEW)
    assert not access.can(STRANGER, access.P_READ)
    access.remove_user(USER_A, STRANGER)
    assert not access.is_authorized_user(STRANGER)

    access.add_group(USER_A, OTHER_GROUP, "ops")
    assert access.group_allowed(OTHER_GROUP)
    assert access.remove_group(USER_A, OTHER_GROUP)
    assert not access.group_allowed(OTHER_GROUP)


def test_promote_existing_operator_to_owner_grants_admin(tmp_profile):
    """Owner/Admin is one stored role. Promotion updates in place; no duplicate row."""
    _seed(tmp_profile)
    before = access.get_user(USER_B)
    assert before.role == "operator"
    assert before.can_send
    assert not access.can(USER_B, access.P_ADMIN)
    promoted = access.set_role(
        USER_A, USER_B, "owner", source="managed_promotion"
    )
    assert promoted.role == "owner"
    assert not promoted.is_bootstrap
    assert access.get_user(USER_A).is_bootstrap
    assert access.can(USER_B, access.P_ADMIN)
    assert access.can(USER_B, access.P_READ)
    assert access.can(USER_B, access.P_DRAFT)
    assert access.can(USER_B, access.P_SEND)
    assert [u.user_id for u in access.users()].count(USER_B) == 1
    audit = [r for r in access.audit_log() if r.action == "set_role" and r.target_id == USER_B]
    assert audit and audit[0].actor_id == USER_A
    assert "managed_promotion" in audit[0].detail
    assert "ac:tok" not in audit[0].detail
    with pytest.raises(access.LastOwnerError):
        access.remove_user(USER_B, USER_A)


def test_operator_cannot_manage_users(tmp_profile):
    _seed(tmp_profile)
    with pytest.raises(access.AccessDenied):
        access.add_user(USER_B, STRANGER, "viewer")
    with pytest.raises(access.AccessDenied):
        access.add_group(USER_B, OTHER_GROUP)


def test_last_owner_and_bootstrap_protected(tmp_profile):
    _seed(tmp_profile)
    with pytest.raises(access.LastOwnerError):
        access.remove_user(USER_A, USER_A)
    extra = access.add_user(USER_A, STRANGER, "owner")
    assert extra.role == "owner"
    with pytest.raises(access.LastOwnerError):
        access.remove_user(STRANGER, USER_A)  # bootstrap owner
    with pytest.raises(access.LastOwnerError):
        access.set_role(STRANGER, USER_A, "operator")
    access.remove_user(USER_A, STRANGER)
    assert access.role_of(USER_A) == "owner"


def test_operator_send_is_opt_in(tmp_profile):
    _seed(tmp_profile)
    access.add_user(USER_A, STRANGER, "operator", can_send=False)
    assert not access.can(STRANGER, access.P_SEND)
    access.set_can_send(USER_A, STRANGER, True)
    assert access.can(STRANGER, access.P_SEND)
    access.set_can_send(USER_A, STRANGER, False)
    assert not access.can(STRANGER, access.P_SEND)
    access.set_can_send(USER_A, USER_A, False)
    assert access.can(USER_A, access.P_SEND)  # owners always keep send approval
    # viewers cannot be granted send
    access.set_role(USER_A, STRANGER, "viewer")
    with pytest.raises(access.AccessDenied):
        access.set_can_send(USER_A, STRANGER, True)


def test_revocation_is_immediate_and_not_reseeding_from_env(tmp_profile):
    _seed(tmp_profile)
    assert access.is_authorized_user(USER_B)
    access.remove_user(USER_A, USER_B)
    assert not access.is_authorized_user(USER_B)
    assert not access.can(USER_B, access.P_VIEW)
    access.ensure()
    assert not access.is_authorized_user(USER_B)


def test_persistence_across_ensure(tmp_profile):
    _seed(tmp_profile)
    access.add_user(USER_A, STRANGER, "viewer")
    access.add_group(USER_A, OTHER_GROUP, "second")
    access.ensure()
    assert access.role_of(STRANGER) == "viewer"
    assert access.group_allowed(OTHER_GROUP)
    assert access.bootstrap_owner_id() == USER_A


def test_action_tokens_are_single_use(tmp_profile):
    _seed(tmp_profile)
    token = access.issue_action(USER_A, "remove_user", {"user_id": USER_B})
    payload = access.consume_action(USER_A, token, "remove_user")
    assert payload["user_id"] == USER_B
    with pytest.raises(access.ReplayError):
        access.consume_action(USER_A, token, "remove_user")
    with pytest.raises(access.ReplayError):
        access.consume_action(USER_A, "missing", "remove_user")


def test_action_token_cannot_be_spent_by_another_admin(tmp_profile):
    _seed(tmp_profile)
    access.add_user(USER_A, STRANGER, "owner")
    token = access.issue_action(USER_A, "remove_user", {"user_id": USER_B})
    with pytest.raises(access.ReplayError):
        access.consume_action(STRANGER, token, "remove_user")


def test_audit_does_not_record_secrets(tmp_profile):
    _seed(tmp_profile)
    access.add_user(USER_A, STRANGER, "operator")
    rows = access.audit_log()
    blob = " ".join(f"{r.action} {r.detail}" for r in rows)
    assert "add_user" in blob
    assert "password" not in blob.lower()
    assert "token" not in blob.lower()
    assert "secret" not in blob.lower()


def test_notify_targets_follow_persisted_groups(tmp_profile):
    _seed(tmp_profile)
    targets = access.effective_notify_targets()
    assert USER_A in targets and GROUP in targets
    access.remove_group(USER_A, GROUP)
    access.add_group(USER_A, OTHER_GROUP)
    targets = access.effective_notify_targets()
    assert GROUP not in targets
    assert OTHER_GROUP in targets
    assert USER_A in targets


def test_invalid_ids_rejected(tmp_profile):
    _seed(tmp_profile)
    with pytest.raises(access.InvalidTargetError):
        access.add_user(USER_A, -5, "operator")
    with pytest.raises(access.InvalidTargetError):
        access.add_group(USER_A, USER_A)
    with pytest.raises(access.InvalidTargetError):
        access.add_user(USER_A, STRANGER, "superadmin")
