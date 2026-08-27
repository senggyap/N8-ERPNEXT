from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime
from typing import Any, Dict
from zoneinfo import ZoneInfo

import frappe
from frappe.utils import getdate, now

from nkt_operations.nkt_store_operations.features.inventory.internal.physical_inventory_offline_intent import (
    ACTION,
    FAMILY,
    canonical_physical_inventory_count_intent_json,
    normalize_physical_inventory_count_intent,
)
from nkt_operations.nkt_store_operations.features.offline_edge.safe_sync import (
    NKTIdempotencyConflict,
    mark_primary_committed,
)
from nkt_operations.nkt_store_operations.features.offline_edge.sync_transport import (
    _runtime_role,
    prepare_event_for_primary,
    validate_transport_packet,
)

MATERIALIZER_PATH = (
    "nkt_operations.nkt_store_operations.features.inventory.internal.physical_inventory_materialization."
    "materialize_preserved_physical_inventory_count"
)

FOUNDATION_VERSION = "C15C.10J-R7"
PH_TZ = ZoneInfo("Asia/Manila")

PRIMARY_JOURNAL = "NKT Primary Physical Inventory Count Intent"
PRESERVATION_STATE = "Physical Inventory Count Intent Preserved"
PRIMARY_ACK_NAMESPACE = uuid.UUID("66da0d78-3749-4bc6-8436-f18f21fe59f7")

CANONICAL_PHYSICAL_INVENTORY_ADJUSTMENT_ENABLED = False
AUTO_STOCK_RECONCILIATION_ENABLED = False
STOCK_LEDGER_MUTATION_ENABLED = False
BIN_MUTATION_ENABLED = False
STALE_COUNT_AUTO_POST_DECIDED = False
INTERVENING_MOVEMENT_POLICY_DECIDED = False


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


def _manila_datetime(value: Any, label: str) -> datetime:
    raw = str(value or "").strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        dt = value if isinstance(value, datetime) else datetime.fromisoformat(raw)
    except Exception as exc:
        raise frappe.ValidationError(f"{label} is not a valid datetime.") from exc
    if dt.tzinfo is None:
        return dt.replace(tzinfo=PH_TZ)
    return dt.astimezone(PH_TZ)


def _require_primary() -> None:
    if _runtime_role() != "Primary":
        raise frappe.PermissionError("Physical Inventory Primary receiver unavailable.")


def _expected_primary_ack_uuid(event_uuid: str, payload_hash: str) -> str:
    event_uuid = _uuid(event_uuid, "Event UUID")
    material = FAMILY + "\0" + event_uuid + "\0" + str(payload_hash or "").lower()
    return str(uuid.uuid5(PRIMARY_ACK_NAMESPACE, material))


def _claim_name(event_uuid: str) -> str:
    return "nkt-10j-physical-inventory-" + hashlib.sha256(
        str(event_uuid).encode("utf-8")
    ).hexdigest()[:28]


def _acquire_claim(event_uuid: str) -> str:
    name = _claim_name(event_uuid)
    rows = frappe.db.sql("SELECT GET_LOCK(%s,%s)", (name, 30), as_list=True)
    if not rows or int(rows[0][0] or 0) != 1:
        raise frappe.ValidationError(
            "Physical Inventory Primary preservation is busy. Safe retry is required."
        )
    return name


def _release_claim(name: str) -> None:
    try:
        frappe.db.sql("SELECT RELEASE_LOCK(%s)", (name,))
    except Exception:
        pass


def _enqueue_materialization_after_commit(event_uuid: str) -> None:
    """Best-effort automatic current-date materialization after preservation.

    Preservation remains authoritative even if the queue is temporarily
    unavailable; Support/Admin can safely retry materialization by Event UUID.
    """
    try:
        frappe.enqueue(
            MATERIALIZER_PATH,
            event_uuid=event_uuid,
            queue="short",
            enqueue_after_commit=True,
            job_id="nkt-c15c10j-materialize-" + str(event_uuid),
            deduplicate=True,
        )
    except TypeError:
        # Compatibility fallback when a Frappe worker lacks deduplicate support.
        frappe.enqueue(
            MATERIALIZER_PATH,
            event_uuid=event_uuid,
            queue="short",
            enqueue_after_commit=True,
            job_id="nkt-c15c10j-materialize-" + str(event_uuid),
        )


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


def validate_primary_physical_inventory_count_contract(packet: Dict[str, Any]) -> Dict[str, Any]:
    envelope, raw_payload, envelope_hash, payload_hash = validate_transport_packet(
        packet,
        expected_family=FAMILY,
    )
    payload = normalize_physical_inventory_count_intent(raw_payload)

    if envelope.get("event_action") != ACTION:
        raise frappe.ValidationError("Physical Inventory Primary contract action is invalid.")

    physical_date = getdate(envelope.get("business_date"))
    settled_at = _manila_datetime(envelope.get("settled_at"), "Physical count settled time")
    count_datetime = _manila_datetime(payload.get("count_datetime"), "Physical count time")

    if getdate(payload.get("business_date")) != physical_date:
        raise frappe.ValidationError(
            "Physical Inventory Business Date must match the immutable Store-Edge business date."
        )
    if settled_at.date() != physical_date or count_datetime.date() != physical_date:
        raise frappe.ValidationError(
            "Physical Inventory count time must remain on the immutable physical business date."
        )
    if abs((settled_at - count_datetime).total_seconds()) > 0.001:
        raise frappe.ValidationError(
            "Physical Inventory event settled time must equal the immutable physical count time."
        )

    origin_user = str(envelope.get("origin_user") or "").strip()
    if origin_user != str(payload.get("counted_by") or "").strip():
        raise frappe.ValidationError(
            "Physical Inventory counted user must match the immutable event origin user."
        )

    if not frappe.db.exists("User", origin_user):
        raise frappe.DoesNotExistError("Physical Inventory origin user is unavailable at Primary.")
    if payload["entry_role"] not in set(frappe.get_roles(origin_user) or []):
        raise frappe.PermissionError(
            "Physical Inventory Entry Role is not assigned to the immutable origin user."
        )

    company = frappe.db.get_value("Company", payload["company"], "name")
    if not company:
        raise frappe.DoesNotExistError("Physical Inventory Company is unavailable at Primary.")

    warehouse = frappe.db.get_value(
        "Warehouse",
        payload["warehouse"],
        ["name", "company", "is_group", "disabled"],
        as_dict=True,
    )
    if not warehouse:
        raise frappe.DoesNotExistError("Physical Inventory Warehouse is unavailable at Primary.")
    if str(warehouse.company or "") != payload["company"]:
        raise frappe.ValidationError("Physical Inventory Warehouse Company conflicts.")
    if int(warehouse.is_group or 0) or int(warehouse.disabled or 0):
        raise frappe.ValidationError("Physical Inventory Warehouse must be an active leaf Warehouse.")

    missing_items = [
        row["item_code"]
        for row in payload["items"]
        if not frappe.db.exists("Item", row["item_code"])
    ]
    if missing_items:
        raise frappe.DoesNotExistError(
            "Physical Inventory counted Item is unavailable at Primary: "
            + ", ".join(sorted(set(missing_items)))
        )

    return {
        "event_uuid": envelope["event_uuid"],
        "event_family": FAMILY,
        "event_action": ACTION,
        "payload_sha256": payload_hash,
        "envelope_sha256": envelope_hash,
        "company": payload["company"],
        "warehouse": payload["warehouse"],
        "business_date": physical_date.isoformat(),
        "count_datetime_manila": count_datetime.isoformat(),
        "counted_by": payload["counted_by"],
        "entry_role": payload["entry_role"],
        "item_count": len(payload["items"]),
        "canonical_physical_inventory_adjustment_enabled": False,
        "auto_stock_reconciliation_enabled": False,
        "stock_ledger_mutation_enabled": False,
        "bin_mutation_enabled": False,
        "stale_count_auto_post_decided": False,
        "intervening_movement_policy_decided": False,
    }


def _preservation_ack(receipt, journal, *, replay: bool) -> Dict[str, Any]:
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
        "business_date": str(journal.business_date),
        "count_datetime": str(journal.count_datetime),
        "company": journal.company,
        "warehouse": journal.warehouse,
        "canonical_physical_inventory_adjustment_created": False,
        "stock_reconciliation_created": False,
        "stock_ledger_mutated": False,
        "bin_mutated": False,
        "requires_primary_reconciliation": True,
    }


def prepare_physical_inventory_count_for_primary(event_uuid: str) -> Dict[str, Any]:
    return prepare_event_for_primary(event_uuid, expected_family=FAMILY)


def receive_physical_inventory_count_at_primary(packet: Dict[str, Any]) -> Dict[str, Any]:
    _require_primary()
    validate_primary_physical_inventory_count_contract(packet)
    envelope, raw_payload, envelope_hash, payload_hash = validate_transport_packet(
        packet,
        expected_family=FAMILY,
    )
    payload = normalize_physical_inventory_count_intent(raw_payload)
    event_uuid = envelope["event_uuid"]
    expected_ack = _expected_primary_ack_uuid(event_uuid, payload_hash)

    lock = _acquire_claim(event_uuid)
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
                or receipt.materialization_state != PRESERVATION_STATE
                or receipt.result_code != "Committed"
            ):
                raise NKTIdempotencyConflict(
                    "Physical Inventory Primary receipt conflicts with immutable content."
                )
            journal = _journal_for_update(event_uuid)
            if not journal:
                raise NKTIdempotencyConflict(
                    "Physical Inventory Primary receipt exists without preserved count journal."
                )
            if (
                str(journal.payload_sha256 or "").lower() != payload_hash
                or str(journal.envelope_sha256 or "").lower() != envelope_hash
                or str(journal.canonical_payload_json or "")
                != canonical_physical_inventory_count_intent_json(payload)
                or str(journal.canonical_envelope_json or "") != _canonical_json(envelope)
            ):
                raise NKTIdempotencyConflict(
                    "Preserved Physical Inventory journal conflicts with immutable content."
                )
            if journal.downstream_state == "Awaiting Physical Inventory Reconciliation":
                _enqueue_materialization_after_commit(event_uuid)
            return _preservation_ack(receipt, journal, replay=True)

        # Re-run all Primary identity checks under the event claim before insert.
        validate_primary_physical_inventory_count_contract(packet)

        journal = frappe.get_doc(
            {
                "doctype": PRIMARY_JOURNAL,
                "event_uuid": event_uuid,
                "event_family": FAMILY,
                "event_action": ACTION,
                "submit_request_id": payload["submit_request_id"],
                "origin_device": envelope["origin_device"],
                "origin_user": envelope["origin_user"],
                "operational_context": envelope["operational_context"],
                "company": payload["company"],
                "warehouse": payload["warehouse"],
                "business_date": envelope["business_date"],
                "settled_at": envelope["settled_at"],
                "count_datetime": _manila_datetime(payload["count_datetime"], "Physical count time").replace(tzinfo=None),
                "client_created_at": envelope.get("client_created_at"),
                "counted_by": payload["counted_by"],
                "entry_role": payload["entry_role"],
                "count_reason": payload["count_reason"],
                "physical_count_reference": payload.get("physical_count_reference"),
                "operator_notes": payload.get("operator_notes"),
                "item_count": len(payload["items"]),
                "envelope_sha256": envelope_hash,
                "payload_sha256": payload_hash,
                "canonical_envelope_json": _canonical_json(envelope),
                "canonical_payload_json": canonical_physical_inventory_count_intent_json(payload),
                "preservation_state": "Preserved",
                "downstream_state": "Awaiting Physical Inventory Reconciliation",
                "primary_ack_uuid": expected_ack,
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
                "primary_received_at": now(),
                "primary_committed_at": now(),
                "result_code": "Committed",
                "canonical_doctype": PRIMARY_JOURNAL,
                "canonical_name": event_uuid,
                "materialization_state": PRESERVATION_STATE,
            }
        )
        receipt.insert(ignore_permissions=True)

        mark_primary_committed(
            event_uuid,
            PRIMARY_JOURNAL,
            event_uuid,
            primary_ack_uuid=expected_ack,
        )
        _enqueue_materialization_after_commit(event_uuid)
        return _preservation_ack(receipt, journal, replay=False)
    finally:
        _release_claim(lock)


def apply_physical_inventory_preservation_ack_at_edge(ack: Dict[str, Any]) -> Dict[str, Any]:
    if _runtime_role() != "Store Edge":
        raise frappe.PermissionError("Physical Inventory preservation ACK unavailable.")
    if not isinstance(ack, dict):
        raise frappe.ValidationError("Physical Inventory preservation ACK is invalid.")

    event_uuid = _uuid(ack.get("event_uuid"), "Event UUID")
    ack_uuid = _uuid(ack.get("primary_ack_uuid"), "Primary ACK UUID")
    payload_hash = str(ack.get("payload_sha256") or "").lower()

    if (
        ack.get("committed") is not True
        or ack.get("result_code") != "Committed"
        or ack.get("materialization_state") != PRESERVATION_STATE
        or ack.get("canonical_doctype") != PRIMARY_JOURNAL
        or ack.get("canonical_name") != event_uuid
        or ack.get("stock_reconciliation_created") is not False
        or ack.get("stock_ledger_mutated") is not False
        or ack.get("bin_mutated") is not False
    ):
        raise frappe.ValidationError(
            "Physical Inventory preservation ACK is not a preservation-only Primary ACK."
        )

    if not frappe.db.exists("NKT Sync Event", event_uuid):
        raise frappe.DoesNotExistError("Physical Inventory event is unavailable at Edge.")

    event = frappe.get_doc("NKT Sync Event", event_uuid)
    if (
        event.event_family != FAMILY
        or str(event.payload_sha256 or "").lower() != payload_hash
    ):
        raise NKTIdempotencyConflict(
            "Physical Inventory preservation ACK conflicts with immutable Edge event."
        )

    bound = str(event.primary_ack_uuid or "").strip()
    if bound and bound != ack_uuid:
        raise NKTIdempotencyConflict(
            "Physical Inventory preservation ACK UUID conflicts with the ACK already bound."
        )

    pending = frappe.db.exists("NKT Sync Pending Payload", event_uuid)
    if event.sync_state in ("Accepted at Edge", "Awaiting Primary"):
        mark_primary_committed(
            event_uuid,
            PRIMARY_JOURNAL,
            event_uuid,
            primary_ack_uuid=ack_uuid,
        )
    elif event.sync_state == "Committed at Primary":
        if (
            str(event.canonical_doctype or "") != PRIMARY_JOURNAL
            or str(event.canonical_name or "") != event_uuid
            or str(event.primary_ack_uuid or "") != ack_uuid
        ):
            raise NKTIdempotencyConflict(
                "Physical Inventory event is committed with different Primary bindings."
            )
    else:
        raise frappe.ValidationError(
            "Physical Inventory event is not eligible for Primary preservation ACK."
        )

    if pending:
        pd = frappe.get_doc("NKT Sync Pending Payload", pending)
        if str(pd.payload_sha256 or "").lower() != payload_hash:
            raise NKTIdempotencyConflict(
                "Physical Inventory preservation ACK conflicts with pending payload."
            )
        frappe.delete_doc(
            "NKT Sync Pending Payload",
            pd.name,
            ignore_permissions=True,
            force=True,
        )

    return {
        "event_uuid": event_uuid,
        "event_family": FAMILY,
        "primary_ack_uuid": ack_uuid,
        "sync_state": "Committed at Primary",
        "pending_payload_purged": bool(pending),
        "replay": not bool(pending),
        "canonical_physical_inventory_adjustment_created": False,
        "stock_reconciliation_created": False,
    }


def contract_status() -> Dict[str, Any]:
    return {
        "foundation_version": FOUNDATION_VERSION,
        "event_family": FAMILY,
        "event_action": ACTION,
        "transport_registered": True,
        "generic_primary_receipt_allowed": False,
        "primary_journal": PRIMARY_JOURNAL,
        "primary_preservation_state": PRESERVATION_STATE,
        "canonical_physical_inventory_adjustment_enabled": False,
        "auto_stock_reconciliation_enabled": False,
        "stock_ledger_mutation_enabled": False,
        "bin_mutation_enabled": False,
        "stale_count_auto_post_decided": False,
        "intervening_movement_policy_decided": False,
        "current_stock_rebase_required_before_future_materialization": True,
    }


@frappe.whitelist()
def receive_physical_inventory_count(packet):
    if isinstance(packet, str):
        packet = frappe.parse_json(packet)
    return receive_physical_inventory_count_at_primary(packet)


@frappe.whitelist()
def apply_physical_inventory_preservation_ack(ack):
    if isinstance(ack, str):
        ack = frappe.parse_json(ack)
    return apply_physical_inventory_preservation_ack_at_edge(ack)
