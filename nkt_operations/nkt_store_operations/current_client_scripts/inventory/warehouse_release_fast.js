/* NKT CURRENT CLIENT SCRIPT — NKT Warehouse Release Fast Screen — Form
 * Consolidated from the accepted live site on 2026-08-27.
 * Source order exactly preserves Frappe Client Script creation order.
 * Edit this file; do not create phase/recovery Client Script records.
 */

/* ===== SOURCE: NKT Warehouse Release Fast Screen V2.0C.4.2.2 ===== */
(() => {
  const DOCTYPE = 'NKT Warehouse Release Fast Screen';
  const NS = 'nktWarehouseReleaseC422';
  const API = 'nkt_operations.nkt_store_operations.features.inventory.warehouse_release_fast_sync';

  frappe.ui.form.on(DOCTYPE, {
    refresh(frm) { render_screen(frm); }
  });

  function render_screen(frm) {
    frm.disable_save();
    frm.page.clear_actions();
    frm.page.set_title(__('NKT Warehouse Release'));
    const wrapper = frm.fields_dict.screen_html.$wrapper;
    prepare_layout(wrapper);
    wrapper.empty().html(markup());
    install_css();
    const state = {
      frm, wrapper, boot: null, queue: [], selected: null,
      requestId: uuid(), finalizing: false, statusTimer: null, lastSuccessAt: 0,
      securityMode:'normal', securityBusy:false, terminalLocked:false, localRestrictionLatch:false
    };
    bind(state);
    initialize_security(state).then(ok=>{
      if(ok&&state.securityMode!=='limited'&&!state.terminalLocked)refresh_queue(state,true);
    });
  }

  function prepare_layout(wrapper) {
    const layout = wrapper.closest('.form-layout');
    layout.find('.layout-side-section').hide();
    layout.find('.layout-main-section-wrapper').css({ width: '100%', maxWidth: 'none', flex: '1 1 100%' });
    wrapper.closest('.layout-main-section').css({ maxWidth: 'none' });
  }

  function markup() {
    return `
      <div class="nkt-rel-shell" tabindex="0">
        <div class="nkt-rel-titlebar">
          <div><strong>NKT Warehouse Release</strong><span>External Warehouse Dispatch</span></div>
          <div class="nkt-rel-badge">LIVE — SAFE RELEASE + PARTIAL RECALL</div>
        </div>
        <div class="nkt-rel-context">
          <span>Operator: <b data-role="operator">Loading…</b></span>
          <span>Open Release Queue: <b data-role="queue-count">0</b></span>
          <button type="button" data-action="refresh">Refresh Queue</button>
        </div>
        <div class="nkt-rel-body">
          <div class="nkt-rel-queue-panel">
            <div class="nkt-panel-title">Pending Warehouse Releases</div>
            <div data-role="queue" class="nkt-rel-queue"><div class="nkt-empty">Loading…</div></div>
          </div>
          <div class="nkt-rel-detail-panel">
            <div data-role="empty-detail" class="nkt-detail-empty">Select a pending release from the left.</div>
            <div data-role="detail" hidden>
              <div class="nkt-detail-head">
                <div><span>Release</span><b data-role="release-name"></b></div>
                <div><span>Order</span><b data-role="order-name"></b></div>
                <div><span>Customer</span><b data-role="customer-name"></b></div>
                <div><span>Warehouse</span><b data-role="warehouse-name"></b></div>
              </div>
              <div class="nkt-release-fields">
                <label>Release Authorization Reference<input data-role="reference" autocomplete="off"></label>
                <label>Driver <span style="font-weight:normal;color:#666">(Optional)</span><input data-role="driver" autocomplete="off"></label>
                <label>Plate Number <span style="font-weight:normal;color:#666">(Optional)</span><input data-role="plate" autocomplete="off"></label>
              </div>
              <table class="nkt-rel-table">
                <thead><tr><th>Item</th><th>Description</th><th>UOM</th><th>Ordered</th><th>Released Before</th><th>Remaining</th><th>Release Now</th></tr></thead>
                <tbody data-role="items"></tbody>
              </table>
              <div class="nkt-rel-summary">
                <span>Release Now Total</span><b data-role="release-total">0</b>
              </div>
              <div class="nkt-rel-note">Enter only the quantity physically leaving this warehouse now. Partial releases are allowed. Remaining quantity stays reserved for the next controlled release.</div>
              <div class="nkt-recall-action" data-role="recall-primary" hidden>
                <div><b>Recall Pending</b><br><span>Confirm only after warehouse staff verifies that nothing on this prepared release physically left the warehouse.</span></div>
                <button type="button" data-action="confirm-recall" class="primary">Confirm Recall — Nothing Released</button>
              </div>
            </div>
          </div>
        </div>
        <div class="nkt-rel-actionbar">
          <span>F5 Refresh • F10 Confirm & Print • F12 Confirm Release</span>
          <div class="spacer"></div>
          <button type="button" data-action="f10"><b>F10</b> Confirm & Print</button>
          <button type="button" data-action="confirm-recall" class="primary" hidden>Confirm Recall — Nothing Released</button>
          <button type="button" data-action="f12" class="primary"><b>F12</b> Confirm Release</button>
        </div>
      </div>`;
  }

  function install_css() {
    if (document.getElementById('nkt-rel-c41-css')) return;
    const style = document.createElement('style');
    style.id = 'nkt-rel-c41-css';
    style.textContent = `
      .nkt-rel-shell{font-family:Tahoma,Arial,sans-serif;font-size:13px;color:#111;background:#d8d8d8;border:1px solid #777;min-height:calc(100vh - 150px);display:flex;flex-direction:column}
      .nkt-rel-titlebar{display:flex;justify-content:space-between;align-items:center;padding:8px 10px;background:linear-gradient(#fafafa,#c8c8c8);border-bottom:1px solid #777;font-size:18px}.nkt-rel-titlebar span{font-size:12px;font-weight:normal;margin-left:12px}.nkt-rel-badge{font-size:11px;padding:4px 8px;background:#e5f3ff;border:1px solid #5b7f9e}
      .nkt-rel-context{display:flex;gap:25px;align-items:center;padding:6px 10px;background:#eee;border-bottom:1px solid #999}.nkt-rel-context button{margin-left:auto}
      .nkt-rel-body{display:grid;grid-template-columns:minmax(330px,36%) 1fr;gap:8px;flex:1;padding:8px;min-height:520px}.nkt-rel-queue-panel,.nkt-rel-detail-panel{background:#fff;border:1px solid #888;overflow:auto}.nkt-panel-title{font-weight:bold;padding:8px;background:linear-gradient(#f4f4f4,#d6d6d6);border-bottom:1px solid #888}
      .nkt-rel-queue-row{padding:8px;border-bottom:1px solid #ddd;cursor:pointer}.nkt-rel-queue-row:hover,.nkt-rel-queue-row.active{background:#d9ecff}.nkt-rel-queue-row strong{display:flex;justify-content:space-between}.nkt-rel-queue-row small{display:block;color:#555;margin-top:3px}.nkt-empty,.nkt-detail-empty{padding:40px;text-align:center;color:#666}
      .nkt-detail-head{display:grid;grid-template-columns:repeat(2,1fr);gap:6px;padding:10px;border-bottom:1px solid #aaa;background:#f5f5f5}.nkt-detail-head div{display:flex;gap:7px}.nkt-detail-head span{color:#555;min-width:75px}.nkt-release-fields{display:grid;grid-template-columns:1.3fr 1fr 1fr;gap:8px;padding:10px}.nkt-release-fields label{font-weight:bold}.nkt-release-fields input{display:block;width:100%;height:29px;border:1px solid #777;padding:3px 6px;margin-top:3px}
      .nkt-rel-table{width:100%;border-collapse:collapse;table-layout:fixed}.nkt-rel-table th,.nkt-rel-table td{border:1px solid #bbb;padding:6px;text-align:left}.nkt-rel-table th{background:linear-gradient(#f3f3f3,#d5d5d5)}.nkt-rel-table input{width:100%;height:28px;border:1px solid #666;padding:3px 6px;font-weight:bold}.nkt-rel-table td:nth-child(n+4){text-align:right}.nkt-rel-summary{display:flex;justify-content:flex-end;gap:15px;padding:10px 14px;font-size:17px;border-top:1px solid #888}.nkt-rel-summary b{min-width:90px;text-align:right}.nkt-rel-note{margin:0 10px 10px;padding:7px;border:1px solid #a98b2a;background:#fff7cf;line-height:1.4}.nkt-recall-action{margin:0 10px 12px;padding:10px;border:1px solid #b97818;background:#fff3dc;display:flex;align-items:center;justify-content:space-between;gap:16px}.nkt-recall-action[hidden]{display:none}.nkt-recall-action span{color:#555}.nkt-recall-action button{font-weight:bold;white-space:nowrap}
      .nkt-rel-actionbar{display:flex;align-items:center;gap:8px;padding:8px;background:linear-gradient(#eee,#c8c8c8);border-top:1px solid #777}.nkt-rel-actionbar .spacer{flex:1}.nkt-rel-shell button{min-height:29px;border:1px solid #666;background:linear-gradient(#fff,#d0d0d0);padding:4px 11px}.nkt-rel-shell button.primary{background:linear-gradient(#edf6ff,#bddbfc);border-color:#3d6f9f}.nkt-rel-shell button:disabled{opacity:.55}
      .nkt-op-unavailable{display:flex;min-height:340px;align-items:center;justify-content:center;border:1px solid #777;background:#f1f1f1;font:700 16px Tahoma,Arial,sans-serif}
      @media(max-width:1000px){.nkt-rel-body{grid-template-columns:1fr}.nkt-rel-queue-panel{max-height:260px}.nkt-release-fields{grid-template-columns:1fr}.nkt-detail-head{grid-template-columns:1fr}}
    `;
    document.head.appendChild(style);
  }

  function bind(state) {
    state.wrapper.on('click', '[data-action="refresh"]', () => refresh_queue(state, true));
    state.wrapper.on('click', '[data-action="f10"]', () => finalize(state, true));
    state.wrapper.on('click', '[data-action="f12"]', () => finalize(state, false));
    state.wrapper.on('click', '[data-action="confirm-recall"]', () => confirm_recall(state));
    state.wrapper.on('click', '[data-release]', function () { select_release(state, $(this).data('release')); });
    state.wrapper.on('input', '[data-role="items"] input', () => update_total(state));
    $(document).off(`keydown.${NS}`).on(`keydown.${NS}`, e => {
      if (e.key === 'F1') { e.preventDefault(); e.stopImmediatePropagation(); return false; }
      if (e.key === 'F12' && e.ctrlKey && e.altKey && e.shiftKey) { e.preventDefault(); e.stopImmediatePropagation(); self_restrict_now(state); return false; }
      if (state.securityMode === 'limited' || state.terminalLocked) return;
      if ($('.modal.show').length) return;
      if (e.key === 'F5') { e.preventDefault(); refresh_queue(state, true); }
      else if (e.key === 'F10') { e.preventDefault(); finalize(state, true); }
      else if (e.key === 'F12') { e.preventDefault(); finalize(state, false); }
    });
  }


  const SECURITY_POLL_MS=2000;
  function bound_device_id(){try{return String(window.localStorage.getItem('nkt_device_id')||'').trim();}catch(_){return '';}}
  function initialize_security(state){clear_security_watch();const id=bound_device_id();if(!id)return Promise.resolve(true);return refresh_security(state).then(ok=>{if(ok&&!state.terminalLocked)start_security_watch(state);return ok;});}
  function clear_security_watch(){const k=`${NS}SecurityTimer`;if(window[k]){clearInterval(window[k]);window[k]=null;}$(window).off(`focus.${NS}Security`);}
  function start_security_watch(state){const k=`${NS}SecurityTimer`;window[k]=setInterval(()=>refresh_security(state),SECURITY_POLL_MS);$(window).off(`focus.${NS}Security`).on(`focus.${NS}Security`,()=>refresh_security(state));}
  function refresh_security(state){if(state.securityBusy||state.terminalLocked)return Promise.resolve(!state.terminalLocked);const id=bound_device_id();if(!id)return Promise.resolve(true);state.securityBusy=true;return frappe.call({method:'nkt_operations.nkt_store_operations.features.offline_edge.internal.device_controls.get_client_security_bootstrap',args:{device_id:id}}).then(r=>{const p=r.message||{};if(p.access==='unavailable'){if(p.local_action==='crypto_erase_sensitive_state')crypto_erase_sensitive_state(state);lock_terminal_screen(state,p.message||'Device access unavailable.');return false;}if(p.ui_mode==='limited'){state.localRestrictionLatch=false;apply_limited_mode(state);return false;}if(state.securityMode==='limited'&&!state.localRestrictionLatch){render_screen(state.frm);return false;}return true;}).catch(()=>{if(state.securityMode==='limited'||state.localRestrictionLatch)apply_limited_mode(state);return !state.terminalLocked;}).finally(()=>{state.securityBusy=false;});}
  function self_restrict_now(state){if(state.terminalLocked)return;state.localRestrictionLatch=true;apply_limited_mode(state);const id=bound_device_id();if(!id)return;frappe.call({method:'nkt_operations.nkt_store_operations.features.offline_edge.internal.device_controls.self_restrict_current_device',args:{device_id:id}}).then(()=>{state.localRestrictionLatch=false;apply_limited_mode(state);}).catch(()=>{state.localRestrictionLatch=true;apply_limited_mode(state);});}
  function clear_sensitive_work(state){state.boot=null;state.queue=[];state.selected=null;$('.modal.show').modal('hide');}
  function apply_limited_mode(state){state.securityMode='limited';clear_sensitive_work(state);state.wrapper.empty().html('<div class="nkt-op-unavailable">This function is unavailable in limited mode.</div>');}
  function crypto_erase_sensitive_state(state){try{for(let i=window.localStorage.length-1;i>=0;i--){const k=window.localStorage.key(i);if(k&&k.startsWith('nkt_')&&k!=='nkt_device_id')window.localStorage.removeItem(k);}}catch(_){}try{for(let i=window.sessionStorage.length-1;i>=0;i--){const k=window.sessionStorage.key(i);if(k&&k.startsWith('nkt_'))window.sessionStorage.removeItem(k);}}catch(_){}clear_sensitive_work(state);}
  function lock_terminal_screen(state,message){state.terminalLocked=true;state.securityMode='limited';clear_security_watch();crypto_erase_sensitive_state(state);$(document).off(`keydown.${NS}`);state.wrapper.empty().html(`<div class="nkt-op-unavailable">${esc(message||'Device access unavailable.')}</div>`);}
  function refresh_queue(state, freeze=false) {
    return frappe.call({ method: `${API}.get_warehouse_release_bootstrap`, args:{device_id:bound_device_id()}, freeze }).then(r => {
      state.boot = r.message || {};
      state.queue = state.boot.queue || [];
      $('[data-role="operator"]', state.wrapper).text(`${state.boot.full_name || ''} (${state.boot.user || ''})`);
      $('[data-role="queue-count"]', state.wrapper).text(state.queue.length);
      render_queue(state);
      if (state.selected) {
        const exists = state.queue.some(x => x.name === state.selected.warehouse_release);
        if (exists) select_release(state, state.selected.warehouse_release);
        else clear_detail(state);
      }
    }).catch(show_error);
  }

  function render_queue(state) {
    const box = $('[data-role="queue"]', state.wrapper);
    if (!state.queue.length) { box.html('<div class="nkt-empty">No pending external warehouse releases.</div>'); return; }
    box.html(state.queue.map(row => `
      <div class="nkt-rel-queue-row ${state.selected?.warehouse_release === row.name ? 'active' : ''}" data-release="${esc_attr(row.name)}">
        <strong><span>${esc(row.name)}</span><span>${esc(row.custom_nkt_source_warehouse || '')}</span></strong>
        <small><b>${esc(row.release_status || 'Draft')}</b>${row.warehouse_change ? ` • Change ${esc(row.warehouse_change)}` : ''}</small>
        <small>${esc(row.customer_name || row.customer || '')}</small>
        <small>Order: ${esc(row.customer_order || '')}</small>
      </div>`).join(''));
  }

  function select_release(state, name) {
    if (!name || state.finalizing) return;
    frappe.call({ method: `${API}.get_warehouse_release_context`, args: { release_name: name, device_id:bound_device_id() }, freeze: true }).then(r => {
      state.selected = r.message;
      state.requestId = uuid();
      render_queue(state);
      render_detail(state);
    }).catch(show_error);
  }

  function render_detail(state) {
    const x = state.selected;
    $('[data-role="empty-detail"]', state.wrapper).attr('hidden', true);
    $('[data-role="detail"]', state.wrapper).removeAttr('hidden');
    $('[data-role="release-name"]', state.wrapper).text(x.warehouse_release || '');
    $('[data-role="order-name"]', state.wrapper).text(x.customer_order || '');
    $('[data-role="customer-name"]', state.wrapper).text(x.customer_name || x.customer || '');
    $('[data-role="warehouse-name"]', state.wrapper).text(x.source_warehouse || '');
    $('[data-role="reference"]', state.wrapper).val(x.mother_release_reference || '');
    $('[data-role="driver"]', state.wrapper).val(x.driver_name || '');
    $('[data-role="plate"]', state.wrapper).val(x.plate_number || '');
    const body = $('[data-role="items"]', state.wrapper);
    const recall = !!x.can_confirm_recall;
    const change = x.warehouse_change_context || {};
    const partialRecall = Number(change.released_quantity_before || 0) > 0;
    body.html((x.items || []).map(row => `
      <tr data-row="${esc_attr(row.name)}">
        <td>${esc(row.item || '')}</td><td>${esc(row.item_name || '')}</td><td>${esc(row.uom || '')}</td>
        <td>${fmt_qty(row.ordered_quantity)}</td><td>${fmt_qty(row.previously_released_quantity)}</td><td>${fmt_qty(row.remaining_quantity)}</td>
        <td><input type="number" step="any" min="0" max="${Number(row.remaining_quantity || 0)}" value="${recall ? 0 : Number(row.remaining_quantity || 0)}" data-release-qty="${esc_attr(row.name)}" ${recall ? 'disabled' : ''}></td>
      </tr>`).join(''));
    if (recall) {
      $('[data-role="reference"],[data-role="driver"],[data-role="plate"]', state.wrapper).prop('disabled', true);
      $('.nkt-rel-note', state.wrapper).html(partialRecall ? `<b>Recall Pending — remaining balance only.</b> ${fmt_qty(change.released_quantity_before || 0)} was already physically released and remains attributed to this warehouse. Confirm only that <b>no further quantity</b> left after this recall request.` : `<b>Recall Pending.</b> A controlled warehouse source change was requested. Physical release is blocked until recall is confirmed.`);
      const recallLabel = partialRecall ? 'Confirm Recall — No Further Release' : 'Confirm Recall — Nothing Released';
      state.wrapper.find('[data-action="confirm-recall"]').text(recallLabel);
      $('[data-role="recall-primary"] span', state.wrapper).text(partialRecall ? 'Confirm after verifying that no additional quantity left after the recall request. Earlier submitted releases remain valid.' : 'Confirm only after warehouse staff verifies that nothing on this prepared release physically left the warehouse.');
      $('[data-role="recall-primary"]', state.wrapper).removeAttr('hidden');
    } else {
      $('[data-role="reference"],[data-role="driver"],[data-role="plate"]', state.wrapper).prop('disabled', false);
      $('.nkt-rel-note', state.wrapper).text('Enter only the quantity physically leaving this warehouse now. Partial releases are allowed. Remaining quantity stays reserved for the next controlled release.');
      $('[data-role="recall-primary"]', state.wrapper).attr('hidden', true);
    }
    update_total(state);
    set_buttons(state, !!x.can_release);
    state.wrapper.find('[data-action="confirm-recall"]').prop('hidden', !recall).prop('disabled', !recall || state.finalizing);
  }

  function clear_detail(state) {
    state.selected = null;
    $('[data-role="detail"]', state.wrapper).attr('hidden', true);
    $('[data-role="empty-detail"]', state.wrapper).removeAttr('hidden');
    set_buttons(state, false);
    render_queue(state);
  }

  function set_buttons(state, enabled) {
    state.wrapper.find('[data-action="f10"],[data-action="f12"]').prop('disabled', !enabled || state.finalizing);
    state.wrapper.find('[data-action="confirm-recall"]').prop('disabled', !state.selected?.can_confirm_recall || state.finalizing);
  }

  function update_total(state) {
    let total = 0;
    state.wrapper.find('[data-release-qty]').each(function () { total += Number($(this).val() || 0); });
    $('[data-role="release-total"]', state.wrapper).text(fmt_qty(total));
  }

  function payload(state) {
    return {
      warehouse_release: state.selected?.warehouse_release,
      request_id: state.requestId,
      mother_release_reference: String($('[data-role="reference"]', state.wrapper).val() || '').trim(),
      driver_name: String($('[data-role="driver"]', state.wrapper).val() || '').trim(),
      plate_number: String($('[data-role="plate"]', state.wrapper).val() || '').trim(),
      items: state.wrapper.find('[data-release-qty]').map(function () {
        return { name: $(this).data('release-qty'), release_quantity: Number($(this).val() || 0) };
      }).get()
    };
  }

  function finalize(state, printAfter) {
    if (state.finalizing) return;
    if (state.lastSuccessAt && Date.now() - state.lastSuccessAt < 3000) return;
    if (!state.selected?.can_release) { frappe.show_alert({ message: __('Select an open warehouse release first.'), indicator: 'orange' }); return; }
    const p = payload(state);
    if (!p.mother_release_reference) {
      frappe.msgprint({ title: __('Release Reference Required'), indicator: 'orange', message: __('Enter the Release Authorization Reference before confirming the release.') });
      return;
    }
    if (!p.items.some(x => Number(x.release_quantity || 0) > 0)) {
      frappe.msgprint({ title: __('Release Quantity Required'), indicator: 'orange', message: __('Enter a release quantity greater than zero for at least one item.') });
      return;
    }
    state.finalizing = true;
    set_buttons(state, true);
    const requestId = state.requestId;
    state.statusTimer = setTimeout(() => recover_status(state, requestId, printAfter), 12000);
    frappe.call({ method: `${API}.finalize_warehouse_release_fast`, args: { payload: JSON.stringify(p), device_id:bound_device_id() }, freeze: true }).then(r => {
      finish_success(state, r.message, printAfter);
    }).catch(err => {
      clearTimeout(state.statusTimer); state.statusTimer = null; state.finalizing = false; set_buttons(state, true); show_error(err);
    });
  }

  function recover_status(state, requestId, printAfter) {
    frappe.call({ method: `${API}.get_warehouse_release_request_status`, args: { request_id: requestId, device_id:bound_device_id() } }).then(r => {
      const x = r.message || {};
      if (x.physical_release_recorded_at_edge || (x.found && Number(x.docstatus) === 1)) finish_success(state, x, printAfter);
    }).catch(() => {});
  }

  function finish_success(state, result, printAfter) {
    if (!state.finalizing && state.lastSuccessAt && Date.now() - state.lastSuccessAt < 3000) return;
    clearTimeout(state.statusTimer); state.statusTimer = null; state.finalizing = false; state.lastSuccessAt = Date.now();
    const locallyRecorded = !!result.physical_release_recorded_at_edge && !result.warehouse_release_submitted;
    const next = (result.next_draft_releases || []).map(x => x.name).join(', ');
    frappe.msgprint({
      title: __(locallyRecorded ? 'Warehouse Release Recorded' : 'Warehouse Release Confirmed'), indicator: 'green',
      message: `
        <div style="font-size:14px;line-height:1.55">
          <div><b>Release:</b> ${esc(result.warehouse_release || '')}</div>
          <div><b>Customer Order:</b> ${esc(result.customer_order || '')}</div>
          <div><b>Quantity Released:</b> ${fmt_qty(result.total_release_quantity || 0)}</div>
          ${result.stock_entry ? `<div><b>Stock Entry:</b> ${esc(result.stock_entry)}</div>` : ''}
          <div><b>Status:</b> ${locallyRecorded ? 'Physical release recorded on this terminal. You may continue working; synchronization can complete when the connection is available.' : (result.is_partial_release ? 'Partial release completed' : 'Release completed')}</div>
          ${locallyRecorded ? '<div><b>Remaining balance:</b> If any remains, it will appear after synchronization.</div>' : ''}
          ${next ? `<div><b>Remaining release prepared:</b> ${esc(next)}</div>` : ''}
          ${printAfter && locallyRecorded ? '<div><b>Print:</b> The official completed release print becomes available after synchronization.</div>' : ''}
        </div>`
    });
    if (printAfter && result.print_url) window.open(result.print_url, '_blank', 'noopener');
    state.requestId = uuid();
    refresh_queue(state, false);
  }


  function confirm_recall(state) {
    if (state.finalizing) return;
    if (!state.selected?.can_confirm_recall) return;
    const change = state.selected?.warehouse_change_context || {};
    const partialRecall = Number(change.released_quantity_before || 0) > 0;
    const confirmText = partialRecall
      ? __('Confirm that no <b>further</b> quantity on this remaining Warehouse Release physically left after the recall request. The quantity released earlier remains final at this warehouse; only the unreleased balance will move.')
      : __('Confirm that no quantity on this prepared Warehouse Release physically left the warehouse. This will recall the old release and let the Encoder\'s controlled source change proceed.');
    frappe.confirm(
      confirmText,
      () => {
        state.finalizing = true; set_buttons(state, false);
        frappe.call({ method: `${API}.confirm_warehouse_change_recall`, args: { release_name: state.selected.warehouse_release, device_id:bound_device_id() }, freeze: true })
          .then(r => {
            const x = r.message || {};
            state.finalizing = false; state.lastSuccessAt = Date.now();
            frappe.msgprint({ title: __(partialRecall ? 'Partial Warehouse Recall Confirmed' : 'Warehouse Recall Confirmed'), indicator: 'green', message: `
              <div style="font-size:14px;line-height:1.55">
                <div><b>Warehouse Change:</b> ${esc(x.warehouse_change || '')}</div>
                <div><b>Customer Order:</b> ${esc(x.customer_order || '')}</div>
                <div><b>Old Warehouse:</b> ${esc(x.original_warehouse || '')}</div>
                <div><b>New Warehouse:</b> ${esc(x.new_warehouse || '')}</div>
                ${Number(x.released_quantity_before || 0)>0 ? `<div><b>Released Earlier / Stayed at Old Warehouse:</b> ${fmt_qty(x.released_quantity_before || 0)}</div>` : ''}
                <div><b>Quantity Moved:</b> ${fmt_qty(x.quantity_to_move || 0)}</div>
                ${x.replacement_reservation ? `<div><b>Replacement Reservation:</b> ${esc(x.replacement_reservation)}</div>` : ''}
                ${x.replacement_stock_entry ? `<div><b>Replacement Stock Entry:</b> ${esc(x.replacement_stock_entry)}</div>` : ''}
              </div>` });
            refresh_queue(state, false);
          })
          .catch(err => { state.finalizing = false; set_buttons(state, false); show_error(err); });
      }
    );
  }
  function show_error(err) {
    const msg = error_text(err) || __('Warehouse release could not be completed. Refresh and review the release details.');
    frappe.msgprint({ title: __('Warehouse Release Not Posted'), indicator: 'red', message: esc(msg) });
  }

  function error_text(err) {
    if (!err) return '';
    if (typeof err === 'string') return err;
    if (err._server_messages) {
      try {
        const a = JSON.parse(err._server_messages); if (a.length) { const x = JSON.parse(a[0]); return x.message || String(a[0]); }
      } catch (_) {}
    }
    return err.message || err.exc || '';
  }

  function uuid() {
    if (window.crypto?.randomUUID) return window.crypto.randomUUID();
    return 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, c => {
      const r = Math.random() * 16 | 0, v = c === 'x' ? r : (r & 3 | 8); return v.toString(16);
    });
  }
  function fmt_qty(v) { return Number(v || 0).toLocaleString(undefined, { maximumFractionDigits: 6 }); }
  function esc(v) { return String(v ?? '').replace(/[&<>'"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[c])); }
  function esc_attr(v) { return esc(v); }
})();

/* ===== END SOURCE: NKT Warehouse Release Fast Screen V2.0C.4.2.2 ===== */

/* ===== SOURCE: NKT R4 UI6 Reserved F1 F7 - NKT Warehouse Release Fast Screen ===== */
// NKT R4 UI6 - reserve F1 and F7 on this NKT Fast Screen.
(() => {
    const DOCTYPE = "NKT Warehouse Release Fast Screen";
    const SLOT = "__nktReservedF1F7_7c5dffe34356ff11";
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

/* ===== END SOURCE: NKT R4 UI6 Reserved F1 F7 - NKT Warehouse Release Fast Screen ===== */
