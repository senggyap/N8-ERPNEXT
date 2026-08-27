from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any, Dict

import frappe
from frappe.utils import flt, now

from nkt_operations.nkt_store_operations.features.offline_edge.safe_sync import NKTIdempotencyConflict
from nkt_operations.nkt_store_operations.features.offline_edge.sync_transport import _runtime_role
from nkt_operations.nkt_store_operations.features.inventory.internal.warehouse_transfer_dispatch_intent import FAMILY

TOLERANCE = 0.000001
PROJECTION = "NKT Edge Warehouse Transfer Projection"


def _require_edge():
    if _runtime_role() != "Store Edge":
        raise frappe.PermissionError(
            "Transfer dispatch projection finalization is available only at Store Edge."
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
            "warehouse_transfer",
            "source_warehouse",
            "destination_warehouse",
            "transit_warehouse",
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
        raise frappe.ValidationError("Transfer dispatch materialization ACK is invalid.")
    event_uuid = _uuid(ack.get("event_uuid"), "Event UUID")
    ack_uuid = _uuid(ack.get("materialization_ack_uuid"), "Materialization ACK UUID")
    payload_hash = str(ack.get("payload_sha256") or "").lower()
    ack_hash = str(ack.get("materialization_ack_sha256") or "").lower()
    if len(payload_hash) != 64 or any(c not in "0123456789abcdef" for c in payload_hash):
        raise frappe.ValidationError("Transfer dispatch materialization payload hash is invalid.")
    if len(ack_hash) != 64 or any(c not in "0123456789abcdef" for c in ack_hash):
        raise frappe.ValidationError("Transfer dispatch materialization ACK hash is invalid.")
    canonical = _canonical_ack_json(ack)
    if hashlib.sha256(canonical.encode("utf-8")).hexdigest() != ack_hash:
        raise NKTIdempotencyConflict(
            "Transfer dispatch materialization ACK conflicts with its durable hash."
        )
    effects = ack.get("stock_effects")
    if not isinstance(effects, list) or not effects:
        raise frappe.ValidationError("Transfer dispatch materialization ACK has no stock effects.")
    return event_uuid, ack_uuid, payload_hash, ack_hash, effects


def _rows(event_uuid: str):
    return frappe.get_all(
        PROJECTION,
        filters={"event_uuid": event_uuid, "projection_action": "Source Dispatch"},
        fields=[
            "name","event_uuid","line_no","warehouse_transfer","warehouse_transfer_item",
            "item_code","source_warehouse","destination_warehouse","transit_warehouse",
            "dispatched_qty","projection_state","primary_ack_uuid","primary_stock_entry",
            "materialization_ack_uuid","materialization_ack_sha256",
            "primary_post_source_actual_qty","primary_post_transit_actual_qty",
            "primary_materialized_at","finalized_at",
        ],
        order_by="line_no asc",
        limit_page_length=500,
    )


def apply_materialization_ack_at_edge(ack: Dict[str, Any]) -> Dict[str, Any]:
    _require_edge()
    event_uuid, ack_uuid, payload_hash, ack_hash, effects = _validate_ack(ack)

    if not frappe.db.exists("NKT Sync Event", event_uuid):
        raise frappe.DoesNotExistError("Transfer Dispatch event is unavailable at Edge.")
    event = frappe.get_doc("NKT Sync Event", event_uuid)
    if (
        event.event_family != FAMILY
        or event.sync_state != "Committed at Primary"
        or str(event.payload_sha256 or "").lower() != payload_hash
    ):
        raise NKTIdempotencyConflict(
            "Transfer dispatch materialization ACK does not match the committed Edge event."
        )

    rows = _rows(event_uuid)
    if not rows:
        raise frappe.ValidationError("Transfer dispatch materialization ACK has no Edge projection.")

    effect_map = {}
    for effect in effects:
        if not isinstance(effect, dict):
            raise frappe.ValidationError("Transfer dispatch materialization stock effect is invalid.")
        item = str(effect.get("item_code") or "")
        source = str(effect.get("source_warehouse") or "")
        transit = str(effect.get("transit_warehouse") or "")
        key = (item, source, transit)
        if not all(key) or key in effect_map:
            raise NKTIdempotencyConflict("Transfer dispatch materialization stock-effect identity is invalid.")
        dispatched = flt(effect.get("dispatched_qty"))
        source_post = flt(effect.get("primary_post_source_actual_qty"))
        transit_post = flt(effect.get("primary_post_transit_actual_qty"))
        if dispatched <= 0:
            raise NKTIdempotencyConflict("Transfer dispatch materialization quantity is invalid.")
        effect_map[key] = {
            "dispatched_qty": dispatched,
            "source_post": source_post,
            "transit_post": transit_post,
        }

    projected_keys = {
        (str(r.item_code or ""), str(r.source_warehouse or ""), str(r.transit_warehouse or ""))
        for r in rows
    }
    if projected_keys != set(effect_map):
        raise NKTIdempotencyConflict(
            "Transfer dispatch materialization ACK does not cover the Edge projection exactly."
        )

    replay = all(r.projection_state in ("Primary Dispatch Materialized", "Finalized") for r in rows)
    for row in rows:
        key = (row.item_code, row.source_warehouse, row.transit_warehouse)
        effect = effect_map[key]
        if abs(flt(row.dispatched_qty) - effect["dispatched_qty"]) > TOLERANCE:
            raise NKTIdempotencyConflict(
                "Transfer dispatch materialization quantity conflicts with Edge projection."
            )
        if row.projection_state not in ("Primary Preserved", "Primary Dispatch Materialized", "Finalized"):
            raise NKTIdempotencyConflict(
                "Edge transfer-dispatch projection is not eligible for materialization ACK."
            )
        if row.projection_state in ("Primary Dispatch Materialized", "Finalized"):
            if (
                str(row.materialization_ack_uuid or "") != ack_uuid
                or str(row.materialization_ack_sha256 or "") != ack_hash
                or str(row.primary_stock_entry or "") != str(ack.get("stock_entry") or "")
            ):
                raise NKTIdempotencyConflict(
                    "Existing Edge transfer materialization binding conflicts with supplied ACK."
                )
            continue

        frappe.db.set_value(
            PROJECTION,
            row.name,
            {
                "projection_state": "Primary Dispatch Materialized",
                "materialization_ack_uuid": ack_uuid,
                "materialization_ack_sha256": ack_hash,
                "primary_stock_entry": ack["stock_entry"],
                "primary_post_source_actual_qty": effect["source_post"],
                "primary_post_transit_actual_qty": effect["transit_post"],
                "primary_materialized_at": now(),
            },
            update_modified=False,
        )

    return {
        "event_uuid": event_uuid,
        "materialization_ack_uuid": ack_uuid,
        "stock_entry": ack["stock_entry"],
        "projection_state": "Primary Dispatch Materialized",
        "projection_still_applies_until_local_stock_rebase": True,
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
    rows = _rows(event_uuid)
    if not rows:
        raise frappe.DoesNotExistError("Edge transfer-dispatch projection is unavailable.")

    if all(r.projection_state == "Finalized" for r in rows):
        if any(str(r.materialization_ack_uuid or "") != ack_uuid for r in rows):
            raise NKTIdempotencyConflict("Finalized transfer projection has another ACK binding.")
        return {
            "event_uuid": event_uuid,
            "materialization_ack_uuid": ack_uuid,
            "projection_state": "Finalized",
            "projection_still_applies_until_local_stock_rebase": False,
            "replay": True,
        }

    if any(r.projection_state != "Primary Dispatch Materialized" for r in rows):
        raise frappe.ValidationError(
            "Transfer dispatch projection cannot finalize before Primary materialization ACK."
        )
    if any(str(r.materialization_ack_uuid or "") != ack_uuid for r in rows):
        raise NKTIdempotencyConflict("Transfer dispatch projection ACK binding is inconsistent.")

    stock_entries = {str(r.primary_stock_entry or "") for r in rows}
    if len(stock_entries) != 1 or not next(iter(stock_entries)):
        raise NKTIdempotencyConflict("Transfer dispatch projection has inconsistent Stock Entry binding.")
    stock_entry = next(iter(stock_entries))
    if not frappe.db.exists("Stock Entry", stock_entry):
        raise frappe.ValidationError(
            "Authoritative Primary outgoing Stock Entry is not yet present on this Store Edge."
        )
    entry = frappe.get_doc("Stock Entry", stock_entry)
    if (
        int(entry.docstatus or 0) != 1
        or str(entry.purpose or "") != "Material Transfer"
        or int(entry.add_to_transit or 0) != 1
    ):
        raise NKTIdempotencyConflict(
            "Replicated Primary Stock Entry is not a submitted Add-to-Transit Material Transfer."
        )

    transfers = {str(r.warehouse_transfer or "") for r in rows}
    if len(transfers) != 1 or not next(iter(transfers)):
        raise NKTIdempotencyConflict("Transfer dispatch projection has inconsistent transfer identity.")
    transfer_name = next(iter(transfers))
    if not frappe.db.exists("NKT Warehouse Transfer", transfer_name):
        raise frappe.ValidationError("Canonical Internal Warehouse Transfer is not yet present on this Store Edge.")
    transfer = frappe.get_doc("NKT Warehouse Transfer", transfer_name)
    if str(transfer.status or "") != "In Transit" or str(transfer.outgoing_stock_entry or "") != stock_entry:
        raise NKTIdempotencyConflict(
            "Replicated transfer does not bind the authoritative outgoing Stock Entry."
        )

    mismatches = []
    for row in rows:
        source_actual = flt(
            frappe.db.get_value(
                "Bin",
                {"item_code": row.item_code, "warehouse": row.source_warehouse},
                "actual_qty",
            )
        )
        transit_actual = flt(
            frappe.db.get_value(
                "Bin",
                {"item_code": row.item_code, "warehouse": row.transit_warehouse},
                "actual_qty",
            )
        )
        if (
            abs(source_actual - flt(row.primary_post_source_actual_qty)) > TOLERANCE
            or abs(transit_actual - flt(row.primary_post_transit_actual_qty)) > TOLERANCE
        ):
            mismatches.append(
                {
                    "item_code": row.item_code,
                    "source_warehouse": row.source_warehouse,
                    "transit_warehouse": row.transit_warehouse,
                    "local_source_actual_qty": source_actual,
                    "required_primary_post_source_actual_qty": flt(row.primary_post_source_actual_qty),
                    "local_transit_actual_qty": transit_actual,
                    "required_primary_post_transit_actual_qty": flt(row.primary_post_transit_actual_qty),
                }
            )
    if mismatches:
        return {
            "event_uuid": event_uuid,
            "materialization_ack_uuid": ack_uuid,
            "projection_state": "Primary Dispatch Materialized",
            "projection_still_applies_until_local_stock_rebase": True,
            "local_stock_rebase_required_before_finalization": True,
            "rebase_mismatches": mismatches,
            "replay": False,
        }

    for row in rows:
        frappe.db.set_value(
            PROJECTION,
            row.name,
            {"projection_state": "Finalized", "finalized_at": now()},
            update_modified=False,
        )

    return {
        "event_uuid": event_uuid,
        "materialization_ack_uuid": ack_uuid,
        "stock_entry": stock_entry,
        "projection_state": "Finalized",
        "projection_still_applies_until_local_stock_rebase": False,
        "local_stock_rebase_required_before_finalization": False,
        "replay": False,
    }


def installation_probe():
    fields = {f.fieldname for f in frappe.get_meta(PROJECTION).fields}
    required = {
        "materialization_ack_uuid",
        "materialization_ack_sha256",
        "primary_post_source_actual_qty",
        "primary_post_transit_actual_qty",
        "primary_materialized_at",
        "finalized_at",
    }
    return {
        "projection_fields_ready": required.issubset(fields),
        "primary_dispatch_materialized_state_present": "Primary Dispatch Materialized"
        in (frappe.get_meta(PROJECTION).get_field("projection_state").options or ""),
        "requires_local_source_and_transit_stock_rebase": True,
        "destination_arrival_enabled": False,
    }


@frappe.whitelist()
def apply_materialization_ack(ack):
    if isinstance(ack, str):
        ack = frappe.parse_json(ack)
    return apply_materialization_ack_at_edge(ack)


@frappe.whitelist()
def finalize_after_stock_rebase(event_uuid: str, materialization_ack_uuid: str):
    return finalize_projection_after_local_stock_rebase(event_uuid, materialization_ack_uuid)
