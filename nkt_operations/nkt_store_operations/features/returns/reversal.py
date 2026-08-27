from __future__ import annotations

from contextlib import contextmanager
from types import MethodType
from typing import Any

import frappe
from frappe import _
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields
from frappe.model.delete_doc import get_dynamic_linked_docs, get_linked_docs
from frappe.utils import flt, get_datetime, getdate, now_datetime, nowdate
from nkt_operations.nkt_store_operations.features.payments_accounts.card_surcharge import normalize_payment_method

VERSION = "V2.0C.7.13C.1-PRODUCTION"
DECL = "NKT Return Exchange Declaration"
REVERSAL = "NKT Return Exchange Reversal"
TOLERANCE = 0.005

AUTHORIZED_ROLES = {"NKT ADMINISTRATOR", "NKT OWNER"}


def _has_authority(user=None):
    user = user or frappe.session.user
    if user == "Administrator":
        return True
    return bool(AUTHORIZED_ROLES.intersection(set(frappe.get_roles(user))))


def _assert_authority():
    if not _has_authority():
        frappe.throw(
            _("Only NKT Admin/Owner/System Manager may prepare or execute a posted Return/Exchange reversal."),
            frappe.PermissionError,
        )


def _exists(doctype, name):
    return bool(name and frappe.db.exists(doctype, name))


def _doc(doctype, name):
    return frappe.get_doc(doctype, name) if _exists(doctype, name) else None


def _active_rows(doctype, filters, fields=None):
    if not frappe.db.exists("DocType", doctype):
        return []
    filters = dict(filters or {})
    if frappe.get_meta(doctype).is_submittable:
        filters.setdefault("docstatus", ["!=", 2])
    return frappe.get_all(
        doctype,
        filters=filters,
        fields=fields or ["name"],
        order_by="creation asc",
        limit_page_length=5000,
    )


def _pair_from_declaration(name):
    if not _exists(DECL, name):
        frappe.throw(_("Return/Exchange {0} does not exist.").format(name))

    selected = frappe.get_doc(DECL, name)
    if selected.docstatus != 1 or selected.posting_status != "Posted":
        frappe.throw(
            _("Return/Exchange {0} is not an active Posted declaration.").format(selected.name)
        )

    other = None
    if selected.matched_declaration and _exists(DECL, selected.matched_declaration):
        candidate = frappe.get_doc(DECL, selected.matched_declaration)
        if (
            candidate.docstatus == 1
            and candidate.posting_status == "Posted"
            and candidate.matched_declaration == selected.name
        ):
            other = candidate

    docs = [selected] + ([other] if other else [])
    cashier = next((d for d in docs if d.side == "Cashier"), None)
    encoder = next((d for d in docs if d.side == "Encoder"), None)

    if cashier and encoder:
        unit_type = "Matched Pair"
    elif cashier:
        unit_type = "Cashier Only"
    else:
        unit_type = "Encoder Only"

    return {
        "selected": selected,
        "cashier": cashier,
        "encoder": encoder,
        "unit_type": unit_type,
        "declarations": docs,
    }


def _movement_rows(source_doctype, source_name):
    if not frappe.db.exists("DocType", "NKT Cashier Movement"):
        return []
    return frappe.get_all(
        "NKT Cashier Movement",
        filters={
            "source_doctype": source_doctype,
            "source_name": source_name,
            "docstatus": 1,
        },
        fields=[
            "name", "cashier_shift", "cashier", "settlement_location",
            "movement_type", "direction", "payment_method", "amount",
            "settlement_amount", "card_surcharge", "affects_cash_drawer", "reference_number", "posting_datetime",
        ],
        order_by="creation asc",
        limit_page_length=500,
    )


def _descendant_declarations(new_sale=None, new_order=None, exclude=None):
    filters = {"docstatus": 1}
    rows = frappe.get_all(
        DECL,
        filters=filters,
        fields=[
            "name", "side", "posting_status", "reconciliation_status",
            "old_cashier_sale", "old_customer_order", "matched_declaration",
        ],
        order_by="creation asc",
        limit_page_length=5000,
    )
    exclude = set(exclude or [])
    out = []
    for row in rows:
        if row.name in exclude:
            continue
        if row.posting_status != "Posted":
            continue
        if new_sale and row.old_cashier_sale == new_sale:
            out.append(row)
        elif new_order and row.old_customer_order == new_order:
            out.append(row)
    # unique by name
    seen = set()
    return [r for r in out if not (r.name in seen or seen.add(r.name))]


def _advance_applications(advance_name):
    if not advance_name or not frappe.db.exists("DocType", "NKT Customer Advance Application"):
        return []
    return frappe.get_all(
        "NKT Customer Advance Application",
        filters={
            "customer_advance": advance_name,
            "application_status": "Applied",
        },
        fields=[
            "name", "customer_order", "applied_amount", "posting_datetime",
            "custom_nkt_source_return_exchange",
        ],
        order_by="creation asc",
        limit_page_length=5000,
    )


def _warehouse_release_dependencies(order_name):
    if not order_name or not frappe.db.exists("DocType", "NKT Warehouse Release"):
        return []
    meta = frappe.get_meta("NKT Warehouse Release")
    possible = [
        f for f in ("customer_order", "source_customer_order", "order")
        if meta.has_field(f)
    ]
    rows = []
    for field in possible:
        try:
            hit = frappe.get_all(
                "NKT Warehouse Release",
                filters={field: order_name, "docstatus": ["!=", 2]},
                fields=["name", "docstatus", "status", field],
                limit_page_length=5000,
            )
            rows.extend(hit)
        except Exception:
            pass
    seen = set()
    return [r for r in rows if not (r.name in seen or seen.add(r.name))]


def _stock_effect(name, kind, action="Cancel stock posting"):
    if not name:
        return None
    row = frappe.db.get_value(
        "Stock Entry", name,
        ["name", "docstatus", "stock_entry_type", "posting_date", "posting_time", "remarks"],
        as_dict=True,
    )
    if not row:
        return {
            "effect_type": kind,
            "original_doctype": "Stock Entry",
            "original_document": name,
            "action": action,
            "status": "Missing",
            "notes": "Linked Stock Entry no longer exists.",
        }
    return {
        "effect_type": kind,
        "original_doctype": "Stock Entry",
        "original_document": name,
        "action": action,
        "status": "Active" if row.docstatus == 1 else "Already Cancelled",
        "notes": row.remarks or "",
    }



def _active_valuation_corrections(encoder):
    """Find active valuation corrections that explicitly belong to this RX.

    Accepted C7.11 history uses more than one source-link convention:
    - custom_nkt_source_return_exchange / source_return_stock_entry
    - custom_nkt_fraction_loss_source_return

    These later corrective inventory documents are not silently cascade-cancelled
    by C7.13. They remain explicit blockers until separately resolved.
    """
    if not encoder or not frappe.db.exists("DocType", "Stock Reconciliation"):
        return []

    names = set()
    reasons = {}
    meta = frappe.get_meta("Stock Reconciliation")

    def _remember(found, reason):
        for value in found or []:
            names.add(value)
            reasons.setdefault(value, []).append(reason)

    if meta.has_field("custom_nkt_source_return_exchange"):
        _remember(
            frappe.get_all(
                "Stock Reconciliation",
                filters={
                    "docstatus": 1,
                    "custom_nkt_source_return_exchange": encoder.name,
                },
                pluck="name",
                limit_page_length=200,
            ),
            f"linked directly to Return/Exchange {encoder.name}",
        )

    if meta.has_field("custom_nkt_fraction_loss_source_return"):
        _remember(
            frappe.get_all(
                "Stock Reconciliation",
                filters={
                    "docstatus": 1,
                    "custom_nkt_fraction_loss_source_return": encoder.name,
                },
                pluck="name",
                limit_page_length=200,
            ),
            f"fraction-loss valuation correction for Return/Exchange {encoder.name}",
        )

    if meta.has_field("custom_nkt_source_return_stock_entry"):
        source_entries = [
            encoder.get("return_stock_entry"),
            encoder.get("custom_nkt_inventory_correction_stock_entry"),
        ]
        for source_entry in [x for x in source_entries if x]:
            _remember(
                frappe.get_all(
                    "Stock Reconciliation",
                    filters={
                        "docstatus": 1,
                        "custom_nkt_source_return_stock_entry": source_entry,
                    },
                    pluck="name",
                    limit_page_length=200,
                ),
                f"linked to return/correction Stock Entry {source_entry}",
            )

    rows = []
    safe_fields = ["name", "posting_date", "posting_time", "docstatus"]
    for name in sorted(names):
        row = frappe.db.get_value(
            "Stock Reconciliation",
            name,
            safe_fields,
            as_dict=True,
        )
        if row and row.docstatus == 1:
            row["nkt_reversal_dependency_notes"] = "; ".join(
                reasons.get(name) or ["active valuation correction"]
            )
            rows.append(row)
    return rows


def _downstream_fraction_recoveries(encoder):
    """Conservatively detect later pooled Fraction use.

    Fraction stock is fungible inside an item+warehouse Bin. If a later
    NKT Stock Recovery consumed the same Fraction item after this RX posted,
    C7.13 cannot prove which kg came from which return. The RX must therefore
    remain blocked until that downstream physical transformation is separately
    reconciled/reversed.
    """
    if not encoder or not frappe.db.exists("DocType", "NKT Stock Recovery"):
        return []

    warehouse = encoder.get("return_warehouse")
    if not warehouse:
        return []

    fraction_items = {
        (row.get("fraction_item") or "").strip()
        for row in (encoder.get("returned_items") or [])
        if (row.get("fraction_item") or "").strip()
    }
    if not fraction_items:
        return []

    posted_on = get_datetime(encoder.get("posted_on") or encoder.get("entry_datetime") or now_datetime())
    rows = []
    seen = set()

    for item in sorted(fraction_items):
        hits = frappe.get_all(
            "NKT Stock Recovery",
            filters={
                "docstatus": 1,
                "fraction_item": item,
                "source_warehouse": warehouse,
            },
            fields=[
                "name", "recovery_datetime", "recovery_type",
                "fraction_kg_consumed", "stock_entry", "status",
            ],
            order_by="recovery_datetime asc",
            limit_page_length=500,
        )
        for row in hits:
            if row.name in seen:
                continue
            if flt(row.fraction_kg_consumed) <= TOLERANCE:
                continue
            when = get_datetime(row.recovery_datetime)
            if when < posted_on:
                continue
            if row.stock_entry:
                se_status = frappe.db.get_value("Stock Entry", row.stock_entry, "docstatus")
                if se_status != 1:
                    continue
            seen.add(row.name)
            rows.append(row)

    return rows

def get_reversal_preview(declaration_name):
    """Read-only pair-aware impact map for a posted Return/Exchange.

    Matched declarations are treated as one atomic operational unit.
    A legitimately Posted-but-Unmatched side remains independently reversible.
    """
    _assert_authority()
    unit = _pair_from_declaration(declaration_name)
    cashier = unit["cashier"]
    encoder = unit["encoder"]
    declarations = unit["declarations"]
    decl_names = [d.name for d in declarations]

    new_sale = None
    new_order = None
    for d in declarations:
        new_sale = new_sale or d.get("new_cashier_sale")
        new_order = new_order or d.get("new_customer_order")

    effects = []
    blockers = []
    warnings = []

    # Already-reversed protection.
    for d in declarations:
        reversal_status = d.get("custom_nkt_reversal_status") or "Not Reversed"
        reversal_record = d.get("custom_nkt_reversal_record")
        if reversal_status == "Reversed" or reversal_record:
            blockers.append(
                f"{d.name} is already linked to reversal {reversal_record or '(unknown)'}. "
                "A posted Return/Exchange may be reversed only once."
            )

    # Future lineage must be reversed newest-first.
    descendants = _descendant_declarations(
        new_sale=new_sale,
        new_order=new_order,
        exclude=decl_names,
    )
    if descendants:
        blockers.append(
            "Generated replacement sale/order already has later posted Return/Exchange activity. "
            "Reverse the descendant Return/Exchange first."
        )
        for r in descendants:
            effects.append({
                "effect_type": "Downstream Return/Exchange",
                "original_doctype": DECL,
                "original_document": r.name,
                "action": "BLOCK - reverse descendant first",
                "status": r.posting_status,
                "notes": f"{r.side} declaration references the generated replacement lineage.",
            })

    # Cashier-side direct refund and generated sale money.
    monetary_effects = []
    if cashier:
        direct = _movement_rows(DECL, cashier.name)
        for m in direct:
            monetary_effects.append(m)
            effects.append({
                "effect_type": "Direct Return/Exchange Money Movement",
                "original_doctype": "NKT Cashier Movement",
                "original_document": m.name,
                "action": "Create opposite reversal movement in a current Open Cashier Shift",
                "status": "Posted",
                "amount": flt(m.amount),
                "payment_method": m.payment_method,
                "original_direction": m.direction,
                "reversal_direction": "Out" if m.direction == "In" else "In",
                "notes": (
                    f"{m.direction} {m.payment_method}; historical movement remains auditable. "
                    "Do not rewrite a closed historical shift."
                ),
            })

    sale = _doc("NKT Cashier Sale", new_sale)
    if sale:
        sale_moves = _movement_rows("NKT Cashier Sale", sale.name)
        for m in sale_moves:
            monetary_effects.append(m)
            effects.append({
                "effect_type": "Generated Exchange Sale Payment Movement",
                "original_doctype": "NKT Cashier Movement",
                "original_document": m.name,
                "action": "Create opposite reversal movement in a current Open Cashier Shift",
                "status": "Posted",
                "amount": flt(m.amount),
                "payment_method": m.payment_method,
                "original_direction": m.direction,
                "reversal_direction": "Out" if m.direction == "In" else "In",
                "notes": (
                    f"{m.direction} {m.payment_method}; preserve historical shift movement, "
                    "offset it through the controlled reversal."
                ),
            })

        if sale.linked_payment_receipt:
            pr = _doc("NKT Payment Receipt", sale.linked_payment_receipt)
            effects.append({
                "effect_type": "Generated Exchange Payment Receipt",
                "original_doctype": "NKT Payment Receipt",
                "original_document": sale.linked_payment_receipt,
                "action": "Cancel after reversal money movements are prepared",
                "status": "Submitted" if pr and pr.docstatus == 1 else "Not Active",
                "notes": "Cash-basis receipt created from the generated Cashier Sale.",
            })

        effects.append({
            "effect_type": "Generated Exchange Cashier Sale",
            "original_doctype": "NKT Cashier Sale",
            "original_document": sale.name,
            "action": "Controlled cancel while preserving historical cashier movements",
            "status": "Submitted" if sale.docstatus == 1 else "Not Active",
            "notes": "Do not silently edit this submitted sale.",
        })

    # Encoder-side stock/account/credit/new order.
    if encoder:
        valuation_corrections = _active_valuation_corrections(encoder)
        if valuation_corrections:
            blockers.append(
                "This Return/Exchange has an active later Stock Reconciliation valuation correction. "
                "Resolve/cancel that corrective inventory document first; C7.13 will not silently cascade it."
            )
            for sr in valuation_corrections:
                effects.append({
                    "effect_type": "Inventory Valuation Correction",
                    "original_doctype": "Stock Reconciliation",
                    "original_document": sr.name,
                    "action": "BLOCK - resolve/cancel valuation correction first",
                    "status": "Active",
                    "notes": sr.get("nkt_reversal_dependency_notes") or "Active valuation correction.",
                })

        downstream_recoveries = _downstream_fraction_recoveries(encoder)
        if downstream_recoveries:
            blockers.append(
                "Returned Fraction stock has later NKT Stock Recovery/repack consumption. "
                "Because Fraction stock is pooled, reconcile/reverse the downstream physical transformation first."
            )
            for recovery in downstream_recoveries:
                effects.append({
                    "effect_type": "Downstream Stock Recovery / Repack",
                    "original_doctype": "NKT Stock Recovery",
                    "original_document": recovery.name,
                    "action": "BLOCK - downstream physical stock transformation must be resolved first",
                    "status": recovery.get("status") or "Submitted",
                    "amount": flt(recovery.get("fraction_kg_consumed")),
                    "notes": (
                        f"{recovery.get('recovery_type') or 'Stock Recovery'}; "
                        f"consumed {flt(recovery.get('fraction_kg_consumed'))} kg Fraction; "
                        f"Stock Entry {recovery.get('stock_entry') or '(none)'}."
                    ),
                })

        for effect in (
            _stock_effect(encoder.get("custom_nkt_inventory_correction_stock_entry"),
                          "Inventory Classification Correction",
                          "Cancel correction Stock Entry before the original return receipt"),
            _stock_effect(encoder.get("return_stock_entry"),
                          "Customer Return Inventory Receipt"),
        ):
            if effect:
                effects.append(effect)

        adj_name = encoder.get("account_adjustment_record")
        if adj_name:
            adj = _doc("NKT Return Account Adjustment", adj_name)
            effects.append({
                "effect_type": "Account Adjustment",
                "original_doctype": "NKT Return Account Adjustment",
                "original_document": adj_name,
                "action": "Restore the OLD order/receivable by the exact adjustment amount",
                "status": "Active" if adj else "Missing",
                "amount": flt(adj.amount) if adj else 0,
                "notes": (
                    f"Receivable {adj.receivable}; OLD order {adj.customer_order}."
                    if adj else ""
                ),
            })

        credit_name = encoder.get("customer_credit_record")
        if credit_name:
            adv = _doc("NKT Customer Advance", credit_name)
            applications = _advance_applications(credit_name)
            if applications:
                blockers.append(
                    f"Return Credit {credit_name} has active advance applications. "
                    "Reverse those applications through the accepted C5.4 correction workflow first."
                )
            effects.append({
                "effect_type": "Return Customer Credit",
                "original_doctype": "NKT Customer Advance",
                "original_document": credit_name,
                "action": "Cancel only after confirming no active applications",
                "status": (
                    adv.advance_status if adv else "Missing"
                ),
                "amount": flt(adv.original_advance_amount) if adv else 0,
                "notes": (
                    "Active applications: " + ", ".join(a.name for a in applications)
                    if applications else "No active applications detected."
                ),
            })

    order = _doc("NKT Customer Order", new_order)
    if order:
        fulfillment = order.get("custom_nkt_retail_stock_entry")
        if fulfillment:
            effect = _stock_effect(
                fulfillment,
                "Generated Exchange Order Fulfillment",
                "Cancel through controlled Customer Order cancellation",
            )
            if effect:
                effects.append(effect)

        warehouse_deps = _warehouse_release_dependencies(order.name)
        if warehouse_deps:
            blockers.append(
                f"Generated Customer Order {order.name} has an active warehouse-release dependency. "
                "That release must be recalled/reconciled before the exchange can be reversed."
            )
            for r in warehouse_deps:
                effects.append({
                    "effect_type": "Warehouse Release Dependency",
                    "original_doctype": "NKT Warehouse Release",
                    "original_document": r.name,
                    "action": "BLOCK - recall/reconcile release first",
                    "status": r.get("status") or str(r.get("docstatus")),
                    "notes": "Physical warehouse release is an independent operational event.",
                })

        rec_name = order.get("custom_nkt_customer_receivable")
        if rec_name:
            rec = _doc("NKT Customer Receivable", rec_name)
            effects.append({
                "effect_type": "Generated Exchange Receivable",
                "original_doctype": "NKT Customer Receivable",
                "original_document": rec_name,
                "action": "Cancelled through controlled Customer Order cancellation if unpaid",
                "status": rec.status if rec else "Missing",
                "amount": flt(rec.outstanding_amount) if rec else 0,
                "notes": (
                    "If later collections were applied to this generated receivable, "
                    "those later collections must be corrected first."
                ),
            })
            if rec and flt(rec.amount_paid) > TOLERANCE:
                blockers.append(
                    f"Generated Customer Order receivable {rec.name} already has payments applied. "
                    "Reverse/reallocate those later collections before reversing this exchange."
                )

        effects.append({
            "effect_type": "Generated Exchange Customer Order",
            "original_doctype": "NKT Customer Order",
            "original_document": order.name,
            "action": "Controlled cancel; its fulfillment/receivable cancellation remains authoritative",
            "status": "Submitted" if order.docstatus == 1 else "Not Active",
            "notes": "Do not silently edit this submitted order.",
        })

    # Original declarations are always last.
    for d in declarations:
        effects.append({
            "effect_type": f"Original {d.side} Return/Exchange Declaration",
            "original_doctype": DECL,
            "original_document": d.name,
            "action": "Controlled cancel after all owned effects reverse successfully",
            "status": d.posting_status,
            "notes": "Cancellation frees its return lineage and preserves the submitted historical record.",
        })

    if monetary_effects:
        warnings.append(
            "This reversal has payment/refund effects. Execution must create opposite movements "
            "in a CURRENT Open Cashier Shift; historical closed-shift movements will not be rewritten."
        )

    stock_effects = [
        e for e in effects
        if "Stock" in (e.get("original_doctype") or "")
        or "Inventory" in e.get("effect_type", "")
        or "Fulfillment" in e.get("effect_type", "")
    ]
    if stock_effects:
        warnings.append(
            "This reversal has inventory effects. ERPNext stock cancellation rules remain the safety gate; "
            "if later physical stock use makes reversal impossible, execution must fail rather than force SQL changes."
        )

    return {
        "version": VERSION,
        "selected_declaration": declaration_name,
        "unit_type": unit["unit_type"],
        "cashier_declaration": cashier.name if cashier else None,
        "encoder_declaration": encoder.name if encoder else None,
        "new_cashier_sale": new_sale,
        "new_customer_order": new_order,
        "effects": effects,
        "blockers": blockers,
        "warnings": warnings,
        "ready_for_reversal": not blockers,
        "correction_rule": (
            "After reversal, Cashier and Encoder re-enter the corrected Return/Exchange independently "
            "through their normal current-date screens. C7.13 will not auto-create a corrected RX."
        ),
    }


@frappe.whitelist()
def preview_reversal(declaration_name):
    return get_reversal_preview(declaration_name)


def _ensure_rx_audit_fields():
    custom_fields = {
        DECL: [
            {
                "fieldname": "custom_nkt_reversal_section",
                "label": "Controlled Reversal",
                "fieldtype": "Section Break",
                "insert_after": "customer_credit_record",
                "collapsible": 1,
            },
            {
                "fieldname": "custom_nkt_reversal_status",
                "label": "Reversal Status",
                "fieldtype": "Select",
                "options": "\nNot Reversed\nReversed",
                "default": "Not Reversed",
                "read_only": 1,
                "insert_after": "custom_nkt_reversal_section",
            },
            {
                "fieldname": "custom_nkt_reversal_record",
                "label": "Reversal Record",
                "fieldtype": "Link",
                "options": REVERSAL,
                "read_only": 1,
                "insert_after": "custom_nkt_reversal_status",
            },
            {
                "fieldname": "custom_nkt_reversed_on",
                "label": "Reversed On",
                "fieldtype": "Datetime",
                "read_only": 1,
                "insert_after": "custom_nkt_reversal_record",
            },
            {
                "fieldname": "custom_nkt_reversed_by",
                "label": "Reversed By",
                "fieldtype": "Link",
                "options": "User",
                "read_only": 1,
                "insert_after": "custom_nkt_reversed_on",
            },
            {
                "fieldname": "custom_nkt_reversal_reason",
                "label": "Reversal Reason",
                "fieldtype": "Small Text",
                "read_only": 1,
                "insert_after": "custom_nkt_reversed_by",
            },
        ],
    }
    create_custom_fields(custom_fields, update=True)
    frappe.clear_cache(doctype=DECL)



# ---------------------------------------------------------------------------
# C7.13B RUNTIME-GATED EXECUTION ENGINE
# ---------------------------------------------------------------------------

REFERENCE_REQUIRED_METHODS = {
    "GCash", "Maya", "Card", "Bank Transfer", "Online", "Check"
}
MONEY_EFFECT_TYPES = {
    "Direct Return/Exchange Money Movement",
    "Generated Exchange Sale Payment Movement",
}
STOCK_EFFECT_TYPES = {
    "Inventory Classification Correction",
    "Customer Return Inventory Receipt",
    "Generated Exchange Order Fulfillment",
}


def _lock(doctype, name):
    if name:
        frappe.db.sql(
            f"SELECT name FROM `tab{doctype}` WHERE name=%s FOR UPDATE",
            name,
        )


def _opposite_direction(direction):
    if direction == "In":
        return "Out"
    if direction == "Out":
        return "In"
    frappe.throw(_("Unsupported historical Cashier Movement direction: {0}").format(direction))


def _effect_input_map(doc):
    out = {}
    for row in (doc.get("effects") or []):
        key = (row.original_doctype or "", row.original_document or "", row.effect_type or "")
        out[key] = {
            "reversal_reference": row.get("reversal_reference") or "",
            "physical_action_notes": row.get("physical_action_notes") or "",
        }
    return out


def _append_preview_effect(doc, effect, preserved):
    key = (
        effect.get("original_doctype") or "",
        effect.get("original_document") or "",
        effect.get("effect_type") or "",
    )
    prior = preserved.get(key) or {}
    action = effect.get("action") or ""
    blocked = action.startswith("BLOCK")
    doc.append("effects", {
        "effect_type": effect.get("effect_type") or "",
        "action": action,
        "effect_status": "Blocked" if blocked else "Planned",
        "original_doctype": effect.get("original_doctype") or "",
        "original_document": effect.get("original_document") or "",
        "amount": flt(effect.get("amount")),
        "payment_method": effect.get("payment_method") or "",
        "original_direction": effect.get("original_direction") or "",
        "reversal_direction": effect.get("reversal_direction") or "",
        "reversal_reference": prior.get("reversal_reference") or "",
        "physical_action_notes": prior.get("physical_action_notes") or "",
        "notes": effect.get("notes") or "",
    })


def prepare_reversal_document(doc):
    """Rebuild the draft from the live pair-aware preview.

    Users may enter the reason, current reversal cashier, confirmations, and
    per-money-effect reversal references. Source/effect identity is server-owned.
    """
    _assert_authority()
    selected = doc.original_cashier_declaration or doc.original_encoder_declaration
    if not selected:
        frappe.throw(_("Select an original posted Cashier or Encoder Return/Exchange declaration."))

    preserved = _effect_input_map(doc)
    preview = get_reversal_preview(selected)

    anchor_decl = preview.get("cashier_declaration") or preview.get("encoder_declaration")
    anchor = frappe.get_doc(DECL, anchor_decl)

    doc.company = anchor.company
    doc.business_date = nowdate()
    doc.reversal_datetime = doc.reversal_datetime or now_datetime()
    doc.requested_by = frappe.session.user
    doc.unit_type = preview.get("unit_type")
    doc.original_cashier_declaration = preview.get("cashier_declaration")
    doc.original_encoder_declaration = preview.get("encoder_declaration")
    doc.original_new_cashier_sale = preview.get("new_cashier_sale")
    doc.original_new_customer_order = preview.get("new_customer_order")

    doc.set("effects", [])
    for effect in preview.get("effects") or []:
        _append_preview_effect(doc, effect, preserved)

    doc.reversal_status = "Blocked" if preview.get("blockers") else "Ready"
    notes = []
    if preview.get("blockers"):
        notes.append("BLOCKERS:\n- " + "\n- ".join(preview["blockers"]))
    if preview.get("warnings"):
        notes.append("WARNINGS:\n- " + "\n- ".join(preview["warnings"]))
    notes.append(preview.get("correction_rule") or "")
    doc.execution_notes = "\n\n".join(x for x in notes if x)

    return preview


def _money_effect_rows(doc):
    return [r for r in (doc.get("effects") or []) if r.effect_type in MONEY_EFFECT_TYPES]


def _stock_effect_rows(doc):
    return [r for r in (doc.get("effects") or []) if r.effect_type in STOCK_EFFECT_TYPES]


def _current_reversal_shift(doc):
    if not doc.reversal_cashier:
        return None
    from nkt_operations.nkt_store_operations.features.payments_accounts.cash_ledger import (
        get_open_shift_for_user,
    )

    shift = get_open_shift_for_user(
        company=doc.company,
        user=doc.reversal_cashier,
    )
    if not shift:
        return None

    shift_date = getdate(shift.shift_start)
    today = getdate(nowdate())
    if shift_date != today:
        frappe.throw(
            _(
                "Cashier Shift {0} belongs to business date {1}, but the reversal "
                "business date is {2}. Close the previous-date shift and open a "
                "current-date Cashier Shift before posting this reversal. NKT does "
                "not backdate or cross-date cashier transactions."
            ).format(shift.name, shift_date, today)
        )

    return shift


def validate_reversal_document(doc, *, for_submit=False):
    _assert_authority()

    # Drafts may be created with a blank reason so Admin/Owner can first review
    # the live server-owned preview. A real reason becomes mandatory at Submit.
    if for_submit and not (doc.reversal_reason or "").strip():
        frappe.throw(_("Reversal Reason is required before Submit."))

    preview = prepare_reversal_document(doc)

    if not for_submit:
        return preview

    if preview.get("blockers"):
        frappe.throw(
            _("This Return/Exchange cannot be reversed yet:\n- {0}").format(
                "\n- ".join(preview["blockers"])
            )
        )

    money_rows = _money_effect_rows(doc)
    stock_rows = _stock_effect_rows(doc)

    if money_rows:
        if not doc.money_correction_confirmed:
            frappe.throw(
                _("Confirm that the money correction reflects the physical/external payment reality.")
            )
        if not doc.reversal_cashier:
            frappe.throw(_("Cashier Handling Money Reversal is required."))
        shift = _current_reversal_shift(doc)
        if not shift:
            frappe.throw(
                _(
                    "The selected reversal cashier must have exactly one current Open Cashier Shift "
                    "for company {0}."
                ).format(doc.company)
            )
        for row in money_rows:
            method = normalize_payment_method(row.payment_method)
            if method in REFERENCE_REQUIRED_METHODS and not (row.reversal_reference or "").strip():
                frappe.throw(
                    _(
                        "Reversal Reference is required for {0} movement {1}."
                    ).format(method, row.original_document)
                )

    if stock_rows and not doc.inventory_correction_confirmed:
        frappe.throw(
            _("Confirm that the inventory correction reflects the physical stock reality.")
        )

    return preview


def _mark_effect(doc, original_doctype, original_document, effect_type, *,
                 status="Reversed", reversal_doctype=None, reversal_document=None, note=None):
    for row in (doc.get("effects") or []):
        if (
            row.original_doctype == original_doctype
            and row.original_document == original_document
            and row.effect_type == effect_type
        ):
            values = {"effect_status": status}
            if reversal_doctype:
                values["reversal_doctype"] = reversal_doctype
            if reversal_document:
                values["reversal_document"] = reversal_document
            if note:
                values["notes"] = ((row.notes or "") + "\n" + note).strip()
            frappe.db.set_value(
                "NKT Return Exchange Reversal Effect",
                row.name,
                values,
                update_modified=False,
            )
            return row.name
    return None


def _create_reversal_movement(doc, effect_row):
    original = frappe.get_doc("NKT Cashier Movement", effect_row.original_document)
    if original.docstatus != 1:
        frappe.throw(
            _("Historical Cashier Movement {0} is not active.").format(original.name)
        )

    shift = _current_reversal_shift(doc)
    if not shift:
        frappe.throw(_("A current Open Cashier Shift is required for the reversal movement."))

    from nkt_operations.nkt_store_operations.features.payments_accounts.cash_ledger import create_cashier_movement

    movement = create_cashier_movement(
        company=doc.company,
        posting_datetime=now_datetime(),
        cashier_shift=shift.name,
        settlement_location=shift.settlement_location,
        cashier=doc.reversal_cashier,
        movement_type="Return/Exchange Reversal",
        direction=_opposite_direction(original.direction),
        payment_method=normalize_payment_method(original.payment_method),
        amount=original.amount,
        settlement_amount=original.get("settlement_amount") or max(flt(original.amount) - flt(original.get("card_surcharge")), 0),
        card_surcharge=flt(original.get("card_surcharge")),
        source_doctype=REVERSAL,
        source_name=doc.name,
        source_row=original.name,
        customer=original.customer,
        reference_number=(effect_row.reversal_reference or "").strip(),
        remarks=(
            f"Controlled reversal {doc.name} offsets historical movement {original.name}. "
            f"Original movement remains posted in historical shift {original.cashier_shift}."
        ),
    )
    if not movement:
        frappe.throw(_("Failed to create reversal movement for {0}.").format(original.name))

    _mark_effect(
        doc,
        "NKT Cashier Movement",
        original.name,
        effect_row.effect_type,
        reversal_doctype="NKT Cashier Movement",
        reversal_document=movement.name,
        note=f"Opposite movement posted in current shift {shift.name}.",
    )
    return movement.name


def _apply_owned_cancel_link_exceptions(
    target,
    reversal_doc,
    owned_declarations,
    owned_cashier_movements=None,
    owned_customer_orders=None,
):
    """Allow cancellation through ONLY this reversal's validated source back-links.

    Important Frappe/ERPNext behavior:
    some controllers (notably Stock Entry) replace ignore_linked_doctypes inside
    their own on_cancel(). Therefore a value set only before doc.cancel() can be
    lost before Frappe's final backlink check.

    We validate the exact allowed backlinks first, then wrap ONLY this document
    instance's on_cancel so its normal controller logic runs unchanged and the
    validated exceptions are merged back immediately afterward.
    """
    allowed = {
        DECL: set(x for x in (owned_declarations or []) if x),
        REVERSAL: {reversal_doc.name},
        "NKT Cashier Movement": set(
            x for x in (owned_cashier_movements or []) if x
        ),
        "NKT Customer Order": set(
            x for x in (owned_customer_orders or []) if x
        ),
    }

    linked = []
    linked.extend(get_linked_docs(target, method="Cancel") or [])
    linked.extend(get_dynamic_linked_docs(target, method="Cancel") or [])

    ignored_doctypes = set()
    for ref in linked:
        ref_dt = ref.get("reference_doctype")
        ref_name = ref.get("reference_docname")
        if ref_dt not in allowed:
            continue
        if ref_name not in allowed[ref_dt]:
            frappe.throw(
                _(
                    "Controlled reversal refused to ignore unexpected backlink "
                    "{0} {1} while cancelling {2} {3}."
                ).format(ref_dt, ref_name, target.doctype, target.name)
            )
        ignored_doctypes.add(ref_dt)

    if not ignored_doctypes:
        return

    validated = tuple(sorted(ignored_doctypes))
    original_on_cancel = getattr(target, "on_cancel", None)

    def _merge_validated_exceptions(self):
        merged = set(self.get("ignore_linked_doctypes") or ())
        merged.update(validated)
        self.set("ignore_linked_doctypes", tuple(sorted(merged)))

    # Set once before cancellation for controllers that do not touch the value.
    _merge_validated_exceptions(target)

    # Then merge again AFTER the controller's own on_cancel. This is what keeps
    # Stock Entry's normal GL/SLE exclusions AND our narrowly validated RX link.
    if callable(original_on_cancel):
        def _controlled_on_cancel(self, *args, **kwargs):
            result = original_on_cancel(*args, **kwargs)
            _merge_validated_exceptions(self)
            return result

        target.on_cancel = MethodType(_controlled_on_cancel, target)


def _cancel_doc(
    doctype,
    name,
    *,
    reversal_doc=None,
    owned_declarations=None,
    owned_customer_orders=None,
    controlled_sale=False,
):
    if not name or not frappe.db.exists(doctype, name):
        return None
    doc = frappe.get_doc(doctype, name)
    if doc.docstatus == 2:
        return doc.name
    if doc.docstatus == 0:
        frappe.delete_doc(doctype, name, ignore_permissions=True, force=True)
        return name

    doc.flags.ignore_permissions = True
    if controlled_sale:
        doc.flags.nkt_controlled_reversal = True
    if reversal_doc:
        owned_cashier_movements = []
        if controlled_sale and doctype == "NKT Cashier Sale":
            owned_cashier_movements = [
                row.name
                for row in _movement_rows("NKT Cashier Sale", name)
            ]
        _apply_owned_cancel_link_exceptions(
            doc,
            reversal_doc,
            owned_declarations or [],
            owned_cashier_movements=owned_cashier_movements,
            owned_customer_orders=owned_customer_orders or [],
        )
    doc.cancel()
    return doc.name


def _reverse_account_adjustment(adjustment_name, reversal_doc):
    if not adjustment_name:
        return None
    _lock("NKT Return Account Adjustment", adjustment_name)
    adj = frappe.get_doc("NKT Return Account Adjustment", adjustment_name)

    existing = adj.get("custom_nkt_reversal_record")
    if existing and existing != reversal_doc.name:
        frappe.throw(
            _("Account Adjustment {0} was already reversed by {1}.").format(
                adjustment_name, existing
            )
        )

    amount = flt(adj.amount)
    _lock("NKT Customer Receivable", adj.receivable)
    _lock("NKT Customer Order", adj.customer_order)

    rec = frappe.get_doc("NKT Customer Receivable", adj.receivable)
    order = frappe.get_doc("NKT Customer Order", adj.customer_order)

    new_original = flt(rec.original_amount) + amount
    paid = flt(rec.amount_paid)
    new_outstanding = max(new_original - paid, 0)

    if new_outstanding <= TOLERANCE:
        rec_status = "Paid"
    elif paid > TOLERANCE:
        rec_status = "Partially Paid"
    else:
        rec_status = "Open"

    account_sale = bool(order.get("account_sale"))
    if new_outstanding <= TOLERANCE:
        payment_status = "Paid"
    elif paid > TOLERANCE:
        payment_status = "Partially Paid"
    else:
        payment_status = "Charged to Account" if account_sale else "Unpaid"

    frappe.db.set_value(
        "NKT Customer Receivable",
        rec.name,
        {
            "original_amount": new_original,
            "outstanding_amount": new_outstanding,
            "status": rec_status,
        },
        update_modified=False,
    )
    frappe.db.set_value(
        "NKT Customer Order",
        order.name,
        {
            "amount_due": new_outstanding,
            "payment_status": payment_status,
        },
        update_modified=False,
    )
    frappe.db.set_value(
        "NKT Return Account Adjustment",
        adj.name,
        {
            "custom_nkt_reversal_record": reversal_doc.name,
            "custom_nkt_reversed_on": now_datetime(),
            "custom_nkt_reversed_by": frappe.session.user,
            "custom_nkt_reversal_reason": reversal_doc.reversal_reason,
        },
        update_modified=False,
    )

    from nkt_operations.nkt_store_operations.features.payments_accounts.credit import refresh_customer_credit
    refresh_customer_credit(adj.customer)

    _mark_effect(
        reversal_doc,
        "NKT Return Account Adjustment",
        adj.name,
        "Account Adjustment",
        reversal_doctype=REVERSAL,
        reversal_document=reversal_doc.name,
        note=f"Restored receivable/order by {frappe.format_value(amount, {'fieldtype':'Currency'})}.",
    )
    return adj.name


def _cancel_return_credit(advance_name, reversal_doc):
    if not advance_name:
        return None
    _lock("NKT Customer Advance", advance_name)
    advance = frappe.get_doc("NKT Customer Advance", advance_name)
    if flt(advance.applied_amount) > TOLERANCE:
        frappe.throw(
            _(
                "Return Credit {0} has active/applied value. Reverse its applications through C5.4 first."
            ).format(advance.name)
        )
    if advance.docstatus == 1:
        advance.flags.ignore_permissions = True
        _apply_owned_cancel_link_exceptions(
            advance,
            reversal_doc,
            [
                reversal_doc.original_cashier_declaration,
                reversal_doc.original_encoder_declaration,
            ],
        )
        advance.cancel()

    from nkt_operations.nkt_store_operations.features.payments_accounts.credit import refresh_customer_credit
    refresh_customer_credit(advance.customer)

    _mark_effect(
        reversal_doc,
        "NKT Customer Advance",
        advance.name,
        "Return Customer Credit",
        reversal_doctype="NKT Customer Advance",
        reversal_document=advance.name,
    )
    return advance.name


def _cancel_original_declarations(preview, reversal_doc):
    names = [
        preview.get("encoder_declaration"),
        preview.get("cashier_declaration"),
    ]
    names = [n for n in names if n]
    for name in names:
        _lock(DECL, name)

    for name in names:
        d = frappe.get_doc(DECL, name)
        if d.docstatus != 1 or d.posting_status != "Posted":
            frappe.throw(
                _("Original Return/Exchange {0} is no longer an active Posted declaration.").format(name)
            )
        current = d.get("custom_nkt_reversal_record")
        if current and current != reversal_doc.name:
            frappe.throw(
                _("{0} has already been reversed by {1}.").format(name, current)
            )

    for name in names:
        d = frappe.get_doc(DECL, name)
        frappe.db.set_value(
            DECL,
            d.name,
            {
                "custom_nkt_reversal_status": "Reversed",
                "custom_nkt_reversal_record": reversal_doc.name,
                "custom_nkt_reversed_on": now_datetime(),
                "custom_nkt_reversed_by": frappe.session.user,
                "custom_nkt_reversal_reason": reversal_doc.reversal_reason,
            },
            update_modified=False,
        )
        d.reload()
        d.flags.nkt_controlled_reversal = True
        d.flags.ignore_permissions = True
        # Both declarations in a matched Cashier/Encoder pair are owned by
        # this one atomic reversal. The paired declaration is therefore an
        # expected submitted backlink while each side is cancelled.
        # Any third/unrelated RX remains a hard blocker.
        owned_historical_movements = []
        for declaration_name in names:
            owned_historical_movements.extend(
                row.name
                for row in _movement_rows(DECL, declaration_name)
            )

        _apply_owned_cancel_link_exceptions(
            d,
            reversal_doc,
            names,
            owned_cashier_movements=owned_historical_movements,
        )
        d.cancel()
        _mark_effect(
            reversal_doc,
            DECL,
            d.name,
            f"Original {d.side} Return/Exchange Declaration",
            reversal_doctype=DECL,
            reversal_document=d.name,
        )


def execute_reversal(reversal):
    """Execute one pair-aware reversal atomically.

    In C7.13B this is invoked only by the runtime-gated DocType submit hook.
    Production submission remains blocked until the rollback suite is accepted.
    """
    _assert_authority()
    doc = reversal if hasattr(reversal, "doctype") else frappe.get_doc(REVERSAL, reversal)

    if doc.reversal_status == "Reversed":
        return {
            "reversed": True,
            "reversal": doc.name,
            "idempotent_replay": True,
        }

    source = doc.original_cashier_declaration or doc.original_encoder_declaration
    preview = get_reversal_preview(source)
    if preview.get("blockers"):
        frappe.throw(
            _("Reversal became blocked before execution:\n- {0}").format(
                "\n- ".join(preview["blockers"])
            )
        )

    # Lock original unit and re-check one-time reversal ownership.
    for name in (
        preview.get("cashier_declaration"),
        preview.get("encoder_declaration"),
    ):
        if name:
            _lock(DECL, name)
            d = frappe.get_doc(DECL, name)
            if d.docstatus != 1 or d.posting_status != "Posted":
                frappe.throw(
                    _("Original Return/Exchange {0} is no longer active Posted history.").format(name)
                )
            existing = d.get("custom_nkt_reversal_record")
            if existing and existing != doc.name:
                frappe.throw(_("{0} was already reversed by {1}.").format(name, existing))

    # 1. Current-period opposite money movements. Historical movement rows stay posted.
    for row in _money_effect_rows(doc):
        _create_reversal_movement(doc, row)

    owned_declarations = [
        preview.get("cashier_declaration"),
        preview.get("encoder_declaration"),
    ]

    # 2. Generated exchange Cashier Sale first. Its accepted on_cancel hook
    # cancels the linked cash-basis Payment Receipt after the Sale itself has
    # entered cancellation, while C7.13 preserves historical movement rows.
    sale_name = preview.get("new_cashier_sale")
    if sale_name:
        _cancel_doc(
            "NKT Cashier Sale",
            sale_name,
            reversal_doc=doc,
            owned_declarations=owned_declarations,
            owned_customer_orders=[preview.get("new_customer_order")],
            controlled_sale=True,
        )
        _mark_effect(
            doc, "NKT Cashier Sale", sale_name, "Generated Exchange Cashier Sale",
            reversal_doctype="NKT Cashier Sale", reversal_document=sale_name,
        )

    # 3. Verify/cancel the generated Payment Receipt after the Sale hook has run.
    for e in preview.get("effects") or []:
        if e.get("effect_type") == "Generated Exchange Payment Receipt":
            name = e.get("original_document")
            if name and frappe.db.exists("NKT Payment Receipt", name):
                pr_status = frappe.db.get_value("NKT Payment Receipt", name, "docstatus")
                if pr_status == 1:
                    _cancel_doc(
                        "NKT Payment Receipt",
                        name,
                        reversal_doc=doc,
                        owned_declarations=owned_declarations,
                    )
            _mark_effect(
                doc, "NKT Payment Receipt", name, e["effect_type"],
                reversal_doctype="NKT Payment Receipt", reversal_document=name,
            )

    # 4. Generated Customer Order.
    #
    # The Reversal Effect table itself dynamically links to the generated
    # fulfillment Stock Entry. If Customer Order.on_cancel is allowed to reach
    # that Stock Entry first, the nested cancellation cannot see this engine's
    # per-instance controlled backlink wrapper and Frappe correctly blocks on
    # the current Reversal audit row.
    #
    # Therefore cancel the exact preview-owned fulfillment Stock Entry first
    # through _cancel_doc(), which validates only the current RX/Reversal
    # backlinks. The normal Customer Order.on_cancel then runs unchanged:
    # cancel_customer_order_fulfillment() sees the already-cancelled entry,
    # clears the order's stock-entry link, and continues with unlink/receivable
    # logic normally.
    order_name = preview.get("new_customer_order")
    if order_name:
        fulfillment_effects = [
            e
            for e in (preview.get("effects") or [])
            if e.get("effect_type") == "Generated Exchange Order Fulfillment"
            and e.get("original_document")
        ]
        for e in fulfillment_effects:
            _cancel_doc(
                "Stock Entry",
                e.get("original_document"),
                reversal_doc=doc,
                owned_declarations=owned_declarations,
                owned_customer_orders=[order_name],
            )

        _cancel_doc(
            "NKT Customer Order",
            order_name,
            reversal_doc=doc,
            owned_declarations=owned_declarations,
        )
        _mark_effect(
            doc, "NKT Customer Order", order_name, "Generated Exchange Customer Order",
            reversal_doctype="NKT Customer Order", reversal_document=order_name,
        )
        for e in preview.get("effects") or []:
            if e.get("effect_type") in {
                "Generated Exchange Order Fulfillment",
                "Generated Exchange Receivable",
            }:
                _mark_effect(
                    doc,
                    e.get("original_doctype"),
                    e.get("original_document"),
                    e.get("effect_type"),
                    reversal_doctype=e.get("original_doctype"),
                    reversal_document=e.get("original_document"),
                )

    # 5. C7.11C classification correction must unwind before the original
    # return receipt. Source RX/Reversal backlinks are expected and narrowly
    # exempted; any unrelated submitted link still blocks cancellation.
    for e in preview.get("effects") or []:
        if e.get("effect_type") == "Inventory Classification Correction":
            name = e.get("original_document")
            _cancel_doc(
                "Stock Entry",
                name,
                reversal_doc=doc,
                owned_declarations=owned_declarations,
            )
            _mark_effect(
                doc, "Stock Entry", name, e["effect_type"],
                reversal_doctype="Stock Entry", reversal_document=name,
            )

    # 6. Official return inventory receipt.
    for e in preview.get("effects") or []:
        if e.get("effect_type") == "Customer Return Inventory Receipt":
            name = e.get("original_document")
            _cancel_doc(
                "Stock Entry",
                name,
                reversal_doc=doc,
                owned_declarations=owned_declarations,
            )
            _mark_effect(
                doc, "Stock Entry", name, e["effect_type"],
                reversal_doctype="Stock Entry", reversal_document=name,
            )

    # 7. Restore OLD-account balance exactly.
    for e in preview.get("effects") or []:
        if e.get("effect_type") == "Account Adjustment":
            _reverse_account_adjustment(e.get("original_document"), doc)

    # 8. Cancel unused Return Credit.
    for e in preview.get("effects") or []:
        if e.get("effect_type") == "Return Customer Credit":
            _cancel_return_credit(e.get("original_document"), doc)

    # 9. Original RX declarations last.
    _cancel_original_declarations(preview, doc)

    frappe.db.set_value(
        REVERSAL,
        doc.name,
        {
            "reversal_status": "Reversed",
            "execution_notes": (
                (doc.execution_notes or "")
                + "\n\nEXECUTED: All owned operational effects reversed successfully. "
                "Corrected business entry must be re-entered independently by Cashier and Encoder."
            ).strip(),
        },
        update_modified=False,
    )

    return {
        "reversed": True,
        "reversal": doc.name,
        "idempotent_replay": False,
        "cashier_declaration": preview.get("cashier_declaration"),
        "encoder_declaration": preview.get("encoder_declaration"),
    }


def create_reversal_draft(declaration_name, request_id=None):
    """Create or reuse one active Admin/Owner reversal draft for the source unit.

    The source pair itself is the idempotency anchor. Repeated UI clicks return
    the same active Draft instead of creating duplicates. Cancelled drafts may
    be replaced by a new request with a new unique request id.
    """
    _assert_authority()
    preview = get_reversal_preview(declaration_name)

    request_id = (request_id or "").strip()
    if request_id:
        existing = frappe.db.get_value(
            REVERSAL,
            {"custom_nkt_reversal_request_id": request_id, "docstatus": 0},
            "name",
        )
        if existing:
            existing_doc = frappe.get_doc(REVERSAL, existing)
            prepare_reversal_document(existing_doc)
            existing_doc.flags.ignore_permissions = True
            existing_doc.save(ignore_permissions=True)
            return existing_doc.as_dict()

    # Stable UI idempotency: reuse an active Draft for either declaration in
    # the exact operational unit even if the browser retries without request_id.
    for fieldname, source_name in (
        ("original_cashier_declaration", preview.get("cashier_declaration")),
        ("original_encoder_declaration", preview.get("encoder_declaration")),
    ):
        if not source_name:
            continue
        existing = frappe.db.get_value(
            REVERSAL,
            {fieldname: source_name, "docstatus": 0},
            "name",
            order_by="creation desc",
        )
        if existing:
            existing_doc = frappe.get_doc(REVERSAL, existing)
            prepare_reversal_document(existing_doc)
            existing_doc.flags.ignore_permissions = True
            existing_doc.save(ignore_permissions=True)
            return existing_doc.as_dict()

    doc = frappe.new_doc(REVERSAL)
    doc.custom_nkt_reversal_request_id = request_id or frappe.generate_hash(length=24)
    doc.original_cashier_declaration = preview.get("cashier_declaration")
    doc.original_encoder_declaration = preview.get("encoder_declaration")
    doc.reversal_reason = ""
    prepare_reversal_document(doc)
    doc.flags.ignore_permissions = True
    doc.insert(ignore_permissions=True)
    return doc.as_dict()


@frappe.whitelist()
def make_reversal_draft(declaration_name, request_id=None):
    return create_reversal_draft(declaration_name, request_id=request_id)


def _ensure_adjustment_audit_fields():
    custom_fields = {
        "NKT Return Account Adjustment": [
            {
                "fieldname": "custom_nkt_reversal_record",
                "label": "Reversal Record",
                "fieldtype": "Link",
                "options": REVERSAL,
                "read_only": 1,
                "insert_after": "remarks",
            },
            {
                "fieldname": "custom_nkt_reversed_on",
                "label": "Reversed On",
                "fieldtype": "Datetime",
                "read_only": 1,
                "insert_after": "custom_nkt_reversal_record",
            },
            {
                "fieldname": "custom_nkt_reversed_by",
                "label": "Reversed By",
                "fieldtype": "Link",
                "options": "User",
                "read_only": 1,
                "insert_after": "custom_nkt_reversed_on",
            },
            {
                "fieldname": "custom_nkt_reversal_reason",
                "label": "Reversal Reason",
                "fieldtype": "Small Text",
                "read_only": 1,
                "insert_after": "custom_nkt_reversed_by",
            },
        ]
    }
    create_custom_fields(custom_fields, update=True)
    frappe.clear_cache(doctype="NKT Return Account Adjustment")

def install():
    _assert_authority()

    # Load child first, then parent.
    frappe.reload_doc("nkt_store_operations", "doctype", "nkt_return_exchange_reversal_effect", force=True)
    frappe.reload_doc("nkt_store_operations", "doctype", "nkt_return_exchange_reversal", force=True)

    _ensure_rx_audit_fields()
    _ensure_adjustment_audit_fields()

    return verify()


def verify():
    result = {
        "version": VERSION,
        "authorized_roles": sorted(AUTHORIZED_ROLES),
        "checks": {},
        "errors": [],
    }

    result["checks"]["reversal_doctype_exists"] = bool(frappe.db.exists("DocType", REVERSAL))
    result["checks"]["reversal_effect_child_exists"] = bool(
        frappe.db.exists("DocType", "NKT Return Exchange Reversal Effect")
    )

    rx_meta = frappe.get_meta(DECL)
    for fieldname in (
        "custom_nkt_reversal_status",
        "custom_nkt_reversal_record",
        "custom_nkt_reversed_on",
        "custom_nkt_reversed_by",
        "custom_nkt_reversal_reason",
    ):
        result["checks"][f"rx_field::{fieldname}"] = rx_meta.has_field(fieldname)

    # Read-only preview every active posted unit exactly once.
    seen = set()
    previews = []
    for row in frappe.get_all(
        DECL,
        filters={"docstatus": 1, "posting_status": "Posted"},
        fields=["name", "side", "matched_declaration"],
        order_by="creation asc",
        limit_page_length=5000,
    ):
        canonical = tuple(sorted(x for x in (row.name, row.matched_declaration) if x))
        if canonical in seen:
            continue
        seen.add(canonical)
        try:
            previews.append(get_reversal_preview(row.name))
        except Exception as exc:
            result["errors"].append(f"{row.name}: {exc}")

    result["previews"] = previews
    result["summary"] = {
        "active_operational_units": len(previews),
        "ready_units": sum(1 for p in previews if p.get("ready_for_reversal")),
        "blocked_units": sum(1 for p in previews if not p.get("ready_for_reversal")),
        "matched_pairs": sum(1 for p in previews if p.get("unit_type") == "Matched Pair"),
        "cashier_only": sum(1 for p in previews if p.get("unit_type") == "Cashier Only"),
        "encoder_only": sum(1 for p in previews if p.get("unit_type") == "Encoder Only"),
    }

    result["checks"]["preview_units_found"] = bool(previews)
    result["checks"]["matched_pair_preview_found"] = any(
        p.get("unit_type") == "Matched Pair" for p in previews
    )
    result["checks"]["correction_preserves_independent_reentry"] = all(
        "independently" in p.get("correction_rule", "") for p in previews
    )

    result["passed"] = all(result["checks"].values()) and not result["errors"]
    return result
