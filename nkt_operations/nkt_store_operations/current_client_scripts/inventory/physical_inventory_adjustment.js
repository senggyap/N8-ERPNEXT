/* NKT CURRENT CLIENT SCRIPT — NKT Physical Inventory Adjustment — Form
 * Consolidated from the accepted live site on 2026-08-27.
 * Source order exactly preserves Frappe Client Script creation order.
 * Edit this file; do not create phase/recovery Client Script records.
 */

/* ===== SOURCE: NKT C8 Physical Inventory Adjustment V2.0C.8C.1 ===== */
frappe.ui.form.on('NKT Physical Inventory Adjustment', {
  refresh(frm) {
    const roles = frappe.user_roles || [];
    const isAdminOwner =
      frappe.session.user === 'Administrator' ||
      roles.includes('NKT OWNER') ||
      roles.includes('NKT ADMINISTRATOR');

    if (frm.doc.docstatus === 0) {
      frm.dashboard.set_headline(
        __('Record the actual physical count. On Submit, ERP stock is corrected immediately and the record becomes Pending Admin Review.')
      );

      frm.add_custom_button(__('Refresh Count Preview'), async () => {
        await frm.save();
        await frm.reload_doc();

        const blockers = (frm.doc.blockers || '').split('\n').filter(Boolean);
        const msg = [
          __('<b>System snapshot refreshed.</b>'),
          __('Lines with variance: {0}', [frm.doc.variance_line_count || 0]),
          blockers.length
            ? '<br><b>' + __('Posting blockers') + '</b><br>' +
              blockers.map(x => '• ' + frappe.utils.escape_html(x)).join('<br>')
            : '<br>' + __('No posting blockers detected.')
        ].join('<br>');

        frappe.msgprint({
          title: __('Physical Count Preview'),
          indicator: blockers.length ? 'orange' : 'green',
          message: msg
        });
      }, __('Actions'));
    }

    if (frm.doc.docstatus === 1) {
      frm.dashboard.set_headline(
        frm.doc.review_status === 'Reconciled'
          ? __('Inventory correction posted and Admin Review reconciled.')
          : __('Inventory correction is already posted. Admin review is separate from the stock correction.')
      );

      if (!isAdminOwner || frm.doc.review_lock) {
        return;
      }

      if (frm.doc.review_status === 'Pending Admin Review') {
        frm.add_custom_button(__('Start Admin Review'), async () => {
          await frappe.call({
            method: 'nkt_operations.nkt_store_operations.features.setup_validation.internal.review.start_admin_review',
            args: { name: frm.doc.name },
            freeze: true,
            freeze_message: __('Starting Admin review...')
          });
          await frm.reload_doc();
        }, __('Admin Review'));
      }

      if (['Pending Admin Review', 'Under Review', 'Discrepancy Flagged'].includes(frm.doc.review_status)) {
        frm.add_custom_button(__('Reconcile'), () => {
          show_review_dialog(frm, false);
        }, __('Admin Review'));

        frm.add_custom_button(__('Flag Discrepancy'), () => {
          show_review_dialog(frm, true);
        }, __('Admin Review'));
      }
    }
  },

  before_submit(frm) {
    if (!frm.doc.physical_count_confirmed) {
      frappe.throw(__('Confirm that the physical count reflects actual stock.'));
    }
    if (!frm.doc.count_reason) {
      frappe.throw(__('Count Reason is required.'));
    }
    const rows = frm.doc.items || [];
    if (!rows.length) {
      frappe.throw(__('At least one counted item is required.'));
    }
    const uncounted = rows.filter(r => !r.physical_qty_confirmed);
    if (uncounted.length) {
      frappe.throw(__('Every item row must be marked Physical Quantity Counted, including rows physically counted as zero.'));
    }
  },

  on_submit(frm) {
    frappe.msgprint({
      title: __('Inventory Corrected'),
      indicator: 'green',
      message:
        __('ERP stock has been corrected to the submitted physical quantities.') +
        '<br><br><b>' +
        __('Admin Review Status: Pending Admin Review') +
        '</b><br>' +
        __('Do not create a fake sale or payroll deduction to explain the variance. Accountability is reviewed separately.')
    });
  }
});

frappe.ui.form.on('NKT Physical Inventory Adjustment Item', {
  item_code: async function(frm, cdt, cdn) {
    const row = locals[cdt][cdn];
    if (!row.item_code || !frm.doc.warehouse) {
      return;
    }

    const response = await frappe.call({
      method: 'nkt_operations.nkt_store_operations.features.inventory.physical_inventory.get_stock_snapshot',
      args: {
        warehouse: frm.doc.warehouse,
        item_code: row.item_code
      }
    });
    const snap = response.message || {};
    frappe.model.set_value(cdt, cdn, 'item_name', snap.item_name || '');
    frappe.model.set_value(cdt, cdn, 'stock_uom', snap.stock_uom || '');
    frappe.model.set_value(cdt, cdn, 'system_qty_snapshot', snap.system_qty || 0);
    frappe.model.set_value(cdt, cdn, 'valuation_rate_snapshot', snap.valuation_rate || 0);
  }
});

function show_review_dialog(frm, flagDiscrepancy) {
  const classificationOptions = [
    'Counting Error',
    'Unrecorded Release',
    'Unrecorded Receipt',
    'Damage / Spoilage',
    'Handling Loss',
    'Suspected Loss / Theft',
    'System / Data Error',
    'Timing Difference',
    'Other'
  ].join('\n');

  const dialog = new frappe.ui.Dialog({
    title: flagDiscrepancy ? __('Flag Inventory Discrepancy') : __('Reconcile Inventory Adjustment'),
    fields: [
      {
        fieldname: 'classification',
        label: __('Accountability Classification'),
        fieldtype: 'Select',
        options: classificationOptions,
        reqd: 1
      },
      {
        fieldname: 'accountability_notes',
        label: __('Accountability Notes'),
        fieldtype: 'Small Text'
      },
      {
        fieldname: 'review_notes',
        label: __('Review Notes'),
        fieldtype: 'Small Text',
        reqd: flagDiscrepancy ? 1 : 0
      },
      {
        fieldname: 'ack',
        label: __('I understand this review does not alter the already-posted stock correction and does not create a payroll deduction.'),
        fieldtype: 'Check',
        reqd: 1
      }
    ],
    primary_action_label: flagDiscrepancy ? __('Flag Discrepancy') : __('Reconcile'),
    primary_action: async values => {
      const method = flagDiscrepancy
        ? 'nkt_operations.nkt_store_operations.features.setup_validation.internal.review.flag_admin_discrepancy'
        : 'nkt_operations.nkt_store_operations.features.setup_validation.internal.review.complete_admin_review';

      await frappe.call({
        method,
        args: {
          name: frm.doc.name,
          classification: values.classification,
          accountability_notes: values.accountability_notes || '',
          review_notes: values.review_notes || ''
        },
        freeze: true,
        freeze_message: flagDiscrepancy
          ? __('Recording discrepancy...')
          : __('Completing Admin review...')
      });
      dialog.hide();
      await frm.reload_doc();
    }
  });
  dialog.show();
}
/* ===== END SOURCE: NKT C8 Physical Inventory Adjustment V2.0C.8C.1 ===== */

/* ===== SOURCE: NKT C15C8C Security Guard - NKT Physical Inventory Adjustment ===== */
(() => {
  const NS="NKTPhysicalInventoryAdjustmentC15C8C", DOCTYPE="NKT Physical Inventory Adjustment", LIMITED_ALLOWED=false, POLL_MS=2000;
  frappe.ui.form.on(DOCTYPE,{refresh(frm){init(frm);}});
  function id(){try{return String(window.localStorage.getItem('nkt_device_id')||'').trim();}catch(_){return '';}}
  function init(frm){clear();bind(frm);if(!id())return;poll(frm).then(()=>watch(frm));}
  function bind(frm){$ (document).off(`keydown.${NS}`).on(`keydown.${NS}`,e=>{if(e.key==='F1'){e.preventDefault();e.stopImmediatePropagation();return false;}if(e.key==='F12'&&e.ctrlKey&&e.altKey&&e.shiftKey){e.preventDefault();e.stopImmediatePropagation();if(!LIMITED_ALLOWED)block(frm);const d=id();if(d)frappe.call({method:'nkt_operations.nkt_store_operations.features.offline_edge.internal.device_controls.self_restrict_current_device',args:{device_id:d}}).catch(()=>{});return false;}});}
  function clear(){const k=`${NS}Timer`;if(window[k]){clearInterval(window[k]);window[k]=null;}$(window).off(`focus.${NS}`);}
  function watch(frm){const k=`${NS}Timer`;window[k]=setInterval(()=>poll(frm),POLL_MS);$(window).off(`focus.${NS}`).on(`focus.${NS}`,()=>poll(frm));}
  function poll(frm){const d=id();if(!d)return Promise.resolve();return frappe.call({method:'nkt_operations.nkt_store_operations.features.offline_edge.internal.device_controls.get_client_security_bootstrap',args:{device_id:d}}).then(r=>{const p=r.message||{};if(p.access==='unavailable'){if(p.local_action==='crypto_erase_sensitive_state')wipe();terminal(frm,p.message||'Device access unavailable.');return;}if(p.ui_mode==='limited'){if(!LIMITED_ALLOWED)block(frm);}else unblock(frm);}).catch(()=>{});}
  function block(frm){frm.__nkt_c15c8c_limited=true;frm.disable_save();frm.page.clear_actions();frm.wrapper.find('.form-layout').hide();message(frm,'This function is unavailable in limited mode.');}
  function unblock(frm){if(!frm.__nkt_c15c8c_limited)return;frm.__nkt_c15c8c_limited=false;frm.wrapper.find('.nkt-c15c8c-guard').remove();frm.wrapper.find('.form-layout').show();frm.refresh();}
  function terminal(frm,text){clear();$(document).off(`keydown.${NS}`);frm.disable_save();frm.page.clear_actions();frm.wrapper.find('.form-layout').hide();message(frm,text||'Device access unavailable.');}
  function message(frm,text){let b=frm.wrapper.find('.nkt-c15c8c-guard');if(!b.length){b=$('<div class="nkt-c15c8c-guard"></div>').css({minHeight:'320px',display:'flex',alignItems:'center',justifyContent:'center',border:'1px solid #777',background:'#f1f1f1',font:'700 16px Tahoma,Arial,sans-serif'});frm.wrapper.prepend(b);}b.text(text).show();}
  function wipe(){try{for(let i=window.localStorage.length-1;i>=0;i--){const k=window.localStorage.key(i);if(k&&k.startsWith('nkt_')&&k!=='nkt_device_id')window.localStorage.removeItem(k);}}catch(_){}try{for(let i=window.sessionStorage.length-1;i>=0;i--){const k=window.sessionStorage.key(i);if(k&&k.startsWith('nkt_'))window.sessionStorage.removeItem(k);}}catch(_){}}
})();

/* ===== END SOURCE: NKT C15C8C Security Guard - NKT Physical Inventory Adjustment ===== */
