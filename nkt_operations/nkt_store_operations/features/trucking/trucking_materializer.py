from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Dict
from zoneinfo import ZoneInfo

import frappe
from frappe.utils import get_datetime

from nkt_operations.nkt_store_operations.features.offline_edge.sync_transport import _runtime_role
from nkt_operations.nkt_store_operations.features.trucking.trucking_offline_contract import (
    TRIP_LIFECYCLE_FAMILY,
    normalize_trucking_trip_lifecycle_intent,
)

FOUNDATION_VERSION = "C15C.10L-R4C"
PRIMARY_JOURNAL = "NKT Primary Trucking Trip Intent"
CANONICAL_DOCTYPE = "NKT Trucking Trip"
PH_TZ = ZoneInfo("Asia/Manila")


def _require_primary():
    if _runtime_role() != "Primary":
        raise frappe.PermissionError("Trucking canonical materializer is Primary-only.")


def _naive_manila(value):
    dt = get_datetime(value)
    if dt.tzinfo is None:
        return dt
    return dt.astimezone(PH_TZ).replace(tzinfo=None)


def _payload(journal) -> Dict[str, Any]:
    raw = json.loads(journal.canonical_payload_json)
    return normalize_trucking_trip_lifecycle_intent(raw)


def _lock_name(edge_trip_uuid):
    return "nkt-10l-trip-" + str(edge_trip_uuid).replace("-", "")[:28]


def _acquire(edge_trip_uuid):
    rows = frappe.db.sql("SELECT GET_LOCK(%s,%s)", (_lock_name(edge_trip_uuid), 30), as_list=True)
    if not rows or int(rows[0][0] or 0) != 1:
        raise frappe.ValidationError("Trucking Trip materialization is busy. Safe retry required.")


def _release(edge_trip_uuid):
    try:
        frappe.db.sql("SELECT RELEASE_LOCK(%s)", (_lock_name(edge_trip_uuid),))
    except Exception:
        pass


def _events(edge_trip_uuid):
    return frappe.get_all(
        PRIMARY_JOURNAL,
        filters={
            "edge_trip_uuid": edge_trip_uuid,
            "event_family": TRIP_LIFECYCLE_FAMILY,
            "preservation_state": "Preserved",
        },
        fields=["name", "primary_preserved_at", "creation"],
        order_by="primary_preserved_at asc, creation asc, name asc",
    )


def _existing_trip(edge_trip_uuid):
    return frappe.db.get_value(
        PRIMARY_JOURNAL,
        {
            "edge_trip_uuid": edge_trip_uuid,
            "materialization_state": "Canonical Materialized",
            "canonical_doctype": CANONICAL_DOCTYPE,
            "canonical_name": ["!=", ""],
        },
        "canonical_name",
        order_by="primary_preserved_at asc",
    )


def _set_flags(doc, journal):
    doc.flags.nkt_c15c10l_primary_materialization = True
    doc.flags.nkt_c15c10l_origin_user = str(journal.origin_user or "")
    doc.flags.nkt_c15c10l_event_datetime = _naive_manila(journal.event_datetime)


def _apply_operational_fields(doc, payload):
    mapping = {
        "trip_date": payload["trip_date"],
        "job_type": payload["job_type"],
        "customer": payload["customer"] or None,
        "related_business": payload["related_business"] or None,
        "company": payload["company"] or None,
        "source_c9_trucking_job": payload["source_c9_trucking_job"] or None,
        "origin": payload["origin"],
        "destination": payload["destination"],
        "dr_no": payload["dr_no"],
        "container_no": payload["container_no"],
        "eir_no": payload["eir_no"],
        "reference_no": payload["reference_no"],
        "remarks": payload["remarks"],
        "vehicle": payload["vehicle"] or None,
        "trailer": payload["trailer"] or None,
        "driver_name": payload["driver_name"],
        "driver_contact": payload["driver_contact"],
        "helper_name": payload["helper_name"],
        "has_backload": payload["has_backload"],
        "backload_description": payload["backload_description"],
        "backload_reference": payload["backload_reference"],
        "container_return_required": payload["container_return_required"],
        "container_returned": payload["container_returned"],
        "eir_required": payload["eir_required"],
        "paperwork_complete": payload["paperwork_complete"],
        "paperwork_notes": payload["paperwork_notes"],
        "pod_attachment": payload["pod_attachment"],
        "pod_notes": payload["pod_notes"],
    }
    for field, value in mapping.items():
        doc.set(field, value)


def _mark(journal, trip_name):
    frappe.db.set_value(
        PRIMARY_JOURNAL,
        journal.name,
        {
            "materialization_state": "Canonical Materialized",
            "canonical_doctype": CANONICAL_DOCTYPE,
            "canonical_name": trip_name,
        },
        update_modified=False,
    )


def _materialize_one(journal, current_trip):
    payload = _payload(journal)

    if str(journal.materialization_state or "") == "Canonical Materialized":
        if (
            journal.canonical_doctype != CANONICAL_DOCTYPE
            or not journal.canonical_name
            or not frappe.db.exists(CANONICAL_DOCTYPE, journal.canonical_name)
        ):
            raise frappe.ValidationError("Trucking journal claims an invalid canonical materialization.")
        return frappe.get_doc(CANONICAL_DOCTYPE, journal.canonical_name), True

    if payload["action"] == "Create":
        if current_trip:
            raise frappe.ValidationError(
                "Offline Trucking Create conflicts with an already materialized Edge Trip identity."
            )
        doc = frappe.get_doc({"doctype": CANONICAL_DOCTYPE, "status": "Draft"})
        _apply_operational_fields(doc, payload)
        _set_flags(doc, journal)
        original_user = frappe.session.user
        try:
            frappe.set_user("Administrator")
            doc.insert(ignore_permissions=True)
        finally:
            frappe.set_user(original_user)
        _mark(journal, doc.name)
        return doc, False

    if not current_trip:
        raise frappe.ValidationError(
            "Offline Trucking event reached Primary before its Create event was materialized. Safe retry required."
        )

    doc = frappe.get_doc(CANONICAL_DOCTYPE, current_trip)
    if str(doc.trip_date or "") != str(payload["trip_date"] or ""):
        raise frappe.ValidationError(
            "Canonical Trucking Trip Date is immutable after offline Create. "
            "Cross-midnight physical events must retain the original Trip Date."
        )
    if str(doc.status or "") != str(payload["previous_status"] or ""):
        raise frappe.ValidationError(
            f"Canonical Trucking status conflicts with immutable Edge history: "
            f"{doc.status} != {payload['previous_status']}."
        )

    _apply_operational_fields(doc, payload)
    doc.status = payload["new_status"]
    _set_flags(doc, journal)
    original_user = frappe.session.user
    try:
        frappe.set_user("Administrator")
        doc.save(ignore_permissions=True)
    finally:
        frappe.set_user(original_user)

    _mark(journal, doc.name)
    return doc, False


def materialize_preserved_trucking_event(event_uuid: str) -> Dict[str, Any]:
    _require_primary()
    journal_name = str(event_uuid or "")
    if not frappe.db.exists(PRIMARY_JOURNAL, journal_name):
        raise frappe.DoesNotExistError("Preserved Trucking lifecycle journal is unavailable.")

    target = frappe.get_doc(PRIMARY_JOURNAL, journal_name)
    if target.event_family != TRIP_LIFECYCLE_FAMILY:
        raise frappe.ValidationError("Journal is not a Trucking Trip lifecycle intent.")

    edge_trip_uuid = str(target.edge_trip_uuid or "")
    _acquire(edge_trip_uuid)
    try:
        if (
            target.materialization_state == "Canonical Materialized"
            and target.canonical_doctype == CANONICAL_DOCTYPE
            and target.canonical_name
        ):
            return {
                "event_uuid": target.name,
                "edge_trip_uuid": edge_trip_uuid,
                "name": target.canonical_name,
                "doctype": CANONICAL_DOCTYPE,
                "replay": True,
            }

        sequence = _events(edge_trip_uuid)
        names = [row.name for row in sequence]
        if journal_name not in names:
            raise frappe.ValidationError("Target Trucking journal is outside the preserved sequence.")

        current_trip = _existing_trip(edge_trip_uuid)
        replay = False
        for row in sequence[: names.index(journal_name) + 1]:
            event = frappe.get_doc(PRIMARY_JOURNAL, row.name)
            if event.materialization_state == "Canonical Materialized":
                current_trip = event.canonical_name or current_trip
                continue
            doc, was_replay = _materialize_one(event, current_trip)
            current_trip = doc.name
            replay = replay or was_replay

        target.reload()
        if target.materialization_state != "Canonical Materialized" or not target.canonical_name:
            raise frappe.ValidationError("Trucking target event did not materialize deterministically.")

        return {
            "event_uuid": target.name,
            "edge_trip_uuid": edge_trip_uuid,
            "name": target.canonical_name,
            "doctype": CANONICAL_DOCTYPE,
            "status": frappe.db.get_value(CANONICAL_DOCTYPE, target.canonical_name, "status"),
            "replay": bool(replay),
        }
    finally:
        _release(edge_trip_uuid)


@frappe.whitelist()
def materialize(event_uuid):
    return materialize_preserved_trucking_event(event_uuid)
