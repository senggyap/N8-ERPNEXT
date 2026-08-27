from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from typing import Any, Dict, Iterable, List

import frappe
from frappe.utils import flt, getdate, now

from nkt_operations.nkt_store_operations.features.payments_accounts.card_surcharge import normalize_payment_method
from nkt_operations.nkt_store_operations.features.payments_accounts.internal.cashier_tender_intent import (
    _canonical_cashier_tender_intent_json,
    _normalize_cashier_tender_intent_payload,
)
from nkt_operations.nkt_store_operations.features.payments_accounts.internal.encoder_settlement_intent import (
    _canonical_encoder_settlement_intent_json,
    _normalize_encoder_settlement_intent_payload,
)
from nkt_operations.nkt_store_operations.features.offline_edge.safe_sync import NKTIdempotencyConflict
from nkt_operations.nkt_store_operations.features.offline_edge.sync_transport import _runtime_role

TENDER_JOURNAL = "NKT Primary Cashier Tender Intent"
ENCODER_JOURNAL = "NKT Primary Encoder Settlement Intent"
TOLERANCE = 0.005

TENDER_READY = {
    "Cashier Movement Materialized - Awaiting Receivable",
    "Cashier Movement Not Required - Awaiting Receivable",
}
TENDER_FINAL = {
    "Receivable Materialized - Awaiting Warehouse/Stock",
    "Receivable Not Required - Awaiting Warehouse/Stock",
}
ENCODER_FINAL = {"Receivable Materialized", "Receivable Not Required"}


def _require_primary():
    if _runtime_role() != "Primary":
        raise frappe.PermissionError("C15C.10D account-effect materializer is Primary-only.")


def _norm_ref(value):
    return "".join(str(value or "").strip().lower().split())


def _payment_signature(rows: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    grouped = defaultdict(float)
    detailed = []
    for row in rows or []:
        method = normalize_payment_method(row.get("payment_method") or row.get("method"))
        amount = flt(row.get("amount"))
        if not method or amount <= TOLERANCE:
            continue
        if method in {"Cash", "Account"}:
            grouped[method] += amount
        else:
            detailed.append({
                "method": method,
                "amount": round(amount, 2),
                "reference": _norm_ref(row.get("check_number") or row.get("reference_number") or row.get("reference")),
            })
    result=[{"method":m,"amount":round(a,2),"reference":""} for m,a in sorted(grouped.items())]
    result.extend(sorted(detailed,key=lambda x:(x["method"],x["reference"],x["amount"])))
    return result


def _tender_basket(rows):
    grouped=defaultdict(float)
    for row in rows or []:
        grouped[(str(row.get("item_code") or ""),round(flt(row.get("rate")),4))]+=flt(row.get("qty"))
    return sorted((k[0],k[1],round(v,6)) for k,v in grouped.items())


def _order_basket(order):
    grouped=defaultdict(float)
    for row in order.get("items") or []:
        rate=flt(row.get("final_rate")) or (flt(row.get("standard_rate"))+flt(row.get("price_adjustment")))
        grouped[(str(row.get("item") or ""),round(rate,4))]+=flt(row.get("quantity"))
    return sorted((k[0],k[1],round(v,6)) for k,v in grouped.items())


def _warehouse_tender(rows):
    grouped=defaultdict(float)
    for row in rows or []:
        grouped[(str(row.get("item_code") or ""),round(flt(row.get("rate")),4),str(row.get("warehouse") or ""))]+=flt(row.get("qty"))
    return sorted((k[0],k[1],k[2],round(v,6)) for k,v in grouped.items())


def _warehouse_order(order):
    grouped=defaultdict(float)
    for row in order.get("items") or []:
        rate=flt(row.get("final_rate")) or (flt(row.get("standard_rate"))+flt(row.get("price_adjustment")))
        grouped[(str(row.get("item") or ""),round(rate,4),str(row.get("source_warehouse") or ""))]+=flt(row.get("quantity"))
    return sorted((k[0],k[1],k[2],round(v,6)) for k,v in grouped.items())


def _lock_name(kind, identity):
    return "nkt-10d-account-"+hashlib.sha256(f"{kind}:{identity}".encode()).hexdigest()[:36]


def _acquire_locks(*names):
    acquired=[]
    try:
        for name in sorted(set(names)):
            rows=frappe.db.sql("SELECT GET_LOCK(%s,%s)",(name,30),as_list=True)
            if not rows or int(rows[0][0] or 0)!=1:
                raise frappe.ValidationError("C15C.10D account promotion is busy. Safe retry is required.")
            acquired.append(name)
    except Exception:
        for name in reversed(acquired):
            try: frappe.db.sql("SELECT RELEASE_LOCK(%s)",(name,))
            except Exception: pass
        raise
    state={"released":False}
    def release_once():
        if state["released"]: return
        for name in reversed(acquired):
            try: frappe.db.sql("SELECT RELEASE_LOCK(%s)",(name,))
            except Exception: pass
        state["released"]=True
    frappe.db.after_commit.add(release_once)
    frappe.db.after_rollback.add(release_once)


def _locked_doc(doctype,name):
    rows=frappe.db.sql(f"SELECT name FROM `tab{doctype}` WHERE name=%s FOR UPDATE",(name,),as_dict=True)
    if not rows: raise frappe.DoesNotExistError(f"{doctype} {name} does not exist.")
    return frappe.get_doc(doctype,name)


def _payload(doc, normalizer, canonicalizer):
    try: raw=json.loads(str(doc.canonical_payload_json or ""))
    except Exception as exc: raise NKTIdempotencyConflict(f"{doc.doctype} canonical payload JSON is invalid.") from exc
    normalized=normalizer(raw)
    if str(doc.canonical_payload_json or "") != canonicalizer(normalized):
        raise NKTIdempotencyConflict(f"{doc.doctype} canonical payload conflicts with normalized truth.")
    return normalized


def _assert_pair(tender,encoder,tp,ep,order):
    if str(tender.company or "")!=str(encoder.company or "") or tp["company"]!=ep["company"]:
        raise NKTIdempotencyConflict("Cashier/Encoder Company declarations do not match.")
    if str(tender.customer or "")!=str(encoder.customer or "") or tp["customer"]!=ep["customer"]:
        raise NKTIdempotencyConflict("Cashier/Encoder Customer declarations do not match.")
    if getdate(tender.business_date)!=getdate(encoder.business_date) or getdate(order.order_date)!=getdate(encoder.business_date):
        raise NKTIdempotencyConflict("Cashier/Encoder business dates do not match.")
    if abs(flt(tp["merchandise_total"])-flt(ep["merchandise_total"]))>0.01:
        raise NKTIdempotencyConflict("Cashier/Encoder merchandise totals do not match.")
    if _tender_basket(tp["items"])!=_order_basket(order):
        raise NKTIdempotencyConflict("Cashier Tender basket does not exactly match the Encoder Customer Order.")
    if _payment_signature(tp["payments"])!=_payment_signature(ep["payments"]):
        raise NKTIdempotencyConflict("Cashier and Encoder payment declarations do not exactly match.")
    if abs(flt(tp["card_surcharge_total"])-flt(ep["declared_card_surcharge_total"]))>0.01:
        raise NKTIdempotencyConflict("Cashier/Encoder Card surcharge declarations do not match.")
    if abs(flt(tp["actual_collected_total"])-flt(ep["declared_total_collected"]))>0.01:
        raise NKTIdempotencyConflict("Cashier/Encoder collected-money totals do not match.")


def _verify_tender_ready(tender,tp):
    if str(tender.downstream_state or "") not in TENDER_READY:
        raise NKTIdempotencyConflict("Cashier Tender is not at the accepted post-10C Receivable boundary.")
    money=[r for r in tp["payments"] if r["payment_method"]!="Account"]
    receipt_name=str(tender.get("payment_receipt") or "").strip()
    if money:
        if not receipt_name or not frappe.db.exists("NKT Payment Receipt",receipt_name):
            raise NKTIdempotencyConflict("Tender money rows are missing their authoritative Payment Receipt.")
        receipt=frappe.get_doc("NKT Payment Receipt",receipt_name)
        if int(receipt.docstatus or 0)!=1 or str(receipt.customer or "")!=tp["customer"]:
            raise NKTIdempotencyConflict("Tender Payment Receipt is not a valid submitted customer receipt.")
    elif receipt_name:
        raise NKTIdempotencyConflict("Account-only Tender unexpectedly carries a Payment Receipt.")


def _materialize_sale(tender,tp):
    existing=str(tender.get("cashier_sale") or "").strip()
    if existing:
        if not frappe.db.exists("NKT Cashier Sale",existing):
            raise NKTIdempotencyConflict("Tender journal points to a missing Cashier Sale.")
        return frappe.get_doc("NKT Cashier Sale",existing),True

    sale=frappe.new_doc("NKT Cashier Sale")
    sale.company=tender.company
    sale.sale_datetime=tender.settled_at
    sale.business_date=tender.business_date
    sale.cashier=tender.origin_user
    sale.cashier_shift=tender.cashier_shift
    sale.settlement_location=tender.settlement_location
    sale.default_warehouse=tender.default_warehouse
    sale.customer=tender.customer
    if tender.get("payment_receipt"): sale.linked_payment_receipt=tender.payment_receipt

    for row in tp["items"]:
        standard=flt(frappe.db.get_value("Item Price",{"item_code":row["item_code"],"price_list":"Standard Selling","selling":1},"price_list_rate"))
        sale.append("items",{
            "item":row["item_code"],
            "quantity":row["qty"],
            "source_warehouse":row["warehouse"],
            "price_adjustment":flt(row["rate"])-standard,
        })

    pmeta=frappe.get_meta("NKT Payment Detail")
    for row in tp["payments"]:
        data={
            "payment_method":row["payment_method"],"amount":row["amount"],
            "cash_tendered":row.get("cash_tendered") if row["payment_method"]=="Cash" else 0,
            "reference_number":row.get("reference_number"),
            "bank_or_provider":row.get("bank_or_provider"),
            "check_number":row.get("check_number"),
            "check_date":row.get("check_date") or None,
            "remarks":row.get("remarks"),
        }
        if pmeta.has_field("card_surcharge"): data["card_surcharge"]=row.get("card_surcharge")
        if pmeta.has_field("collected_amount"): data["collected_amount"]=row.get("collected_amount")
        sale.append("payments",data)

    sale.flags.ignore_permissions=True
    sale.flags.nkt_c15c_preserve_offline_cashier=True
    sale.flags.nkt_c15c_existing_tender_effects=True
    sale.insert(ignore_permissions=True)
    sale.submit()
    sale.reload()
    if int(sale.docstatus or 0)!=1 or str(sale.cashier or "")!=str(tender.origin_user or ""):
        raise NKTIdempotencyConflict("Controlled Cashier Sale lost immutable Tender identity.")

    frappe.db.set_value(TENDER_JOURNAL,tender.name,"cashier_sale",sale.name,update_modified=False)
    if tender.get("payment_receipt"):
        receipt=frappe.get_doc("NKT Payment Receipt",tender.payment_receipt)
        bound=str(receipt.get("source_cashier_sale") or "").strip()
        if bound and bound!=sale.name:
            raise NKTIdempotencyConflict("Payment Receipt is already bound to another Cashier Sale.")
        frappe.db.set_value("NKT Payment Receipt",receipt.name,"source_cashier_sale",sale.name,update_modified=False)
        frappe.db.set_value("NKT Cashier Sale",sale.name,"linked_payment_receipt",receipt.name,update_modified=False)
    return frappe.get_doc("NKT Cashier Sale",sale.name),False


def _verify_final(tender,encoder):
    sale_name=str(tender.get("cashier_sale") or encoder.get("cashier_sale") or "").strip()
    order_name=str(tender.get("matched_customer_order") or encoder.get("customer_order") or "").strip()
    if not sale_name or not order_name:
        raise NKTIdempotencyConflict("Final 10D journals are missing canonical Sale/Order bindings.")
    sale=frappe.get_doc("NKT Cashier Sale",sale_name)
    order=frappe.get_doc("NKT Customer Order",order_name)
    if int(sale.docstatus or 0)!=1 or int(order.docstatus or 0)!=1:
        raise NKTIdempotencyConflict("Final 10D Sale/Order is not submitted.")
    if str(sale.matched_customer_order or "")!=order.name or str(order.matched_cashier_sale or "")!=sale.name:
        raise NKTIdempotencyConflict("Final 10D reconciliation binding is corrupt.")
    receivable=str(tender.get("customer_receivable") or encoder.get("customer_receivable") or "").strip()
    if flt(order.declared_account)>TOLERANCE:
        if not receivable or not frappe.db.exists("NKT Customer Receivable",receivable):
            raise NKTIdempotencyConflict("Final account sale is missing its Customer Receivable.")
        receivable_doc=frappe.get_doc("NKT Customer Receivable",receivable)
        if str(receivable_doc.customer_order or "")!=order.name:
            raise NKTIdempotencyConflict("Final Receivable is bound to another Customer Order.")
        if str(receivable_doc.customer or "")!=str(order.customer or ""):
            raise NKTIdempotencyConflict("Final Receivable is bound to another Customer.")
        if abs(flt(receivable_doc.original_amount)-flt(order.declared_account))>0.01:
            raise NKTIdempotencyConflict(
                "Final Receivable principal conflicts with the immutable Encoder Account declaration."
            )
    elif receivable:
        raise NKTIdempotencyConflict("Non-account sale unexpectedly carries a Customer Receivable.")
    return {"cashier_sale":sale.name,"customer_order":order.name,"customer_receivable":receivable or None,"replay":True}


def materialize_account_effects(encoder_settlement_event_uuid: str,cashier_tender_event_uuid: str)->Dict[str,Any]:
    _require_primary()
    ee=str(encoder_settlement_event_uuid or "").strip()
    te=str(cashier_tender_event_uuid or "").strip()
    if not ee or not te: raise frappe.ValidationError("Both immutable event UUIDs are required.")
    _acquire_locks(_lock_name("encoder",ee),_lock_name("tender",te))

    tender=_locked_doc(TENDER_JOURNAL,te)
    encoder=_locked_doc(ENCODER_JOURNAL,ee)

    if str(tender.downstream_state or "") in TENDER_FINAL or str(encoder.downstream_state or "") in ENCODER_FINAL:
        if not (str(tender.downstream_state or "") in TENDER_FINAL and str(encoder.downstream_state or "") in ENCODER_FINAL):
            raise NKTIdempotencyConflict("10D downstream journals are only partially finalized.")
        return _verify_final(tender,encoder)

    if str(encoder.downstream_state or "")!="Awaiting Cashier Countercheck":
        raise NKTIdempotencyConflict("Encoder Settlement is not awaiting Cashier countercheck.")

    tp=_payload(tender,_normalize_cashier_tender_intent_payload,_canonical_cashier_tender_intent_json)
    ep=_payload(encoder,_normalize_encoder_settlement_intent_payload,_canonical_encoder_settlement_intent_json)
    _verify_tender_ready(tender,tp)

    order=frappe.get_doc("NKT Customer Order",encoder.customer_order)
    if int(order.docstatus or 0)!=0:
        raise NKTIdempotencyConflict("10D account promotion requires the canonical Encoder Customer Order Draft.")
    _assert_pair(tender,encoder,tp,ep,order)

    other=frappe.db.get_value(ENCODER_JOURNAL,{"matched_cashier_tender_event":tender.name,"name":["!=",encoder.name]},"name")
    if other: raise NKTIdempotencyConflict("Cashier Tender is already bound to another Encoder Settlement.")

    before={dt:frappe.db.count(dt) for dt in ("NKT Payment Receipt","NKT Cashier Movement","NKT Warehouse Release","Stock Entry")}
    sale,sale_replay=_materialize_sale(tender,tp)

    order.reload()
    order.flags.ignore_permissions=True
    order.flags.nkt_c15c_preserve_offline_encoder=True
    order.flags.nkt_c15c_defer_fulfillment=True
    order.submit()
    order.reload()
    sale.reload()

    if int(order.docstatus or 0)!=1 or str(order.matched_cashier_sale or "")!=sale.name:
        raise NKTIdempotencyConflict("Controlled Customer Order did not match the canonical Cashier Sale.")
    if str(sale.matched_customer_order or "")!=order.name:
        raise NKTIdempotencyConflict("Canonical Cashier Sale did not bind back to the Encoder order.")

    receipt_name=str(tender.get("payment_receipt") or "").strip()
    if receipt_name:
        receipt=frappe.get_doc("NKT Payment Receipt",receipt_name)
        if str(receipt.customer_order or "")!=order.name or str(receipt.source_cashier_sale or "")!=sale.name:
            raise NKTIdempotencyConflict("Authoritative Payment Receipt allocation/binding is incomplete.")

    receivable=frappe.db.get_value("NKT Customer Receivable",{"customer_order":order.name},"name")
    if flt(order.declared_account)>TOLERANCE and not receivable:
        raise NKTIdempotencyConflict("Account principal did not materialize a Customer Receivable.")
    if flt(order.declared_account)<=TOLERANCE and receivable:
        raise NKTIdempotencyConflict("Non-account sale unexpectedly created a Customer Receivable.")

    after={dt:frappe.db.count(dt) for dt in before}
    if after["NKT Payment Receipt"]!=before["NKT Payment Receipt"]:
        raise NKTIdempotencyConflict("10D created a duplicate Payment Receipt.")
    if after["NKT Cashier Movement"]!=before["NKT Cashier Movement"]:
        raise NKTIdempotencyConflict("10D created a duplicate Cashier Movement.")
    if after["NKT Warehouse Release"]!=before["NKT Warehouse Release"] or after["Stock Entry"]!=before["Stock Entry"]:
        raise NKTIdempotencyConflict("10D crossed the deferred C15C.10E stock boundary.")

    same_wh=_warehouse_tender(tp["items"])==_warehouse_order(order)
    expected_match="Matched" if same_wh else "Matched with Warehouse Warning"
    if str(order.cashier_reconciliation_status or "")!=expected_match:
        raise NKTIdempotencyConflict("Matching status differs from the accepted online reconciliation rule.")

    tender_final="Receivable Materialized - Awaiting Warehouse/Stock" if receivable else "Receivable Not Required - Awaiting Warehouse/Stock"
    encoder_final="Receivable Materialized" if receivable else "Receivable Not Required"
    stamp=now()
    frappe.db.set_value(TENDER_JOURNAL,tender.name,{
        "downstream_state":tender_final,"cashier_sale":sale.name,
        "matched_customer_order":order.name,"customer_receivable":receivable,
        "account_effect_materialized_at":stamp,
    },update_modified=False)
    frappe.db.set_value(ENCODER_JOURNAL,encoder.name,{
        "downstream_state":encoder_final,"matched_cashier_tender_event":tender.name,
        "cashier_sale":sale.name,"customer_receivable":receivable,
        "account_effect_materialized_at":stamp,
    },update_modified=False)

    return {
        "encoder_settlement_event_uuid":encoder.name,
        "cashier_tender_event_uuid":tender.name,
        "cashier_sale":sale.name,
        "cashier_sale_replay":bool(sale_replay),
        "customer_order":order.name,
        "customer_order_submitted":True,
        "matching_state":expected_match,
        "payment_receipt":receipt_name or None,
        "customer_receivable":receivable,
        "receivable_created":bool(receivable),
        "account_principal":flt(order.declared_account),
        "credit_control_status":order.get("custom_nkt_account_credit_status"),
        "payment_receipt_created":False,
        "cashier_movement_created":False,
        "warehouse_release_created":False,
        "stock_entry_created":False,
        "replay":False,
    }


@frappe.whitelist()
def materialize_account_effects_at_primary(encoder_settlement_event_uuid: str,cashier_tender_event_uuid: str):
    return materialize_account_effects(encoder_settlement_event_uuid,cashier_tender_event_uuid)
