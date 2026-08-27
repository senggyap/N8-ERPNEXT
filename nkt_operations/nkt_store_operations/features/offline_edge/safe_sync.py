from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any, Dict, Tuple

import frappe
from frappe.utils import getdate, get_datetime, now

FOUNDATION_VERSION = "C15C.1-R1"
IDEMPOTENCY_SCHEMA_VERSION = 1

# Controlling safety gate: this foundation does NOT enable offline writes for
# Payment Receipt, Cashier Movement, Warehouse Release, collections, trucking
# payments, incentives, or any other critical business family.
CRITICAL_OFFLINE_MUTATIONS_ENABLED = False

IMMUTABLE_IDENTITY_FIELDS = (
    "event_family",
    "event_action",
    "operational_context",
    "origin_device",
    "origin_user",
    "business_date",
    "settled_at",
    "client_created_at",
    "payload_sha256",
    "legacy_request_id",
)


class NKTIdempotencyConflict(frappe.ValidationError):
    pass


def canonical_payload_hash(payload: Any) -> str:
    """Deterministic integrity hash. This is not encryption or key management."""
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _normalized_uuid(value: str) -> str:
    try:
        return str(uuid.UUID(str(value)))
    except Exception as exc:
        raise frappe.ValidationError("Event UUID must be a valid UUID.") from exc


def _normalize_envelope(envelope: Dict[str, Any]) -> Dict[str, Any]:
    required = (
        "event_uuid",
        "event_family",
        "event_action",
        "operational_context",
        "origin_device",
        "origin_user",
        "business_date",
        "settled_at",
        "payload_sha256",
    )
    missing = [key for key in required if envelope.get(key) in (None, "")]
    if missing:
        raise frappe.ValidationError("Missing safe-sync envelope fields: " + ", ".join(missing))

    out = dict(envelope)
    out["event_uuid"] = _normalized_uuid(out["event_uuid"])
    out["payload_sha256"] = str(out["payload_sha256"]).lower()

    if len(out["payload_sha256"]) != 64 or any(c not in "0123456789abcdef" for c in out["payload_sha256"]):
        raise frappe.ValidationError("payload_sha256 must be 64 lowercase hexadecimal characters.")

    settled = get_datetime(out["settled_at"])
    if getdate(out["business_date"]) != settled.date():
        raise frappe.ValidationError(
            "Business Date must equal the local Asia/Manila date of Business / Settled Time."
        )

    if not frappe.db.exists("NKT Device Registry", out["origin_device"]):
        raise frappe.ValidationError("Origin device is not registered.")

    device_status = frappe.db.get_value("NKT Device Registry", out["origin_device"], "status")
    if device_status in ("Revoked", "Lost/Stolen", "Retired"):
        raise frappe.PermissionError("Origin device is not authorized to submit events.")

    return out


def _same_identity(existing, envelope: Dict[str, Any]) -> Tuple[bool, list[str]]:
    mismatches = []
    for field in IMMUTABLE_IDENTITY_FIELDS:
        left = existing.get(field)
        right = envelope.get(field)
        if field == "settled_at":
            left = str(get_datetime(left))
            right = str(get_datetime(right))
        elif field == "business_date":
            left = str(getdate(left))
            right = str(getdate(right))
        else:
            left = "" if left is None else str(left)
            right = "" if right is None else str(right)
        if left != right:
            mismatches.append(field)
    return (not mismatches, mismatches)


def begin_event(envelope: Dict[str, Any]):
    """
    Reserve or replay a permanent event UUID.

    Caller owns the surrounding DB transaction. This function never commits.
    Reusing the same UUID with the same immutable identity returns the existing
    journal event; reusing it with different business identity is rejected.
    """
    env = _normalize_envelope(envelope)
    name = env["event_uuid"]

    existing_name = frappe.db.exists("NKT Sync Event", name)
    if existing_name:
        existing = frappe.get_doc("NKT Sync Event", existing_name)
        same, mismatches = _same_identity(existing, env)
        if not same:
            raise NKTIdempotencyConflict(
                "Event UUID was reused with different immutable content: " + ", ".join(mismatches)
            )
        frappe.db.set_value(
            "NKT Sync Event",
            existing.name,
            {"retry_count": int(existing.retry_count or 0) + 1, "last_retry_at": now()},
            update_modified=False,
        )
        existing.reload()
        return existing, True

    doc = frappe.get_doc({
        "doctype": "NKT Sync Event",
        "event_uuid": name,
        "event_family": env["event_family"],
        "event_action": env["event_action"],
        "operational_context": env["operational_context"],
        "origin_device": env["origin_device"],
        "origin_user": env["origin_user"],
        "business_date": env["business_date"],
        "settled_at": env["settled_at"],
        "client_created_at": env.get("client_created_at"),
        "payload_sha256": env["payload_sha256"],
        "legacy_request_id": env.get("legacy_request_id"),
        "sync_state": "Received",
    })

    try:
        doc.insert(ignore_permissions=True)
        return doc, False
    except frappe.DuplicateEntryError:
        # Concurrency-safe recovery: the unique event_uuid index is the final
        # arbiter if two requests race after the initial existence check.
        existing = frappe.get_doc("NKT Sync Event", name)
        same, mismatches = _same_identity(existing, env)
        if not same:
            raise NKTIdempotencyConflict(
                "Concurrent Event UUID collision with different immutable content: " + ", ".join(mismatches)
            )
        frappe.db.set_value(
            "NKT Sync Event",
            existing.name,
            {"retry_count": int(existing.retry_count or 0) + 1, "last_retry_at": now()},
            update_modified=False,
        )
        existing.reload()
        return existing, True


def mark_edge_accepted(event_uuid: str):
    event_uuid = _normalized_uuid(event_uuid)
    frappe.db.set_value(
        "NKT Sync Event",
        event_uuid,
        {"sync_state": "Accepted at Edge", "edge_accepted_at": now()},
        update_modified=False,
    )


def mark_awaiting_primary(event_uuid: str):
    event_uuid = _normalized_uuid(event_uuid)
    frappe.db.set_value(
        "NKT Sync Event",
        event_uuid,
        {"sync_state": "Awaiting Primary"},
        update_modified=False,
    )


def mark_primary_committed(
    event_uuid: str,
    canonical_doctype: str,
    canonical_name: str,
    primary_ack_uuid: str | None = None,
):
    event_uuid = _normalized_uuid(event_uuid)
    values = {
        "sync_state": "Committed at Primary",
        "primary_committed_at": now(),
        "canonical_doctype": canonical_doctype,
        "canonical_name": canonical_name,
    }
    if primary_ack_uuid:
        values["primary_ack_uuid"] = _normalized_uuid(primary_ack_uuid)
    frappe.db.set_value("NKT Sync Event", event_uuid, values, update_modified=False)


def foundation_status() -> Dict[str, Any]:
    return {
        "foundation_version": FOUNDATION_VERSION,
        "idempotency_schema_version": IDEMPOTENCY_SCHEMA_VERSION,
        "critical_offline_mutations_enabled": CRITICAL_OFFLINE_MUTATIONS_ENABLED,
    }
