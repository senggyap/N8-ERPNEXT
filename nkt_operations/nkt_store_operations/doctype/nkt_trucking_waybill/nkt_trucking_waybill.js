frappe.ui.form.on('NKT Trucking Waybill Item', {
  qty(frm, cdt, cdn) { nkt_waybill_recalc(frm, cdt, cdn); },
  unit_price(frm, cdt, cdn) { nkt_waybill_recalc(frm, cdt, cdn); }
});
function nkt_waybill_recalc(frm, cdt, cdn) {
  const row = locals[cdt][cdn];
  frappe.model.set_value(cdt, cdn, 'line_total', (flt(row.qty) || 0) * (flt(row.unit_price) || 0));
  let total = 0;
  (frm.doc.items || []).forEach(r => total += flt(r.line_total) || 0);
  frm.set_value('grand_total', total);
}
