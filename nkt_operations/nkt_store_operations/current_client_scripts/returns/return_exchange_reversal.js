/* NKT CURRENT CLIENT SCRIPT — NKT Return Exchange Reversal — Form
 * Consolidated from the accepted live site on 2026-08-27.
 * Source order exactly preserves Frappe Client Script creation order.
 * Edit this file; do not create phase/recovery Client Script records.
 */

/* ===== SOURCE: NKT C7.13C Return Exchange Reversal Form ===== */
frappe.ui.form.on('NKT Return Exchange Reversal', {
  refresh(frm) {
    const roles = frappe.user_roles || [];
    const isAdminOwner =
      frappe.session.user === 'Administrator' ||
      roles.includes('NKT OWNER') ||
      roles.includes('NKT ADMINISTRATOR');

    if (!isAdminOwner) {
      return;
    }

    const moneyTypes = [
      'Direct Return/Exchange Money Movement',
      'Generated Exchange Sale Payment Movement'
    ];
    const stockTypes = [
      'Inventory Classification Correction',
      'Customer Return Inventory Receipt',
      'Generated Exchange Order Fulfillment'
    ];
    const moneyRows = (frm.doc.effects || []).filter(r => moneyTypes.includes(r.effect_type));
    const stockRows = (frm.doc.effects || []).filter(r => stockTypes.includes(r.effect_type));

    frm.toggle_reqd('reversal_cashier', moneyRows.length > 0);

    if (frm.doc.docstatus === 0) {
      frm.dashboard.set_headline(
        __('Review the live effects below. Submit is allowed only when all blockers are clear and the required physical/payment confirmations are completed.')
      );

      frm.add_custom_button(__('Refresh Live Preview'), async () => {
        await frm.save();
        await frm.reload_doc();
      }, __('Actions'));

      frm.add_custom_button(__('Review Requirements'), () => {
        const referenceMethods = ['GCash', 'Maya', 'Card', 'Bank Transfer', 'Online', 'Check'];
        const needsReferences = moneyRows.filter(r => referenceMethods.includes(r.payment_method));
        let lines = [
          __('A specific reversal reason is required before Submit.'),
          moneyRows.length
            ? __('Money: select the cashier handling the correction, confirm physical/external money reality, and use a CURRENT-DATE open shift.')
            : __('Money: no reversal money effect.'),
          stockRows.length
            ? __('Inventory: confirm that the inventory correction reflects physical stock reality.')
            : __('Inventory: no stock effect.')
        ];
        if (needsReferences.length) {
          lines.push(__('Reversal / Void Reference is required for:'));
          needsReferences.forEach(r => {
            lines.push('• ' + (r.payment_method || '') + ' — ' + (r.original_document || ''));
          });
        }
        frappe.msgprint({
          title: __('Controlled Reversal Requirements'),
          message: lines.join('<br>')
        });
      }, __('Actions'));
    }
  },

  before_submit(frm) {
    const reason = (frm.doc.reversal_reason || '').trim();
    if (!reason) {
      frappe.throw(__('Reversal Reason is required before Submit.'));
    }

    const moneyTypes = [
      'Direct Return/Exchange Money Movement',
      'Generated Exchange Sale Payment Movement'
    ];
    const stockTypes = [
      'Inventory Classification Correction',
      'Customer Return Inventory Receipt',
      'Generated Exchange Order Fulfillment'
    ];
    const referenceMethods = ['GCash', 'Maya', 'Card', 'Bank Transfer', 'Online', 'Check'];

    const moneyRows = (frm.doc.effects || []).filter(r => moneyTypes.includes(r.effect_type));
    const stockRows = (frm.doc.effects || []).filter(r => stockTypes.includes(r.effect_type));

    if (moneyRows.length) {
      if (!frm.doc.reversal_cashier) {
        frappe.throw(__('Cashier Handling Money Reversal is required.'));
      }
      if (!frm.doc.money_correction_confirmed) {
        frappe.throw(__('Confirm that the money correction reflects physical/external payment reality.'));
      }
      moneyRows.forEach(r => {
        if (referenceMethods.includes(r.payment_method) && !(r.reversal_reference || '').trim()) {
          frappe.throw(
            __('Reversal / Void Reference is required for {0} movement {1}.',
              [r.payment_method, r.original_document])
          );
        }
      });
    }

    if (stockRows.length && !frm.doc.inventory_correction_confirmed) {
      frappe.throw(__('Confirm that the inventory correction reflects physical stock reality.'));
    }
  },

  on_submit(frm) {
    frappe.msgprint({
      title: __('Return/Exchange Reversed'),
      indicator: 'green',
      message:
        __('The original Return/Exchange was reversed through controlled current-period corrections.') +
        '<br><br><b>' +
        __('Cashier and Encoder must now independently re-enter the corrected Return/Exchange using the current business date.') +
        '</b>'
    });
  }
});
/* ===== END SOURCE: NKT C7.13C Return Exchange Reversal Form ===== */
