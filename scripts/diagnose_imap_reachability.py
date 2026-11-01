"""READ-ONLY: TCP reachability of every configured IMAP host.

Resolves each account's IMAP host from the Himalaya config and tries a plain
TCP connect. No credentials are sent, no IMAP commands issued, nothing
written. This distinguishes "mailbox is empty" from "server unreachable" -
a distinction mailops.himalaya_client.envelopes() currently erases.
"""
from __future__ import annotations

import os
import socket
import tempfile
from collections import defaultdict

os.environ["MAILOPS_LOG_FILE"] = os.path.join(
    tempfile.mkdtemp(prefix="diag-reach-"), "probe.log"
)

from mailops.config import load_accounts  # noqa: E402
from mailops.paths import HIMALAYA_DIR  # noqa: E402

TIMEOUT = 6.0


def imap_hosts() -> dict[str, tuple[str, int]]:
    """{account_id: (host, port)} parsed from himalaya/config.toml.

    Himalaya v2 writes ``server = "host:port"`` under ``[accounts.X.imap]``;
    older layouts used separate ``host`` / ``port`` keys, so both are read.
    """
    try:
        import tomllib
    except ImportError:  # pragma: no cover
        import tomli as tomllib  # type: ignore

    path = HIMALAYA_DIR / "config.toml"
    with path.open("rb") as fh:
        cfg = tomllib.load(fh)
    out: dict[str, tuple[str, int]] = {}
    for name, acc in (cfg.get("accounts") or {}).items():
        if not isinstance(acc, dict):
            continue
        imap = acc.get("imap") or {}
        host = str(imap.get("host") or "")
        port = int(imap.get("port") or 0)
        server = str(imap.get("server") or "")
        if server and not host:
            if ":" in server:
                host, _, raw_port = server.rpartition(":")
                try:
                    port = int(raw_port)
                except ValueError:
                    host, port = server, port
            else:
                host = server
        if host:
            out[name] = (host, port or 993)
    return out


def main() -> int:
    hosts = imap_hosts()
    accounts = load_accounts()
    print(f"accounts in registry : {len(accounts)}")
    print(f"accounts with imap   : {len(hosts)}\n")

    # One probe per unique host:port, not per account.
    unique: dict[tuple[str, int], list[str]] = defaultdict(list)
    for acc in accounts:
        hp = hosts.get(acc.id)
        if hp:
            unique[hp].append(acc.id)

    reachable: list[tuple[str, int]] = []
    blocked: list[tuple[str, int]] = []
    for (host, port), ids in sorted(unique.items()):
        try:
            with socket.create_connection((host, port), timeout=TIMEOUT):
                ok, note = True, "reachable"
        except Exception as exc:  # noqa: BLE001
            ok, note = False, f"{type(exc).__name__}: {exc}"
        (reachable if ok else blocked).append((host, port))
        mark = "OK  " if ok else "DOWN"
        print(f"{mark} {host}:{port}  ({len(ids)} mailbox(es))")
        if not ok:
            print(f"       {note}")
        print(f"       {', '.join(ids[:6])}{' ...' if len(ids) > 6 else ''}")

    affected = sum(len(v) for k, v in unique.items() if k in set(blocked))
    print(f"\nhosts reachable : {len(reachable)}")
    print(f"hosts DOWN      : {len(blocked)}")
    print(f"mailboxes affected by unreachable hosts : {affected}/{len(accounts)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
