frappe.ui.form.on('NKT Trucking Customer Collection', {
  refresh(frm) {
    if (frm.is_new()) return;
    if (frm.doc.status === 'Draft') {
      frm.add_custom_button(__('Post Collection'), async () => {
        await frm.call('post_collection');
        await frm.reload_doc();
      });
    }
    if (frm.doc.status === 'Posted') {
      frm.add_custom_button(__('Reverse Collection'), () => {
        frappe.prompt([{fieldname:'reason',fieldtype:'Small Text',label:'Reversal Reason',reqd:1}], async (v) => {
          await frm.call('reverse_collection', {reason:v.reason});
          await frm.reload_doc();
        }, __('Reverse Trucking Collection'));
      });
    }
  }
});

frappe.ui.form.on('NKT Trucking Customer Collection Payment', {
  payment_method(frm, cdt, cdn) { recalc_payment(frm, cdt, cdn); },
  base_amount(frm, cdt, cdn) { recalc_payment(frm, cdt, cdn); }
});
function recalc_payment(frm, cdt, cdn) {
  const r=locals[cdt][cdn];
  const base=flt(r.base_amount)||0;
  const surcharge=(r.payment_method==='Card') ? base*0.02 : 0;
  frappe.model.set_value(cdt,cdn,'surcharge_amount',surcharge);
  frappe.model.set_value(cdt,cdn,'total_received',base+surcharge);
}
