from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import flt

OWNER_ADMIN_ROLES = {"NKT OWNER", "NKT ADMINISTRATOR"}
SETTINGS_DOCTYPE = "NKT Oil Operations Settings"
TOLERANCE = 0.000001


def _session_roles(user: str | None = None) -> set[str]:
    user = user or frappe.session.user
    if user == "Administrator":
        return {"Administrator"}
    return set(frappe.get_roles(user))


def require_owner_admin(user: str | None = None) -> None:
    user = user or frappe.session.user
    if user == "Administrator":
        return
    if not (_session_roles(user) & OWNER_ADMIN_ROLES):
        raise frappe.PermissionError(
            _("Cooking Oil operational entry is restricted to NKT OWNER / NKT ADMINISTRATOR.")
        )


def get_oil_settings(*, require_complete: bool = True):
    if not frappe.db.exists("DocType", SETTINGS_DOCTYPE):
        if require_complete:
            raise frappe.ValidationError(_("Cooking Oil settings are not installed."))
        return None

    doc = frappe.get_single(SETTINGS_DOCTYPE)
    if not require_complete:
        return doc

    required = {
        "company": doc.company,
        "combined_bulk_warehouse": doc.combined_bulk_warehouse,
        "palm_olein_item": doc.palm_olein_item,
        "empty_container_item": doc.empty_container_item,
        "finished_palm_oil_item": doc.finished_palm_oil_item,
    }
    missing = [label for label, value in required.items() if not value]
    if missing:
        raise frappe.ValidationError(
            _("Cooking Oil settings are incomplete: {0}.").format(", ".join(missing))
        )

    if abs(flt(doc.nominal_kg_per_container) - 17.0) > TOLERANCE:
        raise frappe.ValidationError(_("Palm Oil nominal content must remain exactly 17 Kg per container."))

    return doc


def configured_finished_oil_item() -> str:
    try:
        doc = get_oil_settings(require_complete=False)
        return str(doc.finished_palm_oil_item or "") if doc else ""
    except Exception:
        return ""


def is_configured_finished_oil_item(item_code: str) -> bool:
    configured = configured_finished_oil_item()
    return bool(configured and str(item_code or "") == configured)
