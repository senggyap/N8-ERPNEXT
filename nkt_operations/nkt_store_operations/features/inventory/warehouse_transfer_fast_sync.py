from __future__ import annotations

import json
import uuid
from datetime import datetime
from typing import Any, Dict, Optional
from zoneinfo import ZoneInfo

import frappe
from frappe import _
from frappe.utils import flt

from nkt_operations.nkt_store_operations.features.offline_edge.policy import (
    device_policy_snapshot,
)
from nkt_operations.nkt_store_operations.features.offline_edge.sync_transport import (
    _runtime_role,
)
from nkt_operations.nkt_store_operations.features.inventory.internal.warehouse_transfer_dispatch_intent import (
    FAMILY as DISPATCH_FAMILY,
    accept_dispatch_intent_at_edge,
)
from nkt_operations.nkt_store_operations.features.inventory.internal.warehouse_transfer_arrival_intent import (
    FAMILY as ARRIVAL_FAMILY,
    _has_unrebased_materialized_arrival,
    _pending_arrival_qty,
    accept_arrival_intent_at_edge,
)
from nkt_operations.nkt_store_operations.doctype.nkt_warehouse_transfer.nkt_warehouse_transfer import (
    _resolve_transit_warehouse,
    receive_transfer,
    release_transfer,
)

FOUNDATION_VERSION = "C15C.10G-R13"
PH_TZ = ZoneInfo("Asia/Manila")
TOLERANCE = 0.000001

ACTIVE_DISPATCH_PROJECTION_STATES = (
    "Pending Edge",
    "Awaiting Primary",
    "Primary Preserved",
    "Primary Dispatch Materialized",
)
ACTIVE_ARRIVAL_RESERVATION_STATES = (
    "Pending Edge",
    "Awaiting Primary",
    "Primary Arrival Preserved",
)


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _runtime() -> str:
    return _clean(_runtime_role() or "Primary")


def _request_uuid(value: str) -> str:
    try:
        return str(uuid.UUID(_clean(value)))
    except Exception as exc:
        raise frappe.ValidationError(
            _("Warehouse Transfer Request ID is invalid.")
        ) from exc


def _validate_edge_terminal_if_needed(device_id: Optional[str]) -> None:
    if _runtime() != "Store Edge":
        return
    device_id = _clean(device_id)
    if not device_id:
        frappe.throw(
            _("This terminal is not registered for warehouse transfer operation."),
            frappe.PermissionError,
        )
    snapshot = device_policy_snapshot(
        device_id,
        user=frappe.session.user,
        requested_context="NKT Retail",
    )
    if snapshot.get("ui_mode") != "normal":
        frappe.throw(
            _("Warehouse transfer operation is unavailable on this terminal."),
            frappe.PermissionError,
        )


def _require_transfer(transfer_name: str):
    transfer_name = _clean(transfer_name)
    if not transfer_name:
        frappe.throw(_("Internal Warehouse Transfer is required."))
    if not frappe.db.exists("NKT Warehouse Transfer", transfer_name):
        raise frappe.DoesNotExistError(
            _("Internal Warehouse Transfer {0} does not exist.").format(
                transfer_name
            )
        )
    doc = frappe.get_doc("NKT Warehouse Transfer", transfer_name)
    doc.check_permission("read")
    return doc


def _existing_event(event_uuid: str, family: str):
    if not frappe.db.exists("NKT Sync Event", event_uuid):
        return None
    event = frappe.get_doc("NKT Sync Event", event_uuid)
    if event.event_family != family:
        raise frappe.ValidationError(
            _("Warehouse Transfer Request ID is already bound to another operation.")
        )
    return event


def _dispatch_projection(transfer_name: str):
    return frappe.db.get_value(
        "NKT Edge Warehouse Transfer Projection",
        {
            "warehouse_transfer": transfer_name,
            "projection_action": "Source Dispatch",
            "projection_state": ["in", ACTIVE_DISPATCH_PROJECTION_STATES],
        },
        [
            "event_uuid",
            "projection_state",
            "primary_stock_entry",
        ],
        as_dict=True,
        order_by="creation desc",
    )


def _arrival_projection_rows(event_uuid: str):
    return frappe.get_all(
        "NKT Edge Warehouse Transfer Projection",
        filters={
            "event_uuid": event_uuid,
            "projection_action": "Destination Arrival",
        },
        fields=[
            "warehouse_transfer",
            "item_code",
            "arrived_qty",
            "projection_state",
            "primary_stock_entry",
        ],
        order_by="line_no asc",
        limit_page_length=500,
    )


def _edge_dispatch_request_result(event_uuid: str):
    event = _existing_event(event_uuid, DISPATCH_FAMILY)
    if not event:
        return None
    rows = frappe.get_all(
        "NKT Edge Warehouse Transfer Projection",
        filters={
            "event_uuid": event_uuid,
            "projection_action": "Source Dispatch",
        },
        fields=[
            "warehouse_transfer",
            "dispatched_qty",
            "projection_state",
            "primary_stock_entry",
        ],
        order_by="line_no asc",
        limit_page_length=500,
    )
    if not rows:
        frappe.throw(
            _("Recorded physical Release has no local quantity projection.")
        )
    transfers = {row.warehouse_transfer for row in rows if row.warehouse_transfer}
    if len(transfers) != 1:
        frappe.throw(
            _("Recorded physical Release has inconsistent transfer identity.")
        )
    return {
        "ok": True,
        "request_id": event_uuid,
        "warehouse_transfer": next(iter(transfers)),
        "physical_release_recorded": True,
        "total_dispatch_quantity": sum(
            flt(row.dispatched_qty) for row in rows
        ),
        "stock_entry": next(
            (
                row.primary_stock_entry
                for row in rows
                if row.primary_stock_entry
            ),
            None,
        ),
        "replayed": True,
    }


def _edge_arrival_request_result(event_uuid: str):
    event = _existing_event(event_uuid, ARRIVAL_FAMILY)
    if not event:
        return None
    rows = _arrival_projection_rows(event_uuid)
    if not rows:
        frappe.throw(
            _("Recorded physical Arrival has no local quantity projection.")
        )
    transfers = {row.warehouse_transfer for row in rows if row.warehouse_transfer}
    if len(transfers) != 1:
        frappe.throw(
            _("Recorded physical Arrival has inconsistent transfer identity.")
        )
    transfer_name = next(iter(transfers))
    doc = frappe.get_doc("NKT Warehouse Transfer", transfer_name)
    effective = _effective_arrival_rows(doc)
    return {
        "ok": True,
        "request_id": event_uuid,
        "warehouse_transfer": transfer_name,
        "physical_arrival_recorded": True,
        "arrival_quantity": sum(flt(row.arrived_qty) for row in rows),
        "remaining_quantity": sum(
            flt(row["remaining_qty"]) for row in effective
        ),
        "incoming_stock_entry": next(
            (
                row.primary_stock_entry
                for row in rows
                if row.primary_stock_entry
            ),
            None,
        ),
        "replayed": True,
    }


def _effective_arrival_rows(doc):
    if str(doc.status or "") != "In Transit" or not doc.outgoing_stock_entry:
        return []

    out = []
    for row in doc.get("items") or []:
        released = flt(row.released_qty)
        arrived = flt(row.arrived_qty)
        pending = _pending_arrival_qty(doc.name, row.item_code)
        effective_arrived = arrived + pending
        remaining = max(0.0, released - effective_arrived)
        if remaining <= TOLERANCE:
            continue
        out.append(
            {
                "row_name": row.name,
                "item_code": row.item_code,
                "item_name": row.item_name or row.item_code,
                "uom": row.uom,
                "released_qty": released,
                "canonical_arrived_qty": arrived,
                "recorded_pending_arrival_qty": pending,
                "effective_arrived_qty": effective_arrived,
                "remaining_qty": remaining,
            }
        )
    return out


def _frontdoor_state(doc):
    runtime = _runtime()
    if runtime == "Primary":
        remaining = []
        if (
            str(doc.status or "") == "In Transit"
            and doc.outgoing_stock_entry
            and not doc.incoming_stock_entry
        ):
            for row in doc.get("items") or []:
                remaining_qty = max(
                    0.0,
                    flt(row.released_qty) - flt(row.arrived_qty),
                )
                if remaining_qty > TOLERANCE:
                    remaining.append(
                        {
                            "row_name": row.name,
                            "item_code": row.item_code,
                            "item_name": row.item_name or row.item_code,
                            "uom": row.uom,
                            "released_qty": flt(row.released_qty),
                            "canonical_arrived_qty": flt(row.arrived_qty),
                            "recorded_pending_arrival_qty": 0.0,
                            "effective_arrived_qty": flt(row.arrived_qty),
                            "remaining_qty": remaining_qty,
                        }
                    )
        return {
            "warehouse_transfer": doc.name,
            "can_release": (
                str(doc.status or "Draft") == "Draft"
                and not doc.outgoing_stock_entry
                and "NKT Warehouse" in (frappe.get_roles() or [])
            ),
            "physical_release_already_recorded": bool(doc.outgoing_stock_entry),
            "can_arrive": bool(
                remaining
                and "NKT Warehouse" in (frappe.get_roles() or [])
            ),
            "arrival_rebase_pending": False,
            "arrival_rows": remaining,
        }

    if runtime != "Store Edge":
        frappe.throw(_("Warehouse transfer operation is unavailable on this server."))

    dispatch_projection = _dispatch_projection(doc.name)
    rebase_pending = _has_unrebased_materialized_arrival(doc.name)
    remaining = [] if rebase_pending else _effective_arrival_rows(doc)
    can_release = (
        str(doc.status or "Draft") == "Draft"
        and not doc.outgoing_stock_entry
        and not dispatch_projection
        and "NKT Warehouse" in (frappe.get_roles() or [])
    )
    can_arrive = bool(
        str(doc.status or "") == "In Transit"
        and doc.outgoing_stock_entry
        and not doc.incoming_stock_entry
        and remaining
        and not rebase_pending
        and "NKT Warehouse" in (frappe.get_roles() or [])
    )
    return {
        "warehouse_transfer": doc.name,
        "can_release": can_release,
        "physical_release_already_recorded": bool(
            doc.outgoing_stock_entry or dispatch_projection
        ),
        "can_arrive": can_arrive,
        "arrival_rebase_pending": bool(rebase_pending),
        "arrival_rows": remaining,
    }


@frappe.whitelist()
def get_transfer_action_state(
    transfer_name: str,
    device_id: str | None = None,
):
    _validate_edge_terminal_if_needed(device_id)
    doc = _require_transfer(transfer_name)
    return _frontdoor_state(doc)


def _dispatch_payload(doc, observed: datetime):
    items = []
    total = 0.0
    for idx, row in enumerate(doc.get("items") or [], start=1):
        qty = flt(row.requested_qty)
        if qty <= TOLERANCE:
            frappe.throw(
                _("Transfer Item {0} has no dispatch quantity.").format(
                    row.item_code
                )
            )
        items.append(
            {
                "line_no": idx,
                "warehouse_transfer_item": row.name,
                "item_code": row.item_code,
                "uom": row.uom,
                "dispatch_quantity": qty,
            }
        )
        total += qty
    if not items:
        frappe.throw(_("Internal Warehouse Transfer has no Items."))
    return {
        "warehouse_transfer": doc.name,
        "company": doc.company,
        "transfer_date": str(doc.transfer_date),
        "source_warehouse": doc.source_warehouse,
        "destination_warehouse": doc.destination_warehouse,
        "internal_dr_no": _clean(doc.internal_dr_no),
        "client_observed_at": observed.isoformat(timespec="seconds"),
        "client_ui_version": FOUNDATION_VERSION + "-WarehouseTransferForm",
        "items": items,
        "total_dispatch_quantity": total,
    }


def _parse_arrival_quantities(value: Any) -> Dict[str, float]:
    if isinstance(value, str):
        try:
            value = json.loads(value or "{}")
        except Exception as exc:
            raise frappe.ValidationError(
                _("Arrival quantities are not valid JSON.")
            ) from exc
    if not isinstance(value, dict):
        raise frappe.ValidationError(_("Arrival quantities are required."))
    return {
        _clean(item_code): flt(qty)
        for item_code, qty in value.items()
        if _clean(item_code)
    }


def _arrival_payload(doc, observed: datetime, requested: Dict[str, float]):
    if _has_unrebased_materialized_arrival(doc.name):
        frappe.throw(
            _(
                "A previously recorded Arrival is still being applied to local stock. "
                "Record the next physical Arrival after the current stock update is complete."
            )
        )

    effective = _effective_arrival_rows(doc)
    if not effective:
        frappe.throw(_("No remaining physical Arrival quantity is available."))

    expected_items = {row["item_code"] for row in effective}
    if set(requested) != expected_items:
        frappe.throw(
            _("Enter an Arrival quantity for every Item that still has quantity in transit.")
        )

    items = []
    total = 0.0
    for idx, state in enumerate(effective, start=1):
        qty = flt(requested[state["item_code"]])
        if qty <= TOLERANCE:
            frappe.throw(
                _("Arrival quantity must be greater than zero for {0}.").format(
                    state["item_code"]
                )
            )
        if qty - flt(state["remaining_qty"]) > TOLERANCE:
            frappe.throw(
                _("Arrival quantity exceeds remaining transit quantity for {0}.").format(
                    state["item_code"]
                )
            )
        items.append(
            {
                "line_no": idx,
                "warehouse_transfer_item": state["row_name"],
                "item_code": state["item_code"],
                "uom": state["uom"],
                "released_quantity": state["released_qty"],
                "cumulative_arrived_before": state["effective_arrived_qty"],
                "remaining_before": state["remaining_qty"],
                "arrival_quantity": qty,
            }
        )
        total += qty

    return {
        "warehouse_transfer": doc.name,
        "company": doc.company,
        "transfer_date": str(doc.transfer_date),
        "source_warehouse": doc.source_warehouse,
        "destination_warehouse": doc.destination_warehouse,
        "outgoing_stock_entry": doc.outgoing_stock_entry,
        "transit_warehouse": _resolve_transit_warehouse(doc.company),
        "client_observed_at": observed.isoformat(timespec="seconds"),
        "client_ui_version": FOUNDATION_VERSION + "-WarehouseTransferForm",
        "items": items,
        "total_arrival_quantity": total,
    }


@frappe.whitelist()
def confirm_source_dispatch(
    transfer_name: str,
    request_id: str,
    device_id: str | None = None,
):
    """
    One Warehouse Transfer browser action; server runtime owns the authority choice.

    Primary:
      call the existing release_transfer() business controller.

    Store Edge:
      record only immutable source physical-dispatch intent + local projection.
      No canonical Transfer status change or Stock Entry is created locally.
    """
    request_id = _request_uuid(request_id)
    runtime = _runtime()

    if runtime == "Primary":
        result = release_transfer(transfer_name)
        return {
            "ok": True,
            "request_id": request_id,
            "warehouse_transfer": result.get("transfer"),
            "physical_release_recorded": True,
            "total_dispatch_quantity": result.get(
                "total_released_quantity"
            ),
            "stock_entry": result.get("outgoing_stock_entry"),
            "replayed": False,
        }

    if runtime != "Store Edge":
        frappe.throw(_("Warehouse transfer Release is unavailable on this server."))

    _validate_edge_terminal_if_needed(device_id)
    existing = _edge_dispatch_request_result(request_id)
    if existing:
        if existing["warehouse_transfer"] != _clean(transfer_name):
            raise frappe.ValidationError(
                _("Warehouse Transfer Request ID belongs to another transfer.")
            )
        return existing

    doc = _require_transfer(transfer_name)
    observed = datetime.now(PH_TZ).replace(microsecond=0)
    payload = _dispatch_payload(doc, observed)

    accepted = accept_dispatch_intent_at_edge(
        request_id,
        _clean(device_id),
        observed.date().isoformat(),
        observed.isoformat(timespec="seconds"),
        payload,
        user=frappe.session.user,
    )
    return {
        "ok": True,
        "request_id": request_id,
        "warehouse_transfer": doc.name,
        "physical_release_recorded": True,
        "total_dispatch_quantity": flt(
            payload["total_dispatch_quantity"]
        ),
        "stock_entry": None,
        "replayed": bool(accepted.get("replay")),
    }


@frappe.whitelist()
def confirm_destination_arrival(
    transfer_name: str,
    arrival_quantities: Any,
    request_id: str,
    device_id: str | None = None,
):
    """
    One Warehouse Transfer browser action; server runtime owns the authority choice.

    Primary:
      call the existing receive_transfer() business controller.

    Store Edge:
      record only immutable destination physical-Arrival intent + local projection.
      No canonical incoming Stock Entry is created locally.
    """
    request_id = _request_uuid(request_id)
    requested = _parse_arrival_quantities(arrival_quantities)
    runtime = _runtime()

    if runtime == "Primary":
        result = receive_transfer(
            transfer_name,
            arrival_quantities=requested,
        )
        return {
            "ok": True,
            "request_id": request_id,
            "warehouse_transfer": result.get("transfer"),
            "physical_arrival_recorded": True,
            "arrival_quantity": sum(
                flt(qty)
                for qty in (result.get("arrival_quantities") or {}).values()
            ),
            "remaining_quantity": sum(
                flt(qty)
                for qty in (result.get("remaining_in_transit") or {}).values()
            ),
            "incoming_stock_entry": result.get("incoming_stock_entry"),
            "completed": bool(result.get("completed")),
            "replayed": False,
        }

    if runtime != "Store Edge":
        frappe.throw(_("Warehouse transfer Arrival is unavailable on this server."))

    _validate_edge_terminal_if_needed(device_id)
    existing = _edge_arrival_request_result(request_id)
    if existing:
        if existing["warehouse_transfer"] != _clean(transfer_name):
            raise frappe.ValidationError(
                _("Warehouse Transfer Request ID belongs to another transfer.")
            )
        return existing

    doc = _require_transfer(transfer_name)
    observed = datetime.now(PH_TZ).replace(microsecond=0)
    payload = _arrival_payload(doc, observed, requested)

    accepted = accept_arrival_intent_at_edge(
        request_id,
        _clean(device_id),
        observed.date().isoformat(),
        observed.isoformat(timespec="seconds"),
        payload,
        user=frappe.session.user,
    )

    projected_remaining = sum(
        max(
            0.0,
            flt(row["remaining_before"])
            - flt(row["arrival_quantity"]),
        )
        for row in payload["items"]
    )
    return {
        "ok": True,
        "request_id": request_id,
        "warehouse_transfer": doc.name,
        "physical_arrival_recorded": True,
        "arrival_quantity": flt(payload["total_arrival_quantity"]),
        "remaining_quantity": projected_remaining,
        "incoming_stock_entry": None,
        "completed": projected_remaining <= TOLERANCE,
        "replayed": bool(accepted.get("replay")),
    }


def installation_probe():
    return {
        "foundation_version": FOUNDATION_VERSION,
        "one_browser_endpoint_per_business_action": True,
        "runtime_owns_authority_choice": True,
        "primary_dispatch_hook": "release_transfer",
        "primary_arrival_hook": "receive_transfer",
        "store_edge_dispatch_family": DISPATCH_FAMILY,
        "store_edge_arrival_family": ARRIVAL_FAMILY,
        "store_edge_creates_canonical_stock_entry": False,
        "device_policy_enforced_on_store_edge": True,
        "edge_request_uuid_replay_reads_existing_event_projection": True,
        "frontline_response_exposes_sync_internals": False,
        "separate_offline_transfer_screen_required": False,
    }
