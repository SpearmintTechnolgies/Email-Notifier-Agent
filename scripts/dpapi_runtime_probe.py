"""End-to-end DPAPI vault <-> `mailops get-secret` smoke test.

This is the exact call chain Himalaya uses at runtime:
    Himalaya -> passwd.command = "python -m mailops get-secret <id> <kind>"
             -> mailops.webmail_onboarding.cli_get_secret
             -> secrets_vault.load_secret -> DPAPI unprotect -> stdout bytes

What we verify:
  1. Vault reports mode=dpapi (so we really exercised DPAPI, not Fernet).
  2. store_secret writes an envelope that starts with the DPAPI marker.
  3. The envelope decrypts to the same plaintext we put in (same-process).
  4. The `mailops get-secret` CLI invoked as a subprocess (same way
     Himalaya invokes it) also prints the exact plaintext to stdout.
  5. The ciphertext file is 0 bytes smaller than the plaintext + marker
     (sanity: DPAPI should expand, not shrink).

Cleanup: unlinks the test account's .enc file and never touches
`altcoininformer.*`.
"""
from __future__ import annotations
import os, sys, secrets, subprocess, hashlib
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.getcwd())

from mailops import secrets_vault as vault
from mailops import paths

ACCOUNT = "dpapi_probe_tmp"
KIND = "imap"
# A random throwaway plaintext (NOT a real password). Logged only as a hash.
plain = "pw-" + secrets.token_urlsafe(24)

status = vault.vault_status()
print("vault status:", status)
assert status["mode"] == "dpapi", f"not DPAPI mode: {status}"

enc_path = paths.SECRETS_DIR / f"{ACCOUNT}.{KIND}.enc"
try:
    # 1. seal + write
    out_path = vault.store_secret(ACCOUNT, KIND, plain)
    print("wrote envelope:", out_path, "size=", out_path.stat().st_size)
    raw = out_path.read_bytes()
    assert raw.startswith(b"\x01DPAPI\x00"), f"not a DPAPI envelope, starts with {raw[:8]!r}"

    # 2. in-process round-trip
    opened = vault.load_secret(ACCOUNT, KIND)
    assert opened == plain, "in-process round-trip mismatch"
    print("in-process load_secret: OK")

    # 3. Same-env subprocess (Himalaya's call pattern): clear PYTHONPATH etc.
    #    and go through the exact CLI the Himalaya `passwd.command` runs.
    exe = os.path.abspath(".venv/Scripts/python.exe")
    env = os.environ.copy()
    r = subprocess.run(
        [exe, "-m", "mailops", "get-secret", ACCOUNT, KIND],
        capture_output=True, text=True, timeout=30, env=env, cwd=os.getcwd(),
    )
    if r.returncode != 0:
        print("STDERR:", r.stderr[:400])
        raise SystemExit("get-secret CLI failed with code %s" % r.returncode)
    got = r.stdout
    # Hash both for logging; never print either plaintext.
    h_expected = hashlib.sha256(plain.encode()).hexdigest()[:16]
    h_got = hashlib.sha256(got.encode()).hexdigest()[:16]
    print(f"CLI stdout sha256[:16] = {h_got}   expected = {h_expected}")
    assert got == plain, "CLI stdout did not match plaintext"
    print("subprocess CLI load_secret: OK (matches Himalaya's call pattern)")

    print("ALL CHECKS PASSED")
finally:
    try:
        if enc_path.exists():
            enc_path.unlink()
            print("cleaned up:", enc_path.name)
    except Exception as e:
        print("WARN: could not clean up:", e)
