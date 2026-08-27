from __future__ import annotations

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import flt

TOL = 0.000001


class NKTTruckerAdjustment(Document):
    def validate(self):
        job = frappe.get_doc("NKT Trucking Job", self.trucking_job)

        if not job.carrier_account:
            frappe.throw(_(
                "Trucking Job {0} needs a Carrier / Trucker Account before a financial adjustment can be created."
            ).format(job.name))

        self.company = job.company
        self.carrier_account = job.carrier_account
        self.source_supplier_receiving = job.source_supplier_receiving
        self.job_date = job.job_date
        self.dr_no = job.dr_no
        self.plate_number = job.plate_number
        self.internal_vehicle_no = job.internal_vehicle_no
        self.vehicle_operator = job.vehicle_operator

        exception_name = frappe.db.exists(
            "NKT Supplier Delivery Exception",
            {"supplier_receiving": job.source_supplier_receiving},
        )
        self.source_supplier_exception = exception_name or None

        existing = frappe.db.exists(
            "NKT Trucker Adjustment",
            {
                "trucking_job": job.name,
                "name": ["!=", self.name or ""],
            },
        )
        if existing:
            frappe.throw(_("Trucking Job already has Trucker Adjustment {0}.").format(existing))

        if not self.items:
            frappe.throw(_("At least one Trucker Adjustment item is required."))

        gross = 0.0
        agreed = 0.0

        for row in self.items:
            physical = flt(row.physical_issue_qty)
            chargeable = flt(row.trucker_chargeable_qty)

            if physical <= TOL:
                frappe.throw(_("Physical Issue Qty must be greater than zero."))
            if chargeable < -TOL or chargeable - physical > TOL:
                frappe.throw(_(
                    "Qty Chargeable to Trucker must be between zero and the Physical Issue Qty."
                ))

            if row.responsibility in ("Supplier", "NKT / Internal"):
                if chargeable > TOL:
                    frappe.throw(_(
                        "Qty Chargeable to Trucker must be zero when responsibility is {0}."
                    ).format(row.responsibility))

            if row.responsibility == "Pending Investigation":
                if chargeable > TOL or flt(row.agreed_trucker_deduction_amount) > TOL:
                    frappe.throw(_(
                        "Do not charge the trucker while responsibility is Pending Investigation."
                    ))

            if row.responsibility == "Trucker" and row.resolution_type == "Deduction":
                if chargeable <= TOL:
                    frappe.throw(_("Trucker deduction requires Qty Chargeable to Trucker."))

            gross += flt(row.claimed_amount)
            agreed += flt(row.agreed_trucker_deduction_amount)

        self.gross_claim_amount = gross
        self.agreed_trucker_deduction_amount = agreed


ISSUE_TYPE_MAP = {
    "Damaged": "Damage",
    "Wet": "Wet Cargo",
    "Broken Packaging": "Damage",
    "Other Rejected": "Other",
    "Shortage": "Shortlanded",
}


def _populate_from_supplier_exception(adjustment):
    """
    PRIVATE C9G.1 source bridge.

    Copies the already-recorded physical exception facts into the restricted
    Trucker Adjustment. It does NOT copy supplier financial responsibility.
    Trucker responsibility/charge remains a separate management decision.
    """
    if not adjustment.trucking_job:
        frappe.throw(_("Trucking Job is required."))

    job = frappe.get_doc("NKT Trucking Job", adjustment.trucking_job)

    exception_name = frappe.db.exists(
        "NKT Supplier Delivery Exception",
        {"supplier_receiving": job.source_supplier_receiving},
    )
    if not exception_name:
        frappe.throw(_(
            "Trucking Job {0} has no linked physical Supplier Delivery Exception."
        ).format(job.name))

    ex = frappe.get_doc("NKT Supplier Delivery Exception", exception_name)

    adjustment.source_supplier_exception = ex.name
    adjustment.set("items", [])

    for source in ex.items:
        mapped = ISSUE_TYPE_MAP.get(source.issue_type)
        if not mapped:
            continue

        row = adjustment.append("items", {})
        row.source_exception_item = source.name
        row.item_code = source.item_code
        row.item_name = source.item_name
        row.issue_type = mapped
        row.physical_issue_qty = flt(source.issue_qty)
        row.physical_reason = source.condition_reason

        # Financial responsibility is NEVER copied from the Supplier claim layer.
        row.responsibility = "Pending Investigation"
        row.trucker_chargeable_qty = 0
        row.claimed_amount = 0
        row.agreed_trucker_deduction_amount = 0

    if not adjustment.items:
        frappe.throw(_("Linked Supplier Delivery Exception has no trucking-relevant physical rows."))

    return adjustment


TRUCKER_FINANCIAL_ROLES = {"NKT Purchasing", "NKT ADMINISTRATOR", "NKT OWNER", "Administrator"}


def _require_trucker_financial_role():
    roles = set(frappe.get_roles(frappe.session.user))
    if not roles.intersection(TRUCKER_FINANCIAL_ROLES):
        frappe.throw(_("You are not permitted to manage Trucker Adjustments."), frappe.PermissionError)


@frappe.whitelist()
def get_trucker_adjustment_source_payload(trucking_job):
    """
    Production-safe Fast Screen loader.

    Returns only physical truth copied from the linked Supplier Delivery Exception.
    It does not set Trucker responsibility, chargeable quantity, or money.
    """
    _require_trucker_financial_role()

    if not trucking_job:
        frappe.throw(_("Trucking Job is required."))

    temp = frappe.new_doc("NKT Trucker Adjustment")
    temp.trucking_job = trucking_job
    _populate_from_supplier_exception(temp)

    job = frappe.get_doc("NKT Trucking Job", trucking_job)
    if not job.carrier_account:
        frappe.throw(_("Trucking Job requires Carrier / Trucker Account first."))

    return {
        "company": job.company,
        "carrier_account": job.carrier_account,
        "source_supplier_receiving": job.source_supplier_receiving,
        "source_supplier_exception": temp.source_supplier_exception,
        "job_date": job.job_date,
        "dr_no": job.dr_no,
        "plate_number": job.plate_number,
        "internal_vehicle_no": job.internal_vehicle_no,
        "vehicle_operator": job.vehicle_operator,
        "items": [
            {
                "source_exception_item": row.source_exception_item,
                "item_code": row.item_code,
                "item_name": row.item_name,
                "issue_type": row.issue_type,
                "physical_issue_qty": row.physical_issue_qty,
                "physical_reason": row.physical_reason,
                "responsibility": "Pending Investigation",
                "trucker_chargeable_qty": 0,
                "claimed_amount": 0,
                "agreed_trucker_deduction_amount": 0,
            }
            for row in temp.items
        ],
    }
