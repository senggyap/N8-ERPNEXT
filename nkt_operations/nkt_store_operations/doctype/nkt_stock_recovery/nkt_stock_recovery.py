import hashlib
import json
import math

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import flt, get_datetime, now_datetime
from frappe.utils.password import check_password

from nkt_operations.nkt_store_operations.features.security.role_hierarchy import has_nkt_authority

TOL = 0.005
REPAIR = "Repair / Rebag Damaged Sack"
RECOVER = "Recover Damaged Contents to Fraction"
PACK = "Pack Fraction into Saleable Sacks"
COMPLETE = "Complete Underweight Damaged Sack"
DISPOSE_DAMAGED = "Dispose Damaged Stock"
DISPOSE_FRACTION = "Dispose Fraction Stock"
DISPOSALS = {DISPOSE_DAMAGED, DISPOSE_FRACTION}
CONVERSIONS = {REPAIR, RECOVER, PACK, COMPLETE}


class NKTStockRecovery(Document):
    def before_validate(self):
        self.set_defaults()
        self.load_mapping()
        self.clear_unused_quantities()
        self.calculate_summary()
        self.invalidate_stale_approval()

    def validate(self):
        self.validate_mapping()
        self.validate_warehouses()
        self.validate_quantities()
        self.validate_disposal_account()

    def before_submit(self):
        self.load_mapping()
        self.clear_unused_quantities()
        self.calculate_summary()
        self.validate()
        self.validate_approval()
        self.lock_and_validate_stock()

    def on_submit(self):
        entry = self.create_stock_entry()
        self.db_set({"stock_entry": entry.name, "status": "Completed"}, update_modified=False)

    def on_cancel(self):
        if self.stock_entry:
            entry = frappe.get_doc("Stock Entry", self.stock_entry)
            if entry.docstatus == 1:
                entry.flags.ignore_permissions = True
                entry.cancel()
        self.db_set("status", "Cancelled", update_modified=False)

    def set_defaults(self):
        if not self.company:
            self.company = frappe.defaults.get_user_default("Company")
        if not self.recovery_datetime:
            self.recovery_datetime = now_datetime()
        if not self.prepared_by:
            self.prepared_by = frappe.session.user
        if not self.status:
            self.status = "Draft"
        if self.recovery_type in CONVERSIONS:
            self.approval_status = "Not Required"
            if self.docstatus == 0:
                self.status = "Draft"
        elif not self.approval_status or self.approval_status == "Not Required":
            self.approval_status = "Pending Approval"
            if self.docstatus == 0 and self.status not in {"Approved","Completed"}:
                self.status = "Pending Approval"
        if self.company and not self.disposal_expense_account:
            self.disposal_expense_account = frappe.db.get_value(
                "Company", self.company, "stock_adjustment_account"
            )

    def load_mapping(self):
        if not self.base_saleable_item:
            return
        item = frappe.db.get_value(
            "Item", self.base_saleable_item,
            ["disabled","is_stock_item","nkt_stock_form","nkt_standard_sack_weight_kg",
             "nkt_damaged_item","nkt_fraction_item"], as_dict=True
        )
        if not item or item.disabled or not item.is_stock_item:
            frappe.throw(_("Saleable Item is missing, disabled, or not a stock item."))
        if item.nkt_stock_form != "Saleable Sack":
            frappe.throw(_("Selected Item must be marked as NKT Stock Form: Saleable Sack."))
        self.damaged_item = item.nkt_damaged_item
        self.fraction_item = item.nkt_fraction_item
        self.standard_sack_weight_kg = flt(item.nkt_standard_sack_weight_kg)

    def validate_mapping(self):
        if not self.base_saleable_item:
            frappe.throw(_("Saleable Item is required."))
        if not self.damaged_item or not self.fraction_item:
            frappe.throw(_("Create/link the Damaged and Fraction Items first."))
        if flt(self.standard_sack_weight_kg) <= 0:
            frappe.throw(_("Standard Sack Weight must be greater than zero."))

        saleable_uom = frappe.db.get_value("Item", self.base_saleable_item, "stock_uom")
        damaged = frappe.db.get_value(
            "Item", self.damaged_item,
            ["disabled","is_stock_item","stock_uom","nkt_stock_form","nkt_base_saleable_item"],
            as_dict=True
        )
        fraction = frappe.db.get_value(
            "Item", self.fraction_item,
            ["disabled","is_stock_item","stock_uom","nkt_stock_form","nkt_base_saleable_item"],
            as_dict=True
        )
        if (
            not damaged or damaged.disabled or not damaged.is_stock_item
            or damaged.stock_uom != saleable_uom
            or damaged.nkt_stock_form != "Damaged Sack"
            or damaged.nkt_base_saleable_item != self.base_saleable_item
        ):
            frappe.throw(_("Linked Damaged Item mapping is invalid."))
        if (
            not fraction or fraction.disabled or not fraction.is_stock_item
            or fraction.stock_uom != "Kg"
            or fraction.nkt_stock_form != "Fraction Stock"
            or fraction.nkt_base_saleable_item != self.base_saleable_item
        ):
            frappe.throw(_("Linked Fraction Item mapping is invalid."))

    def validate_warehouse(self, name, label):
        if not name:
            frappe.throw(_("{0} is required.").format(label))
        row = frappe.db.get_value(
            "Warehouse", name, ["is_group","disabled","company"], as_dict=True
        )
        if not row:
            frappe.throw(_("{0} does not exist.").format(label))
        if row.is_group or row.disabled:
            frappe.throw(_("{0} must be an enabled leaf warehouse.").format(label))
        if row.company and row.company != self.company:
            frappe.throw(_("{0} belongs to another company.").format(label))

    def validate_warehouses(self):
        self.validate_warehouse(self.source_warehouse, _("Source Warehouse"))
        if self.recovery_type in CONVERSIONS:
            self.validate_warehouse(self.target_warehouse, _("Target Warehouse"))
        if (
            self.recovery_type == PACK
            and self.source_warehouse
            and self.target_warehouse
            and self.source_warehouse != self.target_warehouse
        ):
            frappe.throw(
                _(
                    "Fraction packing must happen inside one warehouse. "
                    "Transfer Fraction stock with the normal DR/warehouse-transfer flow first."
                )
            )

    def fraction_pack_capacity(self):
        if not self.fraction_item or not self.source_warehouse:
            return 0.0, 0
        available = flt(
            frappe.db.get_value(
                "Bin",
                {
                    "item_code": self.fraction_item,
                    "warehouse": self.source_warehouse,
                },
                "actual_qty",
            )
            or 0
        )
        weight = flt(self.standard_sack_weight_kg)
        maximum = (
            int(math.floor((max(available, 0) + TOL) / weight))
            if weight > TOL
            else 0
        )
        return available, maximum

    def clear_unused_quantities(self):
        keep = {
            REPAIR: {"damaged_sacks_consumed","saleable_sacks_produced"},
            RECOVER: {"damaged_sacks_consumed","fraction_kg_produced"},
            PACK: {"fraction_kg_consumed","saleable_sacks_produced"},
            COMPLETE: {"damaged_sacks_consumed","measured_damaged_contents_kg",
                       "fraction_kg_consumed","saleable_sacks_produced"},
            DISPOSE_DAMAGED: {"damaged_sacks_consumed"},
            DISPOSE_FRACTION: {"fraction_kg_consumed"},
        }.get(self.recovery_type)
        if not keep:
            return
        all_fields = {
            "damaged_sacks_consumed","measured_damaged_contents_kg",
            "fraction_kg_consumed","saleable_sacks_produced","fraction_kg_produced"
        }
        for fieldname in all_fields - keep:
            self.set(fieldname, 0)

    def calculate_summary(self):
        self.expected_weight_kg = 0
        self.recorded_loss_kg = 0
        self.available_fraction_kg = 0
        self.max_saleable_sacks = 0

        if self.recovery_type == PACK:
            available, maximum = self.fraction_pack_capacity()
            self.available_fraction_kg = available
            self.max_saleable_sacks = maximum
        if self.recovery_type == RECOVER:
            expected = flt(self.damaged_sacks_consumed) * flt(self.standard_sack_weight_kg)
            self.expected_weight_kg = expected
            self.recorded_loss_kg = max(expected - flt(self.fraction_kg_produced), 0)
        elif self.recovery_type == COMPLETE:
            expected = flt(self.saleable_sacks_produced) * flt(self.standard_sack_weight_kg)
            self.expected_weight_kg = expected
            self.recorded_loss_kg = max(
                expected - flt(self.measured_damaged_contents_kg) - flt(self.fraction_kg_consumed), 0
            )

    def positive(self, value, label):
        if flt(value) <= TOL:
            frappe.throw(_("{0} must be greater than zero.").format(label))

    def whole(self, value, label):
        self.positive(value, label)
        if abs(flt(value) - round(flt(value))) > TOL:
            frappe.throw(_("{0} must be a whole number.").format(label))

    def validate_quantities(self):
        action = self.recovery_type
        d = flt(self.damaged_sacks_consumed)
        measured = flt(self.measured_damaged_contents_kg)
        fin = flt(self.fraction_kg_consumed)
        sout = flt(self.saleable_sacks_produced)
        fout = flt(self.fraction_kg_produced)
        weight = flt(self.standard_sack_weight_kg)

        if action == REPAIR:
            self.whole(d, _("Damaged Sacks Consumed"))
            self.whole(sout, _("Saleable Sacks Produced"))
            if abs(d - sout) > TOL:
                frappe.throw(_("Repair/Rebag must be one damaged sack to one saleable sack."))
        elif action == RECOVER:
            self.whole(d, _("Damaged Sacks Consumed"))
            self.positive(fout, _("Fraction Produced"))
            if fout > d * weight + TOL:
                frappe.throw(_("Fraction Produced exceeds the standard contents of the damaged sacks."))
        elif action == PACK:
            self.positive(fin, _("Fraction Consumed"))
            self.whole(sout, _("Saleable Sacks Produced"))
            required = sout * weight
            if abs(fin - required) > TOL:
                frappe.throw(_("Fraction Consumed must equal {0} kg.").format(required))
            available, maximum = self.fraction_pack_capacity()
            if sout > maximum:
                frappe.throw(
                    _(
                        "Maximum saleable sacks from the current Fraction balance is {0}. "
                        "Available Fraction: {1} kg; Standard Sack Weight: {2} kg."
                    ).format(maximum, available, weight)
                )
        elif action == COMPLETE:
            self.whole(d, _("Damaged Sacks Consumed"))
            self.positive(measured, _("Measured Damaged Contents"))
            self.positive(fin, _("Fraction Consumed"))
            self.whole(sout, _("Saleable Sacks Produced"))
            if abs(d - sout) > TOL:
                frappe.throw(_("Each underweight damaged sack must produce one saleable sack."))
            required = sout * weight
            if measured >= required - TOL:
                frappe.throw(_("Measured contents must be below the completed output weight."))
            if abs(measured + fin - required) > TOL:
                frappe.throw(_("Measured contents plus Fraction Consumed must equal {0} kg.").format(required))
        elif action == DISPOSE_DAMAGED:
            self.whole(d, _("Damaged Sacks Disposed"))
        elif action == DISPOSE_FRACTION:
            self.positive(fin, _("Fraction Disposed"))
        else:
            frappe.throw(_("Select a Recovery Action."))

    def validate_disposal_account(self):
        if self.recovery_type not in DISPOSALS:
            return
        if not self.disposal_expense_account:
            frappe.throw(_("Disposal Expense Account is required."))
        acc = frappe.db.get_value(
            "Account", self.disposal_expense_account,
            ["company","is_group","root_type","account_type"], as_dict=True
        )
        if (
            not acc or acc.is_group or acc.company != self.company
            or acc.root_type != "Expense" or acc.account_type == "Stock"
        ):
            frappe.throw(_("Select a non-group Expense account for this company."))

    def inputs(self):
        rows = []
        if self.recovery_type in {REPAIR, RECOVER, COMPLETE, DISPOSE_DAMAGED}:
            rows.append((self.damaged_item, flt(self.damaged_sacks_consumed)))
        if self.recovery_type in {PACK, COMPLETE, DISPOSE_FRACTION}:
            rows.append((self.fraction_item, flt(self.fraction_kg_consumed)))
        return [(item, qty) for item, qty in rows if qty > TOL]

    def outputs(self):
        if self.recovery_type in {REPAIR, PACK, COMPLETE}:
            return [(self.base_saleable_item, flt(self.saleable_sacks_produced))]
        if self.recovery_type == RECOVER:
            return [(self.fraction_item, flt(self.fraction_kg_produced))]
        return []

    def lock_and_validate_stock(self):
        for item_code, required in self.inputs():
            rows = frappe.db.sql(
                """SELECT actual_qty FROM `tabBin`
                   WHERE item_code=%s AND warehouse=%s FOR UPDATE""",
                (item_code, self.source_warehouse), as_dict=True
            )
            available = flt(rows[0].actual_qty) if rows else 0
            if available + TOL < required:
                frappe.throw(
                    _("Insufficient stock for {0} in {1}. Available: {2}; Required: {3}.").format(
                        item_code, self.source_warehouse, available, required
                    )
                )

    def signature(self):
        recovery_datetime = get_datetime(
            self.recovery_datetime
        )

        payload = {
            "company": self.company,
            "recovery_datetime": (
                recovery_datetime.strftime(
                    "%Y-%m-%d %H:%M:%S"
                )
                if recovery_datetime
                else ""
            ),
            "recovery_type": self.recovery_type,
            "source_warehouse": self.source_warehouse,
            "target_warehouse": self.target_warehouse,
            "base_saleable_item": self.base_saleable_item,
            "damaged_item": self.damaged_item,
            "fraction_item": self.fraction_item,
            "standard_sack_weight_kg": round(
                flt(self.standard_sack_weight_kg), 6
            ),
            "damaged_sacks_consumed": round(
                flt(self.damaged_sacks_consumed), 6
            ),
            "measured_damaged_contents_kg": round(
                flt(self.measured_damaged_contents_kg), 6
            ),
            "fraction_kg_consumed": round(
                flt(self.fraction_kg_consumed), 6
            ),
            "saleable_sacks_produced": round(
                flt(self.saleable_sacks_produced), 6
            ),
            "fraction_kg_produced": round(
                flt(self.fraction_kg_produced), 6
            ),
            "disposal_expense_account": (
                self.disposal_expense_account or ""
            ),
            "reason": (self.reason or "").strip(),
            "remarks": (self.remarks or "").strip(),
        }

        raw = json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")

        return hashlib.sha256(raw).hexdigest()

    def clear_approval(self):
        self.approval_status = "Pending Approval"
        self.status = "Pending Approval"
        self.approved_by = None
        self.approved_on = None
        self.approval_reason = None
        self.approval_signature = None

    def invalidate_stale_approval(self):
        if self.docstatus != 0:
            return
        if self.recovery_type in CONVERSIONS:
            self.approval_status = "Not Required"
            self.status = "Draft"
            self.approved_by = None
            self.approved_on = None
            self.approval_reason = None
            self.approval_signature = None
            return
        if self.approval_status != "Approved":
            self.status = "Pending Approval"
        elif not self.approval_signature or self.approval_signature != self.signature():
            self.clear_approval()

    def validate_approval(self):
        if self.recovery_type in CONVERSIONS:
            return
        if self.approval_status != "Approved":
            frappe.throw(_("Owner or Administrator approval is required before submission."))
        if not all([self.approved_by,self.approved_on,self.approval_reason,self.approval_signature]):
            frappe.throw(_("Approval record is incomplete."))
        if self.approval_signature != self.signature():
            frappe.throw(_("Recovery details changed after approval. Request approval again."))
        if not has_nkt_authority(10, self.approved_by):
            frappe.throw(_("Approving user no longer has Owner or Administrator authority."))

    def difference_account(self):
        if self.recovery_type in DISPOSALS:
            return self.disposal_expense_account
        account = frappe.db.get_value("Company", self.company, "stock_adjustment_account")
        if not account:
            frappe.throw(_("Set the Company's Stock Adjustment Account first."))
        return account

    def stock_entry_type(self, purpose):
        name = frappe.db.get_value(
            "Stock Entry Type", {"purpose": purpose, "is_standard": 1}, "name"
        )
        if not name:
            frappe.throw(_("No standard Stock Entry Type exists for {0}.").format(purpose))
        return name

    def create_stock_entry(self):
        purpose = "Material Issue" if self.recovery_type in DISPOSALS else "Repack"
        dt = get_datetime(self.recovery_datetime)
        account = self.difference_account()

        pack_output_rate = 0
        if self.recovery_type == PACK:
            fraction_rate = flt(
                frappe.db.get_value(
                    "Bin",
                    {
                        "item_code": self.fraction_item,
                        "warehouse": self.source_warehouse,
                    },
                    "valuation_rate",
                )
                or 0
            )
            if fraction_rate <= TOL:
                frappe.throw(
                    _(
                        "Fraction stock has zero inventory valuation. "
                        "Correct the source inventory valuation before repacking."
                    )
                )
            pack_output_rate = (
                fraction_rate * flt(self.fraction_kg_consumed)
                / flt(self.saleable_sacks_produced)
            )

        entry = frappe.get_doc({
            "doctype":"Stock Entry","company":self.company,"purpose":purpose,
            "stock_entry_type":self.stock_entry_type(purpose),"set_posting_time":1,
            "posting_date":dt.date(),"posting_time":dt.time(),
            "remarks":f"Created automatically from NKT Stock Recovery {self.name}.\n"
                      f"Action: {self.recovery_type}\nReason: {self.reason}"
                      + (f"\nRemarks: {self.remarks}" if self.remarks else "")
        })
        for item_code, qty in self.inputs():
            uom = frappe.db.get_value("Item", item_code, "stock_uom")
            input_row = {
                "item_code":item_code,"qty":qty,"uom":uom,"stock_uom":uom,
                "conversion_factor":1,"s_warehouse":self.source_warehouse,
                "expense_account":account,
                "allow_zero_valuation_rate":0 if self.recovery_type == PACK else 1,
            }
            entry.append("items", input_row)

        for item_code, qty in self.outputs():
            uom = frappe.db.get_value("Item", item_code, "stock_uom")
            output_row = {
                "item_code":item_code,"qty":qty,"uom":uom,"stock_uom":uom,
                "conversion_factor":1,"t_warehouse":self.target_warehouse,
                "expense_account":account,
                "allow_zero_valuation_rate":0 if self.recovery_type == PACK else 1,
                "is_finished_item":1,
            }
            if self.recovery_type == PACK:
                output_row["basic_rate"] = pack_output_rate
            entry.append("items", output_row)
        entry.flags.ignore_permissions = True
        entry.insert()
        entry.submit()
        return entry


@frappe.whitelist()
def get_pack_capacity(item_code, warehouse):
    item = frappe.get_doc("Item", item_code)
    item.check_permission("read")
    if item.nkt_stock_form != "Saleable Sack":
        frappe.throw(_("Select an Item marked as Saleable Sack."))
    fraction_item = item.nkt_fraction_item
    weight = flt(item.nkt_standard_sack_weight_kg)
    if not fraction_item or weight <= TOL:
        frappe.throw(_("Saleable Item needs a linked Fraction Item and Standard Sack Weight."))
    available = flt(
        frappe.db.get_value(
            "Bin",
            {"item_code": fraction_item, "warehouse": warehouse},
            "actual_qty",
        )
        or 0
    )
    maximum = int(math.floor((max(available, 0) + TOL) / weight))
    return {
        "fraction_item": fraction_item,
        "available_fraction_kg": available,
        "max_saleable_sacks": maximum,
        "standard_sack_weight_kg": weight,
    }


@frappe.whitelist()
def get_item_mapping(item_code):
    item = frappe.get_doc("Item", item_code)
    item.check_permission("read")
    if item.nkt_stock_form != "Saleable Sack":
        frappe.throw(_("Select an Item marked as Saleable Sack."))
    return {
        "damaged_item": item.nkt_damaged_item,
        "fraction_item": item.nkt_fraction_item,
        "standard_sack_weight_kg": item.nkt_standard_sack_weight_kg,
    }


@frappe.whitelist()
def get_stock_recovery_approval_mode():
    current_user = frappe.session.user

    return {
        "direct_approval": has_nkt_authority(
            10,
            current_user,
        ),
        "current_user": current_user,
    }


@frappe.whitelist(methods=["POST"])
def approve_stock_recovery(
    stock_recovery,
    approval_reason,
    admin_user=None,
    admin_password=None,
):
    approval_reason = (
        approval_reason or ""
    ).strip()

    if not approval_reason:
        frappe.throw(_("Approval Reason is required."))

    doc = frappe.get_doc(
        "NKT Stock Recovery",
        stock_recovery,
    )

    doc.check_permission("write")

    if doc.docstatus != 0:
        frappe.throw(
            _("Only a saved draft can be approved.")
        )
    if doc.recovery_type in CONVERSIONS:
        frappe.throw(
            _("Operational stock conversions do not require Owner/Admin pre-approval.")
        )

    # Normalize and save first. Approval and submission then
    # calculate the signature from the same stored values.
    doc.set_defaults()
    doc.load_mapping()
    doc.clear_unused_quantities()
    doc.calculate_summary()
    doc.validate()
    doc.save()
    doc.reload()

    current_user = frappe.session.user

    if has_nkt_authority(10, current_user):
        authenticated = current_user
    else:
        admin_user = (admin_user or "").strip()

        if not admin_user:
            frappe.throw(
                _(
                    "Owner or Administrator Username "
                    "is required."
                )
            )

        if not admin_password:
            frappe.throw(
                _(
                    "Owner or Administrator Password "
                    "is required."
                )
            )

        authenticated = check_password(
            admin_user,
            admin_password,
        )

        if not frappe.db.get_value(
            "User",
            authenticated,
            "enabled",
        ):
            frappe.throw(
                _("Approving account is disabled.")
            )

        if not has_nkt_authority(
            10,
            authenticated,
        ):
            frappe.throw(
                _(
                    "User does not have NKT Owner or "
                    "Administrator authority."
                )
            )

    # Reloaded stored values are signed here.
    doc.load_mapping()
    doc.clear_unused_quantities()
    doc.calculate_summary()

    values = {
        "approval_status": "Approved",
        "status": "Approved",
        "approved_by": authenticated,
        "approved_on": now_datetime(),
        "approval_reason": approval_reason,
        "approval_signature": doc.signature(),
    }

    frappe.db.set_value(
        "NKT Stock Recovery",
        doc.name,
        values,
        update_modified=True,
    )

    doc.add_comment(
        "Info",
        _(
            "Stock recovery approved by {0}. "
            "Reason: {1}"
        ).format(
            authenticated,
            approval_reason,
        ),
    )

    return {
        "approved_by": authenticated,
        "direct_approval": (
            authenticated == current_user
        ),
    }

