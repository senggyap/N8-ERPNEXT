from __future__ import annotations

import inspect

import frappe
from frappe import _
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields
from frappe.utils import flt, getdate, now_datetime, nowdate, nowtime

VERSION = "V2.0C.8C.1-PRODUCTION"
PARENT = "NKT Physical Inventory Adjustment"
CHILD = "NKT Physical Inventory Adjustment Item"

OPERATIONAL_ROLES = {
    "NKT Encoder",
    "NKT Warehouse",
    "NKT ADMINISTRATOR",
    "NKT OWNER",
}
ADMIN_ROLES = {"NKT ADMINISTRATOR", "NKT OWNER"}
TOL = 0.000001


def _assert_operational_authority():
    if frappe.session.user == "Administrator":
        return
    roles = set(frappe.get_roles())
    if not roles.intersection(OPERATIONAL_ROLES):
        frappe.throw(
            _("Only an authorized NKT Encoder, Warehouse, Admin, or Owner user may record a physical count."),
            frappe.PermissionError,
        )


def _assert_admin_authority():
    if frappe.session.user == "Administrator":
        return
    if not set(frappe.get_roles()).intersection(ADMIN_ROLES):
        frappe.throw(_("Only NKT Admin/Owner may perform the accountability review."), frappe.PermissionError)


def _warehouse_state(warehouse):
    if not warehouse:
        frappe.throw(_("Warehouse is required."))
    row = frappe.db.get_value(
        "Warehouse",
        warehouse,
        ["name", "company", "is_group", "disabled"],
        as_dict=True,
    )
    if not row:
        frappe.throw(_("Warehouse {0} does not exist.").format(warehouse))
    if row.is_group:
        frappe.throw(_("Warehouse {0} is a group and cannot be physically counted.").format(warehouse))
    if row.disabled:
        frappe.throw(_("Warehouse {0} is disabled.").format(warehouse))
    return row


def _uom_whole_number(stock_uom):
    if not stock_uom or not frappe.db.exists("UOM", stock_uom):
        return False
    return bool(frappe.db.get_value("UOM", stock_uom, "must_be_whole_number"))


def _latest_positive_sle_rate(item_code, warehouse):
    if not frappe.db.exists("DocType", "Stock Ledger Entry"):
        return 0
    filters = {
        "item_code": item_code,
        "warehouse": warehouse,
        "valuation_rate": [">", 0],
    }
    sle_meta = frappe.get_meta("Stock Ledger Entry")
    if sle_meta.has_field("is_cancelled"):
        filters["is_cancelled"] = 0
    row = frappe.get_all(
        "Stock Ledger Entry",
        filters=filters,
        fields=["valuation_rate"],
        order_by="posting_date desc, posting_time desc, creation desc",
        limit_page_length=1,
    )
    return flt(row[0].valuation_rate) if row else 0


def _resolve_valuation_rate(item_code, warehouse, bin_rate=0):
    rate = flt(bin_rate)
    if rate > TOL:
        return rate, "Bin"

    rate = _latest_positive_sle_rate(item_code, warehouse)
    if rate > TOL:
        return rate, "Latest Stock Ledger Entry"

    item_meta = frappe.get_meta("Item")
    if item_meta.has_field("valuation_rate"):
        rate = flt(frappe.db.get_value("Item", item_code, "valuation_rate"))
        if rate > TOL:
            return rate, "Item Default"

    return 0, "Unavailable"


def _item_snapshot(item_code, warehouse):
    item = frappe.db.get_value(
        "Item",
        item_code,
        [
            "name", "item_name", "stock_uom", "is_stock_item", "disabled",
            "has_serial_no", "has_batch_no"
        ],
        as_dict=True,
    )
    if not item:
        frappe.throw(_("Item {0} does not exist.").format(item_code))
    if not item.is_stock_item:
        frappe.throw(_("Item {0} is not a stock item.").format(item_code))
    if item.disabled:
        frappe.throw(_("Item {0} is disabled.").format(item_code))

    bin_meta = frappe.get_meta("Bin")
    bin_fields = ["actual_qty"]
    if bin_meta.has_field("valuation_rate"):
        bin_fields.append("valuation_rate")

    bin_row = frappe.db.get_value(
        "Bin",
        {"item_code": item_code, "warehouse": warehouse},
        bin_fields,
        as_dict=True,
    ) or frappe._dict()

    valuation_rate, valuation_source = _resolve_valuation_rate(
        item_code, warehouse, bin_row.get("valuation_rate")
    )

    return {
        "item_code": item.name,
        "item_name": item.item_name,
        "stock_uom": item.stock_uom,
        "system_qty": flt(bin_row.get("actual_qty")),
        "valuation_rate": valuation_rate,
        "valuation_source": valuation_source,
        "has_serial_no": int(item.has_serial_no or 0),
        "has_batch_no": int(item.has_batch_no or 0),
        "requires_whole_number": int(_uom_whole_number(item.stock_uom)),
    }


@frappe.whitelist()
def get_stock_snapshot(warehouse, item_code):
    _assert_operational_authority()
    _warehouse_state(warehouse)
    return _item_snapshot(item_code, warehouse)


def prepare_adjustment_document(doc, *, force_refresh=False):
    _assert_operational_authority()

    if doc.docstatus != 0 and not force_refresh:
        return _preview_from_stored(doc)

    warehouse = _warehouse_state(doc.warehouse)
    if doc.company and doc.company != warehouse.company:
        frappe.throw(
            _("Selected Company {0} does not match Warehouse company {1}.")
            .format(doc.company, warehouse.company)
        )
    doc.company = warehouse.company
    doc.business_date = nowdate()
    doc.counted_by = frappe.session.user
    doc.snapshot_datetime = now_datetime()
    if not doc.count_datetime:
        doc.count_datetime = now_datetime()
    if getdate(doc.count_datetime) != getdate(nowdate()):
        frappe.throw(
            _("Physical Inventory Adjustment must use the current business date. "
              "Do not backdate a physical count correction.")
        )
    if not doc.custom_nkt_adjustment_request_id:
        doc.custom_nkt_adjustment_request_id = frappe.generate_hash(length=24)

    blockers = []
    seen = set()
    variance_lines = 0

    for row in doc.items or []:
        if not row.item_code:
            continue
        if row.item_code in seen:
            frappe.throw(_("Item {0} appears more than once in this adjustment.").format(row.item_code))
        seen.add(row.item_code)

        snap = _item_snapshot(row.item_code, doc.warehouse)
        row.item_name = snap["item_name"]
        row.stock_uom = snap["stock_uom"]
        row.system_qty_snapshot = snap["system_qty"]
        row.valuation_rate_snapshot = snap["valuation_rate"]
        row.valuation_rate_source = snap["valuation_source"]
        row.has_serial_no = snap["has_serial_no"]
        row.has_batch_no = snap["has_batch_no"]
        row.requires_whole_number = snap["requires_whole_number"]
        row.row_blocker = ""
        row.posted_qty = 0

        if not int(row.physical_qty_confirmed or 0):
            row.row_blocker = _("Confirm that this item's physical quantity was actually counted.")
        else:
            physical_qty = flt(row.physical_qty)
            if physical_qty < -TOL:
                row.row_blocker = _("Physical quantity cannot be negative.")
            elif row.requires_whole_number and abs(physical_qty - round(physical_qty)) > TOL:
                row.row_blocker = _(
                    "Stock UOM {0} requires a whole-number quantity."
                ).format(row.stock_uom)
            elif row.has_serial_no or row.has_batch_no:
                row.row_blocker = _(
                    "Serialized/batched inventory needs explicit serial/batch reconciliation. "
                    "C8B does not allow generic quantity-only posting for this item."
                )

        physical_qty = flt(row.physical_qty)
        row.variance_qty = flt(physical_qty - flt(row.system_qty_snapshot), 6)
        if abs(row.variance_qty) <= TOL:
            row.variance_qty = 0
            row.variance_direction = "No Variance"
        elif row.variance_qty > 0:
            row.variance_direction = "Overage"
            variance_lines += 1
            if flt(row.valuation_rate_snapshot) <= TOL and not row.row_blocker:
                row.row_blocker = _(
                    "Overage has no positive valuation rate from Bin, Stock Ledger history, or Item default."
                )
        else:
            row.variance_direction = "Shortage"
            variance_lines += 1

        if row.row_blocker:
            blockers.append("{0}: {1}".format(row.item_code, row.row_blocker))

    doc.variance_line_count = variance_lines
    doc.blockers = "\n".join(blockers)
    if doc.review_status != "Pending Admin Review":
        doc.review_status = "Not Posted"

    if blockers:
        doc.adjustment_status = "Blocked"
    elif not (doc.items or []):
        doc.adjustment_status = "Draft"
    elif variance_lines == 0:
        doc.adjustment_status = "No Variance"
    else:
        doc.adjustment_status = "Ready to Post"

    return _preview_from_stored(doc)


def _preview_from_stored(doc):
    return {
        "name": doc.name,
        "company": doc.company,
        "warehouse": doc.warehouse,
        "business_date": doc.business_date,
        "count_datetime": doc.count_datetime,
        "snapshot_datetime": doc.snapshot_datetime,
        "counted_by": doc.counted_by,
        "adjustment_status": doc.adjustment_status,
        "review_status": doc.review_status,
        "stock_reconciliation": doc.stock_reconciliation,
        "posted_by": doc.get("posted_by"),
        "posted_on": doc.get("posted_on"),
        "variance_line_count": int(doc.variance_line_count or 0),
        "blockers": [x for x in (doc.blockers or "").splitlines() if x.strip()],
        "items": [
            {
                "item_code": row.item_code,
                "item_name": row.item_name,
                "stock_uom": row.stock_uom,
                "system_qty_snapshot": flt(row.system_qty_snapshot),
                "physical_qty": flt(row.physical_qty),
                "physical_qty_confirmed": int(row.physical_qty_confirmed or 0),
                "variance_qty": flt(row.variance_qty),
                "variance_direction": row.variance_direction,
                "posted_qty": flt(row.get("posted_qty")),
                "valuation_rate_snapshot": flt(row.valuation_rate_snapshot),
                "valuation_rate_source": row.get("valuation_rate_source"),
                "row_blocker": row.row_blocker,
            }
            for row in (doc.items or [])
        ],
    }


@frappe.whitelist()
def preview_adjustment(name):
    _assert_operational_authority()
    doc = frappe.get_doc(PARENT, name)
    if doc.docstatus == 0:
        return prepare_adjustment_document(doc)
    return _preview_from_stored(doc)


def validate_adjustment_document(doc, *, for_submit=False):
    preview = prepare_adjustment_document(doc) if doc.docstatus == 0 else _preview_from_stored(doc)
    if for_submit:
        if preview["blockers"]:
            frappe.throw(_("Resolve all physical inventory posting blockers first."))
        if not doc.physical_count_confirmed:
            frappe.throw(_("Confirm that the physical count reflects actual stock before posting."))
        if not doc.count_reason:
            frappe.throw(_("Count Reason is required before posting."))
        if doc.count_reason == "Other" and not (doc.operator_notes or "").strip():
            frappe.throw(_("Operator Notes are required when Count Reason is Other."))
        if not doc.items:
            frappe.throw(_("At least one counted item is required."))
        if int(doc.variance_line_count or 0) <= 0:
            frappe.throw(_("There is no quantity variance to post."))
        if getdate(doc.business_date) != getdate(nowdate()):
            frappe.throw(_("Physical inventory corrections must post on the current business date."))
    return preview


def _stock_reconciliation_accounting_defaults(sr, company):
    sr_meta = frappe.get_meta("Stock Reconciliation")
    company_meta = frappe.get_meta("Company")

    if sr_meta.has_field("expense_account") and not sr.get("expense_account"):
        for fieldname in ("stock_adjustment_account", "default_inventory_account"):
            if company_meta.has_field(fieldname):
                value = frappe.db.get_value("Company", company, fieldname)
                if value:
                    sr.expense_account = value
                    break

    if sr_meta.has_field("cost_center") and not sr.get("cost_center"):
        for fieldname in ("cost_center", "default_cost_center"):
            if company_meta.has_field(fieldname):
                value = frappe.db.get_value("Company", company, fieldname)
                if value:
                    sr.cost_center = value
                    break


def _existing_submitted_reconciliation(doc):
    linked = doc.stock_reconciliation
    if linked:
        state = frappe.db.get_value(
            "Stock Reconciliation",
            linked,
            ["name", "docstatus", "custom_nkt_physical_inventory_adjustment"],
            as_dict=True,
        )
        if not state:
            frappe.throw(_("Linked Stock Reconciliation {0} does not exist.").format(linked))
        if state.custom_nkt_physical_inventory_adjustment != doc.name:
            frappe.throw(_("Linked Stock Reconciliation does not belong to this adjustment."))
        if int(state.docstatus or 0) != 1:
            frappe.throw(_("Linked Stock Reconciliation is not Submitted."))
        return state.name

    existing = frappe.db.get_value(
        "Stock Reconciliation",
        {
            "custom_nkt_physical_inventory_adjustment": doc.name,
            "docstatus": 1,
        },
        "name",
        order_by="creation desc",
    )
    return existing


def _assert_production_posting_enabled():
    """C8C production posting gate.

    C8B R2 passed the complete rollback-only Stock Reconciliation acceptance
    suite. Production posting is therefore enabled, while all operational,
    current-date, quantity, valuation, permission, and post-result exactness
    guards remain active.
    """
    return True


def execute_adjustment(doc):
    _assert_operational_authority()
    _assert_production_posting_enabled()

    existing = _existing_submitted_reconciliation(doc)
    if existing:
        doc.stock_reconciliation = existing
        doc.adjustment_status = "Posted"
        doc.review_status = "Pending Admin Review"
        return frappe.get_doc("Stock Reconciliation", existing)

    # Refresh system quantities/valuation immediately before ledger posting.
    preview = prepare_adjustment_document(doc, force_refresh=True)
    validate_adjustment_document(doc, for_submit=True)

    variance_rows = [
        row for row in (doc.items or [])
        if abs(flt(row.variance_qty)) > TOL
    ]
    if not variance_rows:
        frappe.throw(_("There is no quantity variance to post."))

    sr = frappe.new_doc("Stock Reconciliation")
    sr.company = doc.company
    sr.purpose = "Stock Reconciliation"
    sr_meta = frappe.get_meta("Stock Reconciliation")
    if sr_meta.has_field("set_posting_time"):
        sr.set_posting_time = 1
    if sr_meta.has_field("posting_date"):
        sr.posting_date = nowdate()
    if sr_meta.has_field("posting_time"):
        sr.posting_time = nowtime()
    sr.custom_nkt_physical_inventory_correction = 1
    sr.custom_nkt_physical_inventory_adjustment = doc.name
    _stock_reconciliation_accounting_defaults(sr, doc.company)

    for row in variance_rows:
        values = {
            "item_code": row.item_code,
            "warehouse": doc.warehouse,
            "qty": flt(row.physical_qty),
        }
        if flt(row.valuation_rate_snapshot) > TOL:
            values["valuation_rate"] = flt(row.valuation_rate_snapshot)
        sr.append("items", values)

    # Stock Reconciliation remains a server-owned stock-ledger document.
    # Operational users deliberately do NOT receive direct Stock
    # Reconciliation write/submit permission. ERPNext's own validation calls
    # get_stock_balance_for(), which performs a session-user permission check,
    # so ignore_permissions alone is insufficient here.
    operator_user = frappe.session.user
    try:
        frappe.set_user("Administrator")
        sr.flags.ignore_permissions = True
        sr.insert(ignore_permissions=True)
        sr.flags.ignore_permissions = True
        sr.submit()
    finally:
        frappe.set_user(operator_user)

    # The ledger must end at the observed physical quantity. If ERPNext does
    # not produce that exact state, abort the transaction rather than leave a
    # partially trusted correction.
    for row in variance_rows:
        actual = flt(
            frappe.db.get_value(
                "Bin",
                {"item_code": row.item_code, "warehouse": doc.warehouse},
                "actual_qty",
            )
        )
        if abs(actual - flt(row.physical_qty)) > TOL:
            frappe.throw(
                _("Stock Reconciliation {0} did not leave {1} at the physical quantity. "
                  "Expected {2}, ERP Bin is {3}. Transaction aborted.")
                .format(sr.name, row.item_code, row.physical_qty, actual)
            )
        row.posted_qty = actual

    doc.stock_reconciliation = sr.name
    doc.adjustment_status = "Posted"
    doc.review_status = "Pending Admin Review"
    doc.posted_by = frappe.session.user
    doc.posted_on = now_datetime()
    doc.blockers = ""
    return sr


def install():
    custom_fields = {
        "Stock Reconciliation": [
            {
                "fieldname": "custom_nkt_physical_inventory_correction",
                "label": "NKT Physical Inventory Correction",
                "fieldtype": "Check",
                "insert_after": "purpose",
                "read_only": 1,
                "hidden": 1,
                "default": "0",
            },
            {
                "fieldname": "custom_nkt_physical_inventory_adjustment",
                "label": "NKT Physical Inventory Adjustment",
                "fieldtype": "Link",
                "options": PARENT,
                "insert_after": "custom_nkt_physical_inventory_correction",
                "read_only": 1,
                "hidden": 1,
            },
        ]
    }
    create_custom_fields(custom_fields, update=True)
    frappe.clear_cache(doctype="Stock Reconciliation")
    frappe.clear_cache(doctype=PARENT)
    frappe.clear_cache(doctype=CHILD)
    return verify()


def verify():
    checks = {}
    errors = []

    checks["parent_doctype_exists"] = bool(frappe.db.exists("DocType", PARENT))
    checks["child_doctype_exists"] = bool(frappe.db.exists("DocType", CHILD))
    if not checks["parent_doctype_exists"] or not checks["child_doctype_exists"]:
        return {"version": VERSION, "checks": checks, "errors": ["C8 DocTypes missing"], "passed": False}

    parent_meta = frappe.get_meta(PARENT)
    child_meta = frappe.get_meta(CHILD)

    required_parent_fields = {
        "company", "warehouse", "business_date", "count_datetime", "snapshot_datetime",
        "counted_by", "adjustment_status", "review_status", "physical_count_confirmed",
        "items", "variance_line_count", "blockers", "stock_reconciliation",
        "posted_by", "posted_on",
        "accountability_classification", "accountability_notes", "reviewed_by",
        "reviewed_on", "review_notes", "review_lock",
    }
    required_child_fields = {
        "item_code", "item_name", "stock_uom", "system_qty_snapshot", "physical_qty",
        "physical_qty_confirmed", "variance_qty", "variance_direction", "posted_qty",
        "valuation_rate_snapshot", "valuation_rate_source",
        "has_serial_no", "has_batch_no", "requires_whole_number", "row_blocker",
    }
    checks["parent_fields_complete"] = required_parent_fields.issubset(
        {f.fieldname for f in parent_meta.fields}
    )
    checks["child_fields_complete"] = required_child_fields.issubset(
        {f.fieldname for f in child_meta.fields}
    )

    physical_field = child_meta.get_field("physical_qty")
    checks["zero_physical_count_supported"] = bool(physical_field and not physical_field.reqd)

    role_names = {p.role for p in parent_meta.permissions if int(p.permlevel or 0) == 0 and p.submit}
    checks["operational_submit_roles_defined"] = {
        "NKT Encoder", "NKT Warehouse", "NKT ADMINISTRATOR", "NKT OWNER"
    }.issubset(role_names)
    checks["warehouse_role_exists"] = bool(frappe.db.exists("Role", "NKT Warehouse"))
    checks["warehouse_operator_alias_not_required"] = not bool(
        frappe.db.exists("Role", "NKT Warehouse Operator")
    )

    admin_level1 = {
        p.role for p in parent_meta.permissions
        if int(p.permlevel or 0) == 1 and p.read
    }
    checks["accountability_admin_only_permlevel"] = admin_level1 == {
        "NKT ADMINISTRATOR", "NKT OWNER"
    }

    sr_meta = frappe.get_meta("Stock Reconciliation")
    checks["stock_reconciliation_source_fields"] = bool(
        sr_meta.has_field("custom_nkt_physical_inventory_correction")
        and sr_meta.has_field("custom_nkt_physical_inventory_adjustment")
    )
    checks["no_direct_nkt_stock_reconciliation_submit_permission"] = not any(
        p.role in OPERATIONAL_ROLES and (p.write or p.create or p.submit or p.cancel)
        for p in (sr_meta.permissions or [])
    )

    controller_module = __import__(
        "nkt_operations.nkt_store_operations.doctype.nkt_physical_inventory_adjustment.nkt_physical_inventory_adjustment",
        fromlist=["NKTPhysicalInventoryAdjustment"],
    )
    controller_source = inspect.getsource(controller_module)
    checks["production_submit_unlocked_after_c8b_acceptance"] = (
        "nkt_c8_runtime_test" not in controller_source
        and "Production posting remains disabled until C8B passes" not in controller_source
        and "execute_adjustment(self)" in controller_source
    )

    runtime_functions = [
        _assert_operational_authority,
        _assert_admin_authority,
        _warehouse_state,
        _uom_whole_number,
        _latest_positive_sle_rate,
        _resolve_valuation_rate,
        _item_snapshot,
        get_stock_snapshot,
        prepare_adjustment_document,
        _preview_from_stored,
        preview_adjustment,
        validate_adjustment_document,
        _stock_reconciliation_accounting_defaults,
        _existing_submitted_reconciliation,
        _assert_production_posting_enabled,
        execute_adjustment,
        install,
    ]
    runtime_source = "\n".join(inspect.getsource(fn) for fn in runtime_functions)
    forbidden = ["Salary Slip", "Additional Salary", "Payroll Entry", "Sales Invoice", "NKT Cashier Sale"]
    checks["no_payroll_or_fake_sale_integration"] = not any(x in runtime_source for x in forbidden)

    checks["server_side_stock_reconciliation_only"] = (
        'frappe.new_doc("Stock Reconciliation")' in runtime_source
        and "ignore_permissions=True" in runtime_source
        and "custom_nkt_physical_inventory_adjustment" in runtime_source
    )
    checks["stock_reconciliation_posts_under_server_admin_context"] = all(
        token in runtime_source
        for token in (
            'operator_user = frappe.session.user',
            'frappe.set_user("Administrator")',
            'sr.insert(ignore_permissions=True)',
            'sr.submit()',
            'frappe.set_user(operator_user)',
        )
    )
    checks["pre_post_snapshot_force_refresh"] = "force_refresh=True" in runtime_source
    checks["post_result_bin_exactness_guard"] = "did not leave" in runtime_source and "Transaction aborted" in runtime_source
    checks["current_date_only"] = "Do not backdate a physical count correction" in runtime_source
    checks["serial_batch_generic_posting_blocked"] = "Serialized/batched inventory needs explicit serial/batch reconciliation" in runtime_source
    checks["valuation_fallback_present"] = "Latest Stock Ledger Entry" in runtime_source and "Item Default" in runtime_source
    checks["review_pending_after_posting"] = 'doc.review_status = "Pending Admin Review"' in runtime_source

    checks["no_c8_business_records_created_by_install"] = frappe.db.count(PARENT) == 0

    for key, value in checks.items():
        if not value:
            errors.append(key)

    return {
        "version": VERSION,
        "mode": "PRODUCTION PHYSICAL INVENTORY POSTING ENABLED",
        "authorized_operational_roles": sorted(OPERATIONAL_ROLES),
        "checks": checks,
        "errors": errors,
        "passed": all(checks.values()) and not errors,
    }
