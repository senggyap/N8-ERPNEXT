from __future__ import annotations

import json
import uuid
from datetime import datetime
from typing import Any, Dict, Optional
from zoneinfo import ZoneInfo

import frappe
from frappe import _
from frappe.utils import flt

from nkt_operations.nkt_store_operations import fast_screen_backend as legacy_fast
from nkt_operations.nkt_store_operations.features.offline_edge.policy import (
    device_policy_snapshot,
)
from nkt_operations.nkt_store_operations.features.offline_edge.sync_transport import _runtime_role
from nkt_operations.nkt_store_operations.features.inventory.internal.warehouse_release_intent import (
    WAREHOUSE_RELEASE_INTENT_FAMILY,
    accept_warehouse_release_intent_at_edge,
)

PH_TZ = ZoneInfo("Asia/Manila")
ACTIVE_PROJECTION_STATES = (
    "Pending Edge",
    "Awaiting Primary",
    "Primary Preserved",
    "Primary Stock Materialized",
)


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _runtime() -> str:
    return _clean(_runtime_role() or "Primary")


def _validate_edge_terminal_if_needed(device_id: Optional[str]) -> None:
    if _runtime() != "Store Edge":
        return
    device_id = _clean(device_id)
    if not device_id:
        frappe.throw(
            _("This terminal is not registered for offline warehouse release."),
            frappe.PermissionError,
        )
    snapshot = device_policy_snapshot(
        device_id,
        user=frappe.session.user,
        requested_context="NKT Retail",
    )
    if snapshot.get("ui_mode") != "normal":
        frappe.throw(
            _("Warehouse release is unavailable on this terminal."),
            frappe.PermissionError,
        )


def _active_projection(release_name: str):
    if _runtime() != "Store Edge":
        return None
    return frappe.db.get_value(
        "NKT Edge Warehouse Release Projection",
        {
            "warehouse_release": release_name,
            "projection_state": ["in", ACTIVE_PROJECTION_STATES],
        },
        [
            "event_uuid",
            "customer_order",
            "warehouse",
            "released_qty",
            "projection_state",
            "primary_stock_entry",
        ],
        as_dict=True,
        order_by="creation desc",
    )


def _edge_recorded_status(event_uuid: str) -> Dict[str, Any]:
    event_uuid = _clean(event_uuid)
    if not event_uuid or not frappe.db.exists("NKT Sync Event", event_uuid):
        return {"found": False, "request_id": event_uuid}
    event = frappe.get_doc("NKT Sync Event", event_uuid)
    if event.event_family != WAREHOUSE_RELEASE_INTENT_FAMILY:
        return {"found": False, "request_id": event_uuid}

    rows = frappe.get_all(
        "NKT Edge Warehouse Release Projection",
        filters={"event_uuid": event_uuid},
        fields=[
            "warehouse_release",
            "customer_order",
            "warehouse",
            "released_qty",
            "projection_state",
            "primary_stock_entry",
        ],
        order_by="line_no asc",
        limit_page_length=500,
    )
    if not rows:
        return {"found": False, "request_id": event_uuid}

    releases = {row.warehouse_release for row in rows if row.warehouse_release}
    orders = {row.customer_order for row in rows if row.customer_order}
    warehouses = {row.warehouse for row in rows if row.warehouse}
    if len(releases) != 1 or len(orders) != 1 or len(warehouses) != 1:
        frappe.throw(_("Recorded warehouse release has inconsistent local identity."))

    return {
        "found": True,
        "request_id": event_uuid,
        "warehouse_release": next(iter(releases)),
        "customer_order": next(iter(orders)),
        "source_warehouse": next(iter(warehouses)),
        "total_release_quantity": sum(flt(row.released_qty) for row in rows),
        "physical_release_recorded_at_edge": True,
        "warehouse_release_submitted": False,
        "stock_entry": next(
            (row.primary_stock_entry for row in rows if row.primary_stock_entry),
            None,
        ),
        "next_draft_releases": [],
    }


@frappe.whitelist()
def get_warehouse_release_bootstrap(device_id: str | None = None):
    _validate_edge_terminal_if_needed(device_id)
    result = legacy_fast.get_warehouse_release_bootstrap()
    if _runtime() != "Store Edge":
        return result

    # A physical release already recorded at Edge must not reappear as a
    # releasable draft while transport/Primary stock materialization catches up.
    result["queue"] = [
        row for row in (result.get("queue") or [])
        if not _active_projection(row.get("name"))
    ]
    return result


@frappe.whitelist()
def get_warehouse_release_context(
    release_name: str,
    device_id: str | None = None,
):
    _validate_edge_terminal_if_needed(device_id)
    result = legacy_fast.get_warehouse_release_context(release_name)
    if _runtime() == "Store Edge" and _active_projection(release_name):
        result["can_release"] = False
        result["physical_release_already_recorded"] = True
    return result


def _intent_payload_from_fast_payload(payload: Dict[str, Any]):
    release_name = _clean(payload.get("warehouse_release"))
    request_id = _clean(payload.get("request_id"))
    if not release_name or not request_id:
        frappe.throw(_("Warehouse Release and Request ID are required."))
    try:
        request_id = str(uuid.UUID(request_id))
    except Exception:
        frappe.throw(_("Warehouse Release Request ID is invalid."))

    if not frappe.db.exists("NKT Warehouse Release", release_name):
        frappe.throw(
            _("Warehouse Release {0} no longer exists.").format(release_name)
        )

    doc = frappe.get_doc("NKT Warehouse Release", release_name)
    legacy_fast._validate_release_access(doc)
    if int(doc.docstatus or 0) != 0:
        frappe.throw(_("Warehouse Release is no longer an open draft."))
    if _clean(doc.get("release_status") or "Draft") != "Draft":
        frappe.throw(
            _("Warehouse Release is not available for physical release.")
        )

    legacy_fast._refresh_release_quantities(doc)

    requested = {}
    for raw in payload.get("items") or []:
        row_name = _clean(raw.get("name"))
        if row_name:
            requested[row_name] = flt(raw.get("release_quantity"))

    lines = []
    total = 0.0
    for idx, row in enumerate(doc.get("items") or [], start=1):
        qty = flt(requested.get(row.name))
        if qty < -0.000001:
            frappe.throw(
                _("Release quantity cannot be negative for {0}.").format(row.item)
            )
        if qty - flt(row.remaining_quantity) > 0.000001:
            frappe.throw(
                _("Release quantity exceeds the remaining quantity for {0}.").format(
                    row.item
                )
            )
        if qty <= 0.000001:
            continue
        lines.append(
            {
                "line_no": len(lines) + 1,
                "warehouse_release_item": row.name,
                "customer_order_item": row.customer_order_item,
                "item_code": row.item,
                "uom": row.uom,
                "source_warehouse": row.source_warehouse,
                "release_quantity": qty,
            }
        )
        total += qty

    if total <= 0.000001:
        frappe.throw(
            _("Enter a release quantity greater than zero for at least one item.")
        )

    reference = _clean(payload.get("mother_release_reference"))
    if not reference:
        frappe.throw(
            _("Release Authorization Reference is required before warehouse release.")
        )

    observed = datetime.now(PH_TZ).replace(microsecond=0)
    intent_payload = {
        "warehouse_release": doc.name,
        "customer_order": doc.customer_order,
        "company": doc.company,
        "customer": doc.customer,
        "source_warehouse": doc.get("custom_nkt_source_warehouse"),
        "release_reference": reference,
        "driver_name": _clean(payload.get("driver_name")),
        "plate_number": _clean(payload.get("plate_number")),
        "client_observed_at": observed.isoformat(timespec="seconds"),
        "client_ui_version": "C15C.10E-R8-WarehouseReleaseFastScreen",
        "items": lines,
        "total_release_quantity": total,
    }
    return request_id, observed, doc, intent_payload


@frappe.whitelist()
def finalize_warehouse_release_fast(
    payload: Any,
    device_id: str | None = None,
):
    """
    One browser endpoint; runtime decides authority.

    Primary:
      use the existing online Warehouse Release Fast Screen path unchanged.

    Store Edge:
      record only immutable physical Release Intent + local quantity projection.
      Do NOT submit canonical Warehouse Release or create Stock Entry locally.
    """
    if isinstance(payload, str):
        payload = json.loads(payload or "{}")
    payload = payload or {}

    runtime = _runtime()
    if runtime == "Primary":
        return legacy_fast.finalize_warehouse_release_fast(payload)
    if runtime != "Store Edge":
        frappe.throw(_("Warehouse release is unavailable on this server."))

    _validate_edge_terminal_if_needed(device_id)
    request_id, observed, doc, intent_payload = _intent_payload_from_fast_payload(
        payload
    )

    accepted = accept_warehouse_release_intent_at_edge(
        request_id,
        _clean(device_id),
        observed.date().isoformat(),
        observed.isoformat(timespec="seconds"),
        intent_payload,
        user=frappe.session.user,
    )

    return {
        "ok": True,
        "request_id": request_id,
        "replayed": bool(accepted.get("replay")),
        "warehouse_release": doc.name,
        "customer": doc.customer,
        "customer_name": doc.customer_name,
        "customer_order": doc.customer_order,
        "source_warehouse": doc.get("custom_nkt_source_warehouse"),
        "total_release_quantity": flt(
            intent_payload["total_release_quantity"]
        ),
        "is_partial_release": any(
            flt(line["release_quantity"])
            < flt(
                next(
                    row.remaining_quantity
                    for row in (doc.get("items") or [])
                    if row.name == line["warehouse_release_item"]
                )
            )
            for line in intent_payload["items"]
        ),
        "driver_name": intent_payload["driver_name"],
        "plate_number": intent_payload["plate_number"],
        "mother_release_reference": intent_payload["release_reference"],
        "stock_entry": None,
        "next_draft_releases": [],
        "physical_release_recorded_at_edge": True,
        "warehouse_release_submitted": False,
        "official_print_available": False,
    }


@frappe.whitelist()
def get_warehouse_release_request_status(
    request_id: str,
    device_id: str | None = None,
):
    _validate_edge_terminal_if_needed(device_id)
    if _runtime() == "Primary":
        return legacy_fast.get_warehouse_release_request_status(request_id)
    if _runtime() == "Store Edge":
        return _edge_recorded_status(request_id)
    return {"found": False, "request_id": _clean(request_id)}


@frappe.whitelist()
def confirm_warehouse_change_recall(
    release_name: str,
    device_id: str | None = None,
):
    _validate_edge_terminal_if_needed(device_id)
    if _runtime() == "Primary":
        return legacy_fast.confirm_warehouse_change_recall(release_name)
    frappe.throw(
        _(
            "Recall confirmation requires connection to the main system. "
            "Do not release additional stock on this recalled document until the connection is restored."
        )
    )
