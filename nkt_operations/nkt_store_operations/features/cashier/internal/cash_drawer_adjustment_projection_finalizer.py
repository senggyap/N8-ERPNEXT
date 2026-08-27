from __future__ import annotations

import uuid
from typing import Any, Dict

import frappe
from frappe.utils import flt, now

from nkt_operations.nkt_store_operations.features.offline_edge.safe_sync import (
    NKTIdempotencyConflict,
)
from nkt_operations.nkt_store_operations.features.offline_edge.sync_transport import _runtime_role
from nkt_operations.nkt_store_operations.features.cashier.internal.cash_drawer_adjustment_materializer import (
    _materialization_ack_uuid,
)

PROJECTION = "NKT Edge Cash Drawer Adjustment Projection"
TOLERANCE = 0.000001


def _require_edge():
    if _runtime_role() != "Store Edge":
        raise frappe.PermissionError(
            "Cash Drawer materialization ACK is available only at Store Edge."
        )


def _uuid(value: Any, label: str) -> str:
    try:
        return str(uuid.UUID(str(value)))
    except Exception as exc:
        raise frappe.ValidationError(f"{label} is invalid.") from exc


def _projection(event_uuid: str):
    if not frappe.db.exists(PROJECTION, event_uuid):
        raise frappe.DoesNotExistError("Edge cash-drawer projection is unavailable.")
    return frappe.get_doc(PROJECTION, event_uuid)


def _verify_ack(ack: Dict[str, Any], projection):
    if not isinstance(ack, dict):
        raise frappe.ValidationError("Cash Drawer materialization ACK is invalid.")

    event_uuid = _uuid(ack.get("event_uuid"), "Event UUID")
    if event_uuid != projection.name:
        raise NKTIdempotencyConflict(
            "Cash Drawer materialization ACK targets another Edge projection."
        )
    if ack.get("event_family") != "NKT Cash Drawer Adjustment Intent":
        raise NKTIdempotencyConflict(
            "Cash Drawer materialization ACK family is invalid."
        )

    if not frappe.db.exists("NKT Sync Event", event_uuid):
        raise frappe.DoesNotExistError("Cash Drawer Sync Event is unavailable.")
    event = frappe.get_doc("NKT Sync Event", event_uuid)

    payload_hash = str(ack.get("payload_sha256") or "").lower()
    if payload_hash != str(event.payload_sha256 or "").lower():
        raise NKTIdempotencyConflict(
            "Cash Drawer materialization ACK payload hash conflicts with Event."
        )
    if str(ack.get("primary_ack_uuid") or "") != str(event.primary_ack_uuid or ""):
        raise NKTIdempotencyConflict(
            "Cash Drawer materialization ACK Primary binding conflicts with Event."
        )
    if str(ack.get("cashier_shift") or "") != str(projection.cashier_shift or ""):
        raise NKTIdempotencyConflict("Materialization ACK shift conflicts with projection.")
    if str(ack.get("adjustment_type") or "") != str(projection.adjustment_type or ""):
        raise NKTIdempotencyConflict("Materialization ACK type conflicts with projection.")
    if str(ack.get("direction") or "") != str(projection.direction or ""):
        raise NKTIdempotencyConflict("Materialization ACK direction conflicts with projection.")
    if abs(flt(ack.get("amount")) - flt(projection.amount)) > TOLERANCE:
        raise NKTIdempotencyConflict("Materialization ACK amount conflicts with projection.")

    expected = _materialization_ack_uuid(
        event_uuid,
        payload_hash,
        ack.get("cash_drawer_adjustment"),
        ack.get("cashier_movement"),
    )
    got = _uuid(ack.get("materialization_ack_uuid"), "Materialization ACK UUID")
    if got != expected:
        raise NKTIdempotencyConflict(
            "Cash Drawer materialization ACK UUID is not deterministic for its canonical binding."
        )
    return event_uuid, got


def apply_materialization_ack_at_edge(ack: Dict[str, Any]) -> Dict[str, Any]:
    _require_edge()
    event_uuid = _uuid(ack.get("event_uuid"), "Event UUID")
    projection = _projection(event_uuid)
    event_uuid, ack_uuid = _verify_ack(ack, projection)

    if projection.projection_state in ("Primary Cash Materialized", "Finalized"):
        if (
            str(projection.materialization_ack_uuid or "") != ack_uuid
            or str(projection.materialized_adjustment or "")
            != str(ack.get("cash_drawer_adjustment") or "")
            or str(projection.materialized_movement or "")
            != str(ack.get("cashier_movement") or "")
        ):
            raise NKTIdempotencyConflict(
                "Existing Edge cash materialization binding conflicts with supplied ACK."
            )
        return {
            "event_uuid": event_uuid,
            "materialization_ack_uuid": ack_uuid,
            "projection_state": projection.projection_state,
            "projection_still_affects_edge_drawer": (
                projection.projection_state != "Finalized"
            ),
            "local_cash_rebase_required_before_finalization": (
                projection.projection_state != "Finalized"
            ),
            "replay": True,
        }

    if projection.projection_state != "Primary Preserved":
        raise frappe.ValidationError(
            "Edge cash projection is not eligible for materialization ACK."
        )

    frappe.db.set_value(
        PROJECTION,
        projection.name,
        {
            "projection_state": "Primary Cash Materialized",
            "materialization_ack_uuid": ack_uuid,
            "materialized_adjustment": ack["cash_drawer_adjustment"],
            "materialized_movement": ack["cashier_movement"],
            "primary_materialized_at": now(),
        },
        update_modified=False,
    )
    return {
        "event_uuid": event_uuid,
        "materialization_ack_uuid": ack_uuid,
        "projection_state": "Primary Cash Materialized",
        "projection_still_affects_edge_drawer": True,
        "local_cash_rebase_required_before_finalization": True,
        "replay": False,
    }


def finalize_projection_after_local_cash_rebase(
    event_uuid: str,
    materialization_ack_uuid: str,
) -> Dict[str, Any]:
    _require_edge()
    event_uuid = _uuid(event_uuid, "Event UUID")
    ack_uuid = _uuid(materialization_ack_uuid, "Materialization ACK UUID")
    projection = _projection(event_uuid)

    if projection.projection_state == "Finalized":
        if str(projection.materialization_ack_uuid or "") != ack_uuid:
            raise NKTIdempotencyConflict(
                "Finalized Edge cash projection has another ACK binding."
            )
        return {
            "event_uuid": event_uuid,
            "materialization_ack_uuid": ack_uuid,
            "projection_state": "Finalized",
            "projection_still_affects_edge_drawer": False,
            "replay": True,
        }

    if projection.projection_state != "Primary Cash Materialized":
        raise frappe.ValidationError(
            "Edge cash projection cannot finalize before Primary materialization ACK."
        )
    if str(projection.materialization_ack_uuid or "") != ack_uuid:
        raise NKTIdempotencyConflict(
            "Edge cash projection materialization ACK binding is inconsistent."
        )

    adjustment_name = str(projection.materialized_adjustment or "")
    movement_name = str(projection.materialized_movement or "")
    if (
        not adjustment_name
        or not movement_name
        or not frappe.db.exists("NKT Cash Drawer Adjustment", adjustment_name)
        or not frappe.db.exists("NKT Cashier Movement", movement_name)
    ):
        return {
            "event_uuid": event_uuid,
            "materialization_ack_uuid": ack_uuid,
            "projection_state": "Primary Cash Materialized",
            "projection_still_affects_edge_drawer": True,
            "local_cash_rebase_required_before_finalization": True,
            "replay": False,
        }

    adjustment = frappe.get_doc("NKT Cash Drawer Adjustment", adjustment_name)
    movement = frappe.get_doc("NKT Cashier Movement", movement_name)
    if (
        int(adjustment.docstatus or 0) != 1
        or str(adjustment.status or "") != "Posted"
        or str(adjustment.cashier_movement or "") != movement.name
        or str(adjustment.cashier_shift or "") != str(projection.cashier_shift or "")
        or str(adjustment.adjustment_type or "") != str(projection.adjustment_type or "")
        or str(adjustment.direction or "") != str(projection.direction or "")
        or abs(flt(adjustment.amount) - flt(projection.amount)) > TOLERANCE
    ):
        raise NKTIdempotencyConflict(
            "Replicated Cash Drawer Adjustment conflicts with Edge projection."
        )
    if (
        int(movement.docstatus or 0) != 1
        or str(movement.status or "") != "Posted"
        or str(movement.cashier_shift or "") != str(projection.cashier_shift or "")
        or str(movement.movement_type or "") != str(projection.adjustment_type or "")
        or str(movement.direction or "") != str(projection.direction or "")
        or str(movement.payment_method or "") != "Cash"
        or int(movement.affects_cash_drawer or 0) != 1
        or abs(flt(movement.amount) - flt(projection.amount)) > TOLERANCE
        or str(movement.source_doctype or "") != "NKT Cash Drawer Adjustment"
        or str(movement.source_name or "") != adjustment.name
        or bool(str(movement.source_row or "").strip())
    ):
        raise NKTIdempotencyConflict(
            "Replicated Cashier Movement conflicts with Edge cash projection."
        )

    frappe.db.set_value(
        PROJECTION,
        projection.name,
        {
            "projection_state": "Finalized",
            "finalized_at": now(),
        },
        update_modified=False,
    )
    return {
        "event_uuid": event_uuid,
        "materialization_ack_uuid": ack_uuid,
        "projection_state": "Finalized",
        "projection_still_affects_edge_drawer": False,
        "local_cash_rebase_required_before_finalization": False,
        "replay": False,
    }


@frappe.whitelist()
def apply_materialization_ack(ack):
    if isinstance(ack, str):
        ack = frappe.parse_json(ack)
    return apply_materialization_ack_at_edge(ack)


@frappe.whitelist()
def finalize_after_cash_rebase(event_uuid: str, materialization_ack_uuid: str):
    return finalize_projection_after_local_cash_rebase(
        event_uuid,
        materialization_ack_uuid,
    )
