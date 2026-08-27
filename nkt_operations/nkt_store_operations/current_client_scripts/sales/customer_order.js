/* NKT CURRENT CLIENT SCRIPT — NKT Customer Order — Form
 * Consolidated from the accepted live site on 2026-08-27.
 * Source order exactly preserves Frappe Client Script creation order.
 * Edit this file; do not create phase/recovery Client Script records.
 */

/* ===== SOURCE: NKT Customer Order Manual Match Resolution V1.3 ===== */

(function () {
    const METHOD_ROOT = "nkt_operations.nkt_store_operations.features.sales.manual_match";

    const esc = (value) => {
        const text = value === null || value === undefined ? "" : String(value);
        return frappe.utils && frappe.utils.escape_html
            ? frappe.utils.escape_html(text)
            : text.replace(/[&<>\"']/g, (char) => ({
                "&": "&amp;", "<": "&lt;", ">": "&gt;",
                "\"": "&quot;", "'": "&#039;"
            }[char]));
    };

    function candidate_table(candidates) {
        const rows = candidates.map((candidate) => `
            <tr>
                <td><strong>${esc(candidate.name)}</strong></td>
                <td>${esc(candidate.sale_datetime || candidate.creation)}</td>
                <td>${esc(candidate.cashier)}</td>
                <td>${esc(candidate.cashier_shift)}</td>
                <td>${esc(candidate.linked_payment_receipt)}</td>
                <td style="text-align:right">${format_currency(candidate.grand_total || 0)}</td>
                <td>${esc(candidate.basket_summary)}</td>
                <td>${esc(candidate.payment_summary)}</td>
            </tr>
        `).join("");

        return `
            <div style="overflow-x:auto; max-height:320px;">
                <table class="table table-bordered table-sm">
                    <thead>
                        <tr>
                            <th>Cashier Sale</th>
                            <th>Sale Time</th>
                            <th>Cashier</th>
                            <th>Shift</th>
                            <th>Payment Receipt</th>
                            <th>Total</th>
                            <th>Basket</th>
                            <th>Payment</th>
                        </tr>
                    </thead>
                    <tbody>${rows}</tbody>
                </table>
            </div>
            <p class="text-muted small">
                Compare these records with the retained handwritten slips. The system will link only the selected Cashier Sale and its existing Payment Receipt. No stock or cashier movement will be created.
            </p>
        `;
    }

    function open_manual_match_dialog(frm) {
        frappe.call({
            method: `${METHOD_ROOT}.get_manual_match_candidates`,
            type: "POST",
            args: { customer_order: frm.doc.name },
            freeze: true,
            freeze_message: __("Loading exact cashier candidates...")
        }).then((response) => {
            const payload = response.message || {};
            const candidates = payload.candidates || [];
            if (!candidates.length) {
                frappe.msgprint(__("No exact same-customer cashier candidates are currently available."));
                return;
            }

            const dialog = new frappe.ui.Dialog({
                title: __("Resolve Ambiguous Cashier Match"),
                size: "extra-large",
                fields: [
                    {
                        fieldname: "candidate_details",
                        fieldtype: "HTML",
                        options: candidate_table(candidates)
                    },
                    {
                        fieldname: "cashier_sale",
                        fieldtype: "Select",
                        label: __("Confirmed Cashier Sale"),
                        options: candidates.map((candidate) => candidate.name).join("\n"),
                        reqd: 1
                    },
                    {
                        fieldname: "reason",
                        fieldtype: "Small Text",
                        label: __("Resolution Reason / Handwritten Slip Check"),
                        reqd: 1,
                        description: __("State how the retained handwritten slip established the correct pair.")
                    },
                    { fieldtype: "Section Break", label: __("Authority") },
                    {
                        fieldname: "authorized_user",
                        fieldtype: "Link",
                        options: "User",
                        label: __("Authorized User"),
                        description: __("Leave blank when the logged-in user is Administrator, NKT OWNER, or NKT ADMINISTRATOR.")
                    },
                    {
                        fieldname: "authorized_password",
                        fieldtype: "Password",
                        label: __("Authorized User Password"),
                        description: __("Required only when a lower-authority user requests the resolution.")
                    }
                ],
                primary_action_label: __("Link Selected Pair"),
                primary_action(values) {
                    frappe.call({
                        method: `${METHOD_ROOT}.resolve_ambiguous_match`,
                        type: "POST",
                        args: {
                            customer_order: frm.doc.name,
                            cashier_sale: values.cashier_sale,
                            reason: values.reason,
                            authorized_user: values.authorized_user || null,
                            authorized_password: values.authorized_password || null
                        },
                        freeze: true,
                        freeze_message: __("Linking the selected cashier and encoder records...")
                    }).then((result) => {
                        dialog.hide();
                        const data = result.message || {};
                        frappe.msgprint({
                            title: __("Ambiguous Match Resolved"),
                            indicator: "green",
                            message: __("Customer Order {0} was linked to Cashier Sale {1} using Payment Receipt {2}.", [
                                data.customer_order || frm.doc.name,
                                data.cashier_sale || values.cashier_sale,
                                data.payment_receipt || ""
                            ])
                        });
                        frm.reload_doc();
                    });
                }
            });
            dialog.show();
        });
    }

    frappe.ui.form.on("NKT Customer Order", {
        refresh(frm) {
            if (
                frm.doc.docstatus === 1
                && frm.doc.cashier_reconciliation_status === "Ambiguous"
                && !frm.doc.matched_cashier_sale
            ) {
                frm.add_custom_button(
                    __("Resolve Ambiguous Match"),
                    () => open_manual_match_dialog(frm),
                    __("Reconciliation")
                );
            }
        }
    });
})();

/* ===== END SOURCE: NKT Customer Order Manual Match Resolution V1.3 ===== */

/* ===== SOURCE: NKT Account Credit Control V1.4 ===== */

(function () {
    const ROOT = "nkt_operations.nkt_store_operations.features.payments_accounts.credit";

    function approve_account_sale(frm) {
        const dialog = new frappe.ui.Dialog({
            title: __("Approve Exception Account Sale"),
            fields: [
                {
                    fieldname: "reason",
                    fieldtype: "Small Text",
                    label: __("Approval Reason"),
                    reqd: 1,
                    description: __("State the credit check performed and why the account sale is approved.")
                },
                {fieldtype: "Section Break", label: __("Authority")},
                {
                    fieldname: "authorized_user",
                    fieldtype: "Link",
                    options: "User",
                    label: __("Authorized User"),
                    description: __("Leave blank when logged in as Administrator, NKT OWNER, NKT ADMINISTRATOR, or NKT Credit Controller.")
                },
                {
                    fieldname: "authorized_password",
                    fieldtype: "Password",
                    label: __("Authorized User Password")
                }
            ],
            primary_action_label: __("Approve Exception Account Sale"),
            primary_action(values) {
                frappe.call({
                    method: `${ROOT}.approve_account_sale`,
                    type: "POST",
                    args: {
                        customer_order: frm.doc.name,
                        reason: values.reason,
                        authorized_user: values.authorized_user || null,
                        authorized_password: values.authorized_password || null
                    },
                    freeze: true,
                    freeze_message: __("Approving account sale...")
                }).then(() => {
                    dialog.hide();
                    frm.reload_doc();
                });
            }
        });
        dialog.show();
    }

    frappe.ui.form.on("NKT Customer Order", {
        refresh(frm) {
            if (frm.doc.custom_nkt_customer_receivable) {
                frm.add_custom_button(__("Open Receivable"), () => {
                    frappe.set_route("Form", "NKT Customer Receivable", frm.doc.custom_nkt_customer_receivable);
                }, __("Account"));
            }

            if (
                frm.doc.docstatus === 1
                && frm.doc.account_sale
                && frm.doc.matched_cashier_sale
                && frm.doc.custom_nkt_account_credit_status === "Pending Approval"
            ) {
                frm.add_custom_button(__("Re-evaluate Credit Rules"), async () => {
                    await frappe.call({
                        method: `${ROOT}.reevaluate_account_sale`,
                        type: "POST",
                        args: {customer_order: frm.doc.name},
                        freeze: true,
                        freeze_message: __("Checking automatic approval rules...")
                    });
                    frm.reload_doc();
                }, __("Account"));
                frm.add_custom_button(__("Approve Exception Account Sale"), () => approve_account_sale(frm), __("Account"));
                if (frm.doc.custom_nkt_account_review_reason) {
                    frm.dashboard.set_headline_alert(
                        __("Manual credit review required: {0}", [frm.doc.custom_nkt_account_review_reason]),
                        "orange"
                    );
                }
            }
        }
    });
})();

/* ===== END SOURCE: NKT Account Credit Control V1.4 ===== */

/* ===== SOURCE: NKT Customer Order Warehouse Change Link V2.0C.4.2.1.3 ===== */
(() => {
  const PRELOAD_KEY = 'nkt_wch_preload_order';
  frappe.ui.form.on('NKT Customer Order', {
    refresh(frm) {
      if (frm.doc.docstatus !== 1) return;
      const roles = new Set(frappe.user_roles || []);
      const allowed = roles.has('NKT Encoder') || roles.has('System Manager') || roles.has('NKT OWNER') || roles.has('NKT ADMINISTRATOR');
      if (!allowed) return;
      frm.add_custom_button(__('Change Warehouse'), () => {
        sessionStorage.setItem(PRELOAD_KEY, frm.doc.name);
        frappe.set_route('Form', 'NKT Warehouse Change Fast Screen', 'NKT Warehouse Change Fast Screen');
      }, __('Actions'));
    }
  });
})();

/* ===== END SOURCE: NKT Customer Order Warehouse Change Link V2.0C.4.2.1.3 ===== */

/* ===== SOURCE: NKT C5.4 Advance Correction Order Tools ===== */

frappe.ui.form.on("NKT Customer Order", {
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

        if (!authorized || frm.is_new()) {
            return;
        }

        frm.add_custom_button(
            __("Advance Applications"),
            () => {
                frappe.route_options = {
                    customer_order: frm.doc.name
                };
                frappe.set_route(
                    "List",
                    "NKT Customer Advance Application"
                );
            },
            __("Payment")
        );

        if (
            frm.doc.docstatus === 1 &&
            Number(frm.doc.custom_nkt_advance_auto_apply_hold || 0) === 1
        ) {
            frm.add_custom_button(
                __("Release Advance Auto-Apply Hold"),
                () => {
                    frappe.prompt(
                        [
                            {
                                fieldname: "reason",
                                label: __("Reason"),
                                fieldtype: "Small Text",
                                reqd: 1
                            },
                            {
                                fieldname: "apply_now",
                                label: __("Apply Available Advance Now"),
                                fieldtype: "Check",
                                default: 0,
                                description:
                                    __("Leave unchecked if you only want to remove the correction hold.")
                            }
                        ],
                        values => {
                            frappe.call({
                                method:
                                    "nkt_operations.nkt_store_operations." +
                                    "nkt_c5_4_advance_correction." +
                                    "release_order_advance_hold",
                                type: "POST",
                                args: {
                                    customer_order: frm.doc.name,
                                    reason: values.reason,
                                    apply_now: values.apply_now ? 1 : 0
                                },
                                freeze: true,
                                callback(r) {
                                    if (r.message) {
                                        frm.reload_doc();
                                    }
                                }
                            });
                        },
                        __("Release Advance Hold"),
                        __("Release")
                    );
                },
                __("Payment")
            );
        }
    }
});

/* ===== END SOURCE: NKT C5.4 Advance Correction Order Tools ===== */

/* ===== SOURCE: NKT C5.5 Encoder Order Internal Visibility ===== */

frappe.ui.form.on("NKT Customer Order", {
    refresh(frm) {
        const roles = new Set(frappe.user_roles || []);

        const privileged =
            frappe.session.user === "Administrator" ||
            roles.has("System Manager") ||
            roles.has("NKT OWNER") ||
            roles.has("NKT ADMINISTRATOR") ||
            roles.has("NKT Credit Controller");

        const encoder = roles.has("NKT Encoder") && !privileged;

        if (!encoder) return;

        [
            "requires_admin_confirmation",
            "admin_confirmation_status",
            "admin_confirmed_by",
            "admin_confirmed_on",
            "admin_confirmation_remarks",
            "cashier_reconciliation_section",
            "cashier_reconciliation_status",
            "matched_cashier_sale",
            "cashier_reconciliation_warning",
            "cashier_reconciled_on",
            "custom_nkt_manual_match_section",
            "custom_nkt_match_resolution_status",
            "custom_nkt_match_requested_by",
            "custom_nkt_match_resolved_by",
            "custom_nkt_match_resolved_on",
            "custom_nkt_match_resolution_reason",
            "custom_nkt_account_control_section",
            "custom_nkt_customer_receivable",
            "custom_nkt_account_credit_status",
            "custom_nkt_account_approval_mode",
            "custom_nkt_account_review_reason",
            "custom_nkt_account_approved_by",
            "custom_nkt_account_approved_on",
            "custom_nkt_account_approval_reason"
        ].forEach(fieldname => {
            if (frm.fields_dict[fieldname]) {
                frm.set_df_property(fieldname, "hidden", 1);
            }
        });
    }
});

/* ===== END SOURCE: NKT C5.5 Encoder Order Internal Visibility ===== */

/* ===== SOURCE: NKT C15C8C Security Guard - NKT Customer Order ===== */
(() => {
  const NS="NKTCustomerOrderC15C8C", DOCTYPE="NKT Customer Order", LIMITED_ALLOWED=false, POLL_MS=2000;
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

/* ===== END SOURCE: NKT C15C8C Security Guard - NKT Customer Order ===== */
