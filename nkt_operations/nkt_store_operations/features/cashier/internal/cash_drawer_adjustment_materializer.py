from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any, Dict

import frappe
from frappe.utils import cint, flt, get_datetime, now

from nkt_operations.nkt_store_operations.features.cashier.internal.cashier_shift_alias import resolve_primary_cashier_shift
from nkt_operations.nkt_store_operations.features.offline_edge.safe_sync import (
    NKTIdempotencyConflict,
)
from nkt_operations.nkt_store_operations.features.offline_edge.sync_transport import _runtime_role
from nkt_operations.nkt_store_operations.features.cashier.internal.cash_drawer_adjustment_intent import (
    DENOMINATIONS,
    _canonical_cash_drawer_adjustment_intent_json,
    _normalize_cash_drawer_adjustment_intent_payload,
)
from nkt_operations.nkt_store_operations.features.cashier.internal.primary_cash_drawer_adjustment_intent import (
    PRIMARY_JOURNAL,
)

MATERIALIZATION_VERSION = "C15C.10F-R4"
MATERIALIZATION_ACK_NAMESPACE = uuid.UUID("c86f43e8-12e5-4b5b-b142-7a7d113d8c15")
TOLERANCE = 0.000001


def _require_primary():
    if _runtime_role() != "Primary":
        raise frappe.PermissionError(
            "Cash Drawer Adjustment materialization is Primary-only."
        )


def _uuid(value: Any, label: str) -> str:
    try:
        return str(uuid.UUID(str(value)))
    except Exception as exc:
        raise frappe.ValidationError(f"{label} is invalid.") from exc


def _materialization_ack_uuid(
    event_uuid: str,
    payload_hash: str,
    adjustment_name: str,
    movement_name: str,
) -> str:
    event_uuid = _uuid(event_uuid, "Cash Drawer Adjustment Intent UUID")
    payload_hash = str(payload_hash or "").lower()
    if len(payload_hash) != 64 or any(c not in "0123456789abcdef" for c in payload_hash):
        raise frappe.ValidationError("Cash Drawer Adjustment payload hash is invalid.")
    material = "\0".join(
        (
            "NKT Cash Drawer Adjustment Materialization",
            event_uuid,
            payload_hash,
            str(adjustment_name or ""),
            str(movement_name or ""),
        )
    )
    return str(uuid.uuid5(MATERIALIZATION_ACK_NAMESPACE, material))


def _claim_name(event_uuid: str) -> str:
    return "nkt-10f-cash-" + hashlib.sha256(
        f"event:{event_uuid}".encode("utf-8")
    ).hexdigest()[:36]


def _acquire_claim(event_uuid: str):
    name = _claim_name(event_uuid)
    rows = frappe.db.sql("SELECT GET_LOCK(%s,%s)", (name, 30), as_list=True)
    if not rows or int(rows[0][0] or 0) != 1:
        raise frappe.ValidationError(
            "Cash Drawer Adjustment materialization is busy. Safe retry is required."
        )
    state = {"released": False}

    def release_once():
        if state["released"]:
            return
        try:
            frappe.db.sql("SELECT RELEASE_LOCK(%s)", (name,))
        except Exception:
            pass
        state["released"] = True

    frappe.db.after_commit.add(release_once)
    frappe.db.after_rollback.add(release_once)


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
            "Preserved Cash Drawer Adjustment Intent payload JSON is invalid."
        ) from exc

    payload = _normalize_cash_drawer_adjustment_intent_payload(raw)
    canonical = _canonical_cash_drawer_adjustment_intent_json(payload)
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    if digest != str(journal.payload_sha256 or "").lower():
        raise NKTIdempotencyConflict(
            "Preserved Cash Drawer Adjustment payload hash no longer matches."
        )
    if canonical != str(journal.canonical_payload_json or ""):
        raise NKTIdempotencyConflict(
            "Preserved Cash Drawer Adjustment canonical payload has drifted."
        )
    return payload


def _verify_receipt(journal):
    if not frappe.db.exists("NKT Sync Primary Receipt", journal.name):
        raise NKTIdempotencyConflict(
            "Preserved Cash Drawer Adjustment Intent has no durable Primary receipt."
        )
    receipt = frappe.get_doc("NKT Sync Primary Receipt", journal.name)
    if (
        receipt.event_family != "NKT Cash Drawer Adjustment Intent"
        or str(receipt.payload_sha256 or "") != str(journal.payload_sha256 or "")
        or str(receipt.canonical_doctype or "") != PRIMARY_JOURNAL
        or str(receipt.canonical_name or "") != journal.name
        or receipt.materialization_state != "Cash Drawer Adjustment Intent Preserved"
    ):
        raise NKTIdempotencyConflict(
            "Primary receipt conflicts with preserved Cash Drawer Adjustment Intent."
        )
    return receipt


def _denomination_fields(payload):
    return {
        field: cint(payload["denominations"].get(field) or 0)
        for field in DENOMINATIONS
    }


def _verify_materialized(
    journal,
    payload,
    *,
    adjustment_name=None,
    movement_name=None,
):
    """
    Verify canonical rows before durable journal promotion on creator path, and
    verify via durable journal bindings on replay path.

    First-time materialization must not require bindings that are deliberately
    written only after integrity verification succeeds.
    """
    adjustment_name = str(
        adjustment_name
        if adjustment_name is not None
        else (journal.materialized_adjustment or "")
    )
    movement_name = str(
        movement_name
        if movement_name is not None
        else (journal.materialized_movement or "")
    )
    if not adjustment_name or not movement_name:
        raise NKTIdempotencyConflict(
            "Cash Drawer materialization state is missing canonical bindings."
        )
    if not frappe.db.exists("NKT Cash Drawer Adjustment", adjustment_name):
        raise NKTIdempotencyConflict("Materialized Cash Drawer Adjustment is missing.")
    if not frappe.db.exists("NKT Cashier Movement", movement_name):
        raise NKTIdempotencyConflict("Materialized Cashier Movement is missing.")

    adjustment = frappe.get_doc("NKT Cash Drawer Adjustment", adjustment_name)
    movement = frappe.get_doc("NKT Cashier Movement", movement_name)
    resolved_shift = resolve_primary_cashier_shift(payload["cashier_shift"])

    if (
        int(adjustment.docstatus or 0) != 1
        or str(adjustment.status or "") != "Posted"
        or str(adjustment.cashier_movement or "") != movement.name
        or str(adjustment.cashier_shift or "") != resolved_shift
        or str(adjustment.adjustment_type or "") != payload["adjustment_type"]
        or str(adjustment.direction or "") != payload["direction"]
        or abs(flt(adjustment.amount) - flt(payload["amount"])) > TOLERANCE
    ):
        raise NKTIdempotencyConflict(
            "Materialized Cash Drawer Adjustment conflicts with preserved intent."
        )

    if (
        int(movement.docstatus or 0) != 1
        or str(movement.status or "") != "Posted"
        or str(movement.cashier_shift or "") != resolved_shift
        or str(movement.movement_type or "") != payload["adjustment_type"]
        or str(movement.direction or "") != payload["direction"]
        or str(movement.payment_method or "") != "Cash"
        or int(movement.affects_cash_drawer or 0) != 1
        or abs(flt(movement.amount) - flt(payload["amount"])) > TOLERANCE
        or str(movement.source_doctype or "") != "NKT Cash Drawer Adjustment"
        or str(movement.source_name or "") != adjustment.name
        or bool(str(movement.source_row or "").strip())
    ):
        raise NKTIdempotencyConflict(
            "Materialized Cashier Movement conflicts with preserved intent."
        )

    return adjustment, movement


def _build_ack(journal, payload, adjustment, movement):
    ack_uuid = _materialization_ack_uuid(
        journal.name,
        journal.payload_sha256,
        adjustment.name,
        movement.name,
    )
    return {
        "event_uuid": journal.name,
        "event_family": "NKT Cash Drawer Adjustment Intent",
        "payload_sha256": str(journal.payload_sha256 or "").lower(),
        "primary_ack_uuid": str(
            frappe.db.get_value("NKT Sync Primary Receipt", journal.name, "primary_ack_uuid")
            or ""
        ),
        "materialization_ack_uuid": ack_uuid,
        "cash_drawer_adjustment": adjustment.name,
        "cashier_movement": movement.name,
        "cashier_shift": payload["cashier_shift"],
        "adjustment_type": payload["adjustment_type"],
        "direction": payload["direction"],
        "amount": float(f"{flt(payload['amount']):.6f}"),
        "posted_on": str(adjustment.posted_on or ""),
    }


def materialize_cash_drawer_adjustment(event_uuid: str) -> Dict[str, Any]:
    _require_primary()
    event_uuid = _uuid(event_uuid, "Cash Drawer Adjustment Intent UUID")

    # Serialize before first mutable journal read, matching the proven 10E fix.
    _acquire_claim(event_uuid)
    journal = _journal_for_update(event_uuid)
    if not journal:
        raise frappe.DoesNotExistError(
            "Primary Cash Drawer Adjustment Intent is unavailable."
        )

    if (
        journal.event_family != "NKT Cash Drawer Adjustment Intent"
        or journal.preservation_state != "Preserved"
    ):
        raise NKTIdempotencyConflict(
            "Primary journal is not a preserved Cash Drawer Adjustment Intent."
        )

    payload = _payload_from_journal(journal)
    _verify_receipt(journal)

    if journal.downstream_state == "Cash Drawer Materialized":
        adjustment, movement = _verify_materialized(journal, payload)
        ack = _build_ack(journal, payload, adjustment, movement)
        if str(journal.materialization_ack_uuid or "") != ack["materialization_ack_uuid"]:
            raise NKTIdempotencyConflict(
                "Stored Cash Drawer materialization ACK binding conflicts with replay."
            )
        return {**ack, "replay": True}

    if journal.downstream_state != "Awaiting Cash Drawer Materialization":
        raise frappe.ValidationError(
            "Cash Drawer Adjustment Intent is not ready for materialization."
        )

    resolved_shift = resolve_primary_cashier_shift(payload["cashier_shift"])
    adjustment = frappe.get_doc(
        {
            "doctype": "NKT Cash Drawer Adjustment",
            "cashier_shift": resolved_shift,
            "adjustment_type": payload["adjustment_type"],
            "amount": payload["amount"],
            "party_name": payload["party_name"],
            "purpose": payload["purpose"],
            "supporting_document": payload["supporting_document"],
            "posting_datetime": get_datetime(journal.settled_at),
            **_denomination_fields(payload),
        }
    )
    setattr(
        adjustment.flags,
        "nkt_c15c10f_preserved_cash_drawer_intent",
        event_uuid,
    )
    adjustment.flags.ignore_permissions = True
    adjustment.insert(ignore_permissions=True)

    setattr(
        adjustment.flags,
        "nkt_c15c10f_preserved_cash_drawer_intent",
        event_uuid,
    )
    adjustment.flags.ignore_permissions = True
    adjustment.submit()

    adjustment.reload()
    if not adjustment.cashier_movement:
        raise NKTIdempotencyConflict(
            "Submitted Cash Drawer Adjustment did not create its Cashier Movement."
        )
    movement = frappe.get_doc("NKT Cashier Movement", adjustment.cashier_movement)

    # Server-only preserved flag must have carried physical event time/operator.
    if str(adjustment.posted_by or "") != str(journal.origin_user or ""):
        raise NKTIdempotencyConflict(
            "Materialized Cash Drawer Adjustment lost the Edge operator identity."
        )
    if get_datetime(adjustment.posted_on) != get_datetime(journal.settled_at):
        raise NKTIdempotencyConflict(
            "Materialized Cash Drawer Adjustment lost the Edge physical event time."
        )
    if get_datetime(movement.posting_datetime) != get_datetime(journal.settled_at):
        raise NKTIdempotencyConflict(
            "Materialized Cashier Movement lost the Edge physical event time."
        )

    adjustment, movement = _verify_materialized(
        journal,
        payload,
        adjustment_name=adjustment.name,
        movement_name=movement.name,
    )
    ack = _build_ack(journal, payload, adjustment, movement)

    frappe.db.set_value(
        PRIMARY_JOURNAL,
        journal.name,
        {
            "downstream_state": "Cash Drawer Materialized",
            "materialized_adjustment": adjustment.name,
            "materialized_movement": movement.name,
            "materialized_at": now(),
            "materialization_ack_uuid": ack["materialization_ack_uuid"],
        },
        update_modified=False,
    )

    return {**ack, "replay": False}


@frappe.whitelist()
def materialize(event_uuid: str):
    return materialize_cash_drawer_adjustment(event_uuid)
