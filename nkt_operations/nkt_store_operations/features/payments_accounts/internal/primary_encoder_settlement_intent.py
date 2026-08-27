from __future__ import annotations

import json
import uuid
from typing import Any, Dict

import frappe
from frappe.utils import flt, getdate, now

from nkt_operations.nkt_store_operations.features.sales.customer_order_intent import (
    ORDER_INTENT_FAMILY,
)
from nkt_operations.nkt_store_operations.features.payments_accounts.internal.encoder_settlement_intent import (
    ENCODER_SETTLEMENT_ACTION,
    ENCODER_SETTLEMENT_FAMILY,
    _canonical_encoder_settlement_intent_json,
    _normalize_encoder_settlement_intent_payload,
)
from nkt_operations.nkt_store_operations.features.offline_edge.safe_sync import (
    NKTIdempotencyConflict,
    mark_primary_committed,
)
from nkt_operations.nkt_store_operations.features.offline_edge.sync_transport import (
    _runtime_role,
    _uuid,
    prepare_event_for_primary,
    validate_transport_packet,
)

FOUNDATION_VERSION = "C15C.10D-R2"
PRIMARY_JOURNAL = "NKT Primary Encoder Settlement Intent"
MATERIALIZATION_STATE = "Encoder Settlement Intent Preserved"
TOLERANCE = 0.005
PRIMARY_ACK_NAMESPACE = uuid.UUID("f4dfb0aa-11f2-4f66-b6cb-7dfce0ce7a10")


def _require_primary() -> None:
    if _runtime_role() != "Primary":
        raise frappe.PermissionError("Encoder Settlement Primary materializer unavailable.")


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
    payload_hash = str(payload_hash or "").strip().lower()
    if len(payload_hash) != 64 or any(ch not in "0123456789abcdef" for ch in payload_hash):
        raise frappe.ValidationError("Encoder Settlement payload hash is invalid for ACK binding.")
    material = ENCODER_SETTLEMENT_FAMILY + "\0" + event_uuid + "\0" + payload_hash
    return str(uuid.uuid5(PRIMARY_ACK_NAMESPACE, material))


def _lock_name(kind: str, identity: str) -> str:
    material = f"{kind}:{identity}"
    import hashlib
    return "nkt-10d-" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:40]


def _release_lock(lock_name: str) -> None:
    try:
        frappe.db.sql("SELECT RELEASE_LOCK(%s)", (lock_name,))
    except Exception:
        pass


def _acquire_named_lock(lock_name: str, busy_message: str) -> None:
    rows = frappe.db.sql("SELECT GET_LOCK(%s,%s)", (lock_name, 30), as_list=True)
    if not rows or int(rows[0][0] or 0) != 1:
        raise frappe.ValidationError(busy_message)
    state = {"released": False}

    def release_once():
        if state["released"]:
            return
        _release_lock(lock_name)
        state["released"] = True

    frappe.db.after_commit.add(release_once)
    frappe.db.after_rollback.add(release_once)


def _acquire_claims(event_uuid: str, order_event_uuid: str) -> None:
    names = sorted(
        {
            _lock_name("settlement-event", event_uuid),
            _lock_name("order-settlement", order_event_uuid),
        }
    )
    acquired = []
    try:
        for name in names:
            rows = frappe.db.sql("SELECT GET_LOCK(%s,%s)", (name, 30), as_list=True)
            if not rows or int(rows[0][0] or 0) != 1:
                raise frappe.ValidationError(
                    "Encoder settlement sync is busy. Safe retry is required."
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
        """
        SELECT
            name,event_uuid,event_family,primary_ack_uuid,envelope_sha256,payload_sha256,
            canonical_doctype,canonical_name,materialization_state,
            primary_received_at,primary_committed_at,result_code
        FROM `tabNKT Sync Primary Receipt`
        WHERE name=%s
        FOR UPDATE
        """,
        (event_uuid,),
        as_dict=True,
    )
    return rows[0] if rows else None


def _journal_for_update(event_uuid: str):
    rows = frappe.db.sql(
        f"""
        SELECT *
        FROM `tab{PRIMARY_JOURNAL}`
        WHERE name=%s
        FOR UPDATE
        """,
        (event_uuid,),
        as_dict=True,
    )
    return rows[0] if rows else None


def _order_receipt_for_update(order_event_uuid: str):
    rows = frappe.db.sql(
        """
        SELECT
            name,event_family,canonical_doctype,canonical_name,materialization_state,
            payload_sha256,result_code
        FROM `tabNKT Sync Primary Receipt`
        WHERE name=%s
        FOR UPDATE
        """,
        (order_event_uuid,),
        as_dict=True,
    )
    return rows[0] if rows else None


def _payment_row_raw(row) -> Dict[str, Any]:
    return {
        "payment_method": row.get("payment_method"),
        "amount": row.get("amount"),
        "cash_tendered": row.get("cash_tendered"),
        "change_amount": 0,
        "reference_number": row.get("reference_number"),
        "bank_or_provider": row.get("bank_or_provider"),
        "check_number": row.get("custom_nkt_check_number") or row.get("reference_number"),
        "check_date": row.get("custom_nkt_check_date"),
        "remarks": row.get("remarks"),
        "card_surcharge": row.get("card_surcharge"),
        "collected_amount": row.get("collected_amount"),
    }


def _assert_draft_matches(order, payload: Dict[str, Any], envelope: Dict[str, Any]) -> None:
    if int(order.docstatus or 0) != 0:
        raise NKTIdempotencyConflict(
            "Encoder Settlement Intent is bound to a non-Draft Customer Order."
        )
    if str(order.get("custom_nkt_fast_request_id") or "") != payload["order_event_uuid"]:
        raise NKTIdempotencyConflict(
            "Customer Order Draft is not bound to the referenced immutable order event."
        )
    if str(order.company or "") != payload["company"]:
        raise NKTIdempotencyConflict("Customer Order Company conflicts with Encoder settlement.")
    if str(order.customer or "") != payload["customer"]:
        raise NKTIdempotencyConflict("Customer Order Customer conflicts with Encoder settlement.")
    if str(order.encoder or "") != str(envelope.get("origin_user") or ""):
        raise NKTIdempotencyConflict("Customer Order Encoder identity conflicts with Encoder settlement.")
    if getdate(order.order_date) != getdate(envelope["business_date"]):
        raise NKTIdempotencyConflict("Customer Order Business Date conflicts with Encoder settlement.")
    if abs(flt(order.grand_total) - flt(payload["merchandise_total"])) > 0.01:
        raise NKTIdempotencyConflict(
            "Customer Order merchandise total conflicts with Encoder settlement."
        )

    rows = list(order.get("declared_payments") or [])
    if len(rows) != len(payload["payments"]):
        raise NKTIdempotencyConflict(
            "Customer Order declared-payment row count conflicts with Encoder settlement."
        )
    from nkt_operations.nkt_store_operations.features.payments_accounts.internal.encoder_settlement_intent import (
        _normalize_payment,
    )
    canonical = [
        _normalize_payment(_payment_row_raw(row), idx)
        for idx, row in enumerate(rows, start=1)
    ]
    if canonical != payload["payments"]:
        raise NKTIdempotencyConflict(
            "Customer Order declared payments conflict with immutable Encoder settlement."
        )
    if abs(flt(order.declared_payment_total) - flt(payload["declared_payment_total"])) > 0.01:
        raise NKTIdempotencyConflict("Customer Order declared total conflicts with Encoder settlement.")
    if abs(flt(order.declared_account) - flt(payload["declared_account"])) > 0.01:
        raise NKTIdempotencyConflict("Customer Order Account principal conflicts with Encoder settlement.")
    if order.meta.has_field("declared_card_surcharge_total"):
        if abs(flt(order.declared_card_surcharge_total) - flt(payload["declared_card_surcharge_total"])) > 0.01:
            raise NKTIdempotencyConflict("Customer Order Card surcharge conflicts with Encoder settlement.")
    if order.meta.has_field("declared_total_collected"):
        if abs(flt(order.declared_total_collected) - flt(payload["declared_total_collected"])) > 0.01:
            raise NKTIdempotencyConflict("Customer Order collected total conflicts with Encoder settlement.")


def _append_declared_payments(order, payload: Dict[str, Any]) -> None:
    declared_meta = frappe.get_meta("NKT Declared Payment")
    for row in payload["payments"]:
        data = {
            "payment_method": row["payment_method"],
            "amount": row["amount"],
            "reference_number": row.get("reference_number"),
            "bank_or_provider": row.get("bank_or_provider"),
            "remarks": row.get("remarks"),
        }
        if declared_meta.has_field("cash_tendered"):
            data["cash_tendered"] = row.get("cash_tendered")
        if declared_meta.has_field("card_surcharge"):
            data["card_surcharge"] = row.get("card_surcharge")
        if declared_meta.has_field("collected_amount"):
            data["collected_amount"] = row.get("collected_amount")
        if declared_meta.has_field("custom_nkt_check_date") and row.get("check_date"):
            data["custom_nkt_check_date"] = row.get("check_date")
        if declared_meta.has_field("custom_nkt_check_number") and row.get("check_number"):
            data["custom_nkt_check_number"] = row.get("check_number")
        order.append("declared_payments", data)


def _hydrate_order_draft(order_name: str, payload: Dict[str, Any], envelope: Dict[str, Any]):
    order = frappe.get_doc("NKT Customer Order", order_name)
    if order.get("custom_nkt_customer_receivable") or frappe.db.exists(
        "NKT Customer Receivable", {"customer_order": order.name}
    ):
        raise NKTIdempotencyConflict(
            "Encoder settlement foundation refuses a Customer Order that already has a Receivable."
        )

    if order.get("declared_payments"):
        _assert_draft_matches(order, payload, envelope)
        return order, True

    _append_declared_payments(order, payload)
    order.flags.nkt_c15c_preserve_offline_encoder = True
    try:
        order.save(ignore_permissions=True)
    finally:
        order.flags.nkt_c15c_preserve_offline_encoder = False
    order.reload()
    _assert_draft_matches(order, payload, envelope)

    if order.get("custom_nkt_customer_receivable") or frappe.db.exists(
        "NKT Customer Receivable", {"customer_order": order.name}
    ):
        raise NKTIdempotencyConflict(
            "Encoder settlement Draft hydration unexpectedly created a Receivable."
        )
    return order, False


def _validate_order_binding(order_event_uuid: str, payload: Dict[str, Any], envelope: Dict[str, Any]):
    receipt = _order_receipt_for_update(order_event_uuid)
    if not receipt:
        raise frappe.DoesNotExistError(
            "Encoder settlement cannot materialize before its Customer Order Intent reaches Primary."
        )
    if receipt.event_family != ORDER_INTENT_FAMILY:
        raise NKTIdempotencyConflict("Referenced order receipt has the wrong event family.")
    if receipt.canonical_doctype != "NKT Customer Order":
        raise NKTIdempotencyConflict("Referenced order receipt has the wrong canonical DocType.")
    if receipt.materialization_state != "Canonical Draft Materialized":
        raise NKTIdempotencyConflict(
            "Referenced Customer Order Intent is not a canonical Draft materialization."
        )
    if receipt.result_code != "Committed" or not str(receipt.canonical_name or "").strip():
        raise NKTIdempotencyConflict("Referenced Customer Order receipt is incomplete.")

    order_rows = frappe.db.sql(
        """
        SELECT name,docstatus,custom_nkt_fast_request_id
        FROM `tabNKT Customer Order`
        WHERE name=%s
        FOR UPDATE
        """,
        (receipt.canonical_name,),
        as_dict=True,
    )
    if not order_rows:
        raise NKTIdempotencyConflict("Referenced canonical Customer Order Draft is missing.")
    if int(order_rows[0].docstatus or 0) != 0:
        raise NKTIdempotencyConflict(
            "Encoder Settlement Intent cannot hydrate a submitted/cancelled Customer Order."
        )
    if str(order_rows[0].custom_nkt_fast_request_id or "") != order_event_uuid:
        raise NKTIdempotencyConflict(
            "Referenced Customer Order Draft is not bound to the immutable order event."
        )

    order = frappe.get_doc("NKT Customer Order", receipt.canonical_name)
    if str(order.company or "") != payload["company"] or str(order.customer or "") != payload["customer"]:
        raise NKTIdempotencyConflict(
            "Encoder settlement Company/Customer conflicts with the canonical Customer Order Draft."
        )
    if str(order.encoder or "") != str(envelope.get("origin_user") or ""):
        raise NKTIdempotencyConflict(
            "Encoder settlement origin user conflicts with the canonical Customer Order Encoder."
        )
    if getdate(order.order_date) != getdate(envelope["business_date"]):
        raise NKTIdempotencyConflict(
            "Encoder settlement Business Date conflicts with the canonical Customer Order."
        )
    if abs(flt(order.grand_total) - flt(payload["merchandise_total"])) > 0.01:
        raise NKTIdempotencyConflict(
            "Encoder settlement merchandise total conflicts with the canonical Customer Order."
        )
    return order


def _journal_conflicts(journal, envelope, payload, envelope_hash, payload_hash):
    bad = []
    checks = {
        "event_family": ENCODER_SETTLEMENT_FAMILY,
        "event_action": ENCODER_SETTLEMENT_ACTION,
        "origin_device": envelope.get("origin_device"),
        "origin_user": envelope.get("origin_user"),
        "operational_context": envelope.get("operational_context"),
        "order_event_uuid": payload.get("order_event_uuid"),
        "company": payload.get("company"),
        "customer": payload.get("customer"),
        "envelope_sha256": envelope_hash,
        "payload_sha256": payload_hash,
        "preservation_state": "Preserved",
    }
    for field, expected in checks.items():
        if str(journal.get(field) or "") != str(expected or ""):
            bad.append(field)

    allowed_downstream = {
        "Awaiting Cashier Countercheck",
        "Matched - Awaiting Receivable Materialization",
        "Receivable Materialized",
        "Receivable Not Required",
    }
    if str(journal.get("downstream_state") or "") not in allowed_downstream:
        bad.append("downstream_state")

    numeric_checks = {
        "merchandise_total": payload.get("merchandise_total"),
        "declared_payment_total": payload.get("declared_payment_total"),
        "declared_account": payload.get("declared_account"),
        "declared_card_surcharge_total": payload.get("declared_card_surcharge_total"),
        "declared_total_collected": payload.get("declared_total_collected"),
    }
    for field, expected in numeric_checks.items():
        if abs(flt(journal.get(field)) - flt(expected)) > TOLERANCE:
            bad.append(field)

    if str(journal.get("canonical_payload_json") or "") != _canonical_encoder_settlement_intent_json(payload):
        bad.append("canonical_payload_json")
    if str(journal.get("canonical_envelope_json") or "") != _canonical_json(envelope):
        bad.append("canonical_envelope_json")
    return bad


def _ack(receipt, journal, order, *, replay: bool):
    return {
        "event_uuid": receipt.name,
        "event_family": ENCODER_SETTLEMENT_FAMILY,
        "primary_ack_uuid": receipt.primary_ack_uuid,
        "payload_sha256": receipt.payload_sha256,
        "result_code": receipt.result_code,
        "committed": True,
        "replay": bool(replay),
        "canonical_doctype": PRIMARY_JOURNAL,
        "canonical_name": journal.name,
        "materialization_state": receipt.materialization_state,
        "order_event_uuid": journal.order_event_uuid,
        "customer_order": order.name,
        "customer_order_docstatus": int(order.docstatus or 0),
        "draft_declared_payments_hydrated": True,
        "customer_order_submitted": False,
        "matching_executed": False,
        "payment_receipt_created": False,
        "cashier_movement_created": False,
        "receivable_created": False,
        "advance_applied": False,
        "warehouse_release_created": False,
        "stock_entry_created": False,
    }


def prepare_encoder_settlement_intent_for_primary(event_uuid: str) -> Dict[str, Any]:
    return prepare_event_for_primary(
        event_uuid,
        expected_family=ENCODER_SETTLEMENT_FAMILY,
    )


def receive_encoder_settlement_intent_at_primary(packet: Dict[str, Any]) -> Dict[str, Any]:
    _require_primary()
    envelope, payload, envelope_hash, payload_hash = validate_transport_packet(
        packet,
        expected_family=ENCODER_SETTLEMENT_FAMILY,
    )
    event_uuid = envelope["event_uuid"]
    order_event_uuid = payload["order_event_uuid"]

    _acquire_claims(event_uuid, order_event_uuid)

    current = _receipt_for_update(event_uuid)
    expected_ack_uuid = _expected_primary_ack_uuid(event_uuid, payload_hash)
    if current:
        if (
            current.primary_ack_uuid != expected_ack_uuid
            or current.event_family != ENCODER_SETTLEMENT_FAMILY
            or current.envelope_sha256 != envelope_hash
            or current.payload_sha256 != payload_hash
            or current.canonical_doctype != PRIMARY_JOURNAL
            or current.canonical_name != event_uuid
            or current.materialization_state != MATERIALIZATION_STATE
            or current.result_code != "Committed"
        ):
            raise NKTIdempotencyConflict(
                "Encoder Settlement Primary receipt conflicts with immutable content."
            )
        journal = _journal_for_update(event_uuid)
        if not journal:
            raise NKTIdempotencyConflict(
                "Encoder Settlement Primary receipt exists without its preserved journal."
            )
        bad = _journal_conflicts(journal, envelope, payload, envelope_hash, payload_hash)
        if bad:
            raise NKTIdempotencyConflict(
                "Preserved Encoder Settlement journal conflicts with immutable content: "
                + ", ".join(bad)
            )
        order = _validate_order_binding(order_event_uuid, payload, envelope)
        _assert_draft_matches(order, payload, envelope)
        return _ack(current, journal, order, replay=True)

    prior_for_order = frappe.db.get_value(
        PRIMARY_JOURNAL,
        {"order_event_uuid": order_event_uuid},
        ["name", "event_uuid"],
        as_dict=True,
    )
    if prior_for_order:
        raise NKTIdempotencyConflict(
            "Customer Order Intent is already bound to another immutable Encoder Settlement event."
        )

    order = _validate_order_binding(order_event_uuid, payload, envelope)
    canonical_payload = _canonical_encoder_settlement_intent_json(payload)
    canonical_envelope = _canonical_json(envelope)

    journal = frappe.get_doc(
        {
            "doctype": PRIMARY_JOURNAL,
            "event_uuid": event_uuid,
            "event_family": ENCODER_SETTLEMENT_FAMILY,
            "event_action": ENCODER_SETTLEMENT_ACTION,
            "origin_device": envelope["origin_device"],
            "origin_user": envelope["origin_user"],
            "operational_context": envelope["operational_context"],
            "business_date": envelope["business_date"],
            "settled_at": envelope["settled_at"],
            "client_created_at": envelope.get("client_created_at"),
            "order_event_uuid": order_event_uuid,
            "customer_order": order.name,
            "company": payload["company"],
            "customer": payload["customer"],
            "merchandise_total": payload["merchandise_total"],
            "declared_payment_total": payload["declared_payment_total"],
            "declared_account": payload["declared_account"],
            "declared_card_surcharge_total": payload["declared_card_surcharge_total"],
            "declared_total_collected": payload["declared_total_collected"],
            "envelope_sha256": envelope_hash,
            "payload_sha256": payload_hash,
            "canonical_envelope_json": canonical_envelope,
            "canonical_payload_json": canonical_payload,
            "preservation_state": "Preserved",
            "downstream_state": "Awaiting Cashier Countercheck",
            "primary_preserved_at": now(),
        }
    )
    journal.insert(ignore_permissions=True)

    order, order_replay = _hydrate_order_draft(order.name, payload, envelope)

    receipt = frappe.get_doc(
        {
            "doctype": "NKT Sync Primary Receipt",
            "event_uuid": event_uuid,
            "event_family": ENCODER_SETTLEMENT_FAMILY,
            "primary_ack_uuid": expected_ack_uuid,
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
    return _ack(receipt, journal, order, replay=bool(order_replay))


@frappe.whitelist()
def receive_encoder_settlement_intent(packet):
    if isinstance(packet, str):
        packet = frappe.parse_json(packet)
    return receive_encoder_settlement_intent_at_primary(packet)


def apply_encoder_settlement_intent_ack_at_edge(ack: Dict[str, Any]) -> Dict[str, Any]:
    if _runtime_role() != "Store Edge":
        raise frappe.PermissionError("Encoder Settlement ACK unavailable.")
    if not isinstance(ack, dict):
        raise frappe.ValidationError("Encoder Settlement ACK is invalid.")

    event_uuid = _uuid(ack.get("event_uuid"), "Event UUID")
    ack_uuid = _uuid(ack.get("primary_ack_uuid"), "Primary ACK UUID")
    payload_hash = str(ack.get("payload_sha256") or "").lower()
    canonical_name = str(ack.get("canonical_name") or "").strip()
    expected_ack_uuid = _expected_primary_ack_uuid(event_uuid, payload_hash)
    if ack_uuid != expected_ack_uuid:
        raise NKTIdempotencyConflict(
            "Encoder Settlement ACK UUID is not the deterministic Primary ACK for this immutable settlement."
        )

    if ack.get("committed") is not True or ack.get("result_code") != "Committed":
        raise frappe.ValidationError("Encoder Settlement ACK is not committed.")
    if ack.get("event_family") != ENCODER_SETTLEMENT_FAMILY:
        raise frappe.ValidationError("Encoder Settlement ACK family is invalid.")
    if ack.get("canonical_doctype") != PRIMARY_JOURNAL or canonical_name != event_uuid:
        raise frappe.ValidationError("Encoder Settlement ACK canonical identity is invalid.")
    if ack.get("materialization_state") != MATERIALIZATION_STATE:
        raise frappe.ValidationError("Encoder Settlement ACK state is invalid.")
    if int(ack.get("customer_order_docstatus") or 0) != 0:
        raise frappe.ValidationError("Encoder Settlement ACK must reference a Customer Order Draft.")

    if not frappe.db.exists("NKT Sync Event", event_uuid):
        raise frappe.DoesNotExistError("Encoder Settlement Intent event is unavailable.")
    event = frappe.get_doc("NKT Sync Event", event_uuid)
    if event.event_family != ENCODER_SETTLEMENT_FAMILY:
        raise frappe.ValidationError("Encoder Settlement event family mismatch.")
    if event.payload_sha256 != payload_hash:
        raise NKTIdempotencyConflict(
            "Encoder Settlement ACK payload hash conflicts with immutable event."
        )
    bound = str(event.primary_ack_uuid or "").strip()
    if bound and bound != ack_uuid:
        raise NKTIdempotencyConflict(
            "Encoder Settlement ACK UUID conflicts with the ACK already bound to this event."
        )

    if event.sync_state == "Committed at Primary":
        if (
            bound == ack_uuid
            and event.canonical_doctype == PRIMARY_JOURNAL
            and event.canonical_name == canonical_name
        ):
            return {
                "event_uuid": event_uuid,
                "primary_ack_uuid": ack_uuid,
                "customer_order": ack.get("customer_order"),
                "sync_state": "Committed at Primary",
                "pending_payload_purged": False,
                "replay": True,
            }
        raise NKTIdempotencyConflict(
            "Committed Encoder Settlement event conflicts with supplied ACK."
        )

    pending_name = frappe.db.exists("NKT Sync Pending Payload", event_uuid)
    if not pending_name:
        raise frappe.ValidationError(
            "Encoder Settlement ACK arrived without its pending payload."
        )
    pending = frappe.get_doc("NKT Sync Pending Payload", pending_name)
    if (
        pending.event_family != ENCODER_SETTLEMENT_FAMILY
        or pending.payload_sha256 != payload_hash
    ):
        raise NKTIdempotencyConflict(
            "Encoder Settlement ACK conflicts with pending payload."
        )

    mark_primary_committed(
        event_uuid,
        PRIMARY_JOURNAL,
        canonical_name,
        primary_ack_uuid=ack_uuid,
    )
    frappe.delete_doc(
        "NKT Sync Pending Payload",
        pending.name,
        ignore_permissions=True,
        force=True,
    )
    return {
        "event_uuid": event_uuid,
        "primary_ack_uuid": ack_uuid,
        "customer_order": ack.get("customer_order"),
        "sync_state": "Committed at Primary",
        "pending_payload_purged": True,
        "replay": False,
    }
