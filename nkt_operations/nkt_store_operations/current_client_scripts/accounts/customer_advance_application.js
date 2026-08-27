/* NKT CURRENT CLIENT SCRIPT — NKT Customer Advance Application — Form
 * Consolidated from the accepted live site on 2026-08-27.
 * Source order exactly preserves Frappe Client Script creation order.
 * Edit this file; do not create phase/recovery Client Script records.
 */

/* ===== SOURCE: NKT C5.4 Advance Application Correction ===== */

frappe.ui.form.on("NKT Customer Advance Application", {
    refresh(frm) {
        const allowed = new Set([
            "System Manager",
            "NKT OWNER",
            "NKT ADMINISTRATOR",
            "NKT Credit Controller"
        ]);

        const authorized = (frappe.user_roles || []).some(
            role => allowed.has(role)
        ) || frappe.session.user === "Administrator";

        if (
            !authorized ||
            frm.doc.docstatus !== 1 ||
            frm.doc.application_status !== "Applied"
        ) {
            return;
        }

        frm.add_custom_button(
            __("Reverse / Correct Advance"),
            () => {
                const d = new frappe.ui.Dialog({
                    title: __("Correct Customer Advance Application"),
                    fields: [
                        {
                            fieldname: "reason",
                            label: __("Correction Reason"),
                            fieldtype: "Small Text",
                            reqd: 1
                        },
                        {
                            fieldname: "reapply",
                            label: __("Reapply to Different Account Order"),
                            fieldtype: "Check",
                            default: 0
                        },
                        {
                            fieldname: "target_order",
                            label: __("Target Customer Order"),
                            fieldtype: "Link",
                            options: "NKT Customer Order",
                            depends_on: "eval:doc.reapply==1",
                            mandatory_depends_on: "eval:doc.reapply==1",
                            get_query() {
                                return {
                                    filters: {
                                        customer: frm.doc.customer,
                                        company: frm.doc.company,
                                        docstatus: 1
                                    }
                                };
                            }
                        }
                    ],
                    primary_action_label: __("Apply Correction"),
                    primary_action(values) {
                        d.hide();

                        frappe.call({
                            method:
                                "nkt_operations.nkt_store_operations." +
                                "nkt_c5_4_advance_correction." +
                                "reverse_advance_application",
                            type: "POST",
                            args: {
                                application: frm.doc.name,
                                reason: values.reason,
                                reapply_to_order:
                                    values.reapply
                                        ? values.target_order
                                        : null
                            },
                            freeze: true,
                            freeze_message: __("Applying controlled correction..."),
                            callback(r) {
                                if (r.message) {
                                    frappe.msgprint({
                                        title: __("Advance Correction Completed"),
                                        indicator: "green",
                                        message:
                                            __("Application {0} is now Reversed.", [
                                                frm.doc.name
                                            ]) +
                                            "<br>" +
                                            __("No new Payment Receipt or Cashier Movement was created.")
                                    });
                                    frm.reload_doc();
                                }
                            }
                        });
                    }
                });

                d.show();
            },
            __("Actions")
        );
    }
});

/* ===== END SOURCE: NKT C5.4 Advance Application Correction ===== */

/* ===== SOURCE: NKT C15C8C Security Guard - NKT Customer Advance Application ===== */
(() => {
  const NS="NKTCustomerAdvanceApplicationC15C8C", DOCTYPE="NKT Customer Advance Application", LIMITED_ALLOWED=false, POLL_MS=2000;
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

/* ===== END SOURCE: NKT C15C8C Security Guard - NKT Customer Advance Application ===== */
