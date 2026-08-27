from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any, Dict, Iterable, Optional

import frappe
from frappe.utils import flt, getdate, get_datetime, now

from nkt_operations.nkt_store_operations.features.returns.internal.return_exchange_offline_intent import FAMILY
from nkt_operations.nkt_store_operations.features.offline_edge.safe_sync import NKTIdempotencyConflict
from nkt_operations.nkt_store_operations.features.offline_edge.sync_transport import _runtime_role

FOUNDATION_VERSION = "C15C.10I-R12"
PRIMARY_JOURNAL = "NKT Primary Return Exchange Intent"
MATERIALIZATION_ACK_NAMESPACE = uuid.UUID("fdd0d243-9baa-45d9-a55f-51903dc1cb35")
TOLERANCE = 0.000001

PARENT = "NKT Edge Return Exchange Projection"
STOCK = "NKT Edge Return Exchange Stock Projection"
CASH = "NKT Edge Return Exchange Cash Projection"
NEW_ITEM = "NKT Edge Return Exchange New Item Projection"

MATERIALIZATION_SIGNED_FIELDS = (
    "event_uuid",
    "event_family",
    "payload_sha256",
    "materialization_ack_uuid",
    "side",
    "canonical_declaration",
    "business_date",
    "cashier_shift",
    "new_cashier_sale",
    "new_customer_order",
    "return_stock_entry",
    "account_adjustment_record",
    "customer_credit_record",
    "refund_movement",
)

EFFECT_FIELDS = (
    "new_cashier_sale",
    "new_customer_order",
    "return_stock_entry",
    "account_adjustment_record",
    "customer_credit_record",
    "refund_movement",
)


def _canonical_json(value: Dict[str, Any]) -> str:
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


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _signed_ack_json(ack: Dict[str, Any]) -> str:
    return _canonical_json({field: ack.get(field) for field in MATERIALIZATION_SIGNED_FIELDS})


def _effects(ack: Dict[str, Any]) -> Dict[str, Any]:
    return {field: ack.get(field) for field in EFFECT_FIELDS}


def _expected_materialization_ack_uuid(ack: Dict[str, Any]) -> str:
    material = (
        "NKT Return Exchange Canonical Materialization"
        + "\0" + str(ack.get("event_uuid") or "")
        + "\0" + str(ack.get("payload_sha256") or "")
        + "\0" + str(ack.get("canonical_declaration") or "")
        + "\0" + _canonical_json(_effects(ack))
    )
    return str(uuid.uuid5(MATERIALIZATION_ACK_NAMESPACE, material))


def _validate_materialization_ack(ack: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(ack, dict):
        raise frappe.ValidationError("Return/Exchange materialization ACK is invalid.")

    event_uuid = _uuid(ack.get("event_uuid"), "Event UUID")
    ack_uuid = _uuid(ack.get("materialization_ack_uuid"), "Materialization ACK UUID")
    payload_hash = str(ack.get("payload_sha256") or "").lower()
    ack_hash = str(ack.get("materialization_ack_sha256") or "").lower()

    if ack.get("committed") is not True:
        raise frappe.ValidationError("Return/Exchange materialization ACK is not committed.")
    if ack.get("materialization_state") != "Return Exchange Materialized":
        raise frappe.ValidationError("Return/Exchange materialization ACK state is invalid.")
    if ack.get("event_family") != FAMILY:
        raise frappe.ValidationError("Return/Exchange materialization ACK family is invalid.")
    if ack.get("side") not in {"Cashier", "Encoder"}:
        raise frappe.ValidationError("Return/Exchange materialization ACK side is invalid.")
    if not str(ack.get("canonical_declaration") or "").strip():
        raise frappe.ValidationError(
            "Return/Exchange materialization ACK canonical Declaration is required."
        )
    if len(payload_hash) != 64 or any(c not in "0123456789abcdef" for c in payload_hash):
        raise frappe.ValidationError("Return/Exchange materialization payload hash is invalid.")
    if len(ack_hash) != 64 or any(c not in "0123456789abcdef" for c in ack_hash):
        raise frappe.ValidationError("Return/Exchange materialization ACK hash is invalid.")

    expected_hash = _sha256(_signed_ack_json(ack))
    if ack_hash != expected_hash:
        raise NKTIdempotencyConflict(
            "Return/Exchange materialization ACK hash conflicts with signed content."
        )

    expected_uuid = _expected_materialization_ack_uuid(ack)
    if ack_uuid != expected_uuid:
        raise NKTIdempotencyConflict(
            "Return/Exchange materialization ACK UUID conflicts with canonical bindings."
        )

    return {
        "event_uuid": event_uuid,
        "materialization_ack_uuid": ack_uuid,
        "materialization_ack_sha256": ack_hash,
        "payload_sha256": payload_hash,
    }


def _projection_rows(event_uuid: str):
    parent_rows = frappe.get_all(
        PARENT,
        filters={"event_uuid": event_uuid},
        fields=[
            "name","event_uuid","side","company","customer","business_date",
            "cashier_shift","old_cashier_sale","old_customer_order",
            "projected_customer_credit","projected_account_adjustment",
            "projection_state","primary_ack_uuid","materialization_ack_uuid",
            "materialization_ack_sha256","primary_declaration",
            "primary_materialized_at","finalized_at",
        ],
        limit_page_length=10,
    )
    if len(parent_rows) != 1:
        raise frappe.ValidationError(
            "Return/Exchange materialization ACK requires exactly one parent Edge projection."
        )

    stock_rows = frappe.get_all(
        STOCK,
        filters={"event_uuid": event_uuid},
        fields=[
            "name","event_uuid","side","line_no","item_code",
            "original_source_warehouse","return_warehouse","classification",
            "returned_qty","local_saleable_qty","business_date","physical_entry_at",
            "projection_state","primary_ack_uuid","materialization_ack_uuid",
            "materialization_ack_sha256","primary_stock_entry","finalized_at",
        ],
        order_by="line_no asc",
        limit_page_length=500,
    )
    cash_rows = frappe.get_all(
        CASH,
        filters={"event_uuid": event_uuid},
        fields=[
            "name","event_uuid","line_no","company","customer","cashier_shift",
            "movement_kind","direction","payment_method","settlement_amount",
            "card_surcharge","collected_amount","cash_drawer_delta","reference_number",
            "projection_state","primary_ack_uuid","materialization_ack_uuid",
            "materialization_ack_sha256","primary_cashier_movement","finalized_at",
        ],
        order_by="line_no asc",
        limit_page_length=500,
    )
    new_rows = frappe.get_all(
        NEW_ITEM,
        filters={"event_uuid": event_uuid},
        fields=[
            "name","event_uuid","side","line_no","item_code","quantity","rate",
            "source_warehouse","local_reserved_qty","physical_entry_at",
            "projection_state","primary_ack_uuid","materialization_ack_uuid",
            "materialization_ack_sha256","primary_new_sale","primary_new_order","finalized_at",
        ],
        order_by="line_no asc",
        limit_page_length=500,
    )
    return parent_rows[0], stock_rows, cash_rows, new_rows


def _validate_event_and_projection_bindings(
    ack: Dict[str, Any],
    validated: Dict[str, Any],
    event,
    parent,
    rows: Iterable[Any],
):
    if event.event_family != FAMILY:
        raise NKTIdempotencyConflict(
            "Return/Exchange materialization ACK family conflicts with Edge event."
        )
    if str(event.payload_sha256 or "").lower() != validated["payload_sha256"]:
        raise NKTIdempotencyConflict(
            "Return/Exchange materialization ACK payload conflicts with Edge event."
        )
    if (
        event.sync_state != "Committed at Primary"
        or str(event.canonical_doctype or "") != PRIMARY_JOURNAL
        or str(event.canonical_name or "") != validated["event_uuid"]
        or not str(event.primary_ack_uuid or "").strip()
    ):
        raise NKTIdempotencyConflict(
            "Return/Exchange Edge event is not safely preserved at Primary."
        )
    if frappe.db.exists("NKT Sync Pending Payload", validated["event_uuid"]):
        raise NKTIdempotencyConflict(
            "Return/Exchange materialization ACK cannot rebase while a pending payload remains."
        )

    if str(parent.side or "") != str(ack.get("side") or ""):
        raise NKTIdempotencyConflict("Return/Exchange materialization ACK side conflicts.")
    if str(parent.business_date) != str(getdate(ack.get("business_date"))):
        raise NKTIdempotencyConflict(
            "Return/Exchange materialization ACK business date conflicts."
        )
    if str(parent.cashier_shift or "") != str(ack.get("cashier_shift") or ""):
        raise NKTIdempotencyConflict(
            "Return/Exchange materialization ACK Cashier Shift conflicts."
        )

    for row in [parent, *list(rows)]:
        if str(row.primary_ack_uuid or "") != str(event.primary_ack_uuid or ""):
            raise NKTIdempotencyConflict(
                f"{row.get('doctype') or 'Return/Exchange projection'} lost its Primary preservation ACK."
            )
        if row.projection_state not in (
            "Primary Preserved", "Primary Materialized", "Finalized"
        ):
            raise NKTIdempotencyConflict(
                "Return/Exchange projection is not eligible for canonical rebase."
            )
        bound_uuid = str(row.materialization_ack_uuid or "").strip()
        bound_hash = str(row.materialization_ack_sha256 or "").lower()
        if bound_uuid and bound_uuid != validated["materialization_ack_uuid"]:
            raise NKTIdempotencyConflict(
                "Return/Exchange projection already has a different materialization ACK UUID."
            )
        if bound_hash and bound_hash != validated["materialization_ack_sha256"]:
            raise NKTIdempotencyConflict(
                "Return/Exchange projection already has a different materialization ACK hash."
            )


def _declaration_evidence(ack, event, parent):
    name = str(ack.get("canonical_declaration") or "").strip()
    if not frappe.db.exists("NKT Return Exchange Declaration", name):
        return {"visible": False, "reason": "canonical_declaration_not_local"}

    doc = frappe.get_doc("NKT Return Exchange Declaration", name)
    checks = {
        "event_uuid": (
            str(doc.get("custom_nkt_offline_event_uuid") or "")
            == str(event.name)
        ),
        "payload_sha256": (
            str(doc.get("custom_nkt_offline_payload_sha256") or "").lower()
            == str(event.payload_sha256 or "").lower()
        ),
        "side": str(doc.side or "") == str(parent.side or ""),
        "company": str(doc.company or "") == str(parent.company or ""),
        "customer": str(doc.customer or "") == str(parent.customer or ""),
        "old_cashier_sale": (
            str(doc.old_cashier_sale or "") == str(parent.old_cashier_sale or "")
        ),
        "old_customer_order": (
            str(doc.old_customer_order or "") == str(parent.old_customer_order or "")
        ),
        "business_date": str(doc.business_date) == str(parent.business_date),
        "cashier_shift": (
            str(doc.get("custom_nkt_offline_cashier_shift") or "")
            == str(parent.cashier_shift or "")
        ),
        "submitted": int(doc.docstatus or 0) == 1,
        "posted": str(doc.posting_status or "") == "Posted",
    }
    bad = [key for key, ok in checks.items() if not ok]
    if bad:
        raise NKTIdempotencyConflict(
            "Local canonical Return/Exchange Declaration conflicts with Edge projection: "
            + ", ".join(bad)
        )
    return {"visible": True, "declaration": name}


def _stock_entry_base(ack):
    name = str(ack.get("return_stock_entry") or "").strip()
    if not name:
        return None
    if not frappe.db.exists("Stock Entry", name):
        return {"visible": False, "reason": "return_stock_entry_not_local"}
    doc = frappe.get_doc("Stock Entry", name)
    if (
        int(doc.docstatus or 0) != 1
        or str(doc.get("custom_nkt_return_exchange_declaration") or "")
        != str(ack.get("canonical_declaration") or "")
        or str(doc.get("custom_nkt_return_exchange_kind") or "")
        != "Customer Return Receipt"
    ):
        raise NKTIdempotencyConflict(
            "Local canonical Return Stock Entry conflicts with materialization ACK."
        )
    return {"visible": True, "stock_entry": name}


def _stock_evidence(row, ack, declaration_visible):
    if not declaration_visible:
        return {"visible": False, "reason": "canonical_declaration_not_local"}

    # Cashier-side stock rows and rejected Encoder rows carry no normal
    # saleable-stock projection; the canonical Declaration itself is enough.
    if row.side != "Encoder" or flt(row.local_saleable_qty) <= TOLERANCE:
        if row.side == "Encoder" and row.classification != "Rejected":
            base = _stock_entry_base(ack)
            if not base or not base.get("visible"):
                return base or {"visible": False, "reason": "return_stock_entry_not_local"}
            return base
        return {"visible": True, "reason": "no_local_saleable_stock_delta"}

    base = _stock_entry_base(ack)
    if not base or not base.get("visible"):
        return base or {"visible": False, "reason": "return_stock_entry_not_local"}

    actual = flt(
        frappe.db.sql(
            """
            SELECT COALESCE(SUM(actual_qty),0)
            FROM `tabStock Ledger Entry`
            WHERE voucher_type='Stock Entry'
              AND voucher_no=%s
              AND item_code=%s
              AND warehouse=%s
              AND is_cancelled=0
            """,
            (
                ack["return_stock_entry"],
                row.item_code,
                row.return_warehouse,
            ),
        )[0][0]
    )
    if abs(actual - flt(row.local_saleable_qty)) > TOLERANCE:
        return {
            "visible": False,
            "reason": "return_stock_ledger_effect_not_local",
            "actual_qty": actual,
            "expected_qty": flt(row.local_saleable_qty),
        }
    return {
        "visible": True,
        "stock_entry": ack["return_stock_entry"],
        "stock_ledger_qty": actual,
    }


def _financial_evidence(parent, ack, declaration_visible):
    if not declaration_visible:
        return {"visible": False, "reason": "canonical_declaration_not_local"}

    result = {"visible": True}

    projected_credit = flt(parent.projected_customer_credit)
    if projected_credit > TOLERANCE:
        name = str(ack.get("customer_credit_record") or "").strip()
        if not name or not frappe.db.exists("NKT Customer Advance", name):
            return {"visible": False, "reason": "customer_credit_not_local"}
        doc = frappe.get_doc("NKT Customer Advance", name)
        if (
            int(doc.docstatus or 0) != 1
            or str(doc.get("custom_nkt_credit_origin") or "") != "Return Credit"
            or str(doc.get("custom_nkt_source_return_exchange") or "")
            != str(ack.get("canonical_declaration") or "")
            or abs(flt(doc.get("original_advance_amount")) - projected_credit) > 0.005
        ):
            raise NKTIdempotencyConflict(
                "Local canonical Return Credit conflicts with Edge projection."
            )
        result["customer_credit_record"] = name
        result["customer_credit_original_amount"] = flt(doc.get("original_advance_amount"))

    projected_adjustment = flt(parent.projected_account_adjustment)
    if projected_adjustment > TOLERANCE:
        name = str(ack.get("account_adjustment_record") or "").strip()
        if not name or not frappe.db.exists("NKT Return Account Adjustment", name):
            return {"visible": False, "reason": "account_adjustment_not_local"}
        doc = frappe.get_doc("NKT Return Account Adjustment", name)
        if (
            str(doc.get("return_exchange_declaration") or "")
            != str(ack.get("canonical_declaration") or "")
            or abs(flt(doc.get("amount")) - projected_adjustment) > 0.005
        ):
            raise NKTIdempotencyConflict(
                "Local canonical Return Account Adjustment conflicts with Edge projection."
            )
        result["account_adjustment_record"] = name
        result["account_adjustment_amount"] = flt(doc.get("amount"))

    return result


def _matching_item(doc, row):
    items = list(doc.get("items") or [])
    idx = int(row.line_no or 0) - 1
    if idx < 0 or idx >= len(items):
        return None
    item = items[idx]
    item_code = str(item.get("item") or item.get("item_code") or "")
    qty = flt(item.get("quantity") or item.get("qty"))
    rate = flt(item.get("final_rate") or item.get("rate"))
    warehouse = str(item.get("source_warehouse") or item.get("warehouse") or "")
    if (
        item_code != str(row.item_code or "")
        or abs(qty - flt(row.quantity)) > TOLERANCE
        or abs(rate - flt(row.rate)) > 0.005
    ):
        raise NKTIdempotencyConflict(
            "Canonical replacement item conflicts with Edge NEW-item projection."
        )
    return item, warehouse


def _encoder_order_fulfillment_evidence(order, row, item, warehouse):
    if str(warehouse or "") != str(row.source_warehouse or ""):
        raise NKTIdempotencyConflict(
            "Canonical replacement Customer Order warehouse conflicts with Edge projection."
        )

    immediate = bool(
        frappe.db.get_value(
            "Warehouse",
            row.source_warehouse,
            "custom_nkt_immediate_sale_deduction",
        )
    )
    if immediate:
        stock_entry = str(order.get("custom_nkt_retail_stock_entry") or "").strip()
        if not stock_entry or not frappe.db.exists("Stock Entry", stock_entry):
            return {"visible": False, "reason": "replacement_order_stock_issue_not_local"}
        se = frappe.get_doc("Stock Entry", stock_entry)
        if (
            int(se.docstatus or 0) != 1
            or str(se.get("custom_nkt_customer_order") or "") != order.name
        ):
            raise NKTIdempotencyConflict(
                "Canonical replacement Customer Order immediate Stock Entry conflicts."
            )
        actual = flt(
            frappe.db.sql(
                """
                SELECT COALESCE(SUM(actual_qty),0)
                FROM `tabStock Ledger Entry`
                WHERE voucher_type='Stock Entry'
                  AND voucher_no=%s
                  AND item_code=%s
                  AND warehouse=%s
                  AND is_cancelled=0
                """,
                (stock_entry, row.item_code, row.source_warehouse),
            )[0][0]
        )
        if abs(actual + flt(row.local_reserved_qty)) > TOLERANCE:
            return {
                "visible": False,
                "reason": "replacement_order_stock_issue_sle_not_local",
                "actual_qty": actual,
                "expected_outbound_qty": flt(row.local_reserved_qty),
            }
        return {
            "visible": True,
            "fulfillment_kind": "Immediate Store Deduction",
            "stock_entry": stock_entry,
            "stock_ledger_qty": actual,
        }

    sre_name = str(item.get("custom_nkt_stock_reservation_entry") or "").strip()
    if not sre_name or not frappe.db.exists("Stock Reservation Entry", sre_name):
        return {"visible": False, "reason": "replacement_order_reservation_not_local"}
    sre = frappe.get_doc("Stock Reservation Entry", sre_name)
    if (
        int(sre.docstatus or 0) != 1
        or str(sre.voucher_type or "") != "NKT Customer Order"
        or str(sre.voucher_no or "") != order.name
        or str(sre.item_code or "") != str(row.item_code or "")
        or str(sre.warehouse or "") != str(row.source_warehouse or "")
        or flt(sre.reserved_qty) + TOLERANCE < flt(row.local_reserved_qty)
    ):
        raise NKTIdempotencyConflict(
            "Canonical replacement Customer Order stock reservation conflicts."
        )
    return {
        "visible": True,
        "fulfillment_kind": "Stock Reservation Entry",
        "stock_reservation_entry": sre_name,
        "reserved_qty": flt(sre.reserved_qty),
    }


def _new_item_evidence(row, ack, declaration_visible):
    if not declaration_visible:
        return {"visible": False, "reason": "canonical_declaration_not_local"}

    declaration = str(ack.get("canonical_declaration") or "")
    if row.side == "Cashier":
        name = str(ack.get("new_cashier_sale") or "").strip()
        if not name or not frappe.db.exists("NKT Cashier Sale", name):
            return {"visible": False, "reason": "replacement_cashier_sale_not_local"}
        doc = frappe.get_doc("NKT Cashier Sale", name)
        if (
            int(doc.docstatus or 0) != 1
            or str(doc.get("custom_nkt_source_return_entry") or "") != declaration
        ):
            raise NKTIdempotencyConflict(
                "Canonical replacement Cashier Sale conflicts with materialization ACK."
            )
        _matching_item(doc, row)
        return {"visible": True, "new_cashier_sale": name}

    name = str(ack.get("new_customer_order") or "").strip()
    if not name or not frappe.db.exists("NKT Customer Order", name):
        return {"visible": False, "reason": "replacement_customer_order_not_local"}
    doc = frappe.get_doc("NKT Customer Order", name)
    if (
        int(doc.docstatus or 0) != 1
        or str(doc.get("custom_nkt_source_return_entry") or "") != declaration
    ):
        raise NKTIdempotencyConflict(
            "Canonical replacement Customer Order conflicts with materialization ACK."
        )
    matched = _matching_item(doc, row)
    if not matched:
        return {"visible": False, "reason": "replacement_customer_order_row_not_local"}
    item, warehouse = matched
    fulfillment = _encoder_order_fulfillment_evidence(doc, row, item, warehouse)
    return {
        **fulfillment,
        "new_customer_order": name,
    }


def _cash_evidence(row, ack, declaration_visible):
    if not declaration_visible:
        return {"visible": False, "reason": "canonical_declaration_not_local"}

    if row.movement_kind in {
        "Customer Return Refund",
        "Exchange Difference Refunded",
        "Return/Exchange Refund",
    } or row.direction == "Out":
        name = str(ack.get("refund_movement") or "").strip()
        if not name or not frappe.db.exists("NKT Cashier Movement", name):
            return {"visible": False, "reason": "refund_cashier_movement_not_local"}
        doc = frappe.get_doc("NKT Cashier Movement", name)
        if (
            int(doc.docstatus or 0) != 1
            or str(doc.source_doctype or "") != "NKT Return Exchange Declaration"
            or str(doc.source_name or "") != str(ack.get("canonical_declaration") or "")
            or str(doc.source_row or "") != "refund"
            or str(doc.direction or "") != "Out"
            or str(doc.cashier_shift or "") != str(row.cashier_shift or "")
            or abs(flt(doc.settlement_amount or doc.amount) - flt(row.settlement_amount)) > 0.005
        ):
            raise NKTIdempotencyConflict(
                "Canonical Return/Exchange refund Cashier Movement conflicts."
            )
        return {"visible": True, "cashier_movement": name}

    sale_name = str(ack.get("new_cashier_sale") or "").strip()
    if not sale_name or not frappe.db.exists("NKT Cashier Sale", sale_name):
        return {"visible": False, "reason": "replacement_cashier_sale_not_local"}
    sale = frappe.get_doc("NKT Cashier Sale", sale_name)
    idx = int(row.line_no or 0) - 1
    payments = list(sale.get("payments") or [])
    if idx < 0 or idx >= len(payments):
        return {"visible": False, "reason": "replacement_cashier_payment_row_not_local"}
    payment = payments[idx]
    if (
        str(payment.payment_method or "") != str(row.payment_method or "")
        or abs(flt(payment.amount) - flt(row.settlement_amount)) > 0.005
        or abs(flt(payment.get("card_surcharge")) - flt(row.card_surcharge)) > 0.005
    ):
        raise NKTIdempotencyConflict(
            "Canonical replacement Cashier Sale payment conflicts with Edge cash projection."
        )

    movement = frappe.db.get_value(
        "NKT Cashier Movement",
        {
            "source_doctype": "NKT Cashier Sale",
            "source_name": sale_name,
            "source_row": payment.name,
            "docstatus": ["!=", 2],
        },
        "name",
    )
    if not movement:
        return {"visible": False, "reason": "replacement_cashier_payment_movement_not_local"}
    mv = frappe.get_doc("NKT Cashier Movement", movement)
    if (
        str(mv.direction or "") != "In"
        or str(mv.payment_method or "") != str(row.payment_method or "")
        or abs(flt(mv.settlement_amount or 0) - flt(row.settlement_amount)) > 0.005
        or abs(flt(mv.get("card_surcharge")) - flt(row.card_surcharge)) > 0.005
    ):
        raise NKTIdempotencyConflict(
            "Canonical replacement Cashier payment movement conflicts with Edge projection."
        )
    return {"visible": True, "cashier_movement": movement}


def projection_state_for_evidence(visible: bool) -> str:
    return "Finalized" if visible else "Primary Materialized"


def _bind_common(validated):
    return {
        "materialization_ack_uuid": validated["materialization_ack_uuid"],
        "materialization_ack_sha256": validated["materialization_ack_sha256"],
    }


def _set_if_changed(doctype: str, row, values: Dict[str, Any]) -> bool:
    changed = False
    for field, value in values.items():
        current = row.get(field)
        if field in {"finalized_at", "primary_materialized_at"}:
            if value and not current:
                changed = True
            continue
        if str(current or "") != str(value or ""):
            changed = True
    if changed:
        frappe.db.set_value(
            doctype,
            row.name,
            values,
            update_modified=False,
        )
    return changed


def apply_return_exchange_materialization_ack_at_edge(
    ack: Dict[str, Any],
) -> Dict[str, Any]:
    if _runtime_role() != "Store Edge":
        raise frappe.PermissionError(
            "Return/Exchange materialization ACK application unavailable."
        )

    validated = _validate_materialization_ack(ack)
    event_uuid = validated["event_uuid"]
    if not frappe.db.exists("NKT Sync Event", event_uuid):
        raise frappe.DoesNotExistError("Return/Exchange event is unavailable at Edge.")

    event = frappe.get_doc("NKT Sync Event", event_uuid)
    parent, stock_rows, cash_rows, new_rows = _projection_rows(event_uuid)
    _validate_event_and_projection_bindings(
        ack,
        validated,
        event,
        parent,
        [*stock_rows, *cash_rows, *new_rows],
    )

    declaration = _declaration_evidence(ack, event, parent)
    declaration_visible = bool(declaration.get("visible"))
    parent_evidence = _financial_evidence(parent, ack, declaration_visible)

    stock_evidence = {
        row.name: _stock_evidence(row, ack, declaration_visible)
        for row in stock_rows
    }
    cash_evidence = {
        row.name: _cash_evidence(row, ack, declaration_visible)
        for row in cash_rows
    }
    new_evidence = {
        row.name: _new_item_evidence(row, ack, declaration_visible)
        for row in new_rows
    }

    changed = False
    common = _bind_common(validated)
    materialized_now = now()

    parent_state = projection_state_for_evidence(bool(parent_evidence.get("visible")))
    parent_values = {
        **common,
        "projection_state": parent_state,
        "primary_declaration": ack.get("canonical_declaration"),
        "primary_materialized_at": parent.primary_materialized_at or materialized_now,
    }
    if parent_state == "Finalized":
        parent_values["finalized_at"] = parent.finalized_at or materialized_now
    changed |= _set_if_changed(PARENT, parent, parent_values)

    for row in stock_rows:
        evidence = stock_evidence[row.name]
        state = projection_state_for_evidence(bool(evidence.get("visible")))
        values = {
            **common,
            "projection_state": state,
            "primary_stock_entry": ack.get("return_stock_entry"),
        }
        if state == "Finalized":
            values["finalized_at"] = row.finalized_at or materialized_now
        changed |= _set_if_changed(STOCK, row, values)

    for row in cash_rows:
        evidence = cash_evidence[row.name]
        state = projection_state_for_evidence(bool(evidence.get("visible")))
        values = {
            **common,
            "projection_state": state,
            "primary_cashier_movement": evidence.get("cashier_movement"),
        }
        if state == "Finalized":
            values["finalized_at"] = row.finalized_at or materialized_now
        changed |= _set_if_changed(CASH, row, values)

    for row in new_rows:
        evidence = new_evidence[row.name]
        state = projection_state_for_evidence(bool(evidence.get("visible")))
        values = {
            **common,
            "projection_state": state,
            "primary_new_sale": ack.get("new_cashier_sale"),
            "primary_new_order": ack.get("new_customer_order"),
        }
        if state == "Finalized":
            values["finalized_at"] = row.finalized_at or materialized_now
        changed |= _set_if_changed(NEW_ITEM, row, values)

    # Reload only for final state summary after all per-effect writes.
    parent_now, stock_now, cash_now, new_now = _projection_rows(event_uuid)
    states = {
        "parent": parent_now.projection_state,
        "stock": {row.name: row.projection_state for row in stock_now},
        "cash": {row.name: row.projection_state for row in cash_now},
        "new_item": {row.name: row.projection_state for row in new_now},
    }
    all_states = [
        parent_now.projection_state,
        *[row.projection_state for row in stock_now],
        *[row.projection_state for row in cash_now],
        *[row.projection_state for row in new_now],
    ]
    fully_finalized = all(state == "Finalized" for state in all_states)

    return {
        "event_uuid": event_uuid,
        "event_family": FAMILY,
        "materialization_ack_uuid": validated["materialization_ack_uuid"],
        "materialization_ack_sha256": validated["materialization_ack_sha256"],
        "canonical_declaration_visible": declaration_visible,
        "canonical_evidence": {
            "declaration": declaration,
            "parent_financial": parent_evidence,
            "stock": stock_evidence,
            "cash": cash_evidence,
            "new_item": new_evidence,
        },
        "projection_states": states,
        "event_fully_finalized": fully_finalized,
        "temporary_projection_remains_for_missing_evidence": any(
            state == "Primary Materialized" for state in all_states
        ),
        "replay": not changed,
    }


def foundation_status():
    return {
        "foundation_version": FOUNDATION_VERSION,
        "edge_materialization_ack_rebase_enabled": True,
        "materialization_ack_hash_and_uuid_validated": True,
        "projection_finalization_is_per_effect": True,
        "missing_canonical_evidence_keeps_projection_active": True,
        "parent_credit_finalization_requires_local_return_credit": True,
        "parent_account_adjustment_finalization_requires_local_adjustment": True,
        "saleable_stock_finalization_requires_local_stock_ledger_evidence": True,
        "encoder_new_order_finalization_requires_local_fulfillment_or_reservation": True,
        "cash_finalization_requires_local_cashier_movement": True,
        "cashier_new_sale_finalization_requires_local_submitted_sale": True,
        "canonical_posting_at_edge_enabled": False,
        "controlled_reversal_offline_enabled": False,
    }


@frappe.whitelist()
def apply_return_exchange_materialization_ack(ack: Dict[str, Any]):
    if isinstance(ack, str):
        ack = frappe.parse_json(ack)
    return apply_return_exchange_materialization_ack_at_edge(ack)
