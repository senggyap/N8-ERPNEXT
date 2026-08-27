from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime
from typing import Any, Dict
from zoneinfo import ZoneInfo

import frappe
from frappe.utils import getdate, now

from nkt_operations.nkt_store_operations.features.returns.internal.return_exchange_offline_intent import (
    ACTION,
    FAMILY,
    canonical_return_exchange_intent_json,
    normalize_return_exchange_intent,
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

FOUNDATION_VERSION = "C15C.10I-R3"
PH_TZ = ZoneInfo("Asia/Manila")

PRIMARY_MATERIALIZATION_ENABLED = True
EDGE_ACCEPTANCE_ENABLED = True
EDGE_SALEABLE_STOCK_PROJECTION_ENABLED = True
EDGE_CUSTOMER_CREDIT_PROJECTION_ENABLED = True
EDGE_ACCOUNT_ADJUSTMENT_PROJECTION_ENABLED = True
EDGE_CASHIER_MONEY_PROJECTION_ENABLED = True
EDGE_REPLACEMENT_NEW_SALE_PROJECTION_ENABLED = True
EDGE_REPLACEMENT_NEW_ORDER_PROJECTION_ENABLED = True

PRIMARY_JOURNAL = "NKT Primary Return Exchange Intent"
PRESERVATION_STATE = "Return Exchange Intent Preserved"
PRIMARY_ACK_NAMESPACE = uuid.UUID("2e6dbad4-46cb-4f58-a4ca-8f55a4cb9a3d")
ACTIVE_EDGE_PROJECTION_STATES = (
    "Pending Edge",
    "Awaiting Primary",
    "Primary Preserved",
    "Primary Materialized",
)

LOCKED_CROSS_MIDNIGHT_TRUE_TIME = True
LOCKED_CASHIER_SHIFT_PRESERVED = True
LOCKED_CASHIER_ENCODER_INDEPENDENCE = True
LOCKED_MATCHING_POST_OPERATION_ONLY = True
LOCKED_SALEABLE_RETURN_LOCAL_STOCK = True
LOCKED_ENCODER_CUSTOMER_CREDIT_LOCAL_USE = True
LOCKED_ENCODER_ACCOUNT_ADJUSTMENT_LOCAL_USE = True

CONTROLLED_REVERSAL_OFFLINE_ENABLED = False
EMPLOYEE_MANUAL_BACKDATE_ENABLED = False
CANONICAL_POSTING_AT_EDGE_ENABLED = False


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


def validate_primary_return_exchange_contract(packet: Dict[str, Any]) -> Dict[str, Any]:
    """
    Validate the dedicated Primary-side immutable Return/Exchange contract only.

    This function deliberately creates no accepted C7 business effect:
    - no NKT Return Exchange Declaration;
    - no NEW Cashier Sale or NEW Customer Order;
    - no Stock Entry / Stock Ledger / GL;
    - no Cashier Movement/refund;
    - no Customer Credit / Account Adjustment;
    - no matching result;
    - no controlled reversal.

    Cross-midnight rule:
    Store Edge may upload later, but envelope.business_date, envelope.settled_at,
    payload.business_date, and payload.entry_datetime must all describe the same
    true physical Manila event date. This is server verification of immutable
    evidence, not a user-facing backdate permission.
    """
    envelope, payload, envelope_hash, payload_hash = validate_transport_packet(
        packet,
        expected_family=FAMILY,
    )

    if envelope.get("event_action") != ACTION:
        raise frappe.ValidationError(
            "Return/Exchange Primary contract action is invalid."
        )

    physical_date = getdate(envelope.get("business_date"))
    settled_at = _manila_datetime(
        envelope.get("settled_at"),
        "Return/Exchange physical settled time",
    )
    payload_date = getdate(payload.get("business_date"))
    entry_datetime = _manila_datetime(
        payload.get("entry_datetime"),
        "Return/Exchange entry time",
    )

    if payload_date != physical_date:
        raise frappe.ValidationError(
            "Return/Exchange Business Date must match the immutable Store-Edge business date."
        )
    if settled_at.date() != physical_date:
        raise frappe.ValidationError(
            "Return/Exchange settled time must match the immutable Store-Edge business date."
        )
    if entry_datetime.date() != physical_date:
        raise frappe.ValidationError(
            "Return/Exchange entry time must match the immutable physical event date."
        )

    origin_user = str(envelope.get("origin_user") or "").strip()
    entry_user = str(payload.get("entry_user") or "").strip()
    if not origin_user or origin_user != entry_user:
        raise frappe.ValidationError(
            "Return/Exchange entry user must match the immutable event origin user."
        )

    side = str(payload.get("side") or "").strip()
    if side == "Cashier":
        shift = str(payload.get("cashier_shift") or "").strip()
        shift_date = getdate(payload.get("cashier_shift_business_date"))
        if not shift:
            raise frappe.ValidationError(
                "Cashier Return/Exchange must preserve the original Cashier Shift."
            )
        if shift_date != physical_date:
            raise frappe.ValidationError(
                "Cashier Shift Business Date must match the immutable physical event date."
            )
    elif side == "Encoder":
        if payload.get("cashier_shift") or payload.get("cashier_shift_business_date"):
            raise frappe.ValidationError(
                "Encoder Return/Exchange cannot carry Cashier Shift evidence."
            )
    else:
        raise frappe.ValidationError(
            "Return/Exchange side must be Cashier or Encoder."
        )

    return {
        "event_uuid": envelope["event_uuid"],
        "event_family": FAMILY,
        "event_action": ACTION,
        "payload_sha256": payload_hash,
        "envelope_sha256": envelope_hash,
        "side": side,
        "submit_request_id": payload["submit_request_id"],
        "company": payload["company"],
        "customer": payload["customer"],
        "business_date": physical_date.isoformat(),
        "physical_settled_at_manila": settled_at.isoformat(),
        "entry_datetime_manila": entry_datetime.isoformat(),
        "origin_user": origin_user,
        "cashier_shift": payload.get("cashier_shift"),
        "old_cashier_sale": payload["old_cashier_sale"],
        "old_customer_order": payload["old_customer_order"],
        "source_generation": payload["source_generation"],
        "transaction_type": payload["transaction_type"],
        "returned_item_count": len(payload.get("returned_items") or []),
        "new_item_count": len(payload.get("new_items") or []),
        "settlement_payment_count": len(payload.get("settlement_payments") or []),
        "settlement_destination": payload.get("settlement_destination"),
        "return_warehouse": payload.get("return_warehouse"),
        "primary_materialization_enabled": PRIMARY_MATERIALIZATION_ENABLED,
        "edge_acceptance_enabled": EDGE_ACCEPTANCE_ENABLED,
        "edge_saleable_stock_projection_enabled": EDGE_SALEABLE_STOCK_PROJECTION_ENABLED,
        "edge_customer_credit_projection_enabled": EDGE_CUSTOMER_CREDIT_PROJECTION_ENABLED,
        "edge_account_adjustment_projection_enabled": EDGE_ACCOUNT_ADJUSTMENT_PROJECTION_ENABLED,
        "edge_cashier_money_projection_enabled": EDGE_CASHIER_MONEY_PROJECTION_ENABLED,
        "edge_replacement_new_sale_projection_enabled": EDGE_REPLACEMENT_NEW_SALE_PROJECTION_ENABLED,
        "edge_replacement_new_order_projection_enabled": EDGE_REPLACEMENT_NEW_ORDER_PROJECTION_ENABLED,
        "cross_midnight_true_time_preserved": LOCKED_CROSS_MIDNIGHT_TRUE_TIME,
        "cashier_shift_preserved": LOCKED_CASHIER_SHIFT_PRESERVED,
        "cashier_encoder_operationally_independent": LOCKED_CASHIER_ENCODER_INDEPENDENCE,
        "matching_is_post_operation_reconciliation": LOCKED_MATCHING_POST_OPERATION_ONLY,
        "saleable_return_local_stock_locked_for_next_stage": LOCKED_SALEABLE_RETURN_LOCAL_STOCK,
        "encoder_customer_credit_local_use_locked_for_next_stage": LOCKED_ENCODER_CUSTOMER_CREDIT_LOCAL_USE,
        "encoder_account_adjustment_local_use_locked_for_next_stage": LOCKED_ENCODER_ACCOUNT_ADJUSTMENT_LOCAL_USE,
        "controlled_reversal_offline_enabled": CONTROLLED_REVERSAL_OFFLINE_ENABLED,
        "employee_manual_backdate_enabled": EMPLOYEE_MANUAL_BACKDATE_ENABLED,
        "canonical_posting_at_edge_enabled": CANONICAL_POSTING_AT_EDGE_ENABLED,
    }


def contract_status() -> Dict[str, Any]:
    return {
        "foundation_version": FOUNDATION_VERSION,
        "event_family": FAMILY,
        "event_action": ACTION,
        "transport_registered": True,
        "generic_primary_receipt_allowed": False,
        "primary_materialization_enabled": PRIMARY_MATERIALIZATION_ENABLED,
        "edge_acceptance_enabled": EDGE_ACCEPTANCE_ENABLED,
        "edge_saleable_stock_projection_enabled": EDGE_SALEABLE_STOCK_PROJECTION_ENABLED,
        "edge_customer_credit_projection_enabled": EDGE_CUSTOMER_CREDIT_PROJECTION_ENABLED,
        "edge_account_adjustment_projection_enabled": EDGE_ACCOUNT_ADJUSTMENT_PROJECTION_ENABLED,
        "edge_cashier_money_projection_enabled": EDGE_CASHIER_MONEY_PROJECTION_ENABLED,
        "edge_replacement_new_sale_projection_enabled": EDGE_REPLACEMENT_NEW_SALE_PROJECTION_ENABLED,
        "edge_replacement_new_order_projection_enabled": EDGE_REPLACEMENT_NEW_ORDER_PROJECTION_ENABLED,
        "cross_midnight_true_time_preserved": LOCKED_CROSS_MIDNIGHT_TRUE_TIME,
        "cashier_shift_preserved": LOCKED_CASHIER_SHIFT_PRESERVED,
        "cashier_encoder_operationally_independent": LOCKED_CASHIER_ENCODER_INDEPENDENCE,
        "matching_is_post_operation_reconciliation": LOCKED_MATCHING_POST_OPERATION_ONLY,
        "saleable_return_local_stock_locked_for_next_stage": LOCKED_SALEABLE_RETURN_LOCAL_STOCK,
        "encoder_customer_credit_local_use_locked_for_next_stage": LOCKED_ENCODER_CUSTOMER_CREDIT_LOCAL_USE,
        "encoder_account_adjustment_local_use_locked_for_next_stage": LOCKED_ENCODER_ACCOUNT_ADJUSTMENT_LOCAL_USE,
        "controlled_reversal_offline_enabled": CONTROLLED_REVERSAL_OFFLINE_ENABLED,
        "employee_manual_backdate_enabled": EMPLOYEE_MANUAL_BACKDATE_ENABLED,
        "canonical_posting_at_edge_enabled": CANONICAL_POSTING_AT_EDGE_ENABLED,
        "matching_at_edge_enabled": False,
        "return_exchange_reversal_at_edge_enabled": False,
        "primary_preservation_enabled": True,
        "edge_preservation_ack_enabled": True,
        "primary_journal_doctype": PRIMARY_JOURNAL,
    }


def _canonical_json(value: Dict[str, Any]) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    )


def _require_primary() -> None:
    if _runtime_role() != "Primary":
        raise frappe.PermissionError("Return/Exchange Primary receiver unavailable.")


def _uuid(value: Any, label: str) -> str:
    try:
        return str(uuid.UUID(str(value)))
    except Exception as exc:
        raise frappe.ValidationError(f"{label} is invalid.") from exc


def _expected_primary_ack_uuid(event_uuid: str, payload_hash: str) -> str:
    event_uuid = _uuid(event_uuid, "Event UUID")
    material = FAMILY + "\0" + event_uuid + "\0" + str(payload_hash or "").lower()
    return str(uuid.uuid5(PRIMARY_ACK_NAMESPACE, material))


def _claim_name(kind: str, identity: str) -> str:
    return "nkt-10i-return-" + hashlib.sha256(
        f"{kind}:{identity}".encode("utf-8")
    ).hexdigest()[:28]


def _acquire_claims(event_uuid: str, old_order: str, old_sale: str) -> list[str]:
    names = sorted({
        _claim_name("event", event_uuid),
        _claim_name("order", old_order),
        _claim_name("sale", old_sale),
    })
    acquired = []
    try:
        for name in names:
            rows = frappe.db.sql("SELECT GET_LOCK(%s,%s)", (name, 30), as_list=True)
            if not rows or int(rows[0][0] or 0) != 1:
                raise frappe.ValidationError(
                    "Return/Exchange Primary preservation is busy. Safe retry is required."
                )
            acquired.append(name)
        return acquired
    except Exception:
        for name in reversed(acquired):
            try:
                frappe.db.sql("SELECT RELEASE_LOCK(%s)", (name,))
            except Exception:
                pass
        raise


def _release_claims(names) -> None:
    for name in reversed(names):
        try:
            frappe.db.sql("SELECT RELEASE_LOCK(%s)", (name,))
        except Exception:
            pass


def _journal_for_update(event_uuid: str):
    rows = frappe.db.sql(
        f"SELECT name FROM `tab{PRIMARY_JOURNAL}` WHERE name=%s FOR UPDATE",
        (event_uuid,),
        as_dict=True,
    )
    return frappe.get_doc(PRIMARY_JOURNAL, event_uuid) if rows else None


def _receipt_for_update(event_uuid: str):
    rows = frappe.db.sql(
        "SELECT name FROM `tabNKT Sync Primary Receipt` WHERE name=%s FOR UPDATE",
        (event_uuid,),
        as_dict=True,
    )
    return frappe.get_doc("NKT Sync Primary Receipt", event_uuid) if rows else None


def _validate_primary_identity(payload: Dict[str, Any], envelope: Dict[str, Any]) -> None:
    if not frappe.db.exists("NKT Cashier Sale", payload["old_cashier_sale"]):
        raise frappe.DoesNotExistError("OLD Cashier Sale is unavailable at Primary.")
    if not frappe.db.exists("NKT Customer Order", payload["old_customer_order"]):
        raise frappe.DoesNotExistError("OLD Customer Order is unavailable at Primary.")
    if not frappe.db.exists("Customer", payload["customer"]):
        raise frappe.DoesNotExistError("Customer is unavailable at Primary.")
    if not frappe.db.exists("Company", payload["company"]):
        raise frappe.DoesNotExistError("Company is unavailable at Primary.")
    if not frappe.db.exists("User", envelope["origin_user"]):
        raise frappe.DoesNotExistError("Origin user is unavailable at Primary.")

    if payload["side"] == "Cashier":
        shift = frappe.db.get_value(
            "NKT Cashier Shift",
            payload["cashier_shift"],
            ["company", "cashier", "shift_start", "shift_end"],
            as_dict=True,
        )
        if not shift:
            raise frappe.DoesNotExistError("Original Cashier Shift is unavailable at Primary.")
        if str(shift.company or "") != payload["company"]:
            raise NKTIdempotencyConflict("Original Cashier Shift Company conflicts.")
        if str(shift.cashier or "") != envelope["origin_user"]:
            raise NKTIdempotencyConflict("Original Cashier Shift Cashier conflicts.")
        if getdate(shift.shift_start) != getdate(payload["business_date"]):
            raise NKTIdempotencyConflict("Original Cashier Shift date conflicts.")
        entry_at = _manila_datetime(payload["entry_datetime"], "Entry Time").replace(tzinfo=None)
        if shift.shift_start and entry_at < shift.shift_start:
            raise NKTIdempotencyConflict("Return/Exchange predates original Cashier Shift.")
        if shift.shift_end and entry_at > shift.shift_end:
            raise NKTIdempotencyConflict("Return/Exchange is after original Cashier Shift end.")


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
        "side": journal.side,
        "company": journal.company,
        "customer": journal.customer,
        "old_cashier_sale": journal.old_cashier_sale,
        "old_customer_order": journal.old_customer_order,
        "business_date": str(journal.business_date),
        "cashier_shift": journal.cashier_shift or None,
        "canonical_return_exchange_declaration_created": False,
        "edge_projection_must_remain": True,
    }


def prepare_return_exchange_intent_for_primary(event_uuid: str) -> Dict[str, Any]:
    packet = prepare_event_for_primary(event_uuid, expected_family=FAMILY)
    projection_doctypes = (
        "NKT Edge Return Exchange Projection",
        "NKT Edge Return Exchange Stock Projection",
        "NKT Edge Return Exchange Cash Projection",
        "NKT Edge Return Exchange New Item Projection",
    )
    parent_count = 0
    for dt in projection_doctypes:
        if not frappe.db.exists("DocType", dt):
            continue
        rows = frappe.get_all(
            dt,
            filters={"event_uuid": event_uuid},
            pluck="name",
            limit_page_length=500,
        )
        if dt == "NKT Edge Return Exchange Projection":
            parent_count = len(rows)
        for name in rows:
            frappe.db.set_value(
                dt,
                name,
                "projection_state",
                "Awaiting Primary",
                update_modified=False,
            )
    if parent_count != 1:
        raise frappe.ValidationError(
            "Return/Exchange intent cannot replicate without exactly one parent Edge projection."
        )
    return packet


def receive_return_exchange_intent_at_primary(packet: Dict[str, Any]) -> Dict[str, Any]:
    _require_primary()
    validate_primary_return_exchange_contract(packet)
    envelope, raw_payload, envelope_hash, payload_hash = validate_transport_packet(
        packet,
        expected_family=FAMILY,
    )
    payload = normalize_return_exchange_intent(raw_payload)
    event_uuid = envelope["event_uuid"]
    expected_ack = _expected_primary_ack_uuid(event_uuid, payload_hash)

    locks = _acquire_claims(
        event_uuid,
        payload["old_customer_order"],
        payload["old_cashier_sale"],
    )
    try:
        frappe.db.sql(
            "SELECT name FROM `tabNKT Customer Order` WHERE name=%s FOR UPDATE",
            (payload["old_customer_order"],),
        )
        frappe.db.sql(
            "SELECT name FROM `tabNKT Cashier Sale` WHERE name=%s FOR UPDATE",
            (payload["old_cashier_sale"],),
        )

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
                    "Return/Exchange Primary receipt conflicts with immutable content."
                )
            journal = _journal_for_update(event_uuid)
            if not journal:
                raise NKTIdempotencyConflict(
                    "Return/Exchange Primary receipt exists without preserved journal."
                )
            if (
                str(journal.payload_sha256 or "") != payload_hash
                or str(journal.envelope_sha256 or "") != envelope_hash
                or str(journal.canonical_payload_json or "")
                != canonical_return_exchange_intent_json(payload)
                or str(journal.canonical_envelope_json or "") != _canonical_json(envelope)
            ):
                raise NKTIdempotencyConflict(
                    "Preserved Return/Exchange journal conflicts with immutable content."
                )
            return _preservation_ack(receipt, journal, replay=True)

        _validate_primary_identity(payload, envelope)

        journal = frappe.get_doc(
            {
                "doctype": PRIMARY_JOURNAL,
                "event_uuid": event_uuid,
                "event_family": FAMILY,
                "event_action": ACTION,
                "side": payload["side"],
                "submit_request_id": payload["submit_request_id"],
                "origin_device": envelope["origin_device"],
                "origin_user": envelope["origin_user"],
                "operational_context": envelope["operational_context"],
                "business_date": envelope["business_date"],
                "settled_at": envelope["settled_at"],
                "client_created_at": envelope.get("client_created_at"),
                "company": payload["company"],
                "customer": payload["customer"],
                "old_cashier_sale": payload["old_cashier_sale"],
                "old_customer_order": payload["old_customer_order"],
                "source_generation": payload["source_generation"],
                "cashier_shift": payload.get("cashier_shift"),
                "transaction_type": payload["transaction_type"],
                "return_warehouse": payload.get("return_warehouse"),
                "envelope_sha256": envelope_hash,
                "payload_sha256": payload_hash,
                "canonical_envelope_json": _canonical_json(envelope),
                "canonical_payload_json": canonical_return_exchange_intent_json(payload),
                "preservation_state": "Preserved",
                "downstream_state": "Awaiting Return Exchange Materialization",
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
        return _preservation_ack(receipt, journal, replay=False)
    finally:
        _release_claims(locks)


def apply_return_exchange_preservation_ack_at_edge(ack: Dict[str, Any]) -> Dict[str, Any]:
    if _runtime_role() != "Store Edge":
        raise frappe.PermissionError("Return/Exchange preservation ACK unavailable.")
    if not isinstance(ack, dict):
        raise frappe.ValidationError("Return/Exchange preservation ACK is invalid.")

    event_uuid = _uuid(ack.get("event_uuid"), "Event UUID")
    ack_uuid = _uuid(ack.get("primary_ack_uuid"), "Primary ACK UUID")
    payload_hash = str(ack.get("payload_sha256") or "").lower()

    if (
        ack.get("committed") is not True
        or ack.get("result_code") != "Committed"
        or ack.get("materialization_state") != PRESERVATION_STATE
        or ack.get("canonical_doctype") != PRIMARY_JOURNAL
        or ack.get("canonical_name") != event_uuid
    ):
        raise frappe.ValidationError(
            "Return/Exchange preservation ACK is not a committed Primary preservation."
        )

    if not frappe.db.exists("NKT Sync Event", event_uuid):
        raise frappe.DoesNotExistError("Return/Exchange event is unavailable at Edge.")
    event = frappe.get_doc("NKT Sync Event", event_uuid)
    if event.event_family != FAMILY or str(event.payload_sha256 or "").lower() != payload_hash:
        raise NKTIdempotencyConflict(
            "Return/Exchange preservation ACK conflicts with immutable Edge event."
        )

    projection_doctypes = (
        "NKT Edge Return Exchange Projection",
        "NKT Edge Return Exchange Stock Projection",
        "NKT Edge Return Exchange Cash Projection",
        "NKT Edge Return Exchange New Item Projection",
    )
    rows_by_dt = {}
    for dt in projection_doctypes:
        if frappe.db.exists("DocType", dt):
            rows_by_dt[dt] = frappe.get_all(
                dt,
                filters={"event_uuid": event_uuid},
                fields=["name", "projection_state", "primary_ack_uuid"],
                limit_page_length=500,
            )
    if len(rows_by_dt.get("NKT Edge Return Exchange Projection") or []) != 1:
        raise frappe.ValidationError(
            "Return/Exchange preservation ACK requires exactly one parent Edge projection."
        )

    bound = str(event.primary_ack_uuid or "").strip()
    if bound and bound != ack_uuid:
        raise NKTIdempotencyConflict(
            "Return/Exchange preservation ACK UUID conflicts with the ACK already bound."
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
                "Return/Exchange event is committed with different Primary bindings."
            )
    else:
        raise frappe.ValidationError(
            "Return/Exchange event is not eligible for Primary preservation ACK."
        )

    if pending:
        pd = frappe.get_doc("NKT Sync Pending Payload", pending)
        if str(pd.payload_sha256 or "").lower() != payload_hash:
            raise NKTIdempotencyConflict(
                "Return/Exchange preservation ACK conflicts with pending payload."
            )
        frappe.delete_doc(
            "NKT Sync Pending Payload",
            pd.name,
            ignore_permissions=True,
            force=True,
        )

    for dt, rows in rows_by_dt.items():
        for row in rows:
            if row.primary_ack_uuid and str(row.primary_ack_uuid) != ack_uuid:
                raise NKTIdempotencyConflict(
                    f"{dt} already has a different Primary preservation ACK."
                )
            if row.projection_state not in (
                "Pending Edge",
                "Awaiting Primary",
                "Primary Preserved",
                "Primary Materialized",
                "Finalized",
            ):
                raise NKTIdempotencyConflict(
                    f"{dt} is not eligible for Return/Exchange preservation ACK."
                )
            if row.projection_state in ("Pending Edge", "Awaiting Primary"):
                frappe.db.set_value(
                    dt,
                    row.name,
                    {
                        "projection_state": "Primary Preserved",
                        "primary_ack_uuid": ack_uuid,
                    },
                    update_modified=False,
                )

    return {
        "event_uuid": event_uuid,
        "event_family": FAMILY,
        "primary_ack_uuid": ack_uuid,
        "sync_state": "Committed at Primary",
        "projection_state": "Primary Preserved",
        "pending_payload_purged": bool(pending),
        "replay": not bool(pending),
    }


@frappe.whitelist()
def receive_return_exchange_intent(packet: Dict[str, Any]):
    if isinstance(packet, str):
        packet = frappe.parse_json(packet)
    return receive_return_exchange_intent_at_primary(packet)


@frappe.whitelist()
def apply_return_exchange_preservation_ack(ack: Dict[str, Any]):
    if isinstance(ack, str):
        ack = frappe.parse_json(ack)
    return apply_return_exchange_preservation_ack_at_edge(ack)
