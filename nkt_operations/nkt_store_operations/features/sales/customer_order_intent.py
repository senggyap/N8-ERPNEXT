from __future__ import annotations

import hashlib
import json
import math
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

import frappe
from frappe.utils import flt, now

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

FOUNDATION_VERSION = "C15C.9G-R2"
PH_TZ = ZoneInfo("Asia/Manila")

ORDER_INTENT_FAMILY = "NKT Customer Order Intent"
ORDER_INTENT_ACTION = "Create Order Intent"

ALLOWED_TOP_LEVEL_KEYS = {
    "company",
    "customer",
    "default_warehouse",
    "account_sale",
    "notes",
    "client_observed_at",
    "client_ui_version",
    "items",
}
ALLOWED_ITEM_KEYS = {"item_code", "qty", "rate", "warehouse", "line_no"}

ENCODER_ROLES = {
    "NKT Encoder",
    "NKT OWNER",
    "NKT ADMINISTRATOR",
    "System Manager",
}

FORBIDDEN_MONEY_KEYS = {
    "declared_payments",
    "payment_method",
    "payment_reference",
    "reference_number",
    "cash_tendered",
    "amount_paid",
    "paid_amount",
    "card_surcharge",
    "change_amount",
}


def _runtime_role() -> str:
    return str(frappe.conf.get("nkt_runtime_role") or "Primary").strip()


def _session_user(user: Optional[str] = None) -> str:
    user = user or frappe.session.user
    if not user or user == "Guest":
        raise frappe.PermissionError("Offline order intent unavailable.")
    return user


def _require_encoder_authority(user: str) -> None:
    roles = set(frappe.get_roles(user) or [])
    if not (roles & ENCODER_ROLES):
        raise frappe.PermissionError("Offline order intent unavailable.")


def _nonempty_text(value: Any, label: str, max_len: int = 240) -> str:
    out = str(value or "").strip()
    if not out:
        raise frappe.ValidationError(f"{label} is required.")
    if len(out) > max_len:
        raise frappe.ValidationError(f"{label} is too long.")
    return out


def _optional_text(value: Any, max_len: int) -> str:
    out = str(value or "").strip()
    if len(out) > max_len:
        raise frappe.ValidationError("Offline order intent text is too long.")
    return out


def _bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value in (1, "1", "true", "True", "yes", "Yes"):
        return True
    if value in (0, "0", "false", "False", "no", "No", None, ""):
        return False
    raise frappe.ValidationError("Account Sale must be true or false.")


def _positive_number(value: Any, label: str) -> float:
    try:
        out = float(value)
    except Exception as exc:
        raise frappe.ValidationError(f"{label} must be numeric.") from exc
    if not math.isfinite(out) or out <= 0:
        raise frappe.ValidationError(f"{label} must be greater than zero.")
    return float(f"{out:.6f}")


def _nonnegative_number(value: Any, label: str) -> float:
    try:
        out = float(value)
    except Exception as exc:
        raise frappe.ValidationError(f"{label} must be numeric.") from exc
    if not math.isfinite(out) or out < 0:
        raise frappe.ValidationError(f"{label} must be zero or greater.")
    return float(f"{out:.6f}")


def _normalize_order_intent_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(payload, dict):
        raise frappe.ValidationError("Offline order intent payload must be an object.")

    forbidden = set(payload) & FORBIDDEN_MONEY_KEYS
    if forbidden:
        raise frappe.ValidationError(
            "Payment/tender data is not allowed in the Customer Order Intent stage."
        )

    extra = set(payload) - ALLOWED_TOP_LEVEL_KEYS
    if extra:
        raise frappe.ValidationError("Offline order intent contains unsupported fields.")

    company = _nonempty_text(payload.get("company"), "Company")
    customer = _nonempty_text(payload.get("customer"), "Customer")
    default_warehouse = _nonempty_text(
        payload.get("default_warehouse"), "Default Warehouse"
    )
    account_sale = _bool(payload.get("account_sale"))
    notes = _optional_text(payload.get("notes"), 1000)
    client_observed_at = _nonempty_text(
        payload.get("client_observed_at"), "Client observed time", max_len=80
    )
    client_ui_version = _optional_text(payload.get("client_ui_version"), 120)

    raw_items = payload.get("items")
    if not isinstance(raw_items, list) or not raw_items:
        raise frappe.ValidationError("At least one order item is required.")
    if len(raw_items) > 200:
        raise frappe.ValidationError("Offline order intent has too many item lines.")

    items: List[Dict[str, Any]] = []
    for idx, raw in enumerate(raw_items, start=1):
        if not isinstance(raw, dict):
            raise frappe.ValidationError(f"Order item line {idx} must be an object.")
        if set(raw) - ALLOWED_ITEM_KEYS:
            raise frappe.ValidationError(
                f"Order item line {idx} contains unsupported fields."
            )

        # Canonical pending payloads already contain line_no. The normalizer must
        # therefore be idempotent when the transport layer validates the durable
        # payload again. A supplied line_no is accepted only when it exactly
        # matches the actual list position; it cannot reorder/spoof a line.
        if "line_no" in raw:
            try:
                supplied_line_no = int(raw.get("line_no"))
            except Exception as exc:
                raise frappe.ValidationError(
                    f"Order item line {idx} has an invalid canonical line number."
                ) from exc
            if supplied_line_no != idx:
                raise frappe.ValidationError(
                    f"Order item line {idx} has a mismatched canonical line number."
                )

        item_code = _nonempty_text(
            raw.get("item_code"), f"Item Code on line {idx}"
        )
        qty = _positive_number(raw.get("qty"), f"Qty on line {idx}")
        rate = _nonnegative_number(raw.get("rate"), f"Rate on line {idx}")
        warehouse = _nonempty_text(
            raw.get("warehouse") or default_warehouse,
            f"Warehouse on line {idx}",
        )
        items.append({
            "line_no": idx,
            "item_code": item_code,
            "qty": qty,
            "rate": rate,
            "warehouse": warehouse,
        })

    return {
        "company": company,
        "customer": customer,
        "default_warehouse": default_warehouse,
        "account_sale": account_sale,
        "notes": notes,
        "client_observed_at": client_observed_at,
        "client_ui_version": client_ui_version,
        "items": items,
    }


def _canonical_order_intent_json(payload: Dict[str, Any]) -> str:
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    )


def _manila_sql_datetime(value: Any, *, field_label: str) -> str:
    if isinstance(value, datetime):
        dt = value
    else:
        raw = str(value or "").strip()
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(raw)
        except Exception as exc:
            raise frappe.ValidationError(f"{field_label} is not a valid datetime.") from exc

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=PH_TZ)
    else:
        dt = dt.astimezone(PH_TZ)
    return dt.replace(tzinfo=None).strftime("%Y-%m-%d %H:%M:%S")


def _reservation_key(
    event_uuid: str,
    line_no: int,
    item_code: str,
    warehouse: str,
) -> str:
    raw = f"{event_uuid}|{int(line_no)}|{item_code}|{warehouse}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _expected_projection_rows(
    event_uuid: str,
    business_date: str,
    normalized: Dict[str, Any],
) -> List[Dict[str, Any]]:
    return [
        {
            "reservation_key": _reservation_key(
                event_uuid,
                line["line_no"],
                line["item_code"],
                line["warehouse"],
            ),
            "event_uuid": event_uuid,
            "line_no": line["line_no"],
            "item_code": line["item_code"],
            "warehouse": line["warehouse"],
            "reserved_qty": line["qty"],
            "business_date": business_date,
            "projection_state": "Pending Edge",
        }
        for line in normalized["items"]
    ]


def _verify_or_insert_projections(
    event_uuid: str,
    business_date: str,
    normalized: Dict[str, Any],
) -> int:
    expected = _expected_projection_rows(event_uuid, business_date, normalized)
    existing = frappe.get_all(
        "NKT Edge Order Reservation Projection",
        filters={"event_uuid": event_uuid},
        fields=[
            "reservation_key",
            "event_uuid",
            "line_no",
            "item_code",
            "warehouse",
            "reserved_qty",
            "business_date",
            "projection_state",
        ],
        order_by="line_no asc",
        limit_page_length=500,
    )

    if existing:
        if len(existing) != len(expected):
            raise NKTIdempotencyConflict(
                "Offline order reservation projection count conflicts with immutable intent."
            )
        for got, want in zip(existing, expected):
            if (
                got.reservation_key != want["reservation_key"]
                or int(got.line_no or 0) != int(want["line_no"])
                or str(got.item_code or "") != want["item_code"]
                or str(got.warehouse or "") != want["warehouse"]
                or abs(flt(got.reserved_qty) - flt(want["reserved_qty"])) > 0.000001
                or str(got.business_date) != str(want["business_date"])
                or got.projection_state not in ("Pending Edge", "Awaiting Primary")
            ):
                raise NKTIdempotencyConflict(
                    "Offline order reservation projection conflicts with immutable intent."
                )
        return len(existing)

    for row in expected:
        frappe.get_doc({
            "doctype": "NKT Edge Order Reservation Projection",
            **row,
        }).insert(ignore_permissions=True)
    return len(expected)


def pending_reservation_qty(item_code: str, warehouse: str) -> float:
    item_code = _nonempty_text(item_code, "Item Code")
    warehouse = _nonempty_text(warehouse, "Warehouse")
    value = frappe.db.sql(
        """
        SELECT COALESCE(SUM(reserved_qty), 0)
        FROM `tabNKT Edge Order Reservation Projection`
        WHERE item_code = %s
          AND warehouse = %s
          AND projection_state IN ('Pending Edge', 'Awaiting Primary')
        """,
        (item_code, warehouse),
    )[0][0]
    return flt(value)


def effective_available_qty(base_qty: Any, item_code: str, warehouse: str) -> float:
    return flt(base_qty) - pending_reservation_qty(item_code, warehouse)


def accept_customer_order_intent_at_edge(
    event_uuid: str,
    device_id: str,
    business_date: Any,
    settled_at: Any,
    payload: Dict[str, Any],
    *,
    user: Optional[str] = None,
) -> Dict[str, Any]:
    """
    First real business offline family.

    Edge authority is intentionally limited to:
      1) immutable Customer Order Intent event/payload;
      2) technical local pending-quantity reservation projection.

    It does NOT create/submit NKT Customer Order or any money/AR/stock truth.
    Caller/request transaction owns commit/rollback.
    """
    if _runtime_role() != "Store Edge":
        raise frappe.PermissionError("Offline order intent unavailable.")

    user = _session_user(user)
    _require_encoder_authority(user)

    device = device_policy_snapshot(
        device_id,
        user=user,
        requested_context="NKT Retail",
    )
    if device.get("ui_mode") != "normal":
        raise frappe.PermissionError("Offline order intent unavailable.")

    family_policy = event_family_policy(ORDER_INTENT_FAMILY)
    if family_policy.get("offline_write_allowed") is not True:
        raise frappe.PermissionError("Offline order intent unavailable.")

    business = validate_business_time(business_date, settled_at)
    normalized = _normalize_order_intent_payload(payload)
    digest = canonical_payload_hash(normalized)

    envelope = {
        "event_uuid": event_uuid,
        "event_family": ORDER_INTENT_FAMILY,
        "event_action": ORDER_INTENT_ACTION,
        "operational_context": device.get("operational_context") or "NKT Retail",
        "origin_device": device_id,
        "origin_user": user,
        "business_date": business["business_date"],
        "settled_at": _manila_sql_datetime(
            business["settled_at_manila"],
            field_label="Business / Settled Time",
        ),
        "client_created_at": _manila_sql_datetime(
            normalized["client_observed_at"],
            field_label="Client observed time",
        ),
        "payload_sha256": digest,
    }

    event, replay = begin_event(envelope)

    pending_name = frappe.db.exists("NKT Sync Pending Payload", event.event_uuid)
    canonical_json = _canonical_order_intent_json(normalized)

    if pending_name:
        pending = frappe.get_doc("NKT Sync Pending Payload", pending_name)
        if pending.event_family != ORDER_INTENT_FAMILY:
            raise NKTIdempotencyConflict(
                "Pending payload family conflicts with immutable Event UUID."
            )
        if pending.payload_sha256 != digest:
            raise NKTIdempotencyConflict(
                "Pending payload hash conflicts with immutable Event UUID."
            )
        if pending.payload_json != canonical_json:
            raise NKTIdempotencyConflict(
                "Pending payload content conflicts with immutable Event UUID."
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
    else:
        if replay and event.sync_state not in (
            "Committed at Primary",
            "Conflict",
            "Failed",
        ):
            raise frappe.ValidationError(
                "Customer Order Intent exists but durable pending payload is unavailable."
            )

        if event.sync_state not in ("Committed at Primary", "Conflict", "Failed"):
            frappe.get_doc({
                "doctype": "NKT Sync Pending Payload",
                "event_uuid": event.event_uuid,
                "event_family": ORDER_INTENT_FAMILY,
                "payload_sha256": digest,
                "payload_json": canonical_json,
                "queue_state": "Accepted at Edge",
                "edge_accepted_at": now(),
                "attempt_count": 1,
                "last_attempt_at": now(),
            }).insert(ignore_permissions=True)

    projection_count = _verify_or_insert_projections(
        event.event_uuid,
        business["business_date"],
        normalized,
    )

    if event.sync_state not in ("Committed at Primary", "Conflict", "Failed"):
        mark_edge_accepted(event.event_uuid)
        event.reload()

    return {
        "event_uuid": event.event_uuid,
        "event_family": ORDER_INTENT_FAMILY,
        "sync_state": event.sync_state,
        "durable_ack": True,
        "replay": bool(replay),
        "payload_sha256": digest,
        "projection_rows": projection_count,
        "local_reserved_qty_total": sum(
            flt(line["qty"]) for line in normalized["items"]
        ),
        "canonical_customer_order_created": False,
        "warehouse_release_created": False,
        "stock_entry_created": False,
        "receivable_created": False,
        "payment_receipt_created": False,
        "cashier_movement_created": False,
        "matching_executed": False,
        "primary_materialization_required": True,
    }


@frappe.whitelist()
def submit_customer_order_intent(
    event_uuid: str,
    device_id: str,
    business_date: str,
    settled_at: str,
    payload: Any,
):
    if isinstance(payload, str):
        payload = frappe.parse_json(payload)
    return accept_customer_order_intent_at_edge(
        event_uuid,
        device_id,
        business_date,
        settled_at,
        payload,
        user=_session_user(),
    )


@frappe.whitelist()
def get_order_intent_reservation_summary(
    device_id: str,
    item_code: str,
    warehouse: str,
    base_qty: Any = None,
):
    user = _session_user()
    _require_encoder_authority(user)
    device = device_policy_snapshot(
        device_id,
        user=user,
        requested_context="NKT Retail",
    )
    if device.get("ui_mode") != "normal":
        raise frappe.PermissionError("Offline order reservation unavailable.")

    pending = pending_reservation_qty(item_code, warehouse)
    out = {
        "item_code": str(item_code),
        "warehouse": str(warehouse),
        "pending_order_intent_qty": pending,
    }
    if base_qty not in (None, ""):
        out["base_qty"] = flt(base_qty)
        out["effective_available_qty"] = flt(base_qty) - pending
    return out
