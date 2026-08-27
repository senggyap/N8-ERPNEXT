/* NKT CURRENT CLIENT SCRIPT — NKT Cashier Shift — Form
 * Consolidated from the accepted live site on 2026-08-27.
 * Source order exactly preserves Frappe Client Script creation order.
 * Edit this file; do not create phase/recovery Client Script records.
 */

/* ===== SOURCE: NKT Cashier Shift Controls V1.9 ===== */

frappe.ui.form.on('NKT Cashier Shift', {
  refresh(frm) {
    frm.clear_custom_buttons();
    const roles = frappe.user_roles || [];
    const isAdmin = frappe.session.user === 'Administrator' || ['System Manager', 'NKT OWNER', 'NKT ADMINISTRATOR'].some(r => roles.includes(r));
    const isCashier = roles.includes('NKT Cashier');
    const ownShift = frm.doc.cashier === frappe.session.user;
    const v195Marker = 'NKT Shift Close V1.9.5';

    const removeLegacyShiftButtons = () => {
      const legacyLabels = [
        'Record Denomination Count', 'Record and Turn Over Count', 'Turn Over Shift',
        'Mark Reviewed / OK', 'Reviewed / OK', 'Review / Close Shift', 'Close Shift'
      ];
      legacyLabels.forEach(label => {
        frm.remove_custom_button(__(label));
        ['Shift Actions', 'Review Actions', 'Actions'].forEach(group => frm.remove_custom_button(__(label), __(group)));
      });
      if (frm.page && frm.page.wrapper) {
        frm.page.wrapper.find('.custom-actions button, .inner-group-button button').each(function() {
          const text = ($(this).text() || '').trim();
          if (legacyLabels.some(label => text === __(label) || text === label)) $(this).remove();
        });
      }
    };
    removeLegacyShiftButtons();
    setTimeout(removeLegacyShiftButtons, 250);
    setTimeout(removeLegacyShiftButtons, 1000);

    frm.set_df_property('cashier', 'read_only', 1);
    if (!frm.is_new()) {
      frm.set_df_property('opening_cash', 'read_only', 1);
      // Existing shifts must close/review through controlled V1.9 actions,
      // never through Frappe's generic Submit button.
      setTimeout(() => {
        if (frm.page && frm.page.clear_primary_action) frm.page.clear_primary_action();
      }, 0);
    }

    const formatMoney = value => format_currency(value || 0);
    const denominationFields = [
      ['bill_1000_qty', '₱1,000 Bills', 1000], ['bill_500_qty', '₱500 Bills', 500],
      ['bill_200_qty', '₱200 Bills', 200], ['bill_100_qty', '₱100 Bills', 100],
      ['bill_50_qty', '₱50 Bills', 50], ['bill_20_qty', '₱20 Bills', 20],
      ['coin_20_qty', '₱20 Coins', 20], ['coin_10_qty', '₱10 Coins', 10],
      ['coin_5_qty', '₱5 Coins', 5], ['coin_1_qty', '₱1 Coins', 1],
      ['coin_025_qty', '₱0.25 Coins', 0.25]
    ];

    function openClosingCountDialog() {
      frappe.call({
        method: 'nkt_operations.nkt_store_operations.features.cashier.shift_engine.get_cash_count_draft',
        args: {shift_name: frm.doc.name},
        freeze: true,
        callback(r) {
          if (r.exc) return;
          const existing = r.message || {};
          const fields = [];
          denominationFields.forEach((entry, index) => {
            if (index === 6) fields.push({fieldtype: 'Column Break'});
            fields.push({fieldname: entry[0], label: entry[1], fieldtype: 'Int', default: (existing.denominations || {})[entry[0]] || 0});
          });
          fields.push({fieldtype:'Section Break'});
          fields.push({fieldname:'calculated_total', label:'Current Denomination Total', fieldtype:'Currency', read_only:1, default:existing.actual_cash || 0});
          fields.push({fieldname:'expected_cash_display', label:'Current Expected Cash', fieldtype:'Currency', read_only:1, default:existing.expected_cash || 0});
          fields.push({fieldname:'difference_display', label:'Current Over / (Short)', fieldtype:'Currency', read_only:1, default:existing.over_short || 0});
          fields.push({fieldname:'notes', label:'Cash Difference Explanation / Closing Notes', fieldtype:'Small Text', default:existing.notes || ''});
          fields.push({fieldname:'save_draft_button', label:'Save Count Draft', fieldtype:'Button'});

          const dialog = new frappe.ui.Dialog({
            title: __('Closing Count — Draft or Finalize'),
            fields,
            primary_action_label: __('Finalize and Close My Shift'),
            primary_action(values) {
              const denominations = {};
              denominationFields.forEach(entry => denominations[entry[0]] = values[entry[0]] || 0);
              frappe.confirm(
                __('Finalize this count and close your shift? No more drawer movements can be posted until an Owner/Administrator reopens it.'),
                () => frappe.call({
                  method: 'nkt_operations.nkt_store_operations.features.cashier.shift_engine.finalize_and_close_shift',
                  args: {shift_name: frm.doc.name, denominations, notes: values.notes || ''},
                  freeze: true,
                  callback(res) {
                    if (!res.exc) {
                      dialog.hide();
                      const m = res.message || {};
                      frappe.msgprint(__('Cashier shift closed.<br>Expected Cash: {0}<br>Actual Cash: {1}<br>Over / (Short): {2}<br>Status: {3}', [
                        formatMoney(m.expected_cash), formatMoney(m.actual_cash), formatMoney(m.over_short), m.status || ''
                      ]));
                      frm.reload_doc();
                    }
                  }
                })
              );
            }
          });

          const recalc = () => {
            let total = 0;
            denominationFields.forEach(entry => total += (flt(dialog.get_value(entry[0])) || 0) * entry[2]);
            dialog.set_value('calculated_total', total);
            dialog.set_value('difference_display', total - flt(existing.expected_cash || 0));
          };
          dialog.show();
          denominationFields.forEach(entry => dialog.fields_dict[entry[0]].$input.on('input change', recalc));
          dialog.fields_dict.save_draft_button.$input.off('click').on('click', () => {
            const values = dialog.get_values(true) || {};
            const denominations = {};
            denominationFields.forEach(entry => denominations[entry[0]] = values[entry[0]] || 0);
            frappe.call({
              method: 'nkt_operations.nkt_store_operations.features.cashier.shift_engine.save_cash_count_draft',
              args: {shift_name: frm.doc.name, denominations, notes: values.notes || ''},
              freeze: true,
              callback(res) {
                if (!res.exc) {
                  const m = res.message || {};
                  frappe.show_alert({message: __('Count draft saved. Total {0}; difference {1}', [formatMoney(m.actual_cash), formatMoney(m.over_short)]), indicator:'green'});
                }
              }
            });
          });
          recalc();
        }
      });
    }

    if (!frm.is_new() && (isAdmin || (isCashier && ownShift))) {
      frm.add_custom_button(__('Refresh Shift Totals'), () => {
        frappe.call({
          method: 'nkt_operations.nkt_store_operations.features.cashier.shift_engine.refresh_shift_totals',
          args: {shift_name: frm.doc.name}, freeze: true, callback: () => frm.reload_doc()
        });
      }, __('V1.9.5 Shift Actions'));
      frm.add_custom_button(__('View My Shift Movements'), () => frappe.set_route('List', 'NKT Cashier Movement', {cashier_shift: frm.doc.name}), __('Shift Actions'));
    }

    if (!frm.is_new() && frm.doc.status === 'Open' && ownShift && (isCashier || isAdmin)) {
      frm.add_custom_button(__('Closing Count'), openClosingCountDialog, __('Shift Actions'));
    }

    const awaitingReview = ['Cashier Closed - Awaiting Review', 'Counted - Awaiting Approval', 'Turned Over - Awaiting Review'].includes(frm.doc.status);
    if (!frm.is_new() && awaitingReview && isAdmin) {
      frm.add_custom_button(__('Review and Finalize'), () => {
        const dialog = new frappe.ui.Dialog({
          title: __('Owner / Administrator Reconciliation Review'),
          fields: [{fieldname:'approval_reason', label:'Review Note', fieldtype:'Small Text'}],
          primary_action_label: __('Mark Reviewed and Finalize'),
          primary_action(values) {
            frappe.call({
              method: 'nkt_operations.nkt_store_operations.features.cashier.shift_engine.approve_and_close_shift',
              args: {shift_name: frm.doc.name, approval_reason: values.approval_reason || ''},
              freeze: true,
              callback(r) { if (!r.exc) { dialog.hide(); frm.reload_doc(); } }
            });
          }
        });
        dialog.show();
      }, __('V1.9.5 Review Actions'));
      frm.add_custom_button(__('Reopen for Cashier Correction'), () => {
        frappe.prompt(
          [{fieldname:'reason', label:'Reason for Reopening', fieldtype:'Small Text', reqd:1}],
          values => frappe.call({
            method:'nkt_operations.nkt_store_operations.features.cashier.shift_engine.reopen_cashier_closed_shift',
            args:{shift_name:frm.doc.name, reason:values.reason}, freeze:true, callback:() => frm.reload_doc()
          }),
          __('Controlled Reopen'), __('Reopen Shift')
        );
      }, __('V1.9.5 Review Actions'));
    }
  }
});

/* ===== END SOURCE: NKT Cashier Shift Controls V1.9 ===== */

/* ===== SOURCE: NKT C15C8C Security Guard - NKT Cashier Shift ===== */
(() => {
  const NS="NKTCashierShiftC15C8C", DOCTYPE="NKT Cashier Shift", LIMITED_ALLOWED=true, POLL_MS=2000;
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

/* ===== END SOURCE: NKT C15C8C Security Guard - NKT Cashier Shift ===== */

/* ===== SOURCE: NKT Post-C15F Cashier Shift Frontline Presentation FR2 ===== */
// NKT Post-C15F FR2 - Cashier Shift frontline simplification + contextual print wording
(() => {
    const DOCTYPE = "NKT Cashier Shift";
    const FRONTLINE = new Set(["NKT Cashier", "NKT CASHIER"]);
    const ELEVATED = new Set(["Administrator", "System Manager", "NKT OWNER", "NKT ADMINISTRATOR"]);
    const ALLOW = new Set([
        "shift_details_section", "company", "settlement_location", "cashier", "shift_start", "shift_end", "status",
        "opening_section", "opening_cash", "expected_cash",
        "count_section", "blind_count_confirmed", "actual_cash_count", "over_short", "count_notes",
        "custom_nkt_v193_count_draft_section", "custom_nkt_count_draft_total",
        "denomination_section", "bill_1000_qty", "bill_500_qty", "bill_200_qty", "bill_100_qty", "bill_50_qty", "bill_20_qty",
        "coin_20_qty", "coin_10_qty", "coin_5_qty", "coin_1_qty", "coin_025_qty",
        "turnover_section", "turnover_status", "turnover_amount"
    ]);
    const LABELS = {
        cashier: "Cashier",
        shift_start: "Shift Start",
        shift_end: "Shift Closed On",
        opening_cash: "Opening Cash",
        actual_cash_count: "Actual Cash",
        over_short: "Over / Short",
        count_notes: "Over / Short Explanation / Closing Notes",
        custom_nkt_count_draft_total: "Denomination Count Total",
        turnover_status: "Turnover",
        turnover_amount: "Cash Turned Over"
    };
    const FINAL_STATES = new Set([
        "Cashier Closed - Awaiting Review", "Reviewed / Closed", "Closed",
        "Counted - Awaiting Approval", "Turned Over - Awaiting Review"
    ]);

    function isFrontlineCashier() {
        const roles = new Set(window.frappe?.user_roles || []);
        const hasCashier = Array.from(FRONTLINE).some((role) => roles.has(role));
        const elevated = Array.from(ELEVATED).some((role) => roles.has(role));
        return hasCashier && !elevated;
    }

    function simplifyFields(frm) {
        if (!isFrontlineCashier() || !frm || frm.doctype !== DOCTYPE) return;
        Object.keys(frm.fields_dict || {}).forEach((fieldname) => {
            frm.toggle_display(fieldname, ALLOW.has(fieldname));
        });
        Object.entries(LABELS).forEach(([fieldname, label]) => {
            if (frm.fields_dict?.[fieldname]) frm.set_df_property(fieldname, "label", label);
        });
    }

    function buttonRoot(frm) {
        const wrapper = frm?.page?.wrapper;
        if (wrapper && wrapper[0]) return wrapper[0];
        return document;
    }

    function tidyButtons(frm) {
        if (!isFrontlineCashier() || !frm || frm.doctype !== DOCTYPE) return;
        const root = buttonRoot(frm);
        const buttons = Array.from(root.querySelectorAll("button"));
        const printButtons = [];
        buttons.forEach((button) => {
            const text = (button.textContent || "").trim();
            if (["Print Shift Report", "Preview Shift Report", "Print Final Shift Report"].includes(text)) {
                printButtons.push(button);
            }
            if (["Count, Turn Over and Print", "Close Shift & Print Final Report"].includes(text)) {
                button.textContent = "Close Shift & Print Final Report";
            }
            if (["Refresh Movement Totals", "Mark Reviewed / OK"].includes(text)) {
                button.style.setProperty("display", "none", "important");
                button.setAttribute("data-nkt-fr2-support-action-hidden", "1");
            }
        });
        const finalPrint = FINAL_STATES.has(frm.doc?.status || "");
        printButtons.forEach((button, index) => {
            if (index > 0) {
                button.style.setProperty("display", "none", "important");
                button.setAttribute("data-nkt-fr2-duplicate-print-hidden", "1");
                return;
            }
            button.style.removeProperty("display");
            button.textContent = finalPrint ? "Print Final Shift Report" : "Preview Shift Report";
            button.setAttribute("data-nkt-fr2-contextual-print", "1");
        });
    }

    let queued = false;
    function schedule(frm) {
        if (!isFrontlineCashier() || queued) return;
        queued = true;
        requestAnimationFrame(() => {
            queued = false;
            simplifyFields(frm);
            tidyButtons(frm);
        });
        setTimeout(() => tidyButtons(frm), 80);
    }

    frappe.ui.form.on(DOCTYPE, {
        refresh(frm) { schedule(frm); },
        status(frm) { schedule(frm); }
    });

    if (!window.__nktPostC15fFr2ShiftObserver && document.body) {
        window.__nktPostC15fFr2ShiftObserver = new MutationObserver(() => {
            if (window.cur_frm?.doctype === DOCTYPE) schedule(window.cur_frm);
        });
        window.__nktPostC15fFr2ShiftObserver.observe(document.body, {childList: true, subtree: true});
    }
})();
/* ===== END SOURCE: NKT Post-C15F Cashier Shift Frontline Presentation FR2 ===== */
