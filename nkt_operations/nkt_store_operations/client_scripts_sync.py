"""Canonical NKT Client Script synchronization.

The current working site used many phase/recovery Client Script records. ITC4
squashed the *effective enabled code* into one source-controlled file per live
DocType/view. This module keeps the database runtime records synchronized from
those files so the source tree is authoritative for future IT development.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import frappe

BASE_DIR = Path(__file__).resolve().parent / "current_client_scripts"
MANIFEST_PATH = BASE_DIR / "manifest.json"
CURRENT_PREFIX = "NKT CURRENT — "


def _manifest() -> dict:
    return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))


def _script_path(entry: dict) -> Path:
    return Path(__file__).resolve().parent / entry["source_file"]


def _sha(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def get_sync_status() -> dict:
    manifest = _manifest()
    desired = {row["name"]: row for row in manifest["canonical_records"]}
    rows = frappe.get_all(
        "Client Script",
        fields=["name", "dt", "view", "enabled", "module", "script"],
        order_by="creation asc",
        limit_page_length=0,
    )
    current = {row.name: row for row in rows}
    status = {"desired": len(desired), "present": 0, "mismatched": [], "extra_current": []}

    for name, entry in desired.items():
        row = current.get(name)
        source = _script_path(entry).read_text(encoding="utf-8")
        if not row:
            status["mismatched"].append({"name": name, "reason": "missing"})
            continue
        expected = {
            "dt": entry["dt"],
            "view": entry["view"],
            "enabled": 1,
            "module": None,
            "script_sha256": _sha(source),
        }
        actual = {
            "dt": row.dt,
            "view": row.view,
            "enabled": int(row.enabled or 0),
            "module": row.module or None,
            "script_sha256": _sha(row.script or ""),
        }
        if expected != actual:
            status["mismatched"].append({"name": name, "expected": expected, "actual": actual})
        else:
            status["present"] += 1

    for name in current:
        if name.startswith(CURRENT_PREFIX) and name not in desired:
            status["extra_current"].append(name)

    status["ok"] = (
        status["present"] == status["desired"]
        and not status["mismatched"]
        and not status["extra_current"]
    )
    return status


def sync_current_client_scripts() -> dict:
    """Make Client Script runtime records exactly mirror canonical source files."""
    manifest = _manifest()
    desired = {row["name"]: row for row in manifest["canonical_records"]}
    superseded = set(manifest["superseded_names"])

    # Remove the known phase/history records captured by the accepted baseline,
    # plus stale canonical records from a previous sync version.
    delete_names = set()
    for name in frappe.get_all("Client Script", pluck="name", limit_page_length=0):
        if name in superseded or (name.startswith(CURRENT_PREFIX) and name not in desired):
            delete_names.add(name)

    for name in sorted(delete_names):
        frappe.delete_doc("Client Script", name, ignore_permissions=True, force=True)

    changed = []
    for name, entry in desired.items():
        source = _script_path(entry).read_text(encoding="utf-8")
        if frappe.db.exists("Client Script", name):
            doc = frappe.get_doc("Client Script", name)
        else:
            doc = frappe.new_doc("Client Script")
            doc.name = name

        values = {
            "dt": entry["dt"],
            "view": entry["view"],
            "enabled": 1,
            "module": None,
            "script": source,
        }
        dirty = doc.is_new()
        for field, value in values.items():
            if doc.get(field) != value:
                doc.set(field, value)
                dirty = True
        if dirty:
            if doc.is_new():
                doc.insert(ignore_permissions=True)
            else:
                doc.save(ignore_permissions=True)
            changed.append(name)

    doctypes = sorted({entry["dt"] for entry in desired.values()})
    for dt in doctypes:
        frappe.clear_cache(doctype=dt)

    return {
        "deleted": len(delete_names),
        "upserted": len(changed),
        "canonical_records": len(desired),
        "doctypes": doctypes,
        "status": get_sync_status(),
    }


def after_migrate():
    sync_current_client_scripts()
