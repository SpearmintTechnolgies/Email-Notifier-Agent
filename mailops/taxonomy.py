from __future__ import annotations

from dataclasses import dataclass

from .config import PRIORITY_FOLDERS, TAG_FOLDERS, load_accounts
from .himalaya_client import Himalaya
from .policy import can_organize, describe


@dataclass
class TaxonomyReport:
    account_id: str
    created: list[str]
    existed: list[str]
    failed: list[str]
    # Non-empty when the account's policy forbids creating folders: nothing
    # was listed, created or changed on that mailbox.
    skipped: str = ""


def _normalize_names(names: list[str]) -> set[str]:
    return {n.strip().lower() for n in names}


def ensure_taxonomy(
    account_id: str | None = None, him: Himalaya | None = None
) -> list[TaxonomyReport]:
    if him is None:
        him = Himalaya()
    accounts = load_accounts()
    if account_id:
        accounts = [a for a in accounts if a.id == account_id]
    reports: list[TaxonomyReport] = []
    wanted = PRIORITY_FOLDERS + TAG_FOLDERS
    for acc in accounts:
        if not can_organize(acc.id):
            # Read-only / not-yet-approved account: never touch its folders.
            reports.append(
                TaxonomyReport(
                    account_id=acc.id,
                    created=[],
                    existed=[],
                    failed=[],
                    skipped=describe(acc.id),
                )
            )
            continue
        existing = _normalize_names(him.mailbox_list(acc.id))
        created: list[str] = []
        existed: list[str] = []
        failed: list[str] = []
        for folder in wanted:
            # Ensure parent path exists (INBOX.Tag before INBOX.Tag.Sales-Lead)
            parts = folder.split(".")
            for i in range(2, len(parts)):  # skip bare INBOX
                parent = ".".join(parts[:i])
                if parent.lower() in existing:
                    continue
                pr = him.create_mailbox(acc.id, parent)
                ptext = (pr.stderr + pr.stdout).lower()
                if pr.ok or "already" in ptext or "exist" in ptext:
                    existing.add(parent.lower())
                    if pr.ok:
                        created.append(parent)
            if folder.lower() in existing:
                existed.append(folder)
                continue
            r = him.create_mailbox(acc.id, folder)
            text = (r.stderr + r.stdout).lower()
            if r.ok or "already" in text or "exist" in text:
                existing.add(folder.lower())
                if r.ok:
                    created.append(folder)
                else:
                    existed.append(folder)
            else:
                failed.append(f"{folder}: {(r.stderr or r.stdout).strip()}")
        reports.append(
            TaxonomyReport(
                account_id=acc.id, created=created, existed=existed, failed=failed
            )
        )
    return reports


def bootstrap_all() -> str:
    lines = ["Folder taxonomy bootstrap"]
    for rep in ensure_taxonomy():
        lines.append(f"\n[{rep.account_id}]")
        if rep.skipped:
            lines.append(f"  skipped (no folders created): {rep.skipped}")
            continue
        lines.append(f"  created: {len(rep.created)} {rep.created}")
        lines.append(f"  existed: {len(rep.existed)}")
        if rep.failed:
            lines.append(f"  failed: {rep.failed}")
    return "\n".join(lines)
