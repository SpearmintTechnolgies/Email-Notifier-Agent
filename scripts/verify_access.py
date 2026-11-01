"""Read-only probe of ops.db — verifies CEO admin permissions.
Prints no secrets; just role, permissions, source.
"""
import sqlite3, os, sys
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.getcwd())

from mailops import access

c = sqlite3.connect("data/ops.db")
c.row_factory = sqlite3.Row
cur = c.cursor()

print("--- tables ---")
for r in cur.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"):
    print(" ", r["name"])

print("--- access_users ---")
for r in cur.execute("SELECT * FROM access_users ORDER BY user_id"):
    print(" ", dict(r))

print("--- access_groups ---")
for r in cur.execute("SELECT * FROM access_groups ORDER BY chat_id"):
    print(" ", dict(r))

print("--- access.ensure() + computed roles ---")
access.ensure()
for u in access.users():
    uid = u.user_id
    perms = []
    for p in (access.P_VIEW, access.P_READ, access.P_DRAFT, access.P_SEND,
              access.P_ORGANIZE, access.P_SETTINGS, access.P_ADMIN):
        if access.can(uid, p):
            perms.append(p)
    print(f"  user_id={uid} role={access.role_of(uid)} perms={perms}")

print("--- bootstrap owner ---")
print(" ", access.bootstrap_owner_id())
print("--- group ids ---")
print(" ", access.group_ids())
print("--- can(CEO, P_ADMIN) ---")
print(" ", access.can(5337250772, access.P_ADMIN))
print("--- can(me, P_ADMIN) ---")
print(" ", access.can(8925736576, access.P_ADMIN))
