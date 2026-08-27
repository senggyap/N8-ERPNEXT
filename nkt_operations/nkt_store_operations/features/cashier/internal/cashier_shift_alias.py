from __future__ import annotations

import json
import uuid
from typing import Any, Dict, Optional

import frappe
from frappe.utils import flt, getdate

EDGE_SHIFT_PREFIX = "EDGE-SHIFT-"
PRIMARY_LIFECYCLE_JOURNAL = "NKT Primary Shift Close Z-Out Intent"
EDGE_SHIFT_PROJECTION = "NKT Edge Cashier Shift Projection"
TENDER_FAMILY = "NKT Cashier Tender Intent"
DRAWER_FAMILY = "NKT Cash Drawer Adjustment Intent"
OPEN_FAMILY = "NKT Cashier Shift Open Intent"
PRIVILEGED_ROLES = {"NKT OWNER", "NKT ADMINISTRATOR", "System Manager"}


def edge_shift_uuid_from_reference(reference: Any) -> Optional[str]:
    ref = str(reference or "").strip()
    if not ref.startswith(EDGE_SHIFT_PREFIX):
        return None
    raw = ref[len(EDGE_SHIFT_PREFIX):]
    try:
        return str(uuid.UUID(raw))
    except Exception:
        return None


def is_edge_shift_reference(reference: Any) -> bool:
    return edge_shift_uuid_from_reference(reference) is not None


def _roles(user: str) -> set[str]:
    return set(frappe.get_roles(user) or [])


def edge_shift_projection(reference: str):
    edge_uuid = edge_shift_uuid_from_reference(reference)
    if not edge_uuid:
        return None
    name = frappe.db.get_value(
        EDGE_SHIFT_PROJECTION,
        {"edge_shift_uuid": edge_uuid},
        "name",
    )
    return frappe.get_doc(EDGE_SHIFT_PROJECTION, name) if name else None


def validate_edge_shift_for_money(
    reference: str,
    *,
    company: str,
    settlement_location: str,
    cashier: str,
    require_open: bool = True,
    business_date: Any = None,
) -> Dict[str, Any]:
    doc = edge_shift_projection(reference)
    if not doc:
        raise frappe.DoesNotExistError("Store Edge Cashier Shift projection is unavailable.")

    hard = []
    if str(doc.company or "") != str(company or ""):
        hard.append("company")
    if str(doc.settlement_location or "") != str(settlement_location or ""):
        hard.append("settlement_location")
    privileged = cashier == "Administrator" or bool(_roles(cashier) & PRIVILEGED_ROLES)
    if str(doc.cashier or "") != str(cashier or "") and not privileged:
        hard.append("cashier")
    if business_date and getdate(doc.shift_start) != getdate(business_date):
        hard.append("shift_business_date")
    if hard:
        raise frappe.ValidationError(
            "Store Edge Cashier Shift identity conflicts with offline money event: "
            + ", ".join(hard)
        )
    if require_open and str(doc.local_status or "") != "Open":
        raise frappe.ValidationError("Store Edge Cashier Shift is not open.")

    return {
        "reference": doc.name,
        "edge_shift_uuid": doc.edge_shift_uuid,
        "company": doc.company,
        "settlement_location": doc.settlement_location,
        "cashier": doc.cashier,
        "shift_start": doc.shift_start,
        "opening_cash": flt(doc.opening_cash),
        "local_status": doc.local_status,
        "primary_shift_name": doc.primary_shift_name or None,
    }


def preserved_edge_shift_identity(reference: str) -> Optional[Dict[str, Any]]:
    edge_uuid = edge_shift_uuid_from_reference(reference)
    if not edge_uuid:
        return None

    rows = frappe.db.sql(
        f"""
        SELECT name, canonical_name, materialization_state, canonical_payload_json
        FROM `tab{PRIMARY_LIFECYCLE_JOURNAL}`
        WHERE event_family=%s AND edge_identity=%s
        ORDER BY creation ASC, name ASC
        LIMIT 1
        """,
        (OPEN_FAMILY, edge_uuid),
        as_dict=True,
    )
    if not rows:
        return None

    row = rows[0]
    payload = json.loads(row.canonical_payload_json)
    return {
        "journal": row.name,
        "edge_shift_uuid": edge_uuid,
        "company": payload["company"],
        "settlement_location": payload["settlement_location"],
        "cashier": payload["cashier"],
        "shift_business_date": payload["shift_business_date"],
        "shift_start": payload["shift_start"],
        "opening_cash": payload["opening_cash"],
        "canonical_shift": row.canonical_name or None,
        "materialization_state": row.materialization_state,
    }


def resolve_primary_cashier_shift(reference: str, *, required: bool = True) -> Optional[str]:
    ref = str(reference or "").strip()
    if frappe.db.exists("NKT Cashier Shift", ref):
        return ref

    preserved = preserved_edge_shift_identity(ref)
    canonical = str((preserved or {}).get("canonical_shift") or "").strip()
    if canonical and frappe.db.exists("NKT Cashier Shift", canonical):
        return canonical

    if required:
        raise frappe.DoesNotExistError(
            "Store Edge Cashier Shift has not yet been materialized on Primary. Safe retry required."
        )
    return None


def _pending_tender_cash(reference: str) -> float:
    total = 0.0
    rows = frappe.get_all(
        "NKT Sync Pending Payload",
        filters={"event_family": TENDER_FAMILY},
        fields=["payload_json"],
    )
    for row in rows:
        try:
            payload = json.loads(row.payload_json)
        except Exception:
            continue
        if str(payload.get("cashier_shift") or "") != reference:
            continue
        for payment in payload.get("payments") or []:
            if str(payment.get("payment_method") or "") == "Cash":
                total += flt(payment.get("collected_amount") or payment.get("amount"))
    return total


def edge_alias_expected_cash(
    reference: str,
    *,
    exclude_drawer_event_uuid: Optional[str] = None,
) -> float:
    doc = edge_shift_projection(reference)
    if not doc:
        raise frappe.DoesNotExistError("Store Edge Cashier Shift projection is unavailable.")

    total = flt(doc.opening_cash) + _pending_tender_cash(reference)
    filters = {"cashier_shift": reference}
    rows = frappe.get_all(
        "NKT Edge Cash Drawer Adjustment Projection",
        filters=filters,
        fields=["event_uuid", "signed_cash_effect", "projection_state"],
    )
    for row in rows:
        if exclude_drawer_event_uuid and str(row.event_uuid) == str(exclude_drawer_event_uuid):
            continue
        if str(row.projection_state or "") not in ("Conflict", "Failed"):
            total += flt(row.signed_cash_effect)
    return total
