/* NKT CURRENT CLIENT SCRIPT — NKT Cash Drawer Adjustment — Form
 * Consolidated from the accepted live site on 2026-08-27.
 * Source order exactly preserves Frappe Client Script creation order.
 * Edit this file; do not create phase/recovery Client Script records.
 */

/* ===== SOURCE: NKT Cash Drawer Adjustment V1.9 ===== */

// NKT Shift Close V1.9.6 — C15C.10F FINAL CASH DRAWER FRONT DOOR
// One browser business action. Server runtime owns authority/routing.
frappe.ui.form.on('NKT Cash Drawer Adjustment', {
  setup(frm) {
    const roles = frappe.user_roles || [];
    const isAdmin = frappe.session.user === 'Administrator' || ['System Manager', 'NKT OWNER', 'NKT ADMINISTRATOR'].some(r => roles.includes(r));
    frm.set_query('cashier_shift', () => {
      const filters = {status: 'Open', docstatus: 0};
      if (!isAdmin) filters.cashier = frappe.session.user;
      return {filters};
    });
  },
  onload(frm) {
    nkt_apply_fast_adjustment_layout(frm);
    nkt_load_available_cash(frm);
    nkt_install_cash_drawer_keys(frm);
  },
  refresh(frm) {
    nkt_apply_fast_adjustment_layout(frm);
    nkt_recalculate_deposit(frm);
    nkt_load_available_cash(frm);
    nkt_install_cash_drawer_keys(frm);

    if (frm.is_new() && frm.doc.docstatus === 0) {
      frm.disable_save();
      frm.page.clear_primary_action();
      frm.page.set_primary_action(__('F12 Record Adjustment'), () => nkt_record_cash_drawer_frontdoor(frm, false));
      frm.add_custom_button(__('F10 Record & Open Voucher'), () => nkt_record_cash_drawer_frontdoor(frm, true));
    }

    const roles = frappe.user_roles || [];
    const isAdmin = frappe.session.user === 'Administrator' || ['System Manager', 'NKT OWNER', 'NKT ADMINISTRATOR'].some(r => roles.includes(r));
    if (frm.doc.docstatus === 1 && frm.doc.status === 'Posted' && isAdmin) {
      frm.add_custom_button(__('Reverse Adjustment'), () => {
        frappe.prompt(
          [{fieldname:'reason', label:'Reversal Reason', fieldtype:'Small Text', reqd:1}],
          values => frappe.call({
            method:'nkt_operations.nkt_store_operations.features.cashier.shift_engine.reverse_cash_adjustment',
            args:{adjustment_name:frm.doc.name, reason:values.reason}, freeze:true, callback:() => frm.reload_doc()
          }),
          __('Controlled Reversal'), __('Reverse')
        );
      });
    }
  },
  cashier_shift(frm) {
    if (!frm.doc.cashier_shift) { frm.__nkt_available_cash = null; return; }
    frappe.db.get_value('NKT Cashier Shift', frm.doc.cashier_shift, ['company', 'settlement_location', 'cashier']).then(r => {
      const v = r.message || {};
      frm.set_value('company', v.company);
      frm.set_value('settlement_location', v.settlement_location);
      frm.set_value('cashier', v.cashier);
    });
    nkt_load_available_cash(frm);
  },
  adjustment_type(frm) {
    nkt_apply_fast_adjustment_layout(frm);
    nkt_recalculate_deposit(frm);
    nkt_load_available_cash(frm);
  },
  validate(frm) {
    nkt_validate_cash_drawer_frontdoor(frm);
  }
});

const nkt_deposit_denominations = {
  deposit_bill_1000_qty:1000, deposit_bill_500_qty:500, deposit_bill_200_qty:200,
  deposit_bill_100_qty:100, deposit_bill_50_qty:50, deposit_bill_20_qty:20,
  deposit_coin_20_qty:20, deposit_coin_10_qty:10, deposit_coin_5_qty:5,
  deposit_coin_1_qty:1, deposit_coin_025_qty:0.25
};

function nkt_bound_device_id() {
  try { return String(window.localStorage.getItem('nkt_device_id') || '').trim(); }
  catch (_) { return ''; }
}

function nkt_cash_drawer_uuid() {
  if (window.crypto && window.crypto.randomUUID) return window.crypto.randomUUID();
  return 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, c => {
    const r = Math.random() * 16 | 0;
    return (c === 'x' ? r : (r & 0x3 | 0x8)).toString(16);
  });
}

function nkt_install_cash_drawer_keys(frm) {
  const ns = '.nktCashDrawerC15C10F';
  $(document).off(`keydown${ns}`).on(`keydown${ns}`, e => {
    if ($('.modal.show').length || !cur_frm || cur_frm.doctype !== 'NKT Cash Drawer Adjustment') return;
    if (cur_frm.doc.docstatus !== 0 || !cur_frm.is_new()) return;
    if (e.key === 'F10') {
      e.preventDefault();
      e.stopImmediatePropagation();
      nkt_record_cash_drawer_frontdoor(cur_frm, true);
    }
    if (e.key === 'F12') {
      e.preventDefault();
      e.stopImmediatePropagation();
      nkt_record_cash_drawer_frontdoor(cur_frm, false);
    }
  });
}

function nkt_apply_fast_adjustment_layout(frm) {
  const isDeposit = frm.doc.adjustment_type === 'Advance / Mid-Shift Deposit';
  ['company','settlement_location','cashier','posting_datetime','direction','deposit_section','deposit_destination','deposit_reference_number'].forEach(field => frm.toggle_display(field, false));
  frm.toggle_display('amount', !isDeposit);
  frm.toggle_display('party_name', !isDeposit);
  frm.toggle_display('supporting_document', !isDeposit);
  frm.set_df_property('purpose', 'label', isDeposit ? __('Remarks (Optional)') : __('Remarks / Explanation'));
  frm.set_df_property('purpose', 'reqd', isDeposit ? 0 : 1);
  frm.set_df_property('amount', 'read_only', isDeposit ? 1 : 0);
}

function nkt_recalculate_deposit(frm) {
  if (frm.doc.adjustment_type !== 'Advance / Mid-Shift Deposit' || frm.doc.docstatus !== 0) return;
  let total = 0;
  Object.keys(nkt_deposit_denominations).forEach(field => total += (flt(frm.doc[field]) || 0) * nkt_deposit_denominations[field]);
  frm.set_value('deposit_denomination_total', total);
  frm.set_value('amount', total);
  nkt_show_available_cash(frm, total);
}

function nkt_load_available_cash(frm) {
  if (!frm.doc.cashier_shift || frm.doc.adjustment_type !== 'Advance / Mid-Shift Deposit') return;
  frappe.call({
    method:'nkt_operations.nkt_store_operations.features.cashier.internal.cash_drawer_adjustment_fast_sync.get_cash_drawer_frontdoor_context',
    args:{shift_name:frm.doc.cashier_shift, device_id:nkt_bound_device_id()},
    callback:r => {
      const m = r.message || {};
      frm.__nkt_available_cash = flt(m.expected_cash || 0);
      nkt_show_available_cash(frm, flt(frm.doc.deposit_denomination_total || 0));
    }
  });
}

function nkt_show_available_cash(frm, depositTotal) {
  if (frm.doc.adjustment_type !== 'Advance / Mid-Shift Deposit' || frm.__nkt_available_cash == null) return;
  const available = flt(frm.__nkt_available_cash || 0);
  const remaining = available - flt(depositTotal || 0);
  frm.set_intro(__('Expected drawer cash before deposit: {0}. Remaining after this draft: {1}.', [
    format_currency(available), format_currency(remaining)
  ]), remaining < -0.005 ? 'red' : 'blue');
}

function nkt_validate_cash_drawer_frontdoor(frm) {
  if (!frm.doc.cashier_shift) frappe.throw(__('Cashier Shift is required.'));
  if (!frm.doc.adjustment_type) frappe.throw(__('Adjustment Type is required.'));

  if (frm.doc.adjustment_type === 'Advance / Mid-Shift Deposit') {
    nkt_recalculate_deposit(frm);
    const total = flt(frm.doc.deposit_denomination_total || 0);
    if (total <= 0) {
      frappe.throw(__('Enter at least one denomination. The advance deposit total must be greater than zero.'));
    }
    if (frm.__nkt_available_cash != null && total > flt(frm.__nkt_available_cash) + 0.005) {
      frappe.throw(__('Advance deposit {0} exceeds current expected drawer cash {1}. Reduce the denominations before recording.', [
        format_currency(total), format_currency(frm.__nkt_available_cash)
      ]));
    }
  } else {
    if (flt(frm.doc.amount || 0) <= 0) frappe.throw(__('Amount must be greater than zero.'));
    if (!String(frm.doc.purpose || '').trim()) frappe.throw(__('Remarks / Explanation is required.'));
  }
}

function nkt_cash_drawer_payload(frm) {
  const out = {
    cashier_shift:frm.doc.cashier_shift,
    adjustment_type:frm.doc.adjustment_type,
    amount:flt(frm.doc.amount || 0),
    party_name:String(frm.doc.party_name || '').trim(),
    purpose:String(frm.doc.purpose || '').trim(),
    supporting_document:String(frm.doc.supporting_document || '').trim(),
    client_observed_at:new Date().toISOString()
  };
  Object.keys(nkt_deposit_denominations).forEach(field => out[field] = cint(frm.doc[field] || 0));
  return out;
}

function nkt_record_cash_drawer_frontdoor(frm, openVoucher) {
  if (frm.__nkt_recording) return;
  nkt_validate_cash_drawer_frontdoor(frm);
  if (!frm.__nkt_request_id) frm.__nkt_request_id = nkt_cash_drawer_uuid();

  frm.__nkt_recording = true;
  const requestId = frm.__nkt_request_id;
  const method = 'nkt_operations.nkt_store_operations.features.cashier.internal.cash_drawer_adjustment_fast_sync.record_cash_drawer_adjustment_frontdoor';

  frappe.call({
    method,
    args:{
      payload:JSON.stringify(nkt_cash_drawer_payload(frm)),
      request_id:requestId,
      device_id:nkt_bound_device_id()
    },
    freeze:true,
    freeze_message:__('Recording cash drawer adjustment…')
  }).then(r => {
    nkt_finish_cash_drawer_frontdoor(frm, r.message || {}, openVoucher);
  }).catch(err => {
    frm.__nkt_recording = false;
    frappe.msgprint({
      title:__('Cash Drawer Adjustment Not Recorded'),
      indicator:'red',
      message:(err && err.message) ? err.message : __('The request did not complete. Use F12 again; the same request ID is safe to retry.')
    });
  });
}

function nkt_finish_cash_drawer_frontdoor(frm, result, openVoucher) {
  frm.__nkt_recording = false;
  frappe.show_alert({message:__('Cash drawer adjustment recorded.'), indicator:'green'}, 5);

  if (result.official_print_available && result.cash_drawer_adjustment && openVoucher) {
    frappe.set_route('Form', 'NKT Cash Drawer Adjustment', result.cash_drawer_adjustment);
    return;
  }

  if (!result.official_print_available && openVoucher) {
    frappe.msgprint({
      title:__('Cash Recorded'),
      indicator:'blue',
      message:__('The cash movement is safely recorded. The official voucher will be available after synchronization with the main system.')
    });
  }

  frm.__nkt_request_id = null;
  setTimeout(() => frappe.new_doc('NKT Cash Drawer Adjustment'), 250);
}

frappe.ui.form.on('NKT Cash Drawer Adjustment', Object.fromEntries(
  Object.keys(nkt_deposit_denominations).map(field => [field, nkt_recalculate_deposit])
));

/* ===== END SOURCE: NKT Cash Drawer Adjustment V1.9 ===== */
