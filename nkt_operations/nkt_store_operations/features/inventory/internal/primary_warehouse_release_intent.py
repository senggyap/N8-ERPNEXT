from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any, Dict

import frappe
from frappe.utils import flt, now

from nkt_operations.nkt_store_operations.features.offline_edge.safe_sync import (
    NKTIdempotencyConflict,
    mark_primary_committed,
)
from nkt_operations.nkt_store_operations.features.offline_edge.sync_transport import (
    _runtime_role,
    prepare_event_for_primary,
    validate_transport_packet,
)
from nkt_operations.nkt_store_operations.features.inventory.internal.warehouse_release_intent import (
    WAREHOUSE_RELEASE_INTENT_ACTION,
    WAREHOUSE_RELEASE_INTENT_FAMILY,
    _canonical_warehouse_release_intent_json,
    _normalize_warehouse_release_intent_payload,
)

FOUNDATION_VERSION = "C15C.10E-R2"
PRIMARY_JOURNAL = "NKT Primary Warehouse Release Intent"
MATERIALIZATION_STATE = "Warehouse Release Intent Preserved"
TOLERANCE = 0.000001
PRIMARY_ACK_NAMESPACE = uuid.UUID("e86aa96f-8db7-42c8-9773-b651c0940f98")


def _uuid(value: Any, label: str) -> str:
    try:
        return str(uuid.UUID(str(value)))
    except Exception as exc:
        raise frappe.ValidationError(f"{label} is invalid.") from exc


def _canonical_json(value: Dict[str, Any]) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    )


def _expected_primary_ack_uuid(event_uuid: str, payload_hash: str) -> str:
    event_uuid = _uuid(event_uuid, "Event UUID")
    payload_hash = str(payload_hash or "").lower()
    if len(payload_hash) != 64 or any(c not in "0123456789abcdef" for c in payload_hash):
        raise frappe.ValidationError("Payload hash is invalid.")
    material = (
        WAREHOUSE_RELEASE_INTENT_FAMILY
        + "\0"
        + event_uuid
        + "\0"
        + payload_hash
    )
    return str(uuid.uuid5(PRIMARY_ACK_NAMESPACE, material))


def _require_primary():
    if _runtime_role() != "Primary":
        raise frappe.PermissionError("Warehouse Release Intent Primary receiver unavailable.")


def _claim_name(kind: str, identity: str) -> str:
    return "nkt-10e-release-" + hashlib.sha256(
        f"{kind}:{identity}".encode("utf-8")
    ).hexdigest()[:36]


def _release_lock(name: str):
    try:
        frappe.db.sql("SELECT RELEASE_LOCK(%s)", (name,))
    except Exception:
        pass


def _acquire_claims(event_uuid: str, warehouse_release: str):
    names = sorted(
        {
            _claim_name("event", event_uuid),
            _claim_name("warehouse-release", warehouse_release),
        }
    )
    acquired = []
    try:
        for name in names:
            rows = frappe.db.sql("SELECT GET_LOCK(%s,%s)", (name, 30), as_list=True)
            if not rows or int(rows[0][0] or 0) != 1:
                raise frappe.ValidationError(
                    "Warehouse Release Intent preservation is busy. Safe retry is required."
                )
            acquired.append(name)
    except Exception:
        for name in reversed(acquired):
            _release_lock(name)
        raise

    state = {"released": False}
    def release_once():
        if state["released"]:
            return
        for name in reversed(acquired):
            _release_lock(name)
        state["released"] = True
    frappe.db.after_commit.add(release_once)
    frappe.db.after_rollback.add(release_once)


def _receipt_for_update(event_uuid: str):
    rows = frappe.db.sql(
        "SELECT name FROM `tabNKT Sync Primary Receipt` WHERE name=%s FOR UPDATE",
        (event_uuid,),
        as_dict=True,
    )
    return frappe.get_doc("NKT Sync Primary Receipt", event_uuid) if rows else None


def _journal_for_update(event_uuid: str):
    rows = frappe.db.sql(
        f"SELECT name FROM `tab{PRIMARY_JOURNAL}` WHERE name=%s FOR UPDATE",
        (event_uuid,),
        as_dict=True,
    )
    return frappe.get_doc(PRIMARY_JOURNAL, event_uuid) if rows else None


def _validate_target_identity(payload: Dict[str, Any]) -> Dict[str, Any]:
    release_name = payload["warehouse_release"]
    if not frappe.db.exists("NKT Warehouse Release", release_name):
        raise NKTIdempotencyConflict(
            "Physical release intent references a Warehouse Release that is missing at Primary."
        )
    release = frappe.get_doc("NKT Warehouse Release", release_name)

    exact = {
        "customer_order": release.customer_order,
        "company": release.company,
        "customer": release.customer,
        "source_warehouse": release.get("custom_nkt_source_warehouse"),
    }
    for field, actual in exact.items():
        if str(actual or "") != str(payload[field] or ""):
            raise NKTIdempotencyConflict(
                f"Physical release intent {field} conflicts with Primary Warehouse Release identity."
            )

    release_rows = {row.name: row for row in (release.get("items") or [])}
    seen = set()
    for line in payload["items"]:
        row_name = line["warehouse_release_item"]
        if row_name in seen:
            raise NKTIdempotencyConflict("Physical release intent repeats a Warehouse Release Item.")
        seen.add(row_name)
        row = release_rows.get(row_name)
        if not row:
            raise NKTIdempotencyConflict(
                "Physical release intent row is missing from Primary Warehouse Release."
            )
        checks = {
            "customer_order_item": row.customer_order_item,
            "item_code": row.item,
            "uom": row.uom,
            "source_warehouse": row.source_warehouse,
        }
        for field, actual in checks.items():
            if str(actual or "") != str(line[field] or ""):
                raise NKTIdempotencyConflict(
                    f"Physical release intent row {field} conflicts with Primary identity."
                )

    return {
        "docstatus": int(release.docstatus or 0),
        "release_status": str(release.get("release_status") or ""),
        "fast_request_id": str(release.get("custom_nkt_fast_release_request_id") or ""),
        "linked_stock_entry": str(release.get("custom_nkt_stock_entry") or ""),
    }


def _journal_conflicts(
    journal,
    envelope: Dict[str, Any],
    payload: Dict[str, Any],
    envelope_hash: str,
    payload_hash: str,
) -> list[str]:
    bad = []
    checks = {
        "event_family": WAREHOUSE_RELEASE_INTENT_FAMILY,
        "event_action": WAREHOUSE_RELEASE_INTENT_ACTION,
        "origin_device": envelope.get("origin_device"),
        "origin_user": envelope.get("origin_user"),
        "operational_context": envelope.get("operational_context"),
        "warehouse_release": payload.get("warehouse_release"),
        "customer_order": payload.get("customer_order"),
        "company": payload.get("company"),
        "customer": payload.get("customer"),
        "source_warehouse": payload.get("source_warehouse"),
        "release_reference": payload.get("release_reference"),
        "driver_name": payload.get("driver_name"),
        "plate_number": payload.get("plate_number"),
        "envelope_sha256": envelope_hash,
        "payload_sha256": payload_hash,
        "preservation_state": "Preserved",
    }
    for field, expected in checks.items():
        if str(journal.get(field) or "") != str(expected or ""):
            bad.append(field)
    if abs(flt(journal.total_release_quantity) - flt(payload["total_release_quantity"])) > TOLERANCE:
        bad.append("total_release_quantity")
    if str(journal.canonical_payload_json or "") != _canonical_warehouse_release_intent_json(payload):
        bad.append("canonical_payload_json")
    if str(journal.canonical_envelope_json or "") != _canonical_json(envelope):
        bad.append("canonical_envelope_json")
    return bad


def _ack(receipt, journal, target_state: Dict[str, Any], *, replay: bool) -> Dict[str, Any]:
    return {
        "event_uuid": receipt.name,
        "event_family": WAREHOUSE_RELEASE_INTENT_FAMILY,
        "primary_ack_uuid": receipt.primary_ack_uuid,
        "payload_sha256": receipt.payload_sha256,
        "result_code": receipt.result_code,
        "committed": True,
        "replay": bool(replay),
        "canonical_doctype": PRIMARY_JOURNAL,
        "canonical_name": journal.name,
        "materialization_state": receipt.materialization_state,
        "warehouse_release": journal.warehouse_release,
        "customer_order": journal.customer_order,
        "source_warehouse": journal.source_warehouse,
        "primary_target_docstatus": target_state["docstatus"],
        "primary_target_release_status": target_state["release_status"],
        "primary_target_fast_request_id": target_state["fast_request_id"],
        "primary_target_stock_entry": target_state["linked_stock_entry"],
        "warehouse_release_submitted": False,
        "stock_entry_created": False,
        "edge_projection_must_remain": True,
        "admin_pre_release_approval_required": False,
    }


def prepare_warehouse_release_intent_for_primary(event_uuid: str) -> Dict[str, Any]:
    packet = prepare_event_for_primary(
        event_uuid,
        expected_family=WAREHOUSE_RELEASE_INTENT_FAMILY,
    )
    rows = frappe.get_all(
        "NKT Edge Warehouse Release Projection",
        filters={"event_uuid": event_uuid},
        pluck="name",
        limit_page_length=500,
    )
    if not rows:
        raise frappe.ValidationError(
            "Warehouse Release Intent cannot replicate without its Edge physical-release projection."
        )
    for name in rows:
        frappe.db.set_value(
            "NKT Edge Warehouse Release Projection",
            name,
            "projection_state",
            "Awaiting Primary",
            update_modified=False,
        )
    return packet


def receive_warehouse_release_intent_at_primary(packet: Dict[str, Any]) -> Dict[str, Any]:
    _require_primary()
    envelope, payload, envelope_hash, payload_hash = validate_transport_packet(
        packet,
        expected_family=WAREHOUSE_RELEASE_INTENT_FAMILY,
    )
    event_uuid = envelope["event_uuid"]
    release_name = payload["warehouse_release"]
    expected_ack = _expected_primary_ack_uuid(event_uuid, payload_hash)

    _acquire_claims(event_uuid, release_name)

    receipt = _receipt_for_update(event_uuid)
    if receipt:
        if (
            receipt.event_family != WAREHOUSE_RELEASE_INTENT_FAMILY
            or receipt.primary_ack_uuid != expected_ack
            or receipt.envelope_sha256 != envelope_hash
            or receipt.payload_sha256 != payload_hash
            or receipt.canonical_doctype != PRIMARY_JOURNAL
            or receipt.canonical_name != event_uuid
            or receipt.materialization_state != MATERIALIZATION_STATE
            or receipt.result_code != "Committed"
        ):
            raise NKTIdempotencyConflict(
                "Warehouse Release Intent Primary receipt conflicts with immutable content."
            )
        journal = _journal_for_update(event_uuid)
        if not journal:
            raise NKTIdempotencyConflict(
                "Warehouse Release Intent receipt exists without its preserved journal."
            )
        bad = _journal_conflicts(
            journal, envelope, payload, envelope_hash, payload_hash
        )
        if bad:
            raise NKTIdempotencyConflict(
                "Preserved Warehouse Release Intent conflicts with immutable content: "
                + ", ".join(bad)
            )
        target = _validate_target_identity(payload)
        return _ack(receipt, journal, target, replay=True)

    prior = frappe.db.get_value(
        PRIMARY_JOURNAL,
        {"warehouse_release": release_name, "name": ["!=", event_uuid]},
        "name",
    )
    if prior:
        raise NKTIdempotencyConflict(
            "Warehouse Release is already bound to another immutable physical-release event."
        )

    target = _validate_target_identity(payload)
    journal = frappe.get_doc(
        {
            "doctype": PRIMARY_JOURNAL,
            "event_uuid": event_uuid,
            "event_family": WAREHOUSE_RELEASE_INTENT_FAMILY,
            "event_action": WAREHOUSE_RELEASE_INTENT_ACTION,
            "origin_device": envelope["origin_device"],
            "origin_user": envelope["origin_user"],
            "operational_context": envelope["operational_context"],
            "business_date": envelope["business_date"],
            "settled_at": envelope["settled_at"],
            "client_created_at": envelope.get("client_created_at"),
            "warehouse_release": release_name,
            "customer_order": payload["customer_order"],
            "company": payload["company"],
            "customer": payload["customer"],
            "source_warehouse": payload["source_warehouse"],
            "release_reference": payload["release_reference"],
            "driver_name": payload["driver_name"],
            "plate_number": payload["plate_number"],
            "total_release_quantity": payload["total_release_quantity"],
            "envelope_sha256": envelope_hash,
            "payload_sha256": payload_hash,
            "canonical_envelope_json": _canonical_json(envelope),
            "canonical_payload_json": _canonical_warehouse_release_intent_json(payload),
            "preservation_state": "Preserved",
            "downstream_state": "Awaiting Physical Stock Materialization",
            "primary_preserved_at": now(),
        }
    )
    journal.insert(ignore_permissions=True)

    receipt = frappe.get_doc(
        {
            "doctype": "NKT Sync Primary Receipt",
            "event_uuid": event_uuid,
            "event_family": WAREHOUSE_RELEASE_INTENT_FAMILY,
            "primary_ack_uuid": expected_ack,
            "envelope_sha256": envelope_hash,
            "payload_sha256": payload_hash,
            "canonical_doctype": PRIMARY_JOURNAL,
            "canonical_name": journal.name,
            "materialization_state": MATERIALIZATION_STATE,
            "primary_received_at": now(),
            "primary_committed_at": now(),
            "result_code": "Committed",
        }
    )
    receipt.insert(ignore_permissions=True)
    return _ack(receipt, journal, target, replay=False)


@frappe.whitelist()
def receive_warehouse_release_intent(packet):
    if isinstance(packet, str):
        packet = frappe.parse_json(packet)
    return receive_warehouse_release_intent_at_primary(packet)


def apply_warehouse_release_intent_ack_at_edge(ack: Dict[str, Any]) -> Dict[str, Any]:
    if _runtime_role() != "Store Edge":
        raise frappe.PermissionError("Warehouse Release Intent ACK unavailable.")
    if not isinstance(ack, dict):
        raise frappe.ValidationError("Warehouse Release Intent ACK is invalid.")

    event_uuid = _uuid(ack.get("event_uuid"), "Event UUID")
    ack_uuid = _uuid(ack.get("primary_ack_uuid"), "Primary ACK UUID")
    payload_hash = str(ack.get("payload_sha256") or "").lower()
    if (
        ack.get("event_family") != WAREHOUSE_RELEASE_INTENT_FAMILY
        or ack.get("committed") is not True
        or ack.get("result_code") != "Committed"
        or ack.get("canonical_doctype") != PRIMARY_JOURNAL
        or ack.get("canonical_name") != event_uuid
        or ack.get("materialization_state") != MATERIALIZATION_STATE
    ):
        raise frappe.ValidationError("Warehouse Release Intent ACK is not a committed preservation ACK.")

    expected_ack = _expected_primary_ack_uuid(event_uuid, payload_hash)
    if ack_uuid != expected_ack:
        raise NKTIdempotencyConflict(
            "Warehouse Release Intent ACK UUID conflicts with deterministic immutable binding."
        )

    if not frappe.db.exists("NKT Sync Event", event_uuid):
        raise frappe.DoesNotExistError("Warehouse Release Intent event is unavailable.")
    event = frappe.get_doc("NKT Sync Event", event_uuid)
    if (
        event.event_family != WAREHOUSE_RELEASE_INTENT_FAMILY
        or event.payload_sha256 != payload_hash
    ):
        raise NKTIdempotencyConflict(
            "Warehouse Release Intent ACK conflicts with immutable Edge event."
        )

    projections = frappe.get_all(
        "NKT Edge Warehouse Release Projection",
        filters={"event_uuid": event_uuid},
        fields=["name","projection_state","primary_ack_uuid"],
        order_by="line_no asc",
        limit_page_length=500,
    )
    if not projections:
        raise frappe.ValidationError(
            "Warehouse Release Intent ACK cannot bind without Edge physical-release projection."
        )

    if event.sync_state == "Committed at Primary":
        if (
            event.canonical_doctype == PRIMARY_JOURNAL
            and event.canonical_name == event_uuid
            and str(event.primary_ack_uuid or "") == ack_uuid
            and all(
                row.projection_state == "Primary Preserved"
                and str(row.primary_ack_uuid or "") == ack_uuid
                for row in projections
            )
        ):
            return {
                "event_uuid": event_uuid,
                "primary_ack_uuid": ack_uuid,
                "sync_state": "Committed at Primary",
                "pending_payload_purged": False,
                "edge_projection_retained": True,
                "projection_state": "Primary Preserved",
                "replay": True,
            }
        raise NKTIdempotencyConflict(
            "Committed Warehouse Release Intent conflicts with supplied ACK."
        )

    pending_name = frappe.db.exists("NKT Sync Pending Payload", event_uuid)
    if not pending_name:
        raise frappe.ValidationError(
            "Warehouse Release Intent ACK arrived without its pending payload."
        )
    pending = frappe.get_doc("NKT Sync Pending Payload", pending_name)
    if (
        pending.event_family != WAREHOUSE_RELEASE_INTENT_FAMILY
        or pending.payload_sha256 != payload_hash
    ):
        raise NKTIdempotencyConflict(
            "Warehouse Release Intent ACK conflicts with pending payload."
        )

    mark_primary_committed(
        event_uuid,
        PRIMARY_JOURNAL,
        event_uuid,
        primary_ack_uuid=ack_uuid,
    )
    frappe.delete_doc(
        "NKT Sync Pending Payload",
        pending.name,
        ignore_permissions=True,
        force=True,
    )
    for row in projections:
        frappe.db.set_value(
            "NKT Edge Warehouse Release Projection",
            row.name,
            {
                "projection_state": "Primary Preserved",
                "primary_ack_uuid": ack_uuid,
            },
            update_modified=False,
        )

    return {
        "event_uuid": event_uuid,
        "primary_ack_uuid": ack_uuid,
        "sync_state": "Committed at Primary",
        "pending_payload_purged": True,
        "edge_projection_retained": True,
        "projection_state": "Primary Preserved",
        "replay": False,
    }
