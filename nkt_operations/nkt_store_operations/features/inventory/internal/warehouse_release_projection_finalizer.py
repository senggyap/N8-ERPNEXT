from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any, Dict

import frappe
from frappe.utils import flt, now

from nkt_operations.nkt_store_operations.features.offline_edge.safe_sync import (
    NKTIdempotencyConflict,
)
from nkt_operations.nkt_store_operations.features.offline_edge.sync_transport import _runtime_role
from nkt_operations.nkt_store_operations.features.inventory.internal.warehouse_release_intent import (
    WAREHOUSE_RELEASE_INTENT_FAMILY,
)
from nkt_operations.nkt_store_operations.features.oil.controls import (
    is_configured_finished_oil_item as _nkt_c15d_is_finished_oil_item,
)

TOLERANCE = 0.000001
PROJECTION = "NKT Edge Warehouse Release Projection"


def _require_edge():
    if _runtime_role() != "Store Edge":
        raise frappe.PermissionError(
            "Warehouse release stock-rebase finalization is available only at Store Edge."
        )


def _uuid(value: Any, label: str) -> str:
    try:
        return str(uuid.UUID(str(value)))
    except Exception as exc:
        raise frappe.ValidationError(f"{label} is invalid.") from exc


def _canonical_ack_json(ack: Dict[str, Any]) -> str:
    value = {
        key: ack[key]
        for key in (
            "event_uuid",
            "payload_sha256",
            "materialization_ack_uuid",
            "warehouse_release",
            "customer_order",
            "source_warehouse",
            "stock_entry",
            "stock_effects",
        )
    }
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    )


def _validate_ack(ack: Dict[str, Any]):
    if not isinstance(ack, dict):
        raise frappe.ValidationError("Physical-stock materialization ACK is invalid.")
    event_uuid = _uuid(ack.get("event_uuid"), "Event UUID")
    ack_uuid = _uuid(ack.get("materialization_ack_uuid"), "Materialization ACK UUID")
    payload_hash = str(ack.get("payload_sha256") or "").lower()
    ack_hash = str(ack.get("materialization_ack_sha256") or "").lower()
    if len(payload_hash) != 64 or any(c not in "0123456789abcdef" for c in payload_hash):
        raise frappe.ValidationError("Materialization ACK payload hash is invalid.")
    if len(ack_hash) != 64 or any(c not in "0123456789abcdef" for c in ack_hash):
        raise frappe.ValidationError("Materialization ACK hash is invalid.")
    canonical = _canonical_ack_json(ack)
    if hashlib.sha256(canonical.encode("utf-8")).hexdigest() != ack_hash:
        raise NKTIdempotencyConflict(
            "Physical-stock materialization ACK content conflicts with its durable hash."
        )
    effects = ack.get("stock_effects")
    if not isinstance(effects, list) or not effects:
        raise frappe.ValidationError("Materialization ACK has no stock effects.")
    return event_uuid, ack_uuid, payload_hash, ack_hash, effects


def _projection_rows(event_uuid: str):
    return frappe.get_all(
        PROJECTION,
        filters={"event_uuid": event_uuid},
        fields=[
            "name","event_uuid","warehouse_release","customer_order","item_code",
            "warehouse","released_qty","projection_state","primary_ack_uuid",
            "materialization_ack_uuid","materialization_ack_sha256",
            "primary_stock_entry","primary_post_actual_qty",
            "primary_materialized_at","finalized_at",
        ],
        order_by="line_no asc",
        limit_page_length=500,
    )


def apply_materialization_ack_at_edge(ack: Dict[str, Any]) -> Dict[str, Any]:
    _require_edge()
    event_uuid, ack_uuid, payload_hash, ack_hash, effects = _validate_ack(ack)

    if not frappe.db.exists("NKT Sync Event", event_uuid):
        raise frappe.DoesNotExistError("Warehouse Release Intent event is unavailable at Edge.")
    event = frappe.get_doc("NKT Sync Event", event_uuid)
    if (
        event.event_family != WAREHOUSE_RELEASE_INTENT_FAMILY
        or event.sync_state != "Committed at Primary"
        or str(event.payload_sha256 or "").lower() != payload_hash
    ):
        raise NKTIdempotencyConflict(
            "Materialization ACK does not match the committed Edge release event."
        )

    rows = _projection_rows(event_uuid)
    if not rows:
        raise frappe.ValidationError("Materialization ACK has no Edge physical-release projection.")

    effect_map = {}
    for effect in effects:
        if not isinstance(effect, dict):
            raise frappe.ValidationError("Materialization ACK stock effect is invalid.")
        key = (str(effect.get("item_code") or ""), str(effect.get("warehouse") or ""))
        if not all(key) or key in effect_map:
            raise NKTIdempotencyConflict("Materialization ACK stock-effect identity is invalid.")
        released = flt(effect.get("released_qty"))
        post_actual = flt(effect.get("primary_post_actual_qty"))
        if released <= 0 or (
            post_actual < 0
            and not _nkt_c15d_is_finished_oil_item(key[0])
        ):
            raise NKTIdempotencyConflict("Materialization ACK stock quantities are invalid.")
        effect_map[key] = {
            "released_qty": released,
            "primary_post_actual_qty": post_actual,
        }

    projected = {}
    for row in rows:
        key = (str(row.item_code or ""), str(row.warehouse or ""))
        projected[key] = flt(projected.get(key)) + flt(row.released_qty)
    if set(projected) != set(effect_map):
        raise NKTIdempotencyConflict(
            "Materialization ACK stock effects do not cover the Edge projection exactly."
        )
    for key, qty in projected.items():
        if abs(qty - effect_map[key]["released_qty"]) > TOLERANCE:
            raise NKTIdempotencyConflict(
                "Materialization ACK released quantity conflicts with Edge projection."
            )

    replay = all(row.projection_state in ("Primary Stock Materialized", "Finalized") for row in rows)
    for row in rows:
        if row.projection_state not in ("Primary Preserved", "Primary Stock Materialized", "Finalized"):
            raise NKTIdempotencyConflict(
                "Edge physical-release projection is not eligible for materialization ACK."
            )
        if row.projection_state in ("Primary Stock Materialized", "Finalized"):
            if (
                str(row.materialization_ack_uuid or "") != ack_uuid
                or str(row.materialization_ack_sha256 or "") != ack_hash
                or str(row.primary_stock_entry or "") != str(ack.get("stock_entry") or "")
            ):
                raise NKTIdempotencyConflict(
                    "Existing Edge materialization binding conflicts with supplied ACK."
                )
            continue

        effect = effect_map[(row.item_code, row.warehouse)]
        frappe.db.set_value(
            PROJECTION,
            row.name,
            {
                "projection_state": "Primary Stock Materialized",
                "materialization_ack_uuid": ack_uuid,
                "materialization_ack_sha256": ack_hash,
                "primary_stock_entry": ack["stock_entry"],
                "primary_post_actual_qty": effect["primary_post_actual_qty"],
                "primary_materialized_at": now(),
            },
            update_modified=False,
        )

    return {
        "event_uuid": event_uuid,
        "materialization_ack_uuid": ack_uuid,
        "stock_entry": ack["stock_entry"],
        "projection_state": "Primary Stock Materialized",
        "projection_still_deducts_edge_availability": True,
        "local_stock_rebase_required_before_finalization": True,
        "replay": replay,
    }


def finalize_projection_after_local_stock_rebase(
    event_uuid: str,
    materialization_ack_uuid: str,
) -> Dict[str, Any]:
    _require_edge()
    event_uuid = _uuid(event_uuid, "Event UUID")
    ack_uuid = _uuid(materialization_ack_uuid, "Materialization ACK UUID")
    rows = _projection_rows(event_uuid)
    if not rows:
        raise frappe.DoesNotExistError("Edge physical-release projection is unavailable.")

    if all(row.projection_state == "Finalized" for row in rows):
        if any(str(row.materialization_ack_uuid or "") != ack_uuid for row in rows):
            raise NKTIdempotencyConflict("Finalized Edge projection has another ACK binding.")
        return {
            "event_uuid": event_uuid,
            "materialization_ack_uuid": ack_uuid,
            "projection_state": "Finalized",
            "projection_still_deducts_edge_availability": False,
            "replay": True,
        }

    if any(row.projection_state != "Primary Stock Materialized" for row in rows):
        raise frappe.ValidationError(
            "Edge projection cannot finalize before Primary stock materialization ACK."
        )
    if any(str(row.materialization_ack_uuid or "") != ack_uuid for row in rows):
        raise NKTIdempotencyConflict("Edge projection materialization ACK binding is inconsistent.")

    stock_entries = {str(row.primary_stock_entry or "") for row in rows}
    if len(stock_entries) != 1 or not next(iter(stock_entries)):
        raise NKTIdempotencyConflict("Edge projection has inconsistent Primary Stock Entry binding.")
    stock_entry_name = next(iter(stock_entries))
    if not frappe.db.exists("Stock Entry", stock_entry_name):
        raise frappe.ValidationError(
            "Authoritative Primary Stock Entry is not yet present on this Store Edge."
        )
    entry = frappe.get_doc("Stock Entry", stock_entry_name)
    if int(entry.docstatus or 0) != 1 or str(entry.purpose or "") != "Material Issue":
        raise NKTIdempotencyConflict(
            "Replicated Primary Stock Entry is not a submitted Material Issue."
        )

    grouped = {}
    for row in rows:
        key = (str(row.item_code), str(row.warehouse))
        expected = flt(row.primary_post_actual_qty)
        if key in grouped and abs(grouped[key] - expected) > TOLERANCE:
            raise NKTIdempotencyConflict(
                "Edge projection has inconsistent authoritative post-stock quantity."
            )
        grouped[key] = expected

    mismatches = []
    for (item_code, warehouse), expected in grouped.items():
        local_actual = flt(
            frappe.db.get_value(
                "Bin",
                {"item_code": item_code, "warehouse": warehouse},
                "actual_qty",
            )
        )
        if abs(local_actual - expected) > TOLERANCE:
            mismatches.append(
                {
                    "item_code": item_code,
                    "warehouse": warehouse,
                    "local_actual_qty": local_actual,
                    "required_primary_post_actual_qty": expected,
                }
            )
    if mismatches:
        return {
            "event_uuid": event_uuid,
            "materialization_ack_uuid": ack_uuid,
            "projection_state": "Primary Stock Materialized",
            "projection_still_deducts_edge_availability": True,
            "local_stock_rebase_required_before_finalization": True,
            "rebase_mismatches": mismatches,
            "replay": False,
        }

    for row in rows:
        frappe.db.set_value(
            PROJECTION,
            row.name,
            {
                "projection_state": "Finalized",
                "finalized_at": now(),
            },
            update_modified=False,
        )

    return {
        "event_uuid": event_uuid,
        "materialization_ack_uuid": ack_uuid,
        "stock_entry": stock_entry_name,
        "projection_state": "Finalized",
        "projection_still_deducts_edge_availability": False,
        "local_stock_rebase_required_before_finalization": False,
        "replay": False,
    }


@frappe.whitelist()
def apply_materialization_ack(ack):
    if isinstance(ack, str):
        ack = frappe.parse_json(ack)
    return apply_materialization_ack_at_edge(ack)


@frappe.whitelist()
def finalize_after_stock_rebase(event_uuid: str, materialization_ack_uuid: str):
    return finalize_projection_after_local_stock_rebase(
        event_uuid,
        materialization_ack_uuid,
    )
