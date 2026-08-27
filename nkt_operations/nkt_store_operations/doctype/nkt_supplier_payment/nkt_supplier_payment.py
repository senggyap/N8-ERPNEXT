from __future__ import annotations

import re

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import flt, getdate, now, nowdate

TOL = 0.000001


def _normalize_reference(value):
    value = (value or "").strip().upper()
    value = re.sub(r"\s+", " ", value)
    return value


def _find_company_outgoing_check_duplicate(
    company,
    issuing_bank,
    check_no,
    exclude_supplier_payment=None,
):
    """
    Company-wide outgoing check identity across BOTH restricted payment registers.

    Same physical NKT check must not be reusable merely because one obligation is
    Supplier and another is Trucker.
    """
    bank = (issuing_bank or "").strip().upper()
    number = (check_no or "").strip().upper()

    if not bank or not number:
        return None

    supplier_filters = {
        "company": company,
        "payment_method": "Management-Issued Check",
        "issuing_bank": bank,
        "check_no": number,
    }
    if exclude_supplier_payment:
        supplier_filters["name"] = ["!=", exclude_supplier_payment]

    supplier_payment = frappe.db.exists("NKT Supplier Payment", supplier_filters)
    if supplier_payment:
        return {
            "doctype": "NKT Supplier Payment",
            "name": supplier_payment,
        }

    trucker_payment = frappe.db.exists(
        "NKT Trucker Payment",
        {
            "company": company,
            "payment_method": "Management-Issued Check",
            "issuing_bank": bank,
            "check_no": number,
        },
    )
    if trucker_payment:
        return {
            "doctype": "NKT Trucker Payment",
            "name": trucker_payment,
        }

    return None


class NKTSupplierPayment(Document):
    def validate(self):
        if flt(self.payment_amount) <= TOL:
            frappe.throw(_("Payment Amount must be greater than zero."))

        if getdate(self.payment_date) > getdate(nowdate()):
            frappe.throw(_("Payment / Preparation Date cannot be in the future."))

        self.bank_name = _normalize_reference(self.bank_name)
        self.bank_reference = _normalize_reference(self.bank_reference)
        self.issuing_bank = _normalize_reference(self.issuing_bank)
        self.check_no = _normalize_reference(self.check_no)

        if self.payment_method == "Management-Issued Check":
            self._validate_check()
        else:
            self.check_status = "Not Applicable"
            self.check_replacement_for = None

        if self.payment_method == "Bank Transfer":
            if not self.bank_name:
                frappe.throw(_("Bank / Source Account Label is required for Bank Transfer."))
            if not self.bank_reference:
                frappe.throw(_("Bank Transfer / Payment Reference is required for Bank Transfer."))
            if not self.bank_reference_datetime:
                frappe.throw(_("Reference Date & Time is required for Bank Transfer."))

        self._validate_allocations()
        self._validate_lifecycle_integrity()

        if self.is_new():
            self.payment_status = "Draft"
            self.prepared_by = frappe.session.user
            self.prepared_at = now()

        if self.approved_by and not self.approved_at:
            self.approved_at = now()
        if self.signed_by and not self.signed_at:
            self.signed_at = now()

    def _validate_lifecycle_integrity(self):
        """
        Once a payment is Prepared/Released/Cancelled, the money identity and SOA
        allocations become locked history. Lifecycle actions may change only the
        server-controlled status/audit fields via an internal flag.
        """
        if self.is_new():
            return

        old = self.get_doc_before_save()
        if not old:
            return

        controlled = {
            "payment_status", "check_status",
            "approved_by", "approved_at",
            "signed_by", "signed_at",
            "released_at", "replacement_check",
        }

        if not getattr(self.flags, "nkt_lifecycle_action", False):
            changed_controlled = [
                fld for fld in controlled
                if self.get(fld) != old.get(fld)
            ]
            if changed_controlled:
                frappe.throw(_(
                    "Payment/check lifecycle fields are server-controlled. "
                    "Use the Supplier Payment action buttons."
                ))

        # Financial/core identity locks as soon as the record leaves Draft.
        if old.payment_status != "Draft":
            locked_fields = {
                "company", "supplier", "payment_date", "payment_method",
                "payment_amount", "bank_name", "bank_reference",
                "bank_reference_datetime", "issuing_bank", "check_no",
                "check_date", "check_replacement_for", "purpose",
            }
            changed = [fld for fld in locked_fields if self.get(fld) != old.get(fld)]

            old_alloc = [
                (r.supplier_soa, flt(r.allocated_amount))
                for r in old.allocations
            ]
            new_alloc = [
                (r.supplier_soa, flt(r.allocated_amount))
                for r in self.allocations
            ]
            if old_alloc != new_alloc:
                changed.append("allocations")

            if changed:
                frappe.throw(_(
                    "Prepared/Released Supplier Payment is locked financial history. "
                    "Cancel/replace through the controlled lifecycle instead of editing: {0}"
                ).format(", ".join(sorted(set(changed)))))

    def on_trash(self):
        if self.payment_status != "Draft":
            frappe.throw(_(
                "Prepared/Released/Cancelled Supplier Payment cannot be deleted. "
                "Historical payment records must remain auditable."
            ))

    def _validate_check(self):
        if not self.issuing_bank:
            frappe.throw(_("Issuing Bank is required for a management-issued check."))
        if not self.check_no:
            frappe.throw(_("Check No. is required for a management-issued check."))
        if not self.check_date:
            frappe.throw(_("Check Date is required for a management-issued check."))

        duplicate = _find_company_outgoing_check_duplicate(
            self.company,
            self.issuing_bank,
            self.check_no,
            exclude_supplier_payment=self.name if not self.is_new() else None,
        )
        if duplicate:
            frappe.throw(_(
                "Management-issued Check {0} from {1} is already used in {2} {3}."
            ).format(
                self.check_no,
                self.issuing_bank,
                duplicate["doctype"],
                duplicate["name"],
            ))

        if self.check_status == "Not Applicable":
            self.check_status = "Prepared"

        if self.check_replacement_for:
            old = frappe.get_doc("NKT Supplier Payment", self.check_replacement_for)
            if old.payment_method != "Management-Issued Check":
                frappe.throw(_("Replacement For must reference a management-issued check."))
            if old.company != self.company:
                frappe.throw(_("Replacement check must belong to the same Company."))
            if old.supplier != self.supplier:
                frappe.throw(_("Replacement check must belong to the same Supplier / Payee."))
            if old.check_status not in ("Cancelled", "Replaced", "Stale"):
                frappe.throw(_(
                    "Original check must be Cancelled, Replaced, or Stale before a replacement is linked."
                ))

    def _validate_allocations(self):
        seen = set()
        allocated = 0.0

        for row in self.allocations or []:
            if row.supplier_soa in seen:
                frappe.throw(_("Supplier SOA {0} is allocated more than once.").format(row.supplier_soa))
            seen.add(row.supplier_soa)

            soa = frappe.db.get_value(
                "NKT Supplier SOA",
                row.supplier_soa,
                ["company", "supplier", "statement_date", "net_payable", "status"],
                as_dict=True,
            )
            if not soa:
                frappe.throw(_("Supplier SOA {0} does not exist.").format(row.supplier_soa))
            if soa.company != self.company:
                frappe.throw(_("Supplier SOA {0} belongs to another Company.").format(row.supplier_soa))
            if soa.supplier != self.supplier:
                frappe.throw(_("Supplier SOA {0} belongs to another Supplier.").format(row.supplier_soa))
            if soa.status != "Finalized":
                frappe.throw(_(
                    "Supplier SOA {0} must be Finalized before payment allocation."
                ).format(row.supplier_soa))

            amount = flt(row.allocated_amount)
            if amount <= TOL:
                frappe.throw(_("Allocated Amount must be greater than zero."))

            balance = _get_supplier_soa_operational_balance(
                row.supplier_soa,
                exclude_payment=self.name if not self.is_new() else None,
            )
            if amount - flt(balance.available_to_allocate) > TOL:
                frappe.throw(_(
                    "Supplier SOA {0} has only {1} available to allocate. "
                    "Prepared/Signed active payments reserve their allocations even "
                    "before they reduce the released-payment outstanding balance."
                ).format(row.supplier_soa, frappe.format_value(
                    balance.available_to_allocate, {"fieldtype": "Currency"}
                )))

            row.soa_statement_date = soa.statement_date
            row.soa_net_payable = soa.net_payable
            allocated += amount

        if allocated - flt(self.payment_amount) > TOL:
            frappe.throw(_("Total SOA allocation cannot exceed Payment Amount."))

        self.allocated_amount = allocated
        self.unallocated_amount = max(flt(self.payment_amount) - allocated, 0)


ACTIVE_CHECK_RESERVATION_STATUSES = {"Prepared", "Signed", "Released", "Deposited", "Cleared"}
SETTLED_CHECK_STATUSES = {"Released", "Deposited", "Cleared"}
NON_RESERVING_CHECK_STATUSES = {"Cancelled", "Replaced", "Stale"}

CHECK_TRANSITIONS = {
    "Prepared": {"Signed", "Cancelled", "Stale"},
    "Signed": {"Released", "Cancelled", "Stale"},
    "Released": {"Deposited", "Cleared", "Cancelled", "Stale"},
    "Deposited": {"Cleared", "Cancelled", "Stale"},
    "Cleared": set(),
    "Cancelled": {"Replaced"},
    "Stale": {"Replaced"},
    "Replaced": set(),
}

PAYMENT_TRANSITIONS = {
    "Draft": {"Prepared", "Cancelled"},
    "Prepared": {"Released", "Cancelled"},
    "Released": set(),
    "Cancelled": set(),
}


def _payment_reserves_allocation(payment):
    if payment.payment_status == "Cancelled":
        return False

    if payment.payment_method == "Management-Issued Check":
        return payment.check_status in ACTIVE_CHECK_RESERVATION_STATUSES

    return payment.payment_status in ("Draft", "Prepared", "Released")


def _payment_counts_as_released(payment):
    """
    Operational payment truth:
    - Prepared/Signed never reduce outstanding.
    - Released payment reduces outstanding.
    - Check Deposited/Cleared continues to count.
    - Cancelled/Replaced/Stale check does not count.
    """
    if payment.payment_status != "Released":
        return False

    if payment.payment_method == "Management-Issued Check":
        return payment.check_status in SETTLED_CHECK_STATUSES

    return True


def _get_supplier_soa_operational_balance(soa_name, exclude_payment=None, exclude_advance_application=None):
    soa = frappe.db.get_value(
        "NKT Supplier SOA",
        soa_name,
        ["name", "company", "supplier", "status", "net_payable"],
        as_dict=True,
    )
    if not soa:
        frappe.throw(_("Supplier SOA {0} does not exist.").format(soa_name))

    net = flt(soa.net_payable)
    reserved = 0.0
    released = 0.0

    allocations = frappe.get_all(
        "NKT Supplier Payment Allocation",
        filters={"supplier_soa": soa_name},
        fields=["parent", "allocated_amount"],
        limit_page_length=5000,
    )

    payment_cache = {}
    for alloc in allocations:
        if exclude_payment and alloc.parent == exclude_payment:
            continue

        if alloc.parent not in payment_cache:
            payment_cache[alloc.parent] = frappe.db.get_value(
                "NKT Supplier Payment",
                alloc.parent,
                ["name", "payment_method", "payment_status", "check_status"],
                as_dict=True,
            )

        payment = payment_cache.get(alloc.parent)
        if not payment:
            continue

        amount = flt(alloc.allocated_amount)

        if _payment_reserves_allocation(payment):
            reserved += amount

        if _payment_counts_as_released(payment):
            released += amount

    direct_released = released

    # C15E: later applications of a previously Released Supplier Advance are
    # separate immutable application records. They count as both reserved and
    # released against the chosen SOA, without mutating the original payment.
    advance_released = 0.0
    if frappe.db.exists("DocType", "NKT Supplier Advance Application"):
        app_rows = frappe.db.sql(
            """
            select a.parent, a.allocated_amount
            from `tabNKT Supplier Advance Application Allocation` a
            inner join `tabNKT Supplier Advance Application` h
                on h.name=a.parent
            where a.supplier_soa=%s
              and h.docstatus=1
              and (%s is null or h.name<>%s)
            """,
            (soa_name, exclude_advance_application, exclude_advance_application),
            as_dict=True,
        )
        advance_released = sum(flt(r.allocated_amount) for r in app_rows)
        reserved += advance_released
        released += advance_released

    return frappe._dict({
        "supplier_soa": soa.name,
        "company": soa.company,
        "supplier": soa.supplier,
        "soa_status": soa.status,
        "net_payable": net,
        "active_reserved_amount": reserved,
        "released_paid_amount": released,
        "released_direct_payment_amount": direct_released,
        "released_advance_application_amount": advance_released,
        "outstanding_amount": max(net - released, 0),
        "available_to_allocate": max(net - reserved, 0),
    })




def _get_supplier_payment_advance_balance(payment_name, exclude_advance_application=None):
    payment = frappe.get_doc("NKT Supplier Payment", payment_name)

    initial_allocated = sum(flt(r.allocated_amount) for r in (payment.allocations or []))
    original_unallocated = max(flt(payment.payment_amount) - initial_allocated, 0)

    applied_later = 0.0
    applications = []
    if frappe.db.exists("DocType", "NKT Supplier Advance Application"):
        rows = frappe.db.sql(
            """
            select h.name as application, h.application_date,
                   coalesce(sum(a.allocated_amount),0) as applied
            from `tabNKT Supplier Advance Application` h
            inner join `tabNKT Supplier Advance Application Allocation` a
                on a.parent=h.name
            where h.supplier_payment=%s
              and h.docstatus=1
              and (%s is null or h.name<>%s)
            group by h.name, h.application_date
            order by h.application_date asc, h.name asc
            """,
            (payment_name, exclude_advance_application, exclude_advance_application),
            as_dict=True,
        )
        applications = [dict(r) for r in rows]
        applied_later = sum(flt(r.applied) for r in rows)

    eligible = _payment_counts_as_released(payment)
    available = max(original_unallocated - applied_later, 0) if eligible else 0

    return frappe._dict({
        "supplier_payment": payment.name,
        "company": payment.company,
        "supplier": payment.supplier,
        "payment_status": payment.payment_status,
        "check_status": payment.check_status,
        "payment_amount": flt(payment.payment_amount),
        "initial_allocated_amount": initial_allocated,
        "original_unallocated_advance": original_unallocated,
        "applied_later_amount": applied_later,
        "available_advance_balance": available,
        "eligible_for_advance_application": bool(eligible and original_unallocated > TOL),
        "applications": applications,
    })


def _get_supplier_operational_balance(company, supplier):
    soas = frappe.get_all(
        "NKT Supplier SOA",
        filters={
            "company": company,
            "supplier": supplier,
            "status": "Finalized",
        },
        pluck="name",
        limit_page_length=5000,
    )

    rows = [_get_supplier_soa_operational_balance(name) for name in soas]

    return frappe._dict({
        "company": company,
        "supplier": supplier,
        "soa_count": len(rows),
        "net_payable_total": sum(flt(r.net_payable) for r in rows),
        "active_reserved_total": sum(flt(r.active_reserved_amount) for r in rows),
        "released_paid_total": sum(flt(r.released_paid_amount) for r in rows),
        "outstanding_total": sum(flt(r.outstanding_amount) for r in rows),
        "available_to_allocate_total": sum(flt(r.available_to_allocate) for r in rows),
        "soa_balances": [dict(r) for r in rows],
    })


def _transition_payment_status(payment, new_status):
    """
    Private C9F.1 non-check lifecycle engine.

    Production UI remains locked. This is rollback-tested before any action buttons
    are exposed.
    """
    if payment.payment_method == "Management-Issued Check":
        frappe.throw(_("Use the check lifecycle transition for management-issued checks."))

    current = payment.payment_status or "Draft"
    allowed = PAYMENT_TRANSITIONS.get(current, set())
    if new_status not in allowed:
        frappe.throw(_(
            "Invalid payment status transition: {0} -> {1}."
        ).format(current, new_status))

    payment.payment_status = new_status
    payment.flags.nkt_lifecycle_action = True
    payment.save()
    return payment


def _transition_check_status(payment, new_status):
    """
    Private outgoing-check lifecycle engine.

    Synchronizes Payment Status:
    Prepared/Signed -> Prepared
    Released/Deposited/Cleared -> Released
    Cancelled/Replaced/Stale -> Cancelled
    """
    if payment.payment_method != "Management-Issued Check":
        frappe.throw(_("Check lifecycle is only for Management-Issued Check."))

    current = payment.check_status or "Prepared"
    allowed = CHECK_TRANSITIONS.get(current, set())
    if new_status not in allowed:
        frappe.throw(_(
            "Invalid check status transition: {0} -> {1}."
        ).format(current, new_status))

    payment.check_status = new_status

    if new_status in ("Prepared", "Signed"):
        payment.payment_status = "Prepared"
    elif new_status in ("Released", "Deposited", "Cleared"):
        payment.payment_status = "Released"
    elif new_status in ("Cancelled", "Replaced", "Stale"):
        payment.payment_status = "Cancelled"

    if new_status == "Released" and not payment.released_at:
        payment.released_at = now()

    payment.flags.nkt_lifecycle_action = True
    payment.save()
    return payment


def _link_replacement_check(original, replacement):
    if original.payment_method != "Management-Issued Check":
        frappe.throw(_("Original payment is not a management-issued check."))
    if replacement.payment_method != "Management-Issued Check":
        frappe.throw(_("Replacement payment is not a management-issued check."))

    if replacement.check_replacement_for != original.name:
        frappe.throw(_("Replacement check must reference the original check."))

    if original.check_status not in ("Cancelled", "Stale"):
        frappe.throw(_("Original check must be Cancelled or Stale before replacement."))

    if replacement.company != original.company or replacement.supplier != original.supplier:
        frappe.throw(_("Replacement check must have the same Company and Supplier."))

    original.check_status = "Replaced"
    original.payment_status = "Cancelled"
    original.replacement_check = replacement.name
    original.flags.nkt_lifecycle_action = True
    original.save()

    return original


MANAGEMENT_ROLES = {"NKT ADMINISTRATOR", "NKT OWNER", "Administrator"}
PAYMENT_OPERATIONS_ROLES = MANAGEMENT_ROLES | {"NKT Purchasing"}


def _has_any_role(roles):
    current = set(frappe.get_roles(frappe.session.user))
    return bool(current.intersection(set(roles)))


def _require_payment_operations():
    if not _has_any_role(PAYMENT_OPERATIONS_ROLES):
        frappe.throw(_("You are not permitted to operate Supplier Payments."), frappe.PermissionError)


def _require_payment_management():
    if not _has_any_role(MANAGEMENT_ROLES):
        frappe.throw(_(
            "Only NKT Owner / Administrator may approve, sign, cancel, stale, or replace supplier payments."
        ), frappe.PermissionError)


def _load_payment_for_action(name):
    _require_payment_operations()
    doc = frappe.get_doc("NKT Supplier Payment", name)
    if not frappe.has_permission("NKT Supplier Payment", ptype="write", doc=doc):
        frappe.throw(_("You do not have write permission for Supplier Payment {0}.").format(name), frappe.PermissionError)
    return doc


@frappe.whitelist()
def get_supplier_payment_balance(company, supplier):
    _require_payment_operations()
    return dict(_get_supplier_operational_balance(company, supplier))


@frappe.whitelist()
def get_supplier_advance_balance(name):
    _require_payment_operations()
    return dict(_get_supplier_payment_advance_balance(name))


@frappe.whitelist()
def approve_supplier_payment(name):
    """
    Management approval.

    Check:
      remains check-status Prepared, payment-status becomes Prepared.
    Non-check:
      Draft -> Prepared.
    """
    _require_payment_management()
    doc = _load_payment_for_action(name)

    if doc.payment_status != "Draft":
        frappe.throw(_("Only Draft Supplier Payment can be approved."))

    doc.approved_by = frappe.session.user
    doc.approved_at = now()
    doc.flags.nkt_lifecycle_action = True

    if doc.payment_method == "Management-Issued Check":
        if doc.check_status != "Prepared":
            frappe.throw(_("Draft management-issued check must be in Prepared check status."))
        doc.payment_status = "Prepared"
        doc.save()
    else:
        _transition_payment_status(doc, "Prepared")

    return {
        "name": doc.name,
        "payment_status": doc.payment_status,
        "check_status": doc.check_status,
        "approved_by": doc.approved_by,
        "approved_at": doc.approved_at,
    }


@frappe.whitelist()
def sign_supplier_check(name):
    _require_payment_management()
    doc = _load_payment_for_action(name)

    if doc.payment_method != "Management-Issued Check":
        frappe.throw(_("Sign Check is only for Management-Issued Check."))
    if doc.payment_status != "Prepared" or doc.check_status != "Prepared":
        frappe.throw(_("Only an approved Prepared check can be signed."))
    if not doc.approved_by:
        frappe.throw(_("Approve the check before signing it."))

    doc.signed_by = frappe.session.user
    doc.signed_at = now()
    doc.flags.nkt_lifecycle_action = True
    _transition_check_status(doc, "Signed")

    return {
        "name": doc.name,
        "payment_status": doc.payment_status,
        "check_status": doc.check_status,
        "signed_by": doc.signed_by,
        "signed_at": doc.signed_at,
    }


@frappe.whitelist()
def release_supplier_payment(name):
    _require_payment_operations()
    doc = _load_payment_for_action(name)

    if not doc.approved_by:
        frappe.throw(_("Supplier Payment must be approved before release."))

    if doc.payment_method == "Management-Issued Check":
        if doc.check_status != "Signed":
            frappe.throw(_("Management-issued check must be Signed before Release."))
        if not doc.signed_by:
            frappe.throw(_("Signed By is required before check release."))
        if not (doc.released_to or "").strip():
            frappe.throw(_("Released To / Received By is required before releasing a check."))
        _transition_check_status(doc, "Released")
    else:
        if doc.payment_status != "Prepared":
            frappe.throw(_("Only Prepared payment can be Released."))
        _transition_payment_status(doc, "Released")

    return {
        "name": doc.name,
        "payment_status": doc.payment_status,
        "check_status": doc.check_status,
        "released_at": doc.released_at,
    }


@frappe.whitelist()
def mark_supplier_check_deposited(name):
    _require_payment_operations()
    doc = _load_payment_for_action(name)
    if doc.payment_method != "Management-Issued Check":
        frappe.throw(_("Only a management-issued check can be marked Deposited."))
    if doc.check_status != "Released":
        frappe.throw(_("Only a Released check can be marked Deposited."))
    _transition_check_status(doc, "Deposited")
    return {"name": doc.name, "check_status": doc.check_status}


@frappe.whitelist()
def mark_supplier_check_cleared(name):
    _require_payment_operations()
    doc = _load_payment_for_action(name)
    if doc.payment_method != "Management-Issued Check":
        frappe.throw(_("Only a management-issued check can be marked Cleared."))
    if doc.check_status not in ("Released", "Deposited"):
        frappe.throw(_("Only a Released or Deposited check can be marked Cleared."))
    _transition_check_status(doc, "Cleared")
    return {"name": doc.name, "check_status": doc.check_status}


@frappe.whitelist()
def cancel_supplier_payment(name):
    _require_payment_management()
    doc = _load_payment_for_action(name)

    if doc.payment_method == "Management-Issued Check":
        if doc.check_status not in ("Prepared", "Signed", "Released", "Deposited"):
            frappe.throw(_("This check status cannot be Cancelled."))
        _transition_check_status(doc, "Cancelled")
    else:
        if doc.payment_status not in ("Draft", "Prepared"):
            frappe.throw(_(
                "Released non-check Supplier Payment cannot be cancelled directly. "
                "Use a controlled correction record."
            ))
        _transition_payment_status(doc, "Cancelled")

    return {
        "name": doc.name,
        "payment_status": doc.payment_status,
        "check_status": doc.check_status,
    }


@frappe.whitelist()
def mark_supplier_check_stale(name):
    _require_payment_management()
    doc = _load_payment_for_action(name)
    if doc.payment_method != "Management-Issued Check":
        frappe.throw(_("Only a management-issued check can be marked Stale."))
    if doc.check_status not in ("Prepared", "Signed", "Released", "Deposited"):
        frappe.throw(_("This check status cannot be marked Stale."))
    _transition_check_status(doc, "Stale")
    return {
        "name": doc.name,
        "payment_status": doc.payment_status,
        "check_status": doc.check_status,
    }


@frappe.whitelist()
def link_supplier_replacement_check(name):
    _require_payment_management()
    replacement = _load_payment_for_action(name)

    if replacement.payment_method != "Management-Issued Check":
        frappe.throw(_("Replacement record must be a Management-Issued Check."))
    if not replacement.check_replacement_for:
        frappe.throw(_("Set Replacement For before linking the replacement check."))

    original = frappe.get_doc("NKT Supplier Payment", replacement.check_replacement_for)
    _link_replacement_check(original, replacement)

    return {
        "replacement": replacement.name,
        "original": original.name,
        "original_check_status": original.check_status,
        "original_replacement_check": original.replacement_check,
    }
