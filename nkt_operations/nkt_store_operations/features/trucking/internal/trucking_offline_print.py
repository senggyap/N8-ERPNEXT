from __future__ import annotations

import hashlib
import html
import json
from typing import Any, Dict

import frappe

from nkt_operations.nkt_store_operations.features.trucking.access import (
    is_external_carrier_privileged,
)
from nkt_operations.nkt_store_operations.features.trucking.trucking_offline_contract import (
    canonical_trucking_trip_lifecycle_intent_json,
    normalize_trucking_trip_lifecycle_intent,
)

FOUNDATION_VERSION = "C15C.10L-R4"
PROJECTION = "NKT Edge Trucking Trip Projection"


def _escape(value):
    return html.escape(str(value or ""), quote=True)


def _snapshot(edge_trip_uuid):
    name = frappe.db.get_value(PROJECTION, {"edge_trip_uuid": str(edge_trip_uuid)}, "name")
    if not name:
        raise frappe.DoesNotExistError("Offline Trucking Trip snapshot is unavailable.")
    projection = frappe.get_doc(PROJECTION, name)
    raw = str(projection.print_snapshot_json or "")
    if not raw:
        raise frappe.ValidationError("Offline Trucking print snapshot is unavailable.")
    payload = normalize_trucking_trip_lifecycle_intent(json.loads(raw))
    canonical = canonical_trucking_trip_lifecycle_intent_json(payload)
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    if digest != str(projection.print_snapshot_sha256 or "").lower():
        raise frappe.ValidationError("Offline Trucking print snapshot integrity check failed.")
    return projection, payload, canonical, digest


def _assert_visibility(payload):
    if is_external_carrier_privileged():
        return
    vehicle = str(payload.get("vehicle") or "").strip()
    if vehicle:
        ownership = str(
            frappe.db.get_value("NKT Vehicle", vehicle, "custom_fleet_ownership") or ""
        ).strip()
        if ownership == "External Carrier":
            raise frappe.PermissionError("External carrier trucking is restricted to Owner/Admin.")
    if str(payload.get("source_c9_trucking_job") or "").strip():
        job = frappe.db.get_value(
            "NKT Trucking Job",
            payload["source_c9_trucking_job"],
            ["delivery_vehicle", "carrier_account"],
            as_dict=True,
        )
        if job and str(job.carrier_account or "").strip():
            raise frappe.PermissionError("External carrier trucking is restricted to Owner/Admin.")


def render_offline_operational_waybill(edge_trip_uuid: str) -> Dict[str, Any]:
    projection, payload, canonical, digest = _snapshot(edge_trip_uuid)
    _assert_visibility(payload)

    rows = [
        ("Offline Trip Ref", "EDGE-TRIP-" + payload["edge_trip_uuid"]),
        ("Status", payload["new_status"]),
        ("Trip Date", payload["trip_date"]),
        ("Physical Event Time", payload["event_datetime"]),
        ("Related Business / Customer", payload["customer"] or payload["related_business"]),
        ("Origin", payload["origin"]),
        ("Destination", payload["destination"]),
        ("Truck / Vehicle", payload["vehicle"]),
        ("Trailer / Chassis", payload["trailer"]),
        ("Driver", payload["driver_name"]),
        ("Driver Contact", payload["driver_contact"]),
        ("Helper", payload["helper_name"]),
        ("Container No.", payload["container_no"]),
        ("DR No.", payload["dr_no"]),
        ("EIR No.", payload["eir_no"]),
        ("Reference", payload["reference_no"]),
        ("Container Returned", "YES" if payload["container_returned"] else "NO"),
        ("Required Papers Complete", "YES" if payload["paperwork_complete"] else "NO"),
        ("Backload", payload["backload_description"] if payload["has_backload"] else "None"),
        ("Remarks", payload["remarks"]),
        ("POD / Evidence", payload["pod_attachment"]),
        ("POD Notes", payload["pod_notes"]),
    ]

    body = "".join(
        f"<tr><th>{_escape(label)}</th><td>{_escape(value)}</td></tr>"
        for label, value in rows
        if str(value or "").strip()
    )
    rendered = (
        "<!doctype html><html><head><meta charset='utf-8'>"
        "<title>Offline Operational Waybill</title>"
        "<style>"
        "body{font-family:Arial,sans-serif;font-size:12px;margin:24px}"
        "h1{font-size:20px;margin:0}h2{font-size:14px;margin:4px 0 16px}"
        "table{border-collapse:collapse;width:100%}"
        "th,td{border:1px solid #333;padding:6px;text-align:left;vertical-align:top}"
        "th{width:32%}.note{margin-top:14px;font-size:10px}"
        "</style></head><body>"
        "<h1>ENT Trucking Services</h1>"
        "<h2>OFFLINE OPERATIONAL DISPATCH / WAYBILL</h2>"
        f"<table>{body}</table>"
        "<div class='note'>Operational outage document only. "
        "No trucking rate, carrier payable, Customer SOA, Trucker SOA/payment, "
        "or driver-incentive payout authority is created by this print.</div>"
        "</body></html>"
    )
    return {
        "edge_trip_uuid": payload["edge_trip_uuid"],
        "status": payload["new_status"],
        "snapshot_sha256": digest,
        "html": rendered,
        "contains_money_authority": False,
        "official_financial_statement": False,
    }


@frappe.whitelist()
def print_offline_operational_waybill(edge_trip_uuid):
    return render_offline_operational_waybill(edge_trip_uuid)
