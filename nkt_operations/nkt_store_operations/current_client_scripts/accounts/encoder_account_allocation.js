/* NKT CURRENT CLIENT SCRIPT — NKT Encoder Account Allocation — Form
 * Consolidated from the accepted live site on 2026-08-27.
 * Source order exactly preserves Frappe Client Script creation order.
 * Edit this file; do not create phase/recovery Client Script records.
 */

/* ===== SOURCE: NKT Encoder Account Allocation V1.5 ===== */
(function () {
    const ROOT = "nkt_operations.nkt_store_operations.features.payments_accounts.collection";

    function calculate(frm) {
        let payment = 0;
        let allocated = 0;
        (frm.doc.payments || []).forEach(row => {
            payment += flt(row.amount);
            row.change_amount = 0;
        });
        (frm.doc.allocations || []).forEach(row => {
            allocated += flt(row.allocated_amount);
            row.outstanding_after = Math.max(flt(row.outstanding_before) - flt(row.allocated_amount), 0);
        });
        frm.set_value("collection_amount", payment);
        frm.set_value("total_allocated", allocated);
        frm.set_value("unallocated_amount", Math.max(payment - allocated, 0));
        frm.refresh_field("payments");
        frm.refresh_field("allocations");
    }

    function invalidate_preview(frm) {
        if ((frm.doc.allocations || []).length || frm.doc.application_rule || frm.doc.application_summary) {
            frm.clear_table("allocations");
            frm.set_value("application_rule", "");
            frm.set_value("application_summary", "");
        }
        calculate(frm);
    }

    async function preview_application(frm) {
        calculate(frm);
        if (!frm.doc.customer) {
            frappe.msgprint(__("Select a Customer first."));
            return;
        }
        if (flt(frm.doc.collection_amount) <= 0) {
            frappe.msgprint(__("Enter the independently encoded payment rows first."));
            return;
        }
        const r = await frappe.call({
            method: `${ROOT}.preview_automatic_application`,
            args: {
                customer: frm.doc.customer,
                amount: frm.doc.collection_amount,
                referenced_customer_order: frm.doc.referenced_customer_order || null
            },
            freeze: true,
            freeze_message: __("Building automatic account application...")
        });
        const result = r.message || {};
        frm.clear_table("allocations");
        (result.allocations || []).forEach(row => {
            const child = frm.add_child("allocations");
            Object.assign(child, row);
        });
        await frm.set_value("application_rule", result.rule || "");
        await frm.set_value("application_summary", result.summary || "");
        calculate(frm);
    }

    async function resolve(frm) {
        const r = await frappe.call({method: `${ROOT}.get_collection_candidates`, args: {encoder_allocation: frm.doc.name}});
        const candidates = r.message || [];
        if (!candidates.length) {
            frappe.msgprint(__("No exact cashier collection candidates are currently available."));
            return;
        }
        const dialog = new frappe.ui.Dialog({
            title: __("Resolve Ambiguous Account Collection"),
            fields: [
                {
                    fieldname: "cashier_collection",
                    fieldtype: "Select",
                    label: __("Cashier Collection"),
                    options: candidates.map(c => c.name),
                    reqd: 1,
                    description: candidates.map(c => `${c.name} — ${c.cashier} — ${c.collection_datetime} — ${format_currency(c.total_payment)}`).join("<br>")
                },
                {fieldname: "reason", fieldtype: "Small Text", label: __("Resolution Reason"), reqd: 1},
                {fieldtype: "Section Break", label: __("Authority")},
                {fieldname: "authorized_user", fieldtype: "Link", options: "User", label: __("Authorized User")},
                {fieldname: "authorized_password", fieldtype: "Password", label: __("Authorized Password")}
            ],
            primary_action_label: __("Link Selected Pair"),
            async primary_action(values) {
                await frappe.call({
                    method: `${ROOT}.resolve_collection_match`,
                    type: "POST",
                    args: {
                        encoder_allocation: frm.doc.name,
                        cashier_collection: values.cashier_collection,
                        reason: values.reason,
                        authorized_user: values.authorized_user || null,
                        authorized_password: values.authorized_password || null
                    },
                    freeze: true
                });
                dialog.hide();
                frm.reload_doc();
            }
        });
        dialog.show();
    }

    frappe.ui.form.on("NKT Encoder Account Allocation", {
        setup(frm) {
            frm.set_query("referenced_customer_order", () => ({
                filters: {
                    customer: frm.doc.customer,
                    payment_status: ["in", ["Charged to Account", "Partially Paid"]],
                    amount_due: [">", 0]
                }
            }));
        },
        async refresh(frm) {
            if (frm.is_new()) {
                await frm.set_value("encoder", frappe.session.user);
                await frm.set_value("allocation_date", frappe.datetime.get_today());
            }
            frm.set_df_property("allocations", "read_only", 1);
            calculate(frm);
            if (frm.doc.status === "Draft" && !frm.is_new()) {
                frm.add_custom_button(__("Preview Automatic Application"), () => preview_application(frm), __("Verification"));
                frm.add_custom_button(__("Submit Payment Verification"), async () => {
                    await frappe.call({
                        method: `${ROOT}.submit_encoder_allocation`,
                        type: "POST",
                        args: {allocation: frm.doc.name},
                        freeze: true,
                        freeze_message: __("Verifying collection and applying it to the customer account...")
                    });
                    frm.reload_doc();
                });
            }
            if (["Unmatched", "Ambiguous"].includes(frm.doc.status)) {
                frm.add_custom_button(__("Retry Reconciliation"), async () => {
                    await frappe.call({method: `${ROOT}.retry_collection_match`, type: "POST", args: {encoder_allocation: frm.doc.name}, freeze: true});
                    frm.reload_doc();
                }, __("Reconciliation"));
            }
            if (frm.doc.status === "Ambiguous") {
                frm.add_custom_button(__("Resolve Ambiguous Match"), () => resolve(frm), __("Reconciliation"));
            }
        },
        customer(frm) { invalidate_preview(frm); },
        referenced_customer_order(frm) { invalidate_preview(frm); },
        validate(frm) { calculate(frm); },
        payments_remove(frm) { invalidate_preview(frm); }
    });

    frappe.ui.form.on("NKT Account Collection Payment", {
        payment_method(frm) { invalidate_preview(frm); },
        amount(frm) { invalidate_preview(frm); },
        reference_number(frm) { invalidate_preview(frm); },
        check_number(frm) { invalidate_preview(frm); }
    });
})();

/* ===== END SOURCE: NKT Encoder Account Allocation V1.5 ===== */

/* ===== SOURCE: NKT C15C8C Security Guard - NKT Encoder Account Allocation ===== */
(() => {
  const NS="NKTEncoderAccountAllocationC15C8C", DOCTYPE="NKT Encoder Account Allocation", LIMITED_ALLOWED=false, POLL_MS=2000;
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

/* ===== END SOURCE: NKT C15C8C Security Guard - NKT Encoder Account Allocation ===== */
