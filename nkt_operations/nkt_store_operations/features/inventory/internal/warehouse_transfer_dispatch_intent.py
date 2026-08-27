from __future__ import annotations

import hashlib
import json
import math
import uuid
from datetime import datetime
from typing import Any, Dict, Optional
from zoneinfo import ZoneInfo

import frappe
from frappe.utils import cint, flt, getdate, now, today

from nkt_operations.nkt_store_operations.features.offline_edge.safe_sync import (
    NKTIdempotencyConflict,
    begin_event,
    canonical_payload_hash,
    mark_edge_accepted,
)
from nkt_operations.nkt_store_operations.features.receiving.internal.supplier_receiving_edge_stock import (
    effective_edge_stock_qty,
)
from nkt_operations.nkt_store_operations.doctype.nkt_warehouse_transfer.nkt_warehouse_transfer import (
    _resolve_transit_warehouse,
)

FOUNDATION_VERSION = "C15C.10G-R6"
PH_TZ = ZoneInfo("Asia/Manila")
FAMILY = "NKT Warehouse Transfer Dispatch Intent"
ACTION = "Confirm Source Physical Dispatch"
TOLERANCE = 0.000001

ALLOWED_TOP_LEVEL_KEYS = {
    "warehouse_transfer",
    "company",
    "transfer_date",
    "source_warehouse",
    "destination_warehouse",
    "internal_dr_no",
    "client_observed_at",
    "client_ui_version",
    "items",
    "total_dispatch_quantity",
}
ALLOWED_ITEM_KEYS = {
    "line_no",
    "warehouse_transfer_item",
    "item_code",
    "uom",
    "dispatch_quantity",
}
ACTIVE_PROJECTION_STATES = (
    "Pending Edge",
    "Awaiting Primary",
    "Primary Preserved",
    "Primary Dispatch Materialized",
)


def _runtime_role() -> str:
    return str(frappe.conf.get("nkt_runtime_role") or "Primary").strip()


def _uuid(value: Any, label: str) -> str:
    try:
        return str(uuid.UUID(str(value)))
    except Exception as exc:
        raise frappe.ValidationError(f"{label} is invalid.") from exc


def _text(value: Any, label: str, max_len: int = 240) -> str:
    out = str(value or "").strip()
    if not out:
        raise frappe.ValidationError(f"{label} is required.")
    if len(out) > max_len:
        raise frappe.ValidationError(f"{label} is too long.")
    return out


def _optional_text(value: Any, max_len: int = 240) -> str:
    out = str(value or "").strip()
    if len(out) > max_len:
        raise frappe.ValidationError("Transfer dispatch text is too long.")
    return out


def _positive(value: Any, label: str) -> float:
    try:
        out = float(value)
    except Exception as exc:
        raise frappe.ValidationError(f"{label} must be numeric.") from exc
    if not math.isfinite(out) or out <= 0:
        raise frappe.ValidationError(f"{label} must be greater than zero.")
    return float(f"{out:.6f}")


def _manila_sql_datetime(value: Any, label: str) -> str:
    if isinstance(value, datetime):
        dt = value
    else:
        raw = str(value or "").strip()
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(raw)
        except Exception as exc:
            raise frappe.ValidationError(f"{label} is not a valid datetime.") from exc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=PH_TZ)
    else:
        dt = dt.astimezone(PH_TZ)
    return dt.replace(tzinfo=None).strftime("%Y-%m-%d %H:%M:%S")


def _require_source_dispatch_authority(user: Optional[str] = None) -> str:
    user = user or frappe.session.user
    if not user or user == "Guest":
        raise frappe.PermissionError("Offline internal transfer dispatch unavailable.")
    if "NKT Warehouse" not in (frappe.get_roles(user) or []):
        raise frappe.PermissionError(
            "Only an authorized NKT Warehouse user may record source physical dispatch."
        )
    return user


def _validate_device(device_id: str, user: str) -> Dict[str, Any]:
    device_id = _text(device_id, "Device ID", 140)
    if not frappe.db.exists("NKT Device Registry", device_id):
        raise frappe.PermissionError("Offline internal transfer dispatch unavailable.")
    device = frappe.get_doc("NKT Device Registry", device_id)
    if str(device.status or "") != "Active":
        raise frappe.PermissionError("Offline internal transfer dispatch unavailable.")
    if str(device.operational_context or "") != "NKT Retail":
        raise frappe.PermissionError("Offline internal transfer dispatch unavailable.")
    assigned = str(device.assigned_user or "").strip()
    if assigned and assigned != user:
        raise frappe.PermissionError("Offline internal transfer dispatch unavailable.")
    return {
        "device_id": device.name,
        "operational_context": device.operational_context or "NKT Retail",
    }


def _allowed_source_warehouses_for_user(user: str):
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
            or row.applicable_for == "NKT Warehouse Transfer"
        )
    }
    return values if values else None


def _normalize_item(raw: Dict[str, Any], idx: int) -> Dict[str, Any]:
    if not isinstance(raw, dict):
        raise frappe.ValidationError(f"Transfer dispatch row {idx} must be an object.")
    extra = set(raw) - ALLOWED_ITEM_KEYS
    if extra:
        raise frappe.ValidationError(
            f"Transfer dispatch row {idx} contains unsupported fields."
        )
    if "line_no" in raw and int(raw.get("line_no") or 0) != idx:
        raise frappe.ValidationError(
            f"Transfer dispatch row {idx} has a mismatched line number."
        )
    return {
        "line_no": idx,
        "warehouse_transfer_item": _text(
            raw.get("warehouse_transfer_item"),
            f"Warehouse Transfer Item on row {idx}",
            140,
        ),
        "item_code": _text(raw.get("item_code"), f"Item on row {idx}", 140),
        "uom": _text(raw.get("uom"), f"UOM on row {idx}", 80),
        "dispatch_quantity": _positive(
            raw.get("dispatch_quantity"), f"Dispatch Quantity on row {idx}"
        ),
    }


def normalize_dispatch_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(payload, dict):
        raise frappe.ValidationError("Transfer Dispatch Intent payload must be an object.")
    extra = set(payload) - ALLOWED_TOP_LEVEL_KEYS
    if extra:
        raise frappe.ValidationError("Transfer Dispatch Intent contains unsupported fields.")

    rows = payload.get("items")
    if not isinstance(rows, list) or not rows:
        raise frappe.ValidationError("At least one transfer dispatch row is required.")
    if len(rows) > 200:
        raise frappe.ValidationError("Transfer Dispatch Intent has too many rows.")
    items = [_normalize_item(row, idx) for idx, row in enumerate(rows, start=1)]
    total = float(f"{sum(flt(row['dispatch_quantity']) for row in items):.6f}")
    if total <= TOLERANCE:
        raise frappe.ValidationError("Transfer Dispatch total must be greater than zero.")
    if "total_dispatch_quantity" in payload:
        supplied = flt(payload.get("total_dispatch_quantity"))
        if abs(supplied - total) > TOLERANCE:
            raise NKTIdempotencyConflict(
                "Transfer Dispatch total conflicts with immutable line quantities."
            )

    transfer_date = str(getdate(payload.get("transfer_date")))
    return {
        "warehouse_transfer": _text(
            payload.get("warehouse_transfer"), "Internal Warehouse Transfer", 140
        ),
        "company": _text(payload.get("company"), "Company", 180),
        "transfer_date": transfer_date,
        "source_warehouse": _text(
            payload.get("source_warehouse"), "Source Warehouse", 180
        ),
        "destination_warehouse": _text(
            payload.get("destination_warehouse"), "Destination Warehouse", 180
        ),
        "internal_dr_no": _optional_text(payload.get("internal_dr_no"), 180),
        "client_observed_at": _text(
            payload.get("client_observed_at"), "Client observed time", 80
        ),
        "client_ui_version": _optional_text(payload.get("client_ui_version"), 120),
        "items": items,
        "total_dispatch_quantity": total,
    }


def canonical_dispatch_payload_json(payload: Dict[str, Any]) -> str:
    return json.dumps(
        normalize_dispatch_payload(payload),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    )


def _pending_dispatch_qty(
    item_code: str,
    source_warehouse: str,
    *,
    exclude_event_uuid: Optional[str] = None,
) -> float:
    args = [item_code, source_warehouse]
    extra = ""
    if exclude_event_uuid:
        extra = " AND event_uuid != %s"
        args.append(exclude_event_uuid)
    value = frappe.db.sql(
        f"""
        SELECT COALESCE(SUM(dispatched_qty), 0)
        FROM `tabNKT Edge Warehouse Transfer Projection`
        WHERE item_code=%s
          AND source_warehouse=%s
          AND projection_action='Source Dispatch'
          AND projection_state IN (
              'Pending Edge','Awaiting Primary','Primary Preserved',
              'Primary Dispatch Materialized'
          )
          {extra}
        """,
        tuple(args),
    )[0][0]
    return flt(value)


def effective_edge_source_available_qty(
    base_qty: Any,
    item_code: str,
    source_warehouse: str,
    *,
    exclude_event_uuid: Optional[str] = None,
) -> float:
    # C15C.10H: use the same effective Edge stock view as Warehouse Release so
    # newly accepted Supplier Receiving can be used locally without permitting
    # cross-family double-spend between Release and Transfer Dispatch.
    return effective_edge_stock_qty(
        base_qty,
        item_code,
        source_warehouse,
        exclude_transfer_dispatch_event_uuid=exclude_event_uuid,
    )


def _validate_edge_target(
    event_uuid: str,
    normalized: Dict[str, Any],
    user: str,
) -> tuple[Any, str]:
    name = normalized["warehouse_transfer"]
    if not frappe.db.exists("NKT Warehouse Transfer", name):
        raise frappe.DoesNotExistError(
            "Internal Warehouse Transfer is unavailable on this Store Edge."
        )

    doc = frappe.get_doc("NKT Warehouse Transfer", name)
    if str(doc.status or "Draft") != "Draft":
        raise frappe.ValidationError(
            "Internal Warehouse Transfer is no longer available for source dispatch."
        )
    if doc.outgoing_stock_entry or doc.released_by or doc.released_at:
        raise frappe.ValidationError(
            "Internal Warehouse Transfer already contains source-dispatch audit data."
        )
    if str(getdate(doc.transfer_date)) != str(getdate(today())):
        raise frappe.ValidationError(
            "Physical source Dispatch must use today's live business date."
        )

    expected = {
        "company": doc.company,
        "transfer_date": str(getdate(doc.transfer_date)),
        "source_warehouse": doc.source_warehouse,
        "destination_warehouse": doc.destination_warehouse,
        "internal_dr_no": str(doc.internal_dr_no or "").strip(),
    }
    for field, actual in expected.items():
        if str(normalized[field] or "") != str(actual or ""):
            raise NKTIdempotencyConflict(
                f"Offline transfer dispatch {field} conflicts with the local transfer."
            )

    allowed = _allowed_source_warehouses_for_user(user)
    if allowed is not None and doc.source_warehouse not in allowed:
        raise frappe.PermissionError(
            "User is not authorized for this source warehouse."
        )

    transit = _resolve_transit_warehouse(doc.company)
    if transit in {doc.source_warehouse, doc.destination_warehouse}:
        raise frappe.ValidationError(
            "Operational Source/Destination cannot be the Goods In Transit warehouse."
        )

    local_rows = list(doc.get("items") or [])
    if len(local_rows) != len(normalized["items"]):
        raise NKTIdempotencyConflict(
            "Offline transfer dispatch item count conflicts with the local transfer."
        )

    for local, incoming in zip(local_rows, normalized["items"]):
        if (
            str(local.name) != incoming["warehouse_transfer_item"]
            or str(local.item_code) != incoming["item_code"]
            or str(local.uom) != incoming["uom"]
            or abs(flt(local.requested_qty) - flt(incoming["dispatch_quantity"]))
            > TOLERANCE
        ):
            raise NKTIdempotencyConflict(
                f"Offline transfer dispatch row {local.idx} conflicts with the local transfer."
            )
        if any(
            abs(flt(local.get(field))) > TOLERANCE
            for field in ("released_qty", "arrived_qty", "damaged_qty", "short_qty")
        ):
            raise frappe.ValidationError(
                f"Transfer row {local.idx} is not in a clean Draft quantity state."
            )

        tracking = frappe.db.get_value(
            "Item",
            local.item_code,
            ["has_serial_no", "has_batch_no"],
            as_dict=True,
        )
        if tracking and (
            cint(tracking.has_serial_no) or cint(tracking.has_batch_no)
        ):
            raise frappe.ValidationError(
                f"Serialized/batched Item {local.item_code} is not unlocked for 10G dispatch."
            )

        base_qty = flt(
            frappe.db.get_value(
                "Bin",
                {"item_code": local.item_code, "warehouse": doc.source_warehouse},
                "actual_qty",
            )
            or 0
        )
        available = effective_edge_source_available_qty(
            base_qty,
            local.item_code,
            doc.source_warehouse,
            exclude_event_uuid=event_uuid,
        )
        allow_negative = cint(
            frappe.db.get_single_value("Stock Settings", "allow_negative_stock") or 0
        )
        if flt(incoming["dispatch_quantity"]) > available + TOLERANCE and not allow_negative:
            raise frappe.ValidationError(
                f"Physical source Dispatch exceeds Store-Edge available stock for {local.item_code}."
            )

    return doc, transit


def _projection_key(event_uuid: str, line_no: int, row_name: str) -> str:
    return hashlib.sha256(
        f"{event_uuid}|dispatch|{int(line_no)}|{row_name}".encode("utf-8")
    ).hexdigest()


def _verify_or_insert_projections(
    event_uuid: str,
    business_date: str,
    normalized: Dict[str, Any],
    transit_warehouse: str,
) -> int:
    expected = []
    for line in normalized["items"]:
        expected.append(
            {
                "projection_key": _projection_key(
                    event_uuid, line["line_no"], line["warehouse_transfer_item"]
                ),
                "event_uuid": event_uuid,
                "line_no": line["line_no"],
                "projection_action": "Source Dispatch",
                "warehouse_transfer": normalized["warehouse_transfer"],
                "warehouse_transfer_item": line["warehouse_transfer_item"],
                "item_code": line["item_code"],
                "uom": line["uom"],
                "source_warehouse": normalized["source_warehouse"],
                "destination_warehouse": normalized["destination_warehouse"],
                "transit_warehouse": transit_warehouse,
                "dispatched_qty": line["dispatch_quantity"],
                "arrived_qty": 0.0,
                "business_date": business_date,
                "projection_state": "Pending Edge",
            }
        )

    existing = frappe.get_all(
        "NKT Edge Warehouse Transfer Projection",
        filters={"event_uuid": event_uuid},
        fields=[
            "projection_key",
            "event_uuid",
            "line_no",
            "projection_action",
            "warehouse_transfer",
            "warehouse_transfer_item",
            "item_code",
            "uom",
            "source_warehouse",
            "destination_warehouse",
            "transit_warehouse",
            "dispatched_qty",
            "arrived_qty",
            "business_date",
            "projection_state",
            "primary_ack_uuid",
            "primary_stock_entry",
        ],
        order_by="line_no asc",
        limit_page_length=500,
    )
    if existing:
        if len(existing) != len(expected):
            raise NKTIdempotencyConflict(
                "Edge transfer-dispatch projection count conflicts with immutable event."
            )
        for got, want in zip(existing, expected):
            for field in (
                "projection_key",
                "event_uuid",
                "projection_action",
                "warehouse_transfer",
                "warehouse_transfer_item",
                "item_code",
                "uom",
                "source_warehouse",
                "destination_warehouse",
                "transit_warehouse",
            ):
                if str(got.get(field) or "") != str(want[field] or ""):
                    raise NKTIdempotencyConflict(
                        "Edge transfer-dispatch projection conflicts with immutable event."
                    )
            if (
                int(got.line_no or 0) != int(want["line_no"])
                or abs(flt(got.dispatched_qty) - flt(want["dispatched_qty"]))
                > TOLERANCE
                or abs(flt(got.arrived_qty)) > TOLERANCE
                or str(got.business_date) != str(want["business_date"])
                or got.projection_state
                not in (
                    "Pending Edge",
                    "Awaiting Primary",
                    "Primary Preserved",
                    "Primary Dispatch Materialized",
                    "Finalized",
                )
            ):
                raise NKTIdempotencyConflict(
                    "Edge transfer-dispatch projection conflicts with immutable event."
                )
        return len(existing)

    for row in expected:
        frappe.get_doc(
            {"doctype": "NKT Edge Warehouse Transfer Projection", **row}
        ).insert(ignore_permissions=True)
    return len(expected)


def accept_dispatch_intent_at_edge(
    event_uuid: str,
    device_id: str,
    business_date: Any,
    settled_at: Any,
    payload: Dict[str, Any],
    *,
    user: Optional[str] = None,
) -> Dict[str, Any]:
    if _runtime_role() != "Store Edge":
        raise frappe.PermissionError("Offline internal transfer dispatch unavailable.")

    event_uuid = _uuid(event_uuid, "Transfer Dispatch Event UUID")
    user = _require_source_dispatch_authority(user)
    device = _validate_device(device_id, user)
    normalized = normalize_dispatch_payload(payload)

    settled_sql = _manila_sql_datetime(settled_at, "Business / Settled Time")
    if str(getdate(business_date)) != str(getdate(settled_sql)):
        raise frappe.ValidationError(
            "Business Date must equal the Asia/Manila date of physical Dispatch."
        )
    if str(getdate(business_date)) != str(getdate(today())):
        raise frappe.ValidationError(
            "Physical source Dispatch must use today's live business date."
        )

    doc, transit = _validate_edge_target(event_uuid, normalized, user)
    digest = canonical_payload_hash(normalized)
    envelope = {
        "event_uuid": event_uuid,
        "event_family": FAMILY,
        "event_action": ACTION,
        "operational_context": device["operational_context"],
        "origin_device": device["device_id"],
        "origin_user": user,
        "business_date": str(getdate(business_date)),
        "settled_at": settled_sql,
        "client_created_at": _manila_sql_datetime(
            normalized["client_observed_at"], "Client observed time"
        ),
        "payload_sha256": digest,
    }

    event, replay = begin_event(envelope)
    canonical_json = canonical_dispatch_payload_json(normalized)
    pending_name = frappe.db.exists("NKT Sync Pending Payload", event.event_uuid)

    if pending_name:
        pending = frappe.get_doc("NKT Sync Pending Payload", pending_name)
        if (
            pending.event_family != FAMILY
            or pending.payload_sha256 != digest
            or pending.payload_json != canonical_json
        ):
            raise NKTIdempotencyConflict(
                "Pending transfer-dispatch payload conflicts with immutable Event UUID."
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
                "Transfer Dispatch event exists but durable pending payload is unavailable."
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
        str(getdate(business_date)),
        normalized,
        transit,
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
        "warehouse_transfer": doc.name,
        "source_warehouse": normalized["source_warehouse"],
        "destination_warehouse": normalized["destination_warehouse"],
        "transit_warehouse": transit,
        "physical_dispatch_recorded_at_edge": True,
        "edge_projection_rows": projection_count,
        "edge_projected_dispatch_qty": normalized["total_dispatch_quantity"],
        "primary_preservation_required": True,
        "canonical_transfer_released": False,
        "stock_entry_created": False,
        "admin_pre_release_approval_required": False,
    }


@frappe.whitelist()
def submit_dispatch_intent(
    event_uuid: str,
    device_id: str,
    business_date: str,
    settled_at: str,
    payload: Any,
):
    if isinstance(payload, str):
        payload = frappe.parse_json(payload)
    return accept_dispatch_intent_at_edge(
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
        "stage": "Source dispatch intent/projection only",
        "canonical_stock_materialization_enabled": False,
        "destination_arrival_enabled": False,
    }


def installation_probe() -> Dict[str, Any]:
    return {
        "foundation": foundation_status(),
        "edge_projection_doctype": bool(
            frappe.db.exists("DocType", "NKT Edge Warehouse Transfer Projection")
        ),
        "primary_journal_doctype": bool(
            frappe.db.exists("DocType", "NKT Primary Warehouse Transfer Dispatch Intent")
        ),
        "sync_receipt_state_present": "Warehouse Transfer Dispatch Intent Preserved"
        in str(
            frappe.get_meta("NKT Sync Primary Receipt")
            .get_field("materialization_state")
            .options
            or ""
        ),
        "edge_projection_rows": frappe.db.count(
            "NKT Edge Warehouse Transfer Projection"
        ),
        "primary_dispatch_journal_rows": frappe.db.count(
            "NKT Primary Warehouse Transfer Dispatch Intent"
        ),
        "warehouse_transfer_rows": frappe.db.count("NKT Warehouse Transfer"),
        "stock_entry_rows": frappe.db.count("Stock Entry"),
        "sync_event_rows": frappe.db.count("NKT Sync Event"),
        "sync_pending_rows": frappe.db.count("NKT Sync Pending Payload"),
        "sync_primary_receipt_rows": frappe.db.count("NKT Sync Primary Receipt"),
    }
