from __future__ import annotations

import hashlib
import json
import math
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

import frappe
from frappe.utils import flt, getdate, now

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
from nkt_operations.nkt_store_operations.features.receiving.internal.supplier_receiving_edge_stock import (
    effective_edge_stock_qty,
)

FOUNDATION_VERSION = "C15C.10E-R2"
PH_TZ = ZoneInfo("Asia/Manila")

WAREHOUSE_RELEASE_INTENT_FAMILY = "NKT Warehouse Release Intent"
WAREHOUSE_RELEASE_INTENT_ACTION = "Confirm Physical Warehouse Release"

WAREHOUSE_ROLES = {
    "NKT Warehouse",
    "NKT OWNER",
    "NKT ADMINISTRATOR",
    "System Manager",
}
TOLERANCE = 0.000001

ALLOWED_TOP_LEVEL_KEYS = {
    "warehouse_release",
    "customer_order",
    "company",
    "customer",
    "source_warehouse",
    "release_reference",
    "driver_name",
    "plate_number",
    "client_observed_at",
    "client_ui_version",
    "items",
    "total_release_quantity",
}
ALLOWED_ITEM_KEYS = {
    "line_no",
    "warehouse_release_item",
    "customer_order_item",
    "item_code",
    "uom",
    "source_warehouse",
    "release_quantity",
}


def _runtime_role() -> str:
    return str(frappe.conf.get("nkt_runtime_role") or "Primary").strip()


def _session_user(user: Optional[str] = None) -> str:
    user = user or frappe.session.user
    if not user or user == "Guest":
        raise frappe.PermissionError("Offline warehouse release unavailable.")
    return user


def _require_warehouse_authority(user: str) -> None:
    if not (set(frappe.get_roles(user) or []) & WAREHOUSE_ROLES):
        raise frappe.PermissionError("Offline warehouse release unavailable.")


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
        raise frappe.ValidationError("Warehouse release text is too long.")
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


def _normalize_item(raw: Dict[str, Any], idx: int) -> Dict[str, Any]:
    if not isinstance(raw, dict):
        raise frappe.ValidationError(f"Warehouse release row {idx} must be an object.")
    extra = set(raw) - ALLOWED_ITEM_KEYS
    if extra:
        raise frappe.ValidationError(f"Warehouse release row {idx} contains unsupported fields.")
    if "line_no" in raw and int(raw.get("line_no") or 0) != idx:
        raise frappe.ValidationError(f"Warehouse release row {idx} has a mismatched line number.")
    return {
        "line_no": idx,
        "warehouse_release_item": _text(
            raw.get("warehouse_release_item"), f"Warehouse Release Item on row {idx}", 140
        ),
        "customer_order_item": _text(
            raw.get("customer_order_item"), f"Customer Order Item on row {idx}", 140
        ),
        "item_code": _text(raw.get("item_code"), f"Item Code on row {idx}", 140),
        "uom": _text(raw.get("uom"), f"UOM on row {idx}", 80),
        "source_warehouse": _text(
            raw.get("source_warehouse"), f"Source Warehouse on row {idx}", 180
        ),
        "release_quantity": _positive(
            raw.get("release_quantity"), f"Release Quantity on row {idx}"
        ),
    }


def _normalize_warehouse_release_intent_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(payload, dict):
        raise frappe.ValidationError("Warehouse Release Intent payload must be an object.")
    extra = set(payload) - ALLOWED_TOP_LEVEL_KEYS
    if extra:
        raise frappe.ValidationError("Warehouse Release Intent contains unsupported fields.")

    rows = payload.get("items")
    if not isinstance(rows, list) or not rows:
        raise frappe.ValidationError("At least one physical release row is required.")
    if len(rows) > 200:
        raise frappe.ValidationError("Warehouse Release Intent has too many rows.")
    items = [_normalize_item(row, idx) for idx, row in enumerate(rows, start=1)]
    total = float(f"{sum(flt(x['release_quantity']) for x in items):.6f}")
    if total <= TOLERANCE:
        raise frappe.ValidationError("Physical release total must be greater than zero.")

    if "total_release_quantity" in payload:
        supplied = flt(payload.get("total_release_quantity"))
        if abs(supplied - total) > TOLERANCE:
            raise NKTIdempotencyConflict(
                "Warehouse Release total conflicts with immutable line quantities."
            )

    return {
        "warehouse_release": _text(payload.get("warehouse_release"), "Warehouse Release", 140),
        "customer_order": _text(payload.get("customer_order"), "Customer Order", 140),
        "company": _text(payload.get("company"), "Company", 180),
        "customer": _text(payload.get("customer"), "Customer", 180),
        "source_warehouse": _text(payload.get("source_warehouse"), "Source Warehouse", 180),
        "release_reference": _text(
            payload.get("release_reference"), "Release Authorization Reference", 180
        ).upper(),
        "driver_name": _optional_text(payload.get("driver_name"), 180),
        "plate_number": _optional_text(payload.get("plate_number"), 80).upper(),
        "client_observed_at": _text(payload.get("client_observed_at"), "Client observed time", 80),
        "client_ui_version": _optional_text(payload.get("client_ui_version"), 120),
        "items": items,
        "total_release_quantity": total,
    }


def _canonical_warehouse_release_intent_json(payload: Dict[str, Any]) -> str:
    normalized = _normalize_warehouse_release_intent_payload(payload)
    return json.dumps(
        normalized,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    )


def _allowed_warehouses_for_user(user: str):
    if user == "Administrator" or set(frappe.get_roles(user) or []) & {
        "NKT OWNER", "NKT ADMINISTRATOR", "System Manager"
    }:
        return None
    rows = frappe.get_all(
        "User Permission",
        filters={"user": user, "allow": "Warehouse"},
        fields=["for_value", "applicable_for"],
        limit_page_length=500,
    )
    values = [
        row.for_value
        for row in rows
        if row.for_value
        and (not row.applicable_for or row.applicable_for == "NKT Warehouse Release")
    ]
    return set(values) if values else None


def _pending_projection_qty(
    item_code: str, warehouse: str, *, exclude_event_uuid: Optional[str] = None
) -> float:
    args = [item_code, warehouse]
    extra = ""
    if exclude_event_uuid:
        extra = " AND event_uuid != %s"
        args.append(exclude_event_uuid)
    value = frappe.db.sql(
        f"""
        SELECT COALESCE(SUM(released_qty),0)
        FROM `tabNKT Edge Warehouse Release Projection`
        WHERE item_code=%s
          AND warehouse=%s
          AND projection_state IN (
              'Pending Edge','Awaiting Primary','Primary Preserved',
              'Primary Stock Materialized'
          )
          {extra}
        """,
        tuple(args),
    )[0][0]
    return flt(value)


def effective_edge_available_qty(
    base_qty: Any, item_code: str, warehouse: str, *, exclude_event_uuid: Optional[str] = None
) -> float:
    # C15C.10H: one shared Edge stock view. Pending accepted Supplier Receiving
    # increases local physical availability; pending Warehouse Release and
    # internal-transfer source Dispatch both reserve/decrease that availability.
    return effective_edge_stock_qty(
        base_qty,
        item_code,
        warehouse,
        exclude_release_event_uuid=exclude_event_uuid,
    )


def _validate_edge_release_target(
    event_uuid: str, normalized: Dict[str, Any], user: str
) -> None:
    release_name = normalized["warehouse_release"]
    if not frappe.db.exists("NKT Warehouse Release", release_name):
        raise frappe.DoesNotExistError("Warehouse Release is unavailable on this Store Edge.")

    release = frappe.get_doc("NKT Warehouse Release", release_name)
    if int(release.docstatus or 0) != 0:
        raise frappe.ValidationError("Warehouse Release is no longer an open draft.")
    if str(release.get("release_status") or "Draft") != "Draft":
        raise frappe.ValidationError("Warehouse Release is not in Draft release state.")

    exact = {
        "customer_order": release.customer_order,
        "company": release.company,
        "customer": release.customer,
        "source_warehouse": release.get("custom_nkt_source_warehouse"),
    }
    for field, actual in exact.items():
        if str(actual or "") != str(normalized[field] or ""):
            raise NKTIdempotencyConflict(
                f"Offline physical release {field} conflicts with the local release document."
            )

    allowed = _allowed_warehouses_for_user(user)
    if allowed is not None and normalized["source_warehouse"] not in allowed:
        raise frappe.PermissionError("Offline warehouse release unavailable.")

    other = frappe.db.get_value(
        "NKT Edge Warehouse Release Projection",
        {"warehouse_release": release.name, "event_uuid": ["!=", event_uuid]},
        "event_uuid",
    )
    if other:
        raise NKTIdempotencyConflict(
            "Warehouse Release already has another pending physical-release event."
        )

    duplicate_reference = frappe.db.get_value(
        "NKT Edge Warehouse Release Projection",
        {"release_reference": normalized["release_reference"], "event_uuid": ["!=", event_uuid]},
        "event_uuid",
    )
    if duplicate_reference:
        raise NKTIdempotencyConflict(
            "Release Authorization Reference is already pending on another physical release."
        )

    live_duplicate = frappe.db.get_value(
        "NKT Warehouse Release",
        {
            "custom_nkt_mother_release_reference": normalized["release_reference"],
            "name": ["!=", release.name],
        },
        "name",
    )
    if live_duplicate:
        raise frappe.ValidationError(
            "Release Authorization Reference is already used by another Warehouse Release."
        )

    release_rows = {row.name: row for row in (release.get("items") or [])}
    seen = set()
    aggregate = {}
    for line in normalized["items"]:
        row_name = line["warehouse_release_item"]
        if row_name in seen:
            raise frappe.ValidationError("Warehouse Release Item appears more than once.")
        seen.add(row_name)
        row = release_rows.get(row_name)
        if not row:
            raise NKTIdempotencyConflict("Offline release row is not part of the local release draft.")
        checks = {
            "customer_order_item": row.customer_order_item,
            "item_code": row.item,
            "uom": row.uom,
            "source_warehouse": row.source_warehouse,
        }
        for field, actual in checks.items():
            if str(actual or "") != str(line[field] or ""):
                raise NKTIdempotencyConflict(
                    f"Offline release row {field} conflicts with the local draft."
                )
        qty = flt(line["release_quantity"])
        remaining = flt(row.remaining_quantity)
        reservation_outstanding = flt(row.get("custom_nkt_reservation_outstanding_qty"))
        if qty > remaining + TOLERANCE:
            raise frappe.ValidationError("Physical release exceeds remaining order quantity.")
        if qty > reservation_outstanding + TOLERANCE:
            raise frappe.ValidationError("Physical release exceeds local reservation outstanding quantity.")
        key = (line["item_code"], line["source_warehouse"])
        aggregate[key] = aggregate.get(key, 0.0) + qty

    for (item_code, warehouse), qty in aggregate.items():
        base_qty = flt(
            frappe.db.get_value(
                "Bin",
                {"item_code": item_code, "warehouse": warehouse},
                "actual_qty",
            )
        )
        available = effective_edge_available_qty(
            base_qty, item_code, warehouse, exclude_event_uuid=event_uuid
        )
        if qty > available + TOLERANCE:
            raise frappe.ValidationError(
                f"Physical release exceeds Store-Edge available stock for {item_code} in {warehouse}."
            )


def _projection_key(event_uuid: str, line_no: int, row_name: str) -> str:
    return hashlib.sha256(
        f"{event_uuid}|{int(line_no)}|{row_name}".encode("utf-8")
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
                    event_uuid, line["line_no"], line["warehouse_release_item"]
                ),
                "event_uuid": event_uuid,
                "line_no": line["line_no"],
                "warehouse_release": normalized["warehouse_release"],
                "warehouse_release_item": line["warehouse_release_item"],
                "customer_order": normalized["customer_order"],
                "item_code": line["item_code"],
                "warehouse": line["source_warehouse"],
                "released_qty": line["release_quantity"],
                "release_reference": normalized["release_reference"],
                "business_date": business_date,
                "projection_state": "Pending Edge",
            }
        )

    existing = frappe.get_all(
        "NKT Edge Warehouse Release Projection",
        filters={"event_uuid": event_uuid},
        fields=[
            "projection_key", "event_uuid", "line_no", "warehouse_release",
            "warehouse_release_item", "customer_order", "item_code", "warehouse",
            "released_qty", "release_reference", "business_date", "projection_state",
            "primary_ack_uuid",
        ],
        order_by="line_no asc",
        limit_page_length=500,
    )
    if existing:
        if len(existing) != len(expected):
            raise NKTIdempotencyConflict(
                "Edge physical-release projection count conflicts with immutable event."
            )
        for got, want in zip(existing, expected):
            for field in (
                "projection_key", "event_uuid", "warehouse_release",
                "warehouse_release_item", "customer_order", "item_code",
                "warehouse", "release_reference",
            ):
                if str(got.get(field) or "") != str(want[field] or ""):
                    raise NKTIdempotencyConflict(
                        "Edge physical-release projection conflicts with immutable event."
                    )
            if (
                int(got.line_no or 0) != int(want["line_no"])
                or abs(flt(got.released_qty) - flt(want["released_qty"])) > TOLERANCE
                or str(got.business_date) != str(want["business_date"])
                or got.projection_state not in (
                    "Pending Edge", "Awaiting Primary", "Primary Preserved",
                    "Primary Stock Materialized", "Finalized"
                )
            ):
                raise NKTIdempotencyConflict(
                    "Edge physical-release projection conflicts with immutable event."
                )
        return len(existing)

    for row in expected:
        frappe.get_doc(
            {"doctype": "NKT Edge Warehouse Release Projection", **row}
        ).insert(ignore_permissions=True)
    return len(expected)


def accept_warehouse_release_intent_at_edge(
    event_uuid: str,
    device_id: str,
    business_date: Any,
    settled_at: Any,
    payload: Dict[str, Any],
    *,
    user: Optional[str] = None,
) -> Dict[str, Any]:
    if _runtime_role() != "Store Edge":
        raise frappe.PermissionError("Offline warehouse release unavailable.")

    event_uuid = _uuid(event_uuid, "Physical Release Event UUID")
    user = _session_user(user)
    _require_warehouse_authority(user)

    device = device_policy_snapshot(
        device_id,
        user=user,
        requested_context="NKT Retail",
    )
    if device.get("ui_mode") != "normal":
        raise frappe.PermissionError("Offline warehouse release unavailable.")

    if event_family_policy(WAREHOUSE_RELEASE_INTENT_FAMILY).get(
        "offline_write_allowed"
    ) is not True:
        raise frappe.PermissionError("Offline warehouse release unavailable.")

    business = validate_business_time(business_date, settled_at)
    normalized = _normalize_warehouse_release_intent_payload(payload)
    _validate_edge_release_target(event_uuid, normalized, user)

    digest = canonical_payload_hash(normalized)
    envelope = {
        "event_uuid": event_uuid,
        "event_family": WAREHOUSE_RELEASE_INTENT_FAMILY,
        "event_action": WAREHOUSE_RELEASE_INTENT_ACTION,
        "operational_context": device.get("operational_context") or "NKT Retail",
        "origin_device": device_id,
        "origin_user": user,
        "business_date": business["business_date"],
        "settled_at": _manila_sql_datetime(
            business["settled_at_manila"], "Business / Settled Time"
        ),
        "client_created_at": _manila_sql_datetime(
            normalized["client_observed_at"], "Client observed time"
        ),
        "payload_sha256": digest,
    }

    event, replay = begin_event(envelope)
    canonical_json = _canonical_warehouse_release_intent_json(normalized)
    pending_name = frappe.db.exists("NKT Sync Pending Payload", event.event_uuid)

    if pending_name:
        pending = frappe.get_doc("NKT Sync Pending Payload", pending_name)
        if (
            pending.event_family != WAREHOUSE_RELEASE_INTENT_FAMILY
            or pending.payload_sha256 != digest
            or pending.payload_json != canonical_json
        ):
            raise NKTIdempotencyConflict(
                "Pending physical-release payload conflicts with immutable Event UUID."
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
                "Physical-release event exists but durable pending payload is unavailable."
            )
        frappe.get_doc(
            {
                "doctype": "NKT Sync Pending Payload",
                "event_uuid": event.event_uuid,
                "event_family": WAREHOUSE_RELEASE_INTENT_FAMILY,
                "payload_sha256": digest,
                "payload_json": canonical_json,
                "queue_state": "Accepted at Edge",
                "edge_accepted_at": now(),
                "attempt_count": 1,
                "last_attempt_at": now(),
            }
        ).insert(ignore_permissions=True)

    projection_count = _verify_or_insert_projections(
        event.event_uuid, business["business_date"], normalized
    )

    if event.sync_state not in ("Committed at Primary", "Conflict", "Failed"):
        mark_edge_accepted(event.event_uuid)
        event.reload()

    return {
        "event_uuid": event.event_uuid,
        "event_family": WAREHOUSE_RELEASE_INTENT_FAMILY,
        "sync_state": event.sync_state,
        "durable_ack": True,
        "replay": bool(replay),
        "payload_sha256": digest,
        "warehouse_release": normalized["warehouse_release"],
        "customer_order": normalized["customer_order"],
        "source_warehouse": normalized["source_warehouse"],
        "physical_release_recorded_at_edge": True,
        "edge_projection_rows": projection_count,
        "edge_projected_released_qty": normalized["total_release_quantity"],
        "primary_preservation_required": True,
        "warehouse_release_submitted": False,
        "stock_entry_created": False,
        "admin_pre_release_approval_required": False,
    }


@frappe.whitelist()
def submit_warehouse_release_intent(
    event_uuid: str,
    device_id: str,
    business_date: str,
    settled_at: str,
    payload: Any,
):
    if isinstance(payload, str):
        payload = frappe.parse_json(payload)
    return accept_warehouse_release_intent_at_edge(
        event_uuid,
        device_id,
        business_date,
        settled_at,
        payload,
        user=_session_user(),
    )
