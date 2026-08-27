frappe.ui.form.on('NKT Trucking Customer SOA', {
  refresh(frm) {
    frm.set_df_property("print_layout", "read_only", 1);
    if (frm.is_new()) return;
    if (frm.doc.status === 'Draft') {
      frm.add_custom_button(__('Pull Unbilled Trips / Charges'), async () => {
        const r = await frm.call('pull_unbilled_items');
        await frm.reload_doc();
        if (r && r.message) {
          const companions = r.message.companion_soas || [];
          let msg = __('Added {0} primary-haul line(s) and {1} backload/additional-charge line(s).', [r.message.primary_haul_lines_added || 0, r.message.backload_additional_charge_lines_added || 0]);
          if (companions.length) msg += '<br><b>' + __('Automatic split SOA created:') + '</b> ' + companions.join(', ');
          frappe.msgprint(msg);
        }
      });
    }
    if (['Draft', 'Prepared'].includes(frm.doc.status)) {
      frm.add_custom_button(__('Apply Defaults to Blank Lines'), async () => {
        await frm.call('apply_defaults');
        await frm.reload_doc();
      });
    }
    if (frm.doc.status === 'Draft') {
      frm.add_custom_button(__('Mark Prepared'), async () => {
        await frm.call('mark_prepared');
        await frm.reload_doc();
      });
    }
    if (frm.doc.status === 'Prepared') {
      frm.add_custom_button(__('Finalize SOA'), async () => {
        await frm.call('finalize_statement');
        await frm.reload_doc();
      });
    }
  }
});

frappe.ui.form.on('NKT Trucking Customer SOA Line', {
  qty(frm, cdt, cdn) { nkt_trucking_soa_recalc(frm, cdt, cdn); },
  rate(frm, cdt, cdn) { nkt_trucking_soa_recalc(frm, cdt, cdn); },
  manual_amount_override(frm, cdt, cdn) { nkt_trucking_soa_recalc(frm, cdt, cdn); },
  amount(frm) { nkt_trucking_soa_total(frm); }
});
function nkt_trucking_soa_recalc(frm, cdt, cdn) {
  const row = locals[cdt][cdn];
  if (!row.manual_amount_override) frappe.model.set_value(cdt, cdn, 'amount', (flt(row.qty) || 0) * (flt(row.rate) || 0));
  nkt_trucking_soa_total(frm);
}
function nkt_trucking_soa_total(frm) {
  let total = 0; (frm.doc.lines || []).forEach(r => total += flt(r.amount) || 0); frm.set_value('grand_total', total);
}
