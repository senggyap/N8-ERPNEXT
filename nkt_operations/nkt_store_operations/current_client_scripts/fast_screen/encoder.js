/* NKT CURRENT CLIENT SCRIPT — NKT Encoder Fast Screen — Form
 * Consolidated from the accepted live site on 2026-08-27.
 * Source order exactly preserves Frappe Client Script creation order.
 * Edit this file; do not create phase/recovery Client Script records.
 */

/* ===== SOURCE: NKT Encoder Fast Screen V2.0C.3 ===== */
(() => {
  const MODE = 'encoder';
  const DOCTYPE = 'NKT Encoder Fast Screen';
  const SCREEN_NAME = 'Encoder';
  const SCREEN_SUBTITLE = 'Classic Customer Order';
  const GRID_WAREHOUSE_LABEL = 'Official Source Warehouse';
  const GRID_STOCK_LABEL = 'Available Qty';
  const DOCUMENT_KEY = 'Customer Order';
  const NAMESPACE = 'nktFastTransactionKeyboard';
  const LEGACY_KEYBOARD_NAMESPACES = [
    'nktFastCashierV20C3CKeyboard',
    'nktFastEncoderV20C3CKeyboard'
  ];

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
    prepare_frappe_layout(frm, wrapper);
    wrapper.empty().html(shell_markup());
    install_shell_css();
    const state = make_state(frm, wrapper);
    window.__nktActiveFastScreenState = state;
    bind_shell(state);
    initialize_security(state).then(canContinue => {
      if (canContinue) load_bootstrap(state);
    });
  }

  function prepare_frappe_layout(frm, wrapper) {
    cleanup_fast_screen_layout();
    const page = wrapper.closest('.page-container').first();
    const formSection = wrapper.closest('.form-section');
    const control = wrapper.closest('.frappe-control');

    if (page.length) page.addClass('nkt-fast-page');
    if (formSection.length) formSection.addClass('nkt-fast-screen-section');
    if (control.length) control.addClass('nkt-fast-screen-control');
    $('body').addClass('nkt-fast-screen-focus');

    const apply = () => {
      if (!is_active_fast_doctype()) return;
      if (frm.page && typeof frm.page.clear_indicator === 'function') frm.page.clear_indicator();
      const scope = page.length ? page : wrapper.closest('.page-body');
      scope.find('.layout-side-section,.form-sidebar,.form-footer,.form-comments,.form-timeline,.timeline').each(function () {
        const el = $(this);
        if (!el.attr('data-nkt-fast-hidden')) el.attr('data-nkt-fast-hidden', '1');
      });
    };

    apply();
    setTimeout(apply, 0);
    setTimeout(apply, 120);
    setTimeout(apply, 450);

    window.__nktFastScreenLayoutCleanup = cleanup_fast_screen_layout;
    if (!window.__nktFastScreenRouteCleanupBound && frappe.router && typeof frappe.router.on === 'function') {
      window.__nktFastScreenRouteCleanupBound = true;
      frappe.router.on('change', () => {
        setTimeout(() => {
          if (!is_active_fast_doctype()) cleanup_fast_screen_layout();
        }, 0);
      });
    }
  }

  function is_active_fast_doctype() {
    const dt = window.cur_frm && window.cur_frm.doctype;
    return dt === 'NKT Cashier Fast Screen' || dt === 'NKT Encoder Fast Screen';
  }

  function cleanup_fast_screen_layout() {
    if (window.__nktFastTransactionCaptureHandler) {
      window.removeEventListener('keydown', window.__nktFastTransactionCaptureHandler, true);
      window.__nktFastTransactionCaptureHandler = null;
    }
    $('body').removeClass('nkt-fast-screen-focus');
    $('.nkt-fast-page').removeClass('nkt-fast-page');
    $('.nkt-fast-screen-section').removeClass('nkt-fast-screen-section');
    $('.nkt-fast-screen-control').removeClass('nkt-fast-screen-control');
    $('[data-nkt-fast-hidden="1"]').removeAttr('data-nkt-fast-hidden');
  }

  function make_state(frm, wrapper) {
    return {
      frm, wrapper, mode: MODE, boot: null, rows: [], payments: [], cashTendered: 0, cashTenderedManual: false, paymentConfirmed: false,
      customer: null, itemResults: [], itemIndex: 0, customerResults: [], customerIndex: 0,
      paymentDialog: null, paymentIntent: null, suppressPaymentCloseFocus: false, completionDialog: null,
      validationTarget: '', keyRearmTimer: null,
      requestId: make_request_id(), finalizing: false, postingAttempt: 0, postingWatchdog: null, lastPostCompletedAt: 0,
      securityMode: 'normal', securityBusy: false, terminalLocked: false, localRestrictionLatch: false,
      continuityTender: false, continuityTenderAttempted: false,
      priceAuthorization: null, plateNumber: '', orderSlipNumber: '', remarks: '',
      detailPersistencePromise: null, detailPersistenceKey: '', clearDialog: null
    };
  }

  function shell_markup() {
    const modeText = MODE === 'cashier'
      ? '<span>Shift: <b data-role="shift">Loading…</b></span>'
      : '<span>Mode: <b>Customer Order Entry</b></span>';
    const paymentHeading = MODE === 'cashier' ? 'Payment' : 'Payment Record';
    const paymentPrompt = MODE === 'cashier'
      ? 'Press F11 Payment, or press F12/F10 to continue directly to payment.'
      : 'Press F11 to record the payment already handled by the Cashier.';
    const completeLabel = MODE === 'cashier' ? 'Complete Sale' : 'Complete Order';
    const historyKeys = MODE === 'cashier'
      ? 'F2 Customer • F3 Item • F4 Customer History • Esc Item'
      : 'F2 Customer • F3 Item • F4 Customer History • F6 Item History • Esc Item';
    return `
      <div class="nkt-fast-shell" tabindex="0">
        <div class="nkt-shell-titlebar">
          <div><strong>NKT ${SCREEN_NAME}</strong><span class="nkt-shell-subtitle">${SCREEN_SUBTITLE}</span></div>
          <div class="nkt-ready-badge"><span></span><b data-role="screen-status">READY</b></div>
        </div>
        <div class="nkt-contextbar">
          <span>Operator: <b data-role="operator">Loading…</b></span>
          <span>Branch: <b data-role="branch">Loading…</b></span>
          <span>Default Warehouse: <b data-role="default-warehouse">Loading…</b></span>
          ${modeText}
        </div>
        <div class="nkt-warning" data-role="warning" hidden></div>
        <div class="nkt-validation" data-role="validation" hidden></div>
        <div class="nkt-item-entry-row">
          <label><u>F3</u> Item</label>
          <div class="nkt-combo-wrap">
            <input data-role="item-entry" class="nkt-primary-input" autocomplete="off" placeholder="Type part of the item name, item code, or barcode">
            <div data-role="item-results" class="nkt-results" hidden></div>
          </div>
          <button type="button" data-action="add-item">Add</button>
          <span class="nkt-entry-hint">Type part of the name • Enter selects highlighted item • ↓ Qty • → Rate</span>
        </div>
        <div class="nkt-grid-wrap">
          <table class="nkt-grid">
            <colgroup>
              <col class="c-no"><col class="c-item"><col class="c-desc"><col class="c-qty"><col class="c-uom"><col class="c-rate"><col class="c-wh"><col class="c-stock"><col class="c-amt"><col class="c-remove">
            </colgroup>
            <thead><tr>
              <th>#</th><th>Item</th><th>Description</th><th>Qty</th><th>UOM</th><th>Rate</th><th>${GRID_WAREHOUSE_LABEL}</th><th>${GRID_STOCK_LABEL}</th><th>Amount</th><th></th>
            </tr></thead>
            <tbody data-role="grid-body"><tr class="nkt-empty"><td colspan="10">Type part of an item name in F3 to begin.</td></tr></tbody>
          </table>
        </div>
        <div class="nkt-lower-panel">
          <div class="nkt-customer-panel">
            <div class="nkt-panel-heading"><span><u>F2</u> Customer</span><span class="nkt-panel-status" data-role="customer-status">Required before payment</span></div>
            <div class="nkt-combo-wrap">
              <input data-role="customer-entry" autocomplete="off" placeholder="Search actual customer — no generic Walk-in">
              <div data-role="customer-results" class="nkt-results nkt-customer-results" hidden></div>
            </div>
            <div data-role="customer-selected" class="nkt-customer-card">
              <div class="nkt-customer-name">No customer selected</div>
              <div class="nkt-customer-balance-line">Current Account Balance: <b data-role="customer-balance">₱0.00</b></div>
            </div>
            <button type="button" data-action="new-customer">New Customer</button>
            <button type="button" data-action="customer-history"><u>F4</u> Customer History</button>
          </div>
          <div class="nkt-payment-preview">
            <div class="nkt-panel-heading"><span>${paymentHeading}</span><span class="nkt-panel-status" data-role="payment-status">Not entered</span></div>
            <div data-role="payment-lines" class="nkt-payment-lines">${paymentPrompt}</div>
          </div>
          <div class="nkt-total-panel">
            <div><span>Total Quantity</span><b data-role="total-qty">0</b></div>
            <div class="nkt-grand-total"><span>Grand Total</span><b data-role="grand-total">₱0.00</b></div>
          </div>
        </div>
        <div class="nkt-details-strip nkt-details-strip-encoder">
          <div class="nkt-details-title"><b>Order Details</b><span>Optional paper and release references</span></div>
          <label class="nkt-detail-field"><span>Plate Number <small>Optional</small></span><input data-role="plate-number" maxlength="40" autocomplete="off" placeholder="Customer / pickup vehicle plate"></label>
          <label class="nkt-detail-field"><span>OS# <small>Physical paper slip • Optional</small></span><input data-role="order-slip-number" maxlength="80" autocomplete="off" placeholder="Paper Order Slip number"></label>
          <label class="nkt-detail-field nkt-detail-remarks"><span>Remarks <small>Optional</small></span><input data-role="remarks" maxlength="500" autocomplete="off" placeholder="Operational note for this order"></label>
        </div>
        <div class="nkt-actionbar">
          <button type="button" data-action="clear">Clear</button>
          <span class="nkt-shortcut-note">${historyKeys}</span>
          <div class="nkt-action-spacer"></div>
          <button type="button" data-action="f10"><b>F10</b> Complete + Print</button>
          <button type="button" data-action="f11"><b>F11</b> Payment</button>
          <button type="button" data-action="f12" class="primary"><b>F12</b> ${completeLabel}</button>
        </div>
      </div>`;
  }

  function install_shell_css() {
    ['nkt-fast-shell-style-v20b2', 'nkt-fast-shell-style-ui3'].forEach(id => {
      const old = document.getElementById(id);
      if (old) old.remove();
    });
    const style = document.createElement('style');
    style.id = 'nkt-fast-shell-style-ui3';
    style.textContent = `
      body.nkt-fast-screen-focus .desk-sidebar{display:none!important}
      body.nkt-fast-screen-focus .layout-main{margin-left:0!important;width:100%!important;max-width:none!important}
      .nkt-fast-page .layout-side-section,.nkt-fast-page .form-sidebar,.nkt-fast-page .form-footer,.nkt-fast-page .form-comments,.nkt-fast-page .form-timeline,.nkt-fast-page .timeline,[data-nkt-fast-hidden="1"]{display:none!important}
      .nkt-fast-page .container,.nkt-fast-page .page-body,.nkt-fast-page .page-wrapper,.nkt-fast-page .layout-main-section-wrapper,.nkt-fast-page .layout-main-section,.nkt-fast-page .form-layout{width:100%!important;max-width:none!important;flex:1 1 100%!important}
      .nkt-fast-page .page-head .indicator-pill,.nkt-fast-page .page-head .indicator{display:none!important}
      .nkt-fast-screen-section,.nkt-fast-screen-section .section-body,.nkt-fast-screen-control{margin:0!important;padding:0!important;border:0!important;max-width:none!important}
      .nkt-fast-shell{font-family:Tahoma,Arial,sans-serif;font-size:13px;color:#111;background:#d5d5d5;border:1px solid #6f6f6f;min-height:calc(100vh - 105px);width:100%;display:flex;flex-direction:column;box-shadow:inset 0 0 0 1px #fff}
      .nkt-shell-titlebar{display:flex;justify-content:space-between;align-items:center;padding:7px 10px;background:linear-gradient(#fafafa,#c7c7c7);border-bottom:1px solid #777;font-size:18px}.nkt-shell-subtitle{font-size:13px;font-weight:normal;margin-left:12px}.nkt-ready-badge{display:flex;align-items:center;gap:6px;font-size:11px;padding:4px 9px;background:#edf5e6;border:1px solid #68844e}.nkt-ready-badge>span{width:8px;height:8px;border-radius:50%;background:#2d8a35}
      .nkt-contextbar{display:flex;gap:28px;padding:5px 10px;background:#ececec;border-bottom:1px solid #999;white-space:nowrap;overflow:hidden}.nkt-warning{margin:5px 8px 0;padding:5px 8px;background:#fff0ee;border:1px solid #c25b52;font-size:12px}.nkt-validation{margin:5px 8px 0;padding:7px 9px;background:#fff4cf;border:1px solid #b58a16;color:#5f4300;font-weight:bold;font-size:12px}
      .nkt-item-entry-row{display:grid;grid-template-columns:92px minmax(430px,1.3fr) 68px minmax(360px,.8fr);gap:7px;align-items:center;padding:7px 9px;border-bottom:1px solid #999;background:#dedede}.nkt-item-entry-row label{font-weight:bold}.nkt-entry-hint{color:#4a4a4a;white-space:nowrap}
      .nkt-fast-shell input,.nkt-fast-shell select{height:28px;border:1px solid #666;background:#fff;padding:3px 6px;border-radius:0}.nkt-primary-input{font-size:15px;font-weight:bold;width:100%;box-shadow:inset 1px 1px 2px #aaa}.nkt-fast-shell button{min-height:28px;border:1px solid #666;background:linear-gradient(#fff,#d0d0d0);border-radius:1px;padding:4px 10px;color:#111}.nkt-fast-shell button:active{background:#c2c2c2}.nkt-fast-shell button.primary{background:linear-gradient(#edf6ff,#bddbfc);border-color:#3d6f9f;font-weight:bold}
      .nkt-grid-wrap{flex:1;min-height:350px;background:#fff;overflow:auto;border-bottom:1px solid #777}.nkt-grid{width:100%;border-collapse:collapse;table-layout:fixed}.nkt-grid .c-no{width:3%}.nkt-grid .c-item{width:10%}.nkt-grid .c-desc{width:20%}.nkt-grid .c-qty{width:6%}.nkt-grid .c-uom{width:7%}.nkt-grid .c-rate{width:11%}.nkt-grid .c-wh{width:17%}.nkt-grid .c-stock{width:9%}.nkt-grid .c-amt{width:12%}.nkt-grid .c-remove{width:5%}
      .nkt-grid th{position:sticky;top:0;z-index:1;background:linear-gradient(#f5f5f5,#cecece);border-right:1px solid #999;border-bottom:1px solid #777;padding:6px 7px;text-align:left;line-height:1.15}.nkt-grid td{border-right:1px solid #bbb;border-bottom:1px solid #ccc;padding:4px 6px;vertical-align:middle;line-height:1.2}.nkt-grid input,.nkt-grid select{width:100%;height:27px;border:1px solid transparent}.nkt-grid input:focus,.nkt-grid select:focus{border-color:#1b5790;outline:1px solid #1b5790}.nkt-grid .nkt-special-rate{background:#ffe9e6;border-color:#b54237}.nkt-grid .nkt-adjusted-rate{background:#fff4bf}.nkt-empty td{text-align:center;color:#666;padding:42px}
      .nkt-rate-badge{display:inline-block;margin-top:2px;padding:1px 5px;border:1px solid #999;background:#eee;font-size:10px;white-space:nowrap}.nkt-rate-badge.adjusted{background:#fff2b6;border-color:#b28a00}.nkt-rate-badge.special{background:#ffe0dc;border-color:#b54237;color:#7b160f}.nkt-remove{padding:1px 7px!important;min-height:24px!important}
      .nkt-combo-wrap{position:relative}.nkt-results{position:absolute;left:0;right:0;top:100%;z-index:50;background:#fff;border:1px solid #555;max-height:260px;overflow:auto;box-shadow:2px 3px 7px rgba(0,0,0,.25)}.nkt-result{padding:6px 8px;border-bottom:1px solid #ddd;cursor:pointer}.nkt-result.active,.nkt-result:hover{background:#cfe7ff}.nkt-result small{display:flex;justify-content:space-between;color:#555;margin-top:2px}
      .nkt-details-strip{display:grid;align-items:end;gap:9px;padding:7px 9px;background:#d9d9d9;border-top:1px solid #fff;border-bottom:1px solid #777}.nkt-details-strip-encoder{grid-template-columns:155px minmax(180px,.55fr) minmax(210px,.65fr) minmax(420px,1.8fr)}.nkt-details-strip-cashier{grid-template-columns:155px minmax(520px,1fr)}.nkt-details-title{display:flex;flex-direction:column;align-self:center;line-height:1.25}.nkt-details-title>b{font-size:13px}.nkt-details-title>span{font-size:10px;color:#555}.nkt-detail-field{display:flex;flex-direction:column;gap:3px;margin:0;font-weight:bold}.nkt-detail-field span{display:flex;justify-content:space-between;gap:7px}.nkt-detail-field small{font-size:9px;font-weight:normal;color:#555}.nkt-detail-field input{width:100%;font-weight:normal}.nkt-clear-guard-box{font-family:Tahoma,Arial,sans-serif;border:2px solid #8a6b14;background:#fff6cf;padding:12px;line-height:1.5}.nkt-clear-guard-box>div{margin-top:7px}.nkt-clear-guard-dialog .modal-header .close{outline:none!important}
      .nkt-lower-panel{display:grid;grid-template-columns:minmax(330px,1.05fr) minmax(430px,1.25fr) minmax(270px,.72fr);gap:8px;padding:8px;background:#e2e2e2}.nkt-customer-panel,.nkt-payment-preview,.nkt-total-panel{min-width:0;background:#f7f7f7;border:1px solid #888;padding:8px}.nkt-panel-heading{display:flex;justify-content:space-between;font-weight:bold;border-bottom:1px solid #aaa;margin:-2px -2px 7px;padding:0 2px 5px}.nkt-panel-status{font-size:10px;font-weight:normal;padding:1px 5px;background:#eee;border:1px solid #aaa}.nkt-customer-panel input{width:100%}.nkt-customer-card{min-height:50px;padding:7px 2px}.nkt-customer-name{font-weight:bold;margin-bottom:5px}.nkt-payment-lines{min-height:68px}.nkt-payment-line{display:grid;grid-template-columns:1fr auto;gap:10px;border-bottom:1px dotted #aaa;padding:3px 0}.nkt-payment-line small{grid-column:1/-1;color:#555}.nkt-total-panel>div{display:flex;justify-content:space-between;padding:6px}.nkt-grand-total{font-size:22px;border-top:2px solid #333;margin-top:6px}.nkt-grand-total b{white-space:nowrap}.nkt-actionbar{display:flex;gap:7px;align-items:center;padding:8px;background:linear-gradient(#efefef,#c6c6c6);border-top:1px solid #fff}.nkt-action-spacer{flex:1}.nkt-shortcut-note{color:#555;font-size:11px}.nkt-stock-good{color:#08711c}.nkt-stock-bad{color:#b51e12;font-weight:bold}
      .nkt-payment-dialog{max-width:1220px!important;width:94vw!important}.nkt-payment-grid-shell{font-family:Tahoma,Arial,sans-serif;font-size:12px}.nkt-payment-summary{display:grid;grid-template-columns:repeat(4,1fr);gap:8px;margin-bottom:8px}.nkt-payment-summary.encoder{grid-template-columns:repeat(3,1fr)}.nkt-summary-box{border:1px solid #888;background:#f5f5f5;padding:8px}.nkt-summary-box b{display:block;font-size:19px;margin-top:3px}.nkt-summary-box.remaining.ok b{color:#0a6a1d}.nkt-summary-box.remaining.bad b{color:#b51e12}.nkt-summary-box.change.has-change{background:#fff4bd;border-color:#b28a00}.nkt-summary-box.change.has-change b{color:#0a5f92;font-size:25px}.nkt-pay-table{width:100%;border-collapse:collapse;table-layout:fixed}.nkt-pay-table th,.nkt-pay-table td{border:1px solid #aaa;padding:4px}.nkt-pay-table th{background:linear-gradient(#f5f5f5,#d2d2d2);text-align:left}.nkt-pay-table input,.nkt-pay-table select{width:100%;height:30px;border:1px solid #777;padding:3px}.nkt-pay-table input:focus,.nkt-pay-table select:focus{outline:2px solid #2a6fa8;outline-offset:-1px}.nkt-pay-table input:disabled,.nkt-pay-table input[readonly]{background:#f2f2f2;color:#666}.nkt-pay-table .p-method{width:17%}.nkt-pay-table .p-amount{width:17%}.nkt-pay-table .p-ref{width:25%}.nkt-pay-table .p-date{width:14%}.nkt-pay-table .p-provider{width:22%}.nkt-pay-table .p-remove{width:5%}.nkt-pay-actions{display:flex;gap:7px;margin-top:9px}.nkt-pay-actions .spacer{flex:1}.nkt-payment-inline{display:none;margin:8px 0 0;padding:7px 9px;border:1px solid #b54a3d;background:#fff0ed;color:#831a10;font-weight:bold}.nkt-payment-inline.show{display:block}.nkt-payment-note{margin-top:8px;padding:6px;border:1px solid #a98b2a;background:#fff7cf;line-height:1.45}.nkt-confirm-box{font-family:Tahoma,Arial,sans-serif;border:2px solid #555;background:#f6f6f6;padding:14px}.nkt-confirm-title{font-size:18px;font-weight:bold;margin-bottom:10px}.nkt-confirm-row{display:flex;justify-content:space-between;padding:4px 0;border-bottom:1px dotted #aaa}.nkt-confirm-change{font-size:44px;font-weight:bold;padding:16px 8px;text-align:center;background:#fff4bd;border:2px solid #b28a00;margin:12px 0}.nkt-confirm-hint{text-align:center;font-weight:bold;margin-top:10px}.nkt-payment-confirmation-dialog .modal-footer .btn-primary:focus{outline:3px solid #2a6fa8!important;outline-offset:2px!important}
      .nkt-completion-dialog{max-width:700px!important}.nkt-completion-dialog .modal-header .close{display:none!important}.nkt-completion-box{font-family:Tahoma,Arial,sans-serif;border:3px solid #4c4c4c;background:#f5f5f5;padding:18px;text-align:center}.nkt-completion-title{font-size:25px;font-weight:bold;letter-spacing:.04em}.nkt-completion-reference{margin:7px 0 14px;color:#444}.nkt-completion-change-label{font-size:18px;font-weight:bold}.nkt-completion-change{font-size:64px;line-height:1.05;font-weight:bold;background:#fff1ae;border:3px solid #a77c00;padding:20px 10px;margin:8px 0 14px}.nkt-completion-hint{font-size:16px;font-weight:bold}
      .nkt-fast-shell[data-nkt-ui-mode="limited"] [data-action="customer-history"]{display:none!important}
      .nkt-fast-shell[data-nkt-ui-mode="limited"] [data-role="customer-balance"]{display:none!important}
      .nkt-fast-shell[data-nkt-ui-mode="limited"] [data-role="customer-balance"] + *{display:none!important}
      .nkt-fast-shell[data-nkt-ui-mode="limited"] .nkt-customer-balance-line{display:none!important}
      .nkt-device-unavailable{display:flex;min-height:340px;align-items:center;justify-content:center;background:#f3f3f3;border:1px solid #777;font-family:Tahoma,Arial,sans-serif;font-size:17px;font-weight:bold}
      @media(max-width:1250px){.nkt-details-strip-encoder,.nkt-details-strip-cashier{grid-template-columns:1fr 1fr}.nkt-details-title{grid-column:1/-1}.nkt-detail-remarks{grid-column:1/-1}.nkt-lower-panel{grid-template-columns:1fr 1fr}.nkt-total-panel{grid-column:1/-1}.nkt-item-entry-row{grid-template-columns:95px 1fr 60px}.nkt-entry-hint{grid-column:1/-1}.nkt-contextbar{overflow:auto}.nkt-shortcut-note{display:none}}
    `;
    document.head.appendChild(style);
  }

  function is_active_fast_screen(state) {
    return !!(
      window.__nktActiveFastScreenState === state &&
      state.wrapper &&
      state.wrapper[0] &&
      state.wrapper[0].isConnected &&
      state.wrapper.is(':visible') &&
      state.wrapper.find('.nkt-fast-shell').length
    );
  }

  function fast_function_command_from_event(e) {
    const key = String((e && e.key) || '').toUpperCase();
    const code = String((e && e.code) || '').toUpperCase();
    const keyCode = Number((e && (e.which || e.keyCode)) || 0);
    const ctrl = Boolean(e && e.ctrlKey);
    const shift = Boolean(e && e.shiftKey);
    const alt = Boolean(e && e.altKey);
    const isFunctionKey = n => key === `F${n}` || code === `F${n}` || keyCode === 111 + n;

    if (isFunctionKey(12) && ctrl && alt && shift) return 'self_restrict';
    if (isFunctionKey(1)) return 'suppress_f1';
    if (isFunctionKey(2)) return 'customer';
    if (isFunctionKey(3)) return 'item';
    if (isFunctionKey(4)) return 'customer_history';
    if (isFunctionKey(10)) return 'complete_print';
    if (isFunctionKey(11)) return 'payment';
    if (isFunctionKey(12)) return 'complete';
    if (key === 'ESCAPE' || key === 'ESC' || keyCode === 27) return 'item';
    return '';
  }

  function consume_fast_function_key(e) {
    if (!e) return;
    if (typeof e.preventDefault === 'function') e.preventDefault();
    if (typeof e.stopPropagation === 'function') e.stopPropagation();
    if (typeof e.stopImmediatePropagation === 'function') e.stopImmediatePropagation();
    e.__nktFastHandled = true;
  }

  function run_fast_function_command(state, command) {
    if (!state || state.terminalLocked) return false;
    if (command === 'suppress_f1') return true;
    if (command === 'self_restrict') { self_restrict_now(state); return true; }
    if (command === 'customer') { focus_customer(state); return true; }
    if (command === 'item') { focus_item(state); return true; }
    if (command === 'customer_history') {
      if (state.securityMode !== 'limited' && !state.terminalLocked) open_customer_history(state);
      return true;
    }
    if (command === 'complete_print') { finalize_live(state, true); return true; }
    if (command === 'payment') { open_payment_preview(state, { post: false, print: false, source: 'f11' }); return true; }
    if (command === 'complete') { finalize_live(state, false); return true; }
    return false;
  }

  function install_fast_function_keyboard(state) {
    LEGACY_KEYBOARD_NAMESPACES.forEach(ns => $(document).off(`keydown.${ns}`));
    $(document).off(`keydown.${NAMESPACE}`);

    if (window.__nktFastTransactionCaptureHandler) {
      window.removeEventListener('keydown', window.__nktFastTransactionCaptureHandler, true);
    }

    const handler = e => {
      if (!is_active_fast_screen(state)) return;
      const command = fast_function_command_from_event(e);
      if (!command) return;

      // Preserve the existing Owner/Admin emergency restriction chord.
      if (command === 'self_restrict') {
        consume_fast_function_key(e);
        run_fast_function_command(state, command);
        return;
      }

      // Payment, confirmation, and completion dialogs own their own keyboard
      // flow. Do not let a Fast Screen function key act through a visible modal.
      if ($('.modal.show:visible').length) return;

      // A held F10/F11/F12 key must never open multiple dialogs or post twice.
      if (e.repeat && (command === 'complete_print' || command === 'payment' || command === 'complete')) {
        consume_fast_function_key(e);
        return;
      }

      consume_fast_function_key(e);
      run_fast_function_command(state, command);
    };

    window.__nktFastTransactionCaptureHandler = handler;
    window.addEventListener('keydown', handler, true);
  }

  function bind_shell(state) {
    const w = state.wrapper;

    // The HTML field may survive Frappe refreshes. Remove older delegated
    // handlers before binding the current visible Fast Screen.
    [
      ['click', '[data-action="add-item"]'],
      ['click', '[data-action="clear"]'],
      ['click', '[data-action="new-customer"]'],
      ['click', '[data-action="customer-history"]'],
      ['click', '[data-action="f10"]'],
      ['click', '[data-action="f11"]'],
      ['click', '[data-action="f12"]'],
      ['click', '.nkt-remove'],
      ['click', '[data-item-index]'],
      ['click', '[data-customer-index]']
    ].forEach(([eventName, selector]) => w.off(eventName, selector));

    w.on('click.nktFastShellUI3B', '[data-action="add-item"]', e => { e.preventDefault(); item_enter(state); });
    w.on('click.nktFastShellUI4', '[data-action="clear"]', e => { e.preventDefault(); request_clear_transaction(state); });
    w.on('click.nktFastShellUI3B', '[data-action="new-customer"]', e => { e.preventDefault(); open_fast_new_customer(state); });
    w.on('click.nktFastShellUI3B', '[data-action="customer-history"]', e => {
      e.preventDefault();
      run_fast_function_command(state, 'customer_history');
    });
    w.on('click.nktFastShellUI3B', '[data-action="f10"]', e => { e.preventDefault(); e.stopPropagation(); run_fast_function_command(state, 'complete_print'); });
    w.on('click.nktFastShellUI3B', '[data-action="f11"]', e => { e.preventDefault(); e.stopPropagation(); run_fast_function_command(state, 'payment'); });
    w.on('click.nktFastShellUI3B', '[data-action="f12"]', e => { e.preventDefault(); e.stopPropagation(); run_fast_function_command(state, 'complete'); });
    w.on('click.nktFastShellUI3B', '.nkt-remove', function () { state.rows.splice(Number($(this).data('row')), 1); invalidate_settlement(state); render_grid(state); render_payments(state); });
    w.on('click.nktFastShellUI3B', '[data-item-index]', function () { choose_item(state, Number($(this).data('item-index'))); });
    w.on('click.nktFastShellUI3B', '[data-customer-index]', function () { choose_customer(state, Number($(this).data('customer-index'))); });
    w.find('[data-action="f10"],[data-action="f11"],[data-action="f12"]').prop('disabled', false).attr('aria-disabled', 'false');

    const item = role(state, 'item-entry');
    let itemTimer = null;
    item.on('input', () => { clearTimeout(itemTimer); itemTimer = setTimeout(() => search_items(state), 150); });
    item.on('keydown', e => item_keydown(state, e));

    const customer = role(state, 'customer-entry');
    let customerTimer = null;
    customer.on('input', () => { clearTimeout(customerTimer); customerTimer = setTimeout(() => search_customers(state), 170); });
    customer.on('keydown', e => customer_keydown(state, e));

    [
      ['plate-number', 'plateNumber', 40],
      ['order-slip-number', 'orderSlipNumber', 80],
      ['remarks', 'remarks', 500]
    ].forEach(([roleName, key, maxLength]) => {
      const input = role(state, roleName);
      if (!input.length) return;
      input.off('.nktTransactionDetailsUI4').on('input.nktTransactionDetailsUI4 change.nktTransactionDetailsUI4', () => {
        set_transaction_detail(state, key, input.val(), maxLength);
      });
    });

    install_fast_function_keyboard(state);
  }

function detail_value(value, maxLength) {
  return String(value == null ? '' : value).replace(/[\r\n\t]+/g, ' ').slice(0, maxLength);
}

function set_transaction_detail(state, key, value, maxLength) {
  const next = detail_value(value, maxLength);
  if (state[key] === next) return;
  state[key] = next;
  state.detailPersistencePromise = null;
  state.detailPersistenceKey = '';
  if (state.paymentConfirmed) invalidate_settlement(state);
}

function transaction_detail_values(state) {
  if (MODE === 'cashier') {
    return { notes: String(state.remarks || '').trim() };
  }
  return {
    custom_nkt_plate_number: String(state.plateNumber || '').trim(),
    source_order_slip: String(state.orderSlipNumber || '').trim(),
    notes: String(state.remarks || '').trim()
  };
}

function has_meaningful_transaction_details(state) {
  return Object.values(transaction_detail_values(state)).some(value => String(value || '').trim());
}

function has_unfinished_transaction(state) {
  return Boolean(
    state.customer ||
    state.rows.length ||
    state.payments.length ||
    state.paymentConfirmed ||
    Number(state.cashTendered || 0) ||
    has_meaningful_transaction_details(state)
  );
}

function request_clear_transaction(state) {
  if (!has_unfinished_transaction(state)) {
    start_new_transaction(state);
    return;
  }
  if (state.clearDialog && state.clearDialog.$wrapper && state.clearDialog.$wrapper.is(':visible')) {
    nkt_focus_control(nkt_dialog_primary_button(state.clearDialog));
    return;
  }

  const d = new frappe.ui.Dialog({
    title: __('Clear this unfinished transaction?'),
    fields: [{
      fieldtype: 'HTML',
      fieldname: 'warning_html',
      options: `
        <div class="nkt-clear-guard-box">
          <b>The Customer, items, payment, warehouse choices, or transaction details entered here will be discarded.</b>
          <div>Choose <b>Keep Transaction</b> to continue working, or deliberately choose <b>Clear Transaction</b> to discard it.</div>
        </div>`
    }]
  });
  state.clearDialog = d;
  d.set_primary_action(__('Keep Transaction'), () => d.hide());
  d.set_secondary_action(__('Clear Transaction'), () => {
    d.hide();
    state.clearDialog = null;
    start_new_transaction(state);
  });
  d.$wrapper.addClass('nkt-clear-guard-dialog');
  nkt_dialog_header_close_buttons(d).attr('tabindex', '-1').attr('aria-label', __('Keep Transaction'));
  d.$wrapper.off('hidden.bs.modal.nktClearGuard').on('hidden.bs.modal.nktClearGuard', () => {
    if (state.clearDialog === d) state.clearDialog = null;
    rearm_fast_function_keyboard(state, 1);
  });
  d.show();
  setTimeout(() => nkt_focus_control(nkt_dialog_primary_button(d)), 25);
}

function transaction_detail_document(result) {
  if (MODE === 'cashier') return { doctype: 'NKT Cashier Sale', name: String((result && result.cashier_sale) || '') };
  return { doctype: 'NKT Customer Order', name: String((result && result.customer_order) || '') };
}

function persist_transaction_details(state, result) {
  const values = transaction_detail_values(state);
  const meaningful = Object.values(values).some(value => String(value || '').trim());
  if (!meaningful) return Promise.resolve({ skipped: true });

  if (result && result.local_continuity) {
    return Promise.reject(new Error(__('The transaction was saved for offline continuity, but Plate Number, OS#, and Remarks cannot yet be attached to the canonical document until primary synchronization. Keep this screen and ask an Administrator to complete the detail recovery.')));
  }

  const target = transaction_detail_document(result);
  if (!target.name) {
    return Promise.reject(new Error(__('The posted transaction reference required to save Plate Number, OS#, or Remarks was not returned.')));
  }

  const persistenceKey = `${state.requestId}|${target.doctype}|${target.name}|${JSON.stringify(values)}`;
  if (state.detailPersistencePromise && state.detailPersistenceKey === persistenceKey) {
    return state.detailPersistencePromise;
  }

  const request = frappe.db && typeof frappe.db.set_value === 'function'
    ? frappe.db.set_value(target.doctype, target.name, values)
    : frappe.call({
        method: 'frappe.client.set_value',
        args: { doctype: target.doctype, name: target.name, fieldname: values },
        freeze: false
      });

  state.detailPersistenceKey = persistenceKey;
  state.detailPersistencePromise = Promise.resolve(request).catch(error => {
    state.detailPersistencePromise = null;
    state.detailPersistenceKey = '';
    throw error;
  });
  return state.detailPersistencePromise;
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
    state.plateNumber = '';
    state.orderSlipNumber = '';
    state.remarks = '';
    state.detailPersistencePromise = null;
    state.detailPersistenceKey = '';
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
    if (window.__nktFastTransactionCaptureHandler) {
      window.removeEventListener('keydown', window.__nktFastTransactionCaptureHandler, true);
      window.__nktFastTransactionCaptureHandler = null;
    }
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
    frappe.call({ method: 'nkt_operations.nkt_store_operations.fast_screen_backend.get_fast_ui_bootstrap', args: { mode: state.mode }, freeze: true }).then(r => {
      state.boot = r.message;
      role(state, 'operator').text(`${state.boot.full_name} (${state.boot.user})`);
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
      } else if (state.boot.setup_error) show_warning(state, state.boot.setup_error);
      focus_item(state);
    }).catch(err => {
      show_warning(state, `Fast-screen setup did not load. ${posting_error_text(err) || 'Refresh the page and try again.'}`);
    });
  }

  function role(state, name) { return state.wrapper.find(`[data-role="${name}"]`); }
  function warehouse_label(state, name) { const row = state.boot?.warehouses?.find(x => x.name === name); return row ? row.label : name; }
  function show_warning(state, text) { role(state, 'warning').text(text).prop('hidden', false); }

  function clear_fast_validation(state, target = '') {
    if (!state) return;
    if (target && state.validationTarget && state.validationTarget !== target) return;
    state.validationTarget = '';
    const box = role(state, 'validation');
    if (box.length) box.text('').attr('data-target', '').prop('hidden', true);
  }

  function rearm_fast_function_keyboard(state, attempt = 0) {
    if (!state) return;
    if (state.keyRearmTimer) clearTimeout(state.keyRearmTimer);
    state.keyRearmTimer = setTimeout(() => {
      state.keyRearmTimer = null;
      if (state.terminalLocked || state.finalizing || state.paymentDialog || state.completionDialog) return;
      if (!state.wrapper || !state.wrapper[0] || !state.wrapper[0].isConnected || !state.wrapper.is(':visible')) return;
      if ($('.modal.show:visible').length) {
        if (attempt < 15) rearm_fast_function_keyboard(state, attempt + 1);
        return;
      }
      window.__nktActiveFastScreenState = state;
      install_fast_function_keyboard(state);
      state.wrapper.find('[data-action="f10"],[data-action="f11"],[data-action="f12"]').prop('disabled', false).attr('aria-disabled', 'false');
    }, attempt ? 60 : 0);
  }

  function show_fast_validation(state, target, message) {
    state.validationTarget = target || '';
    const box = role(state, 'validation');
    if (box.length) box.attr('data-target', state.validationTarget).text(message).prop('hidden', false);
    frappe.show_alert({ message: __(message), indicator: 'orange' }, 4);
    if (target === 'customer') focus_customer(state);
    else focus_item(state);
    rearm_fast_function_keyboard(state);
  }

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
    frappe.call({ method: 'nkt_operations.nkt_store_operations.fast_screen_backend.search_items', args: { search_text: text, warehouse: state.boot.default_warehouse, limit: 12 } }).then(r => {
      state.itemResults = r.message || [];
      state.itemIndex = 0;
      const exact = state.itemResults.findIndex(x => x.item_code.toLowerCase() === text.toLowerCase() || (x.item_name || '').toLowerCase() === text.toLowerCase());
      if (exact >= 0) { choose_item(state, exact); return; }
      if (chooseFirst && state.itemResults.length === 1) { choose_item(state, 0); return; }
      render_item_results(state);
    });
  }

  function render_item_results(state) {
    const box = role(state, 'item-results');
    if (!state.itemResults.length) { box.html('<div class="nkt-result">No saleable item matched that name, code, or barcode.</div>').prop('hidden', false); return; }
    box.html(state.itemResults.map((x, i) => `<div class="nkt-result ${i === state.itemIndex ? 'active' : ''}" data-item-index="${i}"><b>${esc(x.item_name || x.item_code)}</b><small><span>Code ${esc(x.item_code)} • ${format_money(x.standard_rate)}</span><span>Available ${format_qty(x.available_qty)}</span></small></div>`).join('')).prop('hidden', false);
  }

  function choose_item(state, index) {
    const x = state.itemResults[index];
    if (!x) return;
    state.rows.push({ item_code: x.item_code, item_name: x.item_name, qty: 1, uom: x.stock_uom, standard_rate: Number(x.standard_rate || 0), rate: Number(x.standard_rate || 0), warehouse: state.boot.default_warehouse, available: Number(x.available_qty || 0) });
    invalidate_settlement(state);
    role(state, 'item-entry').val('');
    role(state, 'item-results').prop('hidden', true);
    render_grid(state);
    clear_fast_validation(state, 'item');
    rearm_fast_function_keyboard(state);
    focus_item(state);
  }

  function render_grid(state) {
    const body = role(state, 'grid-body');
    if (!state.rows.length) { body.html('<tr class="nkt-empty"><td colspan="10">Type part of an item name in F3 to begin.</td></tr>'); update_totals(state); return; }
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
    frappe.call({ method: 'nkt_operations.nkt_store_operations.fast_screen_backend.get_item_context', args: { item_code: r.item_code, warehouse: r.warehouse } }).then(x => { r.available = Number(x.message.available_qty || 0); update_row_display(state, i); });
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
    frappe.call({ method: 'nkt_operations.nkt_store_operations.fast_screen_backend.search_customers', args: { search_text: text, limit: 12 } }).then(r => { state.customerResults = r.message || []; state.customerIndex = 0; render_customer_results(state); });
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
    if (!state.customerResults.length) { box.html('<div class="nkt-result">No customer found. Click New Customer to create one.</div>').prop('hidden', false); return; }
    box.html(state.customerResults.map((x, i) => {
      const balance = state.securityMode === 'limited' ? '' : `<span>Balance ${format_money(x.current_account_balance)}</span>`;
      return `<div class="nkt-result ${i === state.customerIndex ? 'active' : ''}" data-customer-index="${i}"><b>${esc(x.customer_name || x.name)}</b><small><span>${esc(x.name)}</span>${balance}</small></div>`;
    }).join('')).prop('hidden', false);
  }
  function choose_customer(state, i) {
    const x = state.customerResults[i]; if (!x) return;
    const changed = state.customer && state.customer.name !== x.name;
    state.customer = x;
    if (changed) {
      invalidate_settlement(state);
      if (state.payments.length) rebalance_cash(state);
    }
    role(state, 'customer-entry').val(x.customer_name || x.name);
    role(state, 'customer-selected').find('.nkt-customer-name').text(x.customer_name || x.name);
    role(state, 'customer-balance').text(format_money(x.current_account_balance));
    role(state, 'customer-status').text('Selected');
    role(state, 'customer-results').prop('hidden', true);
    render_payments(state);
    clear_fast_validation(state, 'customer');
    rearm_fast_function_keyboard(state);
    focus_item(state);
  }

  function open_fast_new_customer(state) {
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
            invalidate_settlement(state);
            if (state.payments.length) rebalance_cash(state);
          }

          state.customerResults = [];
          state.customerIndex = 0;
          role(state, 'customer-entry').val(state.customer.customer_name);
          role(state, 'customer-selected').find('.nkt-customer-name').text(state.customer.customer_name);
          role(state, 'customer-balance').text(format_money(state.customer.current_account_balance));
          role(state, 'customer-status').text('Selected');
          role(state, 'customer-results').prop('hidden', true);
          render_payments(state);
          clear_fast_validation(state, 'customer');
          d.hide();

          frappe.show_alert({
            message: x.created ? __('Customer created and selected.') : __('That Customer already exists and was selected.'),
            indicator: x.created ? 'green' : 'blue'
          });
          rearm_fast_function_keyboard(state, 1);
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
    if (state.payments.length) {
      rebalance_cash(state);
      if (Math.abs(payment_totals(state).paymentTotal - total(state)) > 0.005) state.paymentConfirmed = false;
      render_payments(state);
    } else {
      render_payment_status(state);
    }
  }
  function total(state) { return round2(state.rows.reduce((a, r) => a + Number(r.qty || 0) * Number(r.rate || 0), 0)); }
  function invalidate_settlement(state) {
    state.paymentConfirmed = false;
    state.priceAuthorization = null;
    render_payment_status(state);
  }

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

  function open_payment_preview(state, intent = null) {
    if (state.completedTransaction) {
      frappe.show_alert({ message: __('This transaction is already complete. Acknowledge the completion window before starting another transaction.'), indicator: 'green' });
      return;
    }

    if (!state.rows.length) {
      show_fast_validation(state, 'item', 'Enter at least one item before taking payment.');
      return;
    }
    if (!state.customer) {
      show_fast_validation(state, 'customer', 'Select or create the actual Customer before taking payment.');
      return;
    }
    clear_fast_validation(state);
    // Encoder independently records the agreed selling rate. Manager PIN price
    // authorization belongs only to the Cashier Fast Screen and must not run here.

    state.paymentIntent = intent || { post: false, print: false, source: 'f11' };
    if (!state.payments.length) {
      if (MODE === 'cashier') set_blank_cash(state);
      else set_exact_cash(state);
    }
    rebalance_cash(state);

    const title = MODE === 'cashier'
      ? __('Payment — enter the amount received')
      : __('Payment Record — record how the customer paid');
    const d = new frappe.ui.Dialog({ title, fields: [{ fieldtype: 'HTML', fieldname: 'payment_grid' }] });
    state.paymentDialog = d;
    d.show();
    d.$wrapper.find('.modal-dialog').addClass('nkt-payment-dialog');
    render_payment_dialog(state, d);
    d.$wrapper.on('hidden.bs.modal', () => {
      state.paymentDialog = null;
      if (!state.suppressPaymentCloseFocus) {
        state.paymentIntent = null;
        render_payments(state);
        setTimeout(() => focus_item(state), 0);
      }
      state.suppressPaymentCloseFocus = false;
    });
  }

  function set_exact_cash(state) {
    const due = total(state);
    state.payments = [{ method: 'Cash', amount: due, reference: '', provider: '', check_date: '' }];
    state.cashTendered = due;
    state.cashTenderedManual = false;
    state.paymentConfirmed = false;
  }

  function set_blank_cash(state) {
    const due = total(state);
    state.payments = [{ method: 'Cash', amount: due, reference: '', provider: '', check_date: '' }];
    state.cashTendered = 0;
    state.cashTenderedManual = true;
    state.paymentConfirmed = false;
  }

  function add_payment_row(state) {
    const defaultMethod = state.payments.some(p => p.method === 'Cash') ? 'Bank Transfer' : 'Cash';
    state.payments.push({ method: defaultMethod, amount: 0, reference: '', provider: '', check_date: '' });
    if (defaultMethod === 'Cash') {
      state.cashTendered = 0;
      state.cashTenderedManual = MODE === 'cashier';
    }
    rebalance_cash(state);
    state.paymentConfirmed = false;
    return state.payments.length - 1;
  }

  function update_payment_inline_message(state, box, forceFocus = false) {
    const t = payment_totals(state);
    const inline = box.find('[data-pay-role="inline"]');
    let message = '';
    let focusCash = false;

    if (MODE === 'cashier' && t.cashRowPresent && t.cashDue > .005 && t.cashTendered + .005 < t.cashDue) {
      const short = t.cashDue - t.cashTendered;
      message = t.cashTendered > .005
        ? `Cash Received is short by ${format_money(short)}. Correct the amount, or click Add Payment Row for a split payment.`
        : `Enter the physical Cash Received. Amount still due: ${format_money(t.cashDue)}.`;
      focusCash = true;
    }

    inline.toggleClass('show', Boolean(message)).text(message);
    if (forceFocus && focusCash) {
      const input = box.find('.nkt-pay-input[data-field="cash_tendered"]:visible').first();
      input.trigger('focus').select();
    }
    return !message;
  }

  function nkt_consume_modal_key(e) {
    if (!e) return;
    if (typeof e.preventDefault === 'function') e.preventDefault();
    if (typeof e.stopPropagation === 'function') e.stopPropagation();
    if (typeof e.stopImmediatePropagation === 'function') e.stopImmediatePropagation();
  }

  function nkt_dialog_header_close_buttons(d) {
    if (!d || !d.$wrapper) return $();
    return d.$wrapper.find('.modal-header button, .modal-header .close, .modal-header .btn-modal-close, .modal-header [data-dismiss="modal"], .modal-header [data-bs-dismiss="modal"]');
  }

  function nkt_dialog_primary_button(d) {
    if (!d || !d.$wrapper) return $();
    if (typeof d.get_primary_btn === 'function') {
      const btn = d.get_primary_btn();
      if (btn && btn.length) return btn;
    }
    return d.$wrapper.find('.modal-footer .btn-primary:visible, .modal-footer button.btn-primary:visible').last();
  }

  function nkt_focus_control(target) {
    const control = target && target.jquery ? target.first() : $(target || []).first();
    if (!control.length) return;
    control.trigger('focus');
    if (control.is('input')) control.select();
  }

  function nkt_payment_default_focus(state, box) {
    if (MODE === 'cashier') {
      const cash = box.find('.nkt-pay-input[data-field="cash_tendered"]:visible').first();
      if (cash.length) return cash;
    }
    const method = box.find('.nkt-pay-method:visible').first();
    if (method.length) return method;
    return nkt_payment_tab_controls(box).first();
  }

  function nkt_payment_tab_controls(box) {
    if (!box || !box.length) return $();
    return box.find([
      'select:visible:not(:disabled)',
      'input:visible:not([readonly]):not([disabled])',
      'button[data-pay-action="add"]:visible:not(:disabled)',
      'button[data-pay-action="exact"]:visible:not(:disabled)',
      'button[data-pay-action="review"]:visible:not(:disabled)'
    ].join(','));
  }

  function nkt_payment_enter_controls(box) {
    if (!box || !box.length) return $();
    return box.find([
      'select:visible:not(:disabled)',
      'input:visible:not([readonly]):not([disabled])',
      'button[data-pay-action="review"]:visible:not(:disabled)'
    ].join(','));
  }

  function nkt_make_header_close_mouse_only(d, focusResolver, namespace) {
    if (!d || !d.$wrapper) return;
    const selector = '.modal-header button, .modal-header .close, .modal-header .btn-modal-close, .modal-header [data-dismiss="modal"], .modal-header [data-bs-dismiss="modal"]';
    const apply = () => {
      const close = nkt_dialog_header_close_buttons(d);
      close.attr('tabindex', '-1').attr('data-nkt-mouse-close-only', '1');
      if (close.filter(document.activeElement).length) {
        const target = typeof focusResolver === 'function' ? focusResolver() : $();
        if (target && target.length) nkt_focus_control(target);
      }
    };
    d.$wrapper.off(`focusin.${namespace}`, selector).on(`focusin.${namespace}`, selector, () => {
      setTimeout(() => {
        if (!d.$wrapper.is(':visible')) return;
        const target = typeof focusResolver === 'function' ? focusResolver() : $();
        if (target && target.length) nkt_focus_control(target);
      }, 0);
    });
    apply();
    setTimeout(apply, 0);
    setTimeout(apply, 60);
    setTimeout(apply, 180);
  }

  function nkt_safe_hide_dialog(d, label) {
    if (!d) return true;
    try {
      d.hide();
      return true;
    } catch (err) {
      console.error(`[NKT] Could not hide ${label || 'dialog'} through Frappe Dialog.hide().`, err);
      try {
        if (d.$wrapper) {
          d.$wrapper.removeClass('show').attr('aria-hidden', 'true').hide();
          d.$wrapper.trigger('hidden.bs.modal');
          return true;
        }
      } catch (fallbackErr) {
        console.error(`[NKT] Fallback hide failed for ${label || 'dialog'}.`, fallbackErr);
      }
      return false;
    }
  }

  function install_payment_dialog_keyboard(state, d, box) {
    if (!d || !d.$wrapper || !d.$wrapper[0]) return;
    const modal = d.$wrapper[0];
    if (d.__nktPaymentKeyHandler) modal.removeEventListener('keydown', d.__nktPaymentKeyHandler, true);

    const handler = e => {
      if (!d.$wrapper.is(':visible')) return;
      const key = e.key;
      const target = $(e.target);
      const headerClose = nkt_dialog_header_close_buttons(d);

      if (headerClose.filter(e.target).length && (key === 'Tab' || key === 'Enter' || key === ' ')) {
        nkt_consume_modal_key(e);
        const controls = nkt_payment_tab_controls(box);
        const next = key === 'Tab' && e.shiftKey ? controls.last() : nkt_payment_default_focus(state, box);
        nkt_focus_control(next);
        return;
      }

      if (key === 'F1') {
        nkt_consume_modal_key(e);
        return;
      }
      if (key === 'Escape') {
        nkt_consume_modal_key(e);
        d.hide();
        return;
      }

      if (key === 'Tab') {
        nkt_consume_modal_key(e);
        const controls = nkt_payment_tab_controls(box);
        if (!controls.length) return;
        const idx = controls.index(e.target);
        const direction = e.shiftKey ? -1 : 1;
        let next = idx >= 0 ? idx + direction : (direction > 0 ? 0 : controls.length - 1);
        if (next < 0) next = controls.length - 1;
        if (next >= controls.length) next = 0;
        nkt_focus_control(controls.eq(next));
        return;
      }

      if (key !== 'Enter' || target.is('textarea')) return;

      if (target.is('[data-pay-action="add"],[data-pay-action="exact"],[data-pay-remove]')) {
        // These exception controls retain their normal mouse/Enter click behavior,
        // but they are not part of the normal Enter progression.
        return;
      }

      nkt_consume_modal_key(e);

      if (target.is('[data-pay-action="review"]')) {
        review_settlement(state, d);
        return;
      }

      if (target.is('.nkt-pay-input[data-field="cash_tendered"]')) {
        if (!update_payment_inline_message(state, box, true)) return;
        review_settlement(state, d);
        return;
      }

      if (MODE !== 'cashier' && target.is('.nkt-pay-method')) {
        const row = Number(target.data('row'));
        if (state.payments[row] && state.payments[row].method === 'Cash') {
          review_settlement(state, d);
          return;
        }
      }

      const controls = nkt_payment_enter_controls(box);
      const idx = controls.index(e.target);
      if (idx >= 0 && idx < controls.length - 1) {
        nkt_focus_control(controls.eq(idx + 1));
      } else {
        review_settlement(state, d);
      }
    };

    d.__nktPaymentKeyHandler = handler;
    modal.addEventListener('keydown', handler, true);
    d.$wrapper.off('hidden.bs.modal.nktPaymentKeyboard').on('hidden.bs.modal.nktPaymentKeyboard', () => {
      if (d.__nktPaymentKeyHandler) {
        modal.removeEventListener('keydown', d.__nktPaymentKeyHandler, true);
        d.__nktPaymentKeyHandler = null;
      }
      d.$wrapper.off('.nktPaymentHeaderClose');
    });
  }

  function render_payment_dialog(state, d, focusSpec = null) {
    rebalance_cash(state);
    const box = d.fields_dict.payment_grid.$wrapper;
    nkt_make_header_close_mouse_only(d, () => nkt_payment_default_focus(state, box), 'nktPaymentHeaderClose');
    const methods = ['Cash', 'Check', 'GCash', 'Maya', 'Card', 'Bank Transfer', 'Online', 'Account'];
    const totals = payment_totals(state);
    const hasAdjusted = state.rows.some(r => Number(r.rate || 0) !== Number(r.standard_rate || 0));
    const changeBox = MODE === 'cashier'
      ? `<div class="nkt-summary-box change ${totals.change > .005 ? 'has-change' : ''}"><span>Change Due</span><b data-pay-summary="change">${format_money(totals.change)}</b></div>`
      : '';
    const summaryClass = MODE === 'cashier' ? '' : ' encoder';
    const amountHeading = MODE === 'cashier' ? 'Amount / Cash Received' : 'Recorded Amount';
    const note = MODE === 'cashier'
      ? `<b>Cash:</b> type the physical amount handed over. Cash Received starts blank. Press Enter to review once the whole receipt is covered; if it is short, the cursor stays in Cash Received. Use Add Payment Row for split payment.<br><b>Check:</b> record Check Number, Check Date, and Issuing Bank. Depositing and clearing remain later workflows.<br><b>Card:</b> exact 2% surcharge applies only to Card. Maya has no surcharge.`
      : `<b>Encoder:</b> record the payment amount and method already handled by the Cashier. Do not enter physical cash tendered or give Change from this screen.<br><b>Check:</b> record Check Number, Check Date, and Issuing Bank.<br><b>Card:</b> exact 2% surcharge applies only to Card. Maya has no surcharge.`;

    box.html(`<div class="nkt-payment-grid-shell">
      <div class="nkt-payment-summary${summaryClass}">
        <div class="nkt-summary-box"><span>Receipt Total</span><b>${format_money(totals.receiptTotal)}</b></div>
        <div class="nkt-summary-box"><span>Settled</span><b data-pay-summary="total">${format_money(totals.paymentTotal)}</b></div>
        <div class="nkt-summary-box remaining ${Math.abs(totals.balance) <= .005 ? 'ok' : 'bad'}"><span>Balance</span><b data-pay-summary="balance">${format_money(totals.balance)}</b></div>
        ${changeBox}
      </div>
      <table class="nkt-pay-table"><thead><tr><th class="p-method">Method</th><th class="p-amount">${amountHeading}</th><th class="p-ref">Reference / Check No.</th><th class="p-date">Check Date</th><th class="p-provider">Bank / Provider</th><th class="p-remove"></th></tr></thead>
      <tbody>${state.payments.map((p, i) => payment_row_markup(methods, p, i, state.payments.length, state)).join('')}</tbody></table>
      <div data-pay-role="inline" class="nkt-payment-inline"></div>
      <div class="nkt-pay-actions"><button type="button" data-pay-action="add">Add Payment Row</button>${MODE === 'cashier' ? '<button type="button" data-pay-action="exact">Exact Cash</button>' : ''}<div class="spacer"></div><button type="button" data-pay-action="review" class="btn-primary">Review Payment</button></div>
      <div class="nkt-payment-note">${hasAdjusted ? '<b>Price:</b> adjusted rates require the accepted authorization before posting.<br>' : ''}${note}</div>
    </div>`);

    update_payment_inline_message(state, box);
    box.off('.nktPay');
    box.on('change.nktPay', '.nkt-pay-method', function () {
      const i = Number($(this).data('row')); const method = $(this).val();
      const oldMethod = state.payments[i].method;
      if (method === 'Card') state.payments[i].provider = state.payments[i].provider || 'Card Terminal';
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
        state.cashTendered = 0;
        state.cashTenderedManual = MODE === 'cashier';
      } else if (method !== 'Check') {
        state.payments[i].check_date = '';
      }
      rebalance_cash(state);
      state.paymentConfirmed = false;
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
      state.paymentConfirmed = false;
      update_payment_dialog_summary(state, box);
    });
    box.on('click.nktPay', '[data-pay-remove]', function () {
      const i = Number($(this).data('pay-remove'));
      const removedCash = state.payments[i] && state.payments[i].method === 'Cash';
      state.payments.splice(i, 1);
      if (removedCash) { state.cashTendered = 0; state.cashTenderedManual = false; }
      if (!state.payments.length) {
        if (MODE === 'cashier') set_blank_cash(state);
        else set_exact_cash(state);
      } else {
        rebalance_cash(state);
      }
      state.paymentConfirmed = false;
      render_payment_dialog(state, d);
    });
    box.on('click.nktPay', '[data-pay-action="add"]', () => {
      const row = add_payment_row(state);
      render_payment_dialog(state, d, { row, method: true });
    });
    box.on('click.nktPay', '[data-pay-action="exact"]', () => { set_exact_cash(state); render_payment_dialog(state, d, { cash: true }); });
    box.on('click.nktPay', '[data-pay-action="review"]', () => review_settlement(state, d));
    install_payment_dialog_keyboard(state, d, box);

    setTimeout(() => {
      let target = $();
      if (focusSpec && focusSpec.cash && MODE === 'cashier') {
        target = box.find('.nkt-pay-input[data-field="cash_tendered"]:visible').first();
      }
      if (!target.length && focusSpec && Number.isInteger(Number(focusSpec.row))) {
        const row = Number(focusSpec.row);
        if (focusSpec.method) target = box.find(`.nkt-pay-method[data-row="${row}"]`).first();
        if (!target.length && focusSpec.afterMethod) target = box.find(`.nkt-pay-input[data-row="${row}"]:visible:not([readonly]):not([disabled])`).first();
        if (!target.length) target = box.find(`.nkt-pay-method[data-row="${row}"]`).first();
      }
      if (!target.length && MODE === 'cashier') target = box.find('.nkt-pay-input[data-field="cash_tendered"]:visible').first();
      if (!target.length) target = box.find('.nkt-pay-method').first();
      target.trigger('focus');
      if (target.is('input')) target.select();
    }, 25);
  }

  function payment_row_markup(methods, p, i, count, state) {
    const cash = p.method === 'Cash';
    const check = p.method === 'Check';
    const cashTenderInput = cash && MODE === 'cashier';
    const field = cashTenderInput ? 'cash_tendered' : 'amount';
    const value = cashTenderInput ? state.cashTendered : p.amount;
    const displayedValue = cashTenderInput && state.cashTenderedManual && Number(value || 0) === 0 ? '' : format_edit(value);
    const amountReadonly = (cash && MODE !== 'cashier');
    const refDisabled = cash || p.method === 'Account';
    const refPlaceholder = cash || p.method === 'Account' ? 'Not required' : (check ? 'Check number required' : 'Required');
    return `<tr>
      <td><select class="nkt-pay-method" data-row="${i}">${methods.map(m => `<option value="${m}" ${m === p.method ? 'selected' : ''}>${m}</option>`).join('')}</select></td>
      <td><input class="nkt-pay-input" data-row="${i}" data-field="${field}" type="number" min="0" step="0.01" value="${displayedValue}" ${amountReadonly ? 'readonly' : ''} placeholder="${cashTenderInput ? 'Cash received' : ''}"></td>
      <td><input class="nkt-pay-input" data-row="${i}" data-field="reference" value="${esc_attr(p.reference || '')}" placeholder="${refPlaceholder}" ${refDisabled ? 'readonly' : ''}></td>
      <td><input class="nkt-pay-input" data-row="${i}" data-field="check_date" type="date" value="${esc_attr(p.check_date || '')}" ${check ? '' : 'disabled'}></td>
      <td><input class="nkt-pay-input" data-row="${i}" data-field="provider" value="${esc_attr(p.provider || '')}" placeholder="${check ? 'Issuing bank required' : ''}" ${cash ? 'readonly' : ''}></td>
      <td><button type="button" data-pay-remove="${i}">×</button></td>
    </tr>`;
  }

  function update_payment_dialog_summary(state, box) {
    const totals = payment_totals(state);
    box.find('[data-pay-summary="total"]').text(format_money(totals.paymentTotal));
    box.find('[data-pay-summary="balance"]').text(format_money(totals.balance)).closest('.remaining').toggleClass('ok', Math.abs(totals.balance) <= .005).toggleClass('bad', Math.abs(totals.balance) > .005);
    box.find('[data-pay-summary="change"]').text(format_money(totals.change)).closest('.change').toggleClass('has-change', totals.change > .005);
    if (MODE !== 'cashier') {
      state.payments.forEach((p, i) => {
        if (p.method === 'Cash') box.find(`.nkt-pay-input[data-row="${i}"][data-field="amount"]`).val(format_edit(p.amount));
      });
    }
    update_payment_inline_message(state, box);
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
    if (MODE === 'cashier' && t.cashDue > .005 && t.cashTendered + .005 < t.cashDue) return `Cash Received is short by ${format_money(t.cashDue - t.cashTendered)}.`;
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
    d.$wrapper.find('.modal-dialog').addClass('nkt-payment-confirmation-dialog');
    d.fields_dict.confirmation.$wrapper.html(`<div class="nkt-confirm-box"><div class="nkt-confirm-title">${MODE === 'cashier' ? 'RECEIPT PAYMENT CONFIRMATION' : 'PAYMENT RECORD CONFIRMATION'}</div>
      <div class="nkt-confirm-row"><span>Receipt Total</span><b>${format_money(total(state))}</b></div>${nonCashBreakdown}
      ${t.cashRowPresent ? `<div class="nkt-confirm-row"><span>${MODE === 'cashier' ? 'Cash Due' : 'Cash Recorded'}</span><b>${format_money(t.cashDue)}</b></div>` : ''}
      ${MODE === 'cashier' && t.cashRowPresent ? `<div class="nkt-confirm-row"><span>Cash Received</span><b>${format_money(t.cashTendered)}</b></div>` : ''}
      ${t.cardSurcharge ? `<div class="nkt-confirm-row"><span>Card Surcharge — 2%</span><b>${format_money(t.cardSurcharge)}</b></div><div class="nkt-confirm-row"><span>Actual Collected</span><b>${format_money(t.grossCollected)}</b></div>` : ''}
      ${MODE === 'cashier' && t.cashRowPresent ? `<div class="nkt-confirm-change">CHANGE DUE: ${format_money(t.change)}</div>` : ''}
      <div class="nkt-confirm-hint">Press Enter to Confirm • Esc to Return</div></div>`);

    let phase = 'ready';
    let primary = $();
    let keyHandler = null;
    const originalIntent = state.paymentIntent;

    const remove_confirmation_keyboard = () => {
      if (d.__nktSettlementConfirmKeyHandler) {
        document.removeEventListener('keydown', d.__nktSettlementConfirmKeyHandler, true);
        d.__nktSettlementConfirmKeyHandler = null;
      }
      d.$wrapper.off('.nktConfirmationHeaderClose');
    };

    const install_confirmation_keyboard = () => {
      if (!keyHandler || d.__nktSettlementConfirmKeyHandler) return;
      d.__nktSettlementConfirmKeyHandler = keyHandler;
      document.addEventListener('keydown', keyHandler, true);
    };

    const return_to_payment = () => {
      setTimeout(() => {
        if (!paymentDialog || !paymentDialog.$wrapper || !paymentDialog.$wrapper.is(':visible')) return;
        const box = paymentDialog.fields_dict.payment_grid.$wrapper;
        nkt_focus_control(nkt_payment_default_focus(state, box));
      }, 0);
    };

    const confirm = () => {
      if (phase !== 'ready') return;
      phase = 'working';
      if (primary.length) primary.prop('disabled', true).attr('aria-busy', 'true');
      const intent = state.paymentIntent || { post: false, print: false, source: 'f11' };

      try {
        state.paymentIntent = null;
        state.paymentConfirmed = true;
        state.suppressPaymentCloseFocus = Boolean(intent.post);

        // The settlement has already passed full validation. A noncritical
        // preview refresh must never strand the operator inside this dialog.
        try {
          render_payments(state);
        } catch (renderErr) {
          console.error('[NKT] Payment was confirmed, but the Fast Screen preview could not refresh immediately.', renderErr);
        }

        // Detach the confirmation capture handler synchronously before
        // Finalize can open Sale Complete. Do not wait for the delayed Bootstrap
        // hidden event, because a stale handler would consume the next Enter.
        remove_confirmation_keyboard();
        const confirmationHidden = nkt_safe_hide_dialog(d, 'Payment Confirmation');
        const paymentHidden = nkt_safe_hide_dialog(paymentDialog, 'Payment Record');
        if (!confirmationHidden || !paymentHidden) {
          throw new Error('One or more payment dialogs could not be closed safely.');
        }
        phase = 'done';

        if (intent.post) {
          setTimeout(() => finalize_live(state, Boolean(intent.print)), 0);
        } else {
          frappe.show_alert({ message: __('Payment confirmed. You may still edit the transaction; any edit will require payment confirmation again.'), indicator: 'green' }, 3);
        }
      } catch (err) {
        console.error('[NKT] Payment confirmation could not finish.', err);
        phase = 'ready';
        state.paymentIntent = originalIntent;
        state.paymentConfirmed = false;
        state.suppressPaymentCloseFocus = false;
        if (primary.length) primary.prop('disabled', false).removeAttr('aria-busy');
        if (d.$wrapper && d.$wrapper.is(':visible')) install_confirmation_keyboard();
        frappe.msgprint({
          title: __('Payment Confirmation Could Not Finish'),
          indicator: 'red',
          message: __('The payment data was preserved. Press Confirm Payment again, or press Esc to return and review the payment.')
        });
      }
    };

    d.set_primary_action(__('Confirm Payment'), confirm);
    primary = nkt_dialog_primary_button(d);
    primary.attr('tabindex', '0').attr('data-nkt-payment-confirm', '1');

    // Own the button click directly instead of depending on a framework latch.
    // This also permits a clean retry if an earlier UI-only step throws.
    primary.off('click').on('click.nktPaymentConfirm', e => {
      nkt_consume_modal_key(e);
      confirm();
    });

    nkt_make_header_close_mouse_only(d, () => primary, 'nktConfirmationHeaderClose');

    keyHandler = e => {
      if (!d.$wrapper.is(':visible')) return;
      if (e.key === 'F1') {
        nkt_consume_modal_key(e);
        return;
      }
      if (e.key === 'Escape') {
        nkt_consume_modal_key(e);
        d.hide();
        return;
      }
      if (e.key === 'Tab') {
        nkt_consume_modal_key(e);
        nkt_focus_control(primary);
        return;
      }
      if (e.key === 'Enter' && !$(e.target).is('textarea')) {
        nkt_consume_modal_key(e);
        confirm();
      }
    };

    install_confirmation_keyboard();
    d.$wrapper.off('hidden.bs.modal.nktSettlementConfirm').on('hidden.bs.modal.nktSettlementConfirm', () => {
      remove_confirmation_keyboard();
      if (phase === 'ready') return_to_payment();
    });

    setTimeout(() => nkt_focus_control(primary), 0);
    setTimeout(() => nkt_focus_control(primary), 60);
  }

  function review_settlement(state, paymentDialog) {
    const box = paymentDialog.fields_dict.payment_grid.$wrapper;
    if (!update_payment_inline_message(state, box, true)) return;
    const error = validate_settlement(state);
    if (error) {
      frappe.msgprint({ title: __('Payment is not complete'), indicator: 'red', message: __(error) });
      return;
    }
    preflight_incoming_checks(state).then(ok => {
      if (ok) show_payment_confirmation(state, paymentDialog);
    });
  }

  function render_payments(state) {
    const box = role(state, 'payment-lines');
    if (!state.payments.length) {
      box.text(MODE === 'cashier'
        ? 'Press F11 Payment, or press F12/F10 to continue directly to payment.'
        : 'Press F11 to record the payment already handled by the Cashier.');
      render_payment_status(state);
      return;
    }
    const t = payment_totals(state);
    box.html(state.payments.map(p => {
      if (p.method === 'Cash') {
        if (MODE === 'cashier') {
          return `<div class="nkt-payment-line"><span>Cash Due</span><b>${format_money(t.cashDue)}</b><small>Cash Received ${format_money(t.cashTendered)} • Change ${format_money(t.change)}</small></div>`;
        }
        return `<div class="nkt-payment-line"><span>Cash Recorded</span><b>${format_money(t.cashDue)}</b></div>`;
      }
      return `<div class="nkt-payment-line"><span>${esc(payment_description(p))}</span><b>${format_money(p.amount)}</b>${p.method === 'Card' ? `<small>2% Card surcharge ${format_money(Number(p.amount || 0) * .02)} • Gross ${format_money(Number(p.amount || 0) * 1.02)}</small>` : ''}</div>`;
    }).join('') + `<div class="nkt-payment-line"><span>Balance</span><b>${format_money(t.balance)}</b></div>`);
    render_payment_status(state);
  }

  function render_payment_status(state) {
    const status = role(state, 'payment-status');
    if (!state.payments.length) status.text('Not entered');
    else if (state.paymentConfirmed && Math.abs(payment_totals(state).balance) <= .005) status.text('Confirmed');
    else if (Math.abs(payment_totals(state).balance) <= .005) status.text('Needs confirmation');
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
      change_amount: t.change,
      plate_number: String(state.plateNumber || '').trim(),
      source_order_slip: String(state.orderSlipNumber || '').trim(),
      remarks: String(state.remarks || '').trim()
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

  function show_cashier_completion_dialog(state, result, print) {
    const t = payment_totals(state);
    const reference = result.receipt_reference || result.payment_receipt || result.cashier_sale || result.request_id || state.requestId || '';
    const d = new frappe.ui.Dialog({ title: __('Sale Complete'), fields: [{ fieldtype: 'HTML', fieldname: 'completion' }] });
    state.completionDialog = d;
    d.show();
    d.$wrapper.find('.modal-dialog').addClass('nkt-completion-dialog');
    d.fields_dict.completion.$wrapper.html(`<div class="nkt-completion-box">
      <div class="nkt-completion-title">SALE COMPLETE</div>
      <div class="nkt-completion-reference">${reference ? `Receipt ${esc(reference)}` : 'Transaction posted successfully'}${print ? ' • Print opened' : ''}</div>
      <div class="nkt-completion-change-label">CHANGE DUE</div>
      <div class="nkt-completion-change">${format_money(t.change)}</div>
      <div class="nkt-completion-hint">Press Enter for the next sale</div>
    </div>`);

    let phase = 'ready';
    let primary = $();
    let completionKeyHandler = null;

    const removeCompletionKeyboard = () => {
      if (completionKeyHandler) {
        window.removeEventListener('keydown', completionKeyHandler, true);
        completionKeyHandler = null;
      }
      if (window.__nktSaleCompleteAckHandler) {
        window.removeEventListener('keydown', window.__nktSaleCompleteAckHandler, true);
        window.__nktSaleCompleteAckHandler = null;
      }
      $(document).off('keydown.nktSaleCompleteAck');
    };

    const nextSale = () => {
      if (phase !== 'ready') return;
      phase = 'working';
      removeCompletionKeyboard();
      d.$wrapper.off('hide.bs.modal.nktSaleCompleteGuard');
      try {
        if (!nkt_safe_hide_dialog(d, 'Sale Complete')) {
          throw new Error('Sale Complete could not be closed safely.');
        }
        state.completionDialog = null;
        start_new_transaction(state);
        focus_item(state);
        phase = 'done';
      } catch (err) {
        console.error('[NKT] Could not start the next sale from the completion window.', err);
        phase = 'ready';
        installCompletionKeyboard();
        if (primary.length) nkt_focus_control(primary);
        frappe.msgprint({
          title: __('Next Sale Could Not Start'),
          indicator: 'red',
          message: __('The completed receipt is safe. Press Enter or click Next Sale again. If it still does not continue, refresh the Fast Screen.')
        });
      }
    };

    d.set_primary_action(__('Next Sale'), nextSale);
    primary = nkt_dialog_primary_button(d);
    primary.attr('tabindex', '0').attr('data-nkt-next-sale', '1');
    primary.off('click.nktSaleComplete').on('click.nktSaleComplete', e => {
      nkt_consume_modal_key(e);
      nextSale();
    });

    const headerClose = nkt_dialog_header_close_buttons(d);
    headerClose.attr('tabindex', '-1').attr('data-nkt-mouse-close-only', '1');
    headerClose.off('click.nktSaleComplete').on('click.nktSaleComplete', e => {
      nkt_consume_modal_key(e);
      nextSale();
    });

    d.$wrapper.off('hide.bs.modal.nktSaleCompleteGuard').on('hide.bs.modal.nktSaleCompleteGuard', e => {
      if (phase !== 'working' && phase !== 'done') e.preventDefault();
    });

    const installCompletionKeyboard = () => {
      removeCompletionKeyboard();
      completionKeyHandler = e => {
        if (state.completionDialog !== d || !d.$wrapper.is(':visible')) return;
        const key = String(e.key || '');
        if (key === 'F1') {
          nkt_consume_modal_key(e);
          return;
        }
        if (key === 'Tab') {
          nkt_consume_modal_key(e);
          nkt_focus_control(primary);
          return;
        }
        if (key === 'Escape' || key === 'Esc') {
          nkt_consume_modal_key(e);
          nkt_focus_control(primary);
          return;
        }
        if (key !== 'Enter') return;
        nkt_consume_modal_key(e);
        nextSale();
      };
      window.__nktSaleCompleteAckHandler = completionKeyHandler;
      window.addEventListener('keydown', completionKeyHandler, true);
    };

    installCompletionKeyboard();
    setTimeout(() => nkt_focus_control(primary), 0);
    setTimeout(() => nkt_focus_control(primary), 60);
  }

  function finish_posting_success(state, result, print, attempt) {
  if (!state.finalizing || attempt !== state.postingAttempt) return;
  clear_posting_watchdog(state);
  role(state, 'screen-status').text(has_meaningful_transaction_details(state) ? 'SAVING DETAILS' : 'COMPLETING');
  set_posting_state(state, true);

  persist_transaction_details(state, result).then(() => {
    if (!release_posting_attempt(state, attempt)) return;
    state.lastPostCompletedAt = Date.now();
    if (result && result.local_continuity) state.continuityTender = true;
    show_posting_result(state, result, print);
    if (print && result.print_html) open_local_cashier_print(result.print_html);
    else if (print && result.print_url) window.open(result.print_url, '_blank', 'noopener');

    state.completedTransaction = result;
    role(state, 'payment-status').text(MODE === 'cashier' ? 'PAID / COMPLETED' : 'COMPLETED');
    role(state, 'screen-status').text('COMPLETED');
    state.wrapper.addClass('nkt-transaction-completed');
    state.wrapper.find('[data-action="f10"],[data-action="f11"],[data-action="f12"],[data-action="add-item"],[data-action="new-customer"],.nkt-remove,.nkt-qty,.nkt-rate,.nkt-warehouse,[data-role="customer-entry"],[data-role="item-entry"],[data-role="plate-number"],[data-role="order-slip-number"],[data-role="remarks"]').prop('disabled', true);

    if (MODE === 'cashier') {
      show_cashier_completion_dialog(state, result, print);
      return;
    }

    setTimeout(() => {
      if (state.completedTransaction !== result || state.finalizing) return;
      start_new_transaction(state);
      focus_item(state);
    }, 250);
  }).catch(error => {
    if (!release_posting_attempt(state, attempt)) return;
    role(state, 'screen-status').text('DETAILS NEED RETRY');
    const detailMessage = posting_error_text(error) || __('The transaction posted, but its optional transaction details were not saved.');
    frappe.msgprint({
      title: __('Transaction posted — details need retry'),
      indicator: 'orange',
      message: `${esc(detailMessage)}<br><br>The Customer, items, payment, Plate Number, OS#, and Remarks were kept on this screen. Press <b>${print ? 'F10' : 'F12'}</b> once after the issue is corrected. The same Request ID will be reused, so the posted transaction will not be duplicated.`
    });
  });
}

  function recover_posting_status(state, print, attempt, pollNo) {
    if (!state.finalizing || attempt !== state.postingAttempt) return;
    if (!pollNo) frappe.show_alert({ message: __('Posting is taking longer than expected. Checking the Request ID before allowing any retry…'), indicator: 'orange' });
    frappe.call({
      method: 'nkt_operations.nkt_store_operations.fast_screen_backend.get_fast_request_status',
      args: { mode: MODE, request_id: state.requestId },
      freeze: false
    }).then(r => {
      if (!state.finalizing || attempt !== state.postingAttempt) return;
      const status = r.message || {};
      if (status.found && status.submitted && status.result && status.result.ok) {
        finish_posting_success(state, status.result, print, attempt);
        return;
      }
      if (status.found && !status.submitted) {
        if (!release_posting_attempt(state, attempt)) return;
        frappe.msgprint({
          title: __('Posting requires inspection'),
          indicator: 'red',
          message: `Request ID <code>${esc(state.requestId)}</code> exists as ${esc(status.name || '')} but is not submitted. Do not re-encode this transaction; ask an Administrator to inspect it.`
        });
        return;
      }
      if ((pollNo || 0) < 4) {
        state.postingWatchdog = setTimeout(() => recover_posting_status(state, print, attempt, (pollNo || 0) + 1), 4000);
        return;
      }
      const requestId = state.requestId;
      if (!release_posting_attempt(state, attempt)) return;
      frappe.msgprint({
        title: __('Posting response was lost'),
        indicator: 'orange',
        message: `No submitted transaction currently exists for Request ID <code>${esc(requestId)}</code>. The screen data was kept. You may press Finalize again; the <b>same Request ID</b> will be reused, so server idempotency prevents a duplicate if the first request later completes.`
      });
    }).catch(() => {
      if (!state.finalizing || attempt !== state.postingAttempt) return;
      if ((pollNo || 0) < 4) {
        state.postingWatchdog = setTimeout(() => recover_posting_status(state, print, attempt, (pollNo || 0) + 1), 4000);
      }
    });
  }

  function finalize_live(state, print) {
    if (state.completedTransaction) {
      frappe.show_alert({ message: MODE === 'cashier' ? __('Press Enter on the Sale Complete window to start the next sale.') : __('This transaction is already completed.'), indicator: 'green' });
      return;
    }

    if (!state.rows.length && !state.customer && state.lastPostCompletedAt && (Date.now() - state.lastPostCompletedAt) < 15000) return;
    if (state.finalizing) {
      frappe.show_alert({ message: __('Posting is already in progress. The screen is checking this Request ID automatically; do not start a second transaction.'), indicator: 'orange' });
      return;
    }
    if (!state.rows.length) {
      show_fast_validation(state, 'item', 'Enter at least one item before continuing.');
      return;
    }
    if (!state.customer) {
      show_fast_validation(state, 'customer', 'Select or create the actual Customer before continuing.');
      return;
    }
    clear_fast_validation(state);
    if (!state.paymentConfirmed) {
      open_payment_preview(state, { post: true, print: Boolean(print), source: print ? 'f10' : 'f12' });
      return;
    }
    if (!state.boot || !state.boot.posting_enabled) {
      frappe.msgprint(__('Live posting is not enabled on this fast screen.'));
      return;
    }
    if (MODE === 'cashier' && !state.boot.open_shift) frappe.show_alert({ message: __('Rechecking the Cashier Shift on the server…'), indicator: 'orange' });
    // Encoder does not invoke Cashier-side Manager PIN price authorization.

    const method = MODE === 'cashier'
      ? 'nkt_operations.nkt_store_operations.fast_screen_backend.finalize_cashier_fast_transaction'
      : 'nkt_operations.nkt_store_operations.fast_screen_backend.finalize_encoder_fast_transaction';
    const attempt = state.postingAttempt + 1;
    if (state.continuityTender) state.continuityTenderAttempted = true;
    state.postingAttempt = attempt;
    state.finalizing = true;
    role(state, 'screen-status').text('POSTING');
    set_posting_state(state, true);
    clear_posting_watchdog(state);
    state.postingWatchdog = setTimeout(() => recover_posting_status(state, print, attempt, 0), 12000);

    frappe.call({
      method,
      args: { payload: JSON.stringify(build_live_payload(state)), device_id: bound_device_id() },
      freeze: true,
      freeze_message: MODE === 'cashier' ? __('Posting Cashier transaction…') : __('Posting Encoder transaction…')
    }).then(r => {
      if (attempt !== state.postingAttempt || !state.finalizing) return;
      const result = r.message || {};
      if (!result.ok) throw new Error(__('The posting endpoint did not return a successful result.'));
      finish_posting_success(state, result, print, attempt);
    }).catch(err => {
      if (attempt !== state.postingAttempt || !state.finalizing) return;
      if (!release_posting_attempt(state, attempt)) return;
      role(state, 'screen-status').text('READY');
      show_posting_error(state, err);
    });
  }

  function set_posting_state(state, active) {
  state.wrapper.find('[data-action="f10"],[data-action="f12"]').prop('disabled', !!active);
  state.wrapper.find('[data-role="plate-number"],[data-role="order-slip-number"],[data-role="remarks"]').prop('disabled', !!active || Boolean(state.completedTransaction));
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
    let reference = '';
    if (MODE === 'cashier') {
      reference = result.receipt_reference || result.payment_receipt || result.cashier_sale || result.request_id || state.requestId || '';
    } else {
      reference = result.customer_order || result.request_id || state.requestId || '';
    }
    let message = MODE === 'cashier' ? __('PAID / COMPLETED') : __('COMPLETED');
    if (reference) message += ` — ${reference}`;
    if (result.replayed) message += ' — Safe retry confirmed';
    if (print) message += ' — Print opened';
    frappe.show_alert({ message, indicator: 'green' }, 3);
  }

  function readonly_notice(state, action) {
    frappe.msgprint({
      title: __('Not connected in V2.0C.3'),
      indicator: 'blue',
      message: __(`${action} is not connected in this standard-payment stage. It creates no operational record.`)
    });
  }

  function start_new_transaction(state) {
    $(document).off('keydown.nktSaleCompleteAck');
    if (state.keyRearmTimer) {
      clearTimeout(state.keyRearmTimer);
      state.keyRearmTimer = null;
    }
    clear_fast_validation(state);
    if (state.clearDialog) {
      try { state.clearDialog.hide(); } catch (_) {}
      state.clearDialog = null;
    }
    if (state.completionDialog) {
      try { state.completionDialog.hide(); } catch (_) {}
      state.completionDialog = null;
    }
    state.completedTransaction = null;
    state.paymentIntent = null;
    state.suppressPaymentCloseFocus = false;
    state.wrapper.removeClass('nkt-transaction-completed');
    state.wrapper.find('[data-action="f10"],[data-action="f11"],[data-action="f12"],[data-action="add-item"],[data-action="new-customer"],[data-role="customer-entry"],[data-role="item-entry"],[data-role="plate-number"],[data-role="order-slip-number"],[data-role="remarks"]').prop('disabled', false);
    state.rows = [];
    state.payments = [];
    state.cashTendered = 0;
    state.cashTenderedManual = false;
    state.paymentConfirmed = false;
    state.customer = null;
    state.priceAuthorization = null;
    state.plateNumber = '';
    state.orderSlipNumber = '';
    state.remarks = '';
    state.detailPersistencePromise = null;
    state.detailPersistenceKey = '';
    state.requestId = make_request_id();
    role(state, 'screen-status').text('READY');
    role(state, 'customer-entry').val('');
    role(state, 'plate-number').val('');
    role(state, 'order-slip-number').val('');
    role(state, 'remarks').val('');
    role(state, 'customer-selected').find('.nkt-customer-name').text('No customer selected');
    role(state, 'customer-balance').text(format_money(0));
    role(state, 'customer-status').text('Required before payment');
    render_grid(state);
    render_payments(state);
    rearm_fast_function_keyboard(state, 1);
    focus_item(state);
  }

  function make_request_id() {
    if (window.crypto && typeof window.crypto.randomUUID === 'function') return window.crypto.randomUUID();
    return `nkt-${Date.now()}-${Math.random().toString(16).slice(2)}-${Math.random().toString(16).slice(2)}`;
  }

  function round2(v) { return Math.round((Number(v || 0) + Number.EPSILON) * 100) / 100; }
  function esc(v) { return frappe.utils.escape_html(String(v ?? '')); }
  function esc_attr(v) { return esc(v).replace(/"/g, '&quot;'); }
  function format_money(v) { return new Intl.NumberFormat('en-PH', { style: 'currency', currency: 'PHP', minimumFractionDigits: 2 }).format(Number(v || 0)); }
  function format_plain_money(v) { return new Intl.NumberFormat('en-PH', { minimumFractionDigits: 2, maximumFractionDigits: 2 }).format(Number(v || 0)); }
  function format_qty(v) { return new Intl.NumberFormat('en-PH', { maximumFractionDigits: 3 }).format(Number(v || 0)); }
  function format_edit(v) { return Number(v || 0).toString(); }
})();

/* ===== END SOURCE: NKT Encoder Fast Screen V2.0C.3 ===== */

/* ===== SOURCE: NKT Encoder Warehouse Change Link V2.0C.4.2.1 ===== */
(() => {
  const DOCTYPE = 'NKT Encoder Fast Screen';
  frappe.ui.form.on(DOCTYPE, {
    refresh(frm) {
      frm.page.add_inner_button(__('Warehouse Change / Recent Orders'), () => {
        frappe.set_route('Form', 'NKT Warehouse Change Fast Screen', 'NKT Warehouse Change Fast Screen');
      });
    }
  });
})();

/* ===== END SOURCE: NKT Encoder Warehouse Change Link V2.0C.4.2.1 ===== */

/* ===== SOURCE: NKT R8A Encoder Frontline Presentation + F6 Item History ===== */
// NKT R4 UI5B - Compact Item Movement History columns, A4, person names, and front-layer print dialogs
(() => {
    const DOCTYPE = "NKT Encoder Fast Screen";
    const SERVER = "nkt_operations.nkt_store_operations.item_movement_history";
    const PAGE_LENGTH = 200;
    const TERMS = [/^\s*Request ID\b/i, /^\s*Reconciliation Diagnostics\b/i];
    const PRINT_COLUMNS = [
        ["encoded_at", "Encoded At"],
        ["movement_type", "Movement"],
        ["customer_name", "Customer"],
        ["quantity", "Qty"],
        ["selling_rate", "Selling Rate"],
        ["amount", "Amount"],
        ["status", "Status"],
        ["return_exchange", "Return / Exchange"],
        ["encoded_by", "Encoded By"]
    ];

    function directText(el) {
        return Array.from(el.childNodes || [])
            .filter((node) => node.nodeType === Node.TEXT_NODE)
            .map((node) => node.nodeValue || "")
            .join(" ")
            .trim();
    }

    function chooseTarget(el, text) {
        let node = el;
        const diagnostics = /Reconciliation Diagnostics/i.test(text);
        for (let i = 0; i < 6 && node; i += 1) {
            const total = (node.textContent || "").trim();
            if (node.matches) {
                if (diagnostics && node.matches(".alert, .card, [class*='diagnostic'], section")) return node;
                if (!diagnostics && node.matches("tr, p, li, .form-group, .control-group, [class*='meta']")) return node;
            }
            const maxLength = diagnostics ? 1800 : 500;
            if (node.parentElement && total.length < maxLength) node = node.parentElement;
            else break;
        }
        return el.parentElement || el;
    }

    function scrub(root = document) {
        if (!window.cur_frm || cur_frm.doctype !== DOCTYPE) return;
        root.querySelectorAll("*").forEach((el) => {
            const text = directText(el);
            if (!text) return;
            if (TERMS.some((rx) => rx.test(text))) {
                const target = chooseTarget(el, text);
                target.style.setProperty("display", "none", "important");
                target.setAttribute("data-nkt-r8a-hidden", "1");
            }
        });
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

    function compactNumber(value) {
        const n = number(value);
        return n.toLocaleString(undefined, {maximumFractionDigits: 6});
    }

    function money(value, blankWhenMissing = true) {
        if ((value === null || value === undefined || value === "") && blankWhenMissing) return "";
        const n = Number(value);
        if (!Number.isFinite(n)) return blankWhenMissing ? "" : "0.00";
        const sign = n < 0 ? "−" : n > 0 ? "+" : "";
        return `${sign}${Math.abs(n).toLocaleString(undefined, {minimumFractionDigits: 2, maximumFractionDigits: 2})}`;
    }

    function formatQty(row) {
        const effect = number(row?.stock_effect_qty);
        const orderQty = number(row?.order_qty);
        const uom = String(row?.uom || "").trim();
        if (Math.abs(effect) > 0.000001) {
            return `${effect > 0 ? "+" : "−"}${compactNumber(Math.abs(effect))}${uom ? ` ${uom}` : ""}`;
        }
        if (orderQty > 0.000001) {
            return `0 (Order ${compactNumber(orderQty)}${uom ? ` ${uom}` : ""})`;
        }
        return `0${uom ? ` ${uom}` : ""}`;
    }

    function estimatePages(rowCount, paperSize, density) {
        const rows = Math.max(1, Number(rowCount || 0));
        const matrix = {
            long: {"5": 92, "4": 118},
            short: {"5": 75, "4": 96},
            a4: {"5": 82, "4": 105}
        };
        const perPage = matrix[paperSize]?.[density] || matrix.long["5"];
        return Math.max(1, Math.ceil(rows / perPage));
    }

    function deviceId() {
        const key = "nkt.item-movement-history.device-id";
        let value = "";
        try { value = localStorage.getItem(key) || ""; } catch (_error) {}
        if (!value) {
            value = (window.crypto?.randomUUID?.() || `imh-${Date.now()}-${Math.random().toString(36).slice(2)}`);
            try { localStorage.setItem(key, value); } catch (_error) {}
        }
        return value;
    }

    function guessItem(frm) {
        const selected = frm?.fields_dict?.items?.grid?.get_selected_children?.() || [];
        const docRows = (frm?.doc?.items || []);
        const docRow = selected[0] || docRows[docRows.length - 1];
        if (docRow) return docRow.item || docRow.item_code || "";
        const gridRows = Array.from(document.querySelectorAll(".nkt-fast-shell .nkt-grid tbody tr[data-row]"));
        const row = gridRows[gridRows.length - 1];
        return row?.querySelector("td:nth-child(2)")?.textContent?.trim() || "";
    }

    function makeLinkControl(host, fieldname, label, options, value) {
        const control = frappe.ui.form.make_control({
            parent: host,
            render_input: true,
            df: {fieldname, label, fieldtype: "Link", options}
        });
        control.refresh();
        if (value) control.set_value(value);
        return control;
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

    function baseMarkup() {
        return `
          <div class="nkt-imh-workspace" role="region" aria-label="Item Movement History">
            <div class="nkt-imh-titlebar">
              <div>
                <strong>ITEM MOVEMENT HISTORY</strong>
                <span>Actual warehouse movement plus pending / unreleased orders</span>
              </div>
              <div class="nkt-imh-title-actions">
                <button type="button" data-action="print">Print</button>
                <button type="button" data-action="close" class="primary">Return to Order</button>
              </div>
            </div>
            <div class="nkt-imh-scopebar">
              <span>Item: <b data-role="scope-item">Select an Item</b></span>
              <span>Warehouse: <b data-role="scope-warehouse">Select one Warehouse</b></span>
              <span>Encoded At includes seconds and preserves original offline capture time when available.</span>
            </div>
            <div class="nkt-imh-filters">
              <div class="nkt-imh-field wide"><div data-control="item"></div></div>
              <div class="nkt-imh-field"><label>Warehouse</label><select data-filter="warehouse"></select></div>
              <div class="nkt-imh-field wide"><div data-control="customer"></div></div>
              <div class="nkt-imh-field"><label>From Encoded At</label><input type="datetime-local" step="1" data-filter="from_encoded_at"></div>
              <div class="nkt-imh-field"><label>To Encoded At</label><input type="datetime-local" step="1" data-filter="to_encoded_at"></div>
              <div class="nkt-imh-field"><label>Exact Quantity</label><input type="number" min="0" step="any" data-filter="exact_quantity" placeholder="e.g. 5"></div>
              <div class="nkt-imh-field"><label>Direction</label><select data-filter="direction"><option>All</option><option>In</option><option>Out</option></select></div>
              <div class="nkt-imh-field"><label>Movement Type</label><select data-filter="movement_type"><option value="">All Types</option></select></div>
              <div class="nkt-imh-field"><label>Status contains</label><input data-filter="status"></div>
              <div class="nkt-imh-field"><label>Return / Exchange</label><select data-filter="return_exchange"><option value="">All</option><option>Return</option><option>Exchange</option><option>None</option></select></div>
              <div class="nkt-imh-field"><label>Sort</label><select data-filter="sort_order"><option>Newest First</option><option>Oldest First</option></select></div>
              <div class="nkt-imh-filter-actions">
                <button type="button" data-action="clear-filters">Clear Filters</button>
                <button type="button" data-action="load" class="primary">Load History</button>
              </div>
            </div>
            <div class="nkt-imh-warning" data-role="warning" hidden></div>
            <div class="nkt-imh-summary" data-role="summary">
              <span>Rows <b data-summary="rows">0</b></span>
              <span>Total In <b data-summary="in">0</b></span>
              <span>Total Out <b data-summary="out">0</b></span>
              <span>Net Movement <b data-summary="net">0</b></span>
              <span>Signed Amount <b data-summary="amount">0.00</b></span>
              <span class="nkt-imh-status" data-role="status">Ready</span>
            </div>
            <div class="nkt-imh-table-wrap">
              <table class="nkt-imh-table">
                <thead><tr>
                  <th>Encoded At</th><th>Movement</th><th>Customer</th><th class="num">Qty</th>
                  <th class="num">Selling Rate</th><th class="num">Amount</th>
                  <th>Status</th><th>Return / Exchange</th><th>Encoded By</th>
                </tr></thead>
                <tbody data-role="rows"><tr class="empty"><td colspan="9">Select an Item and Warehouse, then load history.</td></tr></tbody>
              </table>
            </div>
            <div class="nkt-imh-footer">
              <span data-role="range">0 rows loaded</span>
              <button type="button" data-action="load-more" hidden>Load More</button>
            </div>
          </div>`;
    }

    function installStyle() {
        if (document.getElementById("nkt-item-movement-history-ui5-style")) return;
        const style = document.createElement("style");
        style.id = "nkt-item-movement-history-ui5-style";
        style.textContent = `
          body.nkt-imh-open{overflow:hidden!important}
          .nkt-imh-overlay{position:fixed;inset:0;z-index:1100;background:#e9e9e9;padding:8px;font-family:Tahoma,Arial,sans-serif;color:#171717}
          .nkt-imh-workspace{height:100%;border:1px solid #777;background:#fff;display:flex;flex-direction:column;box-shadow:0 3px 18px rgba(0,0,0,.28)}
          .nkt-imh-titlebar{min-height:44px;padding:6px 10px;background:linear-gradient(#f9f9f9,#d6d6d6);border-bottom:1px solid #777;display:flex;align-items:center;justify-content:space-between;gap:12px}
          .nkt-imh-titlebar strong{font-size:17px;letter-spacing:.2px}.nkt-imh-titlebar span{font-size:11px;margin-left:12px;color:#555}
          .nkt-imh-title-actions{display:flex;gap:6px}.nkt-imh-workspace button{border:1px solid #777;border-radius:1px;background:linear-gradient(#fff,#ddd);min-height:28px;padding:3px 11px;font-weight:600}.nkt-imh-workspace button.primary{background:linear-gradient(#dcecff,#a9cef8);border-color:#467ba7}
          .nkt-imh-scopebar{display:flex;gap:24px;align-items:center;min-height:30px;padding:4px 10px;border-bottom:1px solid #aaa;background:#f4f4f4;font-size:11px}.nkt-imh-scopebar span:last-child{margin-left:auto;color:#555}
          .nkt-imh-filters{display:grid;grid-template-columns:1.35fr 1.15fr 1.35fr repeat(3,minmax(120px,.8fr));gap:5px 8px;padding:7px 9px;border-bottom:1px solid #999;background:#efefef;align-items:end}
          .nkt-imh-field{min-width:0}.nkt-imh-field label,.nkt-imh-field .control-label{display:block;margin:0 0 2px;font-size:10px;font-weight:700;color:#333}.nkt-imh-field input,.nkt-imh-field select,.nkt-imh-field .form-control{width:100%;height:26px!important;min-height:26px!important;padding:2px 5px!important;border:1px solid #888!important;border-radius:1px!important;background:#fff!important;font-size:11px!important}.nkt-imh-field .form-group{margin:0!important}.nkt-imh-field .help-box{display:none!important}
          .nkt-imh-filter-actions{display:flex;gap:5px;justify-content:flex-end;grid-column:span 2}
          .nkt-imh-warning{padding:5px 10px;border-bottom:1px solid #b88500;background:#fff1b9;color:#5d4500;font-size:11px;font-weight:600}
          .nkt-imh-summary{min-height:31px;padding:4px 9px;border-bottom:1px solid #888;display:flex;align-items:center;gap:18px;background:#f8f8f8;font-size:11px}.nkt-imh-summary b{font-size:12px}.nkt-imh-status{margin-left:auto;font-weight:700;color:#335b7b}
          .nkt-imh-table-wrap{flex:1 1 auto;overflow:auto;background:#fff}.nkt-imh-table{width:100%;border-collapse:separate;border-spacing:0;table-layout:fixed;font-size:10px}.nkt-imh-table thead{position:sticky;top:0;z-index:2}.nkt-imh-table th{background:linear-gradient(#f9f9f9,#d2d2d2);border-right:1px solid #999;border-bottom:1px solid #777;padding:4px 5px;text-align:left;white-space:normal;line-height:1.15}.nkt-imh-table td{border-right:1px solid #ddd;border-bottom:1px solid #ddd;padding:3px 5px;vertical-align:top;line-height:1.2;word-break:break-word}.nkt-imh-table tr:nth-child(even) td{background:#fafafa}.nkt-imh-table th:nth-child(1){width:128px}.nkt-imh-table th:nth-child(2){width:120px}.nkt-imh-table th:nth-child(3){width:190px}.nkt-imh-table th:nth-child(4){width:120px}.nkt-imh-table th:nth-child(5),.nkt-imh-table th:nth-child(6){width:96px}.nkt-imh-table th:nth-child(7){width:118px}.nkt-imh-table th:nth-child(8){width:100px}.nkt-imh-table th:nth-child(9){width:104px}.nkt-imh-table .num{text-align:right}.nkt-imh-table .qty-in{color:#086b24;font-weight:700}.nkt-imh-table .qty-out{color:#991b1b;font-weight:700}.nkt-imh-table .qty-pending{color:#725600;font-weight:700}.nkt-imh-table .empty td{text-align:center;padding:30px;color:#666}
          .nkt-imh-footer{min-height:35px;padding:4px 9px;border-top:1px solid #777;background:#eee;display:flex;align-items:center;justify-content:space-between;font-size:11px}
          @media(max-width:1450px){.nkt-imh-filters{grid-template-columns:repeat(4,minmax(150px,1fr))}.nkt-imh-filter-actions{grid-column:span 1}}
          @media print{body.nkt-imh-open *{display:none!important}body.nkt-imh-open:before{display:block!important;content:'Use the authorized Print button inside Item Movement History.';font:16pt Arial;padding:1in}}
        `;
        document.head.appendChild(style);
    }

    function rowMarkup(row) {
        const effect = number(row.stock_effect_qty);
        const qtyClass = effect > 0.000001 ? "qty-in" : effect < -0.000001 ? "qty-out" : "qty-pending";
        return `<tr>
          <td>${esc(row.encoded_at || "")}</td>
          <td>${esc(row.movement_type || "")}</td>
          <td>${esc(row.customer_name || row.customer || "")}</td>
          <td class="num ${qtyClass}">${esc(formatQty(row))}</td>
          <td class="num">${esc(money(row.selling_rate))}</td>
          <td class="num">${esc(money(row.amount))}</td>
          <td>${esc(row.status || "")}</td>
          <td>${esc(row.return_exchange || "")}</td>
          <td title="${escAttr(row.encoded_by_full_name || row.encoded_by || "")}">${esc(row.encoded_by || "")}</td>
        </tr>`;
    }

    function setSummary(state, summary = {}) {
        const root = state.overlay;
        root.find('[data-summary="rows"]').text(compactNumber(summary.row_count || 0));
        root.find('[data-summary="in"]').text(compactNumber(summary.total_in || 0));
        root.find('[data-summary="out"]').text(compactNumber(summary.total_out || 0));
        root.find('[data-summary="net"]').text(compactNumber(summary.net_movement || 0));
        root.find('[data-summary="amount"]').text(money(summary.signed_amount, false));
    }

    function collectFilters(state, start = 0) {
        const root = state.overlay;
        return {
            item_code: String(state.itemControl.get_value() || "").trim(),
            warehouse: String(root.find('[data-filter="warehouse"]').val() || "").trim(),
            customer: String(state.customerControl.get_value() || "").trim(),
            from_encoded_at: String(root.find('[data-filter="from_encoded_at"]').val() || "").replace("T", " "),
            to_encoded_at: String(root.find('[data-filter="to_encoded_at"]').val() || "").replace("T", " "),
            exact_quantity: String(root.find('[data-filter="exact_quantity"]').val() || "").trim(),
            direction: root.find('[data-filter="direction"]').val() || "All",
            movement_type: root.find('[data-filter="movement_type"]').val() || "",
            status: String(root.find('[data-filter="status"]').val() || "").trim(),
            return_exchange: root.find('[data-filter="return_exchange"]').val() || "",
            sort_order: root.find('[data-filter="sort_order"]').val() || "Newest First",
            page_start: start,
            page_length: PAGE_LENGTH
        };
    }

    function filterSignature(filters) {
        const copy = {...filters}; delete copy.page_start; delete copy.page_length;
        return JSON.stringify(copy);
    }

    async function loadHistory(state, reset = true) {
        if (state.loading) return;
        const start = reset ? 0 : state.rows.length;
        const filters = collectFilters(state, start);
        if (!filters.item_code) {
            frappe.show_alert({message: __("Select an Item."), indicator: "orange"});
            state.itemControl.set_focus();
            return;
        }
        if (!filters.warehouse) {
            frappe.show_alert({message: __("Select one Warehouse."), indicator: "orange"});
            state.overlay.find('[data-filter="warehouse"]').trigger("focus");
            return;
        }
        state.loading = true;
        state.overlay.find('[data-action="load"],[data-action="load-more"]').prop("disabled", true);
        state.overlay.find('[data-role="status"]').text("Loading…");
        try {
            const data = await call("get_item_movement_history", {filters});
            if (reset) state.rows = [];
            state.rows.push(...(data.rows || []));
            state.lastData = data;
            state.lastFilterSignature = filterSignature(filters);
            const tbody = state.overlay.find('[data-role="rows"]');
            if (!state.rows.length) tbody.html('<tr class="empty"><td colspan="9">No matching Item Movement History was found.</td></tr>');
            else tbody.html(state.rows.map(rowMarkup).join(""));
            setSummary(state, data.summary || {});
            state.overlay.find('[data-role="scope-item"]').text(data.item?.item_name || data.item?.name || filters.item_code);
            state.overlay.find('[data-role="scope-warehouse"]').text(data.warehouse?.label || data.warehouse?.name || filters.warehouse);
            state.overlay.find('[data-role="range"]').text(`${compactNumber(state.rows.length)} of ${compactNumber(data.summary?.row_count || 0)} rows loaded`);
            state.overlay.find('[data-action="load-more"]').prop("hidden", !data.has_more);
            state.overlay.find('[data-role="warning"]').prop("hidden", !data.warning).text(data.warning || "");
            state.overlay.find('[data-role="status"]').text(data.truncated ? "Narrow filters before printing" : "Loaded");
        } catch (_error) {
            state.overlay.find('[data-role="status"]').text("Load failed — filters preserved");
        } finally {
            state.loading = false;
            state.overlay.find('[data-action="load"],[data-action="load-more"]').prop("disabled", false);
        }
    }

    function clearFilters(state) {
        const root = state.overlay;
        state.customerControl.set_value("");
        root.find('[data-filter="from_encoded_at"],[data-filter="to_encoded_at"],[data-filter="exact_quantity"],[data-filter="status"]').val("");
        root.find('[data-filter="direction"]').val("All");
        root.find('[data-filter="movement_type"],[data-filter="return_exchange"]').val("");
        root.find('[data-filter="sort_order"]').val("Newest First");
    }

    function printRowCells(row) {
        const values = {
            encoded_at: row.encoded_at || "",
            movement_type: row.movement_type || "",
            customer_name: row.customer_name || row.customer || "",
            quantity: formatQty(row),
            selling_rate: money(row.selling_rate),
            amount: money(row.amount),
            status: row.status || "",
            return_exchange: row.return_exchange || "",
            encoded_by: row.encoded_by || ""
        };
        return PRINT_COLUMNS.map(([key]) => `<td class="${["quantity", "selling_rate", "amount"].includes(key) ? "num" : ""}">${esc(values[key])}</td>`).join("");
    }

    function filterText(filters) {
        const entries = [
            ["Customer", filters.customer], ["From", filters.from_encoded_at], ["To", filters.to_encoded_at],
            ["Exact Qty", filters.exact_quantity], ["Direction", filters.direction !== "All" ? filters.direction : ""],
            ["Movement", filters.movement_type], ["Status", filters.status],
            ["Return/Exchange", filters.return_exchange], ["Sort", filters.sort_order]
        ].filter(([, value]) => value !== null && value !== undefined && String(value).trim() !== "");
        return entries.map(([label, value]) => `<span><b>${esc(label)}:</b> ${esc(value)}</span>`).join(" ");
    }

    function buildPrintHtml(payload, options = {}) {
        const paperSize = options.paperSize || payload.paper_size || "long";
        const density = options.density || payload.density || "5";
        const pageCss = paperSize === "short" ? "letter portrait" : (paperSize === "a4" ? "A4 portrait" : "8.5in 13in portrait");
        const fontPt = density === "4" ? 4 : 5;
        const rows = payload.rows || [];
        const summary = payload.summary || {};
        const itemLabel = payload.item?.item_name || payload.item?.name || "";
        const warehouseLabel = payload.warehouse?.label || payload.warehouse?.name || "";
        const columns = PRINT_COLUMNS.map(([, label]) => `<th>${esc(label)}</th>`).join("");
        const body = rows.map((row) => `<tr>${printRowCells(row)}</tr>`).join("") || '<tr><td colspan="9">No matching rows.</td></tr>';
        return `<!doctype html><html><head><meta charset="utf-8"><title>Item Movement History - ${esc(itemLabel)}</title><style>
          @page{size:${pageCss};margin:.18in .18in .22in}
          *{box-sizing:border-box}html,body{margin:0;padding:0;font-family:Arial,sans-serif;color:#000;font-size:${fontPt}pt;line-height:1.08}
          .toolbar{position:sticky;top:0;background:#333;color:#fff;padding:8px;display:flex;gap:8px;align-items:center;font:12px Arial;z-index:5}.toolbar button{font:12px Arial;padding:5px 12px}.toolbar span{margin-left:auto}
          .report{padding:0}.header{border-bottom:.7pt solid #000;margin-bottom:3pt;padding-bottom:2pt}.title{font-size:${fontPt + 3}pt;font-weight:700;text-align:center}.scope{display:flex;justify-content:space-between;font-size:${fontPt + 1}pt;font-weight:700;margin-top:2pt}.filters{font-size:${fontPt}pt;margin-top:2pt;display:flex;gap:5pt;flex-wrap:wrap}
          .summary{display:flex;gap:8pt;margin:2pt 0;font-weight:700;border-bottom:.5pt solid #000;padding-bottom:2pt}
          table{width:100%;border-collapse:collapse;table-layout:fixed}thead{display:table-header-group}tfoot{display:table-footer-group}tr{page-break-inside:avoid}th,td{border:.35pt solid #555;padding:1pt 1.3pt;vertical-align:top;word-break:break-word}th{font-size:${fontPt + .5}pt;text-align:left;background:#eee}.num{text-align:right;white-space:nowrap}
          th:nth-child(1),td:nth-child(1){width:13%}th:nth-child(2),td:nth-child(2){width:12%}th:nth-child(3),td:nth-child(3){width:20%}th:nth-child(4),td:nth-child(4){width:12%}th:nth-child(5),td:nth-child(5){width:9%}th:nth-child(6),td:nth-child(6){width:9%}th:nth-child(7),td:nth-child(7){width:10%}th:nth-child(8),td:nth-child(8){width:7%}th:nth-child(9),td:nth-child(9){width:8%}
          .footer{margin-top:3pt;display:flex;justify-content:space-between;font-size:${fontPt}pt}.page-number:after{content:counter(page)}
          @media print{.toolbar{display:none!important}.report{padding:0}}
        </style></head><body>
          <div class="toolbar"><button id="print-now">Print Now</button><button id="close-preview">Close Preview</button><span>${esc(payload.estimated_pages || estimatePages(rows.length, paperSize, density))} estimated page(s) • ${fontPt} pt • ${paperSize === "short" ? "Short Bond" : (paperSize === "a4" ? "A4" : "Long Bond")} portrait</span></div>
          <main class="report"><header class="header"><div class="title">NKT ITEM MOVEMENT HISTORY</div><div class="scope"><span>Item: ${esc(itemLabel)}</span><span>Warehouse: ${esc(warehouseLabel)}</span></div><div class="filters">${filterText(payload.filters || {})}</div></header>
          <div class="summary"><span>Rows ${esc(summary.row_count || 0)}</span><span>Total In ${esc(compactNumber(summary.total_in || 0))}</span><span>Total Out ${esc(compactNumber(summary.total_out || 0))}</span><span>Net ${esc(compactNumber(summary.net_movement || 0))}</span><span>Signed Amount ${esc(money(summary.signed_amount, false))}</span></div>
          <table><thead><tr>${columns}</tr></thead><tbody>${body}</tbody></table>
          <footer class="footer"><span>Prepared ${esc(payload.generated_at || "")} by ${esc(payload.requested_by || "")} • Authorized by ${esc(payload.authorized_by || "")} • Audit ${esc(payload.print_event || "")}</span><span>Report ${esc(payload.report_sha256 || "")}</span></footer></main>
          <script>document.getElementById('print-now').addEventListener('click',function(){this.disabled=true;window.print();setTimeout(()=>{this.disabled=false},1500)});document.getElementById('close-preview').addEventListener('click',()=>window.close());<\/script>
        </body></html>`;
    }

    function bringDialogToFront(dialog, focusTarget = null) {
        const apply = () => {
            dialog.$wrapper.addClass("nkt-imh-dialog-front").css("z-index", "1410");
            dialog.$wrapper.find(".modal-dialog,.modal-content").css("position", "relative");
            $(".modal-backdrop").last().addClass("nkt-imh-dialog-backdrop").css("z-index", "1400");
            if (focusTarget) setTimeout(() => focusTarget.trigger("focus"), 20);
        };
        dialog.$wrapper.off("shown.bs.modal.nktImhFront").on("shown.bs.modal.nktImhFront", apply);
        setTimeout(apply, 0);
    }

    function pinDialog() {
        return new Promise((resolve) => {
            let settled = false;
            const dialog = new frappe.ui.Dialog({
                title: __("Manager Authorization — Item History Print"),
                fields: [
                    {fieldname: "notice", fieldtype: "HTML", options: '<div class="alert alert-warning">A fresh five-digit Manager PIN authorizes this print only. It never unlocks another warehouse.</div>'},
                    {fieldname: "pin", label: __("Manager PIN"), fieldtype: "Password", reqd: 1}
                ],
                primary_action_label: __("Authorize Print"),
                primary_action(values) {
                    const pin = String(values.pin || "");
                    if (!/^\d{5}$/.test(pin)) {
                        frappe.show_alert({message: __("Manager PIN must be exactly five numeric digits."), indicator: "orange"});
                        dialog.get_field("pin").set_focus();
                        return;
                    }
                    settled = true;
                    dialog.hide();
                    resolve(pin);
                }
            });
            dialog.$wrapper.one("hidden.bs.modal.nktImhPin", () => { if (!settled) resolve(null); });
            dialog.show();
            bringDialogToFront(dialog, dialog.get_field("pin").$input);
            const headerClose = dialog.$wrapper.find('.modal-header .btn-modal-close,.modal-header .close');
            headerClose.attr("tabindex", "-1");
            const input = dialog.get_field("pin").$input;
            input.attr({inputmode: "numeric", maxlength: "5", autocomplete: "off"});
            input.on("keydown.nktImhPin", (event) => {
                if (event.key === "Enter") { event.preventDefault(); dialog.get_primary_btn().trigger("click"); }
            });
            setTimeout(() => input.trigger("focus"), 50);
        });
    }

    function printOptionsDialog(state) {
        return new Promise((resolve) => {
            const rowCount = Number(state.lastData?.summary?.row_count || 0);
            let settled = false;
            const dialog = new frappe.ui.Dialog({
                title: __("Print Item Movement History"),
                fields: [
                    {fieldname: "estimate", fieldtype: "HTML", options: `<div class="alert alert-info"><b>${esc(compactNumber(rowCount))} rows</b>. Long Bond portrait at 5 pt is approximately <b>${estimatePages(rowCount, "long", "5")} page(s)</b>.</div>`},
                    {fieldname: "paper_size", label: __("Paper"), fieldtype: "Select", options: "Long Bond 8.5 × 13 Portrait\nShort Bond Letter 8.5 × 11 Portrait\nA4 210 × 297 mm Portrait", default: "Long Bond 8.5 × 13 Portrait", reqd: 1},
                    {fieldname: "density", label: __("Density"), fieldtype: "Select", options: "5 pt Compact\n4 pt Maximum Density", default: "5 pt Compact", reqd: 1},
                    {fieldname: "explanation", fieldtype: "HTML", options: '<div class="text-muted">5 pt is the normal compact setting; 4 pt is maximum density.</div>'}
                ],
                primary_action_label: __("Prepare Authorized Preview"),
                primary_action(values) {
                    settled = true;
                    dialog.hide();
                    resolve({
                        paper_size: String(values.paper_size || "").startsWith("Short") ? "short" : (String(values.paper_size || "").startsWith("A4") ? "a4" : "long"),
                        density: String(values.density || "").startsWith("4") ? "4" : "5"
                    });
                }
            });
            dialog.$wrapper.one("hidden.bs.modal.nktImhPrintOptions", () => { if (!settled) resolve(null); });
            dialog.show();
            bringDialogToFront(dialog, dialog.get_field("paper_size").$input);
            dialog.$wrapper.find('.modal-header .btn-modal-close,.modal-header .close').attr("tabindex", "-1");
        });
    }

    async function preparePrint(state) {
        if (state.loading || state.printing) return;
        const current = collectFilters(state, 0);
        const signature = filterSignature(current);
        if (!state.lastData || state.lastFilterSignature !== signature) {
            await loadHistory(state, true);
            if (!state.lastData || state.lastFilterSignature !== signature) return;
        }
        if (state.lastData.truncated) {
            frappe.msgprint({title: __("Narrow the filters"), message: __(state.lastData.warning || "Narrow the Encoded At filters before printing."), indicator: "orange"});
            return;
        }
        const options = await printOptionsDialog(state);
        if (!options) return;
        let pin = "";
        if (state.bootstrap.manager_pin_required_for_print) {
            pin = await pinDialog();
            if (!pin) return;
        }
        const preview = window.open("", "_blank");
        if (!preview) {
            frappe.msgprint(__("The browser blocked the authorized print preview. Allow pop-ups for this ERP site and try again."));
            return;
        }
        preview.document.write("<p style='font:14px Arial;padding:20px'>Preparing authorized Item Movement History…</p>");
        state.printing = true;
        state.overlay.find('[data-action="print"]').prop("disabled", true);
        try {
            const payload = await call("prepare_item_movement_history_print", {
                filters: current,
                paper_size: options.paper_size,
                density: options.density,
                pin,
                device_id: deviceId()
            });
            preview.document.open();
            preview.document.write(buildPrintHtml(payload, {paperSize: options.paper_size, density: options.density}));
            preview.document.close();
        } catch (_error) {
            preview.close();
        } finally {
            state.printing = false;
            state.overlay.find('[data-action="print"]').prop("disabled", false);
        }
    }

    function suspendFastKeys(state) {
        state.fastCapture = window.__nktFastTransactionCaptureHandler || null;
        if (state.fastCapture) window.removeEventListener("keydown", state.fastCapture, true);
    }

    function restoreFastKeys(state) {
        if (state.fastCapture && window.__nktFastTransactionCaptureHandler === state.fastCapture) {
            window.addEventListener("keydown", state.fastCapture, true);
        }
        state.fastCapture = null;
    }

    function closeWorkspace(state) {
        if (!state || state.closed) return;
        state.closed = true;
        window.removeEventListener("keydown", state.keyHandler, true);
        restoreFastKeys(state);
        state.overlay.remove();
        document.body.classList.remove("nkt-imh-open");
        if (window.__nktItemMovementWorkspace === state) window.__nktItemMovementWorkspace = null;
        setTimeout(() => document.querySelector('.nkt-fast-shell [data-role="item-entry"]')?.focus(), 0);
    }

    function bindWorkspace(state) {
        const root = state.overlay;
        root.on("click", '[data-action="close"]', () => closeWorkspace(state));
        root.on("click", '[data-action="load"]', () => loadHistory(state, true));
        root.on("click", '[data-action="load-more"]', () => loadHistory(state, false));
        root.on("click", '[data-action="clear-filters"]', () => clearFilters(state));
        root.on("click", '[data-action="print"]', () => preparePrint(state));
        root.on("keydown", "input,select", (event) => {
            if (event.key === "Enter" && !$(event.currentTarget).closest('.awesomplete').length) {
                event.preventDefault();
                loadHistory(state, true);
            }
        });
        state.keyHandler = (event) => {
            if (state.closed) return;
            if ((event.ctrlKey || event.metaKey) && String(event.key).toLowerCase() === "p") {
                event.preventDefault(); event.stopImmediatePropagation();
                frappe.show_alert({message: __("Use the authorized Print button."), indicator: "orange"});
                return;
            }
            if (["F2", "F3", "F6", "F7", "F10", "F11", "F12"].includes(event.key)) {
                event.preventDefault(); event.stopImmediatePropagation();
                return;
            }
            if (event.key === "Escape") {
                if (document.querySelector('.modal.show')) return;
                event.preventDefault(); event.stopImmediatePropagation(); closeWorkspace(state);
            }
        };
        window.addEventListener("keydown", state.keyHandler, true);
    }

    async function openItemMovementHistory(frm) {
        if (window.__nktItemMovementWorkspace && !window.__nktItemMovementWorkspace.closed) {
            window.__nktItemMovementWorkspace.overlay.find('[data-action="load"]').trigger("focus");
            return;
        }
        installStyle();
        const guessedItem = guessItem(frm);
        const overlay = $('<div class="nkt-imh-overlay"></div>').html(baseMarkup()).appendTo(document.body);
        document.body.classList.add("nkt-imh-open");
        const state = {
            frm, overlay, bootstrap: null, itemControl: null, customerControl: null,
            rows: [], lastData: null, lastFilterSignature: "", loading: false, printing: false,
            keyHandler: null, fastCapture: null, closed: false
        };
        window.__nktItemMovementWorkspace = state;
        suspendFastKeys(state);
        bindWorkspace(state);
        overlay.find('[data-role="status"]').text("Loading access…");
        try {
            state.bootstrap = await call("get_item_movement_history_bootstrap", {item_code: guessedItem});
            state.itemControl = makeLinkControl(overlay.find('[data-control="item"]'), "item_code", __("Item"), "Item", guessedItem);
            state.customerControl = makeLinkControl(overlay.find('[data-control="customer"]'), "customer", __("Customer"), "Customer", "");
            const warehouse = overlay.find('[data-filter="warehouse"]');
            warehouse.html((state.bootstrap.warehouses || []).map((row) => `<option value="${escAttr(row.name)}">${esc(row.label || row.name)}</option>`).join(""));
            warehouse.val(state.bootstrap.default_warehouse || "");
            overlay.find('[data-filter="movement_type"]').append((state.bootstrap.movement_types || []).map((type) => `<option value="${escAttr(type)}">${esc(type)}</option>`).join(""));
            overlay.find('[data-role="status"]').text("Ready");
            if (guessedItem && warehouse.val()) await loadHistory(state, true);
            else state.itemControl.set_focus();
        } catch (_error) {
            overlay.find('[data-role="status"]').text("Unavailable");
            closeWorkspace(state);
        }
    }

    let queued = false;
    function schedule() {
        if (queued) return;
        queued = true;
        requestAnimationFrame(() => { queued = false; scrub(document); });
    }

    frappe.ui.form.on(DOCTYPE, {
        refresh(frm) {
            schedule(); setTimeout(schedule, 0); setTimeout(schedule, 80);
            if (!frm.__nktUI5ItemMovementHistoryButton) {
                frm.__nktUI5ItemMovementHistoryButton = true;
                frm.add_custom_button(__("Item Movement History (F6)"), () => openItemMovementHistory(frm), __("History"));
            }
        }
    });

    if (window.__nktR8AEncoderF6Handler) {
        document.removeEventListener("keydown", window.__nktR8AEncoderF6Handler, true);
    }
    window.__nktR8AEncoderF6Handler = (event) => {
        if (event.key !== "F6") return;
        if (!window.cur_frm || cur_frm.doctype !== DOCTYPE) return;
        if (document.querySelector('.modal.show')) return;
        event.preventDefault(); event.stopImmediatePropagation();
        openItemMovementHistory(cur_frm);
    };
    document.addEventListener("keydown", window.__nktR8AEncoderF6Handler, true);

    if (!window.__nktR8AEncoderObserver && document.body) {
        window.__nktR8AEncoderObserver = new MutationObserver(schedule);
        window.__nktR8AEncoderObserver.observe(document.body, {childList: true, subtree: true});
    }

    window.__nktItemMovementHistoryTest = {version: "UI5B", formatQty, estimatePages, buildPrintHtml, printColumns: PRINT_COLUMNS, bringDialogToFront};
})();

/* ===== END SOURCE: NKT R8A Encoder Frontline Presentation + F6 Item History ===== */

/* ===== SOURCE: NKT R4 UI6 Encoder F8 Transaction History ===== */
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

/* ===== END SOURCE: NKT R4 UI6 Encoder F8 Transaction History ===== */

/* ===== SOURCE: NKT R4 UI6 Reserved F1 F7 - NKT Encoder Fast Screen ===== */
// NKT R4 UI6 - reserve F1 and F7 on this NKT Fast Screen.
(() => {
    const DOCTYPE = "NKT Encoder Fast Screen";
    const SLOT = "__nktReservedF1F7_a73c7d779d43b39f";
    frappe.ui.form.on(DOCTYPE, {
        refresh() {
            install();
        }
    });
    function keyIs(event, number) {
        const key = String(event?.key || "").toUpperCase();
        const code = String(event?.code || "").toUpperCase();
        const keyCode = Number(event?.which || event?.keyCode || 0);
        return key === `F${number}` || code === `F${number}` || keyCode === 111 + number;
    }
    function install() {
        if (window[SLOT]) window.removeEventListener("keydown", window[SLOT], true);
        const handler = (event) => {
            if (!window.cur_frm || cur_frm.doctype !== DOCTYPE) return;
            if (!(keyIs(event, 1) || keyIs(event, 7))) return;
            event.preventDefault?.();
            event.stopPropagation?.();
            event.stopImmediatePropagation?.();
            event.__nktReservedFunctionKey = true;
        };
        window[SLOT] = handler;
        window.addEventListener("keydown", handler, true);
    }
})();

/* ===== END SOURCE: NKT R4 UI6 Reserved F1 F7 - NKT Encoder Fast Screen ===== */

/* ===== SOURCE: NKT R4 UI7B Encoder Payment on Account Bridge ===== */
// NKT R4 UI7B5 - active Cashier no-item Payment-on-Account bridge recovery.
// Preserves UI7B3 trusted shift context, UI7B4 advance invariant, F8/F4 history refinements.
// Critical recovery: launch no longer depends solely on transient __nktActiveFastScreenState.
(() => {
  if (window.__nktUI7BAccountHistoryBridgeLoaded) return;
  window.__nktUI7BAccountHistoryBridgeLoaded = true;
  const CASHIER='NKT Cashier Fast Screen', ENCODER='NKT Encoder Fast Screen';
  const FAST='nkt_operations.nkt_store_operations.fast_screen_backend';
  const ROUTED='nkt_operations.nkt_store_operations.features.offline_edge.internal.routed_reads';
  const TX='nkt_operations.nkt_store_operations.transaction_history';
  const ACCOUNT_LAYER='nkt-ui7b-account-layer', CUSTOMER_LAYER='nkt-ui7b-customer-history-layer';
  const CSS_ID='nkt-ui7b-account-history-style';
  [CASHIER,ENCODER].forEach(dt=>frappe.ui.form.on(dt,{refresh(frm){install(frm);}}));

  function mode(frm){return frm?.doctype===CASHIER?'cashier':'encoder'}
  function esc(v){return $('<div>').text(v==null?'':String(v)).html()}
  function money(v){const n=Number(v||0);return `₱${(Number.isFinite(n)?n:0).toLocaleString(undefined,{minimumFractionDigits:2,maximumFractionDigits:2})}`}
  const publicPaymentId=v=>{const s=String(v||'').trim(),m=s.match(/^NKT-PAY-(\d+)$/);return m?`P${String(Number(m[1])).padStart(6,'0')}`:s};
  function id(){return window.crypto?.randomUUID?.() || `acct-${Date.now()}-${Math.random().toString(36).slice(2)}`}
  function current(frm){const s=window.__nktActiveFastScreenState;return s&&s.frm&&s.frm.doctype===frm.doctype?s:null}
  function active(frm){const w=frm?.wrapper?.jquery?frm.wrapper[0]:frm?.wrapper;return Boolean(window.cur_frm?.doctype===frm?.doctype&&w?.isConnected&&$(w).is(':visible')&&$(w).find('.nkt-fast-shell').length)}
  function noItems(s){
    const rows=Array.isArray(s?.rows)?s.rows:[];
    return !rows.some(r=>String(r?.item||r?.item_code||r?.item_name||r?.item_id||'').trim());
  }
  function launchDecision(raw={}){
    const stateCustomer=String(raw.stateCustomer||'').trim();
    const domCustomer=String(raw.domCustomer||'').trim();
    const domSelected=Boolean(raw.domSelected);
    // The visible selected Customer wins when the Fast Screen explicitly marks it Selected;
    // otherwise use the live state object when available. Server eligibility is still authoritative.
    const customer=(domSelected&&domCustomer)?domCustomer:stateCustomer;
    // If either the state or rendered grid says merchandise exists, never divert to Account Payment.
    const hasItems=Boolean(raw.stateHasItems||raw.domHasItems);
    return {customer,noItems:!hasItems,eligible:Boolean(customer&&!hasItems)};
  }
  function domAccountLaunchFacts(frm){
    const w=$(frm.wrapper);
    const customerInput=String(w.find('[data-role="customer-entry"]').first().val()||'').trim();
    const selectedStatus=String(w.find('[data-role="customer-status"]').first().text()||'').trim();
    const selectedCard=String(w.find('[data-role="customer-selected"] .nkt-customer-name').first().text()||'').trim();
    const domCustomer=customerInput||selectedCard;
    const domSelected=/^selected$/i.test(selectedStatus)||Boolean(selectedCard&&domCustomer&&selectedCard===domCustomer);
    let domHasItems=false;
    w.find('.nkt-grid tbody tr').each(function(){
      const tr=$(this);
      if(tr.hasClass('nkt-empty'))return;
      const txt=String(tr.text()||'').replace(/\s+/g,' ').trim();
      const operationalControls=tr.find('.nkt-remove,[data-row],[data-field="item"],[data-field="item_code"],input,select').length;
      if(operationalControls|| (txt&&!/^(press f3|type part of an item)/i.test(txt)))domHasItems=true;
    });
    return {domCustomer,domSelected,domHasItems};
  }
  function accountLaunchContext(frm){
    const s=current(frm);
    const dom=domAccountLaunchFacts(frm);
    const stateCustomer=String(s?.customer?.name||s?.customer?.customer||'').trim();
    const stateHasItems=s?(!noItems(s)):false;
    return {...launchDecision({stateCustomer,stateHasItems,...dom}),state:s};
  }
  if(window.__NKT_UI7B5_TEST_MODE__)window.__nktUI7B5TestHooks={launchDecision};
  function key(e,n){return String(e?.key||'').toUpperCase()===`F${n}`||String(e?.code||'').toUpperCase()===`F${n}`||Number(e?.which||e?.keyCode||0)===111+n}
  function consume(e){e?.preventDefault?.();e?.stopPropagation?.();e?.stopImmediatePropagation?.()}
  function call(method,args={}){return new Promise((resolve,reject)=>frappe.call({method:`${method.includes('.')?method:FAST+'.'+method}`,args,freeze:false,callback:r=>resolve(r.message||{}),error:r=>reject(r)}))}
  function errorText(e){
    const clean=v=>String(v==null?'':v).replace(/<[^>]+>/g,' ').replace(/\s+/g,' ').trim();
    const messages=[];
    const add=v=>{const t=clean(v);if(t&&!messages.includes(t))messages.push(t)};
    try{
      const raw=e?._server_messages;
      const outer=typeof raw==='string'?JSON.parse(raw):raw;
      if(Array.isArray(outer))outer.forEach(entry=>{
        if(typeof entry==='string'){
          try{const parsed=JSON.parse(entry);add(parsed?.message||entry)}catch(_){add(entry)}
        }else add(entry?.message||entry)
      });
    }catch(_){/* fall through to ordinary Frappe error fields */}
    if(!messages.length)add(e?.message||e?.exc_message);
    return messages[0]||'Request failed';
  }
  function installStyle(){if(document.getElementById(CSS_ID))return;const s=document.createElement('style');s.id=CSS_ID;s.textContent=`
    .nkt-ui7b-cashier-privacy .nkt-customer-balance-line,.nkt-ui7b-cashier-privacy [data-action="customer-history"]{display:none!important}
    .nkt-ui7b-cashier-privacy [data-role="customer-results"] .nkt-result small span:nth-child(n+2){display:none!important}
    .nkt-ui7b-layer{position:fixed;inset:0;z-index:1075;background:rgba(25,31,38,.28);display:flex;align-items:center;justify-content:center;font-family:Arial,sans-serif}
    .nkt-ui7b-panel{width:min(1120px,96vw);max-height:94vh;overflow:auto;background:#f7f8fa;border:1px solid #596674;box-shadow:0 8px 34px rgba(0,0,0,.28)}
    .nkt-ui7b-title{display:flex;justify-content:space-between;gap:12px;align-items:center;background:#445b73;color:#fff;padding:7px 10px;font-size:13px}.nkt-ui7b-title span{font-size:10px;opacity:.9}
    .nkt-ui7b-body{padding:8px}.nkt-ui7b-customer{background:#e9edf2;border:1px solid #a6b0ba;padding:5px 7px;margin-bottom:7px;font-size:12px}
    .nkt-ui7b-summary{display:grid;grid-template-columns:repeat(4,1fr);gap:5px;margin:5px 0}.nkt-ui7b-summary div{border:1px solid #9fa9b3;background:#fff;padding:5px}.nkt-ui7b-summary span{display:block;font-size:9px;color:#52606c;text-transform:uppercase}.nkt-ui7b-summary b{font-size:14px}
    .nkt-ui7b-principal{display:grid;grid-template-columns:240px 1fr;gap:8px;align-items:end;margin-bottom:7px}.nkt-ui7b-principal label{font-size:11px;font-weight:bold}.nkt-ui7b-principal input{width:100%;height:30px;font-size:15px;font-weight:bold}.nkt-ui7b-help{font-size:10px;color:#4d5a66;padding-bottom:4px}
    .nkt-ui7b-pay-table{width:100%;border-collapse:collapse;background:#fff}.nkt-ui7b-pay-table th,.nkt-ui7b-pay-table td{border:1px solid #aab3bc;padding:3px;font-size:10px}.nkt-ui7b-pay-table th{background:#dce3ea;text-align:left}.nkt-ui7b-pay-table input,.nkt-ui7b-pay-table select{width:100%;min-width:0;height:26px;font-size:11px}.nkt-ui7b-pay-table button{height:24px}
    .nkt-ui7b-actions{display:flex;justify-content:flex-end;gap:6px;margin-top:7px}.nkt-ui7b-actions button{min-width:110px;padding:6px 10px;border:1px solid #73808c;background:#fff}.nkt-ui7b-actions .primary{background:#263746;color:#fff;border-color:#263746;font-weight:bold}
    .nkt-ui7b-confirm{max-width:520px}.nkt-ui7b-confirm .nkt-ui7b-body{font-size:12px}.nkt-ui7b-confirm-row{display:flex;justify-content:space-between;border-bottom:1px solid #c5ccd2;padding:5px 0}.nkt-ui7b-confirm-row b{font-size:14px}
    .nkt-ui7b-history{width:min(1500px,98vw);height:94vh;display:flex;flex-direction:column;overflow:hidden}.nkt-ui7b-history .nkt-ui7b-body{display:flex;flex-direction:column;min-height:0;flex:1}.nkt-ui7b-history-filters{display:grid;grid-template-columns:2fr 1fr 1fr 1fr 1fr auto;gap:6px;margin-bottom:6px}.nkt-ui7b-history-filters label{font-size:10px}.nkt-ui7b-history-filters input{width:100%;height:27px}.nkt-ui7b-history-table-wrap{min-height:0;overflow:auto;flex:1;border:1px solid #98a4af;background:#fff}.nkt-ui7b-history table{width:100%;border-collapse:collapse;table-layout:fixed}.nkt-ui7b-history th,.nkt-ui7b-history td{border-bottom:1px solid #d3d8dd;padding:3px 5px;font-size:10px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.nkt-ui7b-history th{position:sticky;top:0;background:#445b73;color:#fff;z-index:2;text-align:left}.nkt-ui7b-history td.num{text-align:right}.nkt-ui7b-history tr:nth-child(even){background:#f5f7f9}
    .nkt-ui7b-print-layer{position:fixed;inset:0;z-index:1095;background:rgba(0,0,0,.2);display:flex;align-items:center;justify-content:center}.nkt-ui7b-print-panel{width:390px;background:#fff;border:1px solid #66727e;padding:10px;box-shadow:0 8px 26px rgba(0,0,0,.25)}.nkt-ui7b-print-panel label{display:flex;flex-direction:column;gap:3px;margin:6px 0;font-size:11px}.nkt-ui7b-print-panel .check{flex-direction:row;align-items:center}.nkt-ui7b-print-panel select{height:29px}
  `;document.head.appendChild(s)}

  function install(frm){installStyle();const wrapper=frm?.wrapper?.jquery?frm.wrapper[0]:frm?.wrapper;if(!wrapper)return;if(frm.doctype===CASHIER){$(wrapper).addClass('nkt-ui7b-cashier-privacy');setTimeout(()=>{$(wrapper).find('[data-action="customer-history"]').hide();const note=$(wrapper).find('.nkt-shortcut-note').first();if(note.length)note.text(note.text().replace(/\s*•?\s*F4\s+(Customer\s+)?History/ig,''));},100)}
    const keyName=`__nktUI7BKey_${frm.doctype.replace(/\W/g,'_')}`;if(window[keyName])window.removeEventListener('keydown',window[keyName],true);const kh=e=>{if(!active(frm)||document.getElementById(ACCOUNT_LAYER)||document.getElementById(CUSTOMER_LAYER))return;if(frm.doctype===CASHIER&&key(e,4)){consume(e);return}if(!(key(e,10)||key(e,11)||key(e,12)))return;
      const launch=accountLaunchContext(frm);
      if(!launch.eligible)return;
      // Capture BEFORE the ordinary sale handler. Server decides whether the selected Customer
      // is actually Account-enabled; Cashier still receives no balance/history privilege.
      consume(e);if(e.repeat)return;openAccount(frm,key(e,10),launch);};window[keyName]=kh;window.addEventListener('keydown',kh,true);
    const clickName=`__nktUI7BClick_${frm.doctype.replace(/\W/g,'_')}`;if(window[clickName])wrapper.removeEventListener('click',window[clickName],true);const ch=e=>{if(!active(frm)||document.getElementById(ACCOUNT_LAYER))return;const b=e.target?.closest?.('[data-action="f10"],[data-action="f11"],[data-action="f12"]');if(!b||!wrapper.contains(b))return;const launch=accountLaunchContext(frm);if(!launch.eligible)return;
      consume(e);openAccount(frm,String(b.getAttribute('data-action'))==='f10',launch);};window[clickName]=ch;wrapper.addEventListener('click',ch,true);
    window.__nktOpenEnhancedCustomerHistory=(targetFrm)=>openCustomerHistory(targetFrm||frm);
  }

  function accountCashIndex(st){return st.rows.findIndex(r=>r.method==='Cash')}
  function accountRound(v){return Math.round(Number(v||0)*100)/100}
  function rebalanceAccountCash(st){
    const ci=accountCashIndex(st); if(ci<0)return;
    const nonCash=accountRound(st.rows.reduce((a,r,i)=>a+(i===ci?0:Number(r.amount||0)),0));
    st.rows[ci].amount=accountRound(Math.max(Number(st.principal||0)-nonCash,0));
    if(st.mode==='cashier'&&!st.cashTenderedManual)st.cashTendered=st.rows[ci].amount;
  }
  function accountTotals(st){
    rebalanceAccountCash(st);
    const principal=accountRound(st.principal);
    const settled=accountRound(st.rows.reduce((a,r)=>a+Number(r.amount||0),0));
    const surcharge=accountRound(st.rows.reduce((a,r)=>a+(r.method==='Card'?Number(r.amount||0)*.02:0),0));
    const ci=accountCashIndex(st);
    const cashDue=ci>=0?accountRound(st.rows[ci].amount):0;
    const tendered=st.mode==='cashier'&&ci>=0?accountRound(st.cashTendered):0;
    const change=st.mode==='cashier'&&ci>=0?accountRound(Math.max(tendered-cashDue,0)):0;
    return{principal,settled,balance:accountRound(principal-settled),surcharge,totalCollected:accountRound(settled+surcharge),cashDue,tendered,change,cashRowPresent:ci>=0}
  }
  function accountSetExactCash(st){
    st.rows=[{method:'Cash',amount:accountRound(st.principal),reference:'',check_date:'',provider:''}];
    st.cashTendered=accountRound(st.principal); st.cashTenderedManual=false;
  }
  function accountAddRow(st){
    const method=st.rows.some(r=>r.method==='Cash')?'Bank Transfer':'Cash';
    st.rows.push({method,amount:0,reference:'',check_date:'',provider:''});
    if(method==='Cash'){st.cashTendered=0;st.cashTenderedManual=false}
    rebalanceAccountCash(st);
  }
  async function openAccount(frm,printIntent,launch=null){
    const resolved=launch||accountLaunchContext(frm);if(!resolved?.eligible||!resolved.customer)return;
    let context;
    try{context=await call('get_fast_account_payment_customer_context',{mode:mode(frm),customer:resolved.customer})}
    catch(e){frappe.msgprint({title:'Payment on Account',message:esc(errorText(e)),indicator:'red'});return}
    if(!context.allow_account_sales){frappe.msgprint('This Customer is not enabled for Account transactions.');return}
    document.getElementById(ACCOUNT_LAYER)?.remove();
    const st={frm,mode:mode(frm),context,requestId:id(),printIntent:Boolean(printIntent),principal:0,plateNumber:'',osNo:'',
      rows:[{method:'Cash',amount:0,reference:'',check_date:'',provider:''}],cashTendered:0,cashTenderedManual:false,busy:false};
    const d=new frappe.ui.Dialog({
      title:__(st.mode==='cashier'?'Take Payment on Account':'Verify Payment on Account'),
      fields:[{fieldtype:'HTML',fieldname:'payment_grid'}]
    });
    st.dialog=d; d.show(); d.$wrapper.attr('id',ACCOUNT_LAYER);
    d.$wrapper.find('.modal-dialog').addClass('nkt-payment-dialog');
    renderAccountDialog(st,d);
    d.$wrapper.on('hidden.bs.modal.nktUI7BAccount',()=>{st.dialog=null;setTimeout(()=>$(frm.wrapper).find('[data-role="item-entry"]').trigger('focus'),0)});
  }
  function accountPaymentRowMarkup(st,r,i,count){
    const cash=r.method==='Cash',check=r.method==='Check';
    const field=cash&&st.mode==='cashier'?'cash_tendered':'amount';
    const value=field==='cash_tendered'?st.cashTendered:r.amount;
    const amountReadonly=cash&&st.mode!=='cashier';
    const refDisabled=cash;
    const refPlaceholder=cash?'Not required':(check?'Check number required':'Required');
    return `<tr>
      <td><select class="nkt-pay-method" data-row="${i}">${['Cash','Check','GCash','Maya','Card','Bank Transfer','Online'].map(m=>`<option value="${m}" ${m===r.method?'selected':''}>${m}</option>`).join('')}</select></td>
      <td><input class="nkt-pay-input" data-row="${i}" data-field="${field}" type="number" min="0" step="0.01" value="${value||''}" ${amountReadonly?'readonly':''} placeholder="${field==='cash_tendered'?'Cash received':''}"></td>
      <td><input class="nkt-pay-input" data-row="${i}" data-field="reference" value="${esc(r.reference||'')}" ${refDisabled?'disabled':''} placeholder="${refPlaceholder}"></td>
      <td><input class="nkt-pay-input" data-row="${i}" data-field="check_date" type="date" value="${esc(r.check_date||'')}" ${check?'':'disabled'}></td>
      <td><input class="nkt-pay-input" data-row="${i}" data-field="provider" value="${esc(r.provider||'')}" ${cash?'disabled':''} placeholder="${check?'Issuing bank':'Bank / provider'}"></td>
      <td><button type="button" data-pay-remove="${i}" tabindex="-1" ${count<=1?'disabled':''}>×</button></td>
    </tr>`;
  }
  function renderAccountDialog(st,d,focusSpec=null){
    rebalanceAccountCash(st);
    const box=d.fields_dict.payment_grid.$wrapper,t=accountTotals(st);
    box.html(`<div class="nkt-payment-grid-shell">
      <div style="display:grid;grid-template-columns:240px 1fr;gap:10px;align-items:end;margin:0 0 8px">
        <label style="margin:0;font-size:11px;font-weight:bold">Account Payment Amount
          <input class="form-control" data-account-principal type="number" min="0" step="0.01" value="${st.principal||''}" placeholder="0.00" style="height:32px;font-size:15px;font-weight:bold">
        </label>
        <div class="nkt-payment-note" style="margin:0">${st.mode==='cashier'
          ?'<b>Payment on Account:</b> no merchandise item is being sold. Cashier records the actual money received; customer account principal changes only after the independent Encoder verification matches.'
          :'<b>Payment Verification:</b> independently encode the payment already received by Cashier. If the exact Cashier payment matches, the approved receivable is applied immediately; otherwise the verification remains waiting and the balance does not change.'}</div>
      </div>
      ${st.mode==='encoder'?`<div style="display:grid;grid-template-columns:minmax(180px,1fr) minmax(180px,1fr);gap:10px;margin:0 0 8px">
        <label style="margin:0;font-size:11px;font-weight:bold">Plate Number <span style="font-weight:normal;color:#777">(Optional)</span>
          <input class="form-control" data-account-plate value="${esc(st.plateNumber||'')}" maxlength="140" placeholder="Plate number" style="height:30px">
        </label>
        <label style="margin:0;font-size:11px;font-weight:bold">OS# <span style="font-weight:normal;color:#777">(Optional physical Order Slip)</span>
          <input class="form-control" data-account-os value="${esc(st.osNo||'')}" maxlength="140" placeholder="Paper Order Slip number" style="height:30px">
        </label>
      </div>`:''}
      <div class="nkt-payment-summary">
        <div class="nkt-summary-box"><span>Account Payment</span><b>${money(t.principal)}</b></div>
        <div class="nkt-summary-box"><span>Settled</span><b data-pay-summary="total">${money(t.settled)}</b></div>
        <div class="nkt-summary-box remaining ${Math.abs(t.balance)<=.005?'ok':'bad'}"><span>Balance</span><b data-pay-summary="balance">${money(t.balance)}</b></div>
        <div class="nkt-summary-box change ${t.change>.005?'has-change':''}"><span>Change</span><b data-pay-summary="change">${money(t.change)}</b></div>
      </div>
      <table class="nkt-pay-table"><thead><tr><th class="p-method">Method</th><th class="p-amount">Amount / Tendered</th><th class="p-ref">Reference / Check No.</th><th class="p-date">Check Date</th><th class="p-provider">Bank / Provider</th><th class="p-remove"></th></tr></thead>
      <tbody>${st.rows.map((r,i)=>accountPaymentRowMarkup(st,r,i,st.rows.length)).join('')}</tbody></table>
      <div class="nkt-pay-actions"><button type="button" data-pay-action="add">Add Payment Row</button>${st.mode==='cashier'?'<button type="button" data-pay-action="exact">Exact Cash</button>':''}<div class="spacer"></div><button type="button" data-pay-action="review" class="btn-primary">Review Payment</button></div>
      <div class="nkt-payment-note"><b>Cash:</b> ${st.mode==='cashier'?'type the physical cash handed over; Change is calculated automatically.':'record only the settled Cash amount; physical Tendered/Change stays on the Cashier side.'}<br><b>Check:</b> record Check Number, Check Date, and Issuing Bank.<br><b>Card:</b> 2% applies only to the Card portion of this old-account payment and is collected in addition to account principal. Maya has no surcharge.</div>
    </div>`);
    box.off('.nktUI7BAccount');
    box.on('input.nktUI7BAccount change.nktUI7BAccount','[data-account-principal]',function(){
      st.principal=Math.max(Number($(this).val()||0),0);
      rebalanceAccountCash(st);
      const tt=accountTotals(st);
      box.find('[data-pay-summary="total"]').text(money(tt.settled));
      box.find('[data-pay-summary="balance"]').text(money(tt.balance)).closest('.remaining').toggleClass('ok',Math.abs(tt.balance)<=.005).toggleClass('bad',Math.abs(tt.balance)>.005);
      box.find('[data-pay-summary="change"]').text(money(tt.change));
      const ci=accountCashIndex(st);
      if(ci>=0&&st.mode!=='cashier')box.find(`.nkt-pay-input[data-row="${ci}"][data-field="amount"]`).val(st.rows[ci].amount||'');
    });
    box.on('input.nktUI7BAccount change.nktUI7BAccount','[data-account-plate],[data-account-os]',function(){
      if(st.mode!=='encoder')return;
      if($(this).is('[data-account-plate]'))st.plateNumber=String($(this).val()||'').trim();
      else st.osNo=String($(this).val()||'').trim();
    });
    box.on('change.nktUI7BAccount','.nkt-pay-method',function(){
      const i=Number($(this).data('row')),method=$(this).val(),old=st.rows[i].method;
      if(method==='Cash'&&st.rows.some((r,idx)=>idx!==i&&r.method==='Cash')){frappe.msgprint(__('Only one Cash row is needed.'));$(this).val(old);return}
      st.rows[i].method=method;
      if(method==='Card')st.rows[i].provider=st.rows[i].provider||'Card Terminal';
      if(old==='Cash'&&method!=='Cash'){st.cashTendered=0;st.cashTenderedManual=false}
      if(method==='Cash'){st.rows[i].reference='';st.rows[i].provider='';st.rows[i].check_date='';st.cashTenderedManual=false}
      else if(method!=='Check')st.rows[i].check_date='';
      renderAccountDialog(st,d,{row:i,afterMethod:true});
    });
    box.on('input.nktUI7BAccount change.nktUI7BAccount','.nkt-pay-input',function(){
      const i=Number($(this).data('row')),field=String($(this).data('field')||'');if(!st.rows[i])return;
      if(field==='cash_tendered'){st.cashTendered=Math.max(Number($(this).val()||0),0);st.cashTenderedManual=true}
      else st.rows[i][field]=field==='amount'?Math.max(Number($(this).val()||0),0):String($(this).val()||'');
      rebalanceAccountCash(st);const tt=accountTotals(st);
      box.find('[data-pay-summary="total"]').text(money(tt.settled));
      box.find('[data-pay-summary="balance"]').text(money(tt.balance)).closest('.remaining').toggleClass('ok',Math.abs(tt.balance)<=.005).toggleClass('bad',Math.abs(tt.balance)>.005);
      box.find('[data-pay-summary="change"]').text(money(tt.change));
    });
    box.on('click.nktUI7BAccount','[data-pay-remove]',function(){const i=Number($(this).data('pay-remove'));if(st.rows.length>1){const wasCash=st.rows[i]?.method==='Cash';st.rows.splice(i,1);if(wasCash){st.cashTendered=0;st.cashTenderedManual=false}renderAccountDialog(st,d)}});
    box.on('click.nktUI7BAccount','[data-pay-action="add"]',()=>{accountAddRow(st);renderAccountDialog(st,d)});
    box.on('click.nktUI7BAccount','[data-pay-action="exact"]',()=>{accountSetExactCash(st);renderAccountDialog(st,d)});
    box.on('click.nktUI7BAccount','[data-pay-action="review"]',()=>reviewAccount(st,d));
    box.on('keydown.nktUI7BAccount','input,select,button',function(e){
      if(e.key==='F1'){consume(e);return false}
      if(e.key==='Escape'){e.preventDefault();d.hide();return}
      const focusable=box.find('select:visible:not(:disabled),input:visible:not([readonly]):not([disabled]),button:visible:not(:disabled):not([tabindex="-1"])');
      const idx=focusable.index(this);
      if(e.key==='Tab'){e.preventDefault();if(!focusable.length)return;let next=idx+(e.shiftKey?-1:1);if(next<0)next=focusable.length-1;if(next>=focusable.length)next=0;const target=focusable.eq(next);target.trigger('focus');if(target.is('input'))target.select();return}
      if(e.key!=='Enter')return;e.preventDefault();
      if(idx>=0&&idx<focusable.length-1){const target=focusable.eq(idx+1);target.trigger('focus');if(target.is('input'))target.select()}
      else reviewAccount(st,d);
    });
    setTimeout(()=>{
      let target=$();
      if(focusSpec&&Number.isInteger(Number(focusSpec.row))){
        const row=Number(focusSpec.row);
        if(focusSpec.afterMethod)target=box.find(`.nkt-pay-input[data-row="${row}"]:visible:not([readonly]):not([disabled])`).first();
        if(!target.length)target=box.find(`.nkt-pay-method[data-row="${row}"]`).first();
      }
      if(!target.length)target=box.find('[data-account-principal]').first();
      target.trigger('focus');if(target.is('input'))target.select();
    },25);
  }
  function validateAccount(st){
    const t=accountTotals(st);
    if(t.principal<=0)return'Enter the Account Payment Amount.';
    if(!st.rows.length)return'Enter at least one payment row.';
    if(Math.abs(t.balance)>.01)return`The payment is not complete. Remaining balance: ${money(t.balance)}.`;
    for(let i=0;i<st.rows.length;i++){
      const r=st.rows[i];
      if(Number(r.amount||0)<=0)return`Payment row ${i+1} must have a positive amount.`;
      if(['Check','GCash','Maya','Card','Bank Transfer','Online'].includes(r.method)&&!String(r.reference||'').trim())return`${r.method} requires a reference / check number on row ${i+1}.`;
      if(r.method==='Check'&&(!r.check_date||!String(r.provider||'').trim()))return`Check requires Check Date and Issuing Bank on row ${i+1}.`;
    }
    if(st.mode==='cashier'&&t.cashRowPresent&&t.tendered+.005<t.cashDue)return`Cash Received is short by ${money(t.cashDue-t.tendered)}.`;
    return'';
  }
  function accountPaymentDescription(r){
    if(r.method==='Check')return`${r.method} — ${r.reference||''}${r.check_date?` • ${r.check_date}`:''}${r.provider?` • ${r.provider}`:''}`;
    return`${r.method}${r.reference?` — ${r.reference}`:''}`;
  }
  function reviewAccount(st,paymentDialog){
    const error=validateAccount(st);if(error){frappe.msgprint({title:__('Payment is not complete'),indicator:'red',message:__(error)});return}
    const t=accountTotals(st);
    const nonCash=st.rows.filter(r=>r.method!=='Cash').map(r=>`<div class="nkt-confirm-row"><span>${esc(accountPaymentDescription(r))}</span><b>${money(r.amount)}</b></div>`).join('');
    const d=new frappe.ui.Dialog({title:__('Payment Confirmation'),fields:[{fieldtype:'HTML',fieldname:'confirmation'}]});
    st.confirmDialog=d;d.show();d.$wrapper.find('.modal-dialog').addClass('nkt-payment-dialog');
    d.fields_dict.confirmation.$wrapper.html(`<div class="nkt-confirm-box"><div class="nkt-confirm-title">${st.mode==='cashier'?'PAYMENT ON ACCOUNT CONFIRMATION':'ACCOUNT PAYMENT VERIFICATION'}</div>
      <div class="nkt-confirm-row"><span>Customer</span><b>${esc(st.context.customer_name)}</b></div>
      <div class="nkt-confirm-row"><span>Account Payment</span><b>${money(t.principal)}</b></div>
      ${st.mode==='encoder'&&st.plateNumber?`<div class="nkt-confirm-row"><span>Plate Number</span><b>${esc(st.plateNumber)}</b></div>`:''}
      ${st.mode==='encoder'&&st.osNo?`<div class="nkt-confirm-row"><span>OS#</span><b>${esc(st.osNo)}</b></div>`:''}${nonCash}
      ${t.cashRowPresent?`<div class="nkt-confirm-row"><span>Cash Due</span><b>${money(t.cashDue)}</b></div>${st.mode==='cashier'?`<div class="nkt-confirm-row"><span>Cash Received</span><b>${money(t.tendered)}</b></div>`:''}`:''}
      ${t.surcharge?`<div class="nkt-confirm-row"><span>Card Surcharge — 2%</span><b>${money(t.surcharge)}</b></div><div class="nkt-confirm-row"><span>Actual Collected</span><b>${money(t.totalCollected)}</b></div>`:''}
      ${st.mode==='cashier'&&t.cashRowPresent?`<div class="nkt-confirm-change">CHANGE: ${money(t.change)}</div>`:''}
      <div class="nkt-confirm-hint">Press Enter to Confirm • Esc to Return</div></div>`);
    const confirm=()=>submitAccount(st,paymentDialog,d);
    d.set_primary_action(__('Confirm Payment'),confirm);
    d.$wrapper.find('.modal-header .btn-modal-close,.modal-header .close').attr('tabindex','-1');
    const handler=e=>{
      if(e.key==='Escape'){e.preventDefault();d.hide();return}
      if(e.key==='Tab'){e.preventDefault();d.get_primary_btn().trigger('focus');return}
      if(e.key==='Enter'&&!$(e.target).is('textarea')){e.preventDefault();confirm()}
    };
    $(document).off('keydown.nktUI7BAccountConfirm').on('keydown.nktUI7BAccountConfirm',handler);
    d.$wrapper.on('hidden.bs.modal.nktUI7BAccountConfirm',()=>{$(document).off('keydown.nktUI7BAccountConfirm');st.confirmDialog=null});
    setTimeout(()=>d.get_primary_btn().trigger('focus'),20);
  }
  function accountPayload(st){
    const t=accountTotals(st);
    return{mode:st.mode,request_id:st.requestId,customer:st.context.customer,account_amount:t.principal,
      payments:st.rows.map(r=>({payment_method:r.method,amount:Number(r.amount||0),
        cash_tendered:st.mode==='cashier'&&r.method==='Cash'?t.tendered:0,
        change_amount:st.mode==='cashier'&&r.method==='Cash'?t.change:0,
        reference_number:r.reference||'',check_number:r.method==='Check'?(r.reference||''):'',
        check_date:r.check_date||'',bank_or_provider:r.provider||''})),
      plate_number:st.mode==='encoder'?(st.plateNumber||''):'',os_no:st.mode==='encoder'?(st.osNo||''):'',
      remarks:'Fast Screen Payment on Account'};
  }
  async function submitAccount(st,paymentDialog,confirmDialog){
    if(st.busy)return;st.busy=true;
    confirmDialog.disable_primary_action?.();
    try{
      let result;
      try{result=await call('submit_fast_account_payment',{payload:JSON.stringify(accountPayload(st))})}
      catch(first){
        const status=await call('get_fast_account_payment_status',{mode:st.mode,request_id:st.requestId}).catch(()=>({}));
        if(status.found&&status.submitted&&status.result)result=status.result;else throw first
      }
      $(document).off('keydown.nktUI7BAccountConfirm');
      confirmDialog.hide();paymentDialog.hide();
      await showAccountResult(st,result||{});
    }catch(e){
      st.busy=false;confirmDialog.enable_primary_action?.();
      frappe.msgprint({title:'Payment on Account not posted',message:esc(errorText(e)),indicator:'red'});
    }
  }
  async function refreshAccountBalance(st,result){
    if(st.mode!=='encoder')return null;
    let balance=result.current_account_balance;
    if(balance===undefined||balance===null){
      try{const c=await call('get_fast_account_payment_customer_context',{mode:'encoder',customer:st.context.customer});balance=c.current_account_balance}catch(_){}
    }
    if(balance===undefined||balance===null)return null;
    const s=current(st.frm);
    if(s?.customer&&s.customer.name===st.context.customer){
      s.customer.current_account_balance=Number(balance||0);
      $(st.frm.wrapper).find('[data-role="customer-balance"]').text(money(balance));
    }
    return Number(balance||0);
  }
  function parenMoney(v){const n=Number(v||0);return n<0?`(${money(Math.abs(n))})`:money(n)}
  function fastReceiptPaymentLines(r){return(r.payments||[]).map(p=>{const parts=[`${String(p.method||'Payment')}: ${money(p.amount)}`];if(String(p.method||'').toUpperCase()==='CASH'&&Number(p.cash_tendered||0)>0){parts.push(`Tendered ${money(p.cash_tendered)}`);parts.push(`Change ${money(p.change_amount)}`)}if(p.reference_number)parts.push(`Ref ${p.reference_number}`);if(p.check_number)parts.push(`Check ${p.check_number}`);if(p.check_date)parts.push(p.check_date);if(p.bank_or_provider)parts.push(p.bank_or_provider);return`<div>${esc(parts.join(' · '))}</div>`}).join('')}
  function printFastAccountReceipt(r){
    if(!r||!r.receipt_number)return false;
    const popup=window.open('','_blank');if(!popup){frappe.msgprint('Allow pop-ups for the NKT site, then try again.');return false}
    try{popup.opener=null}catch(_){}
    const refs=[r.plate_reference?`Plate ${r.plate_reference}`:'',r.os_no?`OS# ${r.os_no}`:'',r.remarks||''].filter(Boolean).join(' · ');
    const surcharge=Number(r.card_surcharge||0)>0?`<tr><td>1</td><td><b>Card Surcharge</b></td><td class="num">${money(r.card_surcharge)}</td><td class="num"><b>${money(r.card_surcharge)}</b></td></tr>`:'';
    const balances=r.show_account_balances?`<tr><td><b>Previous Acct Balance:</b></td><td>${parenMoney(r.previous_account_balance)}</td></tr><tr><td><b>TOTAL Acct Balance:</b></td><td>${parenMoney(r.total_account_balance)}</td></tr>`:'';
    popup.document.open();popup.document.write(`<!doctype html><html><head><meta charset="utf-8"><title>${esc(r.receipt_number)}</title><style>@page{size:8.5in 5.5in landscape;margin:.22in}*{box-sizing:border-box}body{font-family:"Courier New",monospace;color:#111;margin:0;font-size:9pt;line-height:1.12}.top{display:grid;grid-template-columns:1fr 2fr 1fr;align-items:start}.printed{font-size:8pt}.title{text-align:center;font-weight:bold;font-size:12pt;letter-spacing:.2px}.number{text-align:right;font-size:10pt}.bill{margin:16px 0 6px}.items{width:100%;border-collapse:collapse;table-layout:fixed}.items th,.items td{border-bottom:1px solid #444;padding:2px 4px;text-align:left}.items th:nth-child(1){width:12%}.items th:nth-child(3){width:19%}.items th:nth-child(4){width:22%}.num{text-align:right!important;font-variant-numeric:tabular-nums}.refs{min-height:20px;padding:5px 10%;font-style:italic}.lower{display:grid;grid-template-columns:1fr 1fr;gap:20px;margin-top:3px}.payments{padding-top:8px}.balances table{width:100%;border-collapse:collapse}.balances td{padding:1px 2px}.balances td:last-child{text-align:right;font-weight:bold}.receipt-total{border-top:1px solid #444;padding-top:3px}.signature{width:32%;margin-left:auto;margin-top:30px;border-top:1px solid #111;text-align:center;font-family:Arial,sans-serif;font-weight:bold;padding-top:2px}</style></head><body><div class="top"><div class="printed">Printed: ${esc(r.generated_at||'')}</div><div class="title">TRUST RECEIPT AGREEMENT/ &nbsp; Sales Receipt</div><div class="number"><b>${esc(r.receipt_number)}</b><br>${esc(r.transaction_date||'')}</div></div><div class="bill"><b>Bill To:</b> ${esc(r.customer_name||r.customer||'')}</div><table class="items"><thead><tr><th>Qty</th><th>Item Name</th><th class="num">Price</th><th class="num">Ext. Price</th></tr></thead><tbody><tr><td>1</td><td><b>Payment On Account</b></td><td class="num">${money(r.account_payment)}</td><td class="num"><b>${money(r.account_payment)}</b></td></tr>${surcharge}</tbody></table><div class="refs">${esc(refs)}</div><div class="lower"><div class="payments">${fastReceiptPaymentLines(r)}</div><div class="balances"><table><tr class="receipt-total"><td><b>RECEIPT TOTAL:</b></td><td>${money(r.receipt_total)}</td></tr>${balances}</table></div></div><div class="signature">Signature Over Printed Name</div></body></html>`);popup.document.close();popup.focus();setTimeout(()=>popup.print(),250);return true
  }

  async function showAccountResult(st,r){
    const status=String(r.status||'');
    const matched=st.mode==='encoder'&&status==='Matched'&&Boolean(r.account_application_complete);
    const ambiguous=status==='Ambiguous';
    const balance=await refreshAccountBalance(st,r);
    let title,indicator,note;
    if(st.mode==='cashier'){
      title='ACCOUNT PAYMENT RECEIVED';
      indicator='blue';
      note='Money was recorded once. The customer account balance changes only after the independent Encoder verification matches this payment.';
    }else if(matched){
      title='ACCOUNT PAYMENT VERIFIED & APPLIED';
      indicator='green';
      note='The matching Cashier payment was found and the approved receivable application was posted.';
    }else if(ambiguous){
      title='PAYMENT VERIFICATION SAVED — REVIEW NEEDED';
      indicator='orange';
      note='Several exact Cashier candidates exist. The account balance has not changed until the retained payment is resolved.';
    }else{
      title='PAYMENT VERIFICATION SAVED — WAITING FOR CASHIER MATCH';
      indicator='orange';
      note='No exact available Cashier payment matched yet. The account balance has not changed. This is intentionally not treated as a completed account payment.';
    }
    const printOpened=Boolean(st.printIntent&&r.print_receipt&&printFastAccountReceipt(r.print_receipt));
    const d=new frappe.ui.Dialog({title:__(title),fields:[{fieldtype:'HTML',fieldname:'result'}]});
    const lines=[
      `<div class="nkt-confirm-row"><span>Reference</span><b>${esc(r.payment_display_number||publicPaymentId(r.payment_receipt)||(st.mode==='encoder'?'Verification Saved':'Payment Saved'))}</b></div>`,
      `<div class="nkt-confirm-row"><span>Account Payment</span><b>${money(r.account_principal)}</b></div>`
    ];
    if(st.mode==='cashier'&&Number(r.card_surcharge||0)>0)lines.push(`<div class="nkt-confirm-row"><span>Card Surcharge</span><b>${money(r.card_surcharge)}</b></div>`);
    if(st.mode==='encoder')lines.push(`<div class="nkt-confirm-row"><span>Automatically Applied</span><b>${money(r.total_allocated)}</b></div><div class="nkt-confirm-row"><span>Unapplied / Advance</span><b>${money(r.unallocated_amount)}</b></div>`);
    if(st.mode==='encoder'&&balance!==null)lines.push(`<div class="nkt-confirm-row"><span>Current Account Balance</span><b>${money(balance)}</b></div>`);
    d.show();d.$wrapper.find('.modal-dialog').addClass('nkt-payment-dialog');
    d.fields_dict.result.$wrapper.html(`<div class="nkt-confirm-box"><div class="nkt-confirm-title">${esc(title)}</div>${lines.join('')}<div class="nkt-payment-note" style="margin-top:8px">${esc(note)}</div><div class="nkt-confirm-hint">${printOpened?'Print opened • ':''}Press Enter for next transaction</div></div>`);
    d.set_primary_action(__('Next Transaction'),()=>d.hide());
    const handler=e=>{if(e.key==='Enter'){e.preventDefault();d.hide()}};
    $(document).off('keydown.nktUI7BAccountResult').on('keydown.nktUI7BAccountResult',handler);
    d.$wrapper.on('hidden.bs.modal.nktUI7BAccountResult',()=>{$(document).off('keydown.nktUI7BAccountResult');st.busy=false;setTimeout(()=>$(st.frm.wrapper).find('[data-role="item-entry"]').trigger('focus'),30)});
    setTimeout(()=>d.get_primary_btn().trigger('focus'),20);
  }

  function historyDevice(){try{return String(localStorage.getItem('nkt_customer_history_device_id')||'').trim()}catch(_){return''}}
  async function openCustomerHistory(frm){if(frm?.doctype!==ENCODER||document.getElementById(CUSTOMER_LAYER))return;const s=current(frm);if(!s?.customer){frappe.show_alert({message:'Select a Customer first.',indicator:'orange'},4);return}const layer=document.createElement('div');layer.id=CUSTOMER_LAYER;layer.className='nkt-ui7b-layer';layer.innerHTML=`<div class="nkt-ui7b-panel nkt-ui7b-history"><div class="nkt-ui7b-title"><b>CUSTOMER HISTORY — ${esc(s.customer.customer_name||s.customer.name)}</b><span>Read only · Plate / OS# searchable</span></div><div class="nkt-ui7b-body"><div class="nkt-ui7b-history-filters"><label>Item<input data-h="item" placeholder="Exact item code (optional)"></label><label>Plate Number<input data-h="plate" placeholder="Contains…"></label><label>OS#<input data-h="os" placeholder="Contains…"></label><label>From Date<input data-h="from" type="date"></label><label>To Date<input data-h="to" type="date"></label><button data-h-action="load">Load</button></div><div class="nkt-ui7b-actions" style="margin-top:0;margin-bottom:5px"><button data-h-action="print">Print</button><button class="primary" data-h-action="close">Return to Order</button></div><div data-h="warning" class="nkt-ui7b-help"></div><div class="nkt-ui7b-history-table-wrap"><table><thead><tr><th style="width:15%">Encoded At</th><th style="width:13%">Order</th><th>Items</th><th style="width:12%">Total</th><th style="width:13%">Status</th></tr></thead><tbody data-h="rows"><tr><td colspan="5">Loading…</td></tr></tbody></table></div></div></div>`;document.body.appendChild(layer);const st={frm,s,layer:$(layer),rows:[],customer:s.customer.name};st.layer.on('click','[data-h-action="close"]',()=>closeCustomer(st));st.layer.on('click','[data-h-action="load"]',()=>loadCustomer(st));st.layer.on('click','[data-h-action="print"]',()=>openCustomerPrint(st));await loadCustomer(st)}
  function customerArgs(st){const v=n=>String(st.layer.find(`[data-h="${n}"]`).val()||'').trim();return{customer:st.customer,device_id:historyDevice(),item:v('item')||null,plate_number:v('plate')||null,os_no:v('os')||null,from_date:v('from')||null,to_date:v('to')||null,limit:200,offset:0}}
  async function loadCustomer(st){st.layer.find('[data-h="rows"]').html('<tr><td colspan="5">Loading…</td></tr>');try{const r=await call(`${ROUTED}.get_encoder_customer_history`,customerArgs(st));st.rows=r.rows||[];st.layer.find('[data-h="warning"]').text(r.warning||'');st.layer.find('[data-h="rows"]').html(st.rows.length?st.rows.map(row=>{const items=(row.items||[]).map(i=>`${i.item_name||i.item||i.item_code} × ${i.quantity||i.qty||0}`).join(' · ');return`<tr title="Plate ${esc(row.plate_number||'')} · OS# ${esc(row.os_no||'')}"><td>${esc(String(row.encoded_at||'').slice(0,19))}</td><td>${esc(row.order_no)}</td><td>${esc(items)}</td><td class="num">${money(row.grand_total)}</td><td>${esc(row.order_status||row.payment_status||'')}</td></tr>`}).join(''):'<tr><td colspan="5">No matching Customer History.</td></tr>')}catch(e){st.layer.find('[data-h="rows"]').html(`<tr><td colspan="5">${esc(errorText(e))}</td></tr>`)}}
  function openCustomerPrint(st){if(document.querySelector('.nkt-ui7b-print-layer'))return;const d=document.createElement('div');d.className='nkt-ui7b-print-layer';d.innerHTML=`<div class="nkt-ui7b-print-panel"><b>Print Customer History</b><label>Paper Size<select data-p="paper"><option value="long">Long Bond 8.5 × 13 Portrait</option><option value="short">Short Bond / Letter Portrait</option><option value="a4">A4 Portrait</option></select></label><label>Font Density<select data-p="density"><option value="5">5 pt Compact</option><option value="4">4 pt Maximum Density</option></select></label><label class="check"><input type="checkbox" data-p="plate"> Include Plate Number</label><label class="check"><input type="checkbox" data-p="os"> Include OS#</label><div class="nkt-ui7b-actions"><button data-p-action="cancel">Cancel</button><button class="primary" data-p-action="go">Prepare Print</button></div></div>`;document.body.appendChild(d);const q=$(d);q.on('click','[data-p-action="cancel"]',()=>d.remove());q.on('click','[data-p-action="go"]',async()=>{const a=customerArgs(st);try{const r=await call(`${TX}.prepare_customer_history_print`,{customer:a.customer,device_id:a.device_id,item:a.item||'',plate_number:a.plate_number||'',os_no:a.os_no||'',from_date:a.from_date||'',to_date:a.to_date||'',paper_size:q.find('[data-p="paper"]').val(),density:q.find('[data-p="density"]').val(),include_plate_number:q.find('[data-p="plate"]').prop('checked')?1:0,include_os_no:q.find('[data-p="os"]').prop('checked')?1:0});d.remove();printCustomer(r)}catch(e){frappe.msgprint({title:'Print could not be prepared',message:esc(errorText(e)),indicator:'red'})}});q.find('[data-p="paper"]').trigger('focus')}
  function printCustomer(r){const popup=window.open('','_blank');if(!popup){frappe.msgprint('Allow pop-ups for the NKT site, then try again.');return}const font=Number(r.density_config?.font_pt||5),page=String(r.paper?.page_css||'8.5in 13in portrait'),plate=Boolean(r.include_plate_number),os=Boolean(r.include_os_no);const head=`<th>Encoded At</th><th>Order</th><th>Items</th><th>Total</th><th>Status</th>${plate?'<th>Plate</th>':''}${os?'<th>OS#</th>':''}`;const rows=(r.rows||[]).map(row=>{const items=(row.items||[]).map(i=>`${i.item_name||i.item||i.item_code} × ${i.quantity||i.qty||0}`).join(' · ');return`<tr><td>${esc(String(row.encoded_at||'').slice(0,19))}</td><td>${esc(row.order_no)}</td><td>${esc(items)}</td><td class="n">${money(row.grand_total)}</td><td>${esc(row.order_status||row.payment_status||'')}</td>${plate?`<td>${esc(row.plate_number||'')}</td>`:''}${os?`<td>${esc(row.os_no||'')}</td>`:''}</tr>`}).join('');popup.document.write(`<!doctype html><html><head><meta charset="utf-8"><style>@page{size:${page};margin:.23in}body{font-family:Arial;font-size:${font}pt;margin:0}h1{font-size:${font+3}pt;margin:0 0 2px}.meta{margin-bottom:4px}table{width:100%;border-collapse:collapse;table-layout:fixed}th,td{border:.3pt solid #555;padding:1px 2px;vertical-align:top}th{text-align:left}.n{text-align:right}</style></head><body><h1>Customer History — ${esc(r.customer)}</h1><div class="meta">Generated ${esc(r.generated_at)} · Printed by ${esc(r.requested_by_identity?.full_name||r.requested_by)} · Audit ${esc(r.print_event)}</div><table><thead><tr>${head}</tr></thead><tbody>${rows}</tbody></table></body></html>`);popup.document.close();popup.focus();setTimeout(()=>popup.print(),250)}
  function closeCustomer(st){st.layer.off();document.getElementById(CUSTOMER_LAYER)?.remove();setTimeout(()=>$(st.frm.wrapper).find('[data-role="item-entry"]').trigger('focus'),20)}
})();

/* ===== END SOURCE: NKT R4 UI7B Encoder Payment on Account Bridge ===== */
