"""Regression tests for the AWS SSM Parameter Store vault backend.

This backend is opt-in via ``MAILOPS_VAULT_BACKEND=ssm`` and is intended
for the eventual Linux/EC2 deployment. These tests never talk to real AWS:
``_ssm_client`` is monkeypatched with a tiny in-memory fake so the suite
stays hermetic and fast, and so CI does not need boto3 installed or AWS
credentials configured.
"""
from __future__ import annotations

import pytest

from mailops import secrets_vault as vault


class _FakeParameterNotFound(Exception):
    pass


class _FakeSSMClient:
    """Minimal stand-in for boto3's SSM client, in-memory only."""

    def __init__(self):
        self.store: dict[str, dict[str, object]] = {}

    def put_parameter(self, *, Name, Value, Type, Overwrite, Tier, KeyId=None):
        self.store[Name] = {"Value": Value, "Type": Type, "KeyId": KeyId}
        return {"Version": 1, "Tier": Tier}

    def get_parameter(self, *, Name, WithDecryption=True):
        if Name not in self.store:
            exc = _FakeParameterNotFound(Name)
            # boto3 raises a dynamically-generated exception class whose
            # __name__ is "ParameterNotFound"; mimic that shape here.
            exc.__class__.__name__ = "ParameterNotFound"
            raise exc
        return {"Parameter": {"Value": self.store[Name]["Value"]}}

    def delete_parameter(self, *, Name):
        if Name not in self.store:
            exc = _FakeParameterNotFound(Name)
            exc.__class__.__name__ = "ParameterNotFound"
            raise exc
        del self.store[Name]


@pytest.fixture
def fake_ssm(monkeypatch):
    client = _FakeSSMClient()
    monkeypatch.setenv("MAILOPS_VAULT_BACKEND", "ssm")
    monkeypatch.setenv("MAILOPS_SSM_PREFIX", "/mailops-test/secrets")
    monkeypatch.setattr(vault, "_ssm_client", lambda: client)
    return client


def test_ssm_store_and_load_roundtrip(fake_ssm):
    vault.store_secret("acct1", "imap", "s3kr3t-pass")
    assert vault.load_secret("acct1", "imap") == "s3kr3t-pass"
    # The ciphertext (parameter value, in this fake) must be stored under
    # the expected name, and nothing was written to the local filesystem.
    assert "/mailops-test/secrets/acct1/imap" in fake_ssm.store


def test_ssm_missing_parameter_returns_empty_string(fake_ssm):
    assert vault.load_secret("never-onboarded", "smtp") == ""


def test_ssm_delete_secrets_removes_both_kinds(fake_ssm):
    vault.store_secret("acct2", "imap", "a")
    vault.store_secret("acct2", "smtp", "b")
    vault.delete_secrets("acct2")
    assert vault.load_secret("acct2", "imap") == ""
    assert vault.load_secret("acct2", "smtp") == ""


def test_ssm_rejects_invalid_account_id(fake_ssm):
    with pytest.raises(vault.VaultError):
        vault.store_secret("../escape", "imap", "x")


def test_ssm_kms_key_id_is_forwarded_when_set(fake_ssm, monkeypatch):
    monkeypatch.setenv("MAILOPS_SSM_KMS_KEY_ID", "alias/mailops-secrets")
    vault.store_secret("acct3", "imap", "pw")
    assert fake_ssm.store["/mailops-test/secrets/acct3/imap"]["KeyId"] == "alias/mailops-secrets"


def test_vault_status_reports_ssm_backend_without_leaking_secrets(fake_ssm):
    status = vault.vault_status()
    assert status["mode"] == "ssm"
    assert status["secrets_dir"] == "/mailops-test/secrets"
    assert "key_dir" in status  # describes KMS, never contains key material


def test_default_backend_is_still_file_based(monkeypatch):
    """Sanity: without the env var, behaviour is unchanged (today's prod path)."""
    monkeypatch.delenv("MAILOPS_VAULT_BACKEND", raising=False)
    assert vault._backend() == "file"
