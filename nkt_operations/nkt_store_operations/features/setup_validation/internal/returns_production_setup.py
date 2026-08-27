from __future__ import annotations

import inspect

import frappe
from frappe import _

VERSION = "V2.0C.7.13C.1-PRODUCTION"
VERIFY_VERSION = "V2.0C.7.13C.2-VERIFY-R2"
DECL = "NKT Return Exchange Declaration"
REVERSAL = "NKT Return Exchange Reversal"
LAUNCH_SCRIPT = "NKT C7.13C Return Exchange Reversal Launch"
FORM_SCRIPT = "NKT C7.13C Return Exchange Reversal Form"

RX_JS = "frappe.ui.form.on('NKT Return Exchange Declaration', {\n  refresh(frm) {\n    const roles = frappe.user_roles || [];\n    const isAdminOwner =\n      frappe.session.user === 'Administrator' ||\n      roles.includes('NKT OWNER') ||\n      roles.includes('NKT ADMINISTRATOR');\n\n    const isReversed = frm.doc.custom_nkt_reversal_status === 'Reversed';\n\n    // Frontline sees only the simple Reversal Status when the record was\n    // actually reversed. Detailed reversal audit links stay Admin/Owner-only.\n    frm.toggle_display('custom_nkt_reversal_section', isAdminOwner || isReversed);\n    frm.toggle_display('custom_nkt_reversal_status', isAdminOwner || isReversed);\n    [\n      'custom_nkt_reversal_record',\n      'custom_nkt_reversed_on',\n      'custom_nkt_reversed_by',\n      'custom_nkt_reversal_reason'\n    ].forEach(field => frm.toggle_display(field, isAdminOwner));\n\n    if (\n      !isAdminOwner ||\n      frm.doc.docstatus !== 1 ||\n      frm.doc.posting_status !== 'Posted' ||\n      isReversed\n    ) {\n      return;\n    }\n\n    frm.add_custom_button(__('Reverse / Correct'), async () => {\n      const previewCall = await frappe.call({\n        method: 'nkt_operations.nkt_store_operations.features.returns.reversal.preview_reversal',\n        args: { declaration_name: frm.doc.name },\n        freeze: true,\n        freeze_message: __('Checking live reversal dependencies...')\n      });\n      const preview = previewCall.message || {};\n      const blockers = preview.blockers || [];\n      const warnings = preview.warnings || [];\n      const effects = preview.effects || [];\n\n      if (blockers.length) {\n        frappe.msgprint({\n          title: __('Reversal Blocked'),\n          indicator: 'red',\n          message:\n            '<b>' + __('This Return/Exchange cannot be reversed yet.') + '</b><br><br>' +\n            blockers.map(x => '• ' + x).join('<br>') +\n            (warnings.length\n              ? '<br><br><b>' + __('Warnings') + '</b><br>' +\n                warnings.map(x => '• ' + x).join('<br>')\n              : '')\n        });\n        return;\n      }\n\n      const moneyEffects = effects.filter(x =>\n        ['Direct Return/Exchange Money Movement', 'Generated Exchange Sale Payment Movement']\n          .includes(x.effect_type)\n      );\n      const stockEffects = effects.filter(x =>\n        ['Inventory Classification Correction',\n         'Customer Return Inventory Receipt',\n         'Generated Exchange Order Fulfillment']\n          .includes(x.effect_type)\n      );\n\n      const summary = [\n        '<b>' + __('Ready for controlled reversal') + '</b>',\n        __('Money effects: {0}', [moneyEffects.length]),\n        __('Inventory effects: {0}', [stockEffects.length]),\n        '',\n        __('Historical cashier movements will remain posted. Any money correction will be posted in a CURRENT-DATE open cashier shift.'),\n        __('After reversal, Cashier and Encoder must independently re-enter the corrected current-date Return/Exchange.')\n      ].join('<br>');\n\n      frappe.confirm(summary, async () => {\n        const draftCall = await frappe.call({\n          method: 'nkt_operations.nkt_store_operations.features.returns.reversal.make_reversal_draft',\n          args: { declaration_name: frm.doc.name },\n          freeze: true,\n          freeze_message: __('Preparing controlled reversal...')\n        });\n        const draft = draftCall.message || {};\n        if (!draft.name) {\n          frappe.throw(__('The reversal draft could not be created.'));\n        }\n        frappe.set_route('Form', 'NKT Return Exchange Reversal', draft.name);\n      });\n    }, __('Actions'));\n  }\n});"
REVERSAL_JS = "frappe.ui.form.on('NKT Return Exchange Reversal', {\n  refresh(frm) {\n    const roles = frappe.user_roles || [];\n    const isAdminOwner =\n      frappe.session.user === 'Administrator' ||\n      roles.includes('NKT OWNER') ||\n      roles.includes('NKT ADMINISTRATOR');\n\n    if (!isAdminOwner) {\n      return;\n    }\n\n    const moneyTypes = [\n      'Direct Return/Exchange Money Movement',\n      'Generated Exchange Sale Payment Movement'\n    ];\n    const stockTypes = [\n      'Inventory Classification Correction',\n      'Customer Return Inventory Receipt',\n      'Generated Exchange Order Fulfillment'\n    ];\n    const moneyRows = (frm.doc.effects || []).filter(r => moneyTypes.includes(r.effect_type));\n    const stockRows = (frm.doc.effects || []).filter(r => stockTypes.includes(r.effect_type));\n\n    frm.toggle_reqd('reversal_cashier', moneyRows.length > 0);\n\n    if (frm.doc.docstatus === 0) {\n      frm.dashboard.set_headline(\n        __('Review the live effects below. Submit is allowed only when all blockers are clear and the required physical/payment confirmations are completed.')\n      );\n\n      frm.add_custom_button(__('Refresh Live Preview'), async () => {\n        await frm.save();\n        await frm.reload_doc();\n      }, __('Actions'));\n\n      frm.add_custom_button(__('Review Requirements'), () => {\n        const referenceMethods = ['GCash', 'Maya', 'Credit Card', 'Bank Transfer', 'Online', 'Check'];\n        const needsReferences = moneyRows.filter(r => referenceMethods.includes(r.payment_method));\n        let lines = [\n          __('A specific reversal reason is required before Submit.'),\n          moneyRows.length\n            ? __('Money: select the cashier handling the correction, confirm physical/external money reality, and use a CURRENT-DATE open shift.')\n            : __('Money: no reversal money effect.'),\n          stockRows.length\n            ? __('Inventory: confirm that the inventory correction reflects physical stock reality.')\n            : __('Inventory: no stock effect.')\n        ];\n        if (needsReferences.length) {\n          lines.push(__('Reversal / Void Reference is required for:'));\n          needsReferences.forEach(r => {\n            lines.push('• ' + (r.payment_method || '') + ' — ' + (r.original_document || ''));\n          });\n        }\n        frappe.msgprint({\n          title: __('Controlled Reversal Requirements'),\n          message: lines.join('<br>')\n        });\n      }, __('Actions'));\n    }\n  },\n\n  before_submit(frm) {\n    const reason = (frm.doc.reversal_reason || '').trim();\n    if (!reason) {\n      frappe.throw(__('Reversal Reason is required before Submit.'));\n    }\n\n    const moneyTypes = [\n      'Direct Return/Exchange Money Movement',\n      'Generated Exchange Sale Payment Movement'\n    ];\n    const stockTypes = [\n      'Inventory Classification Correction',\n      'Customer Return Inventory Receipt',\n      'Generated Exchange Order Fulfillment'\n    ];\n    const referenceMethods = ['GCash', 'Maya', 'Credit Card', 'Bank Transfer', 'Online', 'Check'];\n\n    const moneyRows = (frm.doc.effects || []).filter(r => moneyTypes.includes(r.effect_type));\n    const stockRows = (frm.doc.effects || []).filter(r => stockTypes.includes(r.effect_type));\n\n    if (moneyRows.length) {\n      if (!frm.doc.reversal_cashier) {\n        frappe.throw(__('Cashier Handling Money Reversal is required.'));\n      }\n      if (!frm.doc.money_correction_confirmed) {\n        frappe.throw(__('Confirm that the money correction reflects physical/external payment reality.'));\n      }\n      moneyRows.forEach(r => {\n        if (referenceMethods.includes(r.payment_method) && !(r.reversal_reference || '').trim()) {\n          frappe.throw(\n            __('Reversal / Void Reference is required for {0} movement {1}.',\n              [r.payment_method, r.original_document])\n          );\n        }\n      });\n    }\n\n    if (stockRows.length && !frm.doc.inventory_correction_confirmed) {\n      frappe.throw(__('Confirm that the inventory correction reflects physical stock reality.'));\n    }\n  },\n\n  on_submit(frm) {\n    frappe.msgprint({\n      title: __('Return/Exchange Reversed'),\n      indicator: 'green',\n      message:\n        __('The original Return/Exchange was reversed through controlled current-period corrections.') +\n        '<br><br><b>' +\n        __('Cashier and Encoder must now independently re-enter the corrected Return/Exchange using the current business date.') +\n        '</b>'\n    });\n  }\n});"


def _ensure_client_script(name, dt, script):
    values = {"dt": dt, "view": "Form", "enabled": 1, "script": script}
    if frappe.db.exists("Client Script", name):
        doc = frappe.get_doc("Client Script", name)
        doc.update(values)
        doc.flags.ignore_permissions = True
        doc.save(ignore_permissions=True)
    else:
        doc = frappe.get_doc({"doctype": "Client Script", "name": name, **values})
        doc.flags.ignore_permissions = True
        doc.insert(ignore_permissions=True)
    return name


@frappe.whitelist()
def install():
    if frappe.session.user != "Administrator":
        roles = set(frappe.get_roles())
        if not roles.intersection({"NKT OWNER", "NKT ADMINISTRATOR"}):
            frappe.throw(_("Only NKT Admin/Owner may install C7.13C."), frappe.PermissionError)

    _ensure_client_script(LAUNCH_SCRIPT, DECL, RX_JS)
    _ensure_client_script(FORM_SCRIPT, REVERSAL, REVERSAL_JS)

    frappe.clear_cache(doctype=DECL)
    frappe.clear_cache(doctype=REVERSAL)
    return verify()


@frappe.whitelist()
def verify():
    errors = []
    checks = {}

    from nkt_operations.nkt_store_operations.features.returns import reversal as engine
    from nkt_operations.nkt_store_operations.doctype.nkt_return_exchange_reversal import nkt_return_exchange_reversal as controller

    engine_src = inspect.getsource(engine)
    controller_src = inspect.getsource(controller)

    checks["engine_version_production"] = getattr(engine, "VERSION", "") == VERSION
    checks["production_authority_admin_owner_only"] = set(engine.AUTHORIZED_ROLES) == {
        "NKT ADMINISTRATOR", "NKT OWNER"
    }
    checks["runtime_submit_gate_removed"] = (
        "nkt_c7_13_runtime_test" not in controller_src
        and "Production Submit remains disabled" not in controller_src
    )
    checks["server_submit_validation_retained"] = "validate_reversal_document(self, for_submit=True)" in controller_src
    checks["server_current_date_shift_guard_retained"] = all(
        token in engine_src
        for token in (
            "shift_date = getdate(shift.shift_start)",
            "today = getdate(nowdate())",
            "if shift_date != today:",
            "Close the previous-date shift and open a ",
            "current-date Cashier Shift before posting this reversal.",
        )
    )
    checks["server_reference_guard_retained"] = "Reversal Reference is required" in engine_src
    checks["server_inventory_confirmation_retained"] = "Confirm that the inventory correction reflects the physical stock reality" in engine_src
    checks["draft_reason_blank_allowed"] = 'doc.reversal_reason = ""' in engine_src
    checks["submit_reason_required"] = "Reversal Reason is required before Submit" in engine_src
    checks["stable_active_draft_reuse"] = (
        "Stable UI idempotency" in engine_src
        and '"docstatus": 0' in engine_src
    )

    meta = frappe.get_meta(REVERSAL)
    reason_field = meta.get_field("reversal_reason")
    checks["reason_field_not_required_for_draft"] = bool(reason_field and not reason_field.reqd)

    permission_roles = {
        row.role for row in (meta.permissions or [])
        if row.read or row.write or row.create or row.submit
    }
    checks["doctype_permissions_admin_owner_only"] = permission_roles == {
        "NKT OWNER", "NKT ADMINISTRATOR"
    }

    for name, dt, needles in (
        (
            LAUNCH_SCRIPT,
            DECL,
            [
                "Reverse / Correct",
                "preview_reversal",
                "make_reversal_draft",
                "Reversal Blocked",
                "independently re-enter",
                "custom_nkt_reversal_status",
                "NKT OWNER",
                "NKT ADMINISTRATOR",
            ],
        ),
        (
            FORM_SCRIPT,
            REVERSAL,
            [
                "Refresh Live Preview",
                "Review Requirements",
                "Reversal Reason is required before Submit",
                "Reversal / Void Reference",
                "money_correction_confirmed",
                "inventory_correction_confirmed",
                "CURRENT-DATE open shift",
                "independently re-enter",
            ],
        ),
    ):
        row = frappe.db.get_value(
            "Client Script", name, ["dt", "enabled", "script"], as_dict=True
        )
        ok = bool(row and row.dt == dt and int(row.enabled or 0) == 1)
        checks["client_script_enabled::" + name] = ok
        if not ok:
            errors.append(f"Missing/enabled Client Script: {name}")
            continue
        folded = (row.script or "").casefold()
        for needle in needles:
            if needle.casefold() not in folded:
                errors.append(f"{name} missing {needle}")

    preview = engine.verify()
    checks["c7_13_preview_verify_passed"] = bool(preview.get("passed"))
    checks["independent_reentry_rule_retained"] = bool(
        preview.get("checks", {}).get("correction_preserves_independent_reentry")
    )

    # The production verifier is read-only with respect to operational records:
    # it must not create/submit/cancel an actual reversal.
    reversal_count = frappe.db.count(REVERSAL)
    checks["no_reversal_record_created_by_verify"] = reversal_count == 0

    return {
        "version": VERSION,
        "verify_version": VERIFY_VERSION,
        "mode": "PRODUCTION UI / SUBMIT UNLOCK - READ-ONLY VERIFICATION R2",
        "checks": checks,
        "preview_summary": preview.get("summary"),
        "errors": errors,
        "passed": all(checks.values()) and not errors,
    }
