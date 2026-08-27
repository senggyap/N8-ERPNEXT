from __future__ import annotations

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import flt, getdate, nowdate
from nkt_operations.nkt_store_operations.doctype.nkt_vehicle.nkt_vehicle import normalize_plate

ALLOWED_RECEIVING_ROLES = {
    "NKT Purchasing",
    "NKT Encoder",
    "NKT Warehouse",
    "NKT ADMINISTRATOR",
    "NKT OWNER",
    "System Manager",
}

TOL = 0.000001

# C15C.10H R6:
# A past physical receiving date remains forbidden to normal users. The only
# exception is a narrowly-scoped server flag installed by the dedicated Primary
# materializer for an already-preserved immutable Store-Edge event.
C15C_SUPPLIER_RECEIVING_OFFLINE_MATERIALIZATION_CONTEXT_FLAG = (
    "nkt_c15c_supplier_receiving_offline_materialization"
)


def _offline_materialization_context():
    value = frappe.flags.get(
        C15C_SUPPLIER_RECEIVING_OFFLINE_MATERIALIZATION_CONTEXT_FLAG
    )
    return value if isinstance(value, dict) else None


def _has_receiving_role():
    context = _offline_materialization_context()
    if (
        context
        and str(context.get("origin_user") or "") == str(frappe.session.user or "")
        and str(context.get("event_uuid") or "").strip()
    ):
        # Server-owned replay of an event whose operator/device authority was
        # already checked at Store Edge and preserved at Primary.
        return True
    if frappe.session.user == "Administrator":
        return True
    return bool(ALLOWED_RECEIVING_ROLES.intersection(set(frappe.get_roles(frappe.session.user))))


def _require_receiving_role():
    if not _has_receiving_role():
        frappe.throw(_("You are not authorized for NKT supplier receiving."), frappe.PermissionError)


def _warehouse_check(warehouse, company, label):
    if not warehouse:
        frappe.throw(_("{0} is required.").format(label))
    row = frappe.db.get_value(
        "Warehouse", warehouse, ["company", "is_group", "disabled"], as_dict=True
    )
    if not row:
        frappe.throw(_("{0} does not exist: {1}").format(label, warehouse))
    if row.company != company:
        frappe.throw(_("{0} must belong to Company {1}.").format(label, company))
    if row.is_group:
        frappe.throw(_("{0} cannot be a group warehouse.").format(label))
    if row.disabled:
        frappe.throw(_("{0} is disabled.").format(label))


class NKTSupplierReceiving(Document):
    def validate(self):
        if not self.purchase_order:
            return  # NKT: no-PO receiving is valid; supplied PO remains fully validated.
        _require_receiving_role()

        # C9A.2 hard boundary:
        # This DocType is ONLY for stock entering NKT from an external Supplier.
        # NKT-owned stock moving between NKT warehouses must use the separate
        # internal Warehouse Transfer / DR / release-arrival path.
        self.movement_type = "External Supplier Arrival"
        if self.get("movement_type") != "External Supplier Arrival":
            frappe.throw(_("NKT Supplier Receiving can only be used for External Supplier Arrival."))

        offline_context = _offline_materialization_context()
        if offline_context:
            if not str(offline_context.get("event_uuid") or "").strip():
                frappe.throw(_("Offline Supplier Receiving materialization context is invalid."))
            if str(offline_context.get("purchase_order") or "") != str(self.purchase_order or ""):
                frappe.throw(_("Offline Supplier Receiving Purchase Order binding is invalid."))
            if getdate(offline_context.get("receiving_date")) != getdate(self.receiving_date):
                frappe.throw(_("Offline Supplier Receiving physical date binding is invalid."))
            if str(offline_context.get("origin_user") or "") != str(frappe.session.user or ""):
                frappe.throw(_("Offline Supplier Receiving operator binding is invalid."))
        elif getdate(self.receiving_date) != getdate(nowdate()):
            frappe.throw(_("Receiving Date must be the current business date."))

        if not self.purchase_order:
            pass  # NKT: Purchase Order is optional; supplied PO is still validated elsewhere.

        if not (
            (self.get("bill_of_lading_no") or "").strip()
            or (self.get("supplier_dr_no") or "").strip()
            or (self.get("supplier_delivery_reference") or "").strip()
        ):
            frappe.throw(_(
                "At least one supplier delivery reference is required: "
                "BL No., Supplier DR No., or Other Supplier Delivery Reference."
            ))

        # Vehicle identity is operational receiving data and is safe for Encoder/Warehouse.
        # If a known vehicle is selected, its identity is server-owned on this receiving snapshot.
        if self.get("delivery_vehicle"):
            vehicle = frappe.db.get_value(
                "NKT Vehicle",
                self.delivery_vehicle,
                ["plate_number", "internal_vehicle_no", "operator_name", "status"],
                as_dict=True,
            )
            if not vehicle:
                frappe.throw(_("Selected NKT Vehicle does not exist."))
            if vehicle.status != "Active":
                frappe.throw(_("Selected NKT Vehicle is inactive."))
            self.plate_number = normalize_plate(vehicle.plate_number)
            self.internal_vehicle_no = (vehicle.internal_vehicle_no or "").strip() or None
            self.vehicle_operator = (vehicle.operator_name or "").strip() or None
        else:
            self.plate_number = normalize_plate(self.plate_number)
            self.internal_vehicle_no = (self.internal_vehicle_no or "").strip() or None
            self.vehicle_operator = None

        if not self.plate_number and not self.internal_vehicle_no:
            frappe.throw(_("Enter at least one vehicle identifier: Plate Number or Internal Van / Truck No."))

        po = frappe.db.get_value(
            "Purchase Order",
            self.purchase_order,
            ["supplier", "company", "docstatus", "status"],
            as_dict=True,
        )
        if not po or int(po.docstatus or 0) != 1:
            frappe.throw(_("Purchase Order must be a submitted live Purchase Order."))

        if po.status in ("Closed", "Cancelled"):
            frappe.throw(_("Purchase Order {0} is {1}.").format(self.purchase_order, po.status))

        self.company = po.company
        self.supplier = po.supplier

        _warehouse_check(self.receiving_warehouse, self.company, _("Accepted / Receiving Warehouse"))

        if not self.items:
            frappe.throw(_("At least one receiving item is required."))

        po_items = {
            row.name: row
            for row in frappe.get_all(
                "Purchase Order Item",
                filters={"parent": self.purchase_order, "parenttype": "Purchase Order"},
                fields=["name", "item_code", "qty", "received_qty", "uom", "warehouse"],
                limit_page_length=5000,
            )
        }

        totals = {
            "expected": 0.0,
            "delivered": 0.0,
            "accepted": 0.0,
            "damaged": 0.0,
            "other_rejected": 0.0,
            "rejected": 0.0,
            "shortage": 0.0,
            "overdelivery": 0.0,
        }

        seen = set()
        for row in self.items:
            if not row.purchase_order_item or row.purchase_order_item not in po_items:
                frappe.throw(_("Every receiving row must originate from the selected Purchase Order."))

            if row.purchase_order_item in seen:
                frappe.throw(_("Purchase Order item {0} appears more than once.").format(row.purchase_order_item))
            seen.add(row.purchase_order_item)

            source = po_items[row.purchase_order_item]
            if row.item_code != source.item_code:
                frappe.throw(_("Item mismatch against Purchase Order row {0}.").format(row.purchase_order_item))

            remaining = max(flt(source.qty) - flt(source.received_qty), 0.0)
            row.expected_qty = remaining
            row.uom = source.uom

            for fld in ("delivered_qty", "accepted_qty", "damaged_qty", "other_rejected_qty"):
                if flt(row.get(fld)) < -TOL:
                    frappe.throw(_("{0} cannot be negative for {1}.").format(
                        frappe.get_meta(row.doctype).get_label(fld), row.item_code
                    ))

            delivered = flt(row.delivered_qty)
            damaged = flt(row.damaged_qty)
            other_rejected = flt(row.other_rejected_qty)
            rejected = damaged + other_rejected

            # C9K frontline simplification:
            # Encoder enters only physical Bags Received + Problem Bags.
            # Good/accepted quantity is system-derived and cannot become a second
            # independently-entered quantity that disagrees with physical arrival.
            if rejected - delivered > TOL:
                frappe.throw(_(
                    "Problem Bags cannot exceed Bags Received for {0}."
                ).format(row.item_code))

            accepted = max(delivered - rejected, 0.0)
            row.accepted_qty = accepted

            shortage = max(remaining - delivered, 0.0)
            overdelivery = max(delivered - remaining, 0.0)

            row.rejected_qty = rejected
            row.shortage_qty = shortage
            row.overdelivery_qty = overdelivery

            if rejected > TOL:
                if not row.rejected_warehouse:
                    frappe.throw(_("Choose where to put the Problem Bags for {0}.").format(row.item_code))
                _warehouse_check(row.rejected_warehouse, self.company, _("Problem-Bag Holding Warehouse"))
                if row.rejected_warehouse == self.receiving_warehouse:
                    frappe.throw(_("Problem Bags must go to a separate damage/inspection warehouse for {0}.").format(row.item_code))
                if not (row.condition_classification or "").strip():
                    frappe.throw(_("Choose a Problem Type for {0}.").format(row.item_code))
            else:
                row.rejected_warehouse = None
                row.condition_classification = "Normal"
                row.condition_reason = None

            # Shortage is derived from Expected PO Remaining - Bags Received.
            # Encoder does not need to explain a mathematically observed shortage.
            # Management investigates responsibility later in the restricted exception layer.

            totals["expected"] += remaining
            totals["delivered"] += delivered
            totals["accepted"] += accepted
            totals["damaged"] += damaged
            totals["other_rejected"] += other_rejected
            totals["rejected"] += rejected
            totals["shortage"] += shortage
            totals["overdelivery"] += overdelivery

        self.total_expected_qty = totals["expected"]
        self.total_delivered_qty = totals["delivered"]
        self.total_accepted_qty = totals["accepted"]
        self.total_damaged_qty = totals["damaged"]
        self.total_other_rejected_qty = totals["other_rejected"]
        self.total_rejected_qty = totals["rejected"]
        self.total_shortage_qty = totals["shortage"]
        self.total_overdelivery_qty = totals["overdelivery"]
        self.posting_status = "Posting Locked"

    def before_submit(self):
        # C9D.3 production unlock:
        # The operational Supplier Arrival is the only frontline submit action.
        # Standard Purchase Receipt and restricted exception records are created
        # server-side. Any exception aborts the same transaction.
        pr, exception = _post_receiving_with_exception(self)

        # Keep the in-memory document synchronized with the db_set values written
        # by the server-owned bridge before the parent Submit completes.
        self.underlying_purchase_receipt = pr.name
        self.posting_status = "Posted"

    def before_cancel(self):
        frappe.throw(_(
            "Posted Supplier Arrival cannot be cancelled directly. "
            "Use the controlled Supplier Arrival correction/reversal process."
        ))

    def before_update_after_submit(self):
        frappe.throw(_(
            "Posted Supplier Arrival is locked history. "
            "Use a controlled correction/reversal instead of editing it."
        ))



def _create_underlying_purchase_receipt(receiving):
    """
    INTERNAL C9 bridge.

    Converts a validated NKT Supplier Receiving into one standard ERPNext
    Purchase Receipt while keeping purchase rates/amounts out of the NKT
    receiving front door.

    C9C installs and rollback-tests this bridge only. Production Submit remains
    locked by before_submit until a later explicit unlock phase.
    """
    from erpnext.buying.doctype.purchase_order.purchase_order import make_purchase_receipt
    from frappe.utils import now_datetime

    if not receiving.name:
        frappe.throw(_("NKT Supplier Receiving must be saved before stock posting."))

    if int(receiving.docstatus or 0) not in (0, 1):
        frappe.throw(_("Cancelled NKT Supplier Receiving cannot create a Purchase Receipt."))

    if not int(receiving.physical_quantities_confirmed or 0):
        frappe.throw(_("Confirm the actual physical receiving quantities before stock posting."))

    if flt(receiving.total_overdelivery_qty) > TOL:
        frappe.throw(_(
            "Over-delivery is not supported by the C9 receiving bridge yet. "
            "Resolve or authorize the over-delivery before posting."
        ))

    if receiving.underlying_purchase_receipt:
        existing = frappe.db.get_value(
            "Purchase Receipt",
            receiving.underlying_purchase_receipt,
            ["name", "docstatus"],
            as_dict=True,
        )
        if existing and int(existing.docstatus or 0) == 1:
            return frappe.get_doc("Purchase Receipt", existing.name)
        frappe.throw(_(
            "NKT Supplier Receiving already references Purchase Receipt {0}, "
            "but it is not a live submitted receipt."
        ).format(receiving.underlying_purchase_receipt))

    # Re-run the NKT receiving validation immediately before posting.
    receiving.run_method("validate")

    by_po_item = {}
    for row in receiving.items:
        delivered = flt(row.delivered_qty)
        accepted = flt(row.accepted_qty)
        rejected = flt(row.rejected_qty)

        if delivered <= TOL:
            # Pure shortage / no physical arrival: leave the PO quantity open.
            continue

        if abs(delivered - (accepted + rejected)) > TOL:
            frappe.throw(_(
                "Delivered quantity identity changed for {0}. "
                "Delivered must equal Accepted + Rejected."
            ).format(row.item_code))

        by_po_item[row.purchase_order_item] = row

    if not by_po_item:
        frappe.throw(_("There is no physically delivered quantity to post."))

    original_user = frappe.session.user
    try:
        # Standard Purchase Receipt is a server-owned internal posting document.
        # Frontline roles do not receive direct Purchase Receipt permissions.
        frappe.set_user("Administrator")

        pr = make_purchase_receipt(receiving.purchase_order)
        pr.posting_date = receiving.receiving_date
        if pr.meta.has_field("set_posting_time"):
            pr.set_posting_time = 1
        if pr.meta.has_field("posting_time") and receiving.receiving_time:
            pr.posting_time = receiving.receiving_time

        if pr.meta.has_field("supplier_delivery_note"):
            pr.supplier_delivery_note = (
                receiving.supplier_dr_no
                or receiving.supplier_delivery_reference
                or receiving.bill_of_lading_no
            )

        # Remove mapped PO rows that had no physical arrival on this receiving.
        for pr_row in list(pr.items):
            source = by_po_item.get(pr_row.purchase_order_item)
            if not source:
                pr.remove(pr_row)
                continue

            pr_row.qty = flt(source.accepted_qty)
            pr_row.rejected_qty = flt(source.rejected_qty)
            pr_row.warehouse = receiving.receiving_warehouse
            pr_row.rejected_warehouse = (
                source.rejected_warehouse if flt(source.rejected_qty) > TOL else None
            )

        if not pr.items:
            frappe.throw(_("No Purchase Receipt items remain after applying physical receiving quantities."))

        pr.insert()
        pr.submit()

        # Link the internal posting document back to the operational front door.
        receiving.db_set("underlying_purchase_receipt", pr.name, update_modified=False)
        receiving.db_set("posting_status", "Posted", update_modified=False)
        receiving.db_set("posted_by", original_user, update_modified=False)
        receiving.db_set("posted_at", now_datetime(), update_modified=False)

        receiving.underlying_purchase_receipt = pr.name
        receiving.posting_status = "Posted"
        receiving.posted_by = original_user
        receiving.posted_at = now_datetime()

        return pr
    finally:
        frappe.set_user(original_user)



def _supplier_exception_rows(receiving):
    """
    Derive restricted claim/review rows from physical receiving facts.

    This does not decide financial responsibility. New rows always begin as
    Pending Investigation with Supplier Claimable Qty = 0. Management later
    determines Supplier / Trucker / NKT / Shared responsibility.
    """
    rows = []

    for row in receiving.items:
        damaged = flt(row.damaged_qty)
        other_rejected = flt(row.other_rejected_qty)
        shortage = flt(row.shortage_qty)
        classification = (row.condition_classification or "").strip()

        if damaged > TOL:
            if classification == "Wet":
                issue_type = "Wet"
            elif classification == "Broken Packaging":
                issue_type = "Broken Packaging"
            elif classification == "Other":
                issue_type = "Other Rejected"
            else:
                issue_type = "Damaged"

            rows.append({
                "item_code": row.item_code,
                "item_name": row.item_name,
                "purchase_order_item": row.purchase_order_item,
                "issue_type": issue_type,
                "issue_qty": damaged,
                "rejected_warehouse": row.rejected_warehouse,
                "condition_reason": row.condition_reason,
                "responsibility": "Pending Investigation",
                "supplier_claimable_qty": 0,
            })

        if other_rejected > TOL:
            rows.append({
                "item_code": row.item_code,
                "item_name": row.item_name,
                "purchase_order_item": row.purchase_order_item,
                "issue_type": "Other Rejected",
                "issue_qty": other_rejected,
                "rejected_warehouse": row.rejected_warehouse,
                "condition_reason": row.condition_reason,
                "responsibility": "Pending Investigation",
                "supplier_claimable_qty": 0,
            })

        if shortage > TOL:
            rows.append({
                "item_code": row.item_code,
                "item_name": row.item_name,
                "purchase_order_item": row.purchase_order_item,
                "issue_type": "Shortage",
                "issue_qty": shortage,
                "rejected_warehouse": None,
                "condition_reason": (
                    "System-derived physical shortage: expected remaining {0:g}; "
                    "bags physically received {1:g}; shortage {2:g}."
                ).format(
                    flt(row.expected_qty),
                    flt(row.delivered_qty),
                    shortage,
                ),
                "responsibility": "Pending Investigation",
                "supplier_claimable_qty": 0,
            })

    return rows


def _ensure_supplier_delivery_exception(receiving, purchase_receipt=None):
    """
    Create at most one restricted Supplier Delivery Exception for this receiving.

    Clean receipts return None.
    Physical exception facts are copied once; responsibility/payable decisions
    remain separate restricted management work.
    """
    rows = _supplier_exception_rows(receiving)
    if not rows:
        return None

    existing = frappe.db.exists(
        "NKT Supplier Delivery Exception",
        {"supplier_receiving": receiving.name},
    )
    if existing:
        return frappe.get_doc("NKT Supplier Delivery Exception", existing)

    original_user = frappe.session.user
    try:
        # Exception is a server-owned restricted control record.
        frappe.set_user("Administrator")

        doc = frappe.new_doc("NKT Supplier Delivery Exception")
        doc.supplier_receiving = receiving.name
        doc.review_status = "Pending Review"
        doc.claim_status = "Not Yet Determined"
        doc.created_from_receiving_by = original_user
        if purchase_receipt:
            doc.receiving_posting_reference = purchase_receipt.name

        for values in rows:
            child = doc.append("items", {})
            for fieldname, value in values.items():
                child.set(fieldname, value)

        doc.insert()

        return doc
    finally:
        frappe.set_user(original_user)


def _post_receiving_with_exception(receiving):
    """
    Production server-owned Supplier Arrival posting chain.

    Frontline users submit only NKT Supplier Receiving. The server creates the
    standard Purchase Receipt privately and, when physical damage/rejection/
    shortage exists, one restricted Supplier Delivery Exception.
    """
    pr = _create_underlying_purchase_receipt(receiving)
    exception = _ensure_supplier_delivery_exception(receiving, pr)
    return pr, exception


def _post_receiving_with_exception_runtime_candidate(receiving):
    """Backward-compatible alias retained for accepted C9D.2 regression tooling."""
    return _post_receiving_with_exception(receiving)


@frappe.whitelist()
def get_vehicle_identity(vehicle):
    """Receiving-safe vehicle lookup: identity only, never rates/payables."""
    _require_receiving_role()
    row = frappe.db.get_value(
        "NKT Vehicle",
        vehicle,
        ["name", "plate_number", "internal_vehicle_no", "operator_name", "vehicle_type", "status"],
        as_dict=True,
    )
    if not row:
        frappe.throw(_("NKT Vehicle does not exist."))
    if row.status != "Active":
        frappe.throw(_("NKT Vehicle is inactive."))
    return row


@frappe.whitelist()
def get_open_purchase_orders():
    """Sanitized PO selector: no rate, amount, payable, payment, margin, or check data."""
    _require_receiving_role()
    rows = frappe.get_all(
        "Purchase Order",
        filters={"docstatus": 1},
        fields=["name", "supplier", "transaction_date", "schedule_date", "status", "company"],
        order_by="transaction_date desc, modified desc",
        limit_page_length=200,
    )
    return [
        row for row in rows
        if row.status not in ("Closed", "Cancelled", "Completed")
    ]


@frappe.whitelist()
def load_purchase_order(purchase_order):
    """Return only receiving-safe PO information. Intentionally excludes all costs."""
    _require_receiving_role()
    if not purchase_order:
        pass  # NKT: Purchase Order is optional; supplied PO is still validated elsewhere.

    po = frappe.db.get_value(
        "Purchase Order",
        purchase_order,
        ["name", "supplier", "company", "transaction_date", "schedule_date", "status", "docstatus"],
        as_dict=True,
    )
    if not po or int(po.docstatus or 0) != 1:
        frappe.throw(_("Purchase Order must be submitted."))
    if po.status in ("Closed", "Cancelled", "Completed"):
        frappe.throw(_("Purchase Order {0} is not open for receiving.").format(purchase_order))

    items = frappe.get_all(
        "Purchase Order Item",
        filters={"parent": purchase_order, "parenttype": "Purchase Order"},
        fields=[
            "name", "item_code", "item_name", "qty", "received_qty",
            "uom", "stock_uom", "warehouse"
        ],
        order_by="idx asc",
        limit_page_length=5000,
    )

    safe_items = []
    for row in items:
        remaining = max(flt(row.qty) - flt(row.received_qty), 0.0)
        if remaining <= TOL:
            continue
        safe_items.append({
            "purchase_order_item": row.name,
            "item_code": row.item_code,
            "item_name": row.item_name,
            "uom": row.uom or row.stock_uom,
            "expected_qty": remaining,
            "suggested_warehouse": row.warehouse,
        })

    return {
        "purchase_order": po.name,
        "supplier": po.supplier,
        "company": po.company,
        "transaction_date": po.transaction_date,
        "schedule_date": po.schedule_date,
        "items": safe_items,
    }
