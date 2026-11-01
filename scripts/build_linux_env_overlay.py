"""Produce the Linux .env overlay from the live Windows .env.

Maps Windows-specific values to their Linux equivalents, carries the real
Telegram/LLM settings across, and leaves MAILOPS_TEST_MODE=1 so the service
still cannot poll Telegram or mutate a mailbox until cutover is approved.

Writes build/env.linux.overlay (mode-restricted by the caller) and prints only
key names plus whether each value is empty/set and its length. Never prints a
secret value.
"""
from __future__ import annotations

from pathlib import Path

REPO = Path(r"C:\Users\Global\Downloads\Email-Notifier-Agent")
SRC = REPO / ".env"
OUT = REPO / "build" / "env.linux.overlay"

SECRET_KEYS = {"TELEGRAM_BOT_TOKEN", "OPENAI_API_KEY", "GOOGLE_CLIENT_SECRET", "GOOGLE_CLIENT_ID"}

# Keys carried over verbatim from the Windows .env
CARRY = [
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_ALLOWED_USERS",
    "TELEGRAM_HOME_CHANNEL",
    "OPENAI_API_KEY",
    "OPENAI_BASE_URL",
    "OPENAI_MODEL",
    "MAILOPS_ALLOW_DM_CREDENTIALS",
    "MAILOPS_BULK_ALLOW_GROUP_UPLOAD",
    "MAILOPS_BULK_ALLOWED_GROUP",
]

# Keys forced to a Linux-correct value regardless of the Windows file
FORCED = {
    "HIMALAYA_BIN": "/usr/local/bin/himalaya",
    "MAILOPS_HOME": "/opt/mailops",
    "MAILOPS_HIMALAYA_DIR": "/opt/mailops/himalaya",
    "MAILOPS_KEY_DIR": "/opt/mailops/keys",
    "MAILOPS_VAULT_BACKEND": "file",
    "MAILOPS_FORCE_SOFT_VAULT": "1",
    # Stays 1 until the operator approves cutover. Flipped by the cutover step.
    "MAILOPS_TEST_MODE": "1",
}


def parse(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        out[k.strip()] = v.strip()
    return out


def main() -> int:
    win = parse(SRC)
    final: dict[str, str] = {}
    for k in CARRY:
        final[k] = win.get(k, "")
    final.update(FORCED)

    OUT.parent.mkdir(parents=True, exist_ok=True)
    body = "\n".join(f"{k}={v}" for k, v in final.items()) + "\n"
    OUT.write_bytes(body.encode("utf-8"))  # LF only, no BOM

    print(f"wrote {OUT} ({OUT.stat().st_size} bytes, LF endings)")
    print()
    for k, v in final.items():
        if k in SECRET_KEYS:
            state = "(empty)" if not v else f"(SET, {len(v)} chars - not shown)"
        else:
            state = v if v else "(empty)"
        print(f"  {k:<34} {state}")
    print()
    print("MAILOPS_TEST_MODE is deliberately 1: the service cannot poll Telegram")
    print("or mutate a mailbox until the approved cutover flips it to 0.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
