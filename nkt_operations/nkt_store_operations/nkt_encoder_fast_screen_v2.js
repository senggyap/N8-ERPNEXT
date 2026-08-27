(() => {
  const MODE = 'encoder';
  const DOCTYPE = 'NKT Encoder Fast Screen';
  const SCREEN_NAME = 'Encoder';
  const SCREEN_SUBTITLE = 'Classic Customer Order';
  const GRID_WAREHOUSE_LABEL = 'Official Source Warehouse';
  const GRID_STOCK_LABEL = 'Available Qty';
  const DOCUMENT_KEY = 'Customer Order';
  const NAMESPACE = 'nktFastEncoderV20C3CKeyboard';

  frappe.ui.form.on(DOCTYPE, {
    refresh(frm) {
      render_nkt_fast_shell(frm);
    }
  });

  function render_nkt_fast_shell(frm) {
    frm.disable_save();
    frm.page.clear_actions();
    frm.page.set_title(__(`NKT ${SCREEN_NAME} — ${SCREEN_SUBTITLE}`));
    const wrapper = frm.fields_dict.screen_html.$wrapper;
    prepare_frappe_layout(wrapper);
    wrapper.empty().html(shell_markup());
    install_shell_css();
    const state = make_state(frm, wrapper);
    bind_shell(state);
    initialize_security(state).then(canContinue => {
      if (canContinue) load_bootstrap(state);
    });
  }

  function prepare_frappe_layout(wrapper) {
    const formLayout = wrapper.closest('.form-layout');
    formLayout.find('.layout-side-section').hide();
    formLayout.find('.layout-main-section-wrapper').css({ width: '100%', maxWidth: 'none', flex: '1 1 100%' });
    wrapper.closest('.layout-main-section').css({ maxWidth: 'none' });
  }

  function make_state(frm, wrapper) {
    return {
      frm, wrapper, mode: MODE, boot: null, rows: [], payments: [], cashTendered: 0, cashTenderedManual: false, paymentConfirmed: false,
      customer: null, itemResults: [], itemIndex: 0, customerResults: [], customerIndex: 0,
      paymentDialog: null, requestId: make_request_id(), finalizing: false, postingAttempt: 0, postingWatchdog: null, lastPostCompletedAt: 0,
      securityMode: 'normal', securityBusy: false, terminalLocked: false, localRestrictionLatch: false,
      continuityOrderOnly: false
    };
  }

  function shell_markup() {
    const modeText = MODE === 'cashier' ? '<span>Shift: <b data-role="shift">Loading…</b></span>' : '<span>Mode: <b>Independent Encoder Entry</b></span>';
    return `
      <div class="nkt-fast-shell" tabindex="0">
        <div class="nkt-shell-titlebar">
          <div><strong>NKT ${SCREEN_NAME}</strong><span class="nkt-shell-subtitle">${SCREEN_SUBTITLE}</span></div>
          <div class="nkt-readonly-badge">V2.0C.3C LIVE — KEYBOARD STABILIZED</div>
        </div>
        <div class="nkt-contextbar">
          <span>Operator: <b data-role="operator">Loading…</b></span>
          <span>Branch: <b data-role="branch">Loading…</b></span>
          <span>Default Warehouse: <b data-role="default-warehouse">Loading…</b></span>
          ${modeText}
        </div>
        <div class="nkt-warning" data-role="warning" hidden></div>
        <div class="nkt-item-entry-row">
          <label><u>F3</u> Enter Item</label>
          <div class="nkt-combo-wrap">
            <input data-role="item-entry" class="nkt-primary-input" autocomplete="off" placeholder="Item code, barcode, or description">
            <div data-role="item-results" class="nkt-results" hidden></div>
          </div>
          <button type="button" data-action="add-item">Add</button>
          <span class="nkt-entry-hint">Enter adds row • ↓ Qty • → Rate • Enter returns to F3</span>
        </div>
        <div class="nkt-grid-wrap">
          <table class="nkt-grid">
            <colgroup>
              <col class="c-no"><col class="c-item"><col class="c-desc"><col class="c-qty"><col class="c-uom"><col class="c-rate"><col class="c-wh"><col class="c-stock"><col class="c-amt"><col class="c-remove">
            </colgroup>
            <thead><tr>
              <th>#</th><th>Item</th><th>Description</th><th>Qty</th><th>UOM</th><th>Rate</th><th>${GRID_WAREHOUSE_LABEL}</th><th>${GRID_STOCK_LABEL}</th><th>Amount</th><th></th>
            </tr></thead>
            <tbody data-role="grid-body"><tr class="nkt-empty"><td colspan="10">Press F3 and enter the first item.</td></tr></tbody>
          </table>
        </div>
        <div class="nkt-lower-panel">
          <div class="nkt-customer-panel">
            <div class="nkt-panel-heading"><span><u>F2</u> Customer</span><span class="nkt-panel-status" data-role="customer-status">Required</span></div>
            <div class="nkt-combo-wrap">
              <input data-role="customer-entry" autocomplete="off" placeholder="Search actual customer — no generic Walk-in">
              <div data-role="customer-results" class="nkt-results nkt-customer-results" hidden></div>
            </div>
            <div data-role="customer-selected" class="nkt-customer-card">
              <div class="nkt-customer-name">No customer selected</div>
              <div class="nkt-customer-balance-line">Current Account Balance: <b data-role="customer-balance">₱0.00</b></div>
            </div>
            <button type="button" data-action="new-customer">New Customer</button>
            <button type="button" data-action="customer-history"><u>F4</u> History</button>
          </div>
          <div class="nkt-payment-preview">
            <div class="nkt-panel-heading"><span>Payment Settlement</span><span class="nkt-panel-status" data-role="payment-status">Not entered</span></div>
            <div data-role="payment-lines" class="nkt-payment-lines">Press F11 to settle the whole receipt using one or more payment rows.</div>
          </div>
          <div class="nkt-total-panel">
            <div><span>Total Quantity</span><b data-role="total-qty">0</b></div>
            <div class="nkt-grand-total"><span>Grand Total</span><b data-role="grand-total">₱0.00</b></div>
          </div>
        </div>
        <div class="nkt-actionbar">
          <button type="button" data-action="hold">Hold</button>
          <button type="button" data-action="clear">Clear</button>
          <span class="nkt-shortcut-note">F2 Customer • F3 Item • F4 History • Esc Item</span>
          <div class="nkt-action-spacer"></div>
          <button type="button" data-action="f10"><b>F10</b> Finalize & Print</button>
          <button type="button" data-action="f11" class="primary"><b>F11</b> Take Payment</button>
          <button type="button" data-action="f12"><b>F12</b> Finalize</button>
        </div>
      </div>`;
  }

  function install_shell_css() {
    if (document.getElementById('nkt-fast-shell-style-v20b2')) return;
    const style = document.createElement('style');
    style.id = 'nkt-fast-shell-style-v20b2';
    style.textContent = `
      .nkt-fast-shell{font-family:Tahoma,Arial,sans-serif;font-size:13px;color:#111;background:#d5d5d5;border:1px solid #6f6f6f;min-height:calc(100vh - 150px);width:100%;display:flex;flex-direction:column;box-shadow:inset 0 0 0 1px #fff}
      .nkt-shell-titlebar{display:flex;justify-content:space-between;align-items:center;padding:7px 10px;background:linear-gradient(#fafafa,#c7c7c7);border-bottom:1px solid #777;font-size:18px}.nkt-shell-subtitle{font-size:13px;font-weight:normal;margin-left:12px}.nkt-readonly-badge{font-size:11px;padding:4px 8px;background:#fff1a8;border:1px solid #9b7900}
      .nkt-contextbar{display:flex;gap:28px;padding:5px 10px;background:#ececec;border-bottom:1px solid #999;white-space:nowrap;overflow:hidden}.nkt-warning{margin:5px 8px 0;padding:5px 8px;background:#fff0ee;border:1px solid #c25b52;font-size:12px}
      .nkt-item-entry-row{display:grid;grid-template-columns:125px minmax(320px,1fr) 70px minmax(280px,.85fr);gap:7px;align-items:center;padding:7px 9px;border-bottom:1px solid #999;background:#dedede}.nkt-item-entry-row label{font-weight:bold}.nkt-entry-hint{color:#4a4a4a;white-space:nowrap}
      .nkt-fast-shell input,.nkt-fast-shell select{height:28px;border:1px solid #666;background:#fff;padding:3px 6px;border-radius:0}.nkt-primary-input{font-size:15px;font-weight:bold;width:100%;box-shadow:inset 1px 1px 2px #aaa}.nkt-fast-shell button{min-height:28px;border:1px solid #666;background:linear-gradient(#fff,#d0d0d0);border-radius:1px;padding:4px 10px;color:#111}.nkt-fast-shell button:active{background:#c2c2c2}.nkt-fast-shell button.primary{background:linear-gradient(#edf6ff,#bddbfc);border-color:#3d6f9f}
      .nkt-grid-wrap{flex:1;min-height:315px;background:#fff;overflow:auto;border-bottom:1px solid #777}.nkt-grid{width:100%;border-collapse:collapse;table-layout:fixed}.nkt-grid .c-no{width:4%}.nkt-grid .c-item{width:12%}.nkt-grid .c-desc{width:15%}.nkt-grid .c-qty{width:7%}.nkt-grid .c-uom{width:8%}.nkt-grid .c-rate{width:12%}.nkt-grid .c-wh{width:15%}.nkt-grid .c-stock{width:10%}.nkt-grid .c-amt{width:12%}.nkt-grid .c-remove{width:5%}
      .nkt-grid th{position:sticky;top:0;z-index:1;background:linear-gradient(#f5f5f5,#cecece);border-right:1px solid #999;border-bottom:1px solid #777;padding:6px 7px;text-align:left;line-height:1.15}.nkt-grid td{border-right:1px solid #bbb;border-bottom:1px solid #ccc;padding:4px 6px;vertical-align:middle;line-height:1.2}.nkt-grid input,.nkt-grid select{width:100%;height:27px;border:1px solid transparent}.nkt-grid input:focus,.nkt-grid select:focus{border-color:#1b5790;outline:1px solid #1b5790}.nkt-grid .nkt-special-rate{background:#ffe9e6;border-color:#b54237}.nkt-grid .nkt-adjusted-rate{background:#fff4bf}.nkt-empty td{text-align:center;color:#666;padding:42px}
      .nkt-rate-badge{display:inline-block;margin-top:2px;padding:1px 5px;border:1px solid #999;background:#eee;font-size:10px;white-space:nowrap}.nkt-rate-badge.adjusted{background:#fff2b6;border-color:#b28a00}.nkt-rate-badge.special{background:#ffe0dc;border-color:#b54237;color:#7b160f}.nkt-remove{padding:1px 7px!important;min-height:24px!important}
      .nkt-combo-wrap{position:relative}.nkt-results{position:absolute;left:0;right:0;top:100%;z-index:50;background:#fff;border:1px solid #555;max-height:240px;overflow:auto;box-shadow:2px 3px 7px rgba(0,0,0,.25)}.nkt-result{padding:6px 8px;border-bottom:1px solid #ddd;cursor:pointer}.nkt-result.active,.nkt-result:hover{background:#cfe7ff}.nkt-result small{display:flex;justify-content:space-between;color:#555;margin-top:2px}
      .nkt-lower-panel{display:grid;grid-template-columns:minmax(300px,1fr) minmax(360px,1.2fr) minmax(280px,.9fr);gap:8px;padding:8px;background:#e2e2e2}.nkt-customer-panel,.nkt-payment-preview,.nkt-total-panel{background:#f7f7f7;border:1px solid #888;padding:8px}.nkt-panel-heading{display:flex;justify-content:space-between;font-weight:bold;border-bottom:1px solid #aaa;margin:-2px -2px 7px;padding:0 2px 5px}.nkt-panel-status{font-size:10px;font-weight:normal;padding:1px 5px;background:#eee;border:1px solid #aaa}.nkt-customer-panel input{width:100%}.nkt-customer-card{min-height:50px;padding:7px 2px}.nkt-customer-name{font-weight:bold;margin-bottom:5px}.nkt-payment-lines{min-height:68px}.nkt-payment-line{display:grid;grid-template-columns:1fr auto;gap:10px;border-bottom:1px dotted #aaa;padding:3px 0}.nkt-payment-line small{grid-column:1/-1;color:#555}.nkt-total-panel>div{display:flex;justify-content:space-between;padding:6px}.nkt-grand-total{font-size:22px;border-top:2px solid #333;margin-top:6px}.nkt-actionbar{display:flex;gap:7px;align-items:center;padding:8px;background:linear-gradient(#efefef,#c6c6c6);border-top:1px solid #fff}.nkt-action-spacer{flex:1}.nkt-shortcut-note{color:#555;font-size:11px}.nkt-stock-good{color:#08711c}.nkt-stock-bad{color:#b51e12;font-weight:bold}
      .nkt-payment-dialog{max-width:1180px!important;width:94vw!important}.nkt-payment-grid-shell{font-family:Tahoma,Arial,sans-serif;font-size:12px}.nkt-payment-summary{display:grid;grid-template-columns:repeat(4,1fr);gap:8px;margin-bottom:8px}.nkt-summary-box{border:1px solid #888;background:#f5f5f5;padding:8px}.nkt-summary-box b{display:block;font-size:17px;margin-top:3px}.nkt-summary-box.remaining.ok b{color:#0a6a1d}.nkt-summary-box.remaining.bad b{color:#b51e12}.nkt-summary-box.change.has-change b{color:#0a5f92}.nkt-pay-table{width:100%;border-collapse:collapse;table-layout:fixed}.nkt-pay-table th,.nkt-pay-table td{border:1px solid #aaa;padding:4px}.nkt-pay-table th{background:linear-gradient(#f5f5f5,#d2d2d2);text-align:left}.nkt-pay-table input,.nkt-pay-table select{width:100%;height:28px;border:1px solid #777;padding:3px}.nkt-pay-table input:disabled,.nkt-pay-table input[readonly]{background:#f2f2f2;color:#666}.nkt-pay-table .p-method{width:17%}.nkt-pay-table .p-amount{width:17%}.nkt-pay-table .p-ref{width:25%}.nkt-pay-table .p-date{width:14%}.nkt-pay-table .p-provider{width:22%}.nkt-pay-table .p-remove{width:5%}.nkt-pay-actions{display:flex;gap:7px;margin-top:9px}.nkt-pay-actions .spacer{flex:1}.nkt-payment-note{margin-top:8px;padding:6px;border:1px solid #a98b2a;background:#fff7cf;line-height:1.45}.nkt-confirm-box{font-family:Tahoma,Arial,sans-serif;border:2px solid #555;background:#f6f6f6;padding:14px}.nkt-confirm-title{font-size:18px;font-weight:bold;margin-bottom:10px}.nkt-confirm-row{display:flex;justify-content:space-between;padding:4px 0;border-bottom:1px dotted #aaa}.nkt-confirm-change{font-size:24px;font-weight:bold;padding:12px 0;text-align:center;background:#fff4bd;border:1px solid #b28a00;margin:10px 0}.nkt-confirm-hint{text-align:center;font-weight:bold;margin-top:10px}
      .nkt-fast-shell[data-nkt-ui-mode="limited"] [data-action="customer-history"]{display:none!important}
      .nkt-fast-shell[data-nkt-ui-mode="limited"] [data-role="customer-balance"]{display:none!important}
      .nkt-fast-shell[data-nkt-ui-mode="limited"] [data-role="customer-balance"] + *{display:none!important}
      .nkt-fast-shell[data-nkt-ui-mode="limited"] .nkt-customer-balance-line{display:none!important}
      .nkt-device-unavailable{display:flex;min-height:340px;align-items:center;justify-content:center;background:#f3f3f3;border:1px solid #777;font-family:Tahoma,Arial,sans-serif;font-size:17px;font-weight:bold}
      @media(max-width:1050px){.nkt-lower-panel{grid-template-columns:1fr 1fr}.nkt-total-panel{grid-column:1/-1}.nkt-item-entry-row{grid-template-columns:110px 1fr 60px}.nkt-entry-hint{grid-column:1/-1}.nkt-contextbar{overflow:auto}.nkt-shortcut-note{display:none}}
    `;
    document.head.appendChild(style);
  }

  function bind_shell(state) {
    const w = state.wrapper;
    w.on('click', '[data-action="add-item"]', () => item_enter(state));
    w.on('click', '[data-action="clear"]', () => clear_transaction(state));
    w.on('click', '[data-action="hold"]', () => readonly_notice(state, 'Hold persistence'));
    w.on('click', '[data-action="new-customer"]', () => open_fast_new_customer(state));
    w.on('click', '[data-action="customer-history"]', () => {
      if (state.securityMode !== 'limited' && !state.terminalLocked) open_customer_history(state);
    });
    w.on('click', '[data-action="f10"]', () => finalize_live(state, true));
    w.on('click', '[data-action="f11"]', () => open_payment_preview(state));
    w.on('click', '[data-action="f12"]', () => finalize_live(state, false));
    w.on('click', '.nkt-remove', function () { state.rows.splice(Number($(this).data('row')), 1); invalidate_settlement(state); render_grid(state); });
    w.on('click', '[data-item-index]', function () { choose_item(state, Number($(this).data('item-index'))); });
    w.on('click', '[data-customer-index]', function () { choose_customer(state, Number($(this).data('customer-index'))); });
    const item = role(state, 'item-entry');
    let itemTimer = null;
    item.on('input', () => { clearTimeout(itemTimer); itemTimer = setTimeout(() => search_items(state), 150); });
    item.on('keydown', e => item_keydown(state, e));
    const customer = role(state, 'customer-entry');
    let customerTimer = null;
    customer.on('input', () => { clearTimeout(customerTimer); customerTimer = setTimeout(() => search_customers(state), 170); });
    customer.on('keydown', e => customer_keydown(state, e));
    $(document).off(`keydown.${NAMESPACE}`).on(`keydown.${NAMESPACE}`, e => {
      if (e.key === 'F1') {
        e.preventDefault();
        e.stopImmediatePropagation();
        return false;
      }
      if (e.key === 'F12' && e.ctrlKey && e.altKey && e.shiftKey) {
        e.preventDefault();
        e.stopImmediatePropagation();
        self_restrict_now(state);
        return false;
      }
      if ($('.modal.show').length) return;
      if (e.key === 'F2') { e.preventDefault(); focus_customer(state); }
      else if (e.key === 'F3') { e.preventDefault(); focus_item(state); }
      else if (e.key === 'F4') {
        e.preventDefault();
        if (state.securityMode !== 'limited' && !state.terminalLocked) open_customer_history(state);
      }
      else if (e.key === 'F10') { e.preventDefault(); finalize_live(state, true); }
      else if (e.key === 'F11') { e.preventDefault(); open_payment_preview(state); }
      else if (e.key === 'F12') { e.preventDefault(); finalize_live(state, false); }
      else if (e.key === 'Escape') { e.preventDefault(); focus_item(state); }
    });
  }

  function bound_device_id() {
    try { return String(window.localStorage.getItem('nkt_device_id') || '').trim(); }
    catch (_) { return ''; }
  }

  function bound_customer_history_device_id() {
    try {
      return String(
        window.localStorage.getItem('nkt_customer_history_device_id') ||
        window.localStorage.getItem('nkt_device_id') ||
        ''
      ).trim();
    } catch (_) { return ''; }
  }

  const SECURITY_POLL_MS = 2000;

  function initialize_security(state) {
    clear_security_watch();
    const deviceId = bound_device_id();

    // Device binding is already required for sensitive reads. Until production
    // device-enrollment hardening is complete, an unbound development browser
    // may continue the ordinary Fast Screen but cannot self-restrict centrally.
    if (!deviceId) {
      state.securityMode = 'normal';
      apply_security_mode(state, 'normal');
      return Promise.resolve(true);
    }

    return refresh_security(state, true).then(canContinue => {
      if (!canContinue) return false;
      start_security_watch(state);
      return true;
    });
  }

  function clear_security_watch() {
    const timerKey = `${NAMESPACE}SecurityTimer`;
    if (window[timerKey]) {
      clearInterval(window[timerKey]);
      window[timerKey] = null;
    }
    $(window).off(`focus.${NAMESPACE}Security`);
  }

  function start_security_watch(state) {
    const timerKey = `${NAMESPACE}SecurityTimer`;
    window[timerKey] = setInterval(() => refresh_security(state, false), SECURITY_POLL_MS);
    $(window).off(`focus.${NAMESPACE}Security`).on(`focus.${NAMESPACE}Security`, () => refresh_security(state, false));
  }

  function refresh_security(state, initial) {
    if (state.securityBusy || state.terminalLocked) return Promise.resolve(!state.terminalLocked);
    const deviceId = bound_device_id();
    if (!deviceId) return Promise.resolve(true);

    state.securityBusy = true;
    return frappe.call({
      method: 'nkt_operations.nkt_store_operations.features.offline_edge.internal.device_controls.get_client_security_bootstrap',
      args: { device_id: deviceId }
    }).then(r => {
      const policy = r.message || {};
      if (policy.access === 'unavailable') {
        if (policy.local_action === 'crypto_erase_sensitive_state') crypto_erase_sensitive_state(state);
        lock_terminal_screen(state, policy.message || 'Device access unavailable.');
        return false;
      }

      if (policy.ui_mode === 'limited') {
        state.localRestrictionLatch = false;
        apply_security_mode(state, 'limited');
      } else if (!state.localRestrictionLatch) {
        apply_security_mode(state, 'normal');
      }
      return true;
    }).catch(() => {
      // A failed poll must never expand a locally limited surface.
      if (state.securityMode === 'limited' || state.localRestrictionLatch) apply_security_mode(state, 'limited');
      return !state.terminalLocked;
    }).finally(() => {
      state.securityBusy = false;
    });
  }

  function self_restrict_now(state) {
    if (state.terminalLocked) return;
    const deviceId = bound_device_id();

    // Change the visible surface immediately; no confirm dialog and no waiting
    // for a round trip when someone near the workstation should not see history.
    state.localRestrictionLatch = true;
    apply_security_mode(state, 'limited');

    if (!deviceId) return;

    frappe.call({
      method: 'nkt_operations.nkt_store_operations.features.offline_edge.internal.device_controls.self_restrict_current_device',
      args: { device_id: deviceId }
    }).then(() => {
      state.localRestrictionLatch = false;
      apply_security_mode(state, 'limited');
    }).catch(() => {
      // Stay locally limited. A reload may be needed after connectivity returns,
      // but failure must not reopen the sensitive surface in the current session.
      state.localRestrictionLatch = true;
      apply_security_mode(state, 'limited');
    });
  }

  function apply_security_mode(state, mode) {
    if (state.terminalLocked) return;
    const limited = mode === 'limited';
    state.securityMode = limited ? 'limited' : 'normal';

    const shell = state.wrapper.find('.nkt-fast-shell');
    shell.attr('data-nkt-ui-mode', state.securityMode);

    state.wrapper.find('[data-action="customer-history"]').toggle(!limited);
    state.wrapper.find('.nkt-customer-balance-line').toggle(!limited);

    if (limited) {
      role(state, 'customer-results').find('small span').filter(function () {
        return String($(this).text() || '').trim().startsWith('Balance ');
      }).remove();

      // If a history dialog was already open when Owner/employee restricted the
      // workstation, close it immediately rather than leaving sensitive rows on screen.
      $('.modal.show').each(function () {
        const title = String($(this).find('.modal-title').text() || '');
        if (title.includes('Customer History')) $(this).modal('hide');
      });
    }
  }

  function crypto_erase_sensitive_state(state) {
    const preserve = 'nkt_device_id';
    try {
      for (let i = window.localStorage.length - 1; i >= 0; i--) {
        const key = window.localStorage.key(i);
        if (key && key.startsWith('nkt_') && key !== preserve) window.localStorage.removeItem(key);
      }
    } catch (_) {}

    try {
      for (let i = window.sessionStorage.length - 1; i >= 0; i--) {
        const key = window.sessionStorage.key(i);
        if (key && key.startsWith('nkt_')) window.sessionStorage.removeItem(key);
      }
    } catch (_) {}

    state.rows = [];
    state.payments = [];
    state.customer = null;
    state.itemResults = [];
    state.customerResults = [];
    state.cashTendered = 0;
    state.cashTenderedManual = false;
    state.paymentConfirmed = false;
    state.boot = null;

    if (state.paymentDialog) {
      try { state.paymentDialog.hide(); } catch (_) {}
      state.paymentDialog = null;
    }
    $('.modal.show').modal('hide');
  }

  function lock_terminal_screen(state, message) {
    state.terminalLocked = true;
    state.securityMode = 'limited';
    clear_security_watch();
    crypto_erase_sensitive_state(state);
    $(document).off(`keydown.${NAMESPACE}`);
    state.wrapper.empty().html(`<div class="nkt-device-unavailable">${esc(message || 'Device access unavailable.')}</div>`);
  }

  function open_customer_history(state) {
    if (state.securityMode === 'limited' || state.terminalLocked) return;
    if (!state.customer || !state.customer.name) {
      frappe.show_alert({ message: __('Select a Customer first.'), indicator: 'orange' }, 3);
      focus_customer(state);
      return;
    }
    const deviceId = bound_customer_history_device_id();
    if (!deviceId) {
      frappe.msgprint(__('This workstation is not registered for Customer History yet. Ask Owner/Admin or IT to register this workstation.'));
      return;
    }

    const d = new frappe.ui.Dialog({
      title: __('Customer History'),
      size: 'extra-large',
      fields: [{ fieldname: 'body', fieldtype: 'HTML' }]
    });
    d.show();
    const body = d.fields_dict.body.$wrapper;
    body.html('<div class="text-muted">Loading Customer History…</div>');

    Promise.all([
      frappe.call({
        method: 'nkt_operations.nkt_store_operations.features.offline_edge.internal.routed_reads.get_encoder_customer_history',
        args: { customer: state.customer.name, device_id: deviceId, limit: 50, offset: 0 }
      }),
      frappe.call({
        method: 'nkt_operations.nkt_store_operations.features.offline_edge.internal.routed_reads.get_customer_open_receivables',
        args: { customer: state.customer.name, device_id: deviceId }
      })
    ]).then(([histResp, recResp]) => {
      const hist = histResp.message || { rows: [] };
      const rec = recResp.message || { rows: [], total_outstanding: 0 };
      render_encoder_history_dialog(state, d, hist, rec);
    }).catch(() => {
      body.html('<div class="text-muted">Customer History is unavailable on this workstation right now.</div>');
    });

  }

  function render_encoder_history_dialog(state, d, hist, rec) {
    const body = d.fields_dict.body.$wrapper;
    const receivables = (rec.rows || []).map(x => `
      <tr>
        <td>${esc(x.posting_date || '')}</td>
        <td>${esc(x.customer_order || x.name || '')}</td>
        <td style="text-align:right">${format_money(x.original_amount || 0)}</td>
        <td style="text-align:right">${format_money(x.outstanding_amount || 0)}</td>
        <td>${esc(x.status || '')}</td>
      </tr>`).join('');

    const rows = (hist.rows || []).map(x => {
      const items = (x.items || []).map(i => `
        <tr>
          <td>${esc(i.item_name || i.item || '')}</td>
          <td style="text-align:right">${format_number(i.quantity || 0)}</td>
          <td>${esc(i.uom || '')}</td>
          <td style="text-align:right">${format_money(i.final_rate || 0)}</td>
          <td style="text-align:right">${format_money(i.amount || 0)}</td>
          <td>${esc(i.source_warehouse || '')}</td>
        </tr>`).join('');
      const methods = (x.payment_methods || []).join(' + ') || '—';
      return `
        <details class="nkt-history-row">
          <summary>
            <b>${esc(x.order_date || '')}</b> · ${esc(x.order_no || '')}
            · ${format_money(x.grand_total || 0)}
            · ${esc(methods)}
          </summary>
          <div class="nkt-history-meta">
            Encoder: ${esc(x.encoder || '')}
            ${x.sale_datetime ? ` · Sale: ${esc(x.sale_datetime)}` : ''}
            ${x.remarks ? ` · Remarks: ${esc(x.remarks)}` : ''}
          </div>
          <table class="table table-bordered table-condensed">
            <thead><tr><th>Item</th><th>Qty</th><th>UOM</th><th>Historical Rate</th><th>Amount</th><th>Warehouse</th></tr></thead>
            <tbody>${items || '<tr><td colspan="6">No item rows.</td></tr>'}</tbody>
          </table>
        </details>`;
    }).join('');

    body.html(`
      <div><b>${esc(state.customer.customer_name || state.customer.name)}</b></div>
      <div>Current Account Balance: <b>${format_money(state.customer.current_account_balance || 0)}</b></div>
      <div>Open Receivables: <b>${format_money(rec.total_outstanding || 0)}</b></div>
      ${receivables ? `
        <details>
          <summary><b>Open Account Items (${(rec.rows || []).length})</b></summary>
          <table class="table table-bordered table-condensed">
            <thead><tr><th>Date</th><th>Order</th><th>Original</th><th>Outstanding</th><th>Status</th></tr></thead>
            <tbody>${receivables}</tbody>
          </table>
        </details>` : ''}
      <hr>
      ${rows || '<div class="text-muted">No purchase history found.</div>'}
      <div class="text-muted small">Newest first · 50 records shown in this quick view · no bulk export.</div>
    `);
  }


  function load_bootstrap(state) {
    frappe.call({
      method: 'nkt_operations.nkt_store_operations.fast_screen_backend.get_fast_ui_bootstrap',
      args: { mode: state.mode, device_id: bound_device_id() },
      freeze: true
    }).then(r => {
      state.boot = r.message || {};
      state.continuityOrderOnly = MODE === 'encoder' && state.boot.continuity_order_only === true;
      if (state.continuityOrderOnly) {
        state.payments = [];
        state.cashTendered = 0;
        state.cashTenderedManual = false;
        state.paymentConfirmed = false;
      }
      role(state, 'operator').text(`${state.boot.full_name || ''} (${state.boot.user || ''})`);
      role(state, 'branch').text(state.boot.location ? state.boot.location.friendly_label : 'NOT ASSIGNED');
      role(state, 'default-warehouse').text(warehouse_label(state, state.boot.default_warehouse) || 'NOT ASSIGNED');
      if (MODE === 'cashier') {
        if (state.boot.open_shift) {
          role(state, 'shift').text(state.boot.open_shift.name);
        } else if (state.boot.blocked_shift) {
          const blockedDate = state.boot.blocked_shift.shift_start ? String(state.boot.blocked_shift.shift_start).slice(0, 10) : 'unknown date';
          role(state, 'shift').text(`${state.boot.blocked_shift.name} — BLOCKED (${blockedDate})`);
        } else {
          role(state, 'shift').text('No usable open shift');
        }
        if (state.boot.setup_error) show_warning(state, state.boot.setup_error);
        else if (!state.boot.open_shift) show_warning(state, state.boot.shift_block_reason || "No usable Cashier Shift. F10/F12 live posting is blocked until this Cashier account opens today's shift.");
      } else if (state.boot.setup_error) {
        show_warning(state, state.boot.setup_error);
      } else if (state.continuityOrderOnly) {
        show_warning(state, state.boot.continuity_message || 'Local order entry is available. Payment and new Customer creation will resume when the main connection is restored.');
      }
      render_payment_status(state);
      focus_item(state);
    }).catch(err => {
      show_warning(state, `Fast-screen setup did not load. ${posting_error_text(err) || 'Refresh the page and try again.'}`);
    });
  }

  function role(state, name) { return state.wrapper.find(`[data-role="${name}"]`); }
  function warehouse_label(state, name) { const row = state.boot?.warehouses?.find(x => x.name === name); return row ? row.label : name; }
  function show_warning(state, text) { role(state, 'warning').text(text).prop('hidden', false); }
  function focus_customer(state) { setTimeout(() => role(state, 'customer-entry').trigger('focus').select(), 0); }
  function focus_item(state) { setTimeout(() => role(state, 'item-entry').trigger('focus').select(), 0); }

  function item_keydown(state, e) {
    const results = role(state, 'item-results');
    if (!results.prop('hidden') && state.itemResults.length) {
      if (e.key === 'ArrowDown') { e.preventDefault(); state.itemIndex = Math.min(state.itemIndex + 1, state.itemResults.length - 1); render_item_results(state); return; }
      if (e.key === 'ArrowUp') { e.preventDefault(); state.itemIndex = Math.max(state.itemIndex - 1, 0); render_item_results(state); return; }
      if (e.key === 'Enter') { e.preventDefault(); choose_item(state, state.itemIndex); return; }
      if (e.key === 'Escape') { results.prop('hidden', true); return; }
    }
    if (e.key === 'Enter') { e.preventDefault(); item_enter(state); }
    if (e.key === 'ArrowDown' && state.rows.length) { e.preventDefault(); focus_qty(state, state.rows.length - 1); }
  }

  function item_enter(state) {
    const text = role(state, 'item-entry').val().trim();
    if (text) search_items(state, true);
  }

  function search_items(state, chooseFirst = false) {
    if (!state.boot) return;
    const text = role(state, 'item-entry').val().trim();
    if (!text) { role(state, 'item-results').prop('hidden', true); return; }
    frappe.call({
      method: 'nkt_operations.nkt_store_operations.fast_screen_backend.search_items',
      args: { search_text: text, warehouse: state.boot.default_warehouse, limit: 12, device_id: bound_device_id() }
    }).then(r => {
      state.itemResults = r.message || [];
      state.itemIndex = 0;
      const exact = state.itemResults.findIndex(x => x.item_code.toLowerCase() === text.toLowerCase() || (x.item_name || '').toLowerCase() === text.toLowerCase());
      if (exact >= 0) { choose_item(state, exact); return; }
      if (chooseFirst && state.itemResults.length === 1) { choose_item(state, 0); return; }
      render_item_results(state);
    }).catch(() => {
      role(state, 'item-results').html('<div class="nkt-result">Item search is temporarily unavailable.</div>').prop('hidden', false);
    });
  }

  function render_item_results(state) {
    const box = role(state, 'item-results');
    if (!state.itemResults.length) { box.html('<div class="nkt-result">No saleable item found.</div>').prop('hidden', false); return; }
    box.html(state.itemResults.map((x, i) => `<div class="nkt-result ${i === state.itemIndex ? 'active' : ''}" data-item-index="${i}"><b>${esc(x.item_code)}</b><small><span>${esc(x.item_name || '')} • ${format_money(x.standard_rate)}</span><span>Available ${format_qty(x.available_qty)}</span></small></div>`).join('')).prop('hidden', false);
  }

  function choose_item(state, index) {
    const x = state.itemResults[index];
    if (!x) return;
    state.rows.push({ item_code: x.item_code, item_name: x.item_name, qty: 1, uom: x.stock_uom, standard_rate: Number(x.standard_rate || 0), rate: Number(x.standard_rate || 0), warehouse: state.boot.default_warehouse, available: Number(x.available_qty || 0) });
    invalidate_settlement(state);
    role(state, 'item-entry').val('');
    role(state, 'item-results').prop('hidden', true);
    render_grid(state);
    focus_item(state);
  }

  function render_grid(state) {
    const body = role(state, 'grid-body');
    if (!state.rows.length) { body.html('<tr class="nkt-empty"><td colspan="10">Press F3 and enter the first item.</td></tr>'); update_totals(state); return; }
    const options = (state.boot?.warehouses || []).map(w => `<option value="${esc_attr(w.name)}">${esc(w.label)}</option>`).join('');
    body.html(state.rows.map((r, i) => {
      const diff = Number(r.rate || 0) - Number(r.standard_rate || 0);
      const adjusted = diff !== 0;
      const recognized = !adjusted || state.boot.price_variations.includes(diff);
      const stockText = MODE === 'cashier' ? (r.available >= Number(r.qty || 0) ? 'Available' : (r.available <= 0 ? 'Check with Encoder' : 'Insufficient')) : format_qty(r.available);
      const stockClass = r.available >= Number(r.qty || 0) ? 'nkt-stock-good' : 'nkt-stock-bad';
      return `<tr data-row="${i}">
        <td>${i + 1}</td><td title="${esc_attr(r.item_code)}">${esc(r.item_code)}</td><td>${esc(r.item_name || '')}</td>
        <td><input class="nkt-qty" data-row="${i}" value="${format_edit(r.qty)}"></td><td>${esc(r.uom || '')}</td>
        <td><input class="nkt-rate ${recognized ? (adjusted ? 'nkt-adjusted-rate' : '') : 'nkt-special-rate'}" data-row="${i}" value="${format_edit(r.rate)}"><span class="nkt-rate-badge ${!recognized ? 'special' : (adjusted ? 'adjusted' : '')}">${rate_badge(r, recognized)}</span></td>
        <td><select class="nkt-warehouse" data-row="${i}">${options}</select></td><td class="${stockClass} nkt-stock-cell" data-row="${i}">${stockText}</td>
        <td class="nkt-line-amount" data-row="${i}">${format_money(Number(r.qty || 0) * Number(r.rate || 0))}</td><td><button class="nkt-remove" data-row="${i}" type="button">×</button></td>
      </tr>`;
    }).join(''));
    state.rows.forEach((r, i) => body.find(`.nkt-warehouse[data-row="${i}"]`).val(r.warehouse));
    body.find('.nkt-qty').on('input', function () { const i = Number($(this).data('row')); state.rows[i].qty = Number($(this).val() || 0); invalidate_settlement(state); update_row_display(state, i); });
    body.find('.nkt-rate').on('input', function () { const i = Number($(this).data('row')); state.rows[i].rate = Number($(this).val() || 0); invalidate_settlement(state); update_rate_display(state, i, $(this)); update_row_display(state, i); });
    body.find('.nkt-warehouse').on('change', function () { const i = Number($(this).data('row')); state.rows[i].warehouse = $(this).val(); state.paymentConfirmed = false; refresh_row_context(state, i); });
    body.find('.nkt-qty').on('keydown', function (e) { const i = Number($(this).data('row')); if (e.key === 'ArrowRight' || e.key === 'Enter') { e.preventDefault(); focus_rate(state, i); } else if (e.key === 'ArrowDown') { e.preventDefault(); focus_qty(state, Math.min(i + 1, state.rows.length - 1)); } });
    body.find('.nkt-rate').on('keydown', function (e) { const i = Number($(this).data('row')); if (e.key === 'Enter') { e.preventDefault(); focus_item(state); } else if (e.key === 'ArrowLeft') { e.preventDefault(); focus_qty(state, i); } else if (e.key === 'ArrowDown') { e.preventDefault(); focus_rate(state, Math.min(i + 1, state.rows.length - 1)); } });
    update_totals(state);
  }

  function refresh_row_context(state, i) {
    const r = state.rows[i];
    frappe.call({
      method: 'nkt_operations.nkt_store_operations.fast_screen_backend.get_item_context',
      args: { item_code: r.item_code, warehouse: r.warehouse, device_id: bound_device_id() }
    }).then(x => {
      r.available = Number((x.message || {}).available_qty || 0);
      if (x.message && Number.isFinite(Number(x.message.standard_rate))) r.standard_rate = Number(x.message.standard_rate || 0);
      update_row_display(state, i);
    }).catch(() => show_warning(state, 'Item availability could not be refreshed right now.'));
  }

  function update_row_display(state, i) {
    const r = state.rows[i];
    role(state, 'grid-body').find(`.nkt-line-amount[data-row="${i}"]`).text(format_money(Number(r.qty || 0) * Number(r.rate || 0)));
    const stock = role(state, 'grid-body').find(`.nkt-stock-cell[data-row="${i}"]`);
    const good = r.available >= Number(r.qty || 0);
    const text = MODE === 'cashier' ? (good ? 'Available' : (r.available <= 0 ? 'Check with Encoder' : 'Insufficient')) : format_qty(r.available);
    stock.text(text).toggleClass('nkt-stock-good', good).toggleClass('nkt-stock-bad', !good);
    update_totals(state);
  }
  function update_rate_display(state, i, input) {
    const r = state.rows[i]; const diff = Number(r.rate || 0) - Number(r.standard_rate || 0); const recognized = !diff || state.boot.price_variations.includes(diff);
    input.toggleClass('nkt-special-rate', !recognized).toggleClass('nkt-adjusted-rate', recognized && !!diff);
    input.siblings('.nkt-rate-badge').attr('class', `nkt-rate-badge ${!recognized ? 'special' : (diff ? 'adjusted' : '')}`).text(rate_badge(r, recognized));
  }
  function focus_qty(state, i) { setTimeout(() => role(state, 'grid-body').find(`.nkt-qty[data-row="${i}"]`).trigger('focus').select(), 0); }
  function focus_rate(state, i) { setTimeout(() => role(state, 'grid-body').find(`.nkt-rate[data-row="${i}"]`).trigger('focus').select(), 0); }
  function rate_badge(r, recognized) { const d = Number(r.rate || 0) - Number(r.standard_rate || 0); if (!d) return 'Normal'; if (recognized) return `${d > 0 ? '+' : ''}${format_plain_money(d)} Preset`; return 'Special'; }

  function search_customers(state) {
    const text = role(state, 'customer-entry').val().trim();
    if (!text) { role(state, 'customer-results').prop('hidden', true); return; }
    frappe.call({
      method: 'nkt_operations.nkt_store_operations.fast_screen_backend.search_customers',
      args: { search_text: text, limit: 12, device_id: bound_device_id() }
    }).then(r => {
      state.customerResults = r.message || [];
      state.customerIndex = 0;
      render_customer_results(state);
    }).catch(() => {
      role(state, 'customer-results').html('<div class="nkt-result">Customer search is temporarily unavailable.</div>').prop('hidden', false);
    });
  }

  function customer_keydown(state, e) {
    if (!role(state, 'customer-results').prop('hidden') && state.customerResults.length) {
      if (e.key === 'ArrowDown') { e.preventDefault(); state.customerIndex = Math.min(state.customerIndex + 1, state.customerResults.length - 1); render_customer_results(state); return; }
      if (e.key === 'ArrowUp') { e.preventDefault(); state.customerIndex = Math.max(state.customerIndex - 1, 0); render_customer_results(state); return; }
      if (e.key === 'Enter') { e.preventDefault(); choose_customer(state, state.customerIndex); return; }
      if (e.key === 'Escape') { role(state, 'customer-results').prop('hidden', true); focus_item(state); return; }
    }
    if (e.key === 'Enter') { e.preventDefault(); search_customers(state); }
  }
  function render_customer_results(state) {
    const box = role(state, 'customer-results');
    if (!state.customerResults.length) {
      const msg = state.continuityOrderOnly
        ? 'Customer is not available in the local customer list. Try again when the main connection returns.'
        : 'No customer found. Click New Customer to create one.';
      box.html(`<div class="nkt-result">${esc(msg)}</div>`).prop('hidden', false);
      return;
    }
    box.html(state.customerResults.map((x, i) => {
      const balance = state.securityMode === 'limited' ? '' : `<span>Balance ${format_money(x.current_account_balance)}</span>`;
      return `<div class="nkt-result ${i === state.customerIndex ? 'active' : ''}" data-customer-index="${i}"><b>${esc(x.customer_name || x.name)}</b><small><span>${esc(x.name)}</span>${balance}</small></div>`;
    }).join('')).prop('hidden', false);
  }

  function choose_customer(state, i) {
    const x = state.customerResults[i]; if (!x) return;
    const changed = state.customer && state.customer.name !== x.name;
    state.customer = x;
    if (changed) { state.payments = []; state.cashTendered = 0; state.paymentConfirmed = false; }
    role(state, 'customer-entry').val(x.customer_name || x.name);
    role(state, 'customer-selected').find('.nkt-customer-name').text(x.customer_name || x.name);
    role(state, 'customer-balance').text(format_money(x.current_account_balance));
    role(state, 'customer-status').text('Selected');
    role(state, 'customer-results').prop('hidden', true);
    render_payments(state);
    focus_item(state);
  }

  function open_fast_new_customer(state) {
    if (state.continuityOrderOnly) {
      frappe.msgprint(__('New Customer creation will resume when the main connection is restored. Select an existing Customer from the local list for this order.'));
      return;
    }
    const d = new frappe.ui.Dialog({
      title: __('New Customer'),
      fields: [
        { fieldname: 'customer_name', fieldtype: 'Data', label: __('Customer Name'), reqd: 1 },
        { fieldname: 'customer_type', fieldtype: 'Select', label: __('Customer Type'), options: 'Individual\nCompany', default: 'Individual', reqd: 1 },
        { fieldname: 'mobile_no', fieldtype: 'Data', label: __('Mobile Number') }
      ],
      primary_action_label: __('Create Customer'),
      primary_action(values) {
        if (!values || !(values.customer_name || '').trim()) return;
        d.disable_primary_action();

        frappe.call({
          method: 'nkt_operations.nkt_store_operations.features.fast_screen.fast_customer_creation.create_fast_customer',
          args: {
            customer_name: values.customer_name,
            customer_type: values.customer_type || 'Individual',
            mobile_no: values.mobile_no || ''
          },
          freeze: true,
          freeze_message: __('Creating Customer…')
        }).then(r => {
          const x = r.message || {};
          if (!x.customer) frappe.throw(__('Customer creation returned no Customer.'));

          const previous = state.customer && state.customer.name ? state.customer.name : null;
          state.customer = {
            name: x.customer,
            customer_name: x.customer_name || x.customer,
            mobile_no: x.mobile_no || '',
            territory: x.territory || '',
            current_account_balance: Number(x.current_account_balance || 0)
          };

          if (!previous || previous !== state.customer.name) {
            state.payments = [];
            state.cashTendered = 0;
            state.cashTenderedManual = false;
            state.paymentConfirmed = false;
          }

          state.customerResults = [];
          state.customerIndex = 0;
          role(state, 'customer-entry').val(state.customer.customer_name);
          role(state, 'customer-selected').find('.nkt-customer-name').text(state.customer.customer_name);
          role(state, 'customer-balance').text(format_money(state.customer.current_account_balance));
          role(state, 'customer-status').text('Selected');
          role(state, 'customer-results').prop('hidden', true);
          render_payments(state);
          d.hide();

          frappe.show_alert({
            message: x.created ? __('Customer created and selected.') : __('That Customer already exists and was selected.'),
            indicator: x.created ? 'green' : 'blue'
          });
          focus_item(state);
        }).catch(() => d.enable_primary_action());
      }
    });

    d.show();
    setTimeout(() => {
      const field = d.get_field('customer_name');
      if (field && field.$input) field.$input.trigger('focus');
    }, 80);
  }
  function update_totals(state) {
    const qty = state.rows.reduce((a, r) => a + Number(r.qty || 0), 0);
    role(state, 'total-qty').text(format_qty(qty));
    role(state, 'grand-total').text(format_money(total(state)));
    if (state.payments.length) rebalance_cash(state);
    if (state.payments.length && Math.abs(payment_totals(state).paymentTotal - total(state)) > 0.005) state.paymentConfirmed = false;
    render_payment_status(state);
  }
  function total(state) { return round2(state.rows.reduce((a, r) => a + Number(r.qty || 0) * Number(r.rate || 0), 0)); }
  function invalidate_settlement(state) { state.paymentConfirmed = false; }

  function payment_totals(state) {
    const receiptTotal = round2(total(state));
    const ci = cash_row_index(state);
    const nonCash = round2(state.payments.reduce((a, p, i) => a + (i === ci ? 0 : Number(p.amount || 0)), 0));
    const cashDue = round2(Math.max(receiptTotal - nonCash, 0));
    const cashTendered = ci >= 0 ? round2(Math.max(Number(state.cashTendered || 0), 0)) : 0;
    const cashApplied = ci >= 0 ? round2(Math.min(cashTendered, cashDue)) : 0;
    const paymentTotal = round2(nonCash + cashApplied);
    const balance = round2(receiptTotal - paymentTotal);
    const change = ci >= 0 ? round2(Math.max(cashTendered - cashDue, 0)) : 0;
    const cardSurcharge = round2(state.payments.reduce((a, p) => a + (p.method === 'Card' ? Number(p.amount || 0) * 0.02 : 0), 0));
    const accountPrincipal = round2(state.payments.reduce((a, p) => a + (p.method === 'Account' ? Number(p.amount || 0) : 0), 0));
    const actualPrincipalCollected = round2(Math.max(paymentTotal - accountPrincipal, 0));
    return {
      receiptTotal, paymentTotal, balance, cashDue, cashApplied, nonCash,
      cashTendered, change, cardSurcharge,
      grossCollected: round2(actualPrincipalCollected + cardSurcharge),
      nonCashExcess: round2(Math.max(nonCash - receiptTotal, 0)),
      cashRowPresent: ci >= 0,
    };
  }

  function cash_row_index(state) { return state.payments.findIndex(p => p.method === 'Cash'); }

  function rebalance_cash(state) {
    const ci = cash_row_index(state);
    if (ci < 0) return;
    const nonCash = round2(state.payments.reduce((a, p, i) => a + (i === ci ? 0 : Number(p.amount || 0)), 0));
    const due = round2(Math.max(total(state) - nonCash, 0));
    state.payments[ci].amount = due;
    if (!state.cashTenderedManual) state.cashTendered = due;
  }

  function open_payment_preview(state) {
    if (state.continuityOrderOnly) {
      frappe.msgprint(__('Payment entry is temporarily unavailable. Save the order now and record the truthful payment/account settlement after the main connection is restored.'));
      return;
    }
    if (!state.rows.length) { frappe.msgprint(__('Enter at least one item.')); focus_item(state); return; }
    if (!state.customer) { frappe.msgprint(__('Select an actual Customer before payment.')); focus_customer(state); return; }
    if (!state.payments.length) set_exact_cash(state);
    rebalance_cash(state);
    const d = new frappe.ui.Dialog({ title: __('Take Payment — settle the whole receipt'), fields: [{ fieldtype: 'HTML', fieldname: 'payment_grid' }] });
    state.paymentDialog = d;
    d.show();
    d.$wrapper.find('.modal-dialog').addClass('nkt-payment-dialog');
    render_payment_dialog(state, d);
    d.$wrapper.on('hidden.bs.modal', () => {
      state.paymentDialog = null;
      render_payments(state);
      // Return the operator to the Fast Screen immediately after Payment closes.
      setTimeout(() => focus_item(state), 0);
    });
  }

  function set_exact_cash(state) {
    const due = total(state);
    state.payments = [{ method: 'Cash', amount: due, reference: '', provider: '', check_date: '' }];
    state.cashTendered = due;
    state.cashTenderedManual = false;
    state.paymentConfirmed = false;
  }

  function add_payment_row(state) {
    const defaultMethod = state.payments.some(p => p.method === 'Cash') ? 'Bank Transfer' : 'Cash';
    state.payments.push({ method: defaultMethod, amount: 0, reference: '', provider: '', check_date: '' });
    if (defaultMethod === 'Cash') {
      state.cashTenderedManual = false;
      state.cashTendered = 0;
    }
    rebalance_cash(state);
    state.paymentConfirmed = false;
  }

  function render_payment_dialog(state, d, focusSpec = null) {
    rebalance_cash(state);
    const box = d.fields_dict.payment_grid.$wrapper;
    const methods = ['Cash', 'Check', 'GCash', 'Maya', 'Card', 'Bank Transfer', 'Online', 'Account'];
    const totals = payment_totals(state);
    const hasAdjusted = state.rows.some(r => Number(r.rate || 0) !== Number(r.standard_rate || 0));
    box.html(`<div class="nkt-payment-grid-shell">
      <div class="nkt-payment-summary">
        <div class="nkt-summary-box"><span>Receipt Total</span><b>${format_money(totals.receiptTotal)}</b></div>
        <div class="nkt-summary-box"><span>Settled</span><b data-pay-summary="total">${format_money(totals.paymentTotal)}</b></div>
        <div class="nkt-summary-box remaining ${Math.abs(totals.balance) <= .005 ? 'ok' : 'bad'}"><span>Balance</span><b data-pay-summary="balance">${format_money(totals.balance)}</b></div>
        <div class="nkt-summary-box change ${totals.change > .005 ? 'has-change' : ''}"><span>Change</span><b data-pay-summary="change">${format_money(totals.change)}</b></div>
      </div>
      <table class="nkt-pay-table"><thead><tr><th class="p-method">Method</th><th class="p-amount">Amount / Tendered</th><th class="p-ref">Reference / Check No.</th><th class="p-date">Check Date</th><th class="p-provider">Bank / Provider</th><th class="p-remove"></th></tr></thead>
      <tbody>${state.payments.map((p, i) => payment_row_markup(methods, p, i, state.payments.length, state)).join('')}</tbody></table>
      <div class="nkt-pay-actions"><button type="button" data-pay-action="add">Add Payment Row</button><button type="button" data-pay-action="exact">Exact Cash</button><div class="spacer"></div><button type="button" data-pay-action="review" class="btn-primary">Review Payment</button></div>
      <div class="nkt-payment-note">${hasAdjusted ? '<b>Price:</b> adjusted rates may be previewed but cannot be posted in V2.0C.3; reset to Normal for the first live test.<br>' : ''}<b>Cash:</b> Encoder records the settled Cash Due only. Physical Cash Tendered and Change belong to the Cashier transaction.<br><b>Check:</b> record Check Number, Check Date, and Issuing Bank. Depositing and clearing remain later workflows.<br><b>Card:</b> exact 2% surcharge applies only to the Card amount and is added to the actual amount collected. Maya has no surcharge.</div>
    </div>`);

    box.off('.nktPay');
    box.on('change.nktPay', '.nkt-pay-method', function () {
      const i = Number($(this).data('row')); const method = $(this).val();
      const oldMethod = state.payments[i].method;
      if (method === 'Card') {
        state.payments[i].provider = state.payments[i].provider || 'Card Terminal';
      }
      if (method === 'Cash' && state.payments.some((p, idx) => idx !== i && p.method === 'Cash')) {
        frappe.msgprint(__('Only one Cash row is needed.'));
        $(this).val(oldMethod);
        return;
      }
      state.payments[i].method = method;
      if (oldMethod === 'Cash' && method !== 'Cash') {
        state.cashTendered = 0;
        state.cashTenderedManual = false;
      }
      if (method === 'Cash') {
        state.payments[i].reference = '';
        state.payments[i].provider = '';
        state.payments[i].check_date = '';
        state.cashTenderedManual = false;
      } else if (method !== 'Check') {
        state.payments[i].check_date = '';
      }
      rebalance_cash(state);
      state.paymentConfirmed = false;
      // The method change re-renders the row. Resume on the first usable
      // data-entry control in that same row instead of jumping back to Method.
      render_payment_dialog(state, d, { row: i, afterMethod: true });
    });
    box.on('input.nktPay change.nktPay', '.nkt-pay-input', function () {
      const i = Number($(this).data('row')); const field = $(this).data('field');
      if (field === 'cash_tendered') {
        state.cashTendered = Number($(this).val() || 0);
        state.cashTenderedManual = true;
      } else {
        state.payments[i][field] = field === 'amount' ? Number($(this).val() || 0) : $(this).val();
        if (field === 'amount') rebalance_cash(state);
      }
      state.paymentConfirmed = false; update_payment_dialog_summary(state, box);
    });
    box.on('click.nktPay', '[data-pay-remove]', function () {
      const i = Number($(this).data('pay-remove'));
      const removedCash = state.payments[i] && state.payments[i].method === 'Cash';
      state.payments.splice(i, 1);
      if (removedCash) { state.cashTendered = 0; state.cashTenderedManual = false; }
      if (!state.payments.length) set_exact_cash(state); else rebalance_cash(state);
      state.paymentConfirmed = false;
      render_payment_dialog(state, d);
    });
    box.on('click.nktPay', '[data-pay-action="add"]', () => { add_payment_row(state); render_payment_dialog(state, d); });
    box.on('click.nktPay', '[data-pay-action="exact"]', () => { set_exact_cash(state); render_payment_dialog(state, d); });
    box.on('click.nktPay', '[data-pay-action="review"]', () => review_settlement(state, d));
    box.on('keydown.nktPay', 'input,select,button', function (e) {
      if (e.key === 'F1') {
        e.preventDefault();
        e.stopImmediatePropagation();
        return false;
      }
      if (e.key === 'Escape') {
        e.preventDefault();
        d.hide();
        return;
      }

      const focusable = box.find(
        'select:visible:not(:disabled),input:visible:not([readonly]):not([disabled]),button:visible:not(:disabled)'
      );
      const idx = focusable.index(this);

      if (e.key === 'Tab') {
        e.preventDefault();
        if (!focusable.length) return;
        const direction = e.shiftKey ? -1 : 1;
        let next = idx >= 0 ? idx + direction : (direction > 0 ? 0 : focusable.length - 1);
        if (next < 0) next = focusable.length - 1;
        if (next >= focusable.length) next = 0;
        const target = focusable.eq(next);
        target.trigger('focus');
        if (target.is('input')) target.select();
        return;
      }

      if (e.key !== 'Enter') return;
      e.preventDefault();
      if (idx >= 0 && idx < focusable.length - 1) {
        const target = focusable.eq(idx + 1);
        target.trigger('focus');
        if (target.is('input')) target.select();
      } else {
        review_settlement(state, d);
      }
    });

    setTimeout(() => {
      let target = $();
      if (focusSpec && Number.isInteger(Number(focusSpec.row))) {
        const row = Number(focusSpec.row);
        if (focusSpec.afterMethod) {
          target = box.find(`.nkt-pay-input[data-row="${row}"]:visible:not([readonly]):not([disabled])`).first();
        }
        if (!target.length) target = box.find(`.nkt-pay-method[data-row="${row}"]`).first();
      }
      if (!target.length) target = box.find('.nkt-pay-method').first();
      target.trigger('focus');
      if (target.is('input')) target.select();
    }, 25);
  }

  function payment_row_markup(methods, p, i, count, state) {
    const credit = p.method === 'Card';
    const cash = p.method === 'Cash';
    const check = p.method === 'Check';
    const cashTenderInput = cash && MODE === 'cashier';
    const field = cashTenderInput ? 'cash_tendered' : 'amount';
    const value = cashTenderInput ? state.cashTendered : p.amount;
    const amountReadonly = (cash && MODE !== 'cashier');
    const refDisabled = cash || p.method === 'Account';
    const refPlaceholder = cash || p.method === 'Account' ? 'Not required' : (check ? 'Check number required' : 'Required');
    return `<tr>
      <td><select class="nkt-pay-method" data-row="${i}">${methods.map(m => `<option value="${m}" ${m === p.method ? 'selected' : ''}>${m}</option>`).join('')}</select></td>
      <td><input class="nkt-pay-input" data-row="${i}" data-field="${field}" type="number" min="0" step="0.01" value="${format_edit(value)}" ${amountReadonly ? 'readonly' : ''} placeholder="${cashTenderInput ? 'Cash handed over' : ''}"></td>
      <td><input class="nkt-pay-input" data-row="${i}" data-field="reference" value="${esc_attr(p.reference || '')}" placeholder="${refPlaceholder}" ${refDisabled ? 'readonly' : ''}></td>
      <td><input class="nkt-pay-input" data-row="${i}" data-field="check_date" type="date" value="${esc_attr(p.check_date || '')}" ${check ? '' : 'disabled'}></td>
      <td><input class="nkt-pay-input" data-row="${i}" data-field="provider" value="${esc_attr(p.provider || '')}" placeholder="${check ? 'Issuing bank required' : ''}" ${cash ? 'readonly' : ''}></td>
      <td><button type="button" data-pay-remove="${i}" >×</button></td>
    </tr>`;
  }

  function update_payment_dialog_summary(state, box) {
    const totals = payment_totals(state);
    box.find('[data-pay-summary="total"]').text(format_money(totals.paymentTotal));
    box.find('[data-pay-summary="balance"]').text(format_money(totals.balance)).closest('.remaining').toggleClass('ok', Math.abs(totals.balance) <= .005).toggleClass('bad', Math.abs(totals.balance) > .005);
    box.find('[data-pay-summary="change"]').text(format_money(totals.change)).closest('.change').toggleClass('has-change', totals.change > .005);
    state.payments.forEach((p, i) => {
      if (p.method === 'Cash') {
        const field = MODE === 'cashier' ? 'cash_tendered' : 'amount';
        const value = MODE === 'cashier' ? state.cashTendered : p.amount;
        box.find(`.nkt-pay-input[data-row="${i}"][data-field="${field}"]`).val(format_edit(value));
      }
    });
  }

  function validate_settlement(state) {
    if (!state.payments.length) return 'Enter at least one payment row.';
    const t = payment_totals(state);
    if (t.nonCashExcess > .005) return `Non-cash payments exceed the receipt total by ${format_money(t.nonCashExcess)}. Reduce the excessive non-cash row.`;
    for (let i = 0; i < state.payments.length; i++) {
      const p = state.payments[i];
      if (p.method === 'Cash') continue;
      if (Number(p.amount || 0) <= 0) return `Payment row ${i + 1} must have a positive amount.`;
      if (p.method === 'Check') {
        if (!(p.reference || '').trim()) return `Check Number is required on payment row ${i + 1}.`;
        if (!(p.check_date || '').trim()) return `Check Date is required on payment row ${i + 1}.`;
        if (!(p.provider || '').trim()) return `Issuing Bank is required on payment row ${i + 1}.`;
      } else if (!['Account'].includes(p.method) && !(p.reference || '').trim()) {
        return `Reference Number is required on payment row ${i + 1}.`;
      }
    }
    if (t.cashDue > .005 && !t.cashRowPresent) return `A remaining balance of ${format_money(t.cashDue)} still needs Cash or another payment row.`;
    if (t.cashDue > .005 && t.cashTendered + .005 < t.cashDue) return `Cash Tendered is short by ${format_money(t.cashDue - t.cashTendered)}.`;
    if (t.balance > .005) return `The whole receipt is not yet settled. Remaining balance: ${format_money(t.balance)}.`;
    return '';
  }

  function payment_description(p) {
    if (p.method === 'Check') return `${p.method} — ${p.reference || ''}${p.check_date ? ` • ${p.check_date}` : ''}${p.provider ? ` • ${p.provider}` : ''}`;
    return `${p.method}${p.reference ? ` — ${p.reference}` : ''}`;
  }

  function check_record_markup(payment, hit, currentCustomer) {
    return `<div style="font-family:Tahoma,Arial,sans-serif;font-size:13px;line-height:1.55">
      <div style="display:grid;grid-template-columns:145px 1fr;gap:4px 12px;border:1px solid #d8d8d8;background:#fafafa;padding:10px 12px;margin:8px 0 12px">
        <div><b>Check Number</b></div><div>${esc(payment.reference || '')}</div>
        <div><b>Current Customer</b></div><div>${esc(currentCustomer || '')}</div>
        <div><b>Current Bank</b></div><div>${esc(payment.provider || '')}</div>
        <div><b>Existing Customer</b></div><div>${esc(hit.customer || '')}</div>
        <div><b>Existing Bank</b></div><div>${esc(hit.issuing_bank || '')}</div>
        <div><b>Existing Receipt</b></div><div>${esc(hit.payment_receipt || '')}</div>
        ${hit.cashier_sale ? `<div><b>Cashier Sale</b></div><div>${esc(hit.cashier_sale)}</div>` : ''}
      </div>`;
  }

  function possible_duplicate_check_dialog(state, payment, hit) {
    return new Promise(resolve => {
      let settled = false;
      const finish = value => { if (settled) return; settled = true; d.hide(); resolve(value); };
      const d = new frappe.ui.Dialog({
        title: __('Possible Duplicate Check'),
        fields: [{ fieldtype: 'HTML', fieldname: 'body' }],
        primary_action_label: __('Continue Anyway'),
        primary_action: () => finish(true),
        secondary_action_label: __('Go Back'),
        secondary_action: () => finish(false)
      });
      d.fields_dict.body.$wrapper.html(`${check_record_markup(payment, hit, state.customer && state.customer.name)}
        <div>This Check Number has been used before, but the Customer or Issuing Bank is different. Verify the details above before continuing.</div>`);
      d.$wrapper.on('hidden.bs.modal.nktCheckPreflight', () => { if (!settled) { settled = true; resolve(false); } });
      d.show();
    });
  }

  function preflight_incoming_checks(state) {
    const checks = state.payments.filter(p => p.method === 'Check');
    if (!checks.length) return Promise.resolve(true);
    if (!state.customer || !state.customer.name) return Promise.resolve(false);

    const calls = checks.map((p, idx) => frappe.call({
      method: 'nkt_operations.nkt_store_operations.fast_screen_backend.preflight_incoming_check',
      args: {
        mode: MODE,
        customer: state.customer.name,
        check_number: p.reference || '',
        issuing_bank: p.provider || ''
      },
      freeze: false
    }).then(r => ({ row: idx + 1, payment: p, result: r.message || {} })));

    return Promise.all(calls).then(async results => {
      const exact = results.find(x => x.result && x.result.exact_duplicate);
      if (exact) {
        const hit = exact.result.exact_match || {};
        frappe.msgprint({
          title: __('Duplicate Check'),
          indicator: 'red',
          message: `${check_record_markup(exact.payment, hit, state.customer.name)}
            <div><b>Posting blocked.</b> This physical Check has already been recorded${MODE === 'encoder' ? ' and is not available for a new Encoder transaction' : ''}. Verify the Customer, Issuing Bank, and Check Number.</div>`
        });
        return false;
      }

      if (MODE === 'encoder') {
        results.filter(x => x.result && x.result.encoder_match_available).forEach(x => {
          const hit = x.result.exact_match || {};
          frappe.show_alert({ message: __(`Matching Cashier Check found${hit.payment_receipt ? ` in ${hit.payment_receipt}` : ''}.`), indicator: 'green' });
        });
      }

      const warnings = [];
      results.forEach(x => {
        const matches = (x.result && x.result.other_matches) || [];
        matches.forEach(hit => warnings.push({ payment: x.payment, hit }));
      });
      for (const warning of warnings) {
        const proceed = await possible_duplicate_check_dialog(state, warning.payment, warning.hit || {});
        if (!proceed) return false;
      }
      return true;
    }).catch(err => {
      const detail = posting_error_text(err) || (err && err.message) || 'Check preflight could not be completed.';
      frappe.msgprint({ title: __('Check Validation Could Not Be Completed'), indicator: 'red', message: esc(detail) });
      return false;
    });
  }

  function show_payment_confirmation(state, paymentDialog) {
    const t = payment_totals(state);
    const nonCashBreakdown = state.payments.filter(p => p.method !== 'Cash').map(p => `<div class="nkt-confirm-row"><span>${esc(payment_description(p))}</span><b>${format_money(p.amount)}</b></div>`).join('');
    const d = new frappe.ui.Dialog({ title: __('Payment Confirmation'), fields: [{ fieldtype: 'HTML', fieldname: 'confirmation' }] });
    d.show();
    d.fields_dict.confirmation.$wrapper.html(`<div class="nkt-confirm-box"><div class="nkt-confirm-title">RECEIPT PAYMENT CONFIRMATION</div>
      <div class="nkt-confirm-row"><span>Receipt Total</span><b>${format_money(total(state))}</b></div>${nonCashBreakdown}
      ${t.cashRowPresent ? `<div class="nkt-confirm-row"><span>Cash Due</span><b>${format_money(t.cashDue)}</b></div><div class="nkt-confirm-row"><span>Cash Tendered</span><b>${format_money(t.cashTendered)}</b></div>` : ''}
      ${t.cardSurcharge ? `<div class="nkt-confirm-row"><span>Card Surcharge — 2%</span><b>${format_money(t.cardSurcharge)}</b></div><div class="nkt-confirm-row"><span>Actual Collected</span><b>${format_money(t.grossCollected)}</b></div>` : ''}
      ${t.cashRowPresent ? `<div class="nkt-confirm-change">CHANGE: ${format_money(t.change)}</div>` : ''}
      <div class="nkt-confirm-hint">Press Enter to Confirm • Esc to Return</div></div>`);
    const confirm = () => {
      state.paymentConfirmed = true;
      render_payments(state);
      d.hide(); paymentDialog.hide();
      frappe.show_alert({ message: __('Payment confirmed. Press F10 or F12 to post the transaction.'), indicator: 'green' });
    };
    d.set_primary_action(__('Confirm Payment'), confirm);
    const handler = e => { if (e.key === 'Enter' && !$(e.target).is('textarea')) { e.preventDefault(); confirm(); } };
    $(document).off('keydown.nktSettlementConfirm').on('keydown.nktSettlementConfirm', handler);
    d.$wrapper.on('hidden.bs.modal', () => $(document).off('keydown.nktSettlementConfirm'));
  }

  function review_settlement(state, paymentDialog) {
    const error = validate_settlement(state);
    if (error) { frappe.msgprint({ title: __('Payment is not complete'), indicator: 'red', message: __(error) }); return; }
    preflight_incoming_checks(state).then(ok => {
      if (ok) show_payment_confirmation(state, paymentDialog);
    });
  }

  function render_payments(state) {
    const box = role(state, 'payment-lines');
    if (!state.payments.length) { box.text('Press F11 to settle the whole receipt using one or more payment rows.'); render_payment_status(state); return; }
    const t = payment_totals(state);
    box.html(state.payments.map(p => {
      if (p.method === 'Cash') return `<div class="nkt-payment-line"><span>Cash Due</span><b>${format_money(t.cashDue)}</b>${state.paymentConfirmed ? `<small>Tendered ${format_money(t.cashTendered)} • Change ${format_money(t.change)}</small>` : `<small>Cash handed over ${format_money(t.cashTendered)} • Change ${format_money(t.change)}</small>`}</div>`;
      return `<div class="nkt-payment-line"><span>${esc(payment_description(p))}</span><b>${format_money(p.amount)}</b>${p.method === 'Card' ? `<small>2% Card surcharge ${format_money(Number(p.amount || 0) * .02)} • Gross ${format_money(Number(p.amount || 0) * 1.02)}</small>` : ''}</div>`;
    }).join('') + `<div class="nkt-payment-line"><span>Balance</span><b>${format_money(t.balance)}</b></div>`);
    render_payment_status(state);
  }

  function render_payment_status(state) {
    const status = role(state, 'payment-status');
    if (state.continuityOrderOnly) {
      status.text('Payment after connection');
      return;
    }
    if (!state.payments.length) status.text('Not entered');
    else if (state.paymentConfirmed && Math.abs(payment_totals(state).balance) <= .005) status.text('Preview confirmed');
    else if (Math.abs(payment_totals(state).balance) <= .005) status.text('Ready to confirm');
    else status.text(`Balance ${format_money(payment_totals(state).balance)}`);
  }

  function build_live_payload(state) {
    const t = payment_totals(state);
    return {
      request_id: state.requestId,
      customer: state.customer ? state.customer.name : '',
      items: state.rows.map(r => ({
        item_code: r.item_code,
        qty: Number(r.qty || 0),
        rate: Number(r.rate || 0),
        warehouse: r.warehouse
      })),
      payments: state.payments.map(p => ({
        method: p.method,
        amount: p.method === 'Cash' ? t.cashDue : Number(p.amount || 0),
        reference: p.reference || '',
        provider: p.provider || '',
        check_date: p.check_date || '',
        cash_tendered: p.method === 'Cash' ? t.cashTendered : 0,
        change_amount: p.method === 'Cash' ? t.change : 0
      })),
      cash_tendered: t.cashTendered,
      change_amount: t.change
    };
  }

  function clear_posting_watchdog(state) {
    if (state.postingWatchdog) {
      clearTimeout(state.postingWatchdog);
      state.postingWatchdog = null;
    }
  }

  function release_posting_attempt(state, attempt) {
    if (attempt !== state.postingAttempt) return false;
    clear_posting_watchdog(state);
    state.finalizing = false;
    state.postingAttempt += 1;
    set_posting_state(state, false);
    return true;
  }

  function finish_posting_success(state, result, print, attempt) {
    if (!release_posting_attempt(state, attempt)) return;
    state.lastPostCompletedAt = Date.now();
    show_posting_result(state, result, print);
    if (print && result.print_html) open_local_order_print(result.print_html);
    else if (print && result.print_url) window.open(result.print_url, '_blank', 'noopener');
    clear_transaction(state);
  }

  function recover_posting_status(state, print, attempt, pollNo) {
    if (!state.finalizing || attempt !== state.postingAttempt) return;
    if (!pollNo) frappe.show_alert({ message: __('Posting is taking longer than expected. Checking this transaction before allowing any retry…'), indicator: 'orange' });
    frappe.call({
      method: 'nkt_operations.nkt_store_operations.fast_screen_backend.get_fast_request_status',
      args: { mode: MODE, request_id: state.requestId, device_id: bound_device_id() },
      freeze: false
    }).then(r => {
      if (!state.finalizing || attempt !== state.postingAttempt) return;
      const status = r.message || {};
      if (status.found && status.submitted && status.result && status.result.ok) {
        if (status.result.local_continuity) state.continuityOrderOnly = true;
        finish_posting_success(state, status.result, print, attempt);
        return;
      }
      if (status.found && !status.submitted) {
        if (!release_posting_attempt(state, attempt)) return;
        frappe.msgprint({
          title: __('Posting requires inspection'),
          indicator: 'red',
          message: __('This transaction exists but is not complete. Do not re-encode it; ask an Administrator to inspect it.')
        });
        return;
      }
      if ((pollNo || 0) < 4) {
        state.postingWatchdog = setTimeout(() => recover_posting_status(state, print, attempt, (pollNo || 0) + 1), 4000);
        return;
      }
      if (!release_posting_attempt(state, attempt)) return;
      frappe.msgprint({
        title: __('Posting response was lost'),
        indicator: 'orange',
        message: __('No completed transaction is visible yet. The screen data was kept. You may press Finalize again; the same protected transaction identity will be reused so a retry cannot intentionally create a second copy.')
      });
    }).catch(() => {
      if (!state.finalizing || attempt !== state.postingAttempt) return;
      if ((pollNo || 0) < 4) state.postingWatchdog = setTimeout(() => recover_posting_status(state, print, attempt, (pollNo || 0) + 1), 4000);
    });
  }

  function finalize_live(state, print) {
    if (!state.rows.length && !state.customer && state.lastPostCompletedAt && (Date.now() - state.lastPostCompletedAt) < 15000) return;
    if (state.finalizing) {
      frappe.show_alert({ message: __('Posting is already in progress. The screen is checking this transaction automatically; do not start a second transaction.'), indicator: 'orange' });
      return;
    }
    if (!state.rows.length || !state.customer) {
      frappe.msgprint(__('Customer and at least one item are required.'));
      return;
    }
    if (!state.continuityOrderOnly && !state.paymentConfirmed) {
      frappe.msgprint(__('Confirm the complete F11 payment settlement first.'));
      open_payment_preview(state);
      return;
    }
    if (!state.boot || !state.boot.posting_enabled) {
      frappe.msgprint(__('Live posting is not enabled on this fast screen.'));
      return;
    }
    if (MODE === 'cashier' && !state.boot.open_shift) frappe.show_alert({ message: __('Rechecking the Cashier Shift on the server…'), indicator: 'orange' });
    // NKT_MANAGER_PIN_ENCODER_MP1
    // Encoder independently re-enters/confirms selling rate; Manager PIN is Cashier-side only.
    const method = MODE === 'cashier'
      ? 'nkt_operations.nkt_store_operations.fast_screen_backend.finalize_cashier_fast_transaction'
      : 'nkt_operations.nkt_store_operations.fast_screen_backend.finalize_encoder_fast_transaction';
    const attempt = state.postingAttempt + 1;
    state.postingAttempt = attempt;
    state.finalizing = true;
    set_posting_state(state, true);
    clear_posting_watchdog(state);
    state.postingWatchdog = setTimeout(() => recover_posting_status(state, print, attempt, 0), 12000);
    frappe.call({
      method,
      args: { payload: JSON.stringify(build_live_payload(state)), device_id: bound_device_id() },
      freeze: true,
      freeze_message: MODE === 'cashier'
        ? __('Posting Cashier transaction…')
        : (state.continuityOrderOnly ? __('Saving order…') : __('Posting Encoder transaction…'))
    }).then(r => {
      if (attempt !== state.postingAttempt || !state.finalizing) return;
      const result = r.message || {};
      if (result.order_only_required) {
        if (!release_posting_attempt(state, attempt)) return;
        state.continuityOrderOnly = true;
        state.payments = [];
        state.cashTendered = 0;
        state.cashTenderedManual = false;
        state.paymentConfirmed = false;
        render_payments(state);
        render_payment_status(state);
        frappe.msgprint({
          title: __('Order entry remains available'),
          indicator: 'orange',
          message: __('Payment was NOT recorded. The connection changed while this transaction was open. Press F10/F12 again to save the order only, then record payment after the main connection is restored.')
        });
        return;
      }
      if (!result.ok) throw new Error(result.message || __('The posting endpoint did not return a successful result.'));
      finish_posting_success(state, result, print, attempt);
    }).catch(err => {
      if (attempt !== state.postingAttempt || !state.finalizing) return;
      if (!release_posting_attempt(state, attempt)) return;
      show_posting_error(state, err);
    });
  }

  function set_posting_state(state, active) {
    state.wrapper.find('[data-action="f10"],[data-action="f12"]').prop('disabled', !!active);
    role(state, 'payment-status').text(active ? 'Posting…' : (state.paymentConfirmed ? 'Preview confirmed' : 'Ready'));
  }

  function posting_error_text(err) {
    const values = [];
    const response = err && (err.responseJSON || (err.xhr && err.xhr.responseJSON));
    const add = value => {
      const text = String(value || '').replace(/Traceback[\s\S]*/i, '').trim();
      if (text && !values.includes(text)) values.push(text);
    };
    if (response && response._server_messages) {
      try {
        const outer = JSON.parse(response._server_messages);
        outer.forEach(raw => {
          try { const parsed = JSON.parse(raw); add(parsed.message || parsed); }
          catch (_) { add(raw); }
        });
      } catch (_) { add(response._server_messages); }
    }
    if (response) add(response.message || response.exception);
    if (err) add(err.message || err.statusText);
    return values[0] || '';
  }

  function show_posting_error(state, err) {
    const detail = posting_error_text(err);
    frappe.msgprint({
      title: MODE === 'cashier' ? __('Cashier Transaction Not Posted') : __('Encoder Transaction Not Posted'),
      indicator: 'red',
      message: `${detail ? `<div><b>Reason:</b> ${esc(detail)}</div>` : '<div>The server did not return a successful posting result.</div>'}<div style="margin-top:6px"><b>No screen data was cleared.</b> Correct the reason and press Finalize once.</div><div><b>Request ID:</b> <code>${esc(state.requestId)}</code></div>`
    });
  }

  function reconciliation_diagnostic_markup(diag) {
    if (!diag || diag.matched) return '';
    const reasons = (diag.primary_reasons || []).map(r => `<li><b>${esc(r.label || r.code || '')}</b>${r.detail ? `<div style="color:#555">${esc(r.detail)}</div>` : ''}</li>`).join('');
    const candidates = (diag.candidates || []).slice(0, 3).map(c => `<div style="margin-top:4px"><code>${esc(c.name || '')}</code>${c.reasons && c.reasons.length ? ` — ${c.reasons.map(esc).join(', ')}` : ' — exact candidate'}</div>`).join('');
    return `<div style="margin-top:10px;padding:9px 11px;border:1px solid #d0a33a;background:#fff8df">
      <div style="font-weight:bold;margin-bottom:4px">Reconciliation Diagnostics</div>
      ${reasons ? `<ul style="margin:4px 0 4px 18px;padding:0">${reasons}</ul>` : ''}
      ${candidates ? `<div style="margin-top:6px"><b>Closest candidate(s):</b>${candidates}</div>` : ''}
      <div style="margin-top:6px;color:#666">Diagnostic only — the approved automatic matching rules were not changed.</div>
    </div>`;
  }

  function show_posting_result(state, result, print) {
    let lines = '';
    if (result.local_continuity) {
      lines += `<div><b>Order:</b> ${esc(result.order_reference || '')}</div>`;
      lines += '<div><b>Status:</b> Saved for processing</div>';
      if (result.replayed) lines += '<div class="text-warning"><b>Safe retry:</b> the existing saved order was returned; no duplicate was created.</div>';
      frappe.msgprint({
        title: __('Encoder Order Saved'),
        indicator: 'green',
        message: `${lines}${print ? '<div>Print view opened in a new tab.</div>' : ''}`
      });
      return;
    }
    if (MODE === 'cashier') {
      lines += `<div><b>Cashier Sale:</b> <a href="/app/nkt-cashier-sale/${encodeURIComponent(result.cashier_sale)}">${esc(result.cashier_sale)}</a></div>`;
      if (result.payment_receipt) lines += `<div><b>Payment Receipt:</b> <a href="/app/nkt-payment-receipt/${encodeURIComponent(result.payment_receipt)}">${esc(result.payment_receipt)}</a></div>`;
      if (result.cashier_movements && result.cashier_movements.length) lines += `<div><b>Cashier Movements:</b> ${result.cashier_movements.map(esc).join(', ')}</div>`;
      if (result.integrity) lines += `<div><b>Payment Integrity:</b> ${result.integrity.passed ? 'PASS' : 'FAIL'} • ${Number(result.integrity.movement_count || 0)} movement(s)</div>`;
      lines += `<div><b>Match:</b> ${esc(result.reconciliation_status || result.status || '')}</div>`;
    } else {
      lines += `<div><b>Customer Order:</b> <a href="/app/nkt-customer-order/${encodeURIComponent(result.customer_order)}">${esc(result.customer_order)}</a></div>`;
      lines += `<div><b>Match:</b> ${esc(result.cashier_reconciliation_status || '')}</div>`;
      if (result.immediate_stock_entry) lines += `<div><b>Retail Stock Entry:</b> ${esc(result.immediate_stock_entry)}</div>`;
      if (result.reservation_entries && result.reservation_entries.length) lines += `<div><b>Reservations:</b> ${result.reservation_entries.map(esc).join(', ')}</div>`;
      if (result.warehouse_releases && result.warehouse_releases.length) lines += `<div><b>Warehouse Releases:</b> ${result.warehouse_releases.map(x => esc(x.name)).join(', ')}</div>`;
    }
    if (result.replayed) lines += '<div class="text-warning"><b>Safe retry:</b> the existing transaction was returned; no duplicate was created.</div>';
    lines += reconciliation_diagnostic_markup(result.reconciliation_diagnostics);
    lines += `<div><b>Request ID:</b> <code>${esc(result.request_id || state.requestId)}</code></div>`;
    frappe.msgprint({
      title: MODE === 'cashier' ? __('Cashier Transaction Posted') : __('Encoder Transaction Posted'),
      indicator: 'green',
      message: `${lines}${print ? '<div>Print view opened in a new tab.</div>' : ''}`
    });
  }

  function readonly_notice(state, action) {
    frappe.msgprint({
      title: __('Not connected in V2.0C.3'),
      indicator: 'blue',
      message: __(`${action} is not connected in this standard-payment stage. It creates no operational record.`)
    });
  }

  function open_local_order_print(printHtml) {
    if (!printHtml) return;
    const w = window.open('', '_blank');
    if (!w) {
      frappe.show_alert({ message: __('Print window was blocked. Allow pop-ups for this NKT site and use Finalize & Print again.'), indicator: 'orange' }, 5);
      return;
    }
    try { w.opener = null; } catch (_) {}
    w.document.open();
    w.document.write(String(printHtml));
    w.document.close();
    w.focus();
    setTimeout(() => { try { w.print(); } catch (_) {} }, 120);
  }

  function clear_transaction(state) {
    state.rows = []; state.payments = []; state.cashTendered = 0; state.cashTenderedManual = false; state.paymentConfirmed = false; state.customer = null;
    state.requestId = make_request_id();
    role(state, 'customer-entry').val(''); role(state, 'customer-selected').find('.nkt-customer-name').text('No customer selected'); role(state, 'customer-balance').text(format_money(0)); role(state, 'customer-status').text('Required');
    render_grid(state); render_payments(state); focus_item(state);
  }

  function make_request_id() {
    if (window.crypto && typeof window.crypto.randomUUID === 'function') return window.crypto.randomUUID();
    const bytes = new Uint8Array(16);
    if (window.crypto && typeof window.crypto.getRandomValues === 'function') window.crypto.getRandomValues(bytes);
    else for (let i = 0; i < bytes.length; i++) bytes[i] = Math.floor(Math.random() * 256);
    bytes[6] = (bytes[6] & 0x0f) | 0x40;
    bytes[8] = (bytes[8] & 0x3f) | 0x80;
    const h = Array.from(bytes, b => b.toString(16).padStart(2, '0')).join('');
    return `${h.slice(0, 8)}-${h.slice(8, 12)}-${h.slice(12, 16)}-${h.slice(16, 20)}-${h.slice(20)}`;
  }

  function round2(v) { return Math.round((Number(v || 0) + Number.EPSILON) * 100) / 100; }
  function esc(v) { return frappe.utils.escape_html(String(v ?? '')); }
  function esc_attr(v) { return esc(v).replace(/"/g, '&quot;'); }
  function format_money(v) { return new Intl.NumberFormat('en-PH', { style: 'currency', currency: 'PHP', minimumFractionDigits: 2 }).format(Number(v || 0)); }
  function format_plain_money(v) { return new Intl.NumberFormat('en-PH', { minimumFractionDigits: 2, maximumFractionDigits: 2 }).format(Number(v || 0)); }
  function format_qty(v) { return new Intl.NumberFormat('en-PH', { maximumFractionDigits: 3 }).format(Number(v || 0)); }
  function format_edit(v) { return Number(v || 0).toString(); }
})();
