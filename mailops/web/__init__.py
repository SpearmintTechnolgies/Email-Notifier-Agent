"""Secure mobile onboarding web interface.

This subpackage runs a loopback HTTPS server inside the existing bot
process. It is reached from the operator's phone through a reverse tunnel
(Cloudflare, Tailscale Funnel, etc. - see docs/REMOTE_ONBOARDING.md); the
server itself only ever binds to 127.0.0.1 and refuses direct public
access.

Entry point: ``mailops.web.app.start_web_server`` is called from the bot
process after ``run_polling`` has started. It never spawns a second
Telegram poller.
"""
