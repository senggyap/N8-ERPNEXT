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
from nkt_operations.nkt_store_operations.doctype.nkt_warehouse_transfer.nkt_warehouse_transfer import (
    _resolve_transit_warehouse,
)

FOUNDATION_VERSION = "C15C.10G-R10"
PH_TZ = ZoneInfo("Asia/Manila")
FAMILY = "NKT Warehouse Transfer Arrival Intent"
ACTION = "Confirm Destination Physical Arrival"
TOLERANCE = 0.000001

ALLOWED_TOP_LEVEL_KEYS = {
    "warehouse_transfer",
    "company",
    "transfer_date",
    "source_warehouse",
    "destination_warehouse",
    "outgoing_stock_entry",
    "transit_warehouse",
    "client_observed_at",
    "client_ui_version",
    "items",
    "total_arrival_quantity",
}
ALLOWED_ITEM_KEYS = {
    "line_no",
    "warehouse_transfer_item",
    "item_code",
    "uom",
    "released_quantity",
    "cumulative_arrived_before",
    "remaining_before",
    "arrival_quantity",
}

PENDING_ARRIVAL_PROJECTION_STATES = (
    "Pending Edge",
    "Awaiting Primary",
    "Primary Arrival Preserved",
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
        raise frappe.ValidationError("Transfer arrival text is too long.")
    return out


def _nonnegative(value: Any, label: str) -> float:
    try:
        out = float(value)
    except Exception as exc:
        raise frappe.ValidationError(f"{label} must be numeric.") from exc
    if not math.isfinite(out) or out < 0:
        raise frappe.ValidationError(f"{label} must be zero or greater.")
    return float(f"{out:.6f}")


def _positive(value: Any, label: str) -> float:
    out = _nonnegative(value, label)
    if out <= 0:
        raise frappe.ValidationError(f"{label} must be greater than zero.")
    return out


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


def _require_destination_arrival_authority(user: Optional[str] = None) -> str:
    user = user or frappe.session.user
    if not user or user == "Guest":
        raise frappe.PermissionError("Offline internal transfer Arrival unavailable.")
    if "NKT Warehouse" not in (frappe.get_roles(user) or []):
        raise frappe.PermissionError(
            "Only an authorized NKT Warehouse user may record destination physical Arrival."
        )
    return user


def _validate_device(device_id: str, user: str) -> Dict[str, Any]:
    device_id = _text(device_id, "Device ID", 140)
    if not frappe.db.exists("NKT Device Registry", device_id):
        raise frappe.PermissionError("Offline internal transfer Arrival unavailable.")
    device = frappe.get_doc("NKT Device Registry", device_id)
    if str(device.status or "") != "Active":
        raise frappe.PermissionError("Offline internal transfer Arrival unavailable.")
    if str(device.operational_context or "") != "NKT Retail":
        raise frappe.PermissionError("Offline internal transfer Arrival unavailable.")
    assigned = str(device.assigned_user or "").strip()
    if assigned and assigned != user:
        raise frappe.PermissionError("Offline internal transfer Arrival unavailable.")
    return {
        "device_id": device.name,
        "operational_context": device.operational_context or "NKT Retail",
    }


def _allowed_destination_warehouses_for_user(user: str):
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
        raise frappe.ValidationError(f"Transfer Arrival row {idx} must be an object.")
    extra = set(raw) - ALLOWED_ITEM_KEYS
    if extra:
        raise frappe.ValidationError(
            f"Transfer Arrival row {idx} contains unsupported fields."
        )
    if "line_no" in raw and int(raw.get("line_no") or 0) != idx:
        raise frappe.ValidationError(
            f"Transfer Arrival row {idx} has a mismatched line number."
        )

    released = _positive(raw.get("released_quantity"), f"Released Quantity on row {idx}")
    cumulative = _nonnegative(
        raw.get("cumulative_arrived_before"),
        f"Cumulative Arrived Before on row {idx}",
    )
    remaining = _positive(raw.get("remaining_before"), f"Remaining Before on row {idx}")
    arrival = _positive(raw.get("arrival_quantity"), f"Arrival Quantity on row {idx}")

    if cumulative > released + TOLERANCE:
        raise frappe.ValidationError(
            f"Transfer Arrival row {idx} cumulative quantity exceeds released quantity."
        )
    if abs((released - cumulative) - remaining) > TOLERANCE:
        raise NKTIdempotencyConflict(
            f"Transfer Arrival row {idx} remaining snapshot conflicts with released/cumulative quantities."
        )
    if arrival > remaining + TOLERANCE:
        raise frappe.ValidationError(
            f"Transfer Arrival row {idx} quantity exceeds the recorded remaining transit quantity."
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
        "released_quantity": released,
        "cumulative_arrived_before": cumulative,
        "remaining_before": remaining,
        "arrival_quantity": arrival,
    }


def normalize_arrival_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(payload, dict):
        raise frappe.ValidationError("Transfer Arrival Intent payload must be an object.")
    extra = set(payload) - ALLOWED_TOP_LEVEL_KEYS
    if extra:
        raise frappe.ValidationError("Transfer Arrival Intent contains unsupported fields.")

    rows = payload.get("items")
    if not isinstance(rows, list) or not rows:
        raise frappe.ValidationError("At least one transfer Arrival row is required.")
    if len(rows) > 200:
        raise frappe.ValidationError("Transfer Arrival Intent has too many rows.")
    items = [_normalize_item(row, idx) for idx, row in enumerate(rows, start=1)]

    seen = set()
    for row in items:
        if row["item_code"] in seen:
            raise frappe.ValidationError(
                "Transfer Arrival Intent contains duplicate Item lineage."
            )
        seen.add(row["item_code"])

    total = float(f"{sum(flt(row['arrival_quantity']) for row in items):.6f}")
    if total <= TOLERANCE:
        raise frappe.ValidationError("Transfer Arrival total must be greater than zero.")
    if "total_arrival_quantity" in payload:
        supplied = flt(payload.get("total_arrival_quantity"))
        if abs(supplied - total) > TOLERANCE:
            raise NKTIdempotencyConflict(
                "Transfer Arrival total conflicts with immutable line quantities."
            )

    return {
        "warehouse_transfer": _text(
            payload.get("warehouse_transfer"), "Internal Warehouse Transfer", 140
        ),
        "company": _text(payload.get("company"), "Company", 180),
        "transfer_date": str(getdate(payload.get("transfer_date"))),
        "source_warehouse": _text(
            payload.get("source_warehouse"), "Source Warehouse", 180
        ),
        "destination_warehouse": _text(
            payload.get("destination_warehouse"), "Destination Warehouse", 180
        ),
        "outgoing_stock_entry": _text(
            payload.get("outgoing_stock_entry"), "Outgoing Stock Entry", 180
        ),
        "transit_warehouse": _text(
            payload.get("transit_warehouse"), "Goods In Transit Warehouse", 180
        ),
        "client_observed_at": _text(
            payload.get("client_observed_at"), "Client observed time", 80
        ),
        "client_ui_version": _optional_text(payload.get("client_ui_version"), 120),
        "items": items,
        "total_arrival_quantity": total,
    }


def canonical_arrival_payload_json(payload: Dict[str, Any]) -> str:
    return json.dumps(
        normalize_arrival_payload(payload),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    )


def _pending_arrival_qty(
    transfer_name: str,
    item_code: str,
    *,
    exclude_event_uuid: Optional[str] = None,
) -> float:
    args = [transfer_name, item_code]
    extra = ""
    if exclude_event_uuid:
        extra = " AND event_uuid != %s"
        args.append(exclude_event_uuid)
    rows = frappe.db.sql(
        f"""
        SELECT COALESCE(SUM(arrived_qty), 0)
        FROM `tabNKT Edge Warehouse Transfer Projection`
        WHERE warehouse_transfer=%s
          AND item_code=%s
          AND projection_action='Destination Arrival'
          AND projection_state IN ('Pending Edge','Awaiting Primary','Primary Arrival Preserved')
          {extra}
        """,
        tuple(args),
    )
    return flt(rows[0][0] if rows else 0)


def _has_unrebased_materialized_arrival(transfer_name: str) -> bool:
    return bool(
        frappe.db.exists(
            "NKT Edge Warehouse Transfer Projection",
            {
                "warehouse_transfer": transfer_name,
                "projection_action": "Destination Arrival",
                "projection_state": "Primary Arrival Materialized",
            },
        )
    )


def _validate_edge_target(
    event_uuid: str,
    normalized: Dict[str, Any],
    user: str,
    business_date: Any,
) -> tuple[Any, Any]:
    name = normalized["warehouse_transfer"]
    if not frappe.db.exists("NKT Warehouse Transfer", name):
        raise frappe.DoesNotExistError(
            "Internal Warehouse Transfer is unavailable on this Store Edge."
        )
    doc = frappe.get_doc("NKT Warehouse Transfer", name)
    if str(doc.status or "") != "In Transit":
        raise frappe.ValidationError(
            "Internal Warehouse Transfer is not available for destination Arrival."
        )
    if not doc.outgoing_stock_entry or doc.incoming_stock_entry:
        raise frappe.ValidationError(
            "Internal Warehouse Transfer does not have a valid open transit lineage."
        )
    if getdate(doc.transfer_date) > getdate(business_date):
        raise frappe.ValidationError(
            "Destination Arrival cannot precede the transfer business date."
        )

    expected = {
        "company": doc.company,
        "transfer_date": str(getdate(doc.transfer_date)),
        "source_warehouse": doc.source_warehouse,
        "destination_warehouse": doc.destination_warehouse,
        "outgoing_stock_entry": doc.outgoing_stock_entry,
    }
    for field, actual in expected.items():
        if str(normalized[field] or "") != str(actual or ""):
            raise NKTIdempotencyConflict(
                f"Offline transfer Arrival {field} conflicts with the local transfer."
            )

    allowed = _allowed_destination_warehouses_for_user(user)
    if allowed is not None and doc.destination_warehouse not in allowed:
        raise frappe.PermissionError(
            "User is not authorized for this destination warehouse."
        )

    transit = _resolve_transit_warehouse(doc.company)
    if normalized["transit_warehouse"] != transit:
        raise NKTIdempotencyConflict(
            "Offline transfer Arrival Goods In Transit warehouse conflicts with the local transfer."
        )

    outgoing = frappe.get_doc("Stock Entry", doc.outgoing_stock_entry)
    if (
        int(outgoing.docstatus or 0) != 1
        or str(outgoing.stock_entry_type or "") != "Material Transfer"
        or str(outgoing.purpose or "") != "Material Transfer"
        or int(outgoing.add_to_transit or 0) != 1
        or str(outgoing.from_warehouse or "") != str(doc.source_warehouse)
    ):
        raise frappe.ValidationError(
            "Outgoing transit Stock Entry is not valid for destination Arrival."
        )
    if str(outgoing.to_warehouse or "") and str(outgoing.to_warehouse) != transit:
        raise NKTIdempotencyConflict(
            "Outgoing transit Stock Entry header does not match Goods In Transit."
        )

    if _has_unrebased_materialized_arrival(doc.name):
        raise frappe.ValidationError(
            "A materialized Arrival is still awaiting local stock rebase. Complete rebase before another Arrival."
        )

    local_by_item = {row.item_code: row for row in (doc.items or [])}
    payload_by_item = {row["item_code"]: row for row in normalized["items"]}

    active = {}
    for item_code, local in local_by_item.items():
        tracking = frappe.db.get_value(
            "Item",
            item_code,
            ["has_serial_no", "has_batch_no"],
            as_dict=True,
        )
        if tracking and (cint(tracking.has_serial_no) or cint(tracking.has_batch_no)):
            raise frappe.ValidationError(
                f"Serialized/batched Item {item_code} is not unlocked for 10G Arrival."
            )

        released = flt(local.released_qty)
        arrived = flt(local.arrived_qty)
        if released <= TOLERANCE or arrived < -TOLERANCE or arrived > released + TOLERANCE:
            raise frappe.ValidationError(
                f"Transfer row {local.idx} has invalid released/arrived quantity state."
            )

        pending = _pending_arrival_qty(
            doc.name,
            item_code,
            exclude_event_uuid=event_uuid,
        )
        virtual_cumulative = arrived + pending
        remaining = max(0.0, released - virtual_cumulative)
        if remaining > TOLERANCE:
            active[item_code] = {
                "row": local,
                "released": released,
                "virtual_cumulative": virtual_cumulative,
                "remaining": remaining,
            }

    if set(payload_by_item) != set(active):
        raise frappe.ValidationError(
            "Arrival quantities must cover every Item that still has effective in-transit quantity."
        )

    outgoing_rows = {row.item_code: row for row in outgoing.items}
    for item_code, state in active.items():
        incoming = payload_by_item[item_code]
        local = state["row"]
        out = outgoing_rows.get(item_code)
        if not out:
            raise NKTIdempotencyConflict(
                f"Outgoing Stock Entry is missing Item {item_code}."
            )
        if (
            str(out.s_warehouse or "") != str(doc.source_warehouse)
            or str(out.t_warehouse or "") != transit
            or abs(flt(out.qty) - state["released"]) > TOLERANCE
        ):
            raise NKTIdempotencyConflict(
                f"Outgoing Stock Entry lineage conflicts with transfer Item {item_code}."
            )
        if (
            str(local.name) != incoming["warehouse_transfer_item"]
            or str(local.uom) != incoming["uom"]
            or abs(incoming["released_quantity"] - state["released"]) > TOLERANCE
            or abs(incoming["cumulative_arrived_before"] - state["virtual_cumulative"]) > TOLERANCE
            or abs(incoming["remaining_before"] - state["remaining"]) > TOLERANCE
        ):
            raise NKTIdempotencyConflict(
                f"Offline transfer Arrival row {local.idx} conflicts with the effective local transit snapshot."
            )
        if incoming["arrival_quantity"] > state["remaining"] + TOLERANCE:
            raise frappe.ValidationError(
                f"Physical destination Arrival exceeds effective remaining transit quantity for {item_code}."
            )
        must_whole = cint(
            frappe.db.get_value("UOM", local.uom, "must_be_whole_number") or 0
        )
        if must_whole and abs(
            incoming["arrival_quantity"] - round(incoming["arrival_quantity"])
        ) > TOLERANCE:
            raise frappe.ValidationError(
                f"Arrival Qty must be a whole number for UOM {local.uom}."
            )

    return doc, outgoing


def _projection_key(event_uuid: str, line_no: int, row_name: str) -> str:
    return hashlib.sha256(
        f"{event_uuid}|arrival|{int(line_no)}|{row_name}".encode("utf-8")
    ).hexdigest()


def _verify_or_insert_projections(
    event_uuid: str,
    business_date: str,
    normalized: Dict[str, Any],
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
                "projection_action": "Destination Arrival",
                "warehouse_transfer": normalized["warehouse_transfer"],
                "warehouse_transfer_item": line["warehouse_transfer_item"],
                "item_code": line["item_code"],
                "uom": line["uom"],
                "source_warehouse": normalized["source_warehouse"],
                "destination_warehouse": normalized["destination_warehouse"],
                "transit_warehouse": normalized["transit_warehouse"],
                "dispatched_qty": 0.0,
                "arrived_qty": line["arrival_quantity"],
                "cumulative_arrived_before": line["cumulative_arrived_before"],
                "remaining_before": line["remaining_before"],
                "business_date": business_date,
                "projection_state": "Pending Edge",
            }
        )

    existing = frappe.get_all(
        "NKT Edge Warehouse Transfer Projection",
        filters={"event_uuid": event_uuid},
        fields=[
            "projection_key","event_uuid","line_no","projection_action",
            "warehouse_transfer","warehouse_transfer_item","item_code","uom",
            "source_warehouse","destination_warehouse","transit_warehouse",
            "dispatched_qty","arrived_qty","cumulative_arrived_before",
            "remaining_before","business_date","projection_state",
            "primary_ack_uuid","primary_stock_entry",
        ],
        order_by="line_no asc",
        limit_page_length=500,
    )
    if existing:
        if len(existing) != len(expected):
            raise NKTIdempotencyConflict(
                "Edge transfer-Arrival projection count conflicts with immutable event."
            )
        for got, want in zip(existing, expected):
            for field in (
                "projection_key","event_uuid","projection_action",
                "warehouse_transfer","warehouse_transfer_item","item_code","uom",
                "source_warehouse","destination_warehouse","transit_warehouse",
            ):
                if str(got.get(field) or "") != str(want[field] or ""):
                    raise NKTIdempotencyConflict(
                        "Edge transfer-Arrival projection conflicts with immutable event."
                    )
            for field in (
                "arrived_qty","cumulative_arrived_before","remaining_before",
            ):
                if abs(flt(got.get(field)) - flt(want[field])) > TOLERANCE:
                    raise NKTIdempotencyConflict(
                        "Edge transfer-Arrival quantity projection conflicts with immutable event."
                    )
            if (
                int(got.line_no or 0) != int(want["line_no"])
                or abs(flt(got.dispatched_qty)) > TOLERANCE
                or str(got.business_date) != str(want["business_date"])
                or got.projection_state not in (
                    "Pending Edge","Awaiting Primary","Primary Arrival Preserved",
                    "Primary Arrival Materialized","Finalized",
                )
            ):
                raise NKTIdempotencyConflict(
                    "Edge transfer-Arrival projection conflicts with immutable event."
                )
        return len(existing)

    for row in expected:
        frappe.get_doc(
            {"doctype": "NKT Edge Warehouse Transfer Projection", **row}
        ).insert(ignore_permissions=True)
    return len(expected)


def accept_arrival_intent_at_edge(
    event_uuid: str,
    device_id: str,
    business_date: Any,
    settled_at: Any,
    payload: Dict[str, Any],
    *,
    user: Optional[str] = None,
) -> Dict[str, Any]:
    if _runtime_role() != "Store Edge":
        raise frappe.PermissionError("Offline internal transfer Arrival unavailable.")

    event_uuid = _uuid(event_uuid, "Transfer Arrival Event UUID")
    user = _require_destination_arrival_authority(user)
    device = _validate_device(device_id, user)
    normalized = normalize_arrival_payload(payload)

    settled_sql = _manila_sql_datetime(settled_at, "Business / Settled Time")
    if str(getdate(business_date)) != str(getdate(settled_sql)):
        raise frappe.ValidationError(
            "Business Date must equal the Asia/Manila date of physical destination Arrival."
        )
    if str(getdate(business_date)) != str(getdate(today())):
        raise frappe.ValidationError(
            "Physical destination Arrival must use today's live business date."
        )
    if getdate(normalized["transfer_date"]) > getdate(business_date):
        raise frappe.ValidationError(
            "Destination Arrival cannot precede the transfer business date."
        )

    doc, outgoing = _validate_edge_target(
        event_uuid, normalized, user, business_date
    )
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
    canonical_json = canonical_arrival_payload_json(normalized)
    pending_name = frappe.db.exists("NKT Sync Pending Payload", event.event_uuid)
    if pending_name:
        pending = frappe.get_doc("NKT Sync Pending Payload", pending_name)
        if (
            pending.event_family != FAMILY
            or pending.payload_sha256 != digest
            or pending.payload_json != canonical_json
        ):
            raise NKTIdempotencyConflict(
                "Pending transfer-Arrival payload conflicts with immutable Event UUID."
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
                "Transfer Arrival event exists but durable pending payload is unavailable."
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
        "outgoing_stock_entry": outgoing.name,
        "source_warehouse": normalized["source_warehouse"],
        "destination_warehouse": normalized["destination_warehouse"],
        "transit_warehouse": normalized["transit_warehouse"],
        "physical_arrival_recorded_at_edge": True,
        "edge_projection_rows": projection_count,
        "edge_projected_arrival_qty": normalized["total_arrival_quantity"],
        "primary_preservation_required": True,
        "canonical_arrival_posted": False,
        "stock_entry_created": False,
        "admin_pre_arrival_approval_required": False,
    }


@frappe.whitelist()
def submit_arrival_intent(
    event_uuid: str,
    device_id: str,
    business_date: str,
    settled_at: str,
    payload: Any,
):
    if isinstance(payload, str):
        payload = frappe.parse_json(payload)
    return accept_arrival_intent_at_edge(
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
        "stage": "Destination Arrival intent/projection + Primary preservation",
        "canonical_stock_materialization_enabled": False,
        "partial_arrival_snapshot_reserved_at_primary": True,
        "destination_arrival_offline_intent_enabled": True,
        "split_arrival_destructive_matrix_run": False,
    }


def installation_probe() -> Dict[str, Any]:
    projection_meta = frappe.get_meta("NKT Edge Warehouse Transfer Projection")
    states = str(projection_meta.get_field("projection_state").options or "")
    return {
        "foundation": foundation_status(),
        "edge_projection_doctype": bool(
            frappe.db.exists("DocType", "NKT Edge Warehouse Transfer Projection")
        ),
        "primary_arrival_journal_doctype": bool(
            frappe.db.exists("DocType", "NKT Primary Warehouse Transfer Arrival Intent")
        ),
        "primary_arrival_item_doctype": bool(
            frappe.db.exists("DocType", "NKT Primary Warehouse Transfer Arrival Intent Item")
        ),
        "sync_receipt_state_present": "Warehouse Transfer Arrival Intent Preserved"
        in str(
            frappe.get_meta("NKT Sync Primary Receipt")
            .get_field("materialization_state")
            .options
            or ""
        ),
        "projection_arrival_states_present": all(
            state in states
            for state in ("Primary Arrival Preserved", "Primary Arrival Materialized")
        ),
        "edge_projection_rows": frappe.db.count("NKT Edge Warehouse Transfer Projection"),
        "primary_arrival_journal_rows": frappe.db.count(
            "NKT Primary Warehouse Transfer Arrival Intent"
        ),
    }
