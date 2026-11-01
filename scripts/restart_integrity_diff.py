"""Diff the pre/post-restart integrity snapshots."""
import json
import pathlib

root = pathlib.Path(__file__).resolve().parents[1]
a = json.loads((root / "data" / "_integrity_before.json").read_text(encoding="utf-8-sig"))
b = json.loads((root / "data" / "_integrity_after.json").read_text(encoding="utf-8-sig"))


def verdict(key: str) -> str:
    return "IDENTICAL" if a[key] == b[key] else "CHANGED"


print(f"himalaya/accounts.json : {verdict('accounts.json')}  "
      f"(count {a['accounts.json']['count']} -> {b['accounts.json']['count']})")
print(f"himalaya/config.toml   : {verdict('config.toml')}")
print(f"secrets/*.enc          : {verdict('secrets')}  "
      f"(count {a['secrets']['count']} -> {b['secrets']['count']})")
sa, sb = a["settings.json"], b["settings.json"]
print(f"data/settings.json     : {'IDENTICAL' if sa == sb else 'sha differs (live bot bookkeeping)'}")
print(f"   active_accounts     : {sa['active_accounts']} -> {sb['active_accounts']}")
print(f"   account_policies    : {sa['account_policies']} -> {sb['account_policies']}")
print(f"   group_restricted    : {sa['group_restricted_accounts']} -> {sb['group_restricted_accounts']}")
print(f"   sync_interval_s     : {sa['sync_interval_seconds']} -> {sb['sync_interval_seconds']}")
print(f"   digest_interval_s   : {sa['digest_interval_seconds']} -> {sb['digest_interval_seconds']}")
print(f"data/ops.db tables     : {a['ops.db']['tables']} -> {b['ops.db']['tables']}")
print(f"data/ops.db rows       : {a['ops.db']['rows']} -> {b['ops.db']['rows']}")
