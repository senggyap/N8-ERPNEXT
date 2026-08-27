# Copyright (c) 2026, NKT Grains Trading
# C10E Production Destination Arrival Unlock
from __future__ import annotations

import json
import math

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.model.naming import make_autoname
from frappe.utils import cint, flt, getdate, get_datetime, now_datetime, today

DRAFT_STATUS = "Draft"
RELEASED_STATUS = "In Transit"
COMPLETED_STATUS = "Completed"
TOL = 0.0000001
SYSTEM_PARENT_FIELDS = (
    "released_by",
    "released_at",
    "arrived_by",
    "arrived_at",
    "outgoing_stock_entry",
    "incoming_stock_entry",
    "discrepancy_summary",
)
SYSTEM_ITEM_QTY_FIELDS = ("released_qty", "arrived_qty", "damaged_qty", "short_qty")


class NKTWarehouseTransfer(Document):
    """NKT business/audit wrapper for internal stock movement.

    C10E retains the accepted physical source Release and unlocks the destination
    Warehouse Receive / Arrive action. Standard ERPNext Stock Entries remain
    server-owned and hidden behind the NKT wrapper.
    """

    def autoname(self):
        self.name = make_autoname("NKT-WHT-.#####")
        self.internal_transfer_no = self.name

    def before_insert(self):
        self._validate_creator_role()
        self.requested_by = frappe.session.user
        self.requested_at = now_datetime()
        self.status = DRAFT_STATUS

    def validate(self):
        self._enforce_c10e_state()
        self._validate_transfer_date()
        self._validate_warehouses()
        self._validate_internal_dr_no()
        self._validate_items()
        self._validate_server_owned_audit_fields()

    def _validate_creator_role(self):
        if frappe.session.user == "Administrator":
            return
        if "NKT Encoder" not in frappe.get_roles():
            frappe.throw(
                _("Only NKT Encoder may create an Internal Warehouse Transfer instruction."),
                frappe.PermissionError,
            )

    def _enforce_c10e_state(self):
        if not self.internal_transfer_no:
            self.internal_transfer_no = self.name
        if self.internal_transfer_no != self.name:
            frappe.throw(_("Internal Transfer No. is system-owned and must match the document number."))

        before = self.get_doc_before_save()
        if not before:
            if (self.status or DRAFT_STATUS) != DRAFT_STATUS:
                frappe.throw(_("A new Internal Warehouse Transfer must start in Draft status."))
            self.status = DRAFT_STATUS
            for fieldname in SYSTEM_PARENT_FIELDS:
                if self.get(fieldname):
                    frappe.throw(
                        _("{0} is server-owned and cannot be set on a new transfer.").format(
                            self.meta.get_label(fieldname) or fieldname
                        )
                    )
            for row in self.items or []:
                for fieldname in SYSTEM_ITEM_QTY_FIELDS:
                    if abs(flt(row.get(fieldname))) > TOL:
                        frappe.throw(
                            _("Row {0}: {1} is server-owned and must start at zero.").format(
                                row.idx, row.meta.get_label(fieldname) or fieldname
                            )
                        )
            return

        # Once physical source release has happened, posted transfer history is immutable
        # through ordinary document editing. C10E and later phases use controlled server actions.
        if before.status != DRAFT_STATUS:
            frappe.throw(
                _("This Internal Warehouse Transfer has already left Draft status. Posted release/arrival history cannot be edited directly.")
            )

        # A Draft can be edited by the Encoder, but nobody may manually simulate Release/Arrival
        # by setting the system-owned status/audit/quantity fields.
        if (self.status or DRAFT_STATUS) != DRAFT_STATUS:
            frappe.throw(_("Transfer status can change only through a controlled warehouse action."))
        self.status = DRAFT_STATUS

        for fieldname in SYSTEM_PARENT_FIELDS:
            if self.get(fieldname):
                frappe.throw(
                    _("{0} is server-owned and cannot be edited directly.").format(
                        self.meta.get_label(fieldname) or fieldname
                    )
                )

        for row in self.items or []:
            for fieldname in SYSTEM_ITEM_QTY_FIELDS:
                if abs(flt(row.get(fieldname))) > TOL:
                    frappe.throw(
                        _("Row {0}: {1} is server-owned and can change only through a controlled warehouse action.").format(
                            row.idx, row.meta.get_label(fieldname) or fieldname
                        )
                    )

    def _validate_transfer_date(self):
        if not self.transfer_date:
            frappe.throw(_("Transfer / Business Date is required."))

        transfer_date = getdate(self.transfer_date)
        current_date = getdate(today())
        if transfer_date > current_date:
            frappe.throw(_("Transfer / Business Date cannot be in the future."))

        before = self.get_doc_before_save()
        if self.flags.in_insert:
            if transfer_date != current_date:
                frappe.throw(_("Frontline Internal Warehouse Transfer instructions must use the current live business date."))
        elif before and getdate(before.transfer_date) != transfer_date:
            frappe.throw(_("Transfer / Business Date is historical audit identity and cannot be changed after creation."))

    def _validate_warehouses(self):
        if not self.company:
            frappe.throw(_("Company is required."))
        if not self.source_warehouse or not self.destination_warehouse:
            frappe.throw(_("Source Warehouse and Destination Warehouse are required."))
        if self.source_warehouse == self.destination_warehouse:
            frappe.throw(_("Source Warehouse and Destination Warehouse must be different."))

        for label, warehouse in (
            (_("Source Warehouse"), self.source_warehouse),
            (_("Destination Warehouse"), self.destination_warehouse),
        ):
            values = frappe.db.get_value(
                "Warehouse", warehouse, ["company", "is_group", "disabled"], as_dict=True
            )
            if not values:
                frappe.throw(_("{0} {1} does not exist.").format(label, frappe.bold(warehouse)))
            if values.company != self.company:
                frappe.throw(
                    _("{0} must belong to Company {1}.").format(label, frappe.bold(self.company))
                )
            if cint(values.is_group):
                frappe.throw(_("{0} must be a leaf warehouse, not a group warehouse.").format(label))
            if cint(values.disabled):
                frappe.throw(_("{0} is disabled and cannot be used.").format(label))

    def _validate_internal_dr_no(self):
        self.internal_dr_no = (self.internal_dr_no or "").strip() or None
        if not self.internal_dr_no:
            return
        filters = {"company": self.company, "internal_dr_no": self.internal_dr_no}
        existing = frappe.db.get_value("NKT Warehouse Transfer", filters, "name")
        if existing and existing != self.name:
            frappe.throw(
                _("Internal DR No. / Transfer DR {0} is already used by {1}.").format(
                    frappe.bold(self.internal_dr_no), frappe.bold(existing)
                )
            )

    def _validate_items(self):
        if not self.items:
            frappe.throw(_("At least one transfer item is required."))

        seen = set()
        for row in self.items:
            if not row.item_code:
                frappe.throw(_("Row {0}: Item is required.").format(row.idx))
            if row.item_code in seen:
                frappe.throw(
                    _("Row {0}: Item {1} is duplicated. Use one row per item for deterministic transfer lineage.").format(
                        row.idx, frappe.bold(row.item_code)
                    )
                )
            seen.add(row.item_code)

            item = frappe.db.get_value(
                "Item",
                row.item_code,
                ["item_name", "stock_uom", "is_stock_item", "disabled"],
                as_dict=True,
            )
            if not item:
                frappe.throw(_("Row {0}: Item {1} does not exist.").format(row.idx, frappe.bold(row.item_code)))
            if cint(item.disabled):
                frappe.throw(_("Row {0}: Item {1} is disabled.").format(row.idx, frappe.bold(row.item_code)))
            if not cint(item.is_stock_item):
                frappe.throw(_("Row {0}: Item {1} is not a stock item.").format(row.idx, frappe.bold(row.item_code)))
            if not item.stock_uom:
                frappe.throw(_("Row {0}: Item {1} has no Stock UOM.").format(row.idx, frappe.bold(row.item_code)))

            row.item_name = item.item_name
            row.uom = item.stock_uom
            qty = flt(row.requested_qty)
            if not math.isfinite(qty) or qty <= 0:
                frappe.throw(_("Row {0}: Requested Qty must be greater than zero.").format(row.idx))

            must_whole = cint(frappe.db.get_value("UOM", item.stock_uom, "must_be_whole_number") or 0)
            if must_whole and abs(qty - round(qty)) > TOL:
                frappe.throw(
                    _("Row {0}: Requested Qty must be a whole number for UOM {1}.").format(
                        row.idx, frappe.bold(item.stock_uom)
                    )
                )

    def _validate_server_owned_audit_fields(self):
        before = self.get_doc_before_save()
        if not before:
            return

        immutable = ("internal_transfer_no", "requested_by", "requested_at")
        for fieldname in immutable:
            if self.get(fieldname) != before.get(fieldname):
                frappe.throw(
                    _("{0} is audit-owned and cannot be changed.").format(
                        self.meta.get_label(fieldname) or fieldname
                    )
                )


def _require_source_release_role():
    user = frappe.session.user
    if not user or user == "Guest" or "NKT Warehouse" not in frappe.get_roles(user):
        frappe.throw(
            _("Only an authorized NKT Warehouse user may record the physical source Release."),
            frappe.PermissionError,
        )
    return user


C15C_TRANSFER_DISPATCH_JOURNAL = "NKT Primary Warehouse Transfer Dispatch Intent"
C15C_TRANSFER_DISPATCH_CONTEXT_FLAG = "nkt_c15c_transfer_dispatch_materialization_context"


def _pending_preserved_dispatch_event(transfer_name):
    return frappe.db.get_value(
        C15C_TRANSFER_DISPATCH_JOURNAL,
        {
            "warehouse_transfer": transfer_name,
            "preservation_state": "Preserved",
            "downstream_state": "Awaiting Source Dispatch Materialization",
        },
        "name",
    )


def _preserved_dispatch_context(doc):
    """Resolve a trusted Primary-only offline materialization context.

    The caller cannot inject operator/time fields.  The only process-local input is
    the preserved event UUID; every business value is re-read from the immutable
    Primary journal created by the safe-sync preservation adapter.
    """
    raw = frappe.flags.get(C15C_TRANSFER_DISPATCH_CONTEXT_FLAG)
    if not raw:
        return None
    if not isinstance(raw, dict):
        frappe.throw(_("Internal transfer materialization context is invalid."))
    if str(frappe.conf.get("nkt_runtime_role") or "Primary").strip() != "Primary":
        frappe.throw(
            _("Preserved transfer dispatch materialization is available only at Primary."),
            frappe.PermissionError,
        )

    event_uuid = str(raw.get("event_uuid") or "").strip()
    if not event_uuid or not frappe.db.exists(C15C_TRANSFER_DISPATCH_JOURNAL, event_uuid):
        frappe.throw(_("Preserved transfer dispatch journal is unavailable."))

    journal = frappe.get_doc(C15C_TRANSFER_DISPATCH_JOURNAL, event_uuid)
    if (
        str(journal.preservation_state or "") != "Preserved"
        or str(journal.downstream_state or "") != "Awaiting Source Dispatch Materialization"
    ):
        frappe.throw(_("Preserved transfer dispatch journal is not eligible for materialization."))

    if str(journal.warehouse_transfer or "") != str(doc.name or ""):
        frappe.throw(_("Preserved transfer dispatch points to another transfer."))
    exact = {
        "company": doc.company,
        "source_warehouse": doc.source_warehouse,
        "destination_warehouse": doc.destination_warehouse,
    }
    for fieldname, actual in exact.items():
        if str(journal.get(fieldname) or "") != str(actual or ""):
            frappe.throw(
                _("Preserved transfer dispatch {0} conflicts with the canonical transfer.").format(fieldname)
            )

    business_date = getdate(journal.business_date)
    transfer_date = getdate(journal.transfer_date)
    released_at = get_datetime(journal.settled_at)
    if business_date != transfer_date or getdate(released_at) != business_date:
        frappe.throw(_("Preserved transfer dispatch business time is internally inconsistent."))
    if getdate(doc.transfer_date) != transfer_date:
        frappe.throw(_("Preserved transfer dispatch date conflicts with the canonical transfer."))

    operator = str(journal.origin_user or "").strip()
    if not operator or operator == "Guest":
        frappe.throw(_("Preserved transfer dispatch has no valid warehouse operator."))

    return {
        "event_uuid": event_uuid,
        "operator": operator,
        "released_at": released_at,
        "business_date": business_date,
    }


def _resolve_transit_warehouse(company):
    exact = frappe.get_all(
        "Warehouse",
        filters={
            "company": company,
            "is_group": 0,
            "disabled": 0,
            "warehouse_name": "Goods In Transit",
        },
        pluck="name",
        limit_page_length=10,
    )
    if len(exact) == 1:
        return exact[0]
    if len(exact) > 1:
        frappe.throw(_("Multiple active 'Goods In Transit' warehouses exist for Company {0}; resolve configuration before Release.").format(frappe.bold(company)))

    rows = frappe.get_all(
        "Warehouse",
        filters={"company": company, "is_group": 0, "disabled": 0},
        fields=["name", "warehouse_name"],
        limit_page_length=500,
    )
    candidates = [
        row.name
        for row in rows
        if "transit" in (row.name or "").lower() or "transit" in (row.warehouse_name or "").lower()
    ]
    if len(candidates) == 1:
        return candidates[0]
    if not candidates:
        frappe.throw(_("No active Goods In Transit warehouse is configured for Company {0}.").format(frappe.bold(company)))
    frappe.throw(_("Multiple transit-like warehouses exist for Company {0}; configure one unambiguous Goods In Transit warehouse before Release.").format(frappe.bold(company)))


def _bin_qty(item_code, warehouse):
    return flt(frappe.db.get_value("Bin", {"item_code": item_code, "warehouse": warehouse}, "actual_qty") or 0)


def _lock_transfer_row(transfer_name):
    # Row-level lock prevents two near-simultaneous Release clicks from both creating an outgoing Stock Entry.
    rows = frappe.db.sql(
        "SELECT name FROM `tabNKT Warehouse Transfer` WHERE name=%s FOR UPDATE",
        (transfer_name,),
        as_dict=True,
    )
    if not rows:
        frappe.throw(_("Internal Warehouse Transfer {0} does not exist.").format(frappe.bold(transfer_name)))


def _validate_source_release(doc, transit_warehouse, *, preserved_context=None):
    if doc.status != DRAFT_STATUS:
        frappe.throw(_("Transfer {0} is {1} and cannot be released again.").format(frappe.bold(doc.name), frappe.bold(doc.status)))
    if doc.outgoing_stock_entry:
        frappe.throw(_("Transfer {0} already has an outgoing Stock Entry; duplicate Release is blocked.").format(frappe.bold(doc.name)))
    if doc.released_by or doc.released_at:
        frappe.throw(_("Transfer {0} already contains source-release audit data; duplicate Release is blocked.").format(frappe.bold(doc.name)))
    if preserved_context:
        expected_business_date = getdate(preserved_context["business_date"])
        if getdate(doc.transfer_date) != expected_business_date:
            frappe.throw(
                _("Preserved offline physical Release date conflicts with the Internal Warehouse Transfer.")
            )
        pending_event = _pending_preserved_dispatch_event(doc.name)
        if pending_event and str(pending_event) != str(preserved_context["event_uuid"]):
            frappe.throw(
                _("Another preserved offline source-dispatch event is already bound to this transfer.")
            )
    else:
        pending_event = _pending_preserved_dispatch_event(doc.name)
        if pending_event:
            frappe.throw(
                _("This transfer already has a preserved offline physical Dispatch awaiting safe-sync materialization. Duplicate online Release is blocked.")
            )
        if getdate(doc.transfer_date) != getdate(today()):
            frappe.throw(_("Physical Release must use today's live business date. Create/correct a current-date transfer instruction instead of releasing a stale or backdated Draft."))

    doc._validate_warehouses()
    doc._validate_items()

    if transit_warehouse in {doc.source_warehouse, doc.destination_warehouse}:
        frappe.throw(_("The operational Source and Destination warehouses cannot be the Goods In Transit warehouse."))

    if not doc.items:
        frappe.throw(_("At least one transfer item is required for Release."))

    allow_negative = cint(frappe.db.get_single_value("Stock Settings", "allow_negative_stock") or 0)
    checks = []
    for row in doc.items:
        requested = flt(row.requested_qty)
        if not math.isfinite(requested) or requested <= 0:
            frappe.throw(_("Row {0}: Release quantity must be greater than zero.").format(row.idx))
        if any(abs(flt(row.get(fieldname))) > TOL for fieldname in SYSTEM_ITEM_QTY_FIELDS):
            frappe.throw(_("Row {0}: server-owned release/arrival/discrepancy quantities are not in a clean Draft state.").format(row.idx))

        tracking = frappe.db.get_value("Item", row.item_code, ["has_serial_no", "has_batch_no"], as_dict=True)
        if tracking and (cint(tracking.has_serial_no) or cint(tracking.has_batch_no)):
            frappe.throw(
                _("Row {0}: serialized/batched item {1} is not unlocked by the accepted C10D/C10E quantity-only transfer path yet.").format(
                    row.idx, frappe.bold(row.item_code)
                )
            )

        available = _bin_qty(row.item_code, doc.source_warehouse)
        if requested > available + TOL and not allow_negative:
            frappe.throw(
                _("Row {0}: Release quantity {1} exceeds available stock {2}; negative stock is not enabled.").format(
                    row.idx, requested, available
                )
            )
        checks.append({
            "row": row.idx,
            "item_code": row.item_code,
            "requested_qty": requested,
            "available_qty": available,
            "allow_negative_stock": bool(allow_negative),
        })
    return checks


def _verify_submitted_outgoing(stock_entry, transfer, transit_warehouse):
    if stock_entry.docstatus != 1:
        frappe.throw(_("ERPNext did not submit the outgoing transit Stock Entry."))
    if stock_entry.stock_entry_type != "Material Transfer" or stock_entry.purpose != "Material Transfer":
        frappe.throw(_("Outgoing Stock Entry is not a Material Transfer."))
    if cint(stock_entry.add_to_transit) != 1:
        frappe.throw(_("Outgoing Stock Entry was not marked Add to Transit."))
    if stock_entry.from_warehouse != transfer.source_warehouse or stock_entry.to_warehouse != transit_warehouse:
        frappe.throw(_("Outgoing Stock Entry warehouse lineage does not match the NKT transfer."))

    expected = {row.item_code: flt(row.requested_qty) for row in transfer.items}
    actual = {}
    for row in stock_entry.items:
        if row.item_code in expected:
            if row.s_warehouse != transfer.source_warehouse or row.t_warehouse != transit_warehouse:
                frappe.throw(_("Outgoing Stock Entry item warehouse lineage does not match the NKT transfer."))
            actual[row.item_code] = actual.get(row.item_code, 0.0) + flt(row.qty)
    if set(actual) != set(expected):
        frappe.throw(_("Outgoing Stock Entry item set does not exactly match the NKT transfer."))
    for item_code, qty in expected.items():
        if abs(actual.get(item_code, 0.0) - qty) > TOL:
            frappe.throw(_("Outgoing Stock Entry quantity does not exactly match the NKT transfer for item {0}.").format(frappe.bold(item_code)))


@frappe.whitelist()
def release_transfer(transfer_name: str):
    """Record the physical source Release and move stock into Goods In Transit.

    C10E retains the accepted C10D production Release. It does not create
    Supplier/AP records, assign discrepancy responsibility, or require Admin approval.
    """
    transfer_name = (transfer_name or "").strip()
    if not transfer_name:
        frappe.throw(_("Internal Warehouse Transfer is required."))

    _lock_transfer_row(transfer_name)
    doc = frappe.get_doc("NKT Warehouse Transfer", transfer_name)
    preserved_context = _preserved_dispatch_context(doc)
    if preserved_context:
        operator = preserved_context["operator"]
    else:
        operator = _require_source_release_role()
        doc.check_permission("read")

    transit_warehouse = _resolve_transit_warehouse(doc.company)
    qty_checks = _validate_source_release(
        doc,
        transit_warehouse,
        preserved_context=preserved_context,
    )

    stock_entry_values = {
        "doctype": "Stock Entry",
        "stock_entry_type": "Material Transfer",
        "purpose": "Material Transfer",
        "company": doc.company,
        "from_warehouse": doc.source_warehouse,
        "to_warehouse": transit_warehouse,
        "add_to_transit": 1,
        "remarks": _("NKT Internal Warehouse Transfer {0}: source physical Release to transit; final destination {1}").format(
            doc.name, doc.destination_warehouse
        ),
        "items": [
            {
                "item_code": row.item_code,
                "s_warehouse": doc.source_warehouse,
                "t_warehouse": transit_warehouse,
                "qty": row.requested_qty,
                "uom": row.uom,
                "stock_uom": row.uom,
                "conversion_factor": 1,
            }
            for row in doc.items
        ],
    }
    if preserved_context:
        physical_time = get_datetime(preserved_context["released_at"])
        stock_entry_values.update(
            {
                "set_posting_time": 1,
                "posting_date": getdate(physical_time),
                "posting_time": physical_time.strftime("%H:%M:%S"),
            }
        )

    stock_entry = frappe.get_doc(stock_entry_values)

    # Warehouse users intentionally do not receive direct Stock Entry permissions.
    # The already-accepted C10A.2 engine path posts the standard document server-side.
    stock_entry.flags.ignore_permissions = True
    stock_entry.insert(ignore_permissions=True)
    stock_entry.flags.ignore_permissions = True
    stock_entry.submit()
    _verify_submitted_outgoing(stock_entry, doc, transit_warehouse)

    released_at = (
        get_datetime(preserved_context["released_at"])
        if preserved_context
        else now_datetime()
    )
    frappe.db.set_value(
        "NKT Warehouse Transfer",
        doc.name,
        {
            "status": RELEASED_STATUS,
            "released_by": operator,
            "released_at": released_at,
            "outgoing_stock_entry": stock_entry.name,
        },
        update_modified=True,
    )
    for row in doc.items:
        frappe.db.set_value(
            "NKT Warehouse Transfer Item",
            row.name,
            "released_qty",
            flt(row.requested_qty),
            update_modified=False,
        )

    return {
        "status": RELEASED_STATUS,
        "transfer": doc.name,
        "outgoing_stock_entry": stock_entry.name,
        "transit_warehouse": transit_warehouse,
        "released_by": operator,
        "released_at": released_at,
        "quantity_checks": qty_checks,
        "arrival_unlocked": True,
        "preserved_offline_dispatch_materialized": bool(preserved_context),
        "preserved_event_uuid": (
            preserved_context["event_uuid"] if preserved_context else None
        ),
    }


def _require_destination_arrival_role():
    user = frappe.session.user
    if not user or user == "Guest" or "NKT Warehouse" not in frappe.get_roles(user):
        frappe.throw(
            _("Only an authorized NKT Warehouse user may record the physical destination Arrival."),
            frappe.PermissionError,
        )
    return user


C15C_TRANSFER_ARRIVAL_JOURNAL = "NKT Primary Warehouse Transfer Arrival Intent"
C15C_TRANSFER_ARRIVAL_CONTEXT_FLAG = "nkt_c15c_transfer_arrival_materialization_context"


def _pending_preserved_arrival_event(transfer_name):
    return frappe.db.get_value(
        C15C_TRANSFER_ARRIVAL_JOURNAL,
        {
            "warehouse_transfer": transfer_name,
            "preservation_state": "Preserved",
            "downstream_state": "Awaiting Destination Arrival Materialization",
        },
        "name",
    )


def _preserved_arrival_context(doc):
    """Resolve a trusted Primary-only destination-Arrival materialization context.

    Only the preserved event UUID is supplied process-locally. Operator, physical
    Arrival time, quantities, transit identity, and snapshot order are re-read from
    the immutable Primary journal. No client-supplied privilege/time override exists.
    """
    raw = frappe.flags.get(C15C_TRANSFER_ARRIVAL_CONTEXT_FLAG)
    if not raw:
        return None
    if not isinstance(raw, dict):
        frappe.throw(_("Internal transfer Arrival materialization context is invalid."))
    if str(frappe.conf.get("nkt_runtime_role") or "Primary").strip() != "Primary":
        frappe.throw(
            _("Preserved transfer Arrival materialization is available only at Primary."),
            frappe.PermissionError,
        )

    event_uuid = str(raw.get("event_uuid") or "").strip()
    if not event_uuid or not frappe.db.exists(C15C_TRANSFER_ARRIVAL_JOURNAL, event_uuid):
        frappe.throw(_("Preserved transfer Arrival journal is unavailable."))

    journal = frappe.get_doc(C15C_TRANSFER_ARRIVAL_JOURNAL, event_uuid)
    if (
        str(journal.preservation_state or "") != "Preserved"
        or str(journal.downstream_state or "") != "Awaiting Destination Arrival Materialization"
    ):
        frappe.throw(_("Preserved transfer Arrival journal is not eligible for materialization."))

    exact = {
        "warehouse_transfer": doc.name,
        "company": doc.company,
        "source_warehouse": doc.source_warehouse,
        "destination_warehouse": doc.destination_warehouse,
        "outgoing_stock_entry": doc.outgoing_stock_entry,
    }
    for fieldname, actual in exact.items():
        if str(journal.get(fieldname) or "") != str(actual or ""):
            frappe.throw(
                _("Preserved transfer Arrival {0} conflicts with the canonical transfer.").format(fieldname)
            )

    expected_transit = _resolve_transit_warehouse(doc.company)
    if str(journal.transit_warehouse or "") != str(expected_transit or ""):
        frappe.throw(_("Preserved transfer Arrival Goods In Transit lineage is invalid."))

    business_date = getdate(journal.business_date)
    arrived_at = get_datetime(journal.settled_at)
    if getdate(arrived_at) != business_date:
        frappe.throw(_("Preserved transfer Arrival business time is internally inconsistent."))
    if getdate(doc.transfer_date) > business_date:
        frappe.throw(_("Preserved destination Arrival precedes the transfer business date."))

    operator = str(journal.origin_user or "").strip()
    if not operator or operator == "Guest":
        frappe.throw(_("Preserved transfer Arrival has no valid warehouse operator."))

    by_item = {row.item_code: row for row in (doc.items or [])}
    arrival_quantities = {}
    for item in journal.items or []:
        local = by_item.get(item.item_code)
        if not local or str(local.name) != str(item.warehouse_transfer_item or ""):
            frappe.throw(
                _("Preserved transfer Arrival Item lineage conflicts with the canonical transfer.")
            )
        # This is the materialization-order gate. Reservations may be ahead, but
        # canonical stock may not leapfrog the physical cumulative snapshot.
        if abs(flt(local.arrived_qty) - flt(item.cumulative_arrived_before)) > TOL:
            frappe.throw(
                _("Preserved transfer Arrival is waiting for an earlier physical Arrival to materialize first. Safe retry is required.")
            )
        if abs(
            (flt(local.released_qty) - flt(local.arrived_qty))
            - flt(item.remaining_before)
        ) > TOL:
            frappe.throw(
                _("Preserved transfer Arrival remaining-quantity snapshot no longer matches canonical transit.")
            )
        qty = flt(item.arrival_quantity)
        if qty <= TOL or qty > flt(item.remaining_before) + TOL:
            frappe.throw(_("Preserved transfer Arrival quantity is invalid."))
        arrival_quantities[item.item_code] = qty

    if not arrival_quantities:
        frappe.throw(_("Preserved transfer Arrival has no materializable Item quantities."))

    return {
        "event_uuid": event_uuid,
        "operator": operator,
        "arrived_at": arrived_at,
        "business_date": business_date,
        "arrival_quantities": arrival_quantities,
    }


def _parse_arrival_quantities(raw):
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except Exception:
            frappe.throw(_("Arrival quantities are invalid."))
    if not isinstance(raw, dict):
        frappe.throw(_("Arrival quantities must be supplied by item."))

    out = {}
    for item_code, value in raw.items():
        code = (str(item_code) if item_code is not None else "").strip()
        if not code:
            frappe.throw(_("Arrival quantities contain a blank Item."))
        qty = flt(value)
        if not math.isfinite(qty):
            frappe.throw(_("Arrival quantity for Item {0} is invalid.").format(frappe.bold(code)))
        out[code] = qty
    return out


def _validate_destination_arrival_header(doc, *, preserved_context=None):
    if doc.status != RELEASED_STATUS:
        frappe.throw(
            _("Transfer {0} is {1} and cannot accept another Arrival.").format(
                frappe.bold(doc.name), frappe.bold(doc.status)
            )
        )
    if not doc.outgoing_stock_entry:
        frappe.throw(_("Arrival is blocked because this transfer has no submitted outgoing Stock Entry."))
    if doc.incoming_stock_entry:
        frappe.throw(_("Transfer already contains a final incoming Stock Entry; duplicate Arrival is blocked."))
    if preserved_context:
        if getdate(doc.transfer_date) > getdate(preserved_context["business_date"]):
            frappe.throw(_("Transfer / Business Date cannot be after the preserved physical Arrival date."))
    else:
        pending_event = _pending_preserved_arrival_event(doc.name)
        if pending_event:
            frappe.throw(
                _("This transfer already has a preserved offline destination Arrival awaiting safe-sync materialization. Duplicate online Arrival is blocked.")
            )
        if getdate(doc.transfer_date) > getdate(today()):
            frappe.throw(_("Transfer / Business Date cannot be in the future."))

    doc._validate_warehouses()

    outgoing = frappe.get_doc("Stock Entry", doc.outgoing_stock_entry)
    if outgoing.docstatus != 1:
        frappe.throw(_("Outgoing Stock Entry must be submitted before destination Arrival."))
    if outgoing.company != doc.company:
        frappe.throw(_("Outgoing Stock Entry Company does not match the NKT transfer."))
    if outgoing.stock_entry_type != "Material Transfer" or outgoing.purpose != "Material Transfer":
        frappe.throw(_("Outgoing Stock Entry is not the accepted Material Transfer engine."))
    if cint(outgoing.add_to_transit) != 1:
        frappe.throw(_("Outgoing Stock Entry is not an Add to Transit Release."))
    if outgoing.from_warehouse != doc.source_warehouse:
        frappe.throw(_("Outgoing Stock Entry source does not match the NKT transfer."))

    transit_warehouses = {row.t_warehouse for row in outgoing.items if row.t_warehouse}
    if len(transit_warehouses) != 1:
        frappe.throw(_("Outgoing Stock Entry does not resolve to exactly one Goods In Transit warehouse."))
    transit_warehouse = next(iter(transit_warehouses))
    expected_transit = _resolve_transit_warehouse(doc.company)
    if transit_warehouse != expected_transit:
        frappe.throw(_("Outgoing Stock Entry does not use the configured Goods In Transit warehouse."))
    if outgoing.to_warehouse and outgoing.to_warehouse != transit_warehouse:
        frappe.throw(_("Outgoing Stock Entry header transit warehouse does not match its item lineage."))
    if transit_warehouse in {doc.source_warehouse, doc.destination_warehouse}:
        frappe.throw(_("Operational Source/Destination cannot be the Goods In Transit warehouse."))

    if len(outgoing.items) != len(doc.items):
        frappe.throw(_("Wrapper item lineage does not match the outgoing Stock Entry."))
    outgoing_by_item = {}
    for row in outgoing.items:
        if row.item_code in outgoing_by_item:
            frappe.throw(_("Outgoing Stock Entry contains duplicate item lineage."))
        outgoing_by_item[row.item_code] = row

    for row in doc.items:
        released = flt(row.released_qty)
        arrived = flt(row.arrived_qty)
        if released <= TOL:
            frappe.throw(_("Row {0}: released quantity is invalid for Arrival.").format(row.idx))
        if arrived < -TOL or arrived > released + TOL:
            frappe.throw(_("Row {0}: cumulative arrived quantity is outside the released quantity.").format(row.idx))
        out = outgoing_by_item.get(row.item_code)
        if not out:
            frappe.throw(_("Row {0}: outgoing Stock Entry is missing Item {1}.").format(row.idx, frappe.bold(row.item_code)))
        if out.s_warehouse != doc.source_warehouse or out.t_warehouse != transit_warehouse:
            frappe.throw(_("Row {0}: outgoing Stock Entry warehouse lineage mismatch.").format(row.idx))
        if abs(flt(out.qty) - released) > TOL:
            frappe.throw(_("Row {0}: outgoing Stock Entry quantity does not match released quantity.").format(row.idx))

        tracking = frappe.db.get_value("Item", row.item_code, ["has_serial_no", "has_batch_no"], as_dict=True)
        if tracking and (cint(tracking.has_serial_no) or cint(tracking.has_batch_no)):
            frappe.throw(
                _("Row {0}: serialized/batched Item {1} is not unlocked by the accepted C10 quantity-only Arrival path yet.").format(
                    row.idx, frappe.bold(row.item_code)
                )
            )

    return outgoing, transit_warehouse


def _resolve_arrival_quantities(doc, raw):
    requested = _parse_arrival_quantities(raw)
    remaining_before = {}
    row_by_item = {}
    for row in doc.items:
        remaining = flt(row.released_qty) - flt(row.arrived_qty)
        if remaining > TOL:
            remaining_before[row.item_code] = remaining
            row_by_item[row.item_code] = row

    if not remaining_before:
        frappe.throw(_("No remaining in-transit quantity is available for this transfer."))

    requested_keys = set(requested)
    remaining_keys = set(remaining_before)
    if requested_keys != remaining_keys:
        missing = sorted(remaining_keys - requested_keys)
        extra = sorted(requested_keys - remaining_keys)
        parts = []
        if missing:
            parts.append(_("missing active Items: {0}").format(", ".join(missing)))
        if extra:
            parts.append(_("Items with no remaining transit quantity: {0}").format(", ".join(extra)))
        frappe.throw(
            _("Arrival quantities must cover every Item that still has quantity in transit ({0}).").format("; ".join(parts))
        )

    resolved = {}
    for item_code, remaining in remaining_before.items():
        row = row_by_item[item_code]
        qty = flt(requested[item_code])
        if not math.isfinite(qty) or qty <= 0:
            frappe.throw(_("Row {0}: Arrival Qty must be greater than zero.").format(row.idx))
        if qty > remaining + TOL:
            frappe.throw(
                _("Row {0}: Arrival Qty {1} exceeds remaining in-transit quantity {2}.").format(
                    row.idx, qty, remaining
                )
            )
        must_whole = cint(frappe.db.get_value("UOM", row.uom, "must_be_whole_number") or 0)
        if must_whole and abs(qty - round(qty)) > TOL:
            frappe.throw(
                _("Row {0}: Arrival Qty must be a whole number for UOM {1}.").format(
                    row.idx, frappe.bold(row.uom)
                )
            )
        resolved[item_code] = qty
    return resolved, remaining_before


def _make_incoming_preview_under_admin(outgoing_name):
    from erpnext.stock.doctype.stock_entry.stock_entry import make_stock_in_entry

    business_user = frappe.session.user
    frappe.set_user("Administrator")
    try:
        incoming = make_stock_in_entry(outgoing_name)
    finally:
        frappe.set_user(business_user)

    if isinstance(incoming, dict):
        incoming = frappe.get_doc(incoming)
    if frappe.session.user != business_user:
        frappe.throw(_("Warehouse user context was not restored after ERPNext transit mapping."))
    return incoming


def _validate_native_remaining_mapper(transfer, mapped, transit_warehouse, remaining_before):
    if not mapped:
        frappe.throw(_("ERPNext did not return a remaining-quantity incoming Stock Entry."))
    if mapped.outgoing_stock_entry != transfer.outgoing_stock_entry:
        frappe.throw(_("Mapped Arrival links the wrong outgoing Stock Entry."))
    if mapped.purpose != "Material Transfer" or mapped.stock_entry_type != "Material Transfer":
        frappe.throw(_("Mapped Arrival is not a Material Transfer."))
    if mapped.docstatus != 0 or not mapped.is_new():
        frappe.throw(_("Mapped Arrival must be a new unsaved Stock Entry before posting."))

    rows = {}
    for row in mapped.items:
        if row.item_code in rows:
            frappe.throw(_("Mapped Arrival contains duplicate item lineage."))
        rows[row.item_code] = row
    if set(rows) != set(remaining_before):
        frappe.throw(_("ERPNext remaining-quantity mapper item set does not match the NKT transfer remainder."))

    for item_code, expected_remaining in remaining_before.items():
        row = rows[item_code]
        if row.against_stock_entry != transfer.outgoing_stock_entry:
            frappe.throw(_("Mapped Item {0} has wrong outgoing Stock Entry lineage.").format(frappe.bold(item_code)))
        if row.s_warehouse != transit_warehouse:
            frappe.throw(_("Mapped Item {0} does not source from Goods In Transit.").format(frappe.bold(item_code)))
        if abs(flt(row.qty) - expected_remaining) > TOL:
            frappe.throw(
                _("ERPNext remaining quantity for Item {0} is {1}; expected {2}.").format(
                    frappe.bold(item_code), flt(row.qty), expected_remaining
                )
            )


def _verify_submitted_incoming(stock_entry, transfer, transit_warehouse, arrival_qtys, *, expected_arrival_time=None):
    if stock_entry.docstatus != 1:
        frappe.throw(_("ERPNext did not submit the destination incoming Stock Entry."))
    if stock_entry.stock_entry_type != "Material Transfer" or stock_entry.purpose != "Material Transfer":
        frappe.throw(_("Incoming Stock Entry is not a Material Transfer."))
    if stock_entry.outgoing_stock_entry != transfer.outgoing_stock_entry:
        frappe.throw(_("Incoming Stock Entry is not linked to the correct outgoing transit entry."))
    if stock_entry.from_warehouse != transit_warehouse or stock_entry.to_warehouse != transfer.destination_warehouse:
        frappe.throw(_("Incoming Stock Entry warehouse lineage does not match the NKT transfer."))
    if expected_arrival_time:
        expected_arrival_time = get_datetime(expected_arrival_time)
        posting = get_datetime(f"{stock_entry.posting_date} {stock_entry.posting_time}")
        if posting != expected_arrival_time:
            frappe.throw(_("Incoming Stock Entry did not preserve the original physical Arrival date/time."))
    elif getdate(stock_entry.posting_date) != getdate(today()):
        frappe.throw(_("Incoming Stock Entry did not use today's physical Arrival date."))

    actual = {}
    for row in stock_entry.items:
        if row.item_code in actual:
            frappe.throw(_("Incoming Stock Entry contains duplicate item lineage."))
        actual[row.item_code] = row
    if set(actual) != set(arrival_qtys):
        frappe.throw(_("Incoming Stock Entry item set does not exactly match this Arrival."))
    for item_code, qty in arrival_qtys.items():
        row = actual[item_code]
        if row.s_warehouse != transit_warehouse or row.t_warehouse != transfer.destination_warehouse:
            frappe.throw(_("Incoming Item {0} warehouse lineage does not match this Arrival.").format(frappe.bold(item_code)))
        if row.against_stock_entry != transfer.outgoing_stock_entry:
            frappe.throw(_("Incoming Item {0} has wrong outgoing Stock Entry lineage.").format(frappe.bold(item_code)))
        if abs(flt(row.qty) - flt(qty)) > TOL:
            frappe.throw(_("Incoming Item {0} quantity does not exactly match the recorded Arrival.").format(frappe.bold(item_code)))

    # Verify only this voucher's stock effect, avoiding false failures from unrelated concurrent stock activity.
    sle = frappe.get_all(
        "Stock Ledger Entry",
        filters={"voucher_type": "Stock Entry", "voucher_no": stock_entry.name, "is_cancelled": 0},
        fields=["item_code", "warehouse", "actual_qty"],
        limit_page_length=500,
    )
    summed = {}
    for row in sle:
        key = (row.item_code, row.warehouse)
        summed[key] = summed.get(key, 0.0) + flt(row.actual_qty)

    for item_code, qty in arrival_qtys.items():
        if abs(summed.get((item_code, transit_warehouse), 0.0) + flt(qty)) > TOL:
            frappe.throw(_("Stock Ledger did not remove the exact Arrival quantity from Goods In Transit for Item {0}.").format(frappe.bold(item_code)))
        if abs(summed.get((item_code, transfer.destination_warehouse), 0.0) - flt(qty)) > TOL:
            frappe.throw(_("Stock Ledger did not add the exact Arrival quantity to Destination for Item {0}.").format(frappe.bold(item_code)))
        if abs(summed.get((item_code, transfer.source_warehouse), 0.0)) > TOL:
            frappe.throw(_("Destination Arrival unexpectedly changed Source stock for Item {0}.").format(frappe.bold(item_code)))
        for (sle_item, warehouse), delta in summed.items():
            if sle_item == item_code and warehouse not in {transit_warehouse, transfer.destination_warehouse} and abs(delta) > TOL:
                frappe.throw(_("Destination Arrival created an unexpected Stock Ledger effect for Item {0} in Warehouse {1}.").format(frappe.bold(item_code), frappe.bold(warehouse)))


def _arrival_remaining_after(doc, arrival_qtys):
    remaining_after = {}
    cumulative_after = {}
    completed = True
    for row in doc.items:
        new_arrived = flt(row.arrived_qty) + flt(arrival_qtys.get(row.item_code, 0))
        if new_arrived > flt(row.released_qty) + TOL:
            frappe.throw(_("Row {0}: cumulative Arrival would exceed released quantity.").format(row.idx))
        cumulative_after[row.item_code] = new_arrived
        remaining = max(0.0, flt(row.released_qty) - new_arrived)
        remaining_after[row.item_code] = remaining
        if remaining > TOL:
            completed = False
    return cumulative_after, remaining_after, completed


@frappe.whitelist()
def receive_transfer(transfer_name: str, arrival_quantities=None):
    """Record a physical destination Arrival against an in-transit NKT transfer.

    C10E uses ERPNext's accepted make_stock_in_entry path. Partial arrivals remain
    In Transit; the wrapper becomes Completed only when every released item has zero
    remaining quantity. Short/damaged/wet/busted facts remain in the separate C10C
    discrepancy layer and are never converted here into Supplier/AP/payroll/fake-sale logic.
    """
    transfer_name = (transfer_name or "").strip()
    if not transfer_name:
        frappe.throw(_("Internal Warehouse Transfer is required."))

    _lock_transfer_row(transfer_name)
    doc = frappe.get_doc("NKT Warehouse Transfer", transfer_name)
    preserved_context = _preserved_arrival_context(doc)
    if preserved_context:
        operator = preserved_context["operator"]
        if arrival_quantities is not None:
            requested = _parse_arrival_quantities(arrival_quantities)
            if set(requested) != set(preserved_context["arrival_quantities"]):
                frappe.throw(_("Materializer Arrival quantities conflict with the preserved journal."))
            for item_code, qty in preserved_context["arrival_quantities"].items():
                if abs(flt(requested[item_code]) - flt(qty)) > TOL:
                    frappe.throw(_("Materializer Arrival quantities conflict with the preserved journal."))
        arrival_quantities = preserved_context["arrival_quantities"]
    else:
        operator = _require_destination_arrival_role()
        doc.check_permission("read")

    outgoing, transit_warehouse = _validate_destination_arrival_header(
        doc,
        preserved_context=preserved_context,
    )
    arrival_qtys, remaining_before = _resolve_arrival_quantities(doc, arrival_quantities)

    per_before = flt(outgoing.per_transferred)
    mapped = _make_incoming_preview_under_admin(outgoing.name)
    _validate_native_remaining_mapper(doc, mapped, transit_warehouse, remaining_before)

    arrival_time = (
        get_datetime(preserved_context["arrived_at"])
        if preserved_context
        else now_datetime()
    )
    mapped.from_warehouse = transit_warehouse
    mapped.to_warehouse = doc.destination_warehouse
    mapped.posting_date = getdate(arrival_time)
    mapped.posting_time = arrival_time.strftime("%H:%M:%S")
    if mapped.meta.get_field("set_posting_time"):
        mapped.set_posting_time = 1
    mapped.remarks = _(
        "NKT Internal Warehouse Transfer {0}: physical destination Arrival against outgoing {1}"
    ).format(doc.name, outgoing.name)

    for row in mapped.items:
        row.s_warehouse = transit_warehouse
        row.t_warehouse = doc.destination_warehouse
        row.qty = arrival_qtys[row.item_code]

    # NKT Warehouse remains without direct Stock Entry DocPerm. Mapping used a narrow
    # Administrator context only because ERPNext's mapper checks create permission;
    # the business user is restored before the actual incoming document is inserted/submitted.
    mapped.flags.ignore_permissions = True
    mapped.insert(ignore_permissions=True)
    mapped.flags.ignore_permissions = True
    mapped.submit()
    _verify_submitted_incoming(
        mapped,
        doc,
        transit_warehouse,
        arrival_qtys,
        expected_arrival_time=(arrival_time if preserved_context else None),
    )

    cumulative_after, remaining_after, completed = _arrival_remaining_after(doc, arrival_qtys)
    for row in doc.items:
        frappe.db.set_value(
            "NKT Warehouse Transfer Item",
            row.name,
            "arrived_qty",
            cumulative_after[row.item_code],
            update_modified=False,
        )

    parent_updates = {"status": COMPLETED_STATUS if completed else RELEASED_STATUS}
    if completed:
        parent_updates.update({
            "arrived_by": operator,
            "arrived_at": arrival_time,
            "incoming_stock_entry": mapped.name,
        })
    frappe.db.set_value("NKT Warehouse Transfer", doc.name, parent_updates, update_modified=True)

    outgoing_after = frappe.get_doc("Stock Entry", outgoing.name)
    per_after = flt(outgoing_after.per_transferred)
    if per_after + TOL < per_before:
        frappe.throw(_("ERPNext outgoing transfer percentage moved backwards after Arrival."))
    if completed:
        if abs(per_after - 100.0) > TOL:
            frappe.throw(_("ERPNext outgoing transfer did not reach 100% after full Arrival."))
    elif not (per_after > per_before + TOL and per_after < 100.0 - TOL):
        frappe.throw(_("ERPNext outgoing transfer percentage did not reflect a valid partial Arrival."))

    return {
        "status": COMPLETED_STATUS if completed else RELEASED_STATUS,
        "transfer": doc.name,
        "outgoing_stock_entry": outgoing.name,
        "incoming_stock_entry": mapped.name,
        "arrival_quantities": arrival_qtys,
        "cumulative_arrived": cumulative_after,
        "remaining_in_transit": remaining_after,
        "completed": completed,
        "arrived_by": operator,
        "arrival_time": arrival_time,
        "posting_date": getdate(arrival_time),
        "per_transferred_before": per_before,
        "per_transferred_after": per_after,
        "discrepancy_auto_created": False,
        "supplier_or_financial_record_auto_created": False,
        "preserved_offline_arrival_materialized": bool(preserved_context),
        "preserved_event_uuid": (
            preserved_context["event_uuid"] if preserved_context else None
        ),
    }

