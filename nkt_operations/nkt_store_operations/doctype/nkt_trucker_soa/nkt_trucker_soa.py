from __future__ import annotations

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import flt, now

TOL = 0.000001
DEDUCTION_KINDS = {
    "Wet Cargo Deduction",
    "Damage Deduction",
    "Shortlanded Deduction",
    "Other Deduction",
}


class NKTTruckerSOA(Document):
    def validate(self):
        if self.period_start and self.period_end and self.period_start > self.period_end:
            frappe.throw(_("Period Start cannot be after Period End."))

        gross = 0.0
        deductions = 0.0
        additions = 0.0
        haul_line_numbers = set()

        for idx, row in enumerate(self.lines or [], start=1):
            row.line_no = idx

            qty = flt(row.no_of_bags)
            rate = flt(row.rate)

            if qty < -TOL or rate < -TOL:
                frappe.throw(_("Trucker SOA line {0}: Qty and Rate cannot be negative.").format(idx))

            amount = qty * rate
            row.gross_amount = 0
            row.deduction_amount = 0
            row.addition_amount = 0
            row.net_effect = 0

            if row.line_kind == "Haul Payable":
                if not row.trucking_job:
                    frappe.throw(_("Trucker SOA line {0}: Haul Payable needs a Trucking Job.").format(idx))
                row.gross_amount = amount
                row.net_effect = amount
                gross += amount
                haul_line_numbers.add(idx)

            elif row.line_kind in DEDUCTION_KINDS:
                if not row.source_trucker_adjustment:
                    frappe.throw(_(
                        "Trucker SOA line {0}: deduction requires a Trucker Adjustment source."
                    ).format(idx))
                row.deduction_amount = amount
                row.net_effect = -amount
                deductions += amount

            elif row.line_kind == "Other Addition":
                row.addition_amount = amount
                row.net_effect = amount
                additions += amount

            else:
                frappe.throw(_("Trucker SOA line {0}: unsupported Line Type.").format(idx))

        for row in self.lines or []:
            if row.line_kind in DEDUCTION_KINDS:
                if not row.relates_to_line_no:
                    frappe.throw(_(
                        "Trucker SOA deduction line {0} must reference its affected Haul line."
                    ).format(row.line_no))
                if int(row.relates_to_line_no) not in haul_line_numbers:
                    frappe.throw(_(
                        "Trucker SOA line {0}: Related Haul Line No. {1} is not a Haul Payable line."
                    ).format(row.line_no, row.relates_to_line_no))
                if int(row.relates_to_line_no) >= int(row.line_no):
                    frappe.throw(_(
                        "Trucker deduction line {0} must appear after the affected Haul line."
                    ).format(row.line_no))

        self.gross_haul_amount = gross
        self.total_additions = additions
        self.total_deductions = deductions
        self.net_payable = gross + additions - deductions

        if self.net_payable < -TOL:
            frappe.throw(_("Trucker SOA Net Payable cannot be negative."))

        self._validate_statement_lifecycle()

    def _validate_statement_lifecycle(self):
        if self.is_new():
            self.status = "Draft"
            return

        old = self.get_doc_before_save()
        if not old:
            return

        action = bool(getattr(self.flags, "nkt_soa_lifecycle_action", False))
        if self.status != old.status and not action:
            frappe.throw(_("Trucker SOA status is server-controlled. Use controlled lifecycle actions."))

        if old.status in ("Prepared", "Finalized", "Superseded") and not action:
            locked = ("company","carrier_account","statement_date","period_start","period_end","notes")
            changed = [f for f in locked if self.get(f) != old.get(f)]

            def sig(doc):
                return [
                    (
                        r.line_kind, r.relates_to_line_no, r.trucking_job, r.source_trucker_adjustment,
                        r.job_date, r.dr_no, r.plate_number, r.internal_vehicle_no,
                        r.item_code, r.item_name, flt(r.no_of_bags), flt(r.rate),
                        flt(r.gross_amount), flt(r.deduction_amount),
                        flt(r.addition_amount), flt(r.net_effect), r.notes,
                    )
                    for r in doc.lines
                ]

            if sig(self) != sig(old):
                changed.append("lines")

            if changed:
                frappe.throw(_(
                    "Prepared/Finalized/Superseded Trucker SOA is locked statement history. "
                    "Return Prepared to Draft, or supersede a Finalized statement instead of editing: {0}"
                ).format(", ".join(sorted(set(changed)))))

    def on_trash(self):
        if self.status != "Draft":
            frappe.throw(_("Prepared/Finalized/Superseded Trucker SOA cannot be deleted."))

    def before_save(self):
        if not self.status:
            self.status = "Draft"


TRUCKER_DEDUCTION_STATUS_FINAL = {
    "No Trucker Deduction",
    "Agreed Deduction",
    "Resolved",
}


def _trucker_soa_source_used(trucking_job, exclude_soa=None):
    rows = frappe.get_all(
        "NKT Trucker SOA Line",
        filters={
            "trucking_job": trucking_job,
            "line_kind": "Haul Payable",
        },
        fields=["parent"],
        limit_page_length=100,
    )
    for row in rows:
        if exclude_soa and row.parent == exclude_soa:
            continue
        status = frappe.db.get_value("NKT Trucker SOA", row.parent, "status")
        if status and status != "Superseded":
            return row.parent
    return None


def _assert_trucking_job_financially_ready(job):
    """
    Clean haul: ready immediately.

    Haul with a physical Supplier Delivery Exception:
    - requires one Trucker Adjustment
    - Trucker Adjustment review must be Resolved
    - deduction decision must be final
    - no row may remain Pending Investigation

    This prevents NKT from preparing a trucking payable while responsibility is
    still unresolved.
    """
    exception_name = frappe.db.exists(
        "NKT Supplier Delivery Exception",
        {"supplier_receiving": job.source_supplier_receiving},
    )
    if not exception_name:
        return None

    adjustment_name = frappe.db.exists(
        "NKT Trucker Adjustment",
        {"trucking_job": job.name},
    )
    if not adjustment_name:
        frappe.throw(_(
            "Trucking Job {0} has physical delivery issues but no Trucker Adjustment yet."
        ).format(job.name))

    adjustment = frappe.get_doc("NKT Trucker Adjustment", adjustment_name)

    if adjustment.review_status != "Resolved":
        frappe.throw(_(
            "Trucking Job {0} has unresolved Trucker Adjustment {1}."
        ).format(job.name, adjustment.name))

    if adjustment.deduction_status not in TRUCKER_DEDUCTION_STATUS_FINAL:
        frappe.throw(_(
            "Trucking Job {0} has no final trucker deduction decision."
        ).format(job.name))

    pending = [row.idx for row in adjustment.items if row.responsibility == "Pending Investigation"]
    if pending:
        frappe.throw(_(
            "Trucking Job {0} still has trucking responsibility under Pending Investigation."
        ).format(job.name))

    return adjustment


def _resolve_haul_rate(rate_by_job, job_name):
    try:
        rate = flt(rate_by_job[job_name])
    except Exception:
        frappe.throw(_(
            "A restricted hauling Rate is required for Trucking Job {0}."
        ).format(job_name))

    if rate <= TOL:
        frappe.throw(_(
            "Hauling Rate for Trucking Job {0} must be greater than zero."
        ).format(job_name))
    return rate


def _build_trucker_soa_source_lines(soa, trucking_job_names, rate_by_job):
    """
    PRIVATE C9H.1 source generator.

    Physical source:
      NKT Trucking Job

    Gross hauling basis:
      Hauled / Delivered Qty x restricted management-supplied hauling Rate

    Approved trucking deductions:
      Qty Chargeable to Trucker, with line Rate derived from:
        Agreed Trucker Deduction / Qty Chargeable to Trucker

    This preserves the exact agreed trucking deduction instead of assuming the
    deduction uses the hauling rate.

    Production Populate Sources remains locked until rollback acceptance.
    """
    if not trucking_job_names:
        frappe.throw(_("Select at least one Trucking Job source."))

    names = list(dict.fromkeys(trucking_job_names))
    rate_by_job = rate_by_job or {}
    lines = []

    for job_name in names:
        job = frappe.get_doc("NKT Trucking Job", job_name)

        if job.company != soa.company:
            frappe.throw(_("Trucking Job {0} belongs to another Company.").format(job.name))
        if job.carrier_account != soa.carrier_account:
            frappe.throw(_("Trucking Job {0} belongs to another Carrier / Trucker.").format(job.name))
        if job.job_status != "Recorded":
            frappe.throw(_("Trucking Job {0} must be Recorded.").format(job.name))

        used = _trucker_soa_source_used(
            job.name,
            exclude_soa=soa.name if not soa.is_new() else None,
        )
        if used:
            frappe.throw(_(
                "Trucking Job {0} is already used in active Trucker SOA {1}."
            ).format(job.name, used))

        adjustment = _assert_trucking_job_financially_ready(job)
        haul_rate = _resolve_haul_rate(rate_by_job, job.name)

        haul_line_by_po_item = {}

        for source in job.items:
            if flt(source.hauled_qty) <= TOL:
                continue

            lines.append({
                "line_kind": "Haul Payable",
                "trucking_job": job.name,
                "job_date": job.job_date,
                "dr_no": job.dr_no,
                "plate_number": job.plate_number,
                "internal_vehicle_no": job.internal_vehicle_no,
                "item_code": source.item_code,
                "item_name": source.item_name,
                "no_of_bags": flt(source.hauled_qty),
                "rate": haul_rate,
                "notes": None,
            })
            haul_line_no = len(lines)
            haul_line_by_po_item[source.purchase_order_item] = haul_line_no

        if not adjustment:
            continue

        exception = frappe.get_doc(
            "NKT Supplier Delivery Exception",
            adjustment.source_supplier_exception,
        )
        exception_rows = {row.name: row for row in exception.items}

        issue_to_line = {
            "Wet Cargo": "Wet Cargo Deduction",
            "Damage": "Damage Deduction",
            "Shortlanded": "Shortlanded Deduction",
            "Other": "Other Deduction",
        }

        for row in adjustment.items:
            chargeable = flt(row.trucker_chargeable_qty)
            agreed = flt(row.agreed_trucker_deduction_amount)

            if row.responsibility not in ("Trucker", "Shared"):
                continue
            if row.resolution_type != "Deduction":
                continue
            if chargeable <= TOL or agreed <= TOL:
                continue

            source_exception = exception_rows.get(row.source_exception_item)
            if not source_exception:
                frappe.throw(_(
                    "Trucker Adjustment row {0} cannot be traced to its physical Supplier Delivery Exception row."
                ).format(row.idx))

            haul_line_no = haul_line_by_po_item.get(source_exception.purchase_order_item)
            if not haul_line_no:
                frappe.throw(_(
                    "No Haul Payable line found for Trucker Adjustment row {0}."
                ).format(row.idx))

            line_kind = issue_to_line.get(row.issue_type)
            if not line_kind:
                continue

            effective_rate = agreed / chargeable

            lines.append({
                "line_kind": line_kind,
                "relates_to_line_no": haul_line_no,
                "trucking_job": job.name,
                "source_trucker_adjustment": adjustment.name,
                "job_date": job.job_date,
                "dr_no": job.dr_no,
                "plate_number": job.plate_number,
                "internal_vehicle_no": job.internal_vehicle_no,
                "item_code": row.item_code,
                "item_name": row.item_name,
                "no_of_bags": chargeable,
                "rate": effective_rate,
                "notes": row.physical_reason or row.issue_type,
            })

    return lines


def _populate_trucker_soa_from_sources(soa, trucking_job_names, rate_by_job):
    if soa.lines:
        frappe.throw(_(
            "Automatic Trucker SOA source population requires an empty statement "
            "to prevent duplicate lines."
        ))

    rows = _build_trucker_soa_source_lines(soa, trucking_job_names, rate_by_job)
    for values in rows:
        soa.append("lines", values)

    soa.run_method("validate")
    return soa


TRUCKER_SOA_ROLES = {"NKT Purchasing", "NKT ADMINISTRATOR", "NKT OWNER", "Administrator"}


def _require_trucker_soa_role():
    roles = set(frappe.get_roles(frappe.session.user))
    if not roles.intersection(TRUCKER_SOA_ROLES):
        frappe.throw(_("You are not permitted to prepare Trucker SOA."), frappe.PermissionError)


def _validate_trucker_soa_scope(company, carrier_account, period_start, period_end):
    from frappe.utils import getdate

    if not company or not carrier_account:
        frappe.throw(_("Company and Carrier / Trucker are required."))
    if not period_start or not period_end:
        frappe.throw(_("Period Start and Period End are required."))

    start = getdate(period_start)
    end = getdate(period_end)
    if start > end:
        frappe.throw(_("Period Start cannot be after Period End."))

    return start, end


def _job_item_summary(job):
    parts = []
    total_hauled = 0.0

    for row in job.items:
        qty = flt(row.hauled_qty)
        total_hauled += qty
        parts.append("{0} ({1:g})".format(row.item_name or row.item_code, qty))

    return ", ".join(parts), total_hauled


def _job_in_scope(job, company, carrier_account, start, end):
    from frappe.utils import getdate

    if job.company != company:
        frappe.throw(_("Trucking Job {0} belongs to another Company.").format(job.name))
    if job.carrier_account != carrier_account:
        frappe.throw(_("Trucking Job {0} belongs to another Carrier / Trucker.").format(job.name))
    if job.job_status != "Recorded":
        frappe.throw(_("Trucking Job {0} must be Recorded.").format(job.name))

    job_date = getdate(job.job_date)
    if job_date < start or job_date > end:
        frappe.throw(_("Trucking Job {0} is outside the selected statement period.").format(job.name))


@frappe.whitelist()
def get_trucker_soa_population_candidates(
    company,
    carrier_account,
    period_start,
    period_end,
    current_soa=None,
):
    """
    Restricted production discovery for the Fast Screen rate-entry dialog.

    No money is read from the operational Trucking Job because none exists there.
    Each eligible haul intentionally returns Rate = 0 for management entry.
    """
    _require_trucker_soa_role()
    start, end = _validate_trucker_soa_scope(
        company, carrier_account, period_start, period_end
    )

    names = frappe.get_all(
        "NKT Trucking Job",
        filters={
            "company": company,
            "carrier_account": carrier_account,
            "job_status": "Recorded",
            "job_date": ["between", [start, end]],
        },
        pluck="name",
        order_by="job_date asc, creation asc",
        limit_page_length=5000,
    )

    eligible = []
    blocked = []
    already_used = []

    for name in names:
        job = frappe.get_doc("NKT Trucking Job", name)

        used = _trucker_soa_source_used(job.name, exclude_soa=current_soa)
        if used:
            already_used.append({
                "trucking_job": job.name,
                "dr_no": job.dr_no,
                "existing_trucker_soa": used,
            })
            continue

        try:
            _assert_trucking_job_financially_ready(job)
        except Exception as exc:
            blocked.append({
                "trucking_job": job.name,
                "dr_no": job.dr_no,
                "reason": str(exc),
            })
            continue

        item_summary, total_hauled = _job_item_summary(job)
        eligible.append({
            "include": 1,
            "trucking_job": job.name,
            "job_date": job.job_date,
            "dr_no": job.dr_no,
            "plate_number": job.plate_number,
            "internal_vehicle_no": job.internal_vehicle_no,
            "item_summary": item_summary,
            "hauled_qty": total_hauled,
            "rate": 0,
        })

    return {
        "eligible": eligible,
        "blocked": blocked,
        "already_used": already_used,
    }


@frappe.whitelist()
def get_trucker_soa_population_payload(
    company,
    carrier_account,
    period_start,
    period_end,
    rate_rows,
    current_soa=None,
):
    """
    Restricted production Populate Sources action.

    `rate_rows` is JSON:
      [{"trucking_job": "...", "rate": 20.0}, ...]

    Rates are statement-preparation inputs only. They are snapshotted onto SOA lines;
    C9H.2 does not create a permanent universal trucking-rate master.
    """
    _require_trucker_soa_role()
    start, end = _validate_trucker_soa_scope(
        company, carrier_account, period_start, period_end
    )

    if isinstance(rate_rows, str):
        try:
            rate_rows = frappe.parse_json(rate_rows)
        except Exception:
            frappe.throw(_("Invalid hauling-rate payload."))

    rate_rows = rate_rows or []
    if not isinstance(rate_rows, list) or not rate_rows:
        frappe.throw(_("Select at least one eligible Trucking Job."))

    job_names = []
    rate_by_job = {}

    for row in rate_rows:
        job_name = (row or {}).get("trucking_job")
        rate = flt((row or {}).get("rate"))

        if not job_name:
            frappe.throw(_("Every selected haul needs a Trucking Job."))
        if job_name in rate_by_job:
            frappe.throw(_("Trucking Job {0} appears more than once.").format(job_name))
        if rate <= TOL:
            frappe.throw(_("Enter a hauling Rate greater than zero for {0}.").format(job_name))

        job = frappe.get_doc("NKT Trucking Job", job_name)
        _job_in_scope(job, company, carrier_account, start, end)

        used = _trucker_soa_source_used(job.name, exclude_soa=current_soa)
        if used:
            frappe.throw(_(
                "Trucking Job {0} is already used in active Trucker SOA {1}."
            ).format(job.name, used))

        _assert_trucking_job_financially_ready(job)

        job_names.append(job.name)
        rate_by_job[job.name] = rate

    temp = frappe.new_doc("NKT Trucker SOA")
    temp.company = company
    temp.carrier_account = carrier_account
    temp.statement_date = frappe.utils.nowdate()
    temp.period_start = start
    temp.period_end = end

    _populate_trucker_soa_from_sources(temp, job_names, rate_by_job)

    line_fields = [
        "line_no", "line_kind", "relates_to_line_no",
        "trucking_job", "source_trucker_adjustment",
        "job_date", "dr_no", "plate_number", "internal_vehicle_no",
        "item_code", "item_name", "no_of_bags", "rate",
        "gross_amount", "deduction_amount", "addition_amount",
        "net_effect", "notes",
    ]

    return {
        "trucking_jobs": job_names,
        "lines": [
            {fieldname: row.get(fieldname) for fieldname in line_fields}
            for row in temp.lines
        ],
        "totals": {
            "gross_haul_amount": temp.gross_haul_amount,
            "total_additions": temp.total_additions,
            "total_deductions": temp.total_deductions,
            "net_payable": temp.net_payable,
        },
    }


def _trucker_soa_has_active_payment(soa_name):
    parents = frappe.get_all(
        "NKT Trucker Payment Allocation",
        filters={"trucker_soa": soa_name},
        pluck="parent",
        limit_page_length=5000,
    )
    for parent in parents:
        status = frappe.db.get_value("NKT Trucker Payment", parent, "payment_status")
        if status and status != "Cancelled":
            return parent
    return None


def _prepare_trucker_soa(doc):
    if doc.status != "Draft":
        frappe.throw(_("Only Draft Trucker SOA can be Prepared."))
    if not doc.lines:
        frappe.throw(_("Trucker SOA must have statement lines before Prepare."))

    doc.flags.nkt_soa_lifecycle_action = True
    try:
        doc.run_method("validate")
        if flt(doc.net_payable) <= TOL:
            frappe.throw(_("Trucker SOA Net Payable must be greater than zero before Prepare."))

        doc.status = "Prepared"
        doc.prepared_by_user = frappe.session.user
        doc.prepared_at = now()
        doc.save()
    finally:
        doc.flags.nkt_soa_lifecycle_action = False

    return doc


def _return_trucker_soa_to_draft(doc):
    if doc.status != "Prepared":
        frappe.throw(_("Only Prepared Trucker SOA can be returned to Draft."))

    doc.flags.nkt_soa_lifecycle_action = True
    try:
        doc.status = "Draft"
        doc.prepared_by_user = None
        doc.prepared_at = None
        doc.save()
    finally:
        doc.flags.nkt_soa_lifecycle_action = False

    return doc


def _finalize_trucker_soa(doc):
    if doc.status != "Prepared":
        frappe.throw(_("Only Prepared Trucker SOA can be Finalized."))

    doc.flags.nkt_soa_lifecycle_action = True
    try:
        doc.run_method("validate")
        if flt(doc.net_payable) <= TOL:
            frappe.throw(_("Trucker SOA Net Payable must be greater than zero before Finalize."))

        doc.status = "Finalized"
        doc.finalized_by_user = frappe.session.user
        doc.finalized_at = now()
        doc.save()
    finally:
        doc.flags.nkt_soa_lifecycle_action = False

    return doc


def _supersede_trucker_soa(doc, reason):
    if doc.status != "Finalized":
        frappe.throw(_("Only Finalized Trucker SOA can be Superseded."))

    active = _trucker_soa_has_active_payment(doc.name)
    if active:
        frappe.throw(_(
            "Trucker SOA {0} cannot be Superseded while active Trucker Payment {1} still reserves or pays it."
        ).format(doc.name, active))

    reason = (reason or "").strip()
    if not reason:
        frappe.throw(_("Supersede Reason is required."))

    doc.flags.nkt_soa_lifecycle_action = True
    try:
        doc.status = "Superseded"
        doc.superseded_by_user = frappe.session.user
        doc.superseded_at = now()
        doc.supersede_reason = reason
        doc.save()
    finally:
        doc.flags.nkt_soa_lifecycle_action = False

    return doc



TRUCKER_SOA_OPERATION_ROLES = {
    "NKT Purchasing",
    "NKT ADMINISTRATOR",
    "NKT OWNER",
    "Administrator",
}

TRUCKER_SOA_MANAGEMENT_ROLES = {
    "NKT ADMINISTRATOR",
    "NKT OWNER",
    "Administrator",
}


def _has_trucker_soa_role(allowed_roles):
    return bool(set(frappe.get_roles(frappe.session.user)).intersection(allowed_roles))


def _load_trucker_soa_for_lifecycle(name):
    if not _has_trucker_soa_role(TRUCKER_SOA_OPERATION_ROLES):
        frappe.throw(
            _("You are not permitted to operate Trucker SOA."),
            frappe.PermissionError,
        )

    doc = frappe.get_doc("NKT Trucker SOA", name)
    if not frappe.has_permission("NKT Trucker SOA", ptype="write", doc=doc):
        frappe.throw(
            _("You do not have write permission for Trucker SOA {0}.").format(name),
            frappe.PermissionError,
        )
    return doc


def _require_trucker_soa_management():
    if not _has_trucker_soa_role(TRUCKER_SOA_MANAGEMENT_ROLES):
        frappe.throw(
            _("Only NKT Owner / Administrator may Finalize or Supersede Trucker SOA."),
            frappe.PermissionError,
        )


@frappe.whitelist()
def prepare_trucker_soa(name):
    doc = _load_trucker_soa_for_lifecycle(name)
    _prepare_trucker_soa(doc)
    return {
        "name": doc.name,
        "status": doc.status,
        "prepared_by_user": doc.prepared_by_user,
        "prepared_at": doc.prepared_at,
        "net_payable": doc.net_payable,
    }


@frappe.whitelist()
def return_trucker_soa_to_draft(name):
    doc = _load_trucker_soa_for_lifecycle(name)
    _return_trucker_soa_to_draft(doc)
    return {
        "name": doc.name,
        "status": doc.status,
    }


@frappe.whitelist()
def finalize_trucker_soa(name):
    _require_trucker_soa_management()
    doc = _load_trucker_soa_for_lifecycle(name)
    _finalize_trucker_soa(doc)
    return {
        "name": doc.name,
        "status": doc.status,
        "finalized_by_user": doc.finalized_by_user,
        "finalized_at": doc.finalized_at,
        "net_payable": doc.net_payable,
    }


@frappe.whitelist()
def supersede_trucker_soa(name, reason):
    _require_trucker_soa_management()
    doc = _load_trucker_soa_for_lifecycle(name)
    _supersede_trucker_soa(doc, reason)
    return {
        "name": doc.name,
        "status": doc.status,
        "superseded_by_user": doc.superseded_by_user,
        "superseded_at": doc.superseded_at,
        "supersede_reason": doc.supersede_reason,
    }
