from __future__ import annotations

import json
import uuid
from datetime import datetime
from typing import Any, Dict
from zoneinfo import ZoneInfo

import frappe
from frappe.utils import getdate

FOUNDATION_VERSION = "C15C.10L-R4D"
PH_TZ = ZoneInfo("Asia/Manila")

TRIP_LIFECYCLE_FAMILY = "NKT Trucking Trip Lifecycle Intent"
TRIP_LIFECYCLE_ACTION = "Record Trucking Trip Lifecycle Offline"

TRIP_ACTIONS = {
    "Create",
    "Request",
    "Schedule",
    "Dispatch",
    "Pickup",
    "In Transit",
    "Deliver",
    "Container Return",
    "Close",
    "Record Paperwork",
}

STATUS_BY_ACTION = {
    "Create": "Draft",
    "Request": "Requested",
    "Schedule": "Scheduled",
    "Dispatch": "Dispatched",
    "Pickup": "In Transit",
    "In Transit": "In Transit",
    "Deliver": "Delivered",
    "Close": "Closed",
}

ALLOWED_TRANSITIONS = {
    "Draft": {"Requested", "Cancelled"},
    "Requested": {"Scheduled", "Cancelled"},
    "Scheduled": {"Dispatched", "Cancelled"},
    "Dispatched": {"In Transit", "Delivered", "Cancelled"},
    "In Transit": {"Delivered", "Cancelled"},
    "Delivered": {"Closed"},
    "Closed": set(),
    "Cancelled": set(),
}

TRIP_RAW_FIELDS = {
    "submit_request_id",
    "edge_trip_uuid",
    "action",
    "trip_date",
    "company",
    "job_type",
    "customer",
    "related_business",
    "source_c9_trucking_job",
    "origin",
    "destination",
    "dr_no",
    "container_no",
    "eir_no",
    "reference_no",
    "remarks",
    "vehicle",
    "trailer",
    "driver_name",
    "driver_contact",
    "helper_name",
    "previous_status",
    "new_status",
    "event_datetime",
    "has_backload",
    "backload_description",
    "backload_reference",
    "container_return_required",
    "container_returned",
    "eir_required",
    "paperwork_complete",
    "paperwork_notes",
    "pod_attachment",
    "pod_notes",
    "client_ui_version",
}

DERIVED_FIELDS = {
    "physical_event_time_is_immutable",
    "employee_manual_backdate_allowed",
    "financial_direction",
    "offline_money_fields_allowed",
    "driver_incentive_prerequisites_may_be_observed_offline",
    "driver_incentive_payment_primary_only",
    "external_supplier_arrival_reuses_supplier_receiving",
    "operational_printing_allowed_from_edge_snapshot",
}

FORBIDDEN_MONEY_OR_PRIMARY_FIELDS = {
    "rate",
    "unit_price",
    "amount",
    "grand_total",
    "customer_soa",
    "customer_collection",
    "collection_status",
    "carrier_rate",
    "carrier_payable_amount",
    "trucker_soa",
    "trucker_payment",
    "net_payable",
    "payment_reference",
    "payment_status",
    "driver_incentive_eligible",
    "incentive_tracking_status",
    "incentive_batch",
    "incentive_paid",
    "incentive_paid_amount",
    "incentive_paid_at",
    "fleet_ownership_snapshot",
    "carrier_account_snapshot",
    "paperwork_verified_by",
    "paperwork_verified_at",
}

ASSIGNMENT_REQUIRED_STATUSES = {"Scheduled", "Dispatched", "In Transit", "Delivered", "Closed"}


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    )


def _uuid(value: Any, label: str) -> str:
    try:
        return str(uuid.UUID(str(value)))
    except Exception as exc:
        raise frappe.ValidationError(f"{label} is invalid.") from exc


def _text(value: Any, label: str, max_len: int, *, required: bool = False) -> str:
    out = str(value or "").strip()
    if required and not out:
        raise frappe.ValidationError(f"{label} is required.")
    if len(out) > max_len:
        raise frappe.ValidationError(f"{label} is too long.")
    return out


def _flag(value: Any) -> int:
    return 1 if bool(int(value or 0)) else 0


def _manila_datetime(value: Any, label: str) -> datetime:
    raw = str(value or "").strip()
    if not raw:
        raise frappe.ValidationError(f"{label} is required.")
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        dt = value if isinstance(value, datetime) else datetime.fromisoformat(raw)
    except Exception as exc:
        raise frappe.ValidationError(f"{label} is invalid.") from exc
    if dt.tzinfo is None:
        return dt.replace(tzinfo=PH_TZ)
    return dt.astimezone(PH_TZ)


def _iso(dt: datetime) -> str:
    return dt.replace(microsecond=0).isoformat()


def _reject_unsafe_fields(payload: Dict[str, Any]) -> None:
    if not isinstance(payload, dict):
        raise frappe.ValidationError("Trucking Trip Lifecycle Intent payload must be an object.")

    forbidden = set(payload) & FORBIDDEN_MONEY_OR_PRIMARY_FIELDS
    if forbidden:
        raise frappe.ValidationError(
            "Offline trucking intent cannot contain Primary money/payable/incentive authority fields: "
            + ", ".join(sorted(forbidden))
        )

    extra = set(payload) - TRIP_RAW_FIELDS - DERIVED_FIELDS
    if extra:
        raise frappe.ValidationError(
            "Trucking Trip Lifecycle Intent contains unsupported fields: "
            + ", ".join(sorted(extra))
        )


def _raw_projection(payload: Dict[str, Any]) -> Dict[str, Any]:
    return {key: payload[key] for key in TRIP_RAW_FIELDS if key in payload}


def _validate_action_status(action: str, previous_status: str, new_status: str) -> None:
    if action not in TRIP_ACTIONS:
        raise frappe.ValidationError("Unsupported offline trucking lifecycle action.")

    if action in ("Record Paperwork", "Container Return"):
        if new_status not in ("Delivered", "Closed"):
            raise frappe.ValidationError(
                "Offline trucking paperwork/container-return observation requires a Delivered or Closed trip."
            )
        if previous_status != new_status:
            raise frappe.ValidationError(
                "Offline trucking paperwork/container-return observation may not rewrite trip status."
            )
        return

    expected = STATUS_BY_ACTION.get(action)
    if expected and new_status != expected:
        raise frappe.ValidationError(
            f"Offline trucking action {action} requires status {expected}."
        )

    if action == "Create":
        if previous_status:
            raise frappe.ValidationError("New offline trucking trip cannot claim a prior status.")
        return

    if not previous_status:
        raise frappe.ValidationError("Offline trucking status change requires previous status.")

    if new_status not in ALLOWED_TRANSITIONS.get(previous_status, set()):
        raise frappe.ValidationError(
            f"Invalid offline trucking transition: {previous_status} → {new_status}."
        )


def _normalize_raw(payload: Dict[str, Any]) -> Dict[str, Any]:
    _reject_unsafe_fields(payload)

    action = _text(payload.get("action"), "Action", 40, required=True)
    previous_status = _text(payload.get("previous_status"), "Previous Status", 40)
    new_status = _text(payload.get("new_status"), "New Status", 40, required=True)
    _validate_action_status(action, previous_status, new_status)

    event_dt = _manila_datetime(payload.get("event_datetime"), "Physical Event Date / Time")
    trip_date = getdate(payload.get("trip_date"))
    if action == "Create" and trip_date != event_dt.date():
        raise frappe.ValidationError(
            "Offline trucking Create Trip Date must equal the true Store Edge creation date. "
            "Employees do not receive a manual backdate override."
        )

    job_type = _text(payload.get("job_type"), "Job Type", 80, required=True)
    customer = _text(payload.get("customer"), "Customer", 180)
    related_business = _text(payload.get("related_business"), "Related Business", 180)
    source_job = _text(payload.get("source_c9_trucking_job"), "Source C9 Trucking Job", 180)

    if job_type == "External Customer" and not customer:
        raise frappe.ValidationError("Customer is required for an External Customer trucking trip.")

    if job_type == "Supplier Arrival Link" and not source_job:
        raise frappe.ValidationError(
            "Supplier Arrival Link trip requires its source NKT Trucking Job."
        )

    vehicle = _text(payload.get("vehicle"), "Truck / Vehicle", 180)
    driver_name = _text(payload.get("driver_name"), "Driver", 180, required=True)

    if new_status in ASSIGNMENT_REQUIRED_STATUSES and not vehicle:
        raise frappe.ValidationError(
            "Truck / Vehicle is required once an offline trucking trip is Scheduled or later."
        )

    paperwork_complete = _flag(payload.get("paperwork_complete"))
    container_return_required = _flag(payload.get("container_return_required"))
    container_returned = _flag(payload.get("container_returned"))
    eir_required = _flag(payload.get("eir_required"))
    container_no = _text(payload.get("container_no"), "Container No.", 180)
    eir_no = _text(payload.get("eir_no"), "EIR No.", 180)

    if (container_return_required or eir_required) and not container_no:
        raise frappe.ValidationError(
            "Container No. is required for a container-controlled trucking trip."
        )
    if action == "Container Return" and not container_returned:
        raise frappe.ValidationError(
            "Container Return action requires Container Returned = 1."
        )

    if paperwork_complete:
        if new_status not in ("Delivered", "Closed"):
            raise frappe.ValidationError(
                "All Required Papers Complete may be observed only after the trip is Delivered."
            )
        if container_return_required and not container_returned:
            raise frappe.ValidationError(
                "Container Return must be observed before trip paperwork can be complete."
            )
        if eir_required and not eir_no:
            raise frappe.ValidationError(
                "EIR No. is required before trip paperwork can be complete."
            )

    return {
        "submit_request_id": _text(
            payload.get("submit_request_id"), "Submit Request ID", 180, required=True
        ),
        "edge_trip_uuid": _uuid(payload.get("edge_trip_uuid"), "Edge Trip UUID"),
        "action": action,
        "trip_date": trip_date.isoformat(),
        "company": _text(payload.get("company"), "Company", 180),
        "job_type": job_type,
        "customer": customer,
        "related_business": related_business,
        "source_c9_trucking_job": source_job,
        "origin": _text(payload.get("origin"), "Origin", 500, required=True),
        "destination": _text(payload.get("destination"), "Destination", 500, required=True),
        "dr_no": _text(payload.get("dr_no"), "DR No.", 180),
        "container_no": container_no,
        "eir_no": eir_no,
        "reference_no": _text(payload.get("reference_no"), "Reference No.", 180),
        "remarks": _text(payload.get("remarks"), "Remarks", 2000),
        "vehicle": vehicle,
        "trailer": _text(payload.get("trailer"), "Trailer / Chassis", 180),
        "driver_name": driver_name,
        "driver_contact": _text(payload.get("driver_contact"), "Driver Contact", 180),
        "helper_name": _text(payload.get("helper_name"), "Helper", 180),
        "previous_status": previous_status,
        "new_status": new_status,
        "event_datetime": _iso(event_dt),
        "has_backload": _flag(payload.get("has_backload")),
        "backload_description": _text(
            payload.get("backload_description"), "Backload Details", 2000
        ),
        "backload_reference": _text(
            payload.get("backload_reference"), "Backload Reference", 180
        ),
        "container_return_required": container_return_required,
        "container_returned": container_returned,
        "eir_required": eir_required,
        "paperwork_complete": paperwork_complete,
        "paperwork_notes": _text(
            payload.get("paperwork_notes"), "Paperwork Notes", 2000
        ),
        "pod_attachment": _text(payload.get("pod_attachment"), "POD Attachment", 500),
        "pod_notes": _text(payload.get("pod_notes"), "POD Notes", 2000),
        "client_ui_version": _text(
            payload.get("client_ui_version"), "Client UI Version", 120
        ),
        "physical_event_time_is_immutable": True,
        "employee_manual_backdate_allowed": False,
        "financial_direction": "Primary must classify from vehicle ownership + service beneficiary",
        "offline_money_fields_allowed": False,
        "driver_incentive_prerequisites_may_be_observed_offline": True,
        "driver_incentive_payment_primary_only": True,
        "external_supplier_arrival_reuses_supplier_receiving": True,
        "operational_printing_allowed_from_edge_snapshot": True,
    }


def normalize_trucking_trip_lifecycle_intent(payload: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(payload, dict):
        raise frappe.ValidationError("Trucking Trip Lifecycle Intent payload must be an object.")

    _reject_unsafe_fields(payload)
    if set(payload) & DERIVED_FIELDS:
        raw = _raw_projection(payload)
        renormalized = _normalize_raw(raw)
        if _canonical_json(renormalized) != _canonical_json(payload):
            raise frappe.ValidationError(
                "Trucking Trip Lifecycle Intent normalized payload contains changed or forged derived content."
            )
        return renormalized
    return _normalize_raw(payload)


def canonical_trucking_trip_lifecycle_intent_json(payload: Dict[str, Any]) -> str:
    return _canonical_json(normalize_trucking_trip_lifecycle_intent(payload))


def classify_financial_direction(
    *,
    fleet_ownership: str,
    service_beneficiary_kind: str,
) -> Dict[str, Any]:
    ownership = str(fleet_ownership or "").strip()
    beneficiary = str(service_beneficiary_kind or "").strip().upper()

    if ownership == "ENT-Owned":
        if beneficiary == "NKT":
            return {
                "direction": "ENT Receivable",
                "debtor": "NKT",
                "creditor": "ENT",
                "statement_family": "NKT Trucking Customer SOA",
                "external_carrier_payable": False,
                "ent_driver_incentive_applicable": True,
            }
        if beneficiary == "EXTERNAL_CUSTOMER":
            return {
                "direction": "ENT Receivable",
                "debtor": "External Customer",
                "creditor": "ENT",
                "statement_family": "NKT Trucking Customer SOA",
                "external_carrier_payable": False,
                "ent_driver_incentive_applicable": True,
            }
        raise frappe.ValidationError(
            "ENT-owned trucking requires a recognized service beneficiary."
        )

    if ownership == "External Carrier":
        if beneficiary == "SUPPLIER_TO_NKT_EXTERNAL_CARRIER":
            return {
                "direction": "NKT Payable",
                "debtor": "NKT",
                "creditor": "External Carrier",
                "statement_family": "NKT Trucker SOA",
                "external_carrier_payable": True,
                "ent_driver_incentive_applicable": False,
            }
        raise frappe.ValidationError(
            "External Carrier classification is valid only for the recognized carrier-payable context."
        )

    raise frappe.ValidationError("Vehicle must be classified as ENT-Owned or External Carrier.")


def contract_status() -> Dict[str, Any]:
    return {
        "foundation_version": FOUNDATION_VERSION,
        "new_ent_trips_may_start_offline": True,
        "trip_status_physical_events_may_be_recorded_offline": True,
        "true_event_datetime_immutable": True,
        "employee_manual_backdate_allowed": False,
        "ent_incentive_prerequisites_may_be_recorded_offline": True,
        "ent_incentive_weekly_computation_payment_primary_only": True,
        "external_supplier_arrival_physical_truth_reuses_c15c10h": True,
        "duplicate_external_receiving_intent_allowed": False,
        "external_carrier_rates_statements_payments_primary_only": True,
        "ent_customer_rates_statements_collections_primary_only": True,
        "ent_owned_nkt_haul_direction": "ENT Receivable / NKT owes ENT",
        "external_carrier_inbound_direction": "NKT Payable / NKT owes External Carrier",
        "operational_printing_from_cached_edge_snapshot_required": True,
        "waybill_money_authority_offline": False,
        "shared_trip_lifecycle_family_count": 1,
        "primary_materialization_enabled_at_r2": False,
        "shared_transport_registered_at_r2": False,
        "edge_primary_preservation_enabled_at_r3": True,
        "canonical_trip_materialization_enabled_at_r3": False,
        "canonical_trip_materialization_enabled_at_r4": True,
        "container_identifier_preserved_offline": True,
        "container_return_true_event_time_preserved": True,
        "offline_operational_print_snapshot_enabled_at_r4": True,
        "trip_date_locked_to_create_date": True,
        "cross_midnight_physical_events_allowed": True,
        "safe_sync_business_date_is_physical_event_date": True,
        "payload_trip_date_is_immutable_create_date": True,
        "external_carrier_normal_ui_roles": ["NKT OWNER", "NKT ADMINISTRATOR"],
        "employee_external_carrier_browse_allowed": False,
    }
