from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime
from typing import Any, Dict, Optional
from zoneinfo import ZoneInfo

import frappe
from frappe.utils import cint, flt, getdate, now

from nkt_operations.nkt_store_operations.features.offline_edge.policy import (
    device_policy_snapshot,
    event_family_policy,
    validate_business_time,
)
from nkt_operations.nkt_store_operations.features.offline_edge.safe_sync import (
    NKTIdempotencyConflict,
    begin_event,
    canonical_payload_hash,
    mark_edge_accepted,
)
from nkt_operations.nkt_store_operations.features.receiving.supplier_receiving_physical_intent import (
    ACTION,
    FAMILY,
    canonical_supplier_receiving_payload_json,
    normalize_supplier_receiving_payload,
)
from nkt_operations.nkt_store_operations.features.offline_edge.sync_transport import _runtime_role

FOUNDATION_VERSION = "C15C.10H-R4"
PH_TZ = ZoneInfo("Asia/Manila")
TOLERANCE = 0.000001
ACTIVE_PROJECTION_STATES = (
    "Pending Edge",
    "Awaiting Primary",
    "Primary Preserved",
    "Primary Stock Materialized",
)
RECEIVING_ROLES = {
    "NKT Purchasing",
    "NKT Encoder",
    "NKT Warehouse",
    "NKT ADMINISTRATOR",
    "NKT OWNER",
    "System Manager",
}


def _uuid(value: Any, label: str) -> str:
    try:
        return str(uuid.UUID(str(value)))
    except Exception as exc:
        raise frappe.ValidationError(f"{label} is invalid.") from exc


def _session_user(user: Optional[str] = None) -> str:
    user = user or frappe.session.user
    if not user or user == "Guest":
        raise frappe.PermissionError("Offline supplier receiving unavailable.")
    return user


def _require_receiving_authority(user: str) -> None:
    if user == "Administrator":
        return
    if not (set(frappe.get_roles(user) or []) & RECEIVING_ROLES):
        raise frappe.PermissionError("Offline supplier receiving unavailable.")


def _allowed_receiving_warehouses_for_user(user: str):
    rows = frappe.get_all(
        "User Permission",
        filters={"user": user, "allow": "Warehouse"},
        fields=["for_value", "applicable_for"],
        limit_page_length=500,
    )
    values = {
        row.for_value
        for row in rows
        if row.for_value
        and (
            not row.applicable_for
            or row.applicable_for == "NKT Supplier Receiving"
        )
    }
    return values if values else None


def _warehouse_check(warehouse: str, company: str, label: str) -> None:
    row = frappe.db.get_value(
        "Warehouse",
        warehouse,
        ["company", "is_group", "disabled"],
        as_dict=True,
    )
    if not row:
        raise frappe.ValidationError(f"{label} does not exist.")
    if str(row.company or "") != str(company or ""):
        raise frappe.ValidationError(f"{label} must belong to the Purchase Order Company.")
    if cint(row.is_group):
        raise frappe.ValidationError(f"{label} cannot be a group Warehouse.")
    if cint(row.disabled):
        raise frappe.ValidationError(f"{label} is disabled.")


def _manila_datetime(value: Any, label: str) -> datetime:
    raw = str(value or "").strip()
    if not raw:
        raise frappe.ValidationError(f"{label} is required.")
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(raw)
    except Exception as exc:
        raise frappe.ValidationError(f"{label} is invalid.") from exc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=PH_TZ)
    else:
        dt = dt.astimezone(PH_TZ)
    return dt


def _manila_sql_datetime(value: Any, label: str) -> str:
    return _manila_datetime(value, label).replace(tzinfo=None).strftime(
        "%Y-%m-%d %H:%M:%S"
    )


def _active_projected_delivered_qty(
    purchase_order_item: str,
    *,
    exclude_event_uuid: Optional[str] = None,
) -> float:
    args: list[Any] = [purchase_order_item]
    extra = ""
    if exclude_event_uuid:
        extra = " AND event_uuid != %s"
        args.append(exclude_event_uuid)
    placeholders = ",".join(["%s"] * len(ACTIVE_PROJECTION_STATES))
    args.extend(ACTIVE_PROJECTION_STATES)
    rows = frappe.db.sql(
        f"""
        SELECT COALESCE(SUM(delivered_qty), 0)
        FROM `tabNKT Edge Supplier Receiving Projection`
        WHERE purchase_order_item=%s
          {extra}
          AND projection_state IN ({placeholders})
        """,
        tuple(args),
    )
    return flt(rows[0][0] if rows else 0)


def _validate_edge_target(
    event_uuid: str,
    normalized: Dict[str, Any],
    user: str,
) -> None:
    po = frappe.db.get_value(
        "Purchase Order",
        normalized["purchase_order"],
        ["name", "supplier", "company", "docstatus", "status"],
        as_dict=True,
    )
    if not po or int(po.docstatus or 0) != 1:
        raise frappe.ValidationError(
            "Purchase Order must be a submitted live Purchase Order on this Store Edge."
        )
    if str(po.status or "") in ("Closed", "Cancelled", "Completed"):
        raise frappe.ValidationError("Purchase Order is no longer open for supplier receiving.")
    if str(po.company or "") != normalized["company"]:
        raise NKTIdempotencyConflict(
            "Offline supplier receiving Company conflicts with the local Purchase Order."
        )
    if str(po.supplier or "") != normalized["supplier"]:
        raise NKTIdempotencyConflict(
            "Offline supplier receiving Supplier conflicts with the local Purchase Order."
        )

    _warehouse_check(
        normalized["receiving_warehouse"],
        normalized["company"],
        "Accepted / Receiving Warehouse",
    )
    allowed = _allowed_receiving_warehouses_for_user(user)
    if allowed is not None and normalized["receiving_warehouse"] not in allowed:
        raise frappe.PermissionError("Offline supplier receiving unavailable.")

    if normalized.get("delivery_vehicle"):
        vehicle = frappe.db.get_value(
            "NKT Vehicle",
            normalized["delivery_vehicle"],
            ["name", "plate_number", "internal_vehicle_no", "status"],
            as_dict=True,
        )
        if not vehicle or str(vehicle.status or "") != "Active":
            raise frappe.ValidationError("Selected NKT Vehicle is unavailable.")
        from nkt_operations.nkt_store_operations.doctype.nkt_vehicle.nkt_vehicle import normalize_plate
        expected_plate = normalize_plate(vehicle.plate_number) or None
        expected_internal = str(vehicle.internal_vehicle_no or "").strip() or None
        if expected_plate and normalized.get("plate_number") != expected_plate:
            raise NKTIdempotencyConflict(
                "Offline supplier receiving Plate Number conflicts with the selected NKT Vehicle."
            )
        if expected_internal and normalized.get("internal_vehicle_no") != expected_internal:
            raise NKTIdempotencyConflict(
                "Offline supplier receiving Internal Van / Truck No. conflicts with the selected NKT Vehicle."
            )

    po_items = {
        row.name: row
        for row in frappe.get_all(
            "Purchase Order Item",
            filters={
                "parent": normalized["purchase_order"],
                "parenttype": "Purchase Order",
            },
            fields=["name", "item_code", "qty", "received_qty", "uom", "stock_uom"],
            limit_page_length=5000,
        )
    }

    for line in normalized["items"]:
        source = po_items.get(line["purchase_order_item"])
        if not source:
            raise NKTIdempotencyConflict(
                "Offline supplier receiving row is not part of the local Purchase Order."
            )
        if str(source.item_code or "") != line["item_code"]:
            raise NKTIdempotencyConflict(
                "Offline supplier receiving Item conflicts with the local Purchase Order."
            )
        local_uom = str(source.uom or source.stock_uom or "")
        if local_uom != line["uom"]:
            raise NKTIdempotencyConflict(
                "Offline supplier receiving UOM conflicts with the local Purchase Order."
            )

        tracking = frappe.db.get_value(
            "Item",
            line["item_code"],
            ["has_serial_no", "has_batch_no"],
            as_dict=True,
        )
        if tracking and (cint(tracking.has_serial_no) or cint(tracking.has_batch_no)):
            raise frappe.ValidationError(
                f"Serialized/batched Item {line['item_code']} is not unlocked for 10H offline receiving."
            )

        # PO remaining is consumed by all physically delivered bags, including
        # problem/rejected bags. Only accepted bags are added to locally usable
        # stock, but rejected bags must not remain falsely open on the PO.
        pending_delivered = _active_projected_delivered_qty(
            line["purchase_order_item"],
            exclude_event_uuid=event_uuid,
        )
        effective_remaining = max(
            flt(source.qty) - flt(source.received_qty) - pending_delivered,
            0.0,
        )
        if abs(flt(line["expected_qty"]) - effective_remaining) > TOLERANCE:
            raise NKTIdempotencyConflict(
                f"Offline supplier receiving Expected Quantity is stale for {line['item_code']}."
            )
        if flt(line["delivered_qty"]) > effective_remaining + TOLERANCE:
            raise frappe.ValidationError(
                f"Physical supplier receiving exceeds the effective remaining Purchase Order quantity for {line['item_code']}."
            )

        must_whole = cint(
            frappe.db.get_value("UOM", line["uom"], "must_be_whole_number") or 0
        )
        for qty_field in (
            "delivered_qty",
            "accepted_qty",
            "damaged_qty",
            "other_rejected_qty",
            "rejected_qty",
        ):
            qty = flt(line[qty_field])
            if must_whole and abs(qty - round(qty)) > TOLERANCE:
                raise frappe.ValidationError(
                    f"{qty_field.replace('_', ' ').title()} must be a whole number for UOM {line['uom']}."
                )

        if flt(line["rejected_qty"]) > TOLERANCE:
            _warehouse_check(
                line["rejected_warehouse"],
                normalized["company"],
                "Problem-Bag Holding Warehouse",
            )
            if (
                allowed is not None
                and line["rejected_warehouse"] not in allowed
            ):
                raise frappe.PermissionError("Offline supplier receiving unavailable.")


def _projection_key(event_uuid: str, line_no: int, po_item: str) -> str:
    return hashlib.sha256(
        f"{event_uuid}|supplier-receiving|{int(line_no)}|{po_item}".encode("utf-8")
    ).hexdigest()


def _verify_or_insert_projections(
    event_uuid: str,
    business_date: str,
    settled_at: str,
    normalized: Dict[str, Any],
) -> int:
    expected = []
    for line in normalized["items"]:
        expected.append(
            {
                "projection_key": _projection_key(
                    event_uuid,
                    line["line_no"],
                    line["purchase_order_item"],
                ),
                "event_uuid": event_uuid,
                "line_no": line["line_no"],
                "purchase_order": normalized["purchase_order"],
                "purchase_order_item": line["purchase_order_item"],
                "company": normalized["company"],
                "supplier": normalized["supplier"],
                "item_code": line["item_code"],
                "uom": line["uom"],
                "warehouse": normalized["receiving_warehouse"],
                "expected_qty": line["expected_qty"],
                "delivered_qty": line["delivered_qty"],
                "accepted_qty": line["accepted_qty"],
                "rejected_qty": line["rejected_qty"],
                "rejected_warehouse": line["rejected_warehouse"],
                "business_date": business_date,
                "physical_received_at": settled_at,
                "projection_state": "Pending Edge",
            }
        )

    existing = frappe.get_all(
        "NKT Edge Supplier Receiving Projection",
        filters={"event_uuid": event_uuid},
        fields=[
            "projection_key",
            "event_uuid",
            "line_no",
            "purchase_order",
            "purchase_order_item",
            "company",
            "supplier",
            "item_code",
            "uom",
            "warehouse",
            "expected_qty",
            "delivered_qty",
            "accepted_qty",
            "rejected_qty",
            "rejected_warehouse",
            "business_date",
            "physical_received_at",
            "projection_state",
            "primary_ack_uuid",
            "primary_purchase_receipt",
            "primary_supplier_receiving",
        ],
        order_by="line_no asc",
        limit_page_length=500,
    )
    if existing:
        if len(existing) != len(expected):
            raise NKTIdempotencyConflict(
                "Edge supplier-receiving projection count conflicts with immutable Event UUID."
            )
        for got, want in zip(existing, expected):
            for field in (
                "projection_key",
                "event_uuid",
                "purchase_order",
                "purchase_order_item",
                "company",
                "supplier",
                "item_code",
                "uom",
                "warehouse",
                "rejected_warehouse",
            ):
                if str(got.get(field) or "") != str(want[field] or ""):
                    raise NKTIdempotencyConflict(
                        "Edge supplier-receiving projection conflicts with immutable Event UUID."
                    )
            for field in (
                "expected_qty",
                "delivered_qty",
                "accepted_qty",
                "rejected_qty",
            ):
                if abs(flt(got.get(field)) - flt(want[field])) > TOLERANCE:
                    raise NKTIdempotencyConflict(
                        "Edge supplier-receiving quantity projection conflicts with immutable Event UUID."
                    )
            if (
                int(got.line_no or 0) != int(want["line_no"])
                or str(got.business_date) != str(want["business_date"])
                or str(got.physical_received_at) != str(want["physical_received_at"])
                or got.projection_state
                not in (
                    "Pending Edge",
                    "Awaiting Primary",
                    "Primary Preserved",
                    "Primary Stock Materialized",
                    "Finalized",
                    "Conflict",
                )
            ):
                raise NKTIdempotencyConflict(
                    "Edge supplier-receiving projection conflicts with immutable Event UUID."
                )
        return len(existing)

    for row in expected:
        frappe.get_doc(
            {"doctype": "NKT Edge Supplier Receiving Projection", **row}
        ).insert(ignore_permissions=True)
    return len(expected)


def accept_supplier_receiving_physical_intent_at_edge(
    event_uuid: str,
    device_id: str,
    business_date: Any,
    settled_at: Any,
    payload: Dict[str, Any],
    *,
    user: Optional[str] = None,
) -> Dict[str, Any]:
    if _runtime_role() != "Store Edge":
        raise frappe.PermissionError("Offline supplier receiving unavailable.")

    event_uuid = _uuid(event_uuid, "Supplier Receiving Event UUID")
    user = _session_user(user)
    _require_receiving_authority(user)

    device = device_policy_snapshot(
        device_id,
        user=user,
        requested_context="NKT Retail",
    )
    if device.get("ui_mode") != "normal":
        raise frappe.PermissionError("Offline supplier receiving unavailable.")

    if event_family_policy(FAMILY).get("offline_write_allowed") is not True:
        raise frappe.PermissionError("Offline supplier receiving unavailable.")

    business = validate_business_time(business_date, settled_at)
    normalized = normalize_supplier_receiving_payload(payload)

    if normalized["receiving_date"] != business["business_date"]:
        raise frappe.ValidationError(
            "Receiving Date must equal the immutable Asia/Manila physical receiving date."
        )
    client_dt = _manila_datetime(
        normalized["client_observed_at"],
        "Client observed time",
    )
    if client_dt.date().isoformat() != business["business_date"]:
        raise frappe.ValidationError(
            "Client observed date must match the immutable physical receiving date."
        )

    _validate_edge_target(event_uuid, normalized, user)

    digest = canonical_payload_hash(normalized)
    settled_sql = _manila_sql_datetime(
        business["settled_at_manila"],
        "Business / Settled Time",
    )
    envelope = {
        "event_uuid": event_uuid,
        "event_family": FAMILY,
        "event_action": ACTION,
        "operational_context": device.get("operational_context") or "NKT Retail",
        "origin_device": device_id,
        "origin_user": user,
        "business_date": business["business_date"],
        "settled_at": settled_sql,
        "client_created_at": _manila_sql_datetime(
            normalized["client_observed_at"],
            "Client observed time",
        ),
        "payload_sha256": digest,
    }

    event, replay = begin_event(envelope)
    canonical_json = canonical_supplier_receiving_payload_json(normalized)
    pending_name = frappe.db.exists("NKT Sync Pending Payload", event.event_uuid)
    if pending_name:
        pending = frappe.get_doc("NKT Sync Pending Payload", pending_name)
        if (
            pending.event_family != FAMILY
            or pending.payload_sha256 != digest
            or pending.payload_json != canonical_json
        ):
            raise NKTIdempotencyConflict(
                "Pending supplier-receiving payload conflicts with immutable Event UUID."
            )
        frappe.db.set_value(
            "NKT Sync Pending Payload",
            pending.name,
            {
                "attempt_count": int(pending.attempt_count or 0) + 1,
                "last_attempt_at": now(),
            },
            update_modified=False,
        )
    elif event.sync_state not in ("Committed at Primary", "Conflict", "Failed"):
        if replay:
            raise frappe.ValidationError(
                "Supplier receiving Event exists but durable pending payload is unavailable."
            )
        frappe.get_doc(
            {
                "doctype": "NKT Sync Pending Payload",
                "event_uuid": event.event_uuid,
                "event_family": FAMILY,
                "payload_sha256": digest,
                "payload_json": canonical_json,
                "queue_state": "Accepted at Edge",
                "edge_accepted_at": now(),
                "attempt_count": 1,
                "last_attempt_at": now(),
            }
        ).insert(ignore_permissions=True)

    projection_count = _verify_or_insert_projections(
        event.event_uuid,
        business["business_date"],
        settled_sql,
        normalized,
    )

    if event.sync_state not in ("Committed at Primary", "Conflict", "Failed"):
        mark_edge_accepted(event.event_uuid)
        event.reload()

    return {
        "event_uuid": event.event_uuid,
        "event_family": FAMILY,
        "sync_state": event.sync_state,
        "durable_ack": True,
        "replay": bool(replay),
        "payload_sha256": digest,
        "purchase_order": normalized["purchase_order"],
        "supplier": normalized["supplier"],
        "receiving_warehouse": normalized["receiving_warehouse"],
        "physical_receiving_date": business["business_date"],
        "physical_receiving_time": settled_sql,
        "physical_supplier_receiving_recorded_at_edge": True,
        "edge_projection_rows": projection_count,
        "edge_projected_accepted_qty": normalized["total_accepted_qty"],
        "edge_projected_rejected_qty": normalized["total_rejected_qty"],
        "accepted_goods_locally_available": True,
        "damaged_problem_goods_locally_available": False,
        "canonical_supplier_receiving_created": False,
        "purchase_receipt_created": False,
        "stock_ledger_entry_created": False,
        "supplier_money_side_created": False,
        "primary_preservation_required": True,
    }


@frappe.whitelist()
def submit_supplier_receiving_physical_intent(
    event_uuid: str,
    device_id: str,
    business_date: str,
    settled_at: str,
    payload: Any,
):
    if isinstance(payload, str):
        payload = frappe.parse_json(payload)
    return accept_supplier_receiving_physical_intent_at_edge(
        event_uuid,
        device_id,
        business_date,
        settled_at,
        payload,
        user=frappe.session.user,
    )


def foundation_status() -> Dict[str, Any]:
    return {
        "foundation_version": FOUNDATION_VERSION,
        "event_family": FAMILY,
        "event_action": ACTION,
        "edge_acceptance_enabled": True,
        "accepted_goods_local_stock_projection_enabled": True,
        "damaged_problem_goods_local_stock_projection_enabled": False,
        "cross_midnight_true_physical_time_preserved": True,
        "employee_backdate_control_enabled": False,
        "canonical_supplier_receiving_at_edge": False,
        "canonical_purchase_receipt_at_edge": False,
        "canonical_stock_ledger_at_edge": False,
        "supplier_money_side_offline_enabled": False,
        "primary_materialization_enabled": False,
    }


def installation_probe() -> Dict[str, Any]:
    meta = frappe.get_meta("NKT Edge Supplier Receiving Projection")
    states = str(meta.get_field("projection_state").options or "")
    return {
        "foundation": foundation_status(),
        "projection_doctype_present": bool(
            frappe.db.exists("DocType", "NKT Edge Supplier Receiving Projection")
        ),
        "projection_states_present": all(
            state in states
            for state in (
                "Pending Edge",
                "Awaiting Primary",
                "Primary Preserved",
                "Primary Stock Materialized",
                "Finalized",
                "Conflict",
            )
        ),
        "projection_rows": frappe.db.count(
            "NKT Edge Supplier Receiving Projection"
        ),
    }
