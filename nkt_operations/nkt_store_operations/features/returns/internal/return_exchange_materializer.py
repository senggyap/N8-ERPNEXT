from __future__ import annotations

import hashlib
import json
import uuid
from contextlib import contextmanager
from typing import Any, Dict

import frappe
from frappe.utils import now

from nkt_operations.nkt_store_operations.features.returns.internal.return_exchange_offline_intent import (
    FAMILY,
    canonical_return_exchange_intent_json,
    normalize_return_exchange_intent,
)
from nkt_operations.nkt_store_operations.features.offline_edge.safe_sync import NKTIdempotencyConflict
from nkt_operations.nkt_store_operations.features.offline_edge.sync_transport import _runtime_role

FOUNDATION_VERSION = "C15C.10I-R10"
PRIMARY_JOURNAL = "NKT Primary Return Exchange Intent"
MATERIALIZATION_ACK_NAMESPACE = uuid.UUID("fdd0d243-9baa-45d9-a55f-51903dc1cb35")


def _require_primary():
    if _runtime_role() != "Primary":
        raise frappe.PermissionError("Return/Exchange canonical materializer unavailable.")


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


@contextmanager
def _as_user(user: str):
    old = frappe.session.user
    frappe.set_user(user)
    try:
        yield
    finally:
        frappe.set_user(old)


def _claim_names(event_uuid: str, old_order: str, old_sale: str):
    identities = (
        ("event", event_uuid),
        ("order", old_order),
        ("sale", old_sale),
    )
    return sorted(
        "nkt-10i-mat-" + hashlib.sha256(
            f"{kind}:{identity}".encode("utf-8")
        ).hexdigest()[:28]
        for kind, identity in identities
    )


def _acquire_claims(event_uuid: str, old_order: str, old_sale: str):
    acquired = []
    try:
        for name in _claim_names(event_uuid, old_order, old_sale):
            rows = frappe.db.sql("SELECT GET_LOCK(%s,%s)", (name, 30), as_list=True)
            if not rows or int(rows[0][0] or 0) != 1:
                raise frappe.ValidationError(
                    "Return/Exchange materialization is busy. Safe retry is required."
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


def _release_claims(names):
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


def _payload_from_journal(journal) -> Dict[str, Any]:
    try:
        raw = json.loads(str(journal.canonical_payload_json or ""))
    except Exception as exc:
        raise NKTIdempotencyConflict(
            "Preserved Return/Exchange payload JSON is invalid."
        ) from exc
    payload = normalize_return_exchange_intent(raw)
    canonical = canonical_return_exchange_intent_json(payload)
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    if digest != str(journal.payload_sha256 or "").lower():
        raise NKTIdempotencyConflict(
            "Preserved Return/Exchange payload hash no longer matches canonical content."
        )
    if canonical != str(journal.canonical_payload_json or ""):
        raise NKTIdempotencyConflict(
            "Preserved Return/Exchange canonical payload has drifted."
        )
    return payload


def _existing_declaration(event_uuid: str):
    return frappe.db.get_value(
        "NKT Return Exchange Declaration",
        {
            "custom_nkt_offline_event_uuid": event_uuid,
            "docstatus": ["!=", 2],
        },
        "name",
    )


def _verify_declaration(doc, journal, payload):
    bad = []
    exact = {
        "side": payload["side"],
        "company": payload["company"],
        "customer": payload["customer"],
        "old_cashier_sale": payload["old_cashier_sale"],
        "old_customer_order": payload["old_customer_order"],
        "transaction_type": payload["transaction_type"],
        "entry_user": journal.origin_user,
        "custom_nkt_offline_event_uuid": journal.name,
        "custom_nkt_offline_payload_sha256": journal.payload_sha256,
    }
    for field, value in exact.items():
        if str(doc.get(field) or "") != str(value or ""):
            bad.append(field)
    if str(doc.business_date) != str(journal.business_date):
        bad.append("business_date")
    if str(doc.get("custom_nkt_offline_cashier_shift") or "") != str(journal.cashier_shift or ""):
        bad.append("custom_nkt_offline_cashier_shift")
    if int(doc.docstatus or 0) != 1:
        bad.append("docstatus")
    if str(doc.posting_status or "") != "Posted":
        bad.append("posting_status")
    if bad:
        raise NKTIdempotencyConflict(
            "Canonical Return/Exchange declaration conflicts with preserved intent: "
            + ", ".join(bad)
        )


def _effect_bindings(doc) -> Dict[str, Any]:
    result = {
        "new_cashier_sale": str(doc.new_cashier_sale or "") or None,
        "new_customer_order": str(doc.new_customer_order or "") or None,
        "return_stock_entry": str(doc.return_stock_entry or "") or None,
        "account_adjustment_record": str(doc.account_adjustment_record or "") or None,
        "customer_credit_record": str(doc.customer_credit_record or "") or None,
        "refund_movement": None,
    }
    refund = frappe.db.get_value(
        "NKT Cashier Movement",
        {
            "source_doctype": "NKT Return Exchange Declaration",
            "source_name": doc.name,
            "source_row": "refund",
            "docstatus": ["!=", 2],
        },
        "name",
    )
    result["refund_movement"] = refund or None
    return result


def _materialization_ack(journal, doc, effects) -> Dict[str, Any]:
    material = (
        "NKT Return Exchange Canonical Materialization"
        + "\0" + journal.name
        + "\0" + str(journal.payload_sha256 or "")
        + "\0" + doc.name
        + "\0" + _canonical_json(effects)
    )
    ack_uuid = str(uuid.uuid5(MATERIALIZATION_ACK_NAMESPACE, material))
    ack = {
        "event_uuid": journal.name,
        "event_family": FAMILY,
        "payload_sha256": journal.payload_sha256,
        "materialization_ack_uuid": ack_uuid,
        "side": journal.side,
        "canonical_declaration": doc.name,
        "business_date": str(journal.business_date),
        "cashier_shift": journal.cashier_shift or None,
        **effects,
    }
    ack_json = _canonical_json(ack)
    ack_sha = hashlib.sha256(ack_json.encode("utf-8")).hexdigest()
    return {**ack, "materialization_ack_sha256": ack_sha}


def _stored_result(journal, payload, *, replay: bool):
    name = str(journal.canonical_declaration or "")
    if not name or not frappe.db.exists("NKT Return Exchange Declaration", name):
        raise NKTIdempotencyConflict(
            "Return/Exchange materialization journal lost canonical Declaration binding."
        )
    doc = frappe.get_doc("NKT Return Exchange Declaration", name)
    _verify_declaration(doc, journal, payload)
    effects = _effect_bindings(doc)
    ack = _materialization_ack(journal, doc, effects)
    if str(journal.materialization_ack_uuid or "") != ack["materialization_ack_uuid"]:
        raise NKTIdempotencyConflict("Stored Return/Exchange materialization ACK UUID is invalid.")
    if str(journal.materialization_ack_sha256 or "") != ack["materialization_ack_sha256"]:
        raise NKTIdempotencyConflict("Stored Return/Exchange materialization ACK hash is invalid.")
    return {
        **ack,
        "committed": True,
        "materialization_state": "Return Exchange Materialized",
        "replay": bool(replay),
        "canonical_return_exchange_declaration_created": True,
        "matching_remains_post_operation_reconciliation": True,
        "controlled_reversal_offline_enabled": False,
        "edge_projection_may_finalize_only_after_local_canonical_rebase": True,
    }


def materialize_return_exchange(event_uuid: str) -> Dict[str, Any]:
    _require_primary()
    event_uuid = _uuid(event_uuid, "Return/Exchange Event UUID")
    if not frappe.db.exists(PRIMARY_JOURNAL, event_uuid):
        raise frappe.DoesNotExistError(
            "Preserved Return/Exchange intent is unavailable at Primary."
        )

    seed = frappe.db.get_value(
        PRIMARY_JOURNAL,
        event_uuid,
        ["old_customer_order", "old_cashier_sale"],
        as_dict=True,
    )
    locks = _acquire_claims(
        event_uuid,
        seed.old_customer_order,
        seed.old_cashier_sale,
    )
    try:
        journal = _journal_for_update(event_uuid)
        if not journal:
            raise frappe.DoesNotExistError(
                "Preserved Return/Exchange intent is unavailable at Primary."
            )
        payload = _payload_from_journal(journal)

        frappe.db.sql(
            "SELECT name FROM `tabNKT Customer Order` WHERE name=%s FOR UPDATE",
            (journal.old_customer_order,),
        )
        frappe.db.sql(
            "SELECT name FROM `tabNKT Cashier Sale` WHERE name=%s FOR UPDATE",
            (journal.old_cashier_sale,),
        )

        if str(journal.preservation_state or "") != "Preserved":
            raise NKTIdempotencyConflict("Return/Exchange intent is not preserved.")

        if str(journal.downstream_state or "") == "Return Exchange Materialized":
            return _stored_result(journal, payload, replay=True)
        if str(journal.downstream_state or "") != "Awaiting Return Exchange Materialization":
            raise NKTIdempotencyConflict(
                "Return/Exchange intent is not eligible for canonical materialization."
            )

        existing = _existing_declaration(event_uuid)
        if existing:
            doc = frappe.get_doc("NKT Return Exchange Declaration", existing)
            _verify_declaration(doc, journal, payload)
        else:
            from nkt_operations.nkt_store_operations.features.returns.matching import (
                build_primary_offline_declaration,
            )

            with _as_user(journal.origin_user):
                doc = build_primary_offline_declaration(
                    payload,
                    event_uuid=event_uuid,
                    payload_sha256=journal.payload_sha256,
                    physical_settled_at=journal.settled_at,
                )
                doc.insert(ignore_permissions=True)
                doc.submit()
                doc.reload()

            _verify_declaration(doc, journal, payload)

        effects = _effect_bindings(doc)
        ack = _materialization_ack(journal, doc, effects)
        ack_json = _canonical_json(
            {k: v for k, v in ack.items() if k != "materialization_ack_sha256"}
        )

        frappe.db.set_value(
            PRIMARY_JOURNAL,
            journal.name,
            {
                "downstream_state": "Return Exchange Materialized",
                "canonical_declaration": doc.name,
                "new_cashier_sale": effects["new_cashier_sale"],
                "new_customer_order": effects["new_customer_order"],
                "return_stock_entry": effects["return_stock_entry"],
                "account_adjustment_record": effects["account_adjustment_record"],
                "customer_credit_record": effects["customer_credit_record"],
                "refund_movement": effects["refund_movement"],
                "materialization_ack_uuid": ack["materialization_ack_uuid"],
                "materialization_ack_sha256": ack["materialization_ack_sha256"],
                "materialization_ack_json": ack_json,
                "materialized_at": now(),
            },
            update_modified=False,
        )
        journal.reload()
        return _stored_result(journal, payload, replay=False)
    finally:
        _release_claims(locks)


def foundation_status():
    return {
        "foundation_version": FOUNDATION_VERSION,
        "primary_materialization_enabled": True,
        "uses_accepted_c7_independent_posting_engine": True,
        "cashier_encoder_remain_operationally_independent": True,
        "matching_is_post_operation_reconciliation": True,
        "preserves_true_physical_business_date": True,
        "preserves_original_cashier_shift": True,
        "closed_historical_shift_compatibility_is_scoped": True,
        "controlled_reversal_offline_enabled": False,
        "canonical_posting_at_edge_enabled": False,
    }


@frappe.whitelist()
def materialize_return_exchange_intent(event_uuid: str):
    return materialize_return_exchange(event_uuid)
