/* NKT CURRENT CLIENT SCRIPT — NKT Return Exchange Declaration — Form
 * Consolidated from the accepted live site on 2026-08-27.
 * Source order exactly preserves Frappe Client Script creation order.
 * Edit this file; do not create phase/recovery Client Script records.
 */

/* ===== SOURCE: NKT C7.13C Return Exchange Reversal Launch ===== */
frappe.ui.form.on('NKT Return Exchange Declaration', {
  refresh(frm) {
    const roles = frappe.user_roles || [];
    const isAdminOwner =
      frappe.session.user === 'Administrator' ||
      roles.includes('NKT OWNER') ||
      roles.includes('NKT ADMINISTRATOR');

    const isReversed = frm.doc.custom_nkt_reversal_status === 'Reversed';

    // Frontline sees only the simple Reversal Status when the record was
    // actually reversed. Detailed reversal audit links stay Admin/Owner-only.
    frm.toggle_display('custom_nkt_reversal_section', isAdminOwner || isReversed);
    frm.toggle_display('custom_nkt_reversal_status', isAdminOwner || isReversed);
    [
      'custom_nkt_reversal_record',
      'custom_nkt_reversed_on',
      'custom_nkt_reversed_by',
      'custom_nkt_reversal_reason'
    ].forEach(field => frm.toggle_display(field, isAdminOwner));

    if (
      !isAdminOwner ||
      frm.doc.docstatus !== 1 ||
      frm.doc.posting_status !== 'Posted' ||
      isReversed
    ) {
      return;
    }

    frm.add_custom_button(__('Reverse / Correct'), async () => {
      const previewCall = await frappe.call({
        method: 'nkt_operations.nkt_store_operations.features.returns.reversal.preview_reversal',
        args: { declaration_name: frm.doc.name },
        freeze: true,
        freeze_message: __('Checking live reversal dependencies...')
      });
      const preview = previewCall.message || {};
      const blockers = preview.blockers || [];
      const warnings = preview.warnings || [];
      const effects = preview.effects || [];

      if (blockers.length) {
        frappe.msgprint({
          title: __('Reversal Blocked'),
          indicator: 'red',
          message:
            '<b>' + __('This Return/Exchange cannot be reversed yet.') + '</b><br><br>' +
            blockers.map(x => '• ' + x).join('<br>') +
            (warnings.length
              ? '<br><br><b>' + __('Warnings') + '</b><br>' +
                warnings.map(x => '• ' + x).join('<br>')
              : '')
        });
        return;
      }

      const moneyEffects = effects.filter(x =>
        ['Direct Return/Exchange Money Movement', 'Generated Exchange Sale Payment Movement']
          .includes(x.effect_type)
      );
      const stockEffects = effects.filter(x =>
        ['Inventory Classification Correction',
         'Customer Return Inventory Receipt',
         'Generated Exchange Order Fulfillment']
          .includes(x.effect_type)
      );

      const summary = [
        '<b>' + __('Ready for controlled reversal') + '</b>',
        __('Money effects: {0}', [moneyEffects.length]),
        __('Inventory effects: {0}', [stockEffects.length]),
        '',
        __('Historical cashier movements will remain posted. Any money correction will be posted in a CURRENT-DATE open cashier shift.'),
        __('After reversal, Cashier and Encoder must independently re-enter the corrected current-date Return/Exchange.')
      ].join('<br>');

      frappe.confirm(summary, async () => {
        const draftCall = await frappe.call({
          method: 'nkt_operations.nkt_store_operations.features.returns.reversal.make_reversal_draft',
          args: { declaration_name: frm.doc.name },
          freeze: true,
          freeze_message: __('Preparing controlled reversal...')
        });
        const draft = draftCall.message || {};
        if (!draft.name) {
          frappe.throw(__('The reversal draft could not be created.'));
        }
        frappe.set_route('Form', 'NKT Return Exchange Reversal', draft.name);
      });
    }, __('Actions'));
  }
});
/* ===== END SOURCE: NKT C7.13C Return Exchange Reversal Launch ===== */
