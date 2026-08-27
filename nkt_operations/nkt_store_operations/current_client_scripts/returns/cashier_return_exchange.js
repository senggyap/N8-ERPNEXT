/* NKT CURRENT CLIENT SCRIPT — NKT Cashier Return Exchange — Form
 * Consolidated from the accepted live site on 2026-08-27.
 * Source order exactly preserves Frappe Client Script creation order.
 * Edit this file; do not create phase/recovery Client Script records.
 */

/* ===== SOURCE: NKT Cashier Return Exchange V2.0C.7.12A-C.1 ===== */

(() => {
  const SIDE = "cashier";
  const DOCTYPE = "NKT Cashier Return Exchange";
  const SCREEN = SIDE === "cashier" ? "Cashier" : "Encoder";
  const FINDER = "nkt_operations.nkt_store_operations.features.returns.service";
  const MATCHING = "nkt_operations.nkt_store_operations.features.returns.matching";
  const FAST = "nkt_operations.nkt_store_operations.fast_screen_backend";
  const NS = `nktReturnExchangeFastC741${SCREEN}`;

  frappe.ui.form.on(DOCTYPE, {
    refresh(frm) { render(frm); }
  });

  function render(frm) {
    frm.disable_save();
    frm.page.clear_actions();
    frm.page.set_title(__(`NKT ${SCREEN} — Return / Exchange`));

    const wrapper = frm.fields_dict.screen_html.$wrapper;
    prepare_layout(wrapper);
    wrapper.empty().html(markup());
    install_css();

    const state = {
      frm, wrapper,
      detail: null,
      options: {warehouses: []},
      newRows: [],
      newSearchResults: [],
      newSearchIndex: 0,
      filterItemResults: [],
      filterItemIndex: 0,
      preview: null,
      previewPayload: null,
      paymentRows: [],
      selectedFilterItem: null,
      searching: false,
      submitRequestId: null,
      securityMode: "normal", securityBusy: false, terminalLocked: false, localRestrictionLatch: false,
    };
    bind(state);
    initialize_security(state).then(ok=>{
      if(ok&&state.securityMode!=="limited"&&!state.terminalLocked)bootstrap(state);
    });
  }

  function prepare_layout(wrapper) {
    const layout = wrapper.closest(".form-layout");
    layout.find(".layout-side-section").hide();
    layout.find(".layout-main-section-wrapper").css({width:"100%",maxWidth:"none",flex:"1 1 100%"});
    wrapper.closest(".layout-main-section").css({maxWidth:"none"});
  }

  function markup() {
    return `
    <div class="nkt-rx-fast" tabindex="0">
      <div class="nkt-titlebar">
        <div><strong>NKT ${SCREEN}</strong><span>Return / Exchange</span></div>
        <div class="nkt-mode">${SIDE === "cashier" ? "MONEY SIDE" : "ORDER / INVENTORY SIDE"}</div>
      </div>
      <div class="nkt-context">
        <span>Operator: <b>${frappe.session.user}</b></span>
        <span>Independent ${SCREEN} Entry</span>
        <span>OLD → NEW lineage retained internally</span>
      </div>

      <div class="nkt-workspace">
        <div class="nkt-work-placeholder" data-role="placeholder">
          <b>No OLD ORDER selected.</b>
          Search by Customer Name, Item, Amount, Date or Time in the finder below, then select a transaction.
        </div>

        <div data-role="work" hidden>
          <div class="nkt-section-title">
            <span>OLD ORDER</span>
            <span class="nkt-old-meta" data-role="old-meta"></span>
          </div>

          <div class="nkt-old-summary">
            <div><span>Customer</span><b data-role="old-customer">—</b></div>
            <div><span>OLD Total</span><b data-role="old-total">₱0.00</b></div>
            <div><span>Real-Money Basis</span><b data-role="old-money">₱0.00</b></div>
            <div><span>Account / Credit Basis</span><b data-role="old-account">₱0.00</b></div>
            <div><span>History</span><b data-role="old-generation">Original Sale</b></div>
          </div>

          <div class="nkt-grid-wrap nkt-old-wrap">
            <table class="nkt-grid nkt-old-grid">
              <thead><tr>
                <th>#</th><th>Item</th><th>Description</th><th>Sold</th><th>Returned</th>
                <th>Available</th><th>OLD Rate</th><th>Return Qty</th>
                <th>Return Value</th><th>Actual kg</th>
                ${SIDE === "encoder" ? "<th>Return Stock As *</th>" : ""}
              </tr></thead>
              <tbody data-role="old-body"></tbody>
            </table>
          </div>

          ${SIDE === "encoder" ? `
          <div class="nkt-inline-control">
            <label>Return Receiving Warehouse</label>
            <select data-role="return-warehouse"></select>
            <span data-role="return-warehouse-note">Where the returned stock is physically added.</span>
          </div>` : ""}

          <div class="nkt-section-title nkt-new-title">
            <span>NEW ORDER</span>
            <span class="nkt-new-hint"><u>F3</u> Replacement Item • Qty → Rate • Enter returns to F3</span>
          </div>

          <div class="nkt-new-entry">
            <label><u>F3</u> Enter Item</label>
            <div class="nkt-combo">
              <input data-role="new-item-entry" autocomplete="off" placeholder="Item code, barcode, or description">
              <div data-role="new-item-results" class="nkt-results" hidden></div>
            </div>
            <button data-action="add-new-item">Add</button>
            <span>Searchable like Fast Screen</span>
          </div>

          <div class="nkt-grid-wrap nkt-new-wrap">
            <table class="nkt-grid nkt-new-grid">
              <thead><tr>
                <th>#</th><th>Item</th><th>Description</th><th>Qty</th><th>UOM</th>
                <th>Rate</th>${SIDE === "encoder" ? "<th>Source Warehouse</th>" : ""}
                <th>Amount</th><th></th>
              </tr></thead>
              <tbody data-role="new-body"><tr class="nkt-empty"><td colspan="${SIDE === "encoder" ? 9 : 8}">For a pure Return, leave NEW ORDER empty. For an Exchange, press F3 to add the replacement.</td></tr></tbody>
            </table>
          </div>

          <div class="nkt-settlement">
            <div class="nkt-transaction-row">
              <div><label>Transaction</label><select data-role="transaction-type"><option>Return</option><option>Exchange</option></select></div>
              <div data-role="settlement-direction" class="nkt-direction">No price difference yet.</div>
            </div>

            <div class="nkt-payment-settlement" data-role="payment-section" hidden>
              <div class="nkt-settlement-title">
                <span><b>PAYMENT SETTLEMENT</b> — Customer owes a difference</span>
                <span><u>F11</u> Add Payment</span>
              </div>
              <table class="nkt-grid nkt-pay-grid">
                <thead data-role="payment-head"></thead>
                <tbody data-role="payment-body"></tbody>
              </table>
              <div class="nkt-payment-actions">
                <button data-action="add-payment"><b>F11</b> Add Payment</button>
                <span data-role="payment-help">For one payment method, the system automatically applies the full Difference Due. For Cash, <b>Cash Tendered is required</b> and must be the actual bills received; Change is automatic. Use F11 only when splitting payment methods.</span>
              </div>
              ${SIDE==="cashier"?`
              <div class="nkt-cash-change-strip" data-role="cash-change-strip" hidden>
                <div><span>CASH DUE</span><b data-role="cash-due-summary">₱0.00</b></div>
                <div><span>CASH TENDERED</span><b data-role="cash-tender-summary">—</b></div>
                <div class="nkt-change-give"><span>CHANGE TO GIVE</span><b data-role="cash-change-summary">—</b></div>
              </div>`:""}
              <div class="nkt-payment-totals">
                <div><span>Return Credit Applied</span><b data-role="pay-return-credit">₱0.00</b></div>
                <div class="nkt-due-box"><span>DIFFERENCE DUE</span><b data-role="pay-due" class="nkt-collect-amount">₱0.00</b></div>
                <div><span>Settled Amount</span><b data-role="pay-settled">₱0.00</b></div>
                <div><span data-role="pay-balance-label">Balance Remaining</span><b data-role="pay-balance">₱0.00</b></div>
                <div><span>Status</span><b data-role="pay-status">NOT SETTLED</b></div>
              </div>
            </div>

            <div class="nkt-refund-settlement" data-role="refund-section" hidden>
              <div class="nkt-settlement-title"><span><b>RETURN SETTLEMENT</b> — Value is due back to the customer</span></div>
              <div class="nkt-refund-controls">
                <div>
                  <label>Settle As</label>
                  <select data-role="settlement-destination">
                    <option>None</option>
                    <option value="Refund Money">Refund Money</option>
                    <option>Customer Credit</option>
                    <option>Account Adjustment</option>
                  </select>
                </div>
                <div data-role="refund-method-control">
                  <label>Refund Money Method</label>
                  <select data-role="settlement-method">
                    <option value=""></option><option>Cash</option><option>Check</option><option>GCash</option>
                    <option>Maya</option><option>Bank Transfer</option><option>Online</option>
                  </select>
                </div>
                <div data-role="refund-reference-control">
                  <label>Refund Reference</label><input data-role="settlement-reference">
                </div>
                <div class="nkt-refund-cap">
                  <span>Maximum actual-money refund</span><b data-role="refund-cap">₱0.00</b>
                </div>
              </div>
              <div class="nkt-refund-totals">
                <div class="nkt-refund-due-box"><span>AMOUNT DUE BACK</span><b data-role="refund-due" class="nkt-return-amount">₱0.00</b></div>
                <div><span>Actual Refund</span><b data-role="refund-actual" class="nkt-return-amount">—</b></div>
                <div><span>Account Adjustment</span><b data-role="refund-account">—</b></div>
                <div><span>Customer Credit</span><b data-role="refund-credit">—</b></div>
                <div><span>Status</span><b data-role="refund-status">NOT SETTLED</b></div>
              </div>
            </div>

            <div class="nkt-total-strip">
              <div><span>Return Credit</span><b data-role="sum-return">—</b></div>
              <div><span>NEW ORDER</span><b data-role="sum-new">—</b></div>
              <div><span>Customer Pays</span><b data-role="sum-pays">—</b></div>
              <div><span>Actual Refund</span><b data-role="sum-refund">—</b></div>
              <div><span>Credit / Adjustment</span><b data-role="sum-credit">—</b></div>
            </div>
            <div class="nkt-basis-note" data-role="basis-note">Press F8 Preview before Submit.</div>
          </div>

          <div class="nkt-actionbar">
            <span>F2 Customer Search • F3 NEW Item • F8 Preview • F11 Payment • F12 Submit</span>
            <div class="spacer"></div>
            <button data-action="clear-work">Clear</button>
            <button data-action="preview"><b>F8</b> Preview Settlement</button>
            <button data-action="submit" class="primary"><b>F12</b> Submit Return / Exchange</button>
          </div>
        </div>
      </div>

      <div class="nkt-finder-title">
        <div><strong>FIND OLD ORDER</strong><span>Search results stay below the active transaction.</span></div>
        <button data-action="recent">My Recent</button>
      </div>

      <div class="nkt-filters">
        <div class="wide"><label><u>F2</u> Customer Name</label><input data-f="customer" placeholder="Type customer name..."></div>
        <div><label>Item</label><div class="nkt-combo"><input data-f="item" autocomplete="off" placeholder="Search item..."><div data-role="filter-item-results" class="nkt-results" hidden></div></div></div>
        <div><label>Amount</label><select data-f="amount-mode"><option>Any</option><option>Exact</option><option>Range</option></select></div>
        <div data-role="amount-exact" hidden><label>Exact Amount</label><input data-f="amount-exact" type="number" step="0.01"></div>
        <div data-role="amount-from" hidden><label>Amount From</label><input data-f="amount-from" type="number" step="0.01"></div>
        <div data-role="amount-to" hidden><label>Amount To</label><input data-f="amount-to" type="number" step="0.01"></div>
        <div><label>Date</label><select data-f="date-preset"><option>Today</option><option>7 Days</option><option selected>30 Days</option><option>Custom</option></select></div>
        <div data-role="date-from" hidden><label>Date From</label><input data-f="date-from" type="date"></div>
        <div data-role="date-to" hidden><label>Date To</label><input data-f="date-to" type="date"></div>
        <div><label>Time From</label><input data-f="time-from" type="time" value="00:00"></div>
        <div><label>Time To</label><input data-f="time-to" type="time" value="23:59"></div>
        <div><label>Source</label><select data-f="source"><option>All</option><option>Original Sale</option><option>Previous Exchange</option></select></div>
        <div><label>&nbsp;</label><button data-action="search" class="primary">Search</button></div>
      </div>

      <div class="nkt-search-results" data-role="search-results">
        <div class="nkt-search-empty">Search results will appear here.</div>
      </div>
      <div class="nkt-recent" data-role="recent"></div>
    </div>`;
  }

  function install_css() {
    if (document.getElementById("nkt-rx-fast-c741-style")) return;
    const s = document.createElement("style");
    s.id = "nkt-rx-fast-c741-style";
    s.textContent = `
      .nkt-rx-fast{font-family:Tahoma,Arial,sans-serif;font-size:13px;color:#111;background:#d5d5d5;border:1px solid #6f6f6f;min-height:calc(100vh - 155px);box-shadow:inset 0 0 0 1px #fff}
      .nkt-titlebar{display:flex;justify-content:space-between;align-items:center;padding:7px 10px;background:linear-gradient(#fafafa,#c7c7c7);border-bottom:1px solid #777;font-size:18px}.nkt-titlebar span{font-size:13px;font-weight:normal;margin-left:12px}.nkt-mode{font-size:11px;font-weight:bold;padding:4px 8px;background:#fff1a8;border:1px solid #9b7900}
      .nkt-context{display:flex;gap:26px;padding:5px 10px;background:#ececec;border-bottom:1px solid #999;white-space:nowrap;overflow:auto}
      .nkt-workspace{background:#fff;border-bottom:2px solid #666}.nkt-work-placeholder{padding:22px 12px;background:#fff7cf;border:1px solid #a98b2a;margin:8px;line-height:1.5}
      .nkt-section-title{display:flex;justify-content:space-between;align-items:center;padding:6px 9px;background:linear-gradient(#f5f5f5,#cecece);border-top:1px solid #777;border-bottom:1px solid #777;font-size:15px;font-weight:bold}.nkt-new-title{margin-top:6px}.nkt-old-meta,.nkt-new-hint{font-size:11px;font-weight:normal;color:#444}
      .nkt-old-summary{display:grid;grid-template-columns:2fr repeat(4,1fr);gap:0;border-bottom:1px solid #999;background:#f7f7f7}.nkt-old-summary>div{padding:6px 8px;border-right:1px solid #bbb}.nkt-old-summary span,.nkt-total-strip span{display:block;font-size:10px;color:#555;text-transform:uppercase}.nkt-old-summary b{font-size:14px}
      .nkt-grid-wrap{overflow:auto;background:#fff}.nkt-old-wrap{max-height:220px}.nkt-new-wrap{min-height:110px;max-height:260px}.nkt-grid{width:100%;border-collapse:collapse;table-layout:fixed}.nkt-grid th{background:linear-gradient(#f7f7f7,#d4d4d4);border-right:1px solid #999;border-bottom:1px solid #777;padding:5px 6px;text-align:left;white-space:nowrap}.nkt-grid td{border-right:1px solid #bbb;border-bottom:1px solid #ccc;padding:4px 6px;vertical-align:middle;line-height:1.2}.nkt-grid input,.nkt-grid select{width:100%;height:27px;border:1px solid transparent;background:#fff;border-radius:0;padding:2px 5px}.nkt-grid input:focus,.nkt-grid select:focus{border-color:#1b5790;outline:1px solid #1b5790}.nkt-grid .nkt-return-qty,.nkt-grid .nkt-qty,.nkt-grid .nkt-rate{border:1px solid #5a7690;background:#fff;box-shadow:inset 0 0 0 1px #eef3f7;font-weight:bold}.nkt-grid .nkt-return-qty:focus,.nkt-grid .nkt-qty:focus,.nkt-grid .nkt-rate:focus{border-color:#174f83;outline:2px solid #b9d8f5}.nkt-grid .nkt-rate-edited{background:#fff4bf;border-color:#b28a00}.nkt-settle-required{outline:2px solid #c98b00!important;background:#fff4bf!important}.nkt-empty td{text-align:center;color:#666;padding:22px}
      .nkt-inline-control{display:grid;grid-template-columns:185px minmax(280px,440px) 1fr;gap:8px;align-items:center;padding:6px 9px;background:#efefef;border-bottom:1px solid #999}.nkt-inline-control label{font-weight:bold;margin:0}.nkt-inline-control select{height:28px;border:1px solid #666;border-radius:0}
      .nkt-new-entry{display:grid;grid-template-columns:110px minmax(320px,1fr) 70px minmax(260px,.7fr);gap:7px;align-items:center;padding:7px 9px;background:#dedede;border-bottom:1px solid #999}.nkt-new-entry label{font-weight:bold;margin:0}.nkt-new-entry input{height:29px;border:1px solid #666;border-radius:0;padding:3px 6px;font-size:14px;font-weight:bold;width:100%}
      .nkt-combo{position:relative}.nkt-results{position:absolute;left:0;right:0;top:100%;z-index:70;background:#fff;border:1px solid #555;max-height:240px;overflow:auto;box-shadow:2px 3px 7px rgba(0,0,0,.25)}.nkt-result{padding:6px 8px;border-bottom:1px solid #ddd;cursor:pointer}.nkt-result.active,.nkt-result:hover{background:#cfe7ff}.nkt-result small{display:flex;justify-content:space-between;color:#555;margin-top:2px}
      .nkt-settlement{border-top:1px solid #777;background:#efefef}.nkt-transaction-row{display:grid;grid-template-columns:230px 1fr;gap:10px;align-items:end;padding:7px 9px;border-bottom:1px solid #999;background:#e3e3e3}.nkt-transaction-row label,.nkt-refund-controls label,.nkt-filters label{display:block;font-size:11px;font-weight:bold;margin-bottom:2px}.nkt-transaction-row select,.nkt-refund-controls input,.nkt-refund-controls select,.nkt-filters input,.nkt-filters select{width:100%;height:28px;border:1px solid #666;border-radius:0;padding:3px 5px;background:#fff}.nkt-direction{font-weight:bold;padding:5px 8px;background:#fff7cf;border:1px solid #a98b2a}.nkt-settlement-title{display:flex;justify-content:space-between;align-items:center;padding:6px 9px;background:linear-gradient(#eef5fb,#cbddea);border-bottom:1px solid #6c8498}.nkt-payment-settlement,.nkt-refund-settlement{border-bottom:1px solid #777;background:#fff}.nkt-pay-grid input,.nkt-pay-grid select{border:1px solid #5a7690!important;background:#fff!important}.nkt-applied-cell{background:#f3f3f3!important}.nkt-pay-applied{font-size:16px;font-weight:800;white-space:nowrap}.nkt-payment-actions{display:flex;align-items:center;gap:12px;padding:6px 9px;background:#ececec;border-top:1px solid #bbb}.nkt-payment-actions span{font-size:11px;color:#444}.nkt-cash-change-strip{display:grid;grid-template-columns:repeat(3,1fr);border-top:1px solid #999;border-bottom:1px solid #777;background:#fff}.nkt-cash-change-strip>div{padding:9px 12px;border-right:1px solid #bbb}.nkt-cash-change-strip span{display:block;font-size:11px;font-weight:bold}.nkt-cash-change-strip b{display:block;font-size:22px;margin-top:2px}.nkt-change-give b{font-size:28px}.nkt-payment-totals,.nkt-refund-totals{display:grid;grid-template-columns:repeat(5,1fr);border-top:1px solid #aaa}.nkt-payment-totals>div,.nkt-refund-totals>div{padding:7px 9px;border-right:1px solid #bbb}.nkt-payment-totals span,.nkt-refund-totals span,.nkt-refund-cap span{display:block;font-size:10px;color:#555;text-transform:uppercase}.nkt-payment-totals b,.nkt-refund-totals b{font-size:16px}.nkt-due-box,.nkt-refund-due-box{background:#f8f8f8}.nkt-collect-amount{font-size:22px!important;font-weight:800!important;color:#a91515!important}.nkt-return-amount{font-size:22px!important;font-weight:800!important;color:#176b24!important}.nkt-overpay-amount{font-size:18px!important;font-weight:800!important;color:#a91515!important}.nkt-balance-amount{font-size:18px!important;font-weight:800!important;color:#a91515!important}.nkt-zero-balance{color:#176b24!important}.nkt-direction-collect{border-color:#b23b3b!important;background:#fff0f0!important;color:#8e1111!important}.nkt-direction-return{border-color:#4d8d59!important;background:#eef9f0!important;color:#176b24!important}.nkt-pay-ok,.nkt-refund-ok{color:#176b24}.nkt-not-ok{color:#9b1c1c}.nkt-pay-grid th{white-space:normal;line-height:1.05;font-size:11px}.nkt-pay-grid td{padding:4px}.nkt-refund-controls{display:grid;grid-template-columns:1.2fr 1fr 1fr 1fr;gap:8px;align-items:end;padding:7px 9px}.nkt-refund-cap{padding:5px 8px;background:#fff7cf;border:1px solid #a98b2a}.nkt-refund-cap b{font-size:15px}.nkt-refund-unavailable{color:#777}.nkt-locked-credit{background:#f3f3f3!important;color:#333;font-weight:bold}
      .nkt-total-strip{display:grid;grid-template-columns:repeat(5,1fr);background:#fff}.nkt-total-strip>div{padding:7px 9px;border-right:1px solid #bbb;border-bottom:1px solid #bbb}.nkt-total-strip b{font-size:18px}.nkt-basis-note{padding:5px 9px;background:#fff7cf;border-bottom:1px solid #a98b2a;font-size:11px}
      .nkt-actionbar{display:flex;align-items:center;gap:7px;padding:7px 9px;background:#d7d7d7;border-top:1px solid #777}.nkt-actionbar .spacer{flex:1}.nkt-rx-fast button{min-height:28px;border:1px solid #666;background:linear-gradient(#fff,#d0d0d0);border-radius:1px;padding:4px 10px;color:#111}.nkt-rx-fast button.primary{background:linear-gradient(#edf6ff,#bddbfc);border-color:#3d6f9f}
      .nkt-finder-title{display:flex;justify-content:space-between;align-items:center;padding:7px 9px;background:linear-gradient(#fafafa,#c7c7c7);border-top:1px solid #777;border-bottom:1px solid #777;font-size:14px}.nkt-finder-title span{font-size:11px;font-weight:normal;margin-left:10px}
      .nkt-filters{display:grid;grid-template-columns:2fr 1.5fr 1fr 1fr 1fr;gap:7px 9px;align-items:end;padding:8px 9px;background:#e6e6e6;border-bottom:1px solid #777}.nkt-filters>div{min-width:0}
      .nkt-search-results{background:#fff;min-height:160px;max-height:340px;overflow:auto}.nkt-search-empty{padding:28px;text-align:center;color:#666}.nkt-search-table{width:100%;border-collapse:collapse;font-size:12px}.nkt-search-table th{position:sticky;top:0;background:linear-gradient(#f5f5f5,#cecece);border-right:1px solid #999;border-bottom:1px solid #777;padding:5px 6px;text-align:left}.nkt-search-table td{border-right:1px solid #bbb;border-bottom:1px solid #ccc;padding:5px 6px}.nkt-internal{font-size:9px;color:#777}.nkt-recent{background:#fff;border-top:1px solid #777}
      .nkt-op-unavailable{display:flex;min-height:340px;align-items:center;justify-content:center;border:1px solid #777;background:#f1f1f1;font:700 16px Tahoma,Arial,sans-serif}
      @media(max-width:1100px){.nkt-old-summary{grid-template-columns:1fr 1fr 1fr}.nkt-settle-controls,.nkt-total-strip{grid-template-columns:1fr 1fr 1fr}.nkt-filters{grid-template-columns:1fr 1fr 1fr}.nkt-new-entry{grid-template-columns:100px 1fr 65px}.nkt-new-entry>span{grid-column:1/-1}.nkt-actionbar>span{display:none}}
    `;
    document.head.appendChild(s);
  }

  function role(st,n){return st.wrapper.find(`[data-role="${n}"]`);}
  function esc(v){return frappe.utils.escape_html(String(v ?? ""));}
  function attr(v){return esc(v).replace(/"/g,"&quot;");}
  function money(v){return format_currency(flt(v||0),"PHP");}
  function edit(v){const n=Number(v||0);return Number.isInteger(n)?String(n):String(Math.round(n*100)/100);}
  function warehouse_label(st,name){const x=(st.options.warehouses||[]).find(w=>w.name===name);return x?x.label:name;}

  async function consume_history_prefill(st){
    const key=`nkt_return_exchange_prefill_${SIDE}`;
    let payload=null;
    try{
      payload=JSON.parse(window.localStorage.getItem(key)||"null");
      window.localStorage.removeItem(key);
    }catch(_){payload=null;}
    if(!payload||payload.side!==SIDE||!payload.source_name)return false;
    if(Number(payload.expires_at||0)<Date.now())return false;
    if(payload.requested_by&&payload.requested_by!==frappe.session.user)return false;
    await load_old(st,String(payload.source_name));
    frappe.show_alert({message:__("Original transaction loaded from Transaction History."),indicator:"green"},5);
    return true;
  }

  async function bootstrap(st){
    const r=await frappe.call({method:`${FINDER}.get_return_entry_options`,args:{side:SIDE}});
    st.options=r.message||{warehouses:[]};
    if(SIDE==="encoder"){
      role(st,"return-warehouse").html('<option value=""></option>'+warehouse_options(st));
    }
    await search_old(st);
    const prefilled=await consume_history_prefill(st);
    if(!prefilled)focus_customer(st);
  }

  function warehouse_options(st,selected=""){
    return (st.options.warehouses||[]).map(w=>`<option value="${attr(w.name)}" ${w.name===selected?"selected":""}>${esc(w.label||w.name)}</option>`).join("");
  }

  function update_return_warehouse_note(st){
    if(SIDE!=="encoder")return;
    const exact=role(st,"return-warehouse").val()||"";
    const w=(st.options.warehouses||[]).find(x=>x.name===exact);
    role(st,"return-warehouse-note").html(
      exact
        ? `ERP Warehouse: <b>${esc(exact)}</b>${w&&w.fulfillment_type?` · ${esc(w.fulfillment_type)}`:""}`
        : "Where the returned stock is physically added."
    );
  }

  function invalidate_payment_settlement(st){
    // Payment edits are isolated from general transaction recalculation.
    // This prevents payment rows from being rebuilt/cleared while typing.
    st.preview=null;
    st.previewPayload=null;
    role(st,"pay-status").text("NOT SETTLED").removeClass("nkt-pay-ok").addClass("nkt-not-ok");
    role(st,"refund-status").text("NOT SETTLED").removeClass("nkt-refund-ok").addClass("nkt-not-ok");
    render_payment_totals(st);
  }

  function bind(st){
    const w=st.wrapper;
    w.on("click",'[data-action="search"]',()=>search_old(st));
    w.on("click",'[data-select-source]',function(){load_old(st,$(this).attr("data-select-source"));});
    w.on("click",'[data-action="add-new-item"]',()=>new_item_enter(st));
    w.on("click",'[data-action="add-payment"]',()=>add_payment_row(st));
    w.on("click",'.nkt-pay-remove',function(){
      st.paymentRows.splice(Number($(this).attr("data-pay-row")),1);
      invalidate_payment_settlement(st);
      render_payment_grid(st);
    });
    w.on("change",'.nkt-pay-method',function(){
      const i=Number($(this).attr("data-pay-row"));
      if(st.paymentRows[i]){
        st.paymentRows[i].method=$(this).val();
        normalize_payment_row(st,i);
        invalidate_payment_settlement(st);
        render_payment_grid(st);
      }
    });
    w.on("input",'.nkt-pay-amount,.nkt-pay-cash,.nkt-pay-reference,.nkt-pay-provider,.nkt-pay-check-date',function(){
      const input=$(this),i=Number(input.attr("data-pay-row")),row=st.paymentRows[i];if(!row)return;
      if(input.hasClass("nkt-pay-amount")){
        row.amount=flt(input.val());
        row.auto_amount=false;
      }
      else if(input.hasClass("nkt-pay-cash")){
        row.cash_tendered=flt(input.val());
        row.auto_tendered=false;
      }
      else if(input.hasClass("nkt-pay-reference"))row.reference=input.val();
      else if(input.hasClass("nkt-pay-provider"))row.provider=input.val();
      else if(input.hasClass("nkt-pay-check-date"))row.check_date=input.val();
      row.change=row.method==="Cash"?Math.max(Number(row.cash_tendered||0)-Number(row.amount||0),0):0;
      input.closest("tr").find('[data-role="pay-change-cell"]').text(money(row.change));
      invalidate_payment_settlement(st);
    });
    w.on("click",'[data-new-result]',function(){choose_new_item(st,Number($(this).attr("data-new-result")));});
    w.on("click",'[data-filter-result]',function(){choose_filter_item(st,Number($(this).attr("data-filter-result")));});
    w.on("click",'.nkt-remove',function(){st.newRows.splice(Number($(this).attr("data-row")),1);invalidate(st);render_new_grid(st);});
    w.on("click",'[data-action="preview"]',()=>preview(st));
    w.on("click",'[data-action="submit"]',()=>submit(st));
    w.on("click",'[data-action="clear-work"]',()=>clear_work(st));
    w.on("click",'[data-action="recent"]',()=>load_recent(st));
    w.on("input",'.nkt-qty,.nkt-rate',function(){
      const input=$(this),i=Number(input.attr("data-row")),row=st.newRows[i]; if(!row)return;
      if(input.hasClass("nkt-qty")){
        row.qty=flt(input.val());
      }else{
        row.rate=flt(input.val());
        row.rateEdited=true;
        input.addClass("nkt-rate-edited");
        input.closest("td").find(".nkt-internal").text("Manual Rate");
      }
      input.closest("tr").find('[data-role="line-amount"]').text(money(Number(row.qty||0)*Number(row.rate||0)));
      invalidate(st);
    });
    w.on("change",'[data-role="new-source-warehouse"]',function(){const i=Number($(this).attr("data-row"));if(st.newRows[i])st.newRows[i].warehouse=$(this).val();invalidate(st);});
    w.on("change",'[data-role="return-warehouse"]',()=>update_return_warehouse_note(st));
    w.on("input",'.nkt-return-qty',function(){
      sync_return_value_row(st,$(this).closest("tr"));
      invalidate(st);
    });
    w.on("change",'.nkt-value-treatment,.nkt-classification',function(){
      sync_return_value_row(st,$(this).closest("tr"));
      invalidate(st);
    });
    w.on("input",'.nkt-actual-kg,.nkt-manual-deduction',function(){
      sync_return_value_row(st,$(this).closest("tr"));
      invalidate(st);
    });
    w.on("change input",'[data-role="work"] select,[data-role="work"] input',function(){
      const el=$(this);
      if(
        el.hasClass("nkt-qty") ||
        el.hasClass("nkt-rate") ||
        el.attr("data-role")==="new-item-entry" ||
        el.is(".nkt-pay-method,.nkt-pay-amount,.nkt-pay-cash,.nkt-pay-reference,.nkt-pay-provider,.nkt-pay-check-date,.nkt-value-treatment,.nkt-classification,.nkt-actual-kg,.nkt-manual-deduction")
      )return;
      invalidate(st);conditional(st);
    });
    w.on("change",'[data-f="amount-mode"]',function(){
      const v=$(this).val();role(st,"amount-exact").prop("hidden",v!=="Exact");role(st,"amount-from").prop("hidden",v!=="Range");role(st,"amount-to").prop("hidden",v!=="Range");
    });
    w.on("change",'[data-f="date-preset"]',function(){
      const custom=$(this).val()==="Custom";role(st,"date-from").prop("hidden",!custom);role(st,"date-to").prop("hidden",!custom);
    });

    let nt=null;
    role(st,"new-item-entry").on("input",()=>{clearTimeout(nt);nt=setTimeout(()=>search_new_items(st),140);});
    role(st,"new-item-entry").on("keydown",e=>new_item_keydown(st,e));

    let ft=null;
    w.find('[data-f="item"]').on("input",()=>{st.selectedFilterItem=null;clearTimeout(ft);ft=setTimeout(()=>search_filter_items(st),160);});
    w.find('[data-f="item"]').on("keydown",e=>filter_item_keydown(st,e));
    w.find('[data-f="customer"]').on("keydown",e=>{if(e.key==="Enter"){e.preventDefault();search_old(st);}});

    $(document).off(`keydown.${NS}`).on(`keydown.${NS}`,e=>{
      if(e.key==="F1"){e.preventDefault();e.stopImmediatePropagation();return false;}
      if(e.key==="F12"&&e.ctrlKey&&e.altKey&&e.shiftKey){e.preventDefault();e.stopImmediatePropagation();self_restrict_now(st);return false;}
      if(st.securityMode==="limited"||st.terminalLocked)return;
      if($(".modal.show").length)return;
      if(e.key==="F2"){e.preventDefault();focus_customer(st);}
      else if(e.key==="F3"){e.preventDefault();focus_new_item(st);}
      else if(e.key==="F8"){e.preventDefault();if(st.detail)preview(st);}
      else if(e.key==="F11"){e.preventDefault();if(st.detail)add_payment_row(st);}
      else if(e.key==="F12"){e.preventDefault();if(st.detail)submit(st);}
      else if(e.key==="Escape"&&st.detail){e.preventDefault();focus_new_item(st);}
    });
  }


  const SECURITY_POLL_MS = 2000;

  function bound_device_id() {
    try { return String(window.localStorage.getItem("nkt_device_id") || "").trim(); }
    catch (_) { return ""; }
  }

  function initialize_security(st) {
    clear_security_watch();
    const id=bound_device_id();
    if(!id)return Promise.resolve(true);
    return refresh_security(st).then(ok=>{if(ok&&!st.terminalLocked)start_security_watch(st);return ok;});
  }

  function clear_security_watch() {
    const key=`${NS}SecurityTimer`;
    if(window[key]){clearInterval(window[key]);window[key]=null;}
    $(window).off(`focus.${NS}Security`);
  }

  function start_security_watch(st) {
    const key=`${NS}SecurityTimer`;
    window[key]=setInterval(()=>refresh_security(st),SECURITY_POLL_MS);
    $(window).off(`focus.${NS}Security`).on(`focus.${NS}Security`,()=>refresh_security(st));
  }

  function refresh_security(st) {
    if(st.securityBusy||st.terminalLocked)return Promise.resolve(!st.terminalLocked);
    const id=bound_device_id();if(!id)return Promise.resolve(true);
    st.securityBusy=true;
    return frappe.call({
      method:"nkt_operations.nkt_store_operations.features.offline_edge.internal.device_controls.get_client_security_bootstrap",
      args:{device_id:id}
    }).then(r=>{
      const p=r.message||{};
      if(p.access==="unavailable"){
        if(p.local_action==="crypto_erase_sensitive_state")crypto_erase_sensitive_state(st);
        lock_terminal_screen(st,p.message||"Device access unavailable.");
        return false;
      }
      if(p.ui_mode==="limited"){
        st.localRestrictionLatch=false;
        apply_limited_mode(st);
        return false;
      }
      if(st.securityMode==="limited"&&!st.localRestrictionLatch){render(st.frm);return false;}
      return true;
    }).catch(()=>{
      if(st.securityMode==="limited"||st.localRestrictionLatch)apply_limited_mode(st);
      return !st.terminalLocked;
    }).finally(()=>{st.securityBusy=false;});
  }

  function self_restrict_now(st) {
    if(st.terminalLocked)return;
    st.localRestrictionLatch=true;apply_limited_mode(st);
    const id=bound_device_id();if(!id)return;
    frappe.call({
      method:"nkt_operations.nkt_store_operations.features.offline_edge.internal.device_controls.self_restrict_current_device",
      args:{device_id:id}
    }).then(()=>{st.localRestrictionLatch=false;apply_limited_mode(st);})
      .catch(()=>{st.localRestrictionLatch=true;apply_limited_mode(st);});
  }

  function clear_sensitive_work(st) {
    st.detail=null;st.preview=null;st.previewPayload=null;st.newRows=[];st.newSearchResults=[];
    st.filterItemResults=[];st.paymentRows=[];st.options={warehouses:[]};
    $(".modal.show").modal("hide");
  }

  function apply_limited_mode(st) {
    st.securityMode="limited";
    clear_sensitive_work(st);
    st.wrapper.empty().html('<div class="nkt-op-unavailable">This function is unavailable in limited mode.</div>');
  }

  function crypto_erase_sensitive_state(st) {
    try{
      for(let i=window.localStorage.length-1;i>=0;i--){
        const k=window.localStorage.key(i);
        if(k&&k.startsWith("nkt_")&&k!=="nkt_device_id")window.localStorage.removeItem(k);
      }
    }catch(_){}
    try{
      for(let i=window.sessionStorage.length-1;i>=0;i--){
        const k=window.sessionStorage.key(i);
        if(k&&k.startsWith("nkt_"))window.sessionStorage.removeItem(k);
      }
    }catch(_){}
    clear_sensitive_work(st);
  }

  function lock_terminal_screen(st,message) {
    st.terminalLocked=true;st.securityMode="limited";clear_security_watch();crypto_erase_sensitive_state(st);
    $(document).off(`keydown.${NS}`);
    st.wrapper.empty().html(`<div class="nkt-op-unavailable">${esc(message||"Device access unavailable.")}</div>`);
  }

  function preset_dates(preset){
    const today=frappe.datetime.get_today();
    if(preset==="Today")return {from:today,to:today};
    if(preset==="7 Days")return {from:frappe.datetime.add_days(today,-6),to:today};
    if(preset==="30 Days")return {from:frappe.datetime.add_days(today,-29),to:today};
    return {from:"",to:""};
  }

  function filters(st){
    const w=st.wrapper,get=n=>w.find(`[data-f="${n}"]`).val()||"";
    const preset=get("date-preset"),dates=preset==="Custom"?{from:get("date-from"),to:get("date-to")}:preset_dates(preset);
    const mode=get("amount-mode");
    return {
      customer:get("customer"),
      item:st.selectedFilterItem || get("item"),
      amount_mode:mode,
      amount_exact:mode==="Exact"?get("amount-exact"):"",
      amount_from:mode==="Range"?get("amount-from"):"",
      amount_to:mode==="Range"?get("amount-to"):"",
      date_from:dates.from,date_to:dates.to,time_from:get("time-from"),time_to:get("time-to"),
      source_filter:get("source")
    };
  }

  async function search_old(st){
    if(st.searching)return;
    st.searching=true;
    role(st,"search-results").html('<div class="nkt-search-empty">Searching...</div>');
    try{
      const r=await frappe.call({method:`${FINDER}.search_return_history`,args:{side:SIDE,filters:JSON.stringify(filters(st))}});
      const rows=(r.message||{}).rows||[];
      render_search(st,rows);
    }finally{st.searching=false;}
  }

  function render_search(st,rows){
    if(!rows.length){role(st,"search-results").html('<div class="nkt-search-empty">No matching transactions.</div>');return;}
    role(st,"search-results").html(`
      <table class="nkt-search-table">
        <thead><tr><th>Date</th><th>Time</th><th>Customer</th><th>Items</th><th>Amount</th><th>History</th><th>Available</th><th></th></tr></thead>
        <tbody>${rows.map(x=>`<tr>
          <td>${esc(x.date)}</td><td>${esc(x.time)}</td>
          <td><b>${esc(x.customer_name)}</b><div class="nkt-internal">${esc(x.customer)}</div></td>
          <td>${esc(x.items_text)}</td><td><b>${money(x.total)}</b></td>
          <td>${x.generation?`Exchange #${esc(x.generation)}`:"Original Sale"}${x.has_prior_return?'<div>Prior return/exchange</div>':""}</td>
          <td>${esc(x.available_text)}</td>
          <td><button data-select-source="${attr(x.name)}">Select</button><div class="nkt-internal">${esc(x.name)}</div></td>
        </tr>`).join("")}</tbody>
      </table>`);
  }

  async function load_old(st,name){
    const r=await frappe.call({method:`${FINDER}.get_return_history_detail`,args:{side:SIDE,source_name:name}});
    st.detail=r.message||{};
    st.newRows=[];st.paymentRows=[];st.preview=null;st.previewPayload=null;
    const d=st.detail;
    role(st,"placeholder").prop("hidden",true);
    role(st,"work").prop("hidden",false);
    role(st,"old-customer").text(d.customer_name||d.customer);
    role(st,"old-total").text(money(d.total));
    role(st,"old-money").text(money(d.money_basis));
    role(st,"old-account").text(money(d.account_basis));
    role(st,"old-generation").text(d.generation?`Exchange #${d.generation}`:"Original Sale");
    role(st,"old-meta").text(`${d.date} ${d.time} · ${d.name}${d.counterpart?` · counterpart ${d.counterpart}`:""}`);
    render_old_grid(st);
    if(SIDE==="encoder"){
      role(st,"return-warehouse").html('<option value=""></option>'+warehouse_options(st));
      update_return_warehouse_note(st);
    }
    render_new_grid(st);
    reset_settlement(st);
    st.wrapper.find(".nkt-workspace")[0]?.scrollIntoView({behavior:"smooth",block:"start"});
    setTimeout(()=>focus_return_qty(st),100);
  }

  function render_old_grid(st){
    const rows=st.detail.items||[];
    role(st,"old-body").html(rows.map((x,i)=>`<tr data-old-row="${i}">
      <td>${i+1}</td><td>${esc(x.item)}</td><td>${esc(x.item_name||"")}</td>
      <td>${edit(x.original_qty)}</td><td>${edit(x.previously_returned_qty)}</td><td><b>${edit(x.available_qty)}</b></td>
      <td>${money(x.original_rate)}</td>
      <td><input class="nkt-return-qty" data-old-index="${i}" type="number" min="0" max="${attr(x.available_qty)}" step="0.01" value="0"></td>
      <td class="nkt-return-value-cell">
        <select class="nkt-value-treatment" data-old-index="${i}">
          <option>Full Value</option>
          <option>Deduct Missing kg</option>
          <option>Manual Deduction</option>
        </select>
        <div class="nkt-manual-wrap" hidden>
          <span class="nkt-internal">Deduction ₱</span>
          <input class="nkt-manual-deduction" data-old-index="${i}" type="number" min="0" step="0.01" value="0">
        </div>
        <div class="nkt-internal nkt-value-note">Full original value</div>
      </td>
      <td>
        <input class="nkt-actual-kg" data-old-index="${i}" type="number" min="0" step="0.01" value="0" disabled>
        <div class="nkt-internal">Std: ${edit(x.standard_sack_weight_kg||0)} kg/sack</div>
      </td>
      ${SIDE==="encoder"?`<td><select class="nkt-classification" data-old-index="${i}"><option value="">Select...</option><option>Saleable</option><option>Damaged</option><option>Fraction</option><option>Rejected</option></select><div class="nkt-internal">${x.damaged_item?`Damaged: ${esc(x.damaged_item)}`:""}${x.fraction_item?` · Fraction: ${esc(x.fraction_item)}`:""}</div></td>`:""}
    </tr>`).join(""));
  }

  function sync_return_value_row(st,tr){
    const i=Number(tr.attr("data-old-row")),src=st.detail?.items?.[i]||{};
    const treatment=tr.find(".nkt-value-treatment").val()||"Full Value";
    const classification=SIDE==="encoder"?(tr.find(".nkt-classification").val()||""):"";
    const kg=tr.find(".nkt-actual-kg");
    const manual=tr.find(".nkt-manual-wrap");
    const kgNeeded=treatment==="Deduct Missing kg" || (SIDE==="encoder" && classification==="Fraction");
    kg.prop("disabled",!kgNeeded);
    if(!kgNeeded && SIDE==="cashier")kg.val(0);
    manual.prop("hidden",treatment!=="Manual Deduction");

    const qty=flt(tr.find(".nkt-return-qty").val());
    const rate=Number(src.original_rate||0);
    const full=Math.max(qty,0)*rate;
    const std=Number(src.standard_sack_weight_kg||0);
    const expected=Math.max(qty,0)*std;
    const actual=flt(kg.val());
    if (SIDE==="encoder" && actual>0 && expected>0 && actual<expected-0.005 && ["Saleable","Damaged"].includes(classification)){
      tr.find(".nkt-classification").addClass("nkt-settle-required");
    }else{
      tr.find(".nkt-classification").removeClass("nkt-settle-required");
    }
    const manualDed=Math.max(flt(tr.find(".nkt-manual-deduction").val()),0);
    let deduction=0;
    if(treatment==="Deduct Missing kg" && expected>0 && actual>0){
      deduction=full*Math.max(expected-actual,0)/expected;
    }else if(treatment==="Manual Deduction"){
      deduction=Math.min(manualDed,full);
    }
    const credit=Math.max(full-deduction,0);
    let note="Full original value";
    if(treatment==="Deduct Missing kg"){
      note=actual>0 && expected>0
        ? `Missing ${edit(Math.max(expected-actual,0))} kg • Deduct ${money(deduction)} • Credit ${money(credit)}`
        : "Enter Actual kg Returned";
    }else if(treatment==="Manual Deduction"){
      note=`Manual deduction ${money(deduction)} • Credit ${money(credit)}`;
    }
    tr.find(".nkt-value-note").text(note);
  }

  function sync_all_return_value_rows(st){
    role(st,"old-body").find("tr[data-old-row]").each(function(){sync_return_value_row(st,$(this));});
  }

  function return_row_credit(st,tr,src){
    const qty=Math.max(flt(tr.find(".nkt-return-qty").val()),0);
    const full=qty*Number(src.original_rate||0);
    const treatment=tr.find(".nkt-value-treatment").val()||"Full Value";
    if(treatment==="Manual Deduction"){
      return Math.max(full-Math.max(flt(tr.find(".nkt-manual-deduction").val()),0),0);
    }
    if(treatment==="Deduct Missing kg"){
      const expected=qty*Number(src.standard_sack_weight_kg||0);
      const actual=flt(tr.find(".nkt-actual-kg").val());
      if(expected>0 && actual>0)return Math.max(full*(Math.min(actual,expected)/expected),0);
    }
    return full;
  }

  function reset_settlement(st){
    st.paymentRows=[];
    role(st,"transaction-type").val("Return");
    role(st,"settlement-destination").val("None");
    role(st,"settlement-method").val("");
    role(st,"settlement-reference").val("");
    ["sum-return","sum-new","sum-pays","sum-refund","sum-credit"].forEach(n=>role(st,n).text("—"));
    role(st,"basis-note").text("Press F8 Preview before Submit.");
    render_payment_grid(st);
    conditional(st);
  }

  async function search_new_items(st,chooseFirst=false){
    const text=String(role(st,"new-item-entry").val()||"").trim();
    if(!text){role(st,"new-item-results").prop("hidden",true);return;}
    let wh="";
    if(SIDE==="encoder")wh=st.newRows.length?st.newRows[st.newRows.length-1].warehouse||"":role(st,"return-warehouse").val()||"";
    const r=await frappe.call({method:`${FAST}.search_items`,args:{search_text:text,warehouse:wh,limit:12}});
    st.newSearchResults=r.message||[];st.newSearchIndex=0;
    const exact=st.newSearchResults.findIndex(x=>String(x.item_code).toLowerCase()===text.toLowerCase()||String(x.item_name||"").toLowerCase()===text.toLowerCase());
    if(exact>=0){choose_new_item(st,exact);return;}
    if(chooseFirst&&st.newSearchResults.length===1){choose_new_item(st,0);return;}
    render_new_results(st);
  }

  function render_new_results(st){
    const box=role(st,"new-item-results");
    if(!st.newSearchResults.length){box.html('<div class="nkt-result">No saleable item found.</div>').prop("hidden",false);return;}
    box.html(st.newSearchResults.map((x,i)=>`<div class="nkt-result ${i===st.newSearchIndex?"active":""}" data-new-result="${i}">
      <b>${esc(x.item_code)}</b><small><span>${esc(x.item_name||"")} • ${money(x.standard_rate)}</span><span>${SIDE==="encoder"?`Stock ${edit(x.available_qty)}`:""}</span></small>
    </div>`).join("")).prop("hidden",false);
  }

  function new_item_keydown(st,e){
    if(!role(st,"new-item-results").prop("hidden")&&st.newSearchResults.length){
      if(e.key==="ArrowDown"){e.preventDefault();st.newSearchIndex=Math.min(st.newSearchIndex+1,st.newSearchResults.length-1);render_new_results(st);return;}
      if(e.key==="ArrowUp"){e.preventDefault();st.newSearchIndex=Math.max(st.newSearchIndex-1,0);render_new_results(st);return;}
      if(e.key==="Enter"){e.preventDefault();choose_new_item(st,st.newSearchIndex);return;}
      if(e.key==="Escape"){role(st,"new-item-results").prop("hidden",true);return;}
    }
    if(e.key==="Enter"){e.preventDefault();new_item_enter(st);}
  }

  function new_item_enter(st){const text=String(role(st,"new-item-entry").val()||"").trim();if(text)search_new_items(st,true);}

  function choose_new_item(st,i){
    const x=st.newSearchResults[i];if(!x)return;
    let defaultRate=Number(x.standard_rate||0),source="Current Selling Rate";
    const old=(st.detail?.items||[]).find(r=>r.item===x.item_code);
    if(old){defaultRate=Number(old.original_rate||0);source="Original Sale Rate";}
    st.newRows.push({item:x.item_code,item_name:x.item_name,uom:x.stock_uom,qty:1,rate:defaultRate,standard_rate:Number(x.standard_rate||0),rate_source:source,rateEdited:false,warehouse:""});
    role(st,"new-item-entry").val("");role(st,"new-item-results").prop("hidden",true);
    if(role(st,"transaction-type").val()!=="Exchange")role(st,"transaction-type").val("Exchange");
    invalidate(st);render_new_grid(st);focus_new_item(st);
  }

  function displayed_rate_source(st,r){
    if(r.rateEdited)return "Manual Rate";
    const old=(st.detail?.items||[]).find(x=>x.item===r.item);
    if(old && Math.abs(Number(r.rate||0)-Number(old.original_rate||0))<=0.005)return "Original Sale Rate";
    return "Current Selling Rate";
  }

  function render_new_grid(st,preserve=false,focusIndex=null,focusField=null){
    const body=role(st,"new-body"),colspan=SIDE==="encoder"?9:8;
    if(!st.newRows.length){body.html(`<tr class="nkt-empty"><td colspan="${colspan}">For a pure Return, leave NEW ORDER empty. For an Exchange, press F3 to add the replacement.</td></tr>`);update_local_total(st);return;}
    body.html(st.newRows.map((r,i)=>`<tr data-new-row="${i}" data-item="${attr(r.item)}" data-item-name="${attr(r.item_name||"")}" data-uom="${attr(r.uom||"")}" data-standard-rate="${attr(r.standard_rate||0)}" data-rate-source="${attr(r.rate_source||"")}">
      <td>${i+1}</td><td>${esc(r.item)}</td><td>${esc(r.item_name||"")}</td>
      <td><input class="nkt-qty" data-row="${i}" value="${attr(edit(r.qty))}"></td><td>${esc(r.uom||"")}</td>
      <td><input class="nkt-rate ${r.rateEdited?"nkt-rate-edited":""}" data-row="${i}" value="${attr(edit(r.rate))}"><div class="nkt-internal">${esc(displayed_rate_source(st,r))}</div></td>
      ${SIDE==="encoder"?`<td><select data-role="new-source-warehouse" data-row="${i}"><option value=""></option>${warehouse_options(st,r.warehouse)}</select></td>`:""}
      <td><b data-role="line-amount">${money(Number(r.qty||0)*Number(r.rate||0))}</b></td><td><button class="nkt-remove" data-row="${i}">×</button></td>
    </tr>`).join(""));
    update_local_total(st);
    if(preserve&&focusIndex!==null)setTimeout(()=>body.find(`.${focusField==="rate"?"nkt-rate":"nkt-qty"}[data-row="${focusIndex}"]`).trigger("focus").select(),0);
  }

  function payment_method_options(selected=""){
    const methods=["","Cash","Check","GCash","Maya","Bank Transfer","Online","Account"];
    return methods.map(x=>x
      ? `<option ${x===selected?"selected":""}>${x}</option>`
      : `<option value="" ${selected?"":"selected"}>Select method</option>`
    ).join("");
  }

  function focus_split_input(st,i=0,field="amount"){
    setTimeout(()=>{
      const sel=field==="method"
        ? `.nkt-pay-method[data-pay-row="${i}"]`
        : `.nkt-pay-amount[data-pay-row="${i}"]`;
      const x=role(st,"payment-body").find(sel);
      if(x.length){x.trigger("focus");if(field==="amount")x.select();}
    },0);
  }

  function add_payment_row(st,method=""){
    const v=local_values(st);
    if(v.customerPays<=0){
      frappe.show_alert({message:__("There is no customer balance to settle."),indicator:"orange"},4);
      return;
    }

    if(st.paymentRows.length===1){
      const first=st.paymentRows[0];
      first.auto_amount=false;
      st.paymentRows.push({
        method,
        amount:0,
        auto_amount:true,
        cash_tendered:0,
        auto_tendered:false,
        change:0,
        reference:"",
        provider:"",
        check_date:""
      });
      render_payment_grid(st);
      invalidate_payment_settlement(st);
      render_payment_totals(st);
      focus_split_input(st,0,"amount");
      frappe.show_alert({message:__("Split payment ON — enter the first Split Amount. The second row will balance automatically."),indicator:"blue"},4);
      return;
    }

    if(st.paymentRows.length>1){
      const zeroIndex=st.paymentRows.findIndex(r=>Number(r.amount||0)<=0.005);
      if(zeroIndex>=0){
        frappe.show_alert({message:__("Use the existing unused split row before adding another payment."),indicator:"orange"},4);
        focus_split_input(st,zeroIndex,st.paymentRows[zeroIndex].method?"amount":"method");
        return;
      }

      const settled=st.paymentRows.reduce((a,r)=>a+Number(r.amount||0),0);
      if(settled>=v.customerPays-0.005){
        frappe.show_alert({message:__("The Difference Due is already fully allocated. Edit an existing Split Amount instead of adding another row."),indicator:"orange"},5);
        return;
      }

      st.paymentRows.forEach(r=>r.auto_amount=false);
      const remaining=Math.max(v.customerPays-settled,0);
      st.paymentRows.push({
        method,
        amount:remaining,
        auto_amount:true,
        cash_tendered:0,
        auto_tendered:false,
        change:0,
        reference:"",
        provider:"",
        check_date:""
      });
      render_payment_grid(st);
      invalidate_payment_settlement(st);
      render_payment_totals(st);
      focus_split_input(st,st.paymentRows.length-1,"method");
      return;
    }

    st.paymentRows.push({
      method:method||"Cash",
      amount:v.customerPays,
      auto_amount:true,
      cash_tendered:0,
      auto_tendered:false,
      change:0,
      reference:"",
      provider:"",
      check_date:""
    });
    render_payment_grid(st);
    invalidate_payment_settlement(st);
  }

  function normalize_payment_row(st,i){
    const r=st.paymentRows[i];if(!r)return;
    if(r.method==="Cash"){
      r.reference="";r.provider="";r.check_date="";
      if(SIDE==="cashier"){
        r.auto_tendered=false;
        if(Number(r.cash_tendered||0)<0)r.cash_tendered=0;
      }
      r.change=SIDE==="cashier"?Math.max(Number(r.cash_tendered||0)-Number(r.amount||0),0):0;
    }else{
      r.cash_tendered=0;r.change=0;
      if(r.method==="Account"){r.reference="";r.provider="";r.check_date="";}
      else if(r.method!=="Check"){r.check_date="";}
    }
  }

  function render_payment_grid(st){
    const body=role(st,"payment-body"),head=role(st,"payment-head");
    if(!body.length)return;

    const split=st.paymentRows.length>1;
    const cashier=SIDE==="cashier";

    if(split){
      head.html(`<tr>
        <th style="width:12%">Method</th>
        <th style="width:12%">Split Amount</th>
        ${cashier?'<th style="width:13%">Cash Tendered</th><th style="width:10%">Change</th>':""}
        <th>Reference / Check No.</th><th>Bank / Provider</th><th>Check Date</th><th style="width:5%"></th>
      </tr>`);
      role(st,"payment-help").html(`Split payment is ON. Enter each <b>Split Amount</b>; the final balancing row follows the remaining Difference Due automatically. Repeated F11 will not create zero-value rows. ${cashier?"For Cash, Cash Tendered is required and Change is automatic.":"Encoder independently declares only the actual payment composition."}`);
    }else{
      head.html(`<tr>
        <th style="width:15%">Method</th>
        <th style="width:15%">Amount Applied</th>
        ${cashier?'<th style="width:16%">Cash Tendered</th><th style="width:12%">Change</th>':""}
        <th>Reference / Check No.</th><th>Bank / Provider</th><th>Check Date</th><th style="width:5%"></th>
      </tr>`);
      role(st,"payment-help").html(`<b>Amount Applied</b> is read-only and equals the full Difference Due for a single payment method. ${cashier?"For Cash, enter only <b>Cash Tendered</b>; Change is automatic.":"Encoder does not enter Cash Tendered or Change."} Use F11 only to split payment methods.`);
    }

    if(!st.paymentRows.length){
      body.html(`<tr class="nkt-empty"><td colspan="${cashier?8:6}">Choose/add a payment method. The full Difference Due will be applied automatically and shown as Amount Applied.</td></tr>`);
      render_payment_totals(st);
      return;
    }

    body.html(st.paymentRows.map((r,i)=>{
      const isCash=r.method==="Cash",isCheck=r.method==="Check",isAccount=r.method==="Account";
      const amountCell=split
        ? `<td><input class="nkt-pay-amount" data-pay-row="${i}" type="number" step="0.01" value="${attr(edit(r.amount))}"></td>`
        : `<td class="nkt-applied-cell"><b class="nkt-pay-applied" data-pay-row="${i}">${money(r.amount)}</b><div class="nkt-internal">Read-only</div></td>`;
      return `<tr>
        <td><select class="nkt-pay-method" data-pay-row="${i}">${payment_method_options(r.method)}</select></td>
        ${amountCell}
        ${cashier?`<td><input class="nkt-pay-cash" data-pay-row="${i}" type="number" step="0.01" min="0" placeholder="${isCash?"Enter actual cash received":""}" value="${Number(r.cash_tendered||0)>0?attr(edit(r.cash_tendered)):""}" ${isCash?"":"disabled"}></td><td><b data-role="pay-change-cell">${money(r.change||0)}</b></td>`:""}
        <td><input class="nkt-pay-reference" data-pay-row="${i}" value="${attr(r.reference||"")}" ${isCash||isAccount?"disabled":""} placeholder="${isCheck?"Check no.":"Reference"}"></td>
        <td><input class="nkt-pay-provider" data-pay-row="${i}" value="${attr(r.provider||"")}" ${isCash||isAccount?"disabled":""} placeholder="${isCheck?"Issuing bank":"Bank / provider"}"></td>
        <td><input class="nkt-pay-check-date" data-pay-row="${i}" type="date" value="${attr(r.check_date||"")}" ${isCheck?"":"disabled"}></td>
        <td><button class="nkt-pay-remove" data-pay-row="${i}">×</button></td>
      </tr>`;
    }).join(""));

    render_payment_totals(st);
  }

  function sync_auto_payment_rows(st){
    const v=local_values(st);
    if(v.customerPays<=0 || !st.paymentRows.length)return;

    if(st.paymentRows.length===1){
      const r=st.paymentRows[0];
      r.amount=v.customerPays;
      r.auto_amount=true;
      const applied=role(st,"payment-body").find('.nkt-pay-applied[data-pay-row="0"]');
      if(applied.length)applied.text(money(r.amount));

      if(r.method==="Cash" && SIDE==="cashier"){
        r.auto_tendered=false;
        r.change=Math.max(Number(r.cash_tendered||0)-Number(r.amount||0),0);
      }
      return;
    }

    const autoIndexes=[];
    st.paymentRows.forEach((r,i)=>{if(r.auto_amount)autoIndexes.push(i);});
    if(autoIndexes.length>1){
      autoIndexes.slice(0,-1).forEach(i=>st.paymentRows[i].auto_amount=false);
    }
    const autoIndex=autoIndexes.length?autoIndexes[autoIndexes.length-1]:-1;
    if(autoIndex>=0){
      const other=st.paymentRows.reduce((a,r,i)=>i===autoIndex?a:a+Number(r.amount||0),0);
      const r=st.paymentRows[autoIndex];
      r.amount=Math.max(v.customerPays-other,0);
      if(r.method==="Cash" && SIDE==="cashier"){
        r.change=Math.max(Number(r.cash_tendered||0)-Number(r.amount||0),0);
      }else if(r.method!=="Cash"){
        r.cash_tendered=0;r.change=0;
      }
      const input=role(st,"payment-body").find(`.nkt-pay-amount[data-pay-row="${autoIndex}"]`);
      if(input.length && document.activeElement!==input[0])input.val(edit(r.amount));
      const tr=input.closest("tr");
      if(tr.length)tr.find('[data-role="pay-change-cell"]').text(money(r.change||0));
    }
  }

  function payment_rows_total(st){return st.paymentRows.reduce((a,r)=>a+Number(r.amount||0),0);}

  function render_payment_totals(st){
    const v=local_values(st);
    if(v.customerPays>0)sync_auto_payment_rows(st);

    const settled=payment_rows_total(st);

    if(SIDE==="cashier"){
      const cashRow=st.paymentRows.find(r=>r.method==="Cash");
      const strip=role(st,"cash-change-strip");
      if(cashRow){
        strip.prop("hidden",false);
        role(st,"cash-due-summary").text(money(Number(cashRow.amount||0)));
        const tendered=Number(cashRow.cash_tendered||0);
        role(st,"cash-tender-summary").text(tendered>0?money(tendered):"—");
        role(st,"cash-change-summary").text(tendered>0?money(Number(cashRow.change||0)):"—");
      }else{
        strip.prop("hidden",true);
      }
    }

    const delta=Number(v.customerPays)-settled;
    const under=delta>0.01;
    const over=delta<-0.01;
    const exact=!under&&!over;

    role(st,"pay-return-credit").text(money(Math.min(v.returnCredit,v.newValue)));
    role(st,"pay-due").text(money(v.customerPays));
    role(st,"pay-settled").text(money(settled));

    const bal=role(st,"pay-balance");
    const label=role(st,"pay-balance-label");
    bal.removeClass("nkt-overpay-amount nkt-balance-amount nkt-zero-balance");

    if(under){
      label.text("Balance Remaining");
      bal.text(money(delta)).addClass("nkt-balance-amount");
    }else if(over){
      label.text("OVERPAYMENT");
      bal.text(money(Math.abs(delta))).addClass("nkt-overpay-amount");
    }else{
      label.text("Balance");
      bal.text(money(0)).addClass("nkt-zero-balance");
    }

    const rowsValid=validate_payment_rows_local(st,false);
    const ok=v.customerPays>0 && exact && rowsValid;
    const status=ok?"PAYMENT OK":(over?"OVERPAYMENT":"NOT SETTLED");
    role(st,"pay-status").text(status)
      .toggleClass("nkt-pay-ok",ok)
      .toggleClass("nkt-not-ok",!ok);
  }

  function local_return_basis(st,v=null){
    v=v||local_values(st);
    const total=Number(st.detail?.total||0);
    if(total<=0)return {money:0,account:v.returnCredit};
    const moneyShare=Math.max(Number(st.detail?.money_basis||0),0)/total;
    const moneyBasis=Math.min(v.returnCredit*moneyShare,v.returnCredit);
    return {money:moneyBasis,account:Math.max(v.returnCredit-moneyBasis,0)};
  }

  function render_refund_options(st){
    const v=local_values(st),basis=local_return_basis(st,v);
    const select=role(st,"settlement-destination");
    const refundOpt=select.find('option[value="Refund Money"]');
    refundOpt.prop("disabled",basis.money<=0.005);
    refundOpt.text(basis.money<=0.005?"Refund Money — unavailable (₱0 refundable)":"Refund Money");
    if(basis.money<=0.005 && select.val()==="Refund Money")select.val("Account Adjustment");
    if(v.dueBack>0 && select.val()==="None"){
      select.val(basis.money<=0.005?"Account Adjustment":"Refund Money");
    }
    role(st,"refund-cap").text(money(Math.min(v.dueBack,basis.money)));
    role(st,"refund-due").text(money(v.dueBack));
    if(!st.preview){
      role(st,"refund-actual").text("—");role(st,"refund-account").text("—");role(st,"refund-credit").text("—");
      role(st,"refund-status").text("PRESS F8").removeClass("nkt-refund-ok").addClass("nkt-not-ok");
    }
  }

  function local_values(st){
    let returnCredit=0;
    if(st.detail){
      role(st,"old-body").find("[data-old-row]").each(function(){
        const tr=$(this),i=Number(tr.attr("data-old-row")),src=st.detail.items[i];
        returnCredit += return_row_credit(st,tr,src);
      });
    }
    const newValue=st.newRows.reduce((a,r)=>a+Number(r.qty||0)*Number(r.rate||0),0);
    return {
      returnCredit,
      newValue,
      customerPays:Math.max(newValue-returnCredit,0),
      dueBack:Math.max(returnCredit-newValue,0)
    };
  }

  function update_local_total(st){
    const v=local_values(st);
    if(!st.preview){
      role(st,"sum-return").text(money(v.returnCredit));
      role(st,"sum-new").text(money(v.newValue));
      role(st,"sum-pays").text(money(v.customerPays)).toggleClass("nkt-collect-amount",v.customerPays>0);
      role(st,"sum-refund").text(v.dueBack>0?"—":money(0)).removeClass("nkt-return-amount");
      role(st,"sum-credit").text(v.dueBack>0?"—":money(0));
    }

    if(v.customerPays>0){
      role(st,"settlement-direction").removeClass("nkt-direction-return").addClass("nkt-direction-collect").html(`<b>CUSTOMER OWES <span class="nkt-collect-amount">${money(v.customerPays)}</span></b> after Return Credit.`);
      role(st,"basis-note").html(`<b>PAYMENT SETTLEMENT:</b> Return Credit covers ${money(Math.min(v.returnCredit,v.newValue))}; settle the remaining ${money(v.customerPays)} below.`);
      if(!st.paymentRows.length)add_payment_row(st,"Cash");
    }else if(v.dueBack>0){
      const basis=local_return_basis(st,v);
      role(st,"settlement-direction").removeClass("nkt-direction-collect").addClass("nkt-direction-return").html(`<b>AMOUNT DUE BACK <span class="nkt-return-amount">${money(v.dueBack)}</span></b> to the customer.`);
      role(st,"basis-note").html(`Returned value basis: ${money(basis.money)} real money + ${money(basis.account)} account/credit. Actual-money refund cannot exceed ${money(basis.money)}.`);
      render_refund_options(st);
    }else{
      role(st,"settlement-direction").removeClass("nkt-direction-collect nkt-direction-return").html("<b>No price difference.</b>");
      role(st,"basis-note").text("SETTLEMENT OK — no money difference. Press F8 to validate.");
    }
    conditional(st);
    render_payment_totals(st);
  }

  async function search_filter_items(st){
    const text=String(st.wrapper.find('[data-f="item"]').val()||"").trim();
    if(!text){role(st,"filter-item-results").prop("hidden",true);return;}
    const r=await frappe.call({method:`${FAST}.search_items`,args:{search_text:text,warehouse:"",limit:10}});
    st.filterItemResults=r.message||[];st.filterItemIndex=0;render_filter_results(st);
  }

  function render_filter_results(st){
    const box=role(st,"filter-item-results");
    if(!st.filterItemResults.length){box.html('<div class="nkt-result">No item found.</div>').prop("hidden",false);return;}
    box.html(st.filterItemResults.map((x,i)=>`<div class="nkt-result ${i===st.filterItemIndex?"active":""}" data-filter-result="${i}"><b>${esc(x.item_code)}</b><small><span>${esc(x.item_name||"")}</span><span>${money(x.standard_rate)}</span></small></div>`).join("")).prop("hidden",false);
  }

  function filter_item_keydown(st,e){
    if(!role(st,"filter-item-results").prop("hidden")&&st.filterItemResults.length){
      if(e.key==="ArrowDown"){e.preventDefault();st.filterItemIndex=Math.min(st.filterItemIndex+1,st.filterItemResults.length-1);render_filter_results(st);return;}
      if(e.key==="ArrowUp"){e.preventDefault();st.filterItemIndex=Math.max(st.filterItemIndex-1,0);render_filter_results(st);return;}
      if(e.key==="Enter"){e.preventDefault();choose_filter_item(st,st.filterItemIndex);return;}
      if(e.key==="Escape"){role(st,"filter-item-results").prop("hidden",true);return;}
    }
    if(e.key==="Enter"){e.preventDefault();search_old(st);}
  }

  function choose_filter_item(st,i){
    const x=st.filterItemResults[i];if(!x)return;
    st.selectedFilterItem=x.item_code;
    st.wrapper.find('[data-f="item"]').val(x.item_name||x.item_code);
    role(st,"filter-item-results").prop("hidden",true);
  }

  function sync_new_rows_from_dom(st){
    const domRows=[];
    role(st,"new-body").find("tr[data-new-row]").each(function(index){
      const tr=$(this);
      const oldState=st.newRows[index]||{};
      const item=String(tr.attr("data-item")||oldState.item||"").trim();
      if(!item)return;

      domRows.push({
        item,
        item_name:String(tr.attr("data-item-name")||oldState.item_name||""),
        uom:String(tr.attr("data-uom")||oldState.uom||""),
        qty:flt(tr.find(".nkt-qty").val()),
        rate:flt(tr.find(".nkt-rate").val()),
        standard_rate:flt(tr.attr("data-standard-rate")||oldState.standard_rate||0),
        rate_source:String(tr.attr("data-rate-source")||oldState.rate_source||""),
        rateEdited:tr.find(".nkt-rate").hasClass("nkt-rate-edited") || Boolean(oldState.rateEdited),
        warehouse:SIDE==="encoder"
          ? String(tr.find('[data-role="new-source-warehouse"]').val()||oldState.warehouse||"")
          : ""
      });
    });

    // The visible grid is the source of truth at Preview/Submit time.
    // This specifically protects against a stale JS state object while the
    // operator can still visibly see the NEW ORDER row on screen.
    if(domRows.length){
      st.newRows=domRows;
    }
    return st.newRows;
  }

  function build_payload(st){
    if(!st.detail)throw new Error("Select an OLD ORDER first.");

    sync_new_rows_from_dom(st);

    const returned=[];
    role(st,"old-body").find("[data-old-row]").each(function(){
      const tr=$(this),i=Number(tr.attr("data-old-row")),src=st.detail.items[i],qty=flt(tr.find(".nkt-return-qty").val());
      if(qty<=0)return;
      const treatment=tr.find(".nkt-value-treatment").val()||"Full Value";
      const actualKg=flt(tr.find(".nkt-actual-kg").val());
      returned.push({
        item:src.item,quantity:qty,original_source_warehouse:src.source_warehouse||"",
        classification:SIDE==="encoder"?tr.find(".nkt-classification").val():"",
        actual_kg_returned:actualKg,
        return_value_treatment:treatment,
        manual_deduction:treatment==="Manual Deduction"?flt(tr.find(".nkt-manual-deduction").val()):0
      });
    });

    const transactionType=role(st,"transaction-type").val()||"Return";
    if(transactionType==="Exchange" && !st.newRows.length){
      role(st,"new-item-entry").addClass("nkt-settle-required").trigger("focus");
      frappe.show_alert({
        message:__("NEW ORDER is visible/required for an Exchange. Re-add the replacement item if the grid is empty."),
        indicator:"orange"
      },6);
      throw new Error("Exchange NEW ORDER payload is empty.");
    }

    return {
      source_name:st.detail.name,
      transaction_type:transactionType,
      settlement_destination:role(st,"settlement-destination").val()||"None",
      settlement_method:role(st,"settlement-method").val()||"",
      settlement_reference:role(st,"settlement-reference").val()||"",
      settlement_payments:st.paymentRows.map(r=>({
        payment_method:r.method,amount:r.amount,
        cash_tendered:SIDE==="cashier"?(r.cash_tendered||0):0,
        change_amount:SIDE==="cashier"?(r.change||0):0,
        reference_number:r.reference||"",bank_or_provider:r.provider||"",check_date:r.check_date||""
      })),
      return_warehouse:SIDE==="encoder"?(role(st,"return-warehouse").val()||""):"",
      returned_items:returned,
      new_items:st.newRows.map(r=>({
        item:r.item,
        quantity:r.qty,
        rate:r.rate,
        source_warehouse:SIDE==="encoder"?(r.warehouse||""):""
      }))
    };
  }

  function collect(st){
    return build_payload(st);

  }

  async function preview(st){
    if(!validate_settlement_choice(st))return null;
    let payload;
    try{
      payload=build_payload(st);
    }catch(err){
      console.warn("NKT C7 payload build stopped:",err);
      return null;
    }
    const r=await frappe.call({method:`${MATCHING}.preview_payload`,type:"POST",args:{side:SIDE,payload:JSON.stringify(payload)},freeze:true,freeze_message:__("Checking return/exchange...")});
    st.preview=r.message||{};
    st.previewPayload=JSON.parse(JSON.stringify(payload));
    (st.preview.returned_items||[]).forEach((x,i)=>{
      const tr=role(st,"old-body").find(`tr[data-old-row="${i}"]`);
      if(!tr.length)return;
      let note=x.return_value_treatment||"Full Value";
      if(note==="Deduct Missing kg"){
        note=`Missing ${edit(x.missing_kg||0)} kg • Deduct ${money(x.value_deduction||0)} • Credit ${money(x.credit_amount||0)}`;
      }else if(note==="Manual Deduction"){
        note=`Manual deduction ${money(x.value_deduction||0)} • Credit ${money(x.credit_amount||0)}`;
      }else{
        note=`Full value • Credit ${money(x.credit_amount||0)}`;
      }
      if(SIDE==="encoder" && Number(x.business_absorbed_value||0)>0){
        note += ` • Business absorbs ${money(x.business_absorbed_value)}`;
      }
      tr.find(".nkt-value-note").text(note);
    });
    role(st,"sum-return").text(money(st.preview.return_credit));
    role(st,"sum-new").text(money(st.preview.new_order_value));
    role(st,"sum-pays").text(money(st.preview.customer_pays));
    role(st,"sum-refund").text(money(st.preview.refund_money)).toggleClass("nkt-return-amount",Number(st.preview.refund_money||0)>0);
    role(st,"sum-credit").text(money(st.preview.credit_adjustment));
    role(st,"basis-note").text(`Returned value basis: ${money(st.preview.return_money_basis)} real money + ${money(st.preview.return_account_basis)} account/credit. Actual refund cap: ${money(st.preview.return_money_basis)}.`);
    if(Number(st.preview.customer_pays||0)>0){
      role(st,"pay-status").text(st.preview.settlement_status||"PAYMENT OK").removeClass("nkt-not-ok").addClass("nkt-pay-ok");
      render_payment_totals(st);
    }else if(Math.max(Number(st.preview.return_credit||0)-Number(st.preview.new_order_value||0),0)>0){
      role(st,"refund-actual").text(money(st.preview.refund_money));
      role(st,"refund-account").text(money(st.preview.account_adjustment_amount));
      role(st,"refund-credit").text(money(st.preview.customer_credit_amount));
      const refundStatus=Number(st.preview.refund_money||0)>0 ? "REFUND OK" : "SETTLEMENT OK";
      role(st,"refund-status").text(refundStatus).removeClass("nkt-not-ok").addClass("nkt-refund-ok");
      role(st,"refund-actual").toggleClass("nkt-return-amount",Number(st.preview.refund_money||0)>0);
    }
    conditional(st);
    frappe.show_alert({message:__(st.preview.settlement_status||"Settlement OK"),indicator:"green"});
    return st.preview;
  }

  function conditional(st){
    const v=local_values(st);
    role(st,"payment-section").prop("hidden",v.customerPays<=0);
    role(st,"refund-section").prop("hidden",v.dueBack<=0);

    if(v.customerPays>0){
      role(st,"settlement-destination").val("None");
      role(st,"settlement-method").val("");
      role(st,"settlement-reference").val("");
    }else{
      st.paymentRows=[];
      render_payment_grid(st);
    }

    if(v.dueBack>0){
      render_refund_options(st);
      const actualRefund=st.preview?Number(st.preview.refund_money||0):0;
      const needsRefundMethod=role(st,"settlement-destination").val()==="Refund Money" && (st.preview?actualRefund>0:local_return_basis(st,v).money>0.005);
      role(st,"refund-method-control").prop("hidden",!needsRefundMethod);
      role(st,"refund-reference-control").prop("hidden",!needsRefundMethod);
    }else{
      role(st,"refund-method-control").prop("hidden",true);
      role(st,"refund-reference-control").prop("hidden",true);
    }
  }

  function validate_payment_rows_local(st,show=true){
    const v=local_values(st);
    if(v.customerPays<=0)return true;
    if(!st.paymentRows.length){
      if(show)frappe.show_alert({message:__("Add at least one payment row."),indicator:"orange"},5);
      return false;
    }
    let total=0,cashCount=0;
    for(let i=0;i<st.paymentRows.length;i++){
      const r=st.paymentRows[i],n=i+1;
      if(!r.method||Number(r.amount||0)<=0){
        if(show)frappe.show_alert({message:__(`Payment row ${n} needs a method and positive amount.`),indicator:"orange"},5);
        return false;
      }
      if(r.method==="Cash"){
        cashCount++;
        if(cashCount>1){if(show)frappe.show_alert({message:__("Only one Cash row is allowed."),indicator:"orange"},5);return false;}
        if(SIDE==="cashier" && Number(r.cash_tendered||0)<=0){
          if(show){
            frappe.show_alert({message:__(`Enter the actual Cash Tendered on row ${n}.`),indicator:"orange"},6);
            role(st,"payment-body").find(`.nkt-pay-cash[data-pay-row="${i}"]`).addClass("nkt-settle-required").trigger("focus");
          }
          return false;
        }
        if(SIDE==="cashier" && Number(r.cash_tendered||0)+0.005<Number(r.amount||0)){
          if(show)frappe.show_alert({message:__(`Cash Tendered is less than Cash Due on row ${n}.`),indicator:"orange"},5);
          return false;
        }
      }else if(r.method==="Check"){
        if(!String(r.reference||"").trim()||!String(r.provider||"").trim()||!r.check_date){
          if(show)frappe.show_alert({message:__(`Check row ${n} requires Check Number, Issuing Bank and Check Date.`),indicator:"orange"},5);
          return false;
        }
      }else if(!["Account"].includes(r.method) && !String(r.reference||"").trim()){
        if(show)frappe.show_alert({message:__(`${r.method} row ${n} requires a reference.`),indicator:"orange"},5);
        return false;
      }
      total+=Number(r.amount||0);
    }
    if(Math.abs(total-v.customerPays)>0.01){
      if(show)frappe.show_alert({message:__(`Payment rows ${money(total)} must equal the difference due ${money(v.customerPays)}.`),indicator:"orange"},5);
      return false;
    }
    return true;
  }

  function validate_settlement_choice(st){
    const v=local_values(st);
    if(v.customerPays>0){
      return validate_payment_rows_local(st,true);
    }
    if(v.dueBack>0){
      render_refund_options(st);
      const dest=role(st,"settlement-destination").val();
      if(!dest||dest==="None"){
        role(st,"settlement-destination").addClass("nkt-settle-required").trigger("focus");
        frappe.show_alert({message:__("Choose Refund Money, Customer Credit, or Account Adjustment."),indicator:"orange"},5);
        return false;
      }
      const basis=local_return_basis(st,v);
      if(dest==="Refund Money" && basis.money<=0.005){
        role(st,"settlement-destination").val("Account Adjustment").trigger("focus");
        frappe.show_alert({message:__("This returned value has no refundable real-money basis. Use Account Adjustment or Customer Credit."),indicator:"orange"},6);
        return false;
      }
      if(dest==="Refund Money" && !role(st,"settlement-method").val()){
        role(st,"settlement-method").addClass("nkt-settle-required").trigger("focus");
        frappe.show_alert({message:__("Choose the Refund Money Method."),indicator:"orange"},5);
        return false;
      }
      if(dest==="Refund Money" && ["Check","GCash","Maya","Bank Transfer","Online"].includes(role(st,"settlement-method").val()) && !String(role(st,"settlement-reference").val()||"").trim()){
        role(st,"settlement-reference").addClass("nkt-settle-required").trigger("focus");
        frappe.show_alert({message:__("Enter the refund reference."),indicator:"orange"},5);
        return false;
      }
    }
    return true;
  }

  function invalidate(st){
    st.preview=null;
    st.previewPayload=null;
    st.submitRequestId=null;
    role(st,"pay-status").text("NOT SETTLED").removeClass("nkt-pay-ok").addClass("nkt-not-ok");
    role(st,"refund-status").text("NOT SETTLED").removeClass("nkt-refund-ok").addClass("nkt-not-ok");
    role(st,"sum-pays").removeClass("nkt-collect-amount");
    role(st,"sum-refund").removeClass("nkt-return-amount");
    update_local_total(st);
  }

  function submit_request_id(st){
    if(!st.submitRequestId){
      const uuid=(globalThis.crypto&&typeof globalThis.crypto.randomUUID==="function")
        ? globalThis.crypto.randomUUID()
        : `${Date.now()}-${Math.random().toString(36).slice(2)}-${Math.random().toString(36).slice(2)}`;
      st.submitRequestId=`nkt-rx-${SIDE}-${uuid}`;
    }
    return st.submitRequestId;
  }

  async function submit(st){
    const p=await preview(st);
    if(!p)return;
    const text=SIDE==="cashier"
      ? `Submit this ${esc(p.transaction_type)} on the Cashier side? The Cashier-side NEW SALE and any actual money/refund post now. Encoder remains independent and reconciliation happens afterward.`
      : `Submit this ${esc(p.transaction_type)} on the Encoder side? Official return stock, NEW ORDER and Account/Credit effects post now. Cashier remains independent and reconciliation happens afterward.`;
    const submitPayload=JSON.parse(JSON.stringify(st.previewPayload||build_payload(st)));
    submitPayload.submit_request_id=submit_request_id(st);
    frappe.confirm(text,async()=>{
      const r=await frappe.call({method:`${MATCHING}.submit_from_payload`,type:"POST",args:{side:SIDE,payload:JSON.stringify(submitPayload)},freeze:true,freeze_message:__("Submitting return/exchange...")});
      const x=r.message||{};
      const sidePosted=x.posting_status==="Posted";
      if(x.idempotent_replay){frappe.show_alert({message:__("Repeated submit reused the original Return/Exchange — no duplicate was created."),indicator:"blue"},6);}
      frappe.msgprint({
        title:__("Return / Exchange"),
        indicator:x.status==="Matched"?"green":(sidePosted?"blue":"orange"),
        message:x.status==="Matched"
          ? `${SCREEN} side posted and reconciled with the independent counterpart.`
          : (sidePosted
              ? `${SCREEN} side POSTED. Waiting only for independent reconciliation.`
              : `${SCREEN} declaration recorded; posting is not complete.`)
      });
      clear_work(st);await load_recent(st);await search_old(st);
    });
  }

  async function load_recent(st){
    const r=await frappe.call({method:`${MATCHING}.get_own_recent`,args:{side:SIDE,limit:30}});
    const rows=r.message||[];
    role(st,"recent").html(`<div class="nkt-finder-title"><strong>MY RECENT RETURN / EXCHANGE ENTRIES</strong></div><table class="nkt-search-table"><thead><tr><th>Time</th><th>Customer</th><th>Type</th><th>Return Credit</th><th>NEW ORDER</th><th>Settlement</th><th>Status</th></tr></thead><tbody>${rows.map(x=>`<tr><td>${esc(x.entry_datetime)}</td><td>${esc(x.customer_name)}</td><td>${esc(x.transaction_type)}</td><td>${money(x.return_credit)}</td><td>${money(x.new_order_value)}</td><td>${x.customer_pays?`Pays ${money(x.customer_pays)} · ${esc(x.customer_pays_mode||"")}`:(x.refund_money?`Refund ${money(x.refund_money)}`:`Credit/Adj ${money((x.account_adjustment_amount||0)+(x.customer_credit_amount||0))}`)}</td><td><b>${esc(x.reconciliation_status)}</b>${x.posting_status?` · ${esc(x.posting_status)}`:""}<div class="nkt-internal">${esc(x.name)}</div></td></tr>`).join("")}</tbody></table>`);
  }

  function clear_work(st){st.detail=null;st.newRows=[];st.paymentRows=[];st.preview=null;st.previewPayload=null;st.submitRequestId=null;role(st,"work").prop("hidden",true);role(st,"placeholder").prop("hidden",false);role(st,"new-item-results").prop("hidden",true);}

  function focus_customer(st){setTimeout(()=>st.wrapper.find('[data-f="customer"]').trigger("focus").select(),0);}
  function focus_new_item(st){if(!st.detail)return;setTimeout(()=>role(st,"new-item-entry").trigger("focus").select(),0);}
  function focus_return_qty(st){setTimeout(()=>role(st,"old-body").find(".nkt-return-qty").first().trigger("focus").select(),0);}
})();

/* ===== END SOURCE: NKT Cashier Return Exchange V2.0C.7.12A-C.1 ===== */
