"""Read-only Telegram diagnostics with all output redacted.

    .\\.venv\\Scripts\\python.exe scripts\\telegram_diag.py

Calls getMe, getWebhookInfo, getChat/getChatMember for the configured home
targets and authorized users. Never calls getUpdates (that would fight the
live poller) and never prints the token.
"""
from __future__ import annotations

import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mailops.config import load_dotenv, load_runtime  # noqa: E402
from mailops.redact import redact_text  # noqa: E402


def main() -> int:
    load_dotenv()
    rt = load_runtime()
    tok = rt.telegram_token

    def call(method: str, **params):
        url = f"https://api.telegram.org/bot{tok}/{method}"
        data = urllib.parse.urlencode(params).encode() if params else None
        try:
            with urllib.request.urlopen(urllib.request.Request(url, data=data), timeout=20) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            try:
                return json.loads(e.read())
            except Exception:  # noqa: BLE001
                return {"ok": False, "description": f"HTTP {e.code}"}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "description": redact_text(f"{type(e).__name__}: {e}", (tok,))}

    def show(label: str, value) -> None:
        print(redact_text(f"{label}: {value}", (tok,)))

    me = call("getMe")
    if not me.get("ok"):
        show("getMe FAILED", me.get("description"))
        return 1
    r = me["result"]
    show("bot", {k: r.get(k) for k in ("id", "username", "can_join_groups", "can_read_all_group_messages")})
    wh = call("getWebhookInfo").get("result", {})
    show("webhook set", bool(wh.get("url")))
    show("pending updates", wh.get("pending_update_count"))
    show("authorized users", rt.allowed_users)
    show("notify targets", rt.notify_targets)
    for cid in rt.home_channels:
        c = call("getChat", chat_id=cid)
        if not c.get("ok"):
            show(f"chat {cid}", c.get("description"))
            continue
        res = c["result"]
        m = call("getChatMember", chat_id=cid, user_id=r["id"]).get("result", {})
        show(f"chat {cid}", {"type": res.get("type"), "title": res.get("title"), "bot_status": m.get("status")})
        if cid < 0:
            for u in rt.allowed_users:
                um = call("getChatMember", chat_id=cid, user_id=u)
                show(f"  user {u} in group", um.get("result", {}).get("status") if um.get("ok") else um.get("description"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
