from __future__ import annotations

import inspect

import frappe
from frappe import _
from frappe.utils import now_datetime

VERSION = "V2.0C.8C.1-PRODUCTION"
VERIFY_VERSION = "V2.0C.8C.2-VERIFY-R2"
PARENT = "NKT Physical Inventory Adjustment"
CLIENT_SCRIPT = "NKT C8 Physical Inventory Adjustment V2.0C.8C.1"
ADMIN_ROLES = {"NKT ADMINISTRATOR", "NKT OWNER"}

CLIENT_JS = "frappe.ui.form.on('NKT Physical Inventory Adjustment', {\n  refresh(frm) {\n    const roles = frappe.user_roles || [];\n    const isAdminOwner =\n      frappe.session.user === 'Administrator' ||\n      roles.includes('NKT OWNER') ||\n      roles.includes('NKT ADMINISTRATOR');\n\n    if (frm.doc.docstatus === 0) {\n      frm.dashboard.set_headline(\n        __('Record the actual physical count. On Submit, ERP stock is corrected immediately and the record becomes Pending Admin Review.')\n      );\n\n      frm.add_custom_button(__('Refresh Count Preview'), async () => {\n        await frm.save();\n        await frm.reload_doc();\n\n        const blockers = (frm.doc.blockers || '').split('\\n').filter(Boolean);\n        const msg = [\n          __('<b>System snapshot refreshed.</b>'),\n          __('Lines with variance: {0}', [frm.doc.variance_line_count || 0]),\n          blockers.length\n            ? '<br><b>' + __('Posting blockers') + '</b><br>' +\n              blockers.map(x => '• ' + frappe.utils.escape_html(x)).join('<br>')\n            : '<br>' + __('No posting blockers detected.')\n        ].join('<br>');\n\n        frappe.msgprint({\n          title: __('Physical Count Preview'),\n          indicator: blockers.length ? 'orange' : 'green',\n          message: msg\n        });\n      }, __('Actions'));\n    }\n\n    if (frm.doc.docstatus === 1) {\n      frm.dashboard.set_headline(\n        frm.doc.review_status === 'Reconciled'\n          ? __('Inventory correction posted and Admin Review reconciled.')\n          : __('Inventory correction is already posted. Admin review is separate from the stock correction.')\n      );\n\n      if (!isAdminOwner || frm.doc.review_lock) {\n        return;\n      }\n\n      if (frm.doc.review_status === 'Pending Admin Review') {\n        frm.add_custom_button(__('Start Admin Review'), async () => {\n          await frappe.call({\n            method: 'nkt_operations.nkt_store_operations.features.setup_validation.internal.review.start_admin_review',\n            args: { name: frm.doc.name },\n            freeze: true,\n            freeze_message: __('Starting Admin review...')\n          });\n          await frm.reload_doc();\n        }, __('Admin Review'));\n      }\n\n      if (['Pending Admin Review', 'Under Review', 'Discrepancy Flagged'].includes(frm.doc.review_status)) {\n        frm.add_custom_button(__('Reconcile'), () => {\n          show_review_dialog(frm, false);\n        }, __('Admin Review'));\n\n        frm.add_custom_button(__('Flag Discrepancy'), () => {\n          show_review_dialog(frm, true);\n        }, __('Admin Review'));\n      }\n    }\n  },\n\n  before_submit(frm) {\n    if (!frm.doc.physical_count_confirmed) {\n      frappe.throw(__('Confirm that the physical count reflects actual stock.'));\n    }\n    if (!frm.doc.count_reason) {\n      frappe.throw(__('Count Reason is required.'));\n    }\n    const rows = frm.doc.items || [];\n    if (!rows.length) {\n      frappe.throw(__('At least one counted item is required.'));\n    }\n    const uncounted = rows.filter(r => !r.physical_qty_confirmed);\n    if (uncounted.length) {\n      frappe.throw(__('Every item row must be marked Physical Quantity Counted, including rows physically counted as zero.'));\n    }\n  },\n\n  on_submit(frm) {\n    frappe.msgprint({\n      title: __('Inventory Corrected'),\n      indicator: 'green',\n      message:\n        __('ERP stock has been corrected to the submitted physical quantities.') +\n        '<br><br><b>' +\n        __('Admin Review Status: Pending Admin Review') +\n        '</b><br>' +\n        __('Do not create a fake sale or payroll deduction to explain the variance. Accountability is reviewed separately.')\n    });\n  }\n});\n\nfrappe.ui.form.on('NKT Physical Inventory Adjustment Item', {\n  item_code: async function(frm, cdt, cdn) {\n    const row = locals[cdt][cdn];\n    if (!row.item_code || !frm.doc.warehouse) {\n      return;\n    }\n\n    const response = await frappe.call({\n      method: 'nkt_operations.nkt_store_operations.features.inventory.physical_inventory.get_stock_snapshot',\n      args: {\n        warehouse: frm.doc.warehouse,\n        item_code: row.item_code\n      }\n    });\n    const snap = response.message || {};\n    frappe.model.set_value(cdt, cdn, 'item_name', snap.item_name || '');\n    frappe.model.set_value(cdt, cdn, 'stock_uom', snap.stock_uom || '');\n    frappe.model.set_value(cdt, cdn, 'system_qty_snapshot', snap.system_qty || 0);\n    frappe.model.set_value(cdt, cdn, 'valuation_rate_snapshot', snap.valuation_rate || 0);\n  }\n});\n\nfunction show_review_dialog(frm, flagDiscrepancy) {\n  const classificationOptions = [\n    'Counting Error',\n    'Unrecorded Release',\n    'Unrecorded Receipt',\n    'Damage / Spoilage',\n    'Handling Loss',\n    'Suspected Loss / Theft',\n    'System / Data Error',\n    'Timing Difference',\n    'Other'\n  ].join('\\n');\n\n  const dialog = new frappe.ui.Dialog({\n    title: flagDiscrepancy ? __('Flag Inventory Discrepancy') : __('Reconcile Inventory Adjustment'),\n    fields: [\n      {\n        fieldname: 'classification',\n        label: __('Accountability Classification'),\n        fieldtype: 'Select',\n        options: classificationOptions,\n        reqd: 1\n      },\n      {\n        fieldname: 'accountability_notes',\n        label: __('Accountability Notes'),\n        fieldtype: 'Small Text'\n      },\n      {\n        fieldname: 'review_notes',\n        label: __('Review Notes'),\n        fieldtype: 'Small Text',\n        reqd: flagDiscrepancy ? 1 : 0\n      },\n      {\n        fieldname: 'ack',\n        label: __('I understand this review does not alter the already-posted stock correction and does not create a payroll deduction.'),\n        fieldtype: 'Check',\n        reqd: 1\n      }\n    ],\n    primary_action_label: flagDiscrepancy ? __('Flag Discrepancy') : __('Reconcile'),\n    primary_action: async values => {\n      const method = flagDiscrepancy\n        ? 'nkt_operations.nkt_store_operations.features.setup_validation.internal.review.flag_admin_discrepancy'\n        : 'nkt_operations.nkt_store_operations.features.setup_validation.internal.review.complete_admin_review';\n\n      await frappe.call({\n        method,\n        args: {\n          name: frm.doc.name,\n          classification: values.classification,\n          accountability_notes: values.accountability_notes || '',\n          review_notes: values.review_notes || ''\n        },\n        freeze: true,\n        freeze_message: flagDiscrepancy\n          ? __('Recording discrepancy...')\n          : __('Completing Admin review...')\n      });\n      dialog.hide();\n      await frm.reload_doc();\n    }\n  });\n  dialog.show();\n}"


def _assert_admin():
    if frappe.session.user == "Administrator":
        return
    if not set(frappe.get_roles()).intersection(ADMIN_ROLES):
        frappe.throw(_("Only NKT Admin/Owner may review physical inventory adjustments."), frappe.PermissionError)


def _get_posted_adjustment(name):
    doc = frappe.get_doc(PARENT, name)
    if doc.docstatus != 1 or doc.adjustment_status != "Posted":
        frappe.throw(_("Only a posted physical inventory adjustment may be reviewed."))
    if not doc.stock_reconciliation:
        frappe.throw(_("Posted adjustment has no linked Stock Reconciliation."))
    if frappe.db.get_value("Stock Reconciliation", doc.stock_reconciliation, "docstatus") != 1:
        frappe.throw(_("The linked Stock Reconciliation is not submitted."))
    if doc.review_lock:
        frappe.throw(_("This Admin review is already reconciled and locked."))
    return doc


def _save_review(doc):
    previous = frappe.session.user
    try:
        # Admin/Owner remains the business reviewer. Administrator is used only
        # by the posting engine for the underlying Stock Reconciliation.
        doc.flags.ignore_permissions = False
        doc.save(ignore_permissions=False)
    finally:
        frappe.set_user(previous)
    return doc


@frappe.whitelist()
def start_admin_review(name):
    _assert_admin()
    doc = _get_posted_adjustment(name)
    if doc.review_status not in ("Pending Admin Review", "Discrepancy Flagged"):
        frappe.throw(_("Admin review cannot be started from status {0}.").format(doc.review_status))
    doc.review_status = "Under Review"
    doc.save()
    return {
        "name": doc.name,
        "review_status": doc.review_status,
        "stock_reconciliation": doc.stock_reconciliation,
    }


def _validate_classification(classification):
    classification = (classification or "").strip()
    if not classification or classification == "Unreviewed":
        frappe.throw(_("Accountability Classification is required."))
    allowed = {
        "Counting Error",
        "Unrecorded Release",
        "Unrecorded Receipt",
        "Damage / Spoilage",
        "Handling Loss",
        "Suspected Loss / Theft",
        "System / Data Error",
        "Timing Difference",
        "Other",
    }
    if classification not in allowed:
        frappe.throw(_("Invalid Accountability Classification."))
    return classification


@frappe.whitelist()
def complete_admin_review(name, classification, accountability_notes="", review_notes=""):
    _assert_admin()
    doc = _get_posted_adjustment(name)
    classification = _validate_classification(classification)

    doc.accountability_classification = classification
    doc.accountability_notes = (accountability_notes or "").strip()
    doc.review_notes = (review_notes or "").strip()
    doc.review_status = "Reconciled"
    doc.reviewed_by = frappe.session.user
    doc.reviewed_on = now_datetime()
    doc.review_lock = 1
    doc.save()

    return {
        "name": doc.name,
        "review_status": doc.review_status,
        "review_lock": int(doc.review_lock or 0),
        "accountability_classification": doc.accountability_classification,
        "reviewed_by": doc.reviewed_by,
        "reviewed_on": doc.reviewed_on,
    }


@frappe.whitelist()
def flag_admin_discrepancy(name, classification, accountability_notes="", review_notes=""):
    _assert_admin()
    doc = _get_posted_adjustment(name)
    classification = _validate_classification(classification)
    review_notes = (review_notes or "").strip()
    if not review_notes:
        frappe.throw(_("Review Notes are required when flagging a discrepancy."))

    doc.accountability_classification = classification
    doc.accountability_notes = (accountability_notes or "").strip()
    doc.review_notes = review_notes
    doc.review_status = "Discrepancy Flagged"
    doc.reviewed_by = frappe.session.user
    doc.reviewed_on = now_datetime()
    doc.review_lock = 0
    doc.save()

    return {
        "name": doc.name,
        "review_status": doc.review_status,
        "review_lock": int(doc.review_lock or 0),
        "accountability_classification": doc.accountability_classification,
        "reviewed_by": doc.reviewed_by,
        "reviewed_on": doc.reviewed_on,
    }


def _ensure_client_script():
    values = {
        "dt": PARENT,
        "view": "Form",
        "enabled": 1,
        "script": CLIENT_JS,
    }
    if frappe.db.exists("Client Script", CLIENT_SCRIPT):
        doc = frappe.get_doc("Client Script", CLIENT_SCRIPT)
        doc.update(values)
        doc.flags.ignore_permissions = True
        doc.save(ignore_permissions=True)
    else:
        doc = frappe.get_doc({"doctype": "Client Script", "name": CLIENT_SCRIPT, **values})
        doc.flags.ignore_permissions = True
        doc.insert(ignore_permissions=True)


@frappe.whitelist()
def install():
    if frappe.session.user != "Administrator":
        _assert_admin()
    _ensure_client_script()
    frappe.clear_cache(doctype=PARENT)
    return verify()


@frappe.whitelist()
def verify():
    from nkt_operations.nkt_store_operations.features.inventory import physical_inventory as engine
    from nkt_operations.nkt_store_operations.doctype.nkt_physical_inventory_adjustment import (
        nkt_physical_inventory_adjustment as controller,
    )

    checks = {}
    errors = []

    engine_source = inspect.getsource(engine)
    controller_source = inspect.getsource(controller)

    checks["engine_version_production"] = getattr(engine, "VERSION", "") == VERSION
    checks["runtime_submit_gate_removed"] = (
        "nkt_c8_runtime_test" not in controller_source
        and "Production posting remains disabled until C8B passes" not in controller_source
    )
    checks["production_controller_executes_adjustment"] = "execute_adjustment(self)" in controller_source
    checks["server_owned_sr_admin_context_retained"] = all(
        token in engine_source
        for token in (
            'operator_user = frappe.session.user',
            'frappe.set_user("Administrator")',
            'sr.insert(ignore_permissions=True)',
            'sr.submit()',
            'frappe.set_user(operator_user)',
        )
    )
    checks["operator_direct_sr_permission_still_absent"] = bool(
        engine.verify().get("checks", {}).get("no_direct_nkt_stock_reconciliation_submit_permission")
    )
    checks["current_date_guard_retained"] = "Do not backdate a physical count correction" in engine_source
    checks["force_refresh_before_post_retained"] = "force_refresh=True" in engine_source
    checks["bin_exactness_guard_retained"] = "Transaction aborted" in engine_source
    checks["pending_review_after_post_retained"] = 'doc.review_status = "Pending Admin Review"' in engine_source
    checks["no_payroll_or_fake_sale_integration"] = bool(
        engine.verify().get("checks", {}).get("no_payroll_or_fake_sale_integration")
    )

    meta = frappe.get_meta(PARENT)
    review_field = meta.get_field("review_status")
    checks["review_status_update_after_submit_supported"] = bool(
        review_field and int(review_field.allow_on_submit or 0) == 1
    )

    script = frappe.db.get_value(
        "Client Script", CLIENT_SCRIPT, ["dt", "enabled", "script"], as_dict=True
    )
    checks["production_client_script_enabled"] = bool(
        script and script.dt == PARENT and int(script.enabled or 0) == 1
    )
    script_text = (script.script or "") if script else ""
    for token in (
        "Refresh Count Preview",
        "Pending Admin Review",
        "Start Admin Review",
        "Flag Discrepancy",
        "Reconcile",
        "does not create a payroll deduction",
        "Physical Quantity Counted",
    ):
        key = "client_script::" + token
        checks[key] = token in script_text

    # Inspect only executable Admin-review paths. R1 inspected this whole
    # module, which included the forbidden operation text inside the verifier
    # itself and therefore falsely failed its own check.
    review_functions = [
        _assert_admin,
        _get_posted_adjustment,
        _save_review,
        start_admin_review,
        _validate_classification,
        complete_admin_review,
        flag_admin_discrepancy,
    ]
    review_source = "\n".join(inspect.getsource(fn) for fn in review_functions)

    checks["review_admin_authority_present"] = "Only NKT Admin/Owner may review" in review_source
    checks["reconciled_review_locks"] = 'doc.review_lock = 1' in review_source
    checks["flagged_review_stays_open"] = 'doc.review_lock = 0' in review_source

    forbidden_stock_mutations = (
        'frappe.new_doc("Stock Reconciliation")',
        'frappe.get_doc("Stock Reconciliation"',
        'frappe.db.set_value("Stock Reconciliation"',
        'frappe.delete_doc("Stock Reconciliation"',
        '.cancel()',
    )
    checks["review_does_not_modify_stock_reconciliation"] = not any(
        token in review_source for token in forbidden_stock_mutations
    )
    checks["review_only_validates_linked_sr_state"] = (
        'frappe.db.get_value("Stock Reconciliation", doc.stock_reconciliation, "docstatus")' in review_source
        and 'doc.stock_reconciliation' in review_source
    )
    checks["no_c8_records_created_by_install"] = frappe.db.count(PARENT) == 0

    for key, value in checks.items():
        if not value:
            errors.append(key)

    return {
        "version": VERSION,
        "verify_version": VERIFY_VERSION,
        "mode": "PRODUCTION POSTING + SEPARATE ADMIN REVIEW - VERIFY R2",
        "checks": checks,
        "errors": errors,
        "passed": all(checks.values()) and not errors,
    }
