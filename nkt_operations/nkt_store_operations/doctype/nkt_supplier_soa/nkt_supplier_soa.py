from __future__ import annotations

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import flt, now

TOL = 0.000001
DEDUCTION_KINDS = {
    "Damage Deduction",
    "Shortage Deduction",
    "BPI Sample Deduction",
    "Other Deduction",
}


NKT_C15E_SUPPLIER_SOA_AUTO = "Auto"
NKT_C15E_SUPPLIER_SOA_FORMAT_A = "Rice - Date + BL / Van"
NKT_C15E_SUPPLIER_SOA_FORMAT_B = "Rice - Date Received + DR / Plate"
NKT_C15E_SUPPLIER_SOA_FORMAT_GENERIC = "Other Supplier Payable - Generic"
NKT_C15E_SUPPLIER_SOA_ACTUAL_FORMATS = {
    NKT_C15E_SUPPLIER_SOA_FORMAT_A,
    NKT_C15E_SUPPLIER_SOA_FORMAT_B,
    NKT_C15E_SUPPLIER_SOA_FORMAT_GENERIC,
}


def _nkt_c15e_value(row, fieldname):
    if isinstance(row, dict):
        return row.get(fieldname)
    if hasattr(row, "get"):
        try:
            return row.get(fieldname)
        except Exception:
            pass
    return getattr(row, fieldname, None)


def _nkt_c15e_text(value):
    return str(value or "").strip()


def _nkt_c15e_supplier_receiving_auto_format(row):
    """
    Locked C15E Supplier SOA rule.

    A = imported/containerized supplier arrival carrying BOTH BL + Van/Container.
    B = ordinary supplier truck-load carrying BOTH DR + Plate.
    Generic = other supplier payable/arrival where neither rice layout applies.

    If a record contains both complete A and B identity pairs, A wins because
    BL + Van/Container is the more specific containerized source identity.
    """
    bl_no = _nkt_c15e_text(_nkt_c15e_value(row, "bill_of_lading_no") or _nkt_c15e_value(row, "bl_no"))
    van_no = _nkt_c15e_text(_nkt_c15e_value(row, "internal_vehicle_no") or _nkt_c15e_value(row, "van_no"))
    dr_no = _nkt_c15e_text(
        _nkt_c15e_value(row, "supplier_dr_no")
        or _nkt_c15e_value(row, "supplier_delivery_reference")
        or _nkt_c15e_value(row, "dr_no")
    )
    plate_no = _nkt_c15e_text(_nkt_c15e_value(row, "plate_number"))

    if bl_no and van_no:
        return NKT_C15E_SUPPLIER_SOA_FORMAT_A
    if dr_no and plate_no:
        return NKT_C15E_SUPPLIER_SOA_FORMAT_B
    return NKT_C15E_SUPPLIER_SOA_FORMAT_GENERIC


def _nkt_c15e_group_supplier_receivings(rows):
    """
    Pure deterministic grouping used by the live auto-builder and QA.

    Format A remains one BL per statement (existing NKT rule), therefore two
    different BLs produce two separate A statements. Format B and Generic each
    produce their own statement for the selected supplier/period.
    """
    groups = {}
    order = []
    for row in rows or []:
        fmt = _nkt_c15e_supplier_receiving_auto_format(row)
        bl_no = _nkt_c15e_text(_nkt_c15e_value(row, "bill_of_lading_no") or _nkt_c15e_value(row, "bl_no"))
        key = (fmt, bl_no if fmt == NKT_C15E_SUPPLIER_SOA_FORMAT_A else "")
        if key not in groups:
            groups[key] = {
                "soa_format": fmt,
                "bl_no": bl_no if fmt == NKT_C15E_SUPPLIER_SOA_FORMAT_A else None,
                "receiving_names": [],
            }
            order.append(key)
        name = _nkt_c15e_value(row, "name")
        if name:
            groups[key]["receiving_names"].append(name)
    return [groups[key] for key in order]


class NKTSupplierSOA(Document):
    def validate(self):
        if self.period_start and self.period_end and self.period_start > self.period_end:
            frappe.throw(_("Period Start cannot be after Period End."))

        self._nkt_c15e_resolve_auto_format()

        if self.soa_format == "Rice - Date + BL / Van" and not (self.bl_no or "").strip():
            frappe.throw(_("Header BL No. is required for Rice - Date + BL / Van SOA."))

        delivery_line_numbers = set()
        gross = 0.0
        deductions = 0.0
        additions = 0.0

        for idx, row in enumerate(self.lines or [], start=1):
            row.line_no = idx

            if row.source_doctype == "Manual Adjustment":
                row.source_document = None

            if row.source_document and row.source_doctype:
                if not frappe.db.exists(row.source_doctype, row.source_document):
                    frappe.throw(_("SOA line {0}: source document does not exist.").format(idx))

            qty = flt(row.no_of_bags)
            physical_sample = flt(row.physical_sample_qty)
            chargeable = flt(row.supplier_chargeable_qty)
            price = flt(row.unit_price)

            if qty < -TOL or physical_sample < -TOL or chargeable < -TOL or price < -TOL:
                frappe.throw(_("SOA line {0}: quantities and Unit Price cannot be negative.").format(idx))

            if row.line_kind == "BPI Sample Deduction":
                if chargeable - physical_sample > TOL:
                    frappe.throw(_(
                        "SOA line {0}: BPI Qty Chargeable to Supplier cannot exceed "
                        "Physical Sample Qty Released."
                    ).format(idx))
                basis_qty = chargeable
            else:
                basis_qty = qty

            amount = basis_qty * price
            row.gross_amount = 0
            row.deduction_amount = 0
            row.addition_amount = 0
            row.net_effect = 0

            if row.line_kind == "Delivery Payable":
                row.gross_amount = amount
                row.net_effect = amount
                gross += amount
                delivery_line_numbers.add(idx)
            elif row.line_kind in DEDUCTION_KINDS:
                row.deduction_amount = amount
                row.net_effect = -amount
                deductions += amount
            elif row.line_kind == "Other Addition":
                row.addition_amount = amount
                row.net_effect = amount
                additions += amount
            else:
                frappe.throw(_("SOA line {0}: unsupported line type.").format(idx))

        for row in self.lines or []:
            if row.line_kind in DEDUCTION_KINDS and row.relates_to_line_no:
                if int(row.relates_to_line_no) not in delivery_line_numbers:
                    frappe.throw(_(
                        "SOA line {0}: Related Delivery Line No. {1} does not point "
                        "to a Delivery Payable line."
                    ).format(row.line_no, row.relates_to_line_no))
                if int(row.relates_to_line_no) >= int(row.line_no):
                    frappe.throw(_(
                        "SOA line {0}: deduction must appear after its related Delivery line."
                    ).format(row.line_no))

        self.gross_delivery_amount = gross
        self.total_additions = additions
        self.total_deductions = deductions
        self.net_payable = gross + additions - deductions

        if self.net_payable < -TOL:
            frappe.throw(_("Net Payable cannot be negative."))

        self._validate_statement_lifecycle()

    def _nkt_c15e_resolve_auto_format(self):
        # Historical Prepared/Finalized/Superseded statements retain their
        # stored format. Draft/new statements are always derived from lines.
        if not (self.is_new() or self.status == "Draft"):
            return

        delivery_rows = [r for r in (self.lines or []) if r.line_kind == "Delivery Payable"]

        if not delivery_rows:
            if self.lines:
                # A statement consisting only of manual/non-delivery payable
                # adjustments is the generic supplier-payable layout.
                self.soa_format = NKT_C15E_SUPPLIER_SOA_FORMAT_GENERIC
                self.bl_no = None
            else:
                self.soa_format = NKT_C15E_SUPPLIER_SOA_AUTO
                self.bl_no = None
            return

        formats = {_nkt_c15e_supplier_receiving_auto_format(r) for r in delivery_rows}
        if len(formats) > 1:
            frappe.throw(_(
                "Containerized BL/Van deliveries, ordinary DR/Plate truck-load deliveries, "
                "and generic supplier payables cannot be combined in one Supplier SOA. "
                "Use Auto Build Statement(s); C15E creates separate SOAs automatically."
            ))

        resolved = next(iter(formats))
        self.soa_format = resolved

        if resolved == NKT_C15E_SUPPLIER_SOA_FORMAT_A:
            bls = sorted({_nkt_c15e_text(r.bl_no) for r in delivery_rows if _nkt_c15e_text(r.bl_no)})
            if len(bls) != 1:
                frappe.throw(_(
                    "Containerized Supplier SOA Format A is one BL per statement. "
                    "Use Auto Build Statement(s) so each BL is generated separately."
                ))
            self.bl_no = bls[0]
        else:
            self.bl_no = None

    def _validate_statement_lifecycle(self):
        if self.is_new():
            self.status = "Draft"
            return

        old = self.get_doc_before_save()
        if not old:
            return

        action = bool(getattr(self.flags, "nkt_soa_lifecycle_action", False))
        if self.status != old.status and not action:
            frappe.throw(_("Supplier SOA status is server-controlled. Use controlled lifecycle actions."))

        if old.status in ("Prepared", "Finalized", "Superseded") and not action:
            locked = ("company","supplier","statement_date","soa_format","bl_no","period_start","period_end","notes")
            changed = [f for f in locked if self.get(f) != old.get(f)]

            def sig(doc):
                return [
                    (
                        r.line_kind, r.relates_to_line_no, r.source_doctype, r.source_document,
                        r.date_received, r.bl_no, r.dr_no, r.plate_number, r.van_no,
                        r.item_code, r.commercial_item_description,
                        flt(r.no_of_bags), flt(r.physical_sample_qty),
                        flt(r.supplier_chargeable_qty), flt(r.unit_price),
                        flt(r.gross_amount), flt(r.deduction_amount),
                        flt(r.addition_amount), flt(r.net_effect), r.notes,
                    )
                    for r in doc.lines
                ]

            if sig(self) != sig(old):
                changed.append("lines")

            if changed:
                frappe.throw(_(
                    "Prepared/Finalized/Superseded Supplier SOA is locked statement history. "
                    "Return Prepared to Draft, or supersede a Finalized statement instead of editing: {0}"
                ).format(", ".join(sorted(set(changed)))))

    def on_trash(self):
        if self.status != "Draft":
            frappe.throw(_("Prepared/Finalized/Superseded Supplier SOA cannot be deleted."))

    def before_save(self):
        if not self.status:
            self.status = "Draft"


def _get_po_item_rate(purchase_order_item):
    row = frappe.db.get_value(
        "Purchase Order Item",
        purchase_order_item,
        ["rate", "item_code"],
        as_dict=True,
    )
    if not row:
        frappe.throw(_("Purchase Order Item {0} does not exist.").format(purchase_order_item))
    return flt(row.rate), row.item_code


def _commercial_description(supplier, item_code, fallback):
    value = frappe.db.get_value(
        "NKT Supplier Commercial Item",
        {"supplier": supplier, "item_code": item_code, "status": "Active"},
        "commercial_description",
    )
    return (value or fallback or item_code or "").strip()


def _receiving_identity(receiving, row):
    return {
        "date_received": receiving.receiving_date,
        "bl_no": receiving.bill_of_lading_no,
        "dr_no": receiving.supplier_dr_no or receiving.supplier_delivery_reference,
        "plate_number": receiving.plate_number,
        "van_no": receiving.internal_vehicle_no,
        "item_code": row.item_code,
        "commercial_item_description": _commercial_description(
            receiving.supplier, row.item_code, row.item_name
        ),
    }


def _build_soa_source_lines(soa, receiving_names, bpi_sample_names=None):
    """
    Restricted deterministic source builder.

    It never creates Purchase Invoice / Payment Entry and never alters receiving,
    exception or BPI records. It only returns statement-line dictionaries.

    Delivery basis:
      Expected Qty x PO Unit Price

    Approved supplier deductions:
      Supplier Claimable Qty, with the final agreed deduction amount preserved.
      If the agreed amount differs from PO rate x claimable qty, the statement
      line's displayed Unit Price is derived as agreed amount / claimable qty.

    BPI:
      Physical Sample Qty is preserved for traceability.
      Only Qty Chargeable to Supplier affects the SOA deduction.
    """
    if not receiving_names:
        frappe.throw(_("Select at least one Supplier Arrival source."))

    receiving_names = list(dict.fromkeys(receiving_names))
    bpi_sample_names = list(dict.fromkeys(bpi_sample_names or []))

    lines = []
    delivery_line_by_receiving_po_item = {}

    for receiving_name in receiving_names:
        receiving = frappe.get_doc("NKT Supplier Receiving", receiving_name)

        if receiving.supplier != soa.supplier:
            frappe.throw(_(
                "Supplier Arrival {0} belongs to Supplier {1}, not {2}."
            ).format(receiving.name, receiving.supplier, soa.supplier))
        if receiving.company != soa.company:
            frappe.throw(_(
                "Supplier Arrival {0} belongs to Company {1}, not {2}."
            ).format(receiving.name, receiving.company, soa.company))
        if int(receiving.docstatus or 0) != 1:
            frappe.throw(_("Supplier Arrival {0} must be submitted.").format(receiving.name))
        if not receiving.underlying_purchase_receipt:
            frappe.throw(_(
                "Supplier Arrival {0} has no underlying Purchase Receipt."
            ).format(receiving.name))

        for r in receiving.items:
            rate, po_item_code = _get_po_item_rate(r.purchase_order_item)
            if po_item_code != r.item_code:
                frappe.throw(_("PO Item / Supplier Arrival Item mismatch on {0}.").format(r.item_code))

            identity = _receiving_identity(receiving, r)
            line = {
                "line_kind": "Delivery Payable",
                "source_doctype": "NKT Supplier Receiving",
                "source_document": receiving.name,
                **identity,
                # Gross SOA basis stays visible. Damage/shortage are shown beneath
                # it as deductions rather than silently shrinking this quantity.
                "no_of_bags": flt(r.expected_qty),
                "unit_price": rate,
                "notes": None,
            }
            lines.append(line)
            delivery_line_no = len(lines)
            delivery_line_by_receiving_po_item[(receiving.name, r.purchase_order_item)] = delivery_line_no

        exception_name = frappe.db.exists(
            "NKT Supplier Delivery Exception",
            {"supplier_receiving": receiving.name},
        )
        if not exception_name:
            continue

        ex = frappe.get_doc("NKT Supplier Delivery Exception", exception_name)
        if ex.review_status != "Resolved":
            continue
        if ex.claim_status not in ("Agreed Deduction", "Resolved"):
            continue

        recv_items = {r.purchase_order_item: r for r in receiving.items}

        for erow in ex.items:
            claim_qty = flt(erow.supplier_claimable_qty)
            agreed = flt(erow.agreed_deduction_amount)
            if claim_qty <= TOL or agreed <= TOL:
                continue
            if erow.responsibility not in ("Supplier", "Shared"):
                continue
            if erow.resolution_type != "Deduction":
                continue

            source_row = recv_items.get(erow.purchase_order_item)
            if not source_row:
                frappe.throw(_(
                    "Exception item {0} cannot be matched back to Supplier Arrival {1}."
                ).format(erow.item_code, receiving.name))

            delivery_line_no = delivery_line_by_receiving_po_item.get(
                (receiving.name, erow.purchase_order_item)
            )
            if not delivery_line_no:
                frappe.throw(_("No Delivery Payable line found for exception Item {0}.").format(erow.item_code))

            issue_to_line = {
                "Damaged": "Damage Deduction",
                "Wet": "Damage Deduction",
                "Broken Packaging": "Damage Deduction",
                "Other Rejected": "Damage Deduction",
                "Shortage": "Shortage Deduction",
            }
            line_kind = issue_to_line.get(erow.issue_type)
            if not line_kind:
                continue

            identity = _receiving_identity(receiving, source_row)
            effective_unit_price = agreed / claim_qty

            lines.append({
                "line_kind": line_kind,
                "relates_to_line_no": delivery_line_no,
                "source_doctype": "NKT Supplier Delivery Exception",
                "source_document": ex.name,
                **identity,
                "no_of_bags": claim_qty,
                "unit_price": effective_unit_price,
                "notes": erow.condition_reason or erow.issue_type,
            })

    # BPI comes after the related delivery/exception lines.
    for sample_name in bpi_sample_names:
        sample = frappe.get_doc("NKT BPI Sample Release", sample_name)

        if sample.supplier != soa.supplier:
            frappe.throw(_("BPI Sample {0} belongs to another Supplier.").format(sample.name))
        if sample.company != soa.company:
            frappe.throw(_("BPI Sample {0} belongs to another Company.").format(sample.name))

        chargeable = flt(sample.supplier_chargeable_qty)
        physical = flt(sample.physical_sample_qty)
        if chargeable <= TOL:
            continue
        if sample.charge_status not in ("Partially Chargeable", "Fully Chargeable"):
            continue

        related_receiving = sample.source_supplier_receiving
        if not related_receiving:
            frappe.throw(_(
                "BPI Sample {0} must be linked to a Supplier Arrival for automatic SOA generation."
            ).format(sample.name))

        receiving = frappe.get_doc("NKT Supplier Receiving", related_receiving)
        matching_rows = [r for r in receiving.items if r.item_code == sample.item_code]
        if not matching_rows:
            frappe.throw(_(
                "BPI Sample Item {0} is not present on linked Supplier Arrival {1}."
            ).format(sample.item_code, related_receiving))
        if len(matching_rows) != 1:
            frappe.throw(_(
                "BPI Sample Item {0} appears on multiple PO lines in Supplier Arrival {1}. "
                "Automatic SOA population requires unambiguous source lineage."
            ).format(sample.item_code, related_receiving))
        source_row = matching_rows[0]

        delivery_line_no = delivery_line_by_receiving_po_item.get(
            (related_receiving, source_row.purchase_order_item)
        )
        if not delivery_line_no:
            frappe.throw(_(
                "BPI Sample {0} is linked to a Supplier Arrival not selected for this SOA."
            ).format(sample.name))

        rate, _ = _get_po_item_rate(source_row.purchase_order_item)
        identity = _receiving_identity(receiving, source_row)

        lines.append({
            "line_kind": "BPI Sample Deduction",
            "relates_to_line_no": delivery_line_no,
            "source_doctype": "NKT BPI Sample Release",
            "source_document": sample.name,
            **identity,
            "no_of_bags": 0,
            "physical_sample_qty": physical,
            "supplier_chargeable_qty": chargeable,
            "unit_price": rate,
            "notes": (
                "BPI/sample: {0:g} physically released; {1:g} chargeable to supplier."
            ).format(physical, chargeable),
        })

    return lines


def _populate_soa_from_sources(soa, receiving_names, bpi_sample_names=None):
    if soa.lines:
        frappe.throw(_(
            "Automatic source population requires an empty SOA to prevent duplicate statement lines."
        ))

    rows = _build_soa_source_lines(soa, receiving_names, bpi_sample_names)
    for values in rows:
        soa.append("lines", values)

    soa.run_method("validate")
    return soa


def _assert_supplier_soa_population_permission():
    if not frappe.has_permission("NKT Supplier SOA", ptype="create"):
        frappe.throw(_("You are not permitted to populate Supplier SOA."), frappe.PermissionError)


def _validate_population_period(period_start, period_end):
    from frappe.utils import getdate
    if not period_start or not period_end:
        frappe.throw(_("Period Start and Period End are required before Populate Sources."))
    start = getdate(period_start)
    end = getdate(period_end)
    if start > end:
        frappe.throw(_("Period Start cannot be after Period End."))
    return start, end


def _source_used_in_other_active_soa(source_doctype, source_document, exclude_soa=None):
    if not source_document:
        return None

    rows = frappe.get_all(
        "NKT Supplier SOA Line",
        filters={
            "source_doctype": source_doctype,
            "source_document": source_document,
        },
        fields=["parent"],
        limit_page_length=200,
    )

    for row in rows:
        if exclude_soa and row.parent == exclude_soa:
            continue
        status = frappe.db.get_value("NKT Supplier SOA", row.parent, "status")
        if status and status != "Superseded":
            return row.parent
    return None


def _assert_receiving_financially_ready(receiving_name):
    """
    Never generate a supplier payable from unresolved physical exceptions.

    A clean receiving is ready immediately.
    An exception receiving must be management-resolved first, even when the
    final answer is "No Supplier Claim" / Trucker / NKT responsibility.
    """
    exception_name = frappe.db.exists(
        "NKT Supplier Delivery Exception",
        {"supplier_receiving": receiving_name},
    )
    if not exception_name:
        return None

    ex = frappe.get_doc("NKT Supplier Delivery Exception", exception_name)

    if ex.review_status != "Resolved":
        frappe.throw(_(
            "Supplier Arrival {0} has unresolved delivery exception {1}. "
            "Resolve responsibility before preparing the Supplier SOA."
        ).format(receiving_name, ex.name))

    if ex.claim_status in (
        "Not Yet Determined",
        "Claim Pending",
        "Claimed",
        "Replacement Pending",
        "Credit Memo Pending",
    ):
        frappe.throw(_(
            "Supplier Arrival {0} has delivery exception {1} whose supplier "
            "claim/deduction decision is not final."
        ).format(receiving_name, ex.name))

    pending_rows = [r.idx for r in ex.items if r.responsibility == "Pending Investigation"]
    if pending_rows:
        frappe.throw(_(
            "Supplier Arrival {0} still has exception rows under Pending Investigation."
        ).format(receiving_name))

    return ex


def _eligible_receiving_names(company, supplier, start, end, soa_format, bl_no=None, exclude_soa=None):
    filters = {
        "company": company,
        "supplier": supplier,
        "docstatus": 1,
        "posting_status": "Posted",
        "receiving_date": ["between", [start, end]],
    }

    if soa_format == "Rice - Date + BL / Van" and bl_no:
        filters["bill_of_lading_no"] = bl_no

    names = frappe.get_all(
        "NKT Supplier Receiving",
        filters=filters,
        pluck="name",
        order_by="receiving_date asc, receiving_time asc, creation asc",
        limit_page_length=5000,
    )

    if soa_format == "Rice - Date + BL / Van" and not bl_no:
        bls = sorted({
            x for x in frappe.get_all(
                "NKT Supplier Receiving",
                filters={
                    "company": company,
                    "supplier": supplier,
                    "docstatus": 1,
                    "posting_status": "Posted",
                    "receiving_date": ["between", [start, end]],
                },
                pluck="bill_of_lading_no",
                limit_page_length=5000,
            )
            if x
        })
        if len(bls) == 1:
            bl_no = bls[0]
            filters["bill_of_lading_no"] = bl_no
            names = frappe.get_all(
                "NKT Supplier Receiving",
                filters=filters,
                pluck="name",
                order_by="receiving_date asc, receiving_time asc, creation asc",
                limit_page_length=5000,
            )
        elif len(bls) == 0:
            frappe.throw(_("No submitted Supplier Arrival with a BL No. exists in the selected period."))
        else:
            frappe.throw(_(
                "Format 'Date + BL / Van' is one BL per statement. "
                "Enter Header BL No. first. Eligible BLs in this period: {0}"
            ).format(", ".join(bls[:20])))

    eligible = []
    skipped_used = []
    for name in names:
        used = _source_used_in_other_active_soa("NKT Supplier Receiving", name, exclude_soa)
        if used:
            skipped_used.append({"receiving": name, "soa": used})
            continue
        _assert_receiving_financially_ready(name)
        eligible.append(name)

    return eligible, bl_no, skipped_used


def _eligible_bpi_samples(company, supplier, start, end, receiving_names, exclude_soa=None):
    if not receiving_names:
        return [], []

    rows = frappe.get_all(
        "NKT BPI Sample Release",
        filters={
            "company": company,
            "supplier": supplier,
            "docstatus": 1,
            "stock_posting_status": "Posted",
            "sample_date": ["between", [start, end]],
            "source_supplier_receiving": ["in", receiving_names],
        },
        fields=[
            "name",
            "source_supplier_receiving",
            "charge_status",
            "supplier_chargeable_qty",
        ],
        order_by="sample_date asc, creation asc",
        limit_page_length=5000,
    )

    eligible = []
    skipped_used = []
    for row in rows:
        if row.charge_status == "Pending Management Decision":
            frappe.throw(_(
                "BPI Sample {0} has no final supplier-charge decision yet. "
                "Management must decide Fully / Partially / Not Chargeable before SOA population."
            ).format(row.name))

        used = _source_used_in_other_active_soa("NKT BPI Sample Release", row.name, exclude_soa)
        if used:
            # A BPI source cannot silently disappear while its receiving is being
            # repopulated into a new active SOA.
            frappe.throw(_(
                "BPI Sample {0} is already used in Supplier SOA {1}. "
                "Reuse the existing SOA or supersede it before generating another."
            ).format(row.name, used))

        if flt(row.supplier_chargeable_qty) > TOL:
            eligible.append(row.name)

    return eligible, skipped_used



def _nkt_c15e_auto_eligible_supplier_soa_groups(
    company,
    supplier,
    start,
    end,
    exclude_soa=None,
):
    rows = frappe.get_all(
        "NKT Supplier Receiving",
        filters={
            "company": company,
            "supplier": supplier,
            "docstatus": 1,
            "posting_status": "Posted",
            "receiving_date": ["between", [start, end]],
        },
        fields=[
            "name",
            "receiving_date",
            "receiving_time",
            "bill_of_lading_no",
            "supplier_dr_no",
            "supplier_delivery_reference",
            "internal_vehicle_no",
            "plate_number",
            "creation",
        ],
        order_by="receiving_date asc, receiving_time asc, creation asc",
        limit_page_length=5000,
    )

    eligible_rows = []
    skipped_used = []
    for row in rows:
        used = _source_used_in_other_active_soa(
            "NKT Supplier Receiving",
            row.name,
            exclude_soa,
        )
        if used:
            skipped_used.append({"receiving": row.name, "soa": used})
            continue
        _assert_receiving_financially_ready(row.name)
        eligible_rows.append(row)

    groups = _nkt_c15e_group_supplier_receivings(eligible_rows)

    for group in groups:
        bpi_names, skipped_bpi = _eligible_bpi_samples(
            company,
            supplier,
            start,
            end,
            group["receiving_names"],
            exclude_soa,
        )
        group["bpi_sample_names"] = bpi_names
        group["skipped_bpi_already_used"] = skipped_bpi

    return groups, skipped_used


def _nkt_c15e_apply_auto_group_to_soa(
    doc,
    group,
    company,
    supplier,
    statement_date,
    period_start,
    period_end,
):
    doc.company = company
    doc.supplier = supplier
    doc.statement_date = statement_date
    doc.period_start = period_start
    doc.period_end = period_end
    doc.soa_format = group["soa_format"]
    doc.bl_no = group.get("bl_no")
    _populate_soa_from_sources(
        doc,
        group["receiving_names"],
        group.get("bpi_sample_names") or [],
    )
    return doc


@frappe.whitelist()
def auto_build_supplier_soas(name):
    """
    C15E locked behavior: no manual A/B/Generic selector.

    The saved empty Draft is populated with the first deterministic group.
    Additional BLs and/or different supplier-delivery layouts are created as
    separate companion Draft Supplier SOAs automatically.
    """
    _assert_supplier_soa_population_permission()

    doc = frappe.get_doc("NKT Supplier SOA", name)
    if not frappe.has_permission("NKT Supplier SOA", ptype="write", doc=doc):
        frappe.throw(
            _("You do not have write permission for Supplier SOA {0}.").format(name),
            frappe.PermissionError,
        )
    if doc.status != "Draft":
        frappe.throw(_("Auto Build Statement(s) is available only for a Draft Supplier SOA."))
    if doc.lines:
        frappe.throw(_(
            "Auto Build Statement(s) requires an empty Draft Supplier SOA to prevent duplicate statement lines."
        ))
    if not doc.company or not doc.supplier:
        frappe.throw(_("Company and Supplier are required before Auto Build Statement(s)."))

    start, end = _validate_population_period(doc.period_start, doc.period_end)
    groups, skipped_receivings = _nkt_c15e_auto_eligible_supplier_soa_groups(
        doc.company,
        doc.supplier,
        start,
        end,
        doc.name,
    )

    if not groups:
        frappe.throw(_(
            "No unused, financially-ready Supplier Arrival exists for the selected supplier and period."
        ))

    created = []
    first = groups[0]
    _nkt_c15e_apply_auto_group_to_soa(
        doc,
        first,
        doc.company,
        doc.supplier,
        doc.statement_date or frappe.utils.nowdate(),
        start,
        end,
    )
    doc.save()
    created.append({
        "name": doc.name,
        "soa_format": doc.soa_format,
        "bl_no": doc.bl_no,
        "receiving_names": first["receiving_names"],
        "is_current": True,
    })

    for group in groups[1:]:
        companion = frappe.new_doc("NKT Supplier SOA")
        companion.notes = (
            "Automatically split by C15E Supplier SOA rules from {0}. "
            "Do not combine containerized BL/Van, ordinary DR/Plate truck-load, "
            "or generic supplier payable layouts."
        ).format(doc.name)
        _nkt_c15e_apply_auto_group_to_soa(
            companion,
            group,
            doc.company,
            doc.supplier,
            doc.statement_date or frappe.utils.nowdate(),
            start,
            end,
        )
        companion.insert()
        created.append({
            "name": companion.name,
            "soa_format": companion.soa_format,
            "bl_no": companion.bl_no,
            "receiving_names": group["receiving_names"],
            "is_current": False,
        })

    return {
        "primary_soa": doc.name,
        "created_soas": created,
        "companion_soas": [row["name"] for row in created if not row["is_current"]],
        "skipped_receivings_already_used": skipped_receivings,
        "format_a_count": sum(1 for row in created if row["soa_format"] == NKT_C15E_SUPPLIER_SOA_FORMAT_A),
        "format_b_count": sum(1 for row in created if row["soa_format"] == NKT_C15E_SUPPLIER_SOA_FORMAT_B),
        "generic_count": sum(1 for row in created if row["soa_format"] == NKT_C15E_SUPPLIER_SOA_FORMAT_GENERIC),
    }


@frappe.whitelist()
def get_soa_population_payload(
    company,
    supplier,
    period_start,
    period_end,
    soa_format,
    bl_no=None,
    current_soa=None,
):
    """
    Restricted Fast Screen Populate Sources action.

    Returns calculated draft statement lines only. It does not create or finalize
    an SOA, Purchase Invoice, Payment Entry, or GL transaction.
    """
    _assert_supplier_soa_population_permission()
    start, end = _validate_population_period(period_start, period_end)

    auto_groups, skipped_receivings = _nkt_c15e_auto_eligible_supplier_soa_groups(
        company,
        supplier,
        start,
        end,
        current_soa,
    )
    if len(auto_groups) != 1:
        frappe.throw(_(
            "Manual Supplier SOA format selection is disabled. "
            "Use Auto Build Statement(s); the selected scope resolves to {0} separate statement group(s)."
        ).format(len(auto_groups)))

    auto_group = auto_groups[0]
    soa_format = auto_group["soa_format"]
    resolved_bl = auto_group.get("bl_no")
    receiving_names = auto_group["receiving_names"]

    if not receiving_names:
        frappe.throw(_("No unused, financially-ready Supplier Arrival exists for the selected statement scope."))

    bpi_names = auto_group.get("bpi_sample_names") or []
    skipped_bpi = auto_group.get("skipped_bpi_already_used") or []

    temp = frappe.new_doc("NKT Supplier SOA")
    temp.company = company
    temp.supplier = supplier
    temp.statement_date = frappe.utils.nowdate()
    temp.period_start = start
    temp.period_end = end
    temp.soa_format = soa_format
    if soa_format == "Rice - Date + BL / Van":
        temp.bl_no = resolved_bl

    _populate_soa_from_sources(temp, receiving_names, bpi_names)

    line_fields = [
        "line_no", "line_kind", "relates_to_line_no",
        "source_doctype", "source_document",
        "date_received", "bl_no", "dr_no", "plate_number", "van_no",
        "item_code", "commercial_item_description",
        "no_of_bags", "physical_sample_qty", "supplier_chargeable_qty",
        "unit_price", "gross_amount", "deduction_amount", "addition_amount",
        "net_effect", "notes",
    ]

    return {
        "resolved_bl_no": resolved_bl,
        "receiving_names": receiving_names,
        "bpi_sample_names": bpi_names,
        "skipped_receivings_already_used": skipped_receivings,
        "lines": [
            {fieldname: row.get(fieldname) for fieldname in line_fields}
            for row in temp.lines
        ],
        "totals": {
            "gross_delivery_amount": temp.gross_delivery_amount,
            "total_additions": temp.total_additions,
            "total_deductions": temp.total_deductions,
            "net_payable": temp.net_payable,
        },
    }


def _supplier_soa_has_active_payment(soa_name):
    parents = frappe.get_all(
        "NKT Supplier Payment Allocation",
        filters={"supplier_soa": soa_name},
        pluck="parent",
        limit_page_length=5000,
    )
    for parent in parents:
        status = frappe.db.get_value("NKT Supplier Payment", parent, "payment_status")
        if status and status != "Cancelled":
            return parent

    # C15E: a posted advance application is also active paid history against
    # this finalized SOA and must block silent supersede.
    if frappe.db.exists("DocType", "NKT Supplier Advance Application"):
        advance = frappe.db.sql(
            """
            select h.name
            from `tabNKT Supplier Advance Application Allocation` a
            inner join `tabNKT Supplier Advance Application` h on h.name=a.parent
            where a.supplier_soa=%s and h.docstatus=1
            limit 1
            """,
            (soa_name,),
            as_dict=True,
        )
        if advance:
            return "Supplier Advance Application " + advance[0].name
    return None


def _prepare_supplier_soa(doc):
    if doc.status != "Draft":
        frappe.throw(_("Only Draft Supplier SOA can be Prepared."))
    if not doc.lines:
        frappe.throw(_("Supplier SOA must have statement lines before Prepare."))

    doc.flags.nkt_soa_lifecycle_action = True
    try:
        # Authorization covers the entire controlled validation + transition.
        doc.run_method("validate")
        if flt(doc.net_payable) <= TOL:
            frappe.throw(_("Supplier SOA Net Payable must be greater than zero before Prepare."))

        doc.status = "Prepared"
        doc.prepared_by_user = frappe.session.user
        doc.prepared_at = now()
        doc.save()
    finally:
        doc.flags.nkt_soa_lifecycle_action = False

    return doc


def _return_supplier_soa_to_draft(doc):
    if doc.status != "Prepared":
        frappe.throw(_("Only Prepared Supplier SOA can be returned to Draft."))

    doc.flags.nkt_soa_lifecycle_action = True
    try:
        doc.status = "Draft"
        doc.prepared_by_user = None
        doc.prepared_at = None
        doc.save()
    finally:
        doc.flags.nkt_soa_lifecycle_action = False

    return doc


def _finalize_supplier_soa(doc):
    if doc.status != "Prepared":
        frappe.throw(_("Only Prepared Supplier SOA can be Finalized."))

    doc.flags.nkt_soa_lifecycle_action = True
    try:
        # Frappe may still carry the prior before-save snapshot on this same object.
        # The controlled authorization must therefore already be active here.
        doc.run_method("validate")
        if flt(doc.net_payable) <= TOL:
            frappe.throw(_("Supplier SOA Net Payable must be greater than zero before Finalize."))

        doc.status = "Finalized"
        doc.finalized_by_user = frappe.session.user
        doc.finalized_at = now()
        doc.save()
    finally:
        doc.flags.nkt_soa_lifecycle_action = False

    return doc


def _supersede_supplier_soa(doc, reason):
    if doc.status != "Finalized":
        frappe.throw(_("Only Finalized Supplier SOA can be Superseded."))

    active = _supplier_soa_has_active_payment(doc.name)
    if active:
        frappe.throw(_(
            "Supplier SOA {0} cannot be Superseded while active Supplier Payment {1} still reserves or pays it."
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



SUPPLIER_SOA_OPERATION_ROLES = {
    "NKT Purchasing",
    "NKT ADMINISTRATOR",
    "NKT OWNER",
    "Administrator",
}

SUPPLIER_SOA_MANAGEMENT_ROLES = {
    "NKT ADMINISTRATOR",
    "NKT OWNER",
    "Administrator",
}


def _has_supplier_soa_role(allowed_roles):
    return bool(set(frappe.get_roles(frappe.session.user)).intersection(allowed_roles))


def _load_supplier_soa_for_lifecycle(name):
    if not _has_supplier_soa_role(SUPPLIER_SOA_OPERATION_ROLES):
        frappe.throw(
            _("You are not permitted to operate Supplier SOA."),
            frappe.PermissionError,
        )

    doc = frappe.get_doc("NKT Supplier SOA", name)
    if not frappe.has_permission("NKT Supplier SOA", ptype="write", doc=doc):
        frappe.throw(
            _("You do not have write permission for Supplier SOA {0}.").format(name),
            frappe.PermissionError,
        )
    return doc


def _require_supplier_soa_management():
    if not _has_supplier_soa_role(SUPPLIER_SOA_MANAGEMENT_ROLES):
        frappe.throw(
            _("Only NKT Owner / Administrator may Finalize or Supersede Supplier SOA."),
            frappe.PermissionError,
        )


@frappe.whitelist()
def prepare_supplier_soa(name):
    doc = _load_supplier_soa_for_lifecycle(name)
    _prepare_supplier_soa(doc)
    return {
        "name": doc.name,
        "status": doc.status,
        "prepared_by_user": doc.prepared_by_user,
        "prepared_at": doc.prepared_at,
        "net_payable": doc.net_payable,
    }


@frappe.whitelist()
def return_supplier_soa_to_draft(name):
    doc = _load_supplier_soa_for_lifecycle(name)
    _return_supplier_soa_to_draft(doc)
    return {
        "name": doc.name,
        "status": doc.status,
    }


@frappe.whitelist()
def finalize_supplier_soa(name):
    _require_supplier_soa_management()
    doc = _load_supplier_soa_for_lifecycle(name)
    _finalize_supplier_soa(doc)
    return {
        "name": doc.name,
        "status": doc.status,
        "finalized_by_user": doc.finalized_by_user,
        "finalized_at": doc.finalized_at,
        "net_payable": doc.net_payable,
    }


@frappe.whitelist()
def supersede_supplier_soa(name, reason):
    _require_supplier_soa_management()
    doc = _load_supplier_soa_for_lifecycle(name)
    _supersede_supplier_soa(doc, reason)
    return {
        "name": doc.name,
        "status": doc.status,
        "superseded_by_user": doc.superseded_by_user,
        "superseded_at": doc.superseded_at,
        "supersede_reason": doc.supersede_reason,
    }
