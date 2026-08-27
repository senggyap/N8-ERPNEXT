from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import flt, now_datetime

REFERENCE_EDIT_ROLES = {
    "NKT Warehouse",
    "NKT OWNER",
    "NKT ADMINISTRATOR",
    "System Manager",
    "Administrator",
}


def _has_reference_role() -> bool:
    if frappe.session.user == "Administrator":
        return True
    return bool(set(frappe.get_roles(frappe.session.user)).intersection(REFERENCE_EDIT_ROLES))


def _customer_open_balance(company: str, customer: str) -> float:
    if not company or not customer:
        return 0.0
    row = frappe.db.sql(
        """
        select coalesce(sum(outstanding_amount), 0) as outstanding
        from `tabNKT Customer Receivable`
        where company=%s
          and customer=%s
          and status in ('Open', 'Partially Paid')
        """,
        (company, customer),
        as_dict=True,
    )[0]
    return flt(row.outstanding)


def ensure_customer_receipt_record(customer_order: str):
    """
    Create the immutable balance snapshot companion for a Customer Order.

    The original sale/order remains the authoritative commercial transaction.
    This companion exists only to preserve what the customer-facing Trust
    Receipt should show at finalization and to hold later-known Plate/DR
    references without silently rewriting the sale.
    """
    existing = frappe.db.exists("NKT Customer Receipt Record", customer_order)
    if existing:
        return frappe.get_doc("NKT Customer Receipt Record", existing)

    order = frappe.get_doc("NKT Customer Order", customer_order)
    previous = _customer_open_balance(order.company, order.customer)
    account_charge = flt(order.declared_account)
    after = previous + account_charge

    doc = frappe.get_doc(
        {
            "doctype": "NKT Customer Receipt Record",
            "customer_order": order.name,
            "company": order.company,
            "customer": order.customer,
            "customer_name": order.customer_name,
            "order_date": order.order_date,
            "previous_account_balance": previous,
            "account_charge": account_charge,
            "account_balance_after_order": after,
            "snapshot_at": now_datetime(),
        }
    )
    doc.flags.ignore_permissions = True
    doc.insert()
    return doc


@frappe.whitelist(methods=["POST"])
def update_customer_receipt_references(
    customer_order: str,
    plate_reference: str | None = None,
    dr_reference: str | None = None,
    reason: str | None = None,
    warehouse_release: str | None = None,
):
    if not _has_reference_role():
        frappe.throw(
            _("Only Warehouse / Owner / Administrator may complete customer receipt Plate/DR references."),
            frappe.PermissionError,
        )

    order = frappe.get_doc("NKT Customer Order", customer_order)
    order.check_permission("read")

    if warehouse_release:
        release = frappe.db.get_value(
            "NKT Warehouse Release",
            warehouse_release,
            ["name", "customer_order", "docstatus"],
            as_dict=True,
        )
        if not release:
            frappe.throw(_("Warehouse Release does not exist."))
        if release.customer_order != order.name:
            frappe.throw(_("Warehouse Release belongs to another Customer Order."))

    plate_reference = (plate_reference or "").strip()
    dr_reference = (dr_reference or "").strip()
    reason = (reason or "").strip()

    if not plate_reference and not dr_reference:
        frappe.throw(_("Enter a Plate reference, DR/reference, or both."))
    if not reason:
        frappe.throw(
            _("Reason / source note is required so later reference completion remains auditable.")
        )

    rec = ensure_customer_receipt_record(order.name)
    old_plate = (rec.plate_reference or "").strip()
    old_dr = (rec.dr_reference or "").strip()

    if old_plate == plate_reference and old_dr == dr_reference:
        return {
            "name": rec.name,
            "changed": False,
            "plate_reference": old_plate,
            "dr_reference": old_dr,
        }

    rec.db_set(
        {
            "plate_reference": plate_reference,
            "dr_reference": dr_reference,
            "reference_source_release": warehouse_release,
            "references_updated_by": frappe.session.user,
            "references_updated_at": now_datetime(),
            "reference_update_reason": reason,
        },
        update_modified=True,
    )
    rec.reload()
    rec.add_comment(
        "Info",
        _(
            "Customer receipt delivery references updated. "
            "Plate: '{0}' → '{1}'. DR/Reference: '{2}' → '{3}'. "
            "Source Release: {4}. Reason: {5}"
        ).format(
            old_plate or "—",
            plate_reference or "—",
            old_dr or "—",
            dr_reference or "—",
            warehouse_release or "—",
            reason,
        ),
    )

    return {
        "name": rec.name,
        "changed": True,
        "plate_reference": rec.plate_reference,
        "dr_reference": rec.dr_reference,
        "references_updated_by": rec.references_updated_by,
        "references_updated_at": rec.references_updated_at,
    }
