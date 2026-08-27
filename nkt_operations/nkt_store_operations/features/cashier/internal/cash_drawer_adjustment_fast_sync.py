from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime
from typing import Any, Dict, Optional
from zoneinfo import ZoneInfo

import frappe
from frappe import _
from frappe.utils import cint, flt, get_datetime, now_datetime

from nkt_operations.nkt_store_operations.features.cashier.internal.cash_drawer_adjustment_intent import (
    CASH_DRAWER_ADJUSTMENT_INTENT_FAMILY,
    DENOMINATIONS,
    _normalize_cash_drawer_adjustment_intent_payload,
    accept_cash_drawer_adjustment_intent_at_edge,
    effective_edge_expected_cash,
)
from nkt_operations.nkt_store_operations.features.offline_edge.policy import (
    device_policy_snapshot,
)
from nkt_operations.nkt_store_operations.features.offline_edge.safe_sync import (
    NKTIdempotencyConflict,
    canonical_payload_hash,
)
from nkt_operations.nkt_store_operations.features.offline_edge.sync_transport import (
    _runtime_role,
)
from nkt_operations.nkt_store_operations.features.cashier.shift_engine import (
    ADJUSTMENT,
    ADJUSTMENT_MAP,
    CONTROL_LOG,
    SHIFT,
    calculate_shift_summary,
)

PH_TZ = ZoneInfo("Asia/Manila")
LOG_ACTION = "Cash Drawer Fast Request"
UI_VERSION = "C15C.10F-R5-CashDrawerFrontDoor"
TOLERANCE = 0.000001
AUTHORIZED_ROLES = {"NKT Cashier", "System Manager", "NKT OWNER", "NKT ADMINISTRATOR"}


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _runtime() -> str:
    return _clean(_runtime_role() or "Primary")


def _request_uuid(value: Any) -> str:
    try:
        return str(uuid.UUID(str(value)))
    except Exception as exc:
        raise frappe.ValidationError("Cash Drawer request ID is invalid.") from exc


def _roles(user: str) -> set[str]:
    return set(frappe.get_roles(user) or [])


def _require_operator(user: Optional[str] = None) -> str:
    user = _clean(user or frappe.session.user)
    if not user or user == "Guest":
        raise frappe.PermissionError("Cash Drawer adjustment is unavailable.")
    if user != "Administrator" and not (_roles(user) & AUTHORIZED_ROLES):
        raise frappe.PermissionError("Cash Drawer adjustment is unavailable.")
    return user


def _validate_edge_terminal_if_needed(device_id: Optional[str]) -> None:
    if _runtime() != "Store Edge":
        return
    device_id = _clean(device_id)
    if not device_id:
        frappe.throw(
            _("This terminal is not registered for offline Cash Drawer recording."),
            frappe.PermissionError,
        )
    snapshot = device_policy_snapshot(
        device_id,
        user=frappe.session.user,
        requested_context="NKT Retail",
    )
    if snapshot.get("ui_mode") != "normal":
        frappe.throw(
            _("Cash Drawer adjustment is unavailable on this terminal."),
            frappe.PermissionError,
        )


def _normalize_frontdoor_payload(payload: Any) -> Dict[str, Any]:
    if isinstance(payload, str):
        payload = frappe.parse_json(payload)
    if not isinstance(payload, dict):
        raise frappe.ValidationError("Cash Drawer adjustment payload must be an object.")

    raw = {
        "cashier_shift": payload.get("cashier_shift"),
        "adjustment_type": payload.get("adjustment_type"),
        "amount": payload.get("amount"),
        "party_name": payload.get("party_name"),
        "purpose": payload.get("purpose"),
        "supporting_document": payload.get("supporting_document"),
        "client_observed_at": payload.get("client_observed_at")
        or datetime.now(PH_TZ).isoformat(timespec="seconds"),
        "client_ui_version": UI_VERSION,
    }
    for field in DENOMINATIONS:
        raw[field] = payload.get(field) or 0
    return _normalize_cash_drawer_adjustment_intent_payload(raw)


def _payload_hash(normalized: Dict[str, Any]) -> str:
    return canonical_payload_hash(normalized)


def _named_lock(request_id: str):
    lock_name = "nkt-10f-fast-" + hashlib.sha256(
        request_id.encode("utf-8")
    ).hexdigest()[:32]
    rows = frappe.db.sql("SELECT GET_LOCK(%s,%s)", (lock_name, 30), as_list=True)
    if not rows or int(rows[0][0] or 0) != 1:
        raise frappe.ValidationError(
            "Cash Drawer request is busy. Safe retry is required."
        )
    state = {"released": False}

    def release_once():
        if state["released"]:
            return
        try:
            frappe.db.sql("SELECT RELEASE_LOCK(%s)", (lock_name,))
        except Exception:
            pass
        state["released"] = True

    frappe.db.after_commit.add(release_once)
    frappe.db.after_rollback.add(release_once)


def _log_rows(request_id: str):
    return frappe.get_all(
        CONTROL_LOG,
        filters={"action": LOG_ACTION, "reason": request_id},
        fields=["name", "cashier_shift", "adjustment", "performed_by", "details_json"],
        order_by="creation asc",
        limit_page_length=3,
    )


def _log_detail(row) -> Dict[str, Any]:
    try:
        return json.loads(row.details_json or "{}")
    except Exception as exc:
        raise NKTIdempotencyConflict(
            "Cash Drawer request audit binding is unreadable."
        ) from exc


def _verify_primary_adjustment(adjustment_name: str, normalized: Dict[str, Any]):
    doc = frappe.get_doc(ADJUSTMENT, adjustment_name)
    if cint(doc.docstatus) != 1 or str(doc.status or "") != "Posted":
        raise NKTIdempotencyConflict(
            "Cash Drawer request is bound to a non-posted canonical adjustment."
        )
    expected_direction = ADJUSTMENT_MAP[normalized["adjustment_type"]]["direction"]
    if (
        str(doc.cashier_shift or "") != normalized["cashier_shift"]
        or str(doc.adjustment_type or "") != normalized["adjustment_type"]
        or str(doc.direction or "") != expected_direction
        or abs(flt(doc.amount) - flt(normalized["amount"])) > TOLERANCE
        or not doc.cashier_movement
    ):
        raise NKTIdempotencyConflict(
            "Cash Drawer canonical adjustment conflicts with immutable request."
        )
    return doc


def _primary_result(
    request_id: str,
    normalized: Dict[str, Any],
    adjustment,
    *,
    replay: bool,
) -> Dict[str, Any]:
    return {
        "ok": True,
        "request_id": request_id,
        "recorded": True,
        "replayed": bool(replay),
        "cashier_shift": normalized["cashier_shift"],
        "adjustment_type": normalized["adjustment_type"],
        "direction": normalized["direction"],
        "amount": flt(normalized["amount"]),
        "cash_drawer_adjustment": adjustment.name,
        "cashier_movement": adjustment.cashier_movement,
        "official_print_available": True,
        "synchronization_pending": False,
    }


def _record_primary(
    request_id: str,
    normalized: Dict[str, Any],
) -> Dict[str, Any]:
    user = _require_operator()
    _named_lock(request_id)
    digest = _payload_hash(normalized)

    rows = _log_rows(request_id)
    if len(rows) > 1:
        raise NKTIdempotencyConflict(
            "Duplicate Cash Drawer request audit bindings exist."
        )

    if rows:
        row = rows[0]
        detail = _log_detail(row)
        if detail.get("payload_sha256") != digest:
            raise NKTIdempotencyConflict(
                "Cash Drawer request ID was reused with different business data."
            )
        if row.adjustment:
            adjustment = _verify_primary_adjustment(row.adjustment, normalized)
            return _primary_result(
                request_id,
                normalized,
                adjustment,
                replay=True,
            )
        log_name = row.name
    else:
        log = frappe.get_doc(
            {
                "doctype": CONTROL_LOG,
                "action": LOG_ACTION,
                "cashier_shift": normalized["cashier_shift"],
                "performed_by": user,
                "performed_on": now_datetime(),
                "reason": request_id,
                "details_json": json.dumps(
                    {
                        "payload_sha256": digest,
                        "state": "Recording",
                        "ui_version": UI_VERSION,
                    },
                    sort_keys=True,
                ),
            }
        )
        log.flags.ignore_permissions = True
        log.insert(ignore_permissions=True)
        log_name = log.name

    values = {
        "doctype": ADJUSTMENT,
        "cashier_shift": normalized["cashier_shift"],
        "adjustment_type": normalized["adjustment_type"],
        "amount": normalized["amount"],
        "party_name": normalized["party_name"],
        "purpose": normalized["purpose"],
        "supporting_document": normalized["supporting_document"],
    }
    for field, qty in normalized["denominations"].items():
        values[field] = qty

    doc = frappe.get_doc(values)

    # The F10/F12 business endpoint is the controlled authority boundary.
    # NKT Cashier is intentionally NOT granted raw DocType create/submit rights.
    # The endpoint already authenticated the operator, canonicalized the allowed
    # six-type payload, and the Cash Drawer controller still revalidates
    # cashier-role, own-shift, open-shift, amount/denomination and business rules.
    #
    # Therefore bypass only framework DocType permission here; do not bypass
    # document lifecycle/business validation.
    doc.flags.ignore_permissions = True
    doc.insert(ignore_permissions=True)
    doc.flags.ignore_permissions = True
    doc.submit()
    doc.reload()

    frappe.db.set_value(
        CONTROL_LOG,
        log_name,
        {
            "adjustment": doc.name,
            "details_json": json.dumps(
                {
                    "payload_sha256": digest,
                    "state": "Recorded",
                    "ui_version": UI_VERSION,
                    "cashier_movement": doc.cashier_movement,
                },
                sort_keys=True,
            ),
        },
        update_modified=False,
    )
    return _primary_result(
        request_id,
        normalized,
        doc,
        replay=False,
    )


def _edge_result(
    request_id: str,
    normalized: Dict[str, Any],
    accepted: Dict[str, Any],
) -> Dict[str, Any]:
    return {
        "ok": True,
        "request_id": request_id,
        "recorded": True,
        "replayed": bool(accepted.get("replay")),
        "cashier_shift": normalized["cashier_shift"],
        "adjustment_type": normalized["adjustment_type"],
        "direction": normalized["direction"],
        "amount": flt(normalized["amount"]),
        "cash_drawer_adjustment": None,
        "cashier_movement": None,
        "official_print_available": False,
        "synchronization_pending": True,
    }


@frappe.whitelist()
def get_cash_drawer_frontdoor_context(
    shift_name: str,
    device_id: str | None = None,
):
    user = _require_operator()
    _validate_edge_terminal_if_needed(device_id)

    shift_name = _clean(shift_name)
    if not shift_name or not frappe.db.exists(SHIFT, shift_name):
        frappe.throw(_("Cashier Shift is unavailable."))
    shift = frappe.get_doc(SHIFT, shift_name)
    if cint(shift.docstatus) != 0 or str(shift.status or "") != "Open":
        frappe.throw(_("Cashier Shift is not open."))

    privileged = user == "Administrator" or bool(
        _roles(user) & {"System Manager", "NKT OWNER", "NKT ADMINISTRATOR"}
    )
    if not privileged and str(shift.cashier or "") != user:
        frappe.throw(
            _("Only the assigned Cashier may record this Cash Drawer adjustment."),
            frappe.PermissionError,
        )

    summary = calculate_shift_summary(shift.name)
    expected = flt(summary["expected_cash"])
    if _runtime() == "Store Edge":
        expected = effective_edge_expected_cash(expected, shift.name)

    return {
        "cashier_shift": shift.name,
        "cashier": shift.cashier,
        "settlement_location": shift.settlement_location,
        "expected_cash": expected,
    }


@frappe.whitelist()
def record_cash_drawer_adjustment_frontdoor(
    payload: Any,
    request_id: str,
    device_id: str | None = None,
):
    request_id = _request_uuid(request_id)
    normalized = _normalize_frontdoor_payload(payload)
    runtime = _runtime()

    if runtime == "Primary":
        return _record_primary(request_id, normalized)

    if runtime != "Store Edge":
        frappe.throw(_("Cash Drawer adjustment is unavailable on this server."))

    _validate_edge_terminal_if_needed(device_id)

    if normalized["supporting_document"]:
        frappe.throw(
            _(
                "Offline supporting-file synchronization is not enabled yet. "
                "Record the cash movement without an attachment, or attach the file after synchronization."
            )
        )

    observed = datetime.now(PH_TZ).replace(microsecond=0)
    accepted = accept_cash_drawer_adjustment_intent_at_edge(
        request_id,
        _clean(device_id),
        observed.date().isoformat(),
        observed.isoformat(timespec="seconds"),
        {
            "cashier_shift": normalized["cashier_shift"],
            "adjustment_type": normalized["adjustment_type"],
            "amount": normalized["amount"],
            "party_name": normalized["party_name"],
            "purpose": normalized["purpose"],
            "supporting_document": "",
            "client_observed_at": normalized["client_observed_at"],
            "client_ui_version": UI_VERSION,
            **normalized["denominations"],
        },
        user=frappe.session.user,
    )
    return _edge_result(request_id, normalized, accepted)


@frappe.whitelist()
def get_cash_drawer_request_status(
    request_id: str,
    device_id: str | None = None,
):
    request_id = _request_uuid(request_id)
    runtime = _runtime()

    if runtime == "Primary":
        rows = _log_rows(request_id)
        if not rows:
            return {"found": False, "request_id": request_id}
        if len(rows) > 1:
            raise NKTIdempotencyConflict(
                "Duplicate Cash Drawer request audit bindings exist."
            )
        row = rows[0]
        return {
            "found": bool(row.adjustment),
            "request_id": request_id,
            "recorded": bool(row.adjustment),
            "cash_drawer_adjustment": row.adjustment or None,
            "official_print_available": bool(row.adjustment),
            "synchronization_pending": False,
        }

    if runtime != "Store Edge":
        return {"found": False, "request_id": request_id}

    _validate_edge_terminal_if_needed(device_id)
    if not frappe.db.exists("NKT Sync Event", request_id):
        return {"found": False, "request_id": request_id}
    event = frappe.get_doc("NKT Sync Event", request_id)
    if event.event_family != CASH_DRAWER_ADJUSTMENT_INTENT_FAMILY:
        return {"found": False, "request_id": request_id}
    if not frappe.db.exists("NKT Edge Cash Drawer Adjustment Projection", request_id):
        return {"found": False, "request_id": request_id}

    return {
        "found": True,
        "request_id": request_id,
        "recorded": True,
        "cash_drawer_adjustment": None,
        "official_print_available": False,
        "synchronization_pending": True,
    }
