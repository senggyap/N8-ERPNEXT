from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any, Dict

import frappe
from frappe.utils import flt, getdate, now

from nkt_operations.nkt_store_operations.features.offline_edge.safe_sync import (
    NKTIdempotencyConflict,
    mark_primary_committed,
)
from nkt_operations.nkt_store_operations.features.offline_edge.sync_transport import (
    _runtime_role,
    prepare_event_for_primary,
    validate_transport_packet,
)
from nkt_operations.nkt_store_operations.features.inventory.internal.warehouse_transfer_dispatch_intent import (
    ACTION,
    FAMILY,
    TOLERANCE,
    canonical_dispatch_payload_json,
    normalize_dispatch_payload,
)

FOUNDATION_VERSION = "C15C.10G-R6"
PRIMARY_JOURNAL = "NKT Primary Warehouse Transfer Dispatch Intent"
MATERIALIZATION_STATE = "Warehouse Transfer Dispatch Intent Preserved"
PRIMARY_ACK_NAMESPACE = uuid.UUID("761f2523-544d-43fa-9a92-3cbef41484cd")


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
    material = FAMILY + "\0" + event_uuid + "\0" + payload_hash
    return str(uuid.uuid5(PRIMARY_ACK_NAMESPACE, material))


def _require_primary():
    if _runtime_role() != "Primary":
        raise frappe.PermissionError("Transfer Dispatch Intent Primary receiver unavailable.")


def _claim_name(kind: str, identity: str) -> str:
    return "nkt-10g-dispatch-" + hashlib.sha256(
        f"{kind}:{identity}".encode("utf-8")
    ).hexdigest()[:32]


def _release_lock(name: str) -> None:
    try:
        frappe.db.sql("SELECT RELEASE_LOCK(%s)", (name,))
    except Exception:
        pass


def _acquire_claims(event_uuid: str, transfer_name: str) -> list[str]:
    names = [
        _claim_name("event", event_uuid),
        _claim_name("transfer", transfer_name),
    ]
    acquired = []
    try:
        for name in names:
            row = frappe.db.sql("SELECT GET_LOCK(%s, 10)", (name,))
            if not row or int(row[0][0] or 0) != 1:
                raise frappe.ValidationError(
                    "Could not acquire transfer-dispatch preservation lock."
                )
            acquired.append(name)
        return acquired
    except Exception:
        for name in reversed(acquired):
            _release_lock(name)
        raise


def _receipt_for_update(event_uuid: str):
    rows = frappe.db.sql(
        "SELECT name FROM `tabNKT Sync Primary Receipt` WHERE name=%s FOR UPDATE",
        (event_uuid,),
        as_dict=True,
    )
    return frappe.get_doc("NKT Sync Primary Receipt", event_uuid) if rows else None


def _journal_for_update(event_uuid: str):
    rows = frappe.db.sql(
        "SELECT name FROM `tabNKT Primary Warehouse Transfer Dispatch Intent` WHERE name=%s FOR UPDATE",
        (event_uuid,),
        as_dict=True,
    )
    return (
        frappe.get_doc(PRIMARY_JOURNAL, event_uuid)
        if rows
        else None
    )


def _validate_target_identity(payload: Dict[str, Any]) -> Dict[str, Any]:
    payload = normalize_dispatch_payload(payload)
    name = payload["warehouse_transfer"]
    if not frappe.db.exists("NKT Warehouse Transfer", name):
        raise frappe.DoesNotExistError(
            "Internal Warehouse Transfer no longer exists on Primary."
        )
    doc = frappe.get_doc("NKT Warehouse Transfer", name)
    if str(doc.status or "Draft") != "Draft":
        raise frappe.ValidationError(
            "Internal Warehouse Transfer is no longer in Draft source-dispatch state."
        )
    if doc.outgoing_stock_entry or doc.released_by or doc.released_at:
        raise frappe.ValidationError(
            "Internal Warehouse Transfer already contains source-dispatch audit data."
        )

    exact = {
        "company": doc.company,
        "transfer_date": str(getdate(doc.transfer_date)),
        "source_warehouse": doc.source_warehouse,
        "destination_warehouse": doc.destination_warehouse,
        "internal_dr_no": str(doc.internal_dr_no or "").strip(),
    }
    for field, actual in exact.items():
        if str(payload[field] or "") != str(actual or ""):
            raise NKTIdempotencyConflict(
                f"Primary transfer target {field} conflicts with immutable dispatch intent."
            )

    local_rows = list(doc.get("items") or [])
    if len(local_rows) != len(payload["items"]):
        raise NKTIdempotencyConflict(
            "Primary transfer target item count conflicts with immutable dispatch intent."
        )
    for local, incoming in zip(local_rows, payload["items"]):
        if (
            str(local.name) != incoming["warehouse_transfer_item"]
            or str(local.item_code) != incoming["item_code"]
            or str(local.uom) != incoming["uom"]
            or abs(flt(local.requested_qty) - flt(incoming["dispatch_quantity"]))
            > TOLERANCE
        ):
            raise NKTIdempotencyConflict(
                f"Primary transfer target row {local.idx} conflicts with immutable dispatch intent."
            )

    return {
        "transfer": doc.name,
        "status": doc.status,
        "outgoing_stock_entry": doc.outgoing_stock_entry,
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
        "event_family": FAMILY,
        "event_action": ACTION,
        "origin_device": envelope.get("origin_device"),
        "origin_user": envelope.get("origin_user"),
        "operational_context": envelope.get("operational_context"),
        "warehouse_transfer": payload.get("warehouse_transfer"),
        "company": payload.get("company"),
        "source_warehouse": payload.get("source_warehouse"),
        "destination_warehouse": payload.get("destination_warehouse"),
        "internal_dr_no": payload.get("internal_dr_no"),
        "envelope_sha256": envelope_hash,
        "payload_sha256": payload_hash,
        "preservation_state": "Preserved",
    }
    for field, expected in checks.items():
        if str(journal.get(field) or "") != str(expected or ""):
            bad.append(field)
    if str(getdate(journal.transfer_date)) != str(getdate(payload["transfer_date"])):
        bad.append("transfer_date")
    if (
        abs(flt(journal.total_dispatch_quantity) - flt(payload["total_dispatch_quantity"]))
        > TOLERANCE
    ):
        bad.append("total_dispatch_quantity")
    if str(journal.canonical_payload_json or "") != canonical_dispatch_payload_json(payload):
        bad.append("canonical_payload_json")
    if str(journal.canonical_envelope_json or "") != _canonical_json(envelope):
        bad.append("canonical_envelope_json")
    return bad


def _ack(receipt, journal, target: Dict[str, Any], *, replay: bool) -> Dict[str, Any]:
    return {
        "event_uuid": receipt.name,
        "event_family": FAMILY,
        "primary_ack_uuid": receipt.primary_ack_uuid,
        "payload_sha256": receipt.payload_sha256,
        "result_code": receipt.result_code,
        "committed": True,
        "replay": bool(replay),
        "canonical_doctype": PRIMARY_JOURNAL,
        "canonical_name": journal.name,
        "materialization_state": receipt.materialization_state,
        "warehouse_transfer": journal.warehouse_transfer,
        "source_warehouse": journal.source_warehouse,
        "destination_warehouse": journal.destination_warehouse,
        "primary_target_status": target["status"],
        "primary_target_outgoing_stock_entry": target["outgoing_stock_entry"],
        "canonical_transfer_released": False,
        "stock_entry_created": False,
        "edge_projection_must_remain": True,
        "admin_pre_release_approval_required": False,
    }


def prepare_dispatch_intent_for_primary(event_uuid: str) -> Dict[str, Any]:
    packet = prepare_event_for_primary(
        event_uuid,
        expected_family=FAMILY,
    )
    rows = frappe.get_all(
        "NKT Edge Warehouse Transfer Projection",
        filters={
            "event_uuid": event_uuid,
            "projection_action": "Source Dispatch",
        },
        pluck="name",
        limit_page_length=500,
    )
    if not rows:
        raise frappe.ValidationError(
            "Transfer Dispatch Intent cannot replicate without its Edge projection."
        )
    for name in rows:
        frappe.db.set_value(
            "NKT Edge Warehouse Transfer Projection",
            name,
            "projection_state",
            "Awaiting Primary",
            update_modified=False,
        )
    return packet


def receive_dispatch_intent_at_primary(packet: Dict[str, Any]) -> Dict[str, Any]:
    _require_primary()
    envelope, payload, envelope_hash, payload_hash = validate_transport_packet(
        packet,
        expected_family=FAMILY,
    )
    payload = normalize_dispatch_payload(payload)
    event_uuid = envelope["event_uuid"]
    transfer_name = payload["warehouse_transfer"]
    expected_ack = _expected_primary_ack_uuid(event_uuid, payload_hash)

    locks = _acquire_claims(event_uuid, transfer_name)
    try:
        receipt = _receipt_for_update(event_uuid)
        if receipt:
            if (
                receipt.event_family != FAMILY
                or receipt.primary_ack_uuid != expected_ack
                or receipt.envelope_sha256 != envelope_hash
                or receipt.payload_sha256 != payload_hash
                or receipt.canonical_doctype != PRIMARY_JOURNAL
                or receipt.canonical_name != event_uuid
                or receipt.materialization_state != MATERIALIZATION_STATE
                or receipt.result_code != "Committed"
            ):
                raise NKTIdempotencyConflict(
                    "Transfer Dispatch Primary receipt conflicts with immutable content."
                )
            journal = _journal_for_update(event_uuid)
            if not journal:
                raise NKTIdempotencyConflict(
                    "Transfer Dispatch receipt exists without preserved journal."
                )
            bad = _journal_conflicts(
                journal, envelope, payload, envelope_hash, payload_hash
            )
            if bad:
                raise NKTIdempotencyConflict(
                    "Preserved Transfer Dispatch conflicts with immutable content: "
                    + ", ".join(bad)
                )
            target = _validate_target_identity(payload)
            return _ack(receipt, journal, target, replay=True)

        prior = frappe.db.get_value(
            PRIMARY_JOURNAL,
            {
                "warehouse_transfer": transfer_name,
                "name": ["!=", event_uuid],
            },
            "name",
        )
        if prior:
            raise NKTIdempotencyConflict(
                "Internal Warehouse Transfer is already bound to another immutable source-dispatch event."
            )

        target = _validate_target_identity(payload)
        journal = frappe.get_doc(
            {
                "doctype": PRIMARY_JOURNAL,
                "event_uuid": event_uuid,
                "event_family": FAMILY,
                "event_action": ACTION,
                "origin_device": envelope["origin_device"],
                "origin_user": envelope["origin_user"],
                "operational_context": envelope["operational_context"],
                "business_date": envelope["business_date"],
                "settled_at": envelope["settled_at"],
                "client_created_at": envelope.get("client_created_at"),
                "warehouse_transfer": transfer_name,
                "company": payload["company"],
                "transfer_date": payload["transfer_date"],
                "source_warehouse": payload["source_warehouse"],
                "destination_warehouse": payload["destination_warehouse"],
                "internal_dr_no": payload["internal_dr_no"],
                "total_dispatch_quantity": payload["total_dispatch_quantity"],
                "envelope_sha256": envelope_hash,
                "payload_sha256": payload_hash,
                "canonical_envelope_json": _canonical_json(envelope),
                "canonical_payload_json": canonical_dispatch_payload_json(payload),
                "preservation_state": "Preserved",
                "downstream_state": "Awaiting Source Dispatch Materialization",
                "primary_preserved_at": now(),
            }
        )
        journal.insert(ignore_permissions=True)

        receipt = frappe.get_doc(
            {
                "doctype": "NKT Sync Primary Receipt",
                "event_uuid": event_uuid,
                "event_family": FAMILY,
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
    finally:
        for name in reversed(locks):
            _release_lock(name)


@frappe.whitelist()
def receive_dispatch_intent(packet):
    if isinstance(packet, str):
        packet = frappe.parse_json(packet)
    return receive_dispatch_intent_at_primary(packet)


def apply_dispatch_preservation_ack_at_edge(ack: Dict[str, Any]) -> Dict[str, Any]:
    if _runtime_role() != "Store Edge":
        raise frappe.PermissionError("Transfer Dispatch Intent ACK unavailable.")
    if not isinstance(ack, dict):
        raise frappe.ValidationError("Transfer Dispatch Intent ACK is invalid.")

    event_uuid = _uuid(ack.get("event_uuid"), "Event UUID")
    ack_uuid = _uuid(ack.get("primary_ack_uuid"), "Primary ACK UUID")
    payload_hash = str(ack.get("payload_sha256") or "").lower()
    if (
        ack.get("event_family") != FAMILY
        or ack.get("committed") is not True
        or ack.get("result_code") != "Committed"
        or ack.get("canonical_doctype") != PRIMARY_JOURNAL
        or ack.get("canonical_name") != event_uuid
        or ack.get("materialization_state") != MATERIALIZATION_STATE
    ):
        raise frappe.ValidationError(
            "Transfer Dispatch ACK is not a committed preservation ACK."
        )
    if ack_uuid != _expected_primary_ack_uuid(event_uuid, payload_hash):
        raise NKTIdempotencyConflict(
            "Transfer Dispatch ACK UUID conflicts with immutable binding."
        )
    if not frappe.db.exists("NKT Sync Event", event_uuid):
        raise frappe.DoesNotExistError("Transfer Dispatch event is unavailable.")

    event = frappe.get_doc("NKT Sync Event", event_uuid)
    if event.event_family != FAMILY or event.payload_sha256 != payload_hash:
        raise NKTIdempotencyConflict(
            "Transfer Dispatch ACK conflicts with immutable Edge event."
        )

    projections = frappe.get_all(
        "NKT Edge Warehouse Transfer Projection",
        filters={
            "event_uuid": event_uuid,
            "projection_action": "Source Dispatch",
        },
        fields=["name", "projection_state", "primary_ack_uuid"],
        limit_page_length=500,
    )
    if not projections:
        raise frappe.ValidationError("Transfer Dispatch projection is unavailable.")

    for row in projections:
        if row.primary_ack_uuid and row.primary_ack_uuid != ack_uuid:
            raise NKTIdempotencyConflict(
                "Transfer Dispatch projection is already bound to another ACK."
            )
        if row.projection_state == "Finalized":
            raise frappe.ValidationError(
                "Finalized Transfer Dispatch projection cannot accept preservation ACK."
            )
        if row.projection_state not in (
            "Pending Edge",
            "Awaiting Primary",
            "Primary Preserved",
        ):
            raise frappe.ValidationError(
                "Transfer Dispatch projection is not in a preservation-ACK state."
            )

    for row in projections:
        frappe.db.set_value(
            "NKT Edge Warehouse Transfer Projection",
            row.name,
            {
                "projection_state": "Primary Preserved",
                "primary_ack_uuid": ack_uuid,
            },
            update_modified=False,
        )

    pending = frappe.db.exists("NKT Sync Pending Payload", event_uuid)
    if pending:
        frappe.delete_doc(
            "NKT Sync Pending Payload",
            pending,
            ignore_permissions=True,
            force=True,
        )

    mark_primary_committed(
        event_uuid,
        PRIMARY_JOURNAL,
        event_uuid,
        primary_ack_uuid=ack_uuid,
    )
    return {
        "event_uuid": event_uuid,
        "primary_ack_uuid": ack_uuid,
        "projection_state": "Primary Preserved",
        "pending_payload_purged": not bool(
            frappe.db.exists("NKT Sync Pending Payload", event_uuid)
        ),
        "edge_projection_must_remain": True,
        "canonical_transfer_released": False,
        "stock_entry_created": False,
    }


@frappe.whitelist()
def apply_dispatch_preservation_ack(ack):
    if isinstance(ack, str):
        ack = frappe.parse_json(ack)
    return apply_dispatch_preservation_ack_at_edge(ack)
