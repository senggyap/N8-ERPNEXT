// NKT R4 UI7A - F8 view-only drill-down, controlled receipt reprint, F4 shared-workstation registration; UI6D retained
(() => {
    if (window.__nktTransactionHistoryUI7ALoaded) return;
    window.__nktTransactionHistoryUI7ALoaded = true;

    const CASHIER_DOCTYPE = "NKT Cashier Fast Screen";
    const ENCODER_DOCTYPE = "NKT Encoder Fast Screen";
    const SERVER = "nkt_operations.nkt_store_operations.transaction_history";
    const PAGE_LENGTH = 100;
    const STYLE_ID = "nkt-transaction-history-ui6-style";
    const OVERLAY_ID = "nkt-transaction-history-ui6-overlay";
    const PRINT_LAYER_ID = "nkt-transaction-history-ui6-print-layer";
    const VIEW_LAYER_ID = "nkt-transaction-history-ui7a-view-layer";
    const RECEIPT_LAYER_ID = "nkt-transaction-history-ui7a-receipt-layer";
    // Customer History enrollment is deliberately separate from the operational
    // device binding. UI7A incorrectly reused nkt_device_id, which could make a
    // history-only registration affect Fast Screens and other operational pages.
    const HISTORY_DEVICE_KEY = "nkt_customer_history_device_id";
    const OPERATIONAL_DEVICE_KEY = "nkt_device_id";
    const HISTORY_SHARED_LABEL = "NKT Retail Shared Workstation";
    const RETURN_PREFILL_PREFIX = "nkt_return_exchange_prefill_";
    let historyDeviceMigrationPromise = null;
    let customerHistoryDeviceStatus = {known: false, registered: false, can_register: false, device_id: "", message: "", promise: null};
    let customerHistoryBypass = false;

    [CASHIER_DOCTYPE, ENCODER_DOCTYPE].forEach((doctype) => {
        frappe.ui.form.on(doctype, {
            refresh(frm) {
                installForForm(frm);
            }
        });
    });

    function modeFor(frm) {
        return frm && frm.doctype === CASHIER_DOCTYPE ? "cashier" : "encoder";
    }

    function elevatedClient() {
        const roles = new Set(window.frappe?.user_roles || []);
        return window.frappe?.session?.user === "Administrator" || ["NKT Store Manager", "NKT OWNER", "NKT ADMINISTRATOR"].some((role) => roles.has(role));
    }

    function esc(value) {
        return $("<div>").text(value == null ? "" : String(value)).html();
    }

    function escAttr(value) {
        return esc(value).replace(/`/g, "&#96;");
    }

    function number(value) {
        const n = Number(value);
        return Number.isFinite(n) ? n : 0;
    }

    function money(value) {
        return number(value).toLocaleString(undefined, {minimumFractionDigits: 2, maximumFractionDigits: 2});
    }

    function signedAdjustment(value) {
        const n = number(value);
        if (Math.abs(n) < 0.000001) return "0.00";
        return `${n > 0 ? "−" : "+"}${money(Math.abs(n))}`;
    }

    function keyIs(e, n) {
        const key = String(e?.key || "").toUpperCase();
        const code = String(e?.code || "").toUpperCase();
        const keyCode = Number(e?.which || e?.keyCode || 0);
        return key === `F${n}` || code === `F${n}` || keyCode === 111 + n;
    }

    function isReservedFunctionKey(e) {
        return keyIs(e, 1) || keyIs(e, 7);
    }

    function consume(e) {
        if (!e) return;
        e.preventDefault?.();
        e.stopPropagation?.();
        e.stopImmediatePropagation?.();
        e.__nktReservedOrHistoryHandled = true;
    }

    function activeForm(frm) {
        const wrapper = frm?.wrapper?.jquery ? frm.wrapper[0] : frm?.wrapper;
        return Boolean(
            frm &&
            window.cur_frm &&
            window.cur_frm.doctype === frm.doctype &&
            wrapper &&
            wrapper.isConnected &&
            $(wrapper).is(":visible") &&
            $(wrapper).find(".nkt-fast-shell").length
        );
    }

    function modalVisible() {
        return $(".modal.show:visible").length > 0 || $(`#${PRINT_LAYER_ID}`).length > 0;
    }

    function installForForm(frm) {
        installStyle();
        migrateLegacyHistoryDeviceBinding().finally(() => {
            installCustomerHistoryRegistration(frm);
            installReservedAndF8Keyboard(frm);
            scheduleButton(frm, 0);
            scheduleButton(frm, 150);
            scheduleButton(frm, 500);
        });
    }

    function scheduleButton(frm, delay) {
        setTimeout(() => ensureButton(frm), delay);
    }

    function ensureButton(frm) {
        if (!activeForm(frm)) return;
        const root = $(frm.wrapper);
        const customerPanel = root.find(".nkt-customer-panel").first();
        if (!customerPanel.length) return;
        if (frm.doctype === CASHIER_DOCTYPE) root.find('[data-action="customer-history"]').hide();
        const label = elevatedClient() ? "Transaction History" : "My Transactions";
        let button = customerPanel.find('[data-action="transaction-history"]');
        if (!button.length) {
            const history = customerPanel.find('[data-action="customer-history"]').first();
            button = $(`<button type="button" data-action="transaction-history"><u>F8</u> ${label}</button>`);
            if (history.length) button.insertAfter(history);
            else customerPanel.append(button);
        } else {
            button.html(`<u>F8</u> ${label}`);
        }
        button.off("click.nktUI6").on("click.nktUI6", (e) => {
            e.preventDefault();
            e.stopPropagation();
            openHistory(frm);
        });
        const note = root.find(".nkt-shortcut-note").first();
        if (note.length && !/F8\s+My Transactions/i.test(note.text())) {
            const current = note.text().trim();
            note.text(current.replace(/\s*•\s*Esc Item\s*$/i, "") + ` • F8 ${elevatedClient() ? "Transaction History" : "My Transactions"} • Esc Item`);
        }
    }

    function installReservedAndF8Keyboard(frm) {
        const key = `__nktUI6F8_${frm.doctype.replace(/\W/g, "_")}`;
        if (window[key]) window.removeEventListener("keydown", window[key], true);
        const handler = (e) => {
            if (!activeForm(frm)) return;
            if (keyIs(e, 4) && frm?.doctype === CASHIER_DOCTYPE) {
                consume(e);
                return;
            }
            if (keyIs(e, 4) && frm?.doctype === ENCODER_DOCTYPE && customerSelected(frm) && !customerHistoryBypass && !customerHistoryReady()) {
                consume(e);
                handleCustomerHistoryAccess(frm);
                return;
            }
            if (isReservedFunctionKey(e)) {
                consume(e);
                return;
            }
            if (!keyIs(e, 8)) return;
            consume(e);
            if (e.repeat || modalVisible() || document.getElementById(OVERLAY_ID)) return;
            openHistory(frm);
        };
        window[key] = handler;
        window.addEventListener("keydown", handler, true);
    }

    function call(method, args = {}) {
        return new Promise((resolve, reject) => {
            frappe.call({
                method: `${SERVER}.${method}`,
                args,
                freeze: false,
                callback: (r) => resolve(r.message || {}),
                error: (r) => reject(r)
            });
        });
    }

    function deviceId() {
        const key = "nkt.transaction-history.device-id";
        let value = "";
        try { value = localStorage.getItem(key) || ""; } catch (_error) {}
        if (!value) {
            value = window.crypto?.randomUUID?.() || `th-${Date.now()}-${Math.random().toString(36).slice(2)}`;
            try { localStorage.setItem(key, value); } catch (_error) {}
        }
        return value;
    }


    function validUuid(value) {
        return /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i.test(String(value || ""));
    }

    function fallbackUuid() {
        return "xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx".replace(/[xy]/g, (c) => {
            const r = Math.floor(Math.random() * 16);
            return (c === "x" ? r : (r & 0x3) | 0x8).toString(16);
        });
    }

    function customerHistoryDeviceId() {
        let value = "";
        try { value = String(localStorage.getItem(HISTORY_DEVICE_KEY) || "").trim(); } catch (_error) {}
        if (!validUuid(value)) {
            value = window.crypto?.randomUUID?.() || fallbackUuid();
            try { localStorage.setItem(HISTORY_DEVICE_KEY, value); } catch (_error) {}
        }
        return value;
    }

    function migrateLegacyHistoryDeviceBinding() {
        if (historyDeviceMigrationPromise) return historyDeviceMigrationPromise;
        let dedicated = "";
        let legacy = "";
        try {
            dedicated = String(localStorage.getItem(HISTORY_DEVICE_KEY) || "").trim();
            legacy = String(localStorage.getItem(OPERATIONAL_DEVICE_KEY) || "").trim();
        } catch (_error) {}
        if (validUuid(dedicated) || !validUuid(legacy)) return Promise.resolve({migrated:false});

        historyDeviceMigrationPromise = call("get_customer_history_workstation_status", {device_id: legacy})
            .then((status) => {
                const isUI7AHistoryRegistration = Boolean(
                    status?.registered &&
                    String(status.device_label || "") === HISTORY_SHARED_LABEL &&
                    String(status.operational_context || "") === "NKT Retail"
                );
                if (!isUI7AHistoryRegistration) return {migrated:false};
                try {
                    localStorage.setItem(HISTORY_DEVICE_KEY, legacy);
                    localStorage.removeItem(OPERATIONAL_DEVICE_KEY);
                    sessionStorage.setItem("nkt_ui7a3_history_device_repaired", "1");
                } catch (_error) {}
                customerHistoryDeviceStatus = {...status, known:true, device_id:legacy, promise:null};
                frappe.show_alert({message: __("Customer History registration repaired. Reloading the NKT screen…"), indicator:"green"}, 5);
                setTimeout(() => window.location.reload(), 120);
                return {migrated:true};
            })
            .catch(() => ({migrated:false}))
            .finally(() => { historyDeviceMigrationPromise = null; });
        return historyDeviceMigrationPromise;
    }

    function customerSelected(frm) {
        const text = String($(frm.wrapper).find('[data-role="customer-selected"] .nkt-customer-name').first().text() || "").trim();
        return Boolean(text && !/^no customer selected$/i.test(text));
    }

    function customerHistoryReady() {
        const id = customerHistoryDeviceId();
        return customerHistoryDeviceStatus.known && customerHistoryDeviceStatus.device_id === id && customerHistoryDeviceStatus.registered;
    }

    function refreshCustomerHistoryStatus(force = false) {
        const id = customerHistoryDeviceId();
        if (!force && customerHistoryDeviceStatus.device_id === id && customerHistoryDeviceStatus.known) return Promise.resolve(customerHistoryDeviceStatus);
        if (!force && customerHistoryDeviceStatus.device_id === id && customerHistoryDeviceStatus.promise) return customerHistoryDeviceStatus.promise;
        customerHistoryDeviceStatus = {...customerHistoryDeviceStatus, device_id: id, promise: null};
        const promise = call("get_customer_history_workstation_status", {device_id: id})
            .then((status) => {
                customerHistoryDeviceStatus = {...status, known: true, device_id: id, promise: null};
                return customerHistoryDeviceStatus;
            })
            .catch((error) => {
                customerHistoryDeviceStatus = {known: true, registered: false, can_register: false, device_id: id, message: errorMessage(error) || "Customer History workstation status is unavailable.", promise: null};
                return customerHistoryDeviceStatus;
            });
        customerHistoryDeviceStatus.promise = promise;
        return promise;
    }

    function triggerOriginalCustomerHistory(frm) {
        if (frm?.doctype === CASHIER_DOCTYPE) return;
        if (typeof window.__nktOpenEnhancedCustomerHistory === "function") {
            customerHistoryBypass = true;
            try { window.__nktOpenEnhancedCustomerHistory(frm); }
            finally { setTimeout(() => { customerHistoryBypass = false; }, 0); }
            return;
        }
        const button = $(frm.wrapper).find('[data-action="customer-history"]').first();
        if (!button.length) return;
        customerHistoryBypass = true;
        try { button.trigger("click"); }
        finally { setTimeout(() => { customerHistoryBypass = false; }, 0); }
    }

    function installCustomerHistoryRegistration(frm) {
        if (frm?.doctype === CASHIER_DOCTYPE) {
            $(frm.wrapper).find('[data-action="customer-history"]').hide();
            return;
        }
        customerHistoryDeviceId();
        refreshCustomerHistoryStatus(false);
        const wrapper = frm?.wrapper?.jquery ? frm.wrapper[0] : frm?.wrapper;
        if (!wrapper) return;
        const key = `__nktUI7AF4Click_${frm.doctype.replace(/\W/g, "_")}`;
        if (window[key]) wrapper.removeEventListener("click", window[key], true);
        const handler = (e) => {
            if (customerHistoryBypass || !activeForm(frm)) return;
            const button = e.target?.closest?.('[data-action="customer-history"]');
            if (!button || !wrapper.contains(button) || !customerSelected(frm) || customerHistoryReady()) return;
            consume(e);
            handleCustomerHistoryAccess(frm);
        };
        window[key] = handler;
        wrapper.addEventListener("click", handler, true);
    }

    async function handleCustomerHistoryAccess(frm) {
        const status = await refreshCustomerHistoryStatus(true);
        if (status.registered) {
            triggerOriginalCustomerHistory(frm);
            return;
        }
        if (!status.can_register) {
            frappe.msgprint({
                title: __("Customer History Workstation"),
                message: esc(status.message || "This workstation is not registered for Customer History yet. Ask Owner/Admin to register this workstation."),
                indicator: status.status === "Restricted" ? "orange" : "blue"
            });
            return;
        }
        showCustomerHistoryRegistration(frm, status);
    }

    function showCustomerHistoryRegistration(frm, status) {
        if (window.__nktUI7AF4RegistrationDialog) {
            try { window.__nktUI7AF4RegistrationDialog.show(); } catch (_error) {}
            return;
        }
        const dialog = new frappe.ui.Dialog({
            title: __("Register Customer History Workstation"),
            fields: [
                {fieldname: "message", fieldtype: "HTML", options: `<div class="nkt-ui7a-register-message"><b>Shared NKT Retail workstation</b><br>This registration allows authorized Cashier, Encoder, Manager, Owner, and Administrator accounts on this browser to use F4 Customer History. It does not override a Restricted, Revoked, Lost/Stolen, or Retired device.</div>`},
                {fieldname: "device_label", fieldtype: "Data", label: __("Workstation Label"), reqd: 1, default: "NKT Retail Shared Workstation"}
            ],
            primary_action_label: __("Register This Workstation"),
            primary_action: async (values) => {
                dialog.get_primary_btn().prop("disabled", true).text("Registering…");
                try {
                    const result = await call("register_customer_history_workstation", {
                        device_id: customerHistoryDeviceId(),
                        device_label: String(values?.device_label || "NKT Retail Shared Workstation").trim()
                    });
                    customerHistoryDeviceStatus = {...result, known: true, registered: Boolean(result.registered), can_register: false, device_id: customerHistoryDeviceId(), promise: null};
                    dialog.hide();
                    frappe.show_alert({message: __("Workstation registered. Opening Customer History…"), indicator: "green"}, 4);
                    triggerOriginalCustomerHistory(frm);
                } catch (error) {
                    frappe.msgprint({title: __("Workstation registration failed"), message: esc(errorMessage(error) || "Registration failed."), indicator: "red"});
                } finally {
                    dialog.get_primary_btn().prop("disabled", false).text("Register This Workstation");
                }
            }
        });
        window.__nktUI7AF4RegistrationDialog = dialog;
        dialog.$wrapper.on("hidden.bs.modal.nktUI7A", () => { window.__nktUI7AF4RegistrationDialog = null; });
        dialog.show();
    }

    function makeLinkControl(host, fieldname, label, options) {
        const control = frappe.ui.form.make_control({
            parent: host,
            render_input: true,
            df: {fieldname, label, fieldtype: "Link", options}
        });
        control.refresh();
        return control;
    }

    function overlayMarkup(mode) {
        const secondary = mode === "cashier" ? "Shift Date" : "Encoded Date";
        const returnLabel = mode === "cashier" ? "Return to Sale" : "Return to Order";
        return `
          <div class="nkt-th-workspace" role="region" aria-label="Transaction History">
            <div class="nkt-th-titlebar">
              <div class="nkt-th-heading">
                <strong data-role="title">TRANSACTION HISTORY</strong>
                <span>F8 · ↑/↓ select · Enter expands · Double-click opens view-only transaction</span>
              </div>
              <div class="nkt-th-title-actions">
                <button type="button" data-action="expand-all" title="Expand or collapse all loaded transactions">Expand All</button>
                <button type="button" data-action="print">Print</button>
                <button type="button" data-action="close" class="primary">${returnLabel}</button>
              </div>
            </div>
            <div class="nkt-th-scopebar">
              <span data-role="scope-text">Loading access scope…</span>
              <span class="nkt-th-key-note">F1 / F7 reserved</span>
            </div>
            <div class="nkt-th-filter-panel">
              <div class="nkt-th-filter-caption"><b>FILTERS</b><span>Choose any combination, then Load History.</span></div>
              <div class="nkt-th-filters">
                <div class="nkt-th-field span-4"><div data-control="customer"></div></div>
                <div class="nkt-th-field span-4"><div data-control="item"></div></div>
                <div class="nkt-th-field span-3"><label>From Date / Time</label><input type="datetime-local" step="1" data-filter="from_datetime"></div>
                <div class="nkt-th-field span-3"><label>To Date / Time</label><input type="datetime-local" step="1" data-filter="to_datetime"></div>
                <div class="nkt-th-filter-actions span-2">
                  <button type="button" data-action="clear-filters">Clear</button>
                  <button type="button" data-action="load" class="primary">Load History</button>
                </div>
                <div class="nkt-th-field span-2"><label>Net Amount From</label><input type="number" step="0.01" data-filter="amount_from"></div>
                <div class="nkt-th-field span-2"><label>Net Amount To</label><input type="number" step="0.01" data-filter="amount_to"></div>
                <div class="nkt-th-field span-2"><label>Plate Number</label><input data-filter="plate_number" placeholder="Contains…"></div>
                <div class="nkt-th-field span-2"><label>OS#</label><input data-filter="os_no" placeholder="Contains…"></div>
                <div class="nkt-th-field span-2"><label>Account</label><select data-filter="account"><option>All</option><option>Yes</option><option>No</option></select></div>
                <div class="nkt-th-field span-2"><label>Payment</label><select data-filter="payment"><option value="">All</option></select></div>
                <div class="nkt-th-field span-2"><label>Status</label><input data-filter="status" placeholder="All statuses"></div>
                <div class="nkt-th-field span-2"><label>${secondary}</label><input type="date" data-filter="secondary_date"></div>
                <div class="nkt-th-field span-2" data-role="user-filter-wrap"><label>User</label><select data-filter="user"><option value="">All Users</option></select></div>
                <div class="nkt-th-field span-2"><label>Sort</label><select data-filter="sort_order"><option>Newest First</option><option>Oldest First</option></select></div>
              </div>
            </div>
            <div class="nkt-th-warning" data-role="warning" hidden></div>
            <div class="nkt-th-summary">
              <div class="nkt-th-metric"><span>Rows</span><b data-summary="rows">0</b></div>
              <div class="nkt-th-metric"><span>Gross</span><b data-summary="gross">0.00</b></div>
              <div class="nkt-th-metric"><span>Adjustment</span><b data-summary="adjustment">0.00</b></div>
              <div class="nkt-th-metric emphasized"><span>Net</span><b data-summary="net">0.00</b></div>
              <div class="nkt-th-metric"><span>Account</span><b data-summary="account">0.00</b></div>
              <span class="nkt-th-status" data-role="status">Ready</span>
            </div>
            <div class="nkt-th-table-wrap">
              <table class="nkt-th-table">
                <colgroup>
                  <col class="nkt-th-col-expander"><col class="nkt-th-col-datetime"><col class="nkt-th-col-customer">
                  <col class="nkt-th-col-money"><col class="nkt-th-col-adjustment"><col class="nkt-th-col-money">
                  <col class="nkt-th-col-account"><col class="nkt-th-col-payment"><col class="nkt-th-col-secondary">
                  <col class="nkt-th-col-user"><col class="nkt-th-col-status">
                </colgroup>
                <thead><tr>
                  <th class="nkt-th-expander-cell" aria-label="Expand"></th><th>Date / Time</th><th>Customer</th><th class="num">Gross</th>
                  <th class="num" title="Total Price Adjustment">Price Adj.</th><th class="num">Net</th><th class="center">Account</th>
                  <th>Payment</th><th>${secondary}</th><th>User</th><th>Status</th>
                </tr></thead>
                <tbody data-role="rows"><tr class="empty"><td colspan="11">Load Transaction History.</td></tr></tbody>
              </table>
            </div>
            <div class="nkt-th-footer"><span data-role="range">0 rows loaded</span><button type="button" data-action="load-more" hidden>Load More</button></div>
          </div>`;
    }

    async function openHistory(frm) {
        if (!activeForm(frm) || document.getElementById(OVERLAY_ID)) return;
        const mode = modeFor(frm);
        const overlay = document.createElement("div");
        overlay.id = OVERLAY_ID;
        overlay.className = "nkt-th-overlay";
        overlay.innerHTML = overlayMarkup(mode);
        document.body.appendChild(overlay);
        document.body.classList.add("nkt-th-open");
        const root = $(overlay);
        const state = {
            frm, mode, root, bootstrap: null, rows: [], nextStart: 0, hasMore: false,
            busy: false, expanded: new Set(), selectedRowId: "", expandAllActive: false, customerControl: null, itemControl: null,
            keyHandler: null, lastFocus: document.activeElement
        };
        overlay.__nktTransactionHistoryState = state;
        bindWorkspace(state);
        setStatus(state, "Loading access and defaults…");
        try {
            state.bootstrap = await call("get_transaction_history_bootstrap", {mode});
            applyBootstrap(state);
            await loadRows(state, true);
        } catch (error) {
            showWarning(state, errorMessage(error) || "Transaction History is unavailable.");
            setStatus(state, "Unavailable");
        }
    }

    function bindWorkspace(state) {
        const root = state.root;
        state.customerControl = makeLinkControl(root.find('[data-control="customer"]')[0], "customer", "Customer", "Customer");
        state.itemControl = makeLinkControl(root.find('[data-control="item"]')[0], "item_code", "Item", "Item");
        root.on("click.nktUI6", '[data-action="close"]', () => closeHistory(state));
        root.on("click.nktUI6", '[data-action="load"]', () => loadRows(state, true));
        root.on("click.nktUI6", '[data-action="load-more"]', () => loadRows(state, false));
        root.on("click.nktUI6", '[data-action="clear-filters"]', () => clearFilters(state));
        root.on("click.nktUI6", '[data-action="print"]', () => openPrintSettings(state));
        root.on("click.nktUI6", '[data-action="expand-all"]', () => toggleAllRows(state));
        root.on("click.nktUI6", ".nkt-th-expander-cell", function (e) {
            e.preventDefault();
            e.stopPropagation();
            const rowId = String($(this).closest("tr").data("row-id") || "");
            selectRow(state, rowId, false);
            toggleRow(state, rowId);
        });
        root.on("click.nktUI6", "tr.nkt-th-summary-row", function () {
            selectRow(state, String($(this).data("row-id") || ""), true);
        });
        root.on("dblclick.nktUI6", "tr.nkt-th-summary-row", function (e) {
            if ($(e.target).closest(".nkt-th-expander-cell").length) return;
            e.preventDefault();
            const rowId = String($(this).data("row-id") || "");
            selectRow(state, rowId, false);
            openTransactionView(state, rowId);
        });
        root.on("click.nktUI6", "tr.nkt-th-detail-row", function () {
            selectRow(state, String($(this).data("detail-for") || ""), true);
        });
        root.on("keydown.nktUI6", "tr.nkt-th-summary-row", function (e) {
            handleRowKeydown(state, e, String($(this).data("row-id") || ""));
        });
        state.keyHandler = (e) => {
            if (!document.getElementById(OVERLAY_ID)) return;
            if (document.getElementById(VIEW_LAYER_ID) || document.getElementById(RECEIPT_LAYER_ID)) return;
            if (isReservedFunctionKey(e)) { consume(e); return; }
            if (keyIs(e, 8)) { consume(e); return; }
            if ((e.ctrlKey || e.metaKey) && String(e.key || "").toLowerCase() === "p") { consume(e); openPrintSettings(state); return; }
            if (e.key === "Escape") {
                consume(e);
                if (document.getElementById(PRINT_LAYER_ID)) closePrintSettings();
                else closeHistory(state);
            }
        };
        window.addEventListener("keydown", state.keyHandler, true);
    }

    function applyBootstrap(state) {
        const boot = state.bootstrap || {};
        state.root.find('[data-role="title"]').text(String(boot.title || "Transaction History").toUpperCase());
        state.root.find('[data-role="scope-text"]').text(boot.own_only ? `Showing only ${boot.current_user?.full_name || "your"} transactions.` : "Manager / Owner / Administrator may review all authorized users.");
        const payment = state.root.find('[data-filter="payment"]');
        (boot.payment_options || []).forEach((value) => payment.append(`<option value="${escAttr(value)}">${esc(value)}</option>`));
        const userWrap = state.root.find('[data-role="user-filter-wrap"]');
        const userSelect = state.root.find('[data-filter="user"]');
        if (boot.own_only) {
            userWrap.hide();
            userSelect.val(boot.scope_user || "");
        } else {
            (boot.users || []).forEach((user) => userSelect.append(`<option value="${escAttr(user.user)}">${esc(user.full_name)} (${esc(user.display_name)})</option>`));
        }
        const defaults = boot.default_period || {};
        state.root.find('[data-filter="from_datetime"]').val(toLocalInput(defaults.from_datetime));
        state.root.find('[data-filter="to_datetime"]').val(toLocalInput(defaults.to_datetime));
        if (defaults.secondary_date) state.root.find('[data-filter="secondary_date"]').val(defaults.secondary_date);
    }

    function toLocalInput(value) {
        return String(value || "").replace(" ", "T").slice(0, 19);
    }

    function filters(state, pageStart = 0) {
        const value = (name) => String(state.root.find(`[data-filter="${name}"]`).val() || "").trim();
        return {
            customer: String(state.customerControl?.get_value?.() || "").trim(),
            item_code: String(state.itemControl?.get_value?.() || "").trim(),
            plate_number: value("plate_number"),
            os_no: value("os_no"),
            from_datetime: value("from_datetime"),
            to_datetime: value("to_datetime"),
            amount_from: value("amount_from"),
            amount_to: value("amount_to"),
            account: value("account") || "All",
            payment: value("payment"),
            status: value("status"),
            secondary_date: value("secondary_date"),
            user: state.bootstrap?.own_only ? String(state.bootstrap.scope_user || "") : value("user"),
            sort_order: value("sort_order") || "Newest First",
            page_start: pageStart,
            page_length: PAGE_LENGTH
        };
    }

    async function loadRows(state, reset) {
        if (state.busy) return;
        state.busy = true;
        setStatus(state, "Loading…");
        state.root.find('[data-action="load"],[data-action="load-more"]').prop("disabled", true);
        const oldLength = state.rows.length;
        try {
            const result = await call("get_transaction_history", {filters: filters(state, reset ? 0 : state.nextStart), mode: state.mode});
            const incoming = result.rows || [];
            if (reset) {
                state.rows = incoming;
                state.expanded.clear();
                state.expandAllActive = false;
                state.selectedRowId = state.rows.length ? String(state.rows[0].row_id || "") : "";
            } else {
                state.rows.push(...incoming);
                if (state.expandAllActive) incoming.forEach((row) => state.expanded.add(String(row.row_id || "")));
            }
            state.nextStart = Number(result.next_start || state.rows.length);
            state.hasMore = Boolean(result.has_more);
            renderRows(state);
            renderSummary(state, result.summary || {});
            showWarning(state, result.warning || "");
            setStatus(state, `Loaded ${state.rows.length}${state.hasMore ? "+" : ""}`);
        } catch (error) {
            showWarning(state, errorMessage(error) || "Could not load Transaction History.");
            setStatus(state, "Load failed");
        } finally {
            state.busy = false;
            state.root.find('[data-action="load"],[data-action="load-more"]').prop("disabled", false);
        }
    }

    function renderRows(state) {
        const body = state.root.find('[data-role="rows"]');
        if (!state.rows.length) {
            state.selectedRowId = "";
            body.html('<tr class="empty"><td colspan="11">No matching transactions.</td></tr>');
        } else {
            const ids = new Set(state.rows.map((row) => String(row.row_id || "")));
            if (!state.selectedRowId || !ids.has(state.selectedRowId)) state.selectedRowId = String(state.rows[0].row_id || "");
            body.html(state.rows.map((row) => {
                const rowId = String(row.row_id || "");
                return rowMarkup(row, state.expanded.has(rowId), rowId === state.selectedRowId, state.mode);
            }).join(""));
        }
        state.root.find('[data-action="load-more"]').prop("hidden", !state.hasMore);
        state.root.find('[data-role="range"]').text(`${state.rows.length} row${state.rows.length === 1 ? "" : "s"} loaded`);
        updateExpandAllButton(state);
    }

    function rowMarkup(row, expanded, selected, mode) {
        const account = row.account_flag ? `<span class="nkt-th-check" title="Account amount ${money(row.account_amount)}">✓</span>` : "";
        const kind = `<span class="nkt-th-kind">${esc(row.kind || "Transaction")}</span>`;
        return `
          <tr class="nkt-th-summary-row${selected ? " selected" : ""}" tabindex="0" data-row-id="${escAttr(row.row_id)}" aria-expanded="${expanded ? "true" : "false"}" aria-selected="${selected ? "true" : "false"}">
            <td class="nkt-th-expander-cell"><span class="nkt-th-expander-glyph" aria-hidden="true">${expanded ? "−" : "+"}</span></td>
            <td class="nkt-th-datetime-cell"><span class="nkt-th-primary-text">${esc(row.transaction_datetime)}</span>${kind}</td>
            <td class="nkt-th-customer-cell" title="${escAttr(row.customer_name || row.customer)}">${esc(row.customer_name || row.customer)}</td>
            <td class="num">${money(row.gross_amount)}</td>
            <td class="num">${signedAdjustment(row.price_adjustment)}</td>
            <td class="num strong">${money(row.net_amount)}</td>
            <td class="center">${account}</td>
            <td class="nkt-th-payment-cell">${esc(row.payment_label || "")}</td>
            <td class="nkt-th-secondary-cell">${esc(row.secondary_date || "")}</td>
            <td class="nkt-th-user-cell" title="${escAttr(row.user_full_name || row.user)}">${esc(row.user_display || row.user)}</td>
            <td>${statusBadge(row.status)}</td>
          </tr>
          <tr class="nkt-th-detail-row" data-detail-for="${escAttr(row.row_id)}" ${expanded ? "" : "hidden"}><td colspan="11">${detailMarkup(row, mode)}</td></tr>`;
    }

    function statusBadge(value) {
        const text = String(value || "");
        const lower = text.toLowerCase();
        let cls = "active";
        if (lower.includes("cancel") || lower.includes("revers")) cls = "bad";
        else if (lower.includes("pending") || lower.includes("unmatched") || lower.includes("partial")) cls = "warn";
        return `<span class="nkt-th-status-badge ${cls}">${esc(text)}</span>`;
    }

    function inlinePaymentText(payment, showCashTender = false) {
        const method = String(payment.method || "Payment");
        const parts = [`${method} ₱${money(payment.amount)}`];
        if (showCashTender && method.toUpperCase() === "CASH") {
            const tendered = number(payment.cash_tendered);
            const change = number(payment.change_amount);
            const recorded = Boolean(payment.cash_tender_recorded) || tendered > 0.000001 || change > 0.000001;
            if (recorded) {
                parts.push(`Tendered ₱${money(tendered)}`);
                parts.push(`Change ₱${money(change)}`);
            }
        }
        return parts.join(" · ");
    }

    function detailMarkup(row, mode) {
        const items = row.items || [];
        const payments = row.payments || [];
        const itemLines = items.map((item) => `
          <div class="nkt-th-compact-item" title="${escAttr(item.item_code || item.item_name)}">
            <span class="nkt-th-compact-name">${esc(item.item_name || item.item_code)}</span>
            <span class="nkt-th-compact-qty">${esc(item.quantity)} ${esc(item.uom)}</span>
            <span class="nkt-th-compact-rate">@ ₱${money(item.rate)}</span>
            <span class="nkt-th-compact-warehouse">${esc(item.warehouse)}</span>
          </div>`).join("");
        const paymentLines = payments.map((payment) => `<span class="nkt-th-compact-payment">${esc(inlinePaymentText(payment, mode === "cashier"))}</span>`).join("");
        if (!itemLines && !paymentLines) return '<div class="nkt-th-compact-empty">No item or payment detail recorded.</div>';
        return `<div class="nkt-th-compact-detail">${itemLines}${paymentLines ? `<div class="nkt-th-compact-payments">${paymentLines}</div>` : ""}</div>`;
    }

    function humanize(value) {
        return String(value || "").replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
    }

    function rowIds(state) {
        return state.rows.map((row) => String(row.row_id || "")).filter(Boolean);
    }

    function navigationIndex(current, length, key) {
        if (length <= 0) return -1;
        const safe = current < 0 ? 0 : Math.min(current, length - 1);
        if (key === "ArrowDown") return Math.min(length - 1, safe + 1);
        if (key === "ArrowUp") return Math.max(0, safe - 1);
        if (key === "PageDown") return Math.min(length - 1, safe + 10);
        if (key === "PageUp") return Math.max(0, safe - 10);
        if (key === "Home") return 0;
        if (key === "End") return length - 1;
        return safe;
    }

    function selectRow(state, rowId, focus) {
        if (!rowId) return;
        state.selectedRowId = String(rowId);
        state.root.find("tr.nkt-th-summary-row").removeClass("selected").attr("aria-selected", "false");
        const row = state.root.find(`tr[data-row-id="${cssEscape(rowId)}"]`).first();
        row.addClass("selected").attr("aria-selected", "true");
        if (focus && row.length) {
            const element = row[0];
            try { element.focus({preventScroll: true}); } catch (_error) { element.focus(); }
            element.scrollIntoView?.({block: "nearest", inline: "nearest"});
        }
    }

    function setRowExpanded(state, rowId, expanded) {
        if (!rowId) return;
        if (expanded) state.expanded.add(String(rowId));
        else state.expanded.delete(String(rowId));
        state.expandAllActive = rowIds(state).length > 0 && rowIds(state).every((id) => state.expanded.has(id));
        state.selectedRowId = String(rowId);
        renderRows(state);
        selectRow(state, rowId, true);
    }

    function toggleRow(state, rowId) {
        if (!rowId) return;
        setRowExpanded(state, rowId, !state.expanded.has(String(rowId)));
    }

    function updateExpandAllButton(state) {
        const ids = rowIds(state);
        const allExpanded = ids.length > 0 && ids.every((id) => state.expanded.has(id));
        state.expandAllActive = allExpanded;
        state.root.find('[data-action="expand-all"]').prop("disabled", !ids.length).text(allExpanded ? "Collapse All" : "Expand All");
    }

    function toggleAllRows(state) {
        const ids = rowIds(state);
        if (!ids.length) return;
        const allExpanded = ids.every((id) => state.expanded.has(id));
        state.expanded = allExpanded ? new Set() : new Set(ids);
        state.expandAllActive = !allExpanded;
        if (!state.selectedRowId) state.selectedRowId = ids[0];
        renderRows(state);
        selectRow(state, state.selectedRowId, true);
    }

    function moveSelection(state, key) {
        const ids = rowIds(state);
        if (!ids.length) return;
        const current = ids.indexOf(state.selectedRowId);
        const targetIndex = navigationIndex(current, ids.length, key);
        const movingPastEnd = current === ids.length - 1 && targetIndex === ids.length - 1 && ["ArrowDown", "PageDown", "End"].includes(key);
        if (movingPastEnd && state.hasMore && !state.busy) {
            const priorLength = state.rows.length;
            loadRows(state, false).then(() => {
                const next = String(state.rows[Math.min(priorLength, state.rows.length - 1)]?.row_id || "");
                if (next) selectRow(state, next, true);
            });
            return;
        }
        const target = ids[targetIndex];
        if (target) selectRow(state, target, true);
    }

    function handleRowKeydown(state, e, rowId) {
        const key = String(e.key || "");
        if (["ArrowDown", "ArrowUp", "PageDown", "PageUp", "Home", "End"].includes(key)) {
            e.preventDefault();
            e.stopPropagation();
            selectRow(state, rowId, false);
            moveSelection(state, key);
            return;
        }
        if (key === "Enter" || key === " ") {
            e.preventDefault();
            e.stopPropagation();
            toggleRow(state, rowId);
            return;
        }
        if (key === "ArrowRight") {
            e.preventDefault();
            setRowExpanded(state, rowId, true);
            return;
        }
        if (key === "ArrowLeft") {
            e.preventDefault();
            setRowExpanded(state, rowId, false);
        }
    }

    function cssEscape(value) {
        if (window.CSS?.escape) return CSS.escape(value);
        return String(value).replace(/(["'\\.#:[\]()=])/g, "\\$1");
    }

    function renderSummary(state, summary) {
        state.root.find('[data-summary="rows"]').text(Number(summary.row_count || 0).toLocaleString());
        state.root.find('[data-summary="gross"]').text(money(summary.gross_total));
        state.root.find('[data-summary="adjustment"]').text(signedAdjustment(summary.price_adjustment_total));
        state.root.find('[data-summary="net"]').text(money(summary.net_total));
        state.root.find('[data-summary="account"]').text(money(summary.account_total));
    }

    function clearFilters(state) {
        state.customerControl?.set_value?.("");
        state.itemControl?.set_value?.("");
        state.root.find('[data-filter="amount_from"],[data-filter="amount_to"],[data-filter="plate_number"],[data-filter="os_no"],[data-filter="status"],[data-filter="secondary_date"]').val("");
        state.root.find('[data-filter="account"]').val("All");
        state.root.find('[data-filter="payment"]').val("");
        state.root.find('[data-filter="sort_order"]').val("Newest First");
        if (!state.bootstrap?.own_only) state.root.find('[data-filter="user"]').val("");
        const defaults = state.bootstrap?.default_period || {};
        state.root.find('[data-filter="from_datetime"]').val(toLocalInput(defaults.from_datetime));
        state.root.find('[data-filter="to_datetime"]').val(toLocalInput(defaults.to_datetime));
    }

    function setStatus(state, text) {
        state.root.find('[data-role="status"]').text(text);
    }

    function showWarning(state, text) {
        const box = state.root.find('[data-role="warning"]');
        if (text) box.text(text).prop("hidden", false);
        else box.text("").prop("hidden", true);
    }

    function errorMessage(error) {
        return String(error?.message || error?.exc || error?._server_messages || "").replace(/^.*?frappe\.throw\(/, "").slice(0, 700);
    }

    function publicPaymentId(value) {
        const text = String(value || "").trim();
        const match = text.match(/^NKT-PAY-(\d+)$/);
        return match ? `P${String(Number(match[1])).padStart(6, "0")}` : text;
    }

    function displayReference(value, explicit = "") {
        return String(explicit || publicPaymentId(value) || "");
    }


    function rowFor(state, rowId) {
        return (state.rows || []).find((row) => String(row.row_id || "") === String(rowId || "")) || null;
    }

    async function openTransactionView(state, rowId) {
        if (!rowId || document.getElementById(VIEW_LAYER_ID)) return;
        const selected = rowFor(state, rowId);
        if (!selected) return;
        const layer = document.createElement("div");
        layer.id = VIEW_LAYER_ID;
        layer.className = "nkt-ui7a-view-layer";
        layer.innerHTML = `<div class="nkt-ui7a-view-loading"><b>Opening posted transaction…</b><span>${esc(displayReference(selected.source_name || selected.row_id, selected.display_source_name))}</span></div>`;
        document.body.appendChild(layer);
        try {
            const access = await call("get_transaction_view_access", {mode: state.mode, row_id: rowId});
            renderTransactionView(state, selected, access);
        } catch (error) {
            layer.remove();
            frappe.msgprint({title: __("Transaction could not be opened"), message: esc(errorMessage(error) || "The selected transaction is unavailable."), indicator: "red"});
        }
    }

    function transactionKindLabel(view) {
        if (view.receipt_kind === "account_payment") return "PAYMENT ON ACCOUNT";
        if (view.receipt_kind === "customer_advance") return "CUSTOMER ADVANCE";
        return view.source_doctype === "NKT Cashier Sale" ? "CASHIER SALE" : "CUSTOMER ORDER";
    }

    function viewPaymentMarkup(view, mode) {
        const rows = view.payments || [];
        if (!rows.length) return `<div class="nkt-ui7a-empty-line">No payment detail recorded.</div>`;
        return rows.map((payment) => {
            const main = inlinePaymentText(payment, mode === "cashier");
            const extras = [];
            if (payment.reference_number) extras.push(`Ref ${payment.reference_number}`);
            if (payment.check_number) extras.push(`Check ${payment.check_number}`);
            if (payment.check_date) extras.push(payment.check_date);
            if (payment.bank_or_provider) extras.push(payment.bank_or_provider);
            return `<div class="nkt-ui7a-payment-line"><b>${esc(main)}</b>${extras.length ? `<span>${esc(extras.join(" · "))}</span>` : ""}</div>`;
        }).join("");
    }

    function viewItemsMarkup(view) {
        const items = view.items || [];
        if (!items.length) return `<div class="nkt-ui7a-empty-line">No item lines on this transaction.</div>`;
        return `<table class="nkt-ui7a-view-grid"><thead><tr><th>#</th><th>Item</th><th>Qty / UOM</th><th class="num">Rate</th><th class="num">Amount</th><th>Warehouse</th></tr></thead><tbody>${items.map((item, index) => `
          <tr><td>${index + 1}</td><td><b>${esc(item.item_name || item.item_code)}</b>${item.description && item.description !== item.item_name ? `<small>${esc(item.description)}</small>` : ""}</td><td>${esc(item.quantity)} ${esc(item.uom)}</td><td class="num">₱${money(item.rate)}</td><td class="num">₱${money(item.amount)}</td><td>${esc(item.warehouse)}</td></tr>`).join("")}</tbody></table>`;
    }

    function viewReferenceMarkup(view) {
        const values = [];
        if (view.os_no) values.push(`<b>OS#:</b> ${esc(view.os_no)}`);
        if (view.plate_reference) values.push(`<b>Plate:</b> ${esc(view.plate_reference)}`);
        if (view.dr_reference) values.push(`<b>DR:</b> ${esc(view.dr_reference)}`);
        if (view.remarks) values.push(`<b>Remarks:</b> ${esc(view.remarks)}`);
        return values.length ? `<div class="nkt-ui7a-reference-strip">${values.join("<span class=\"sep\">•</span>")}</div>` : "";
    }

    function renderTransactionView(state, selected, access) {
        const layer = document.getElementById(VIEW_LAYER_ID);
        if (!layer) return;
        const view = access.view || {};
        const kind = transactionKindLabel(view);
        const returnAction = access.can_start_return_exchange
            ? `<button type="button" data-view-action="return" class="warn">Start Return / Exchange</button>`
            : "";
        const printAction = access.can_reprint
            ? `<button type="button" data-view-action="reprint">Print / Reprint Receipt</button>`
            : "";
        const accountInfo = view.receipt_kind === "customer_advance"
            ? `<div><span>Original Advance</span><b>₱${money(view.customer_advance_amount)}</b></div><div><span>Applied</span><b>₱${money(view.advance_applied_amount)}</b></div><div><span>Available</span><b>₱${money(view.advance_available_amount)}</b></div>`
            : `<div><span>Previous Account Balance</span><b>₱${money(view.previous_account_balance)}</b></div><div><span>Account on Transaction</span><b>₱${money(view.account_amount)}</b></div><div><span>Total Account Balance</span><b>₱${money(view.total_account_balance)}</b></div>`;
        layer.innerHTML = `
          <div class="nkt-ui7a-view-screen" role="dialog" aria-modal="true" aria-label="View-only posted transaction">
            <div class="nkt-ui7a-view-titlebar">
              <div><strong>${esc(kind)}</strong><span>${esc(displayReference(view.source_name || access.source_name, view.display_source_name))}</span></div>
              <div class="nkt-ui7a-view-actions">${returnAction}${printAction}<button type="button" data-view-action="back" class="primary">Back to History</button></div>
            </div>
            <div class="nkt-ui7a-view-banner">VIEW ONLY — POSTED TRANSACTION</div>
            <div class="nkt-ui7a-view-meta">
              <div><span>Customer</span><b>${esc(view.customer_name || view.customer || "—")}</b></div>
              <div><span>Receipt / Order</span><b>${esc(view.receipt_number || view.source_name || "—")}</b></div>
              <div><span>Encoded At</span><b>${esc(view.transaction_datetime || "—")}</b></div>
              <div><span>Encoded By</span><b title="${escAttr(view.operator_identity?.full_name || view.operator)}">${esc(view.operator_identity?.display_name || view.operator || "—")}</b></div>
              <div><span>Status</span><b>${esc(view.status || selected.status || "—")}</b></div>
              <div><span>Shift</span><b>${esc(view.cashier_shift || "—")}</b></div>
            </div>
            <div class="nkt-ui7a-section-title">ORIGINAL TRANSACTION</div>
            <div class="nkt-ui7a-view-items">${viewItemsMarkup(view)}</div>
            ${viewReferenceMarkup(view)}
            <div class="nkt-ui7a-view-lower">
              <div class="nkt-ui7a-view-payments"><div class="nkt-ui7a-subtitle">PAYMENT</div>${viewPaymentMarkup(view, state.mode)}</div>
              <div class="nkt-ui7a-view-totals">
                <div><span>Gross</span><b>₱${money(view.gross_total)}</b></div>
                <div><span>Price Adjustment</span><b>${signedAdjustment(view.price_adjustment)}</b></div>
                <div class="grand"><span>Net / Receipt Total</span><b>₱${money(view.receipt_total)}</b></div>
              </div>
            </div>
            <div class="nkt-ui7a-account-strip">${accountInfo}</div>
            <div class="nkt-ui7a-view-footer"><span>Nothing on this screen can be edited, added, paid, saved, or finalized.</span><span>Esc returns to Transaction History.</span></div>
          </div>`;
        const $layer = $(layer);
        $layer.on("click.nktUI7A", '[data-view-action="back"]', () => closeTransactionView(state));
        $layer.on("click.nktUI7A", '[data-view-action="return"]', () => startReturnExchange(state, access));
        $layer.on("click.nktUI7A", '[data-view-action="reprint"]', () => openReceiptSettings(state, access));
        const keyHandler = (e) => {
            if (!document.getElementById(VIEW_LAYER_ID)) return;
            if (document.getElementById(RECEIPT_LAYER_ID)) return;
            if (e.key === "Escape") { consume(e); closeTransactionView(state); return; }
            if ([1,2,3,4,5,6,7,8,10,11,12].some((n) => keyIs(e, n))) consume(e);
        };
        layer.__nktUI7AKeyHandler = keyHandler;
        window.addEventListener("keydown", keyHandler, true);
        $layer.find('[data-view-action="back"]').trigger("focus");
    }

    function closeTransactionView(state) {
        closeReceiptSettings();
        const layer = document.getElementById(VIEW_LAYER_ID);
        if (!layer) return;
        if (layer.__nktUI7AKeyHandler) window.removeEventListener("keydown", layer.__nktUI7AKeyHandler, true);
        $(layer).off(".nktUI7A");
        layer.remove();
        if (state?.selectedRowId) selectRow(state, state.selectedRowId, true);
    }

    function startReturnExchange(state, access) {
        const info = access.return_exchange || {};
        if (!access.can_start_return_exchange || !info.source_name) {
            frappe.msgprint(info.reason || "This transaction is not eligible for another return or exchange.");
            return;
        }
        const side = info.side === "cashier" ? "cashier" : "encoder";
        const key = `${RETURN_PREFILL_PREFIX}${side}`;
        const payload = {
            version: "UI7A",
            side,
            source_name: info.source_name,
            requested_by: window.frappe?.session?.user || "",
            issued_at: new Date().toISOString(),
            expires_at: Date.now() + 10 * 60 * 1000
        };
        try { localStorage.setItem(key, JSON.stringify(payload)); }
        catch (_error) {
            frappe.msgprint("The browser could not prepare the Return / Exchange handoff.");
            return;
        }
        const route = side === "cashier" ? "/app/nkt-cashier-return-exchange" : "/app/nkt-encoder-return-exchange";
        const popup = window.open(`${window.location.origin}${route}`, "_blank");
        if (!popup) {
            try { localStorage.removeItem(key); } catch (_error) {}
            frappe.msgprint("The browser blocked the Return / Exchange window. Allow pop-ups for the NKT site, then try again.");
            return;
        }
        try { popup.opener = null; } catch (_error) {}
    }

    function openReceiptSettings(state, access) {
        if (document.getElementById(RECEIPT_LAYER_ID)) return;
        const layer = document.createElement("div");
        layer.id = RECEIPT_LAYER_ID;
        layer.className = "nkt-ui7a-receipt-layer";
        layer.innerHTML = `
          <div class="nkt-ui7a-receipt-panel" role="dialog" aria-modal="true" aria-label="Receipt Reprint Settings">
            <div class="nkt-ui7a-receipt-title"><b>Print / Reprint Receipt</b><span>Historical copies are always marked REPRINTED.</span></div>
            <label>Paper Size<select data-receipt="paper"><option value="half_short">Half Short Bond 8.5 × 5.5 Landscape</option><option value="a5">A5 210 × 148 mm Landscape</option></select></label>
            ${access?.view?.plate_reference ? '<label class="nkt-th-print-check"><input type="checkbox" data-receipt="plate" checked> Include Plate Number</label>' : ''}
            ${access?.view?.os_no ? '<label class="nkt-th-print-check"><input type="checkbox" data-receipt="os" checked> Include OS#</label>' : ''}
            <div class="nkt-ui7a-receipt-note">Cashier cannot reprint. Encoder, Store Manager, Owner, and NKT Administrator reprints are audited internally.</div>
            <div class="nkt-ui7a-receipt-actions"><button type="button" data-receipt-action="cancel">Cancel</button><button type="button" data-receipt-action="prepare" class="primary">Prepare Reprint</button></div>
          </div>`;
        document.body.appendChild(layer);
        const $layer = $(layer);
        $layer.on("click.nktUI7A", '[data-receipt-action="cancel"]', closeReceiptSettings);
        $layer.on("click.nktUI7A", '[data-receipt-action="prepare"]', () => prepareReceiptReprint(state, access, $layer));
        const keyHandler = (e) => {
            if (!document.getElementById(RECEIPT_LAYER_ID)) return;
            if (e.key === "Escape") { consume(e); closeReceiptSettings(); return; }
            if ([1,2,3,4,5,6,7,8,10,11,12].some((n) => keyIs(e, n))) consume(e);
        };
        layer.__nktUI7AKeyHandler = keyHandler;
        window.addEventListener("keydown", keyHandler, true);
        $layer.find('[data-receipt="paper"]').trigger("focus");
    }

    function closeReceiptSettings() {
        const layer = document.getElementById(RECEIPT_LAYER_ID);
        if (!layer) return;
        if (layer.__nktUI7AKeyHandler) window.removeEventListener("keydown", layer.__nktUI7AKeyHandler, true);
        $(layer).off(".nktUI7A");
        layer.remove();
        const view = document.getElementById(VIEW_LAYER_ID);
        if (view) $(view).find('[data-view-action="reprint"]').trigger("focus");
    }

    async function prepareReceiptReprint(state, access, layer) {
        const button = layer.find('[data-receipt-action="prepare"]');
        const popup = window.open("", "_blank");
        if (!popup) {
            frappe.msgprint("The browser blocked the receipt window. Allow pop-ups for the NKT site, then try again.");
            return;
        }
        button.prop("disabled", true).text("Preparing…");
        try {
            popup.document.write("<p style='font-family:Arial'>Preparing receipt…</p>");
            const result = await call("prepare_transaction_receipt_reprint", {
                mode: state.mode,
                row_id: access.row_id,
                paper_size: String(layer.find('[data-receipt="paper"]').val() || "half_short"),
                device_id: customerHistoryDeviceId(),
                include_plate_number: layer.find('[data-receipt="plate"]').length ? (layer.find('[data-receipt="plate"]').prop("checked") ? 1 : 0) : 0,
                include_os_no: layer.find('[data-receipt="os"]').length ? (layer.find('[data-receipt="os"]').prop("checked") ? 1 : 0) : 0
            });
            closeReceiptSettings();
            try { popup.opener = null; } catch (_error) {}
            popup.document.open();
            popup.document.write(receiptPrintHtml(result));
            popup.document.close();
            popup.focus();
            setTimeout(() => popup.print(), 250);
        } catch (error) {
            try { popup.close(); } catch (_error) {}
            frappe.msgprint({title: __("Receipt reprint could not be prepared"), message: esc(errorMessage(error) || "Receipt reprint failed."), indicator: "red"});
        } finally {
            button.prop("disabled", false).text("Prepare Reprint");
        }
    }

    function parenthesizedMoney(value) {
        const n = number(value);
        return n < -0.000001 ? `(₱${money(Math.abs(n))})` : `₱${money(n)}`;
    }

    function receiptPaymentLines(receipt) {
        const rows = receipt.payments || [];
        if (!rows.length) return "";
        return rows.map((payment) => {
            const parts = [`${String(payment.method || "Payment")}: ₱${money(payment.amount)}`];
            if (String(payment.method || "").toUpperCase() === "CASH" && payment.cash_tender_recorded) {
                parts.push(`Tendered ₱${money(payment.cash_tendered)}`);
                parts.push(`Change ₱${money(payment.change_amount)}`);
            }
            if (payment.reference_number) parts.push(`Ref ${payment.reference_number}`);
            if (payment.check_number) parts.push(`Check ${payment.check_number}`);
            if (payment.check_date) parts.push(payment.check_date);
            if (payment.bank_or_provider) parts.push(payment.bank_or_provider);
            return `<div>${esc(parts.join(" · "))}</div>`;
        }).join("");
    }

    function receiptPrintHtml(result) {
        const receipt = result.receipt || {};
        const page = String(result.paper?.page_css || "8.5in 5.5in landscape");
        const items = receipt.items || [];
        const itemRows = items.map((item) => `<tr><td>${esc(item.quantity)} ${esc(item.uom || "")}</td><td><b>${esc(item.item_name || item.item_code || "")}</b></td><td class="num">₱${money(item.rate)}</td><td class="num"><b>₱${money(item.amount)}</b></td></tr>`).join("");
        const refs = [receipt.plate_reference ? `Plate ${receipt.plate_reference}` : "", receipt.dr_reference ? `DR ${receipt.dr_reference}` : "", receipt.os_no ? `OS# ${receipt.os_no}` : "", receipt.remarks || ""].filter(Boolean).join(" · ");
        const accountLine = Math.abs(number(receipt.account_amount)) > 0.000001 ? `<div>Account: ${parenthesizedMoney(receipt.account_amount)}</div>` : "";
        return `<!doctype html><html><head><meta charset="utf-8"><title>REPRINTED ${esc(receipt.receipt_number || receipt.source_name)}</title><style>
          @page{size:${page};margin:.22in}*{box-sizing:border-box}body{font-family:"Courier New",monospace;color:#111;margin:0;font-size:9pt;line-height:1.12}.top{display:grid;grid-template-columns:1fr 2fr 1fr;align-items:start}.printed{font-size:8pt}.title{text-align:center;font-weight:bold;font-size:12pt;letter-spacing:.2px}.number{text-align:right;font-size:10pt}.reprinted{text-align:center;font-size:11pt;font-weight:bold;margin:11px 0 8px}.bill{margin:0 0 6px}.items{width:100%;border-collapse:collapse;table-layout:fixed}.items th,.items td{border-bottom:1px solid #444;padding:2px 4px;text-align:left}.items th:nth-child(1){width:12%}.items th:nth-child(3){width:19%}.items th:nth-child(4){width:22%}.num{text-align:right!important;font-variant-numeric:tabular-nums}.refs{min-height:20px;padding:5px 10%;font-style:italic}.lower{display:grid;grid-template-columns:1fr 1fr;gap:20px;margin-top:3px}.payments{padding-top:8px}.balances table{width:100%;border-collapse:collapse}.balances td{padding:1px 2px}.balances td:last-child{text-align:right;font-weight:bold}.receipt-total{border-top:1px solid #444;padding-top:3px}.signature{width:32%;margin-left:auto;margin-top:30px;border-top:1px solid #111;text-align:center;font-family:Arial,sans-serif;font-weight:bold;padding-top:2px}
        </style></head><body>
          <div class="top"><div class="printed">Printed: ${esc(result.generated_at)}</div><div class="title">TRUST RECEIPT AGREEMENT/ &nbsp; Sales Receipt</div><div class="number"><b>${esc(receipt.receipt_number || receipt.source_name || "")}</b><br>${esc(receipt.transaction_date || "")}</div></div>
          <div class="reprinted">REPRINTED</div>
          <div class="bill"><b>Bill To:</b> ${esc(receipt.customer_name || receipt.customer || "")}</div>
          <table class="items"><thead><tr><th>Qty</th><th>Item Name</th><th class="num">Price</th><th class="num">Ext. Price</th></tr></thead><tbody>${itemRows || `<tr><td colspan="4">No item detail recorded.</td></tr>`}</tbody></table>
          <div class="refs">${esc(refs)}</div>
          <div class="lower"><div class="payments">${accountLine}${receiptPaymentLines(receipt)}</div><div class="balances"><table><tr class="receipt-total"><td><b>RECEIPT TOTAL:</b></td><td>₱${money(receipt.receipt_total)}</td></tr><tr><td><b>Previous Acct Balance:</b></td><td>${parenthesizedMoney(receipt.previous_account_balance)}</td></tr><tr><td><b>TOTAL Acct Balance:</b></td><td>${parenthesizedMoney(receipt.total_account_balance)}</td></tr></table></div></div>
          <div class="signature">Signature Over Printed Name</div>
        </body></html>`;
    }

    function closeHistory(state) {
        closeTransactionView(state);
        closePrintSettings();
        if (state.keyHandler) window.removeEventListener("keydown", state.keyHandler, true);
        state.root.off(".nktUI6");
        document.getElementById(OVERLAY_ID)?.remove();
        document.body.classList.remove("nkt-th-open");
        setTimeout(() => {
            if (state.lastFocus && document.contains(state.lastFocus)) state.lastFocus.focus?.();
            else $(state.frm.wrapper).find('[data-role="item-entry"]').trigger("focus");
            ensureButton(state.frm);
        }, 0);
    }

    function openPrintSettings(state) {
        if (document.getElementById(PRINT_LAYER_ID)) return;
        const layer = document.createElement("div");
        layer.id = PRINT_LAYER_ID;
        layer.className = "nkt-th-print-layer";
        layer.innerHTML = `
          <div class="nkt-th-print-panel" role="dialog" aria-modal="true" aria-label="Transaction History Print Settings">
            <div class="nkt-th-print-title"><b>Print Transaction History</b><span>Choose paper, density, and details.</span></div>
            <label>Paper Size<select data-print="paper"><option value="long">Long Bond 8.5 x 13 Portrait</option><option value="short">Short Bond Letter Portrait</option><option value="a4">A4 Portrait</option></select></label>
            <label>Font Density<select data-print="density"><option value="5">5 pt Compact</option><option value="4">4 pt Maximum Density</option></select></label>
            <label>Content<select data-print="details"><option value="summary">Summary Only</option><option value="details">Include Item and Payment Details</option></select></label>
            <label class="nkt-th-print-check"><input type="checkbox" data-print="plate"> Include Plate Number</label>
            <label class="nkt-th-print-check"><input type="checkbox" data-print="os"> Include OS#</label>
            <div class="nkt-th-print-note">All authorized Cashier/Encoder/Manager/Owner/Admin prints are audited. A Manager PIN is not required and never expands transaction visibility.</div>
            <div class="nkt-th-print-actions"><button type="button" data-print-action="cancel">Cancel</button><button type="button" data-print-action="prepare" class="primary">Prepare Print</button></div>
          </div>`;
        document.body.appendChild(layer);
        const $layer = $(layer);
        $layer.on("click.nktUI6", '[data-print-action="cancel"]', closePrintSettings);
        $layer.on("click.nktUI6", '[data-print-action="prepare"]', () => preparePrint(state, $layer));
        $layer.find('[data-print="paper"]').trigger("focus");
    }

    function closePrintSettings() {
        const layer = document.getElementById(PRINT_LAYER_ID);
        if (layer) {
            $(layer).off(".nktUI6");
            layer.remove();
        }
    }

    async function preparePrint(state, layer) {
        const button = layer.find('[data-print-action="prepare"]');
        button.prop("disabled", true).text("Preparing…");
        try {
            const result = await call("prepare_transaction_history_print", {
                filters: filters(state, 0),
                mode: state.mode,
                paper_size: String(layer.find('[data-print="paper"]').val() || "long"),
                density: String(layer.find('[data-print="density"]').val() || "5"),
                detail_mode: String(layer.find('[data-print="details"]').val() || "summary"),
                include_plate_number: layer.find('[data-print="plate"]').prop("checked") ? 1 : 0,
                include_os_no: layer.find('[data-print="os"]').prop("checked") ? 1 : 0,
                device_id: deviceId()
            });
            closePrintSettings();
            openPrintDocument(result);
        } catch (error) {
            frappe.msgprint({title: "Print could not be prepared", message: esc(errorMessage(error) || "Transaction History print failed."), indicator: "red"});
        } finally {
            button.prop("disabled", false).text("Prepare Print");
        }
    }

    function openPrintDocument(result) {
        const popup = window.open("", "_blank");
        if (!popup) {
            frappe.msgprint("The browser blocked the print window. Allow pop-ups for the NKT site, then try again.");
            return;
        }
        try { popup.opener = null; } catch (_error) {}
        popup.document.open();
        popup.document.write(printHtml(result));
        popup.document.close();
        popup.focus();
        setTimeout(() => popup.print(), 250);
    }

    function printHtml(result) {
        const rows = result.rows || [];
        const font = Number(result.density_config?.font_pt || 5);
        const page = String(result.paper?.page_css || "8.5in 13in portrait");
        const includeDetails = result.detail_mode === "details";
        const includePlate = Boolean(result.include_plate_number);
        const includeOS = Boolean(result.include_os_no);
        const extraCount = (includePlate ? 1 : 0) + (includeOS ? 1 : 0);
        const summaryRows = rows.map((row) => {
          const optional = `${includePlate ? `<td>${esc(row.plate_number || "")}</td>` : ""}${includeOS ? `<td>${esc(row.os_no || "")}</td>` : ""}`;
          return `<tr><td>${esc(row.transaction_datetime)}</td><td>${esc(row.customer_name || row.customer)}</td><td class="n">${money(row.gross_amount)}</td><td class="n">${signedAdjustment(row.price_adjustment)}</td><td class="n">${money(row.net_amount)}</td><td class="c">${row.account_flag ? "✓" : ""}</td><td>${esc(row.payment_label)}</td><td>${esc(row.secondary_date)}</td><td>${esc(row.user_display)}</td><td>${esc(row.status)}</td>${optional}</tr>
          ${includeDetails ? `<tr class="details"><td colspan="${10 + extraCount}">${printDetail(row, result.mode, includePlate, includeOS)}</td></tr>` : ""}`;
        }).join("");
        const optionalHeaders = `${includePlate ? "<th>Plate</th>" : ""}${includeOS ? "<th>OS#</th>" : ""}`;
        const filtersText = Object.entries(result.filters || {}).filter(([, v]) => v !== "" && v !== null && v !== undefined && v !== "All").map(([k, v]) => `${humanize(k)}: ${v}`).join(" • ");
        return `<!doctype html><html><head><meta charset="utf-8"><title>${esc(result.title)}</title><style>
          @page{size:${page};margin:0.23in}*{box-sizing:border-box}body{font-family:Arial,sans-serif;font-size:${font}pt;color:#000;margin:0}h1{font-size:${font + 3}pt;margin:0 0 2px}.meta{font-size:${font}pt;margin-bottom:4px}.summary{display:flex;gap:12px;border:1px solid #000;padding:2px;margin-bottom:3px}table{width:100%;border-collapse:collapse;table-layout:fixed}th,td{border:0.3pt solid #555;padding:1px 2px;vertical-align:top;word-wrap:break-word}th{font-size:${font + 0.5}pt}thead{display:table-header-group}.n{text-align:right}.c{text-align:center}.details td{padding:.5px 1px;background:#fff}.print-detail{font-size:${Math.max(4, font - 0.3)}pt;line-height:1.05}.print-item{display:grid;grid-template-columns:minmax(0,1fr) 11% 15% 28%;gap:2px;border-bottom:.2pt dotted #888;padding:.4px 0}.print-item span:nth-child(2),.print-item span:nth-child(3){text-align:right;white-space:nowrap}.print-payments{padding:.5px 0;white-space:normal}.print-payments span{white-space:nowrap}.footer{position:fixed;bottom:0;right:0;font-size:${font}pt}
        </style></head><body>
          <h1>${esc(result.title)} — ${esc(result.detail_label)}</h1>
          <div class="meta">Generated ${esc(result.generated_at)} • Printed by ${esc(result.requested_by_identity?.full_name || result.requested_by)} • Audit ${esc(result.print_event)} • ${esc(filtersText)}</div>
          <div class="summary"><span>Rows <b>${number(result.summary?.row_count).toLocaleString()}</b></span><span>Gross <b>${money(result.summary?.gross_total)}</b></span><span>Price Adjustment <b>${signedAdjustment(result.summary?.price_adjustment_total)}</b></span><span>Net <b>${money(result.summary?.net_total)}</b></span><span>Account <b>${money(result.summary?.account_total)}</b></span></div>
          <table><thead><tr><th>Date / Time</th><th>Customer</th><th>Gross</th><th>Price Adj.</th><th>Net</th><th>Acct.</th><th>Payment</th><th>${result.mode === "cashier" ? "Shift Date" : "Encoded Date"}</th><th>User</th><th>Status</th>${optionalHeaders}</tr></thead><tbody>${summaryRows}</tbody></table>
          <div class="footer">Report SHA-256 ${esc(result.report_sha256)}</div>
        </body></html>`;
    }

    function printDetail(row, mode, includePlate = false, includeOS = false) {
        const items = row.items || [];
        const payments = row.payments || [];
        const itemHtml = items.map((item) => `<div class="print-item"><span>${esc(item.item_name || item.item_code)}</span><span>${esc(item.quantity)} ${esc(item.uom)}</span><span>@ ₱${money(item.rate)}</span><span>${esc(item.warehouse)}</span></div>`).join("");
        const paymentHtml = payments.length ? `<div class="print-payments">${payments.map((payment) => `<span>${esc(inlinePaymentText(payment, mode === "cashier"))}</span>`).join(" · ")}</div>` : "";
        const refs = [includePlate && row.plate_number ? `Plate: ${row.plate_number}` : "", includeOS && row.os_no ? `OS#: ${row.os_no}` : ""].filter(Boolean).join(" · ");
        const refsHtml = refs ? `<div class="print-payments">${esc(refs)}</div>` : "";
        return `<div class="print-detail">${itemHtml}${paymentHtml}${refsHtml}</div>`;
    }

    function installStyle() {
        const prior = document.getElementById(STYLE_ID);
        if (prior) prior.remove();
        const style = document.createElement("style");
        style.id = STYLE_ID;
        style.textContent = `
          body.nkt-th-open{overflow:hidden!important}
          .nkt-th-overlay{position:fixed;inset:0;z-index:1300;background:#cfd5dc;padding:6px;font-family:Tahoma,Arial,sans-serif;color:#17202a}
          .nkt-th-workspace{height:100%;display:flex;flex-direction:column;min-width:0;background:#fff;border:1px solid #5e6873;box-shadow:0 4px 18px rgba(0,0,0,.28)}
          .nkt-th-titlebar{min-height:48px;padding:7px 10px;background:linear-gradient(#f9fbfd,#cbd7e3);border-bottom:1px solid #677687;display:flex;justify-content:space-between;align-items:center;gap:12px}
          .nkt-th-heading{display:flex;align-items:baseline;min-width:0}.nkt-th-titlebar strong{font-size:17px;letter-spacing:.2px;color:#172b3f;white-space:nowrap}.nkt-th-titlebar .nkt-th-heading span{margin-left:12px;font-size:11px;color:#4f5d69;white-space:nowrap}
          .nkt-th-title-actions{display:flex;gap:7px;flex:0 0 auto}
          .nkt-th-overlay button,.nkt-th-print-layer button{font:12px Tahoma,Arial,sans-serif;border:1px solid #66717c;border-radius:2px;background:linear-gradient(#fff,#d9dee3);padding:5px 11px;min-height:28px;cursor:pointer;color:#17202a}
          .nkt-th-overlay button:hover,.nkt-th-print-layer button:hover{background:linear-gradient(#fff,#cbd9e6);border-color:#3f617f}
          .nkt-th-overlay button.primary,.nkt-th-print-layer button.primary{background:linear-gradient(#4c91c8,#25669b);color:#fff;border-color:#1e527e;font-weight:bold}
          .nkt-th-overlay button:focus-visible,.nkt-th-print-layer button:focus-visible{outline:2px solid #1d71b8;outline-offset:1px}
          .nkt-th-scopebar{display:flex;justify-content:space-between;align-items:center;gap:12px;padding:4px 9px;background:#fff7cf;border-bottom:1px solid #b49a37;font-size:11px;color:#4e4215}
          .nkt-th-key-note{font-weight:bold;white-space:nowrap;color:#6c5b1d}
          .nkt-th-filter-panel{background:#edf1f5;border-bottom:1px solid #7b8794}
          .nkt-th-filter-caption{display:flex;align-items:baseline;gap:9px;padding:5px 9px 0;color:#273849}.nkt-th-filter-caption b{font-size:10px;letter-spacing:.7px}.nkt-th-filter-caption span{font-size:10px;color:#64717d}
          .nkt-th-filters{display:grid;grid-template-columns:repeat(16,minmax(0,1fr));gap:6px;padding:5px 8px 8px}
          .nkt-th-field{min-width:0}.nkt-th-field.span-2{grid-column:span 2}.nkt-th-field.span-3{grid-column:span 3}.nkt-th-field.span-4{grid-column:span 4}.nkt-th-filter-actions.span-2{grid-column:span 2}
          .nkt-th-field .form-group{margin:0!important}.nkt-th-field .control-label,.nkt-th-field label{display:block;margin:0 0 3px!important;font-weight:bold;font-size:10px;line-height:1.1;color:#273849}.nkt-th-field .awesomplete{width:100%}
          .nkt-th-field input,.nkt-th-field select,.nkt-th-field .form-control{width:100%;height:30px!important;min-height:30px!important;border:1px solid #85919d;border-radius:2px;background:#fff;padding:3px 6px;font-size:11px;color:#17202a;box-shadow:inset 0 1px 1px rgba(0,0,0,.05)}
          .nkt-th-field input:focus,.nkt-th-field select:focus,.nkt-th-field .form-control:focus{border-color:#2c74ad;box-shadow:0 0 0 1px #2c74ad;outline:none}
          .nkt-th-filter-actions{display:flex;gap:6px;align-items:end;justify-content:flex-end;padding-top:14px}.nkt-th-filter-actions button{flex:1;white-space:nowrap}
          .nkt-th-warning{padding:6px 9px;background:#ffe5df;border-bottom:1px solid #b34a3a;color:#7d180e;font-weight:bold;font-size:11px}
          .nkt-th-summary{display:flex;align-items:stretch;gap:0;padding:0 8px;background:#f8fafc;border-bottom:1px solid #87939f;min-height:40px}
          .nkt-th-metric{display:flex;align-items:baseline;gap:6px;padding:7px 12px 6px 0;margin-right:12px;border-right:1px solid #d3d9df;white-space:nowrap}.nkt-th-metric span{font-size:10px;text-transform:uppercase;letter-spacing:.3px;color:#64717d}.nkt-th-metric b{font-size:12px;color:#1f2f3d}.nkt-th-metric.emphasized b{font-size:14px;color:#0b4f83}
          .nkt-th-status{margin-left:auto;align-self:center;font-size:11px;font-weight:bold;color:#40505f;white-space:nowrap}
          .nkt-th-table-wrap{flex:1;min-height:0;overflow:auto;background:#fff}
          .nkt-th-table{width:100%;min-width:1080px;border-collapse:collapse;table-layout:fixed;font-size:11px}
          .nkt-th-table col.nkt-th-col-expander{width:34px}.nkt-th-table col.nkt-th-col-datetime{width:145px}.nkt-th-table col.nkt-th-col-customer{width:auto}.nkt-th-table col.nkt-th-col-money{width:96px}.nkt-th-table col.nkt-th-col-adjustment{width:76px}.nkt-th-table col.nkt-th-col-account{width:68px}.nkt-th-table col.nkt-th-col-payment{width:92px}.nkt-th-table col.nkt-th-col-secondary{width:96px}.nkt-th-table col.nkt-th-col-user{width:98px}.nkt-th-table col.nkt-th-col-status{width:122px}
          .nkt-th-table th,.nkt-th-table td{border-right:1px solid #c7cdd3;border-bottom:1px solid #d5dae0;padding:4px 5px;vertical-align:middle;overflow:hidden;text-overflow:ellipsis}
          .nkt-th-table th{position:sticky;top:0;z-index:2;background:linear-gradient(#eef3f7,#cbd5df);border-top:0;border-bottom:1px solid #6f7d8b;color:#203244;font-weight:bold;line-height:1.15;white-space:nowrap}
          .nkt-th-table th:first-child,.nkt-th-table td:first-child{border-left:0}.nkt-th-table th:last-child,.nkt-th-table td:last-child{border-right:0}
          .nkt-th-expander-cell{width:34px!important;min-width:34px!important;max-width:34px!important;text-align:center!important;padding:3px!important;white-space:nowrap!important}
          .nkt-th-expander-glyph{display:inline-flex;align-items:center;justify-content:center;width:20px;height:20px;border:1px solid #6e7b88;border-radius:2px;background:linear-gradient(#fff,#dbe1e7);font:bold 13px/1 Tahoma,Arial,sans-serif;color:#173a58}
          .nkt-th-summary-row{cursor:default;background:#fff}.nkt-th-summary-row:nth-child(4n+1){background:#f8fafc}.nkt-th-summary-row:hover{background:#e7f2fc}.nkt-th-summary-row:focus,.nkt-th-summary-row.selected{background:#d9ecfb;outline:2px solid #2c74ad;outline-offset:-2px}.nkt-th-summary-row[aria-expanded="true"]{background:#dcecf8}.nkt-th-summary-row.selected[aria-expanded="true"]{background:#cfe6f6}
          .nkt-th-table .num{text-align:right;white-space:nowrap;font-variant-numeric:tabular-nums}.nkt-th-table .center{text-align:center}.nkt-th-table .strong{font-weight:bold;color:#102f48}.nkt-th-primary-text{display:block;white-space:nowrap;font-variant-numeric:tabular-nums}.nkt-th-datetime-cell,.nkt-th-secondary-cell,.nkt-th-user-cell,.nkt-th-payment-cell{white-space:nowrap}.nkt-th-customer-cell{white-space:nowrap;overflow:hidden;text-overflow:ellipsis;line-height:1.2}
          .nkt-th-kind{display:inline-block;margin-top:3px;padding:1px 4px;border:1px solid #bdc7d0;border-radius:2px;background:#f0f3f6;color:#52616f;font-size:9px;line-height:1.1}
          .nkt-th-check{display:inline-flex;align-items:center;justify-content:center;border:1px solid #3d5c75;width:17px;height:17px;font-weight:bold;background:#fff;color:#0d4b78}
          .nkt-th-status-badge{display:inline-block;max-width:100%;padding:2px 5px;border:1px solid #5c8b5c;border-radius:2px;background:#edf8ed;white-space:normal;line-height:1.15}.nkt-th-status-badge.warn{border-color:#ae8c25;background:#fff6d2}.nkt-th-status-badge.bad{border-color:#ad4a40;background:#ffe6e2}
          .nkt-th-detail-row td{background:#edf3f7;padding:2px 7px 3px;overflow:visible}.nkt-th-compact-detail{border-left:3px solid #6c91ad;background:#fff;padding:2px 5px;font-size:10px;line-height:1.15}.nkt-th-compact-item{display:grid;grid-template-columns:minmax(220px,1fr) 90px 105px minmax(180px,.75fr);gap:7px;align-items:center;padding:2px 0;border-bottom:1px dotted #c2cbd3}.nkt-th-compact-name{font-weight:bold;white-space:normal}.nkt-th-compact-qty,.nkt-th-compact-rate{text-align:right;white-space:nowrap;font-variant-numeric:tabular-nums}.nkt-th-compact-warehouse{white-space:normal;color:#354b5e}.nkt-th-compact-payments{display:flex;gap:6px 12px;flex-wrap:wrap;padding:2px 0 1px}.nkt-th-compact-payment{white-space:nowrap;font-weight:bold;color:#193c57}.nkt-th-compact-empty{padding:3px 5px;color:#66727e;font-style:italic}
          .nkt-th-footer{display:flex;justify-content:space-between;align-items:center;padding:5px 8px;background:#e6eaee;border-top:1px solid #7b8794;font-size:11px;color:#42515e}
          .nkt-th-print-layer{position:fixed;inset:0;z-index:2500;background:rgba(0,0,0,.50);display:flex;align-items:center;justify-content:center;font-family:Tahoma,Arial,sans-serif}.nkt-th-print-panel{width:470px;background:#f5f5f5;border:2px solid #444;box-shadow:0 8px 28px rgba(0,0,0,.45);padding:12px}.nkt-th-print-title{border-bottom:1px solid #888;padding-bottom:7px;margin-bottom:8px}.nkt-th-print-title b{display:block;font-size:16px}.nkt-th-print-title span{font-size:11px;color:#555}.nkt-th-print-panel label{display:grid;grid-template-columns:135px 1fr;gap:8px;align-items:center;margin:7px 0;font-weight:bold}.nkt-th-print-panel .nkt-th-print-check{display:flex;align-items:center;gap:6px}.nkt-th-print-panel select{height:30px}.nkt-th-print-note{padding:7px;border:1px solid #a68b2b;background:#fff7ca;font-size:11px;line-height:1.4}.nkt-th-print-actions{display:flex;justify-content:flex-end;gap:7px;margin-top:10px}
          .nkt-ui7a-view-layer{position:fixed;inset:0;z-index:2350;background:#e9ecef;font:12px Tahoma,Arial,sans-serif;color:#17202a}.nkt-ui7a-view-loading{position:absolute;inset:0;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:7px;background:rgba(240,243,246,.96);font-size:16px}.nkt-ui7a-view-loading span{font-size:12px;color:#586777}.nkt-ui7a-view-screen{height:100%;display:flex;flex-direction:column;overflow:auto;background:#f4f4f4}.nkt-ui7a-view-titlebar{min-height:48px;padding:7px 10px;border-bottom:1px solid #647485;background:linear-gradient(#f9fbfd,#cbd7e3);display:flex;align-items:center;justify-content:space-between;gap:12px}.nkt-ui7a-view-titlebar>div:first-child{display:flex;align-items:baseline;gap:12px;min-width:0}.nkt-ui7a-view-titlebar strong{font-size:18px;color:#152d42;white-space:nowrap}.nkt-ui7a-view-titlebar span{font-weight:bold;color:#4a5c6c;white-space:nowrap}.nkt-ui7a-view-actions{display:flex;gap:7px;flex:0 0 auto}.nkt-ui7a-view-actions button,.nkt-ui7a-receipt-panel button{font:12px Tahoma,Arial,sans-serif;border:1px solid #66717c;border-radius:2px;background:linear-gradient(#fff,#d9dee3);padding:6px 11px;cursor:pointer}.nkt-ui7a-view-actions button.primary,.nkt-ui7a-receipt-panel button.primary{background:linear-gradient(#4c91c8,#25669b);color:#fff;border-color:#1e527e;font-weight:bold}.nkt-ui7a-view-actions button.warn{background:linear-gradient(#fff8d7,#e7c969);border-color:#9c7b1d;color:#443400;font-weight:bold}.nkt-ui7a-view-banner{text-align:center;background:#fff1a9;border-bottom:2px solid #a57a00;padding:6px;font-size:14px;font-weight:bold;letter-spacing:.8px}.nkt-ui7a-view-meta{display:grid;grid-template-columns:2fr 1fr 1.1fr 1fr 1fr 1fr;border-bottom:1px solid #7e8993;background:#f9fafb}.nkt-ui7a-view-meta>div{padding:7px 9px;border-right:1px solid #c7cdd3;min-width:0}.nkt-ui7a-view-meta>div:last-child{border-right:0}.nkt-ui7a-view-meta span{display:block;font-size:10px;text-transform:uppercase;color:#61707d}.nkt-ui7a-view-meta b{display:block;margin-top:2px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.nkt-ui7a-section-title,.nkt-ui7a-subtitle{padding:5px 8px;background:linear-gradient(#edf3f8,#ccd8e2);border-bottom:1px solid #7d8995;font-weight:bold;color:#21384d;letter-spacing:.3px}.nkt-ui7a-view-items{background:#fff}.nkt-ui7a-view-grid{width:100%;border-collapse:collapse;table-layout:fixed}.nkt-ui7a-view-grid th,.nkt-ui7a-view-grid td{border-right:1px solid #c7cdd3;border-bottom:1px solid #d5dae0;padding:6px 7px;vertical-align:top}.nkt-ui7a-view-grid th{background:#e5ebf0;text-align:left}.nkt-ui7a-view-grid th:nth-child(1){width:42px}.nkt-ui7a-view-grid th:nth-child(3){width:120px}.nkt-ui7a-view-grid th:nth-child(4),.nkt-ui7a-view-grid th:nth-child(5){width:120px}.nkt-ui7a-view-grid th:nth-child(6){width:230px}.nkt-ui7a-view-grid small{display:block;color:#66727e;margin-top:2px}.nkt-ui7a-view-grid .num{text-align:right;white-space:nowrap}.nkt-ui7a-reference-strip{padding:7px 9px;background:#fffbe3;border-bottom:1px solid #bcab61;white-space:normal}.nkt-ui7a-reference-strip .sep{margin:0 10px;color:#9a8b4a}.nkt-ui7a-view-lower{display:grid;grid-template-columns:minmax(0,1.3fr) minmax(340px,.7fr);border-bottom:1px solid #7e8993;min-height:145px}.nkt-ui7a-view-payments{background:#fff;border-right:1px solid #7e8993}.nkt-ui7a-payment-line{display:flex;justify-content:space-between;gap:12px;padding:7px 9px;border-bottom:1px dotted #c6ccd2}.nkt-ui7a-payment-line span{color:#536475;text-align:right}.nkt-ui7a-view-totals{background:#f8fafb;padding:7px 10px}.nkt-ui7a-view-totals>div{display:flex;justify-content:space-between;padding:5px 2px;border-bottom:1px solid #d4d9de}.nkt-ui7a-view-totals .grand{font-size:16px;border:2px solid #596a79;background:#fff;padding:8px}.nkt-ui7a-account-strip{display:grid;grid-template-columns:repeat(3,1fr);background:#fff7cf;border-bottom:1px solid #a68b2b}.nkt-ui7a-account-strip>div{display:flex;justify-content:space-between;gap:10px;padding:7px 10px;border-right:1px solid #c9b96e}.nkt-ui7a-account-strip>div:last-child{border-right:0}.nkt-ui7a-empty-line{padding:10px;color:#64717d;font-style:italic}.nkt-ui7a-view-footer{margin-top:auto;padding:6px 9px;background:#e5eaee;border-top:1px solid #7b8794;display:flex;justify-content:space-between;color:#50606e}.nkt-ui7a-receipt-layer{position:fixed;inset:0;z-index:2600;background:rgba(0,0,0,.56);display:flex;align-items:center;justify-content:center;font:12px Tahoma,Arial,sans-serif}.nkt-ui7a-receipt-panel{width:500px;background:#f4f4f4;border:2px solid #3e4851;box-shadow:0 10px 35px rgba(0,0,0,.48);padding:12px}.nkt-ui7a-receipt-title{padding-bottom:8px;border-bottom:1px solid #8b949c;margin-bottom:9px}.nkt-ui7a-receipt-title b{display:block;font-size:17px}.nkt-ui7a-receipt-title span{font-size:11px;color:#566471}.nkt-ui7a-receipt-panel label{display:grid;grid-template-columns:130px 1fr;align-items:center;gap:8px;font-weight:bold;margin:9px 0}.nkt-ui7a-receipt-panel select{height:31px}.nkt-ui7a-receipt-note,.nkt-ui7a-register-message{padding:8px;border:1px solid #aa8c24;background:#fff6c9;line-height:1.4}.nkt-ui7a-receipt-actions{display:flex;justify-content:flex-end;gap:7px;margin-top:11px}
          @media(max-width:1400px){.nkt-th-filters{grid-template-columns:repeat(12,minmax(0,1fr))}.nkt-th-field.span-4{grid-column:span 3}.nkt-th-field.span-3{grid-column:span 3}.nkt-th-field.span-2,.nkt-th-filter-actions.span-2{grid-column:span 2}.nkt-th-titlebar .nkt-th-heading span{display:none}}
        `;
        document.head.appendChild(style);
    }

    window.__nktTransactionHistoryTest = {
        version: "UI7A",
        keyIs,
        isReservedFunctionKey,
        money,
        signedAdjustment,
        statusBadge,
        detailMarkup,
        inlinePaymentText,
        navigationIndex,
        printDetail,
        printHtml,
        receiptPrintHtml,
        transactionKindLabel,
        customerHistoryDeviceId,
        paymentLabels: ["CASH", "CHECK", "GCASH", "MAYA", "CARD", "BANK TRANSFER", "ONLINE", "ACCOUNT", "RETURN CREDIT", "SPLIT"]
    };
})();
