(() => {
  const DOCTYPE = 'NKT Warehouse Change Fast Screen';
  const API = 'nkt_operations.nkt_store_operations.fast_screen_backend';
  const NS = 'nktWarehouseChangeC422';
  const PRELOAD_KEY = 'nkt_wch_preload_order';

  frappe.ui.form.on(DOCTYPE, { refresh(frm) { render(frm); } });

  function render(frm) {
    frm.disable_save(); frm.page.clear_actions(); frm.page.set_title(__('NKT Warehouse Change'));
    const w = frm.fields_dict.screen_html.$wrapper;
    w.closest('.form-layout').find('.layout-side-section').hide();
    w.closest('.layout-main-section-wrapper').css({width:'100%',maxWidth:'none',flex:'1 1 100%'});
    w.empty().html(markup()); css();
    const state = { frm, w, boot:null, context:null, requestId:uuid(), busy:false,
      securityMode:'normal', securityBusy:false, terminalLocked:false, localRestrictionLatch:false };
    bind(state);
    initialize_security(state).then(ok=>{
      if(!ok||state.securityMode==='limited'||state.terminalLocked)return;
      bootstrap(state).then(() => {
        const preload = String(sessionStorage.getItem(PRELOAD_KEY) || '').trim();
        if (preload) {
          sessionStorage.removeItem(PRELOAD_KEY);
          $('[data-role="order"]', state.w).val(preload);
          load_order(state);
        }
      });
    });
  }

  function markup() { return `
    <div class="nkt-wch-shell">
      <div class="nkt-wch-title"><strong>NKT Controlled Warehouse Change</strong><span>V2.0C.4.2.2 — PARTIAL BALANCE MOVE</span></div>
      <div class="nkt-wch-bar">
        <label class="history-search-label">Find recent Encoder transaction<input data-role="history-search" placeholder="Customer, item, warehouse or order..." autocomplete="off"></label>
        <button data-action="refresh-history">Refresh</button>
        <div class="spacer"></div>
        <label>Order # <input data-role="order" placeholder="NKT-ORD-00000" autocomplete="off"></label>
        <button data-action="load">Load</button>
        <button data-action="release-screen">Warehouse Release Queue</button>
      </div>
      <div class="nkt-wch-body">
        <section class="nkt-wch-history">
          <div class="panel-head"><b>Recent Encoder Orders</b><small>Today • newest first • click Review / Change</small></div>
          <div data-role="recent-orders" class="history-list"><div class="empty-card">Loading recent orders...</div></div>
        </section>
        <section class="nkt-wch-detail">
          <div data-role="order-summary" class="nkt-wch-summary">Select a recent Encoder order, or enter an Order # as a fallback.</div>
          <table class="nkt-wch-table"><thead><tr><th>#</th><th>Item</th><th>Official Source</th><th>Qty</th><th>Released</th><th>Remaining</th><th>Status</th><th>Change</th></tr></thead><tbody data-role="rows"><tr><td colspan="8" class="empty">No order loaded.</td></tr></tbody></table>
          <div class="nkt-wch-recent"><b>Recent Warehouse Changes</b><div data-role="recent"></div></div>
        </section>
      </div>
    </div>`; }

  function css(){ if(document.getElementById('nkt-wch-c4213-css'))return; const s=document.createElement('style'); s.id='nkt-wch-c4213-css'; s.textContent=`
    .nkt-wch-shell{font-family:Tahoma,Arial,sans-serif;font-size:13px;border:1px solid #777;background:#ddd;min-height:650px}.nkt-wch-title{display:flex;justify-content:space-between;padding:9px 12px;font-size:18px;background:linear-gradient(#fafafa,#ccc);border-bottom:1px solid #777}.nkt-wch-title span{font-size:11px;border:1px solid #7d8b63;background:#eef3df;padding:4px 8px}.nkt-wch-bar{display:flex;align-items:end;gap:8px;padding:10px;background:#eee;border-bottom:1px solid #999}.nkt-wch-bar label{font-weight:bold}.nkt-wch-bar input{display:block;width:180px;height:29px;border:1px solid #666;padding:3px 6px}.nkt-wch-bar .history-search-label{flex:0 1 330px}.nkt-wch-bar .history-search-label input{width:100%}.nkt-wch-bar .spacer{flex:1}.nkt-wch-shell button{min-height:29px;border:1px solid #666;background:linear-gradient(#fff,#d2d2d2);padding:4px 11px}.nkt-wch-body{display:grid;grid-template-columns:minmax(300px,34%) minmax(0,66%);gap:0;min-height:560px}.nkt-wch-history{background:#f5f5f5;border-right:1px solid #999;min-width:0}.panel-head{padding:9px 10px;background:linear-gradient(#f7f7f7,#d8d8d8);border-bottom:1px solid #aaa;display:flex;flex-direction:column;gap:2px}.panel-head small{color:#555}.history-list{max-height:545px;overflow:auto;padding:7px}.history-card{background:#fff;border:1px solid #aaa;margin-bottom:7px;padding:8px;display:grid;grid-template-columns:1fr auto;gap:7px}.history-card .order{font-weight:bold;font-size:14px}.history-card .customer{font-weight:bold;margin-top:3px}.history-card .meta,.history-card .items{color:#555;line-height:1.35;margin-top:3px}.history-card .status-chip{display:inline-block;border:1px solid #999;background:#f3f3f3;padding:1px 5px;margin-top:4px;font-size:11px}.history-card button{align-self:center;white-space:nowrap}.empty-card{padding:25px;text-align:center;color:#666}.nkt-wch-detail{min-width:0;background:#ddd}.nkt-wch-summary{padding:10px;background:#fff;border-bottom:1px solid #aaa}.nkt-wch-table{width:100%;border-collapse:collapse;background:#fff}.nkt-wch-table th,.nkt-wch-table td{border:1px solid #bbb;padding:7px}.nkt-wch-table th{background:linear-gradient(#f4f4f4,#d5d5d5)}.nkt-wch-table .empty{text-align:center;color:#666;padding:35px}.nkt-wch-table button{white-space:nowrap}.nkt-wch-recent{margin:10px;background:#fff;border:1px solid #999;padding:9px}.nkt-wch-recent .row{padding:5px;border-top:1px solid #ddd}.nkt-lock{color:#8a2c2c}.nkt-open{color:#185c26;font-weight:bold}.nkt-op-unavailable{display:flex;min-height:340px;align-items:center;justify-content:center;border:1px solid #777;background:#f1f1f1;font:700 16px Tahoma,Arial,sans-serif}
      @media(max-width:1050px){.nkt-wch-body{grid-template-columns:1fr}.nkt-wch-history{border-right:0;border-bottom:1px solid #999}.history-list{max-height:320px}}
  `; document.head.appendChild(s); }

  function bind(state){
    state.w.on('click','[data-action="load"]',()=>load_order(state));
    state.w.on('click','[data-action="refresh-history"]',()=>bootstrap(state));
    state.w.on('click','[data-action="release-screen"]',()=>frappe.set_route('Form','NKT Warehouse Release Fast Screen','NKT Warehouse Release Fast Screen'));
    state.w.on('click','[data-change-row]',function(){ open_change(state,String($(this).data('change-row'))); });
    state.w.on('click','[data-order-load]',function(){ const n=String($(this).data('order-load')||''); $('[data-role="order"]',state.w).val(n); load_order(state); });
    $('[data-role="order"]',state.w).on('keydown',e=>{if(e.key==='Enter'){e.preventDefault();load_order(state);}});
    $('[data-role="history-search"]',state.w).on('input',()=>render_recent_orders(state));
    $(document).off(`keydown.${NS}`).on(`keydown.${NS}`,e=>{ if(e.key==='F1'){e.preventDefault();e.stopImmediatePropagation();return false;} if(e.key==='F12'&&e.ctrlKey&&e.altKey&&e.shiftKey){e.preventDefault();e.stopImmediatePropagation();self_restrict_now(state);return false;} if(state.securityMode==='limited'||state.terminalLocked)return; if($('.modal.show').length)return; if(e.key==='F5'){e.preventDefault();bootstrap(state).then(()=>state.context&&load_order(state));} });
  }


  const SECURITY_POLL_MS=2000;
  function bound_device_id(){try{return String(window.localStorage.getItem('nkt_device_id')||'').trim();}catch(_){return '';}}
  function initialize_security(state){clear_security_watch();const id=bound_device_id();if(!id)return Promise.resolve(true);return refresh_security(state).then(ok=>{if(ok&&!state.terminalLocked)start_security_watch(state);return ok;});}
  function clear_security_watch(){const k=`${NS}SecurityTimer`;if(window[k]){clearInterval(window[k]);window[k]=null;}$(window).off(`focus.${NS}Security`);}
  function start_security_watch(state){const k=`${NS}SecurityTimer`;window[k]=setInterval(()=>refresh_security(state),SECURITY_POLL_MS);$(window).off(`focus.${NS}Security`).on(`focus.${NS}Security`,()=>refresh_security(state));}
  function refresh_security(state){if(state.securityBusy||state.terminalLocked)return Promise.resolve(!state.terminalLocked);const id=bound_device_id();if(!id)return Promise.resolve(true);state.securityBusy=true;return frappe.call({method:'nkt_operations.nkt_store_operations.features.offline_edge.internal.device_controls.get_client_security_bootstrap',args:{device_id:id}}).then(r=>{const p=r.message||{};if(p.access==='unavailable'){if(p.local_action==='crypto_erase_sensitive_state')crypto_erase_sensitive_state(state);lock_terminal_screen(state,p.message||'Device access unavailable.');return false;}if(p.ui_mode==='limited'){state.localRestrictionLatch=false;apply_limited_mode(state);return false;}if(state.securityMode==='limited'&&!state.localRestrictionLatch){render(state.frm);return false;}return true;}).catch(()=>{if(state.securityMode==='limited'||state.localRestrictionLatch)apply_limited_mode(state);return !state.terminalLocked;}).finally(()=>{state.securityBusy=false;});}
  function self_restrict_now(state){if(state.terminalLocked)return;state.localRestrictionLatch=true;apply_limited_mode(state);const id=bound_device_id();if(!id)return;frappe.call({method:'nkt_operations.nkt_store_operations.features.offline_edge.internal.device_controls.self_restrict_current_device',args:{device_id:id}}).then(()=>{state.localRestrictionLatch=false;apply_limited_mode(state);}).catch(()=>{state.localRestrictionLatch=true;apply_limited_mode(state);});}
  function clear_sensitive_work(state){state.boot=null;state.context=null;$('.modal.show').modal('hide');}
  function apply_limited_mode(state){state.securityMode='limited';clear_sensitive_work(state);state.w.empty().html('<div class="nkt-op-unavailable">This function is unavailable in limited mode.</div>');}
  function crypto_erase_sensitive_state(state){try{for(let i=window.localStorage.length-1;i>=0;i--){const k=window.localStorage.key(i);if(k&&k.startsWith('nkt_')&&k!=='nkt_device_id')window.localStorage.removeItem(k);}}catch(_){}try{for(let i=window.sessionStorage.length-1;i>=0;i--){const k=window.sessionStorage.key(i);if(k&&k.startsWith('nkt_'))window.sessionStorage.removeItem(k);}}catch(_){}clear_sensitive_work(state);}
  function lock_terminal_screen(state,message){state.terminalLocked=true;state.securityMode='limited';clear_security_watch();crypto_erase_sensitive_state(state);$(document).off(`keydown.${NS}`);state.w.empty().html(`<div class="nkt-op-unavailable">${esc(message||'Device access unavailable.')}</div>`);}
  function bootstrap(state){ return frappe.call({method:`${API}.get_warehouse_change_bootstrap`,freeze:true}).then(r=>{state.boot=r.message||{};render_recent_orders(state);render_recent(state);}).catch(show_error); }
  function load_order(state){ const name=String($('[data-role="order"]',state.w).val()||'').trim(); if(!name)return; return frappe.call({method:`${API}.get_warehouse_change_context`,args:{order_name:name},freeze:true}).then(r=>{state.context=r.message;render_context(state);}).catch(show_error); }

  function render_recent_orders(state){
    const box=$('[data-role="recent-orders"]',state.w); const q=String($('[data-role="history-search"]',state.w).val()||'').trim().toLowerCase();
    const rows=(state.boot?.recent_orders||[]).filter(x=>!q||[x.order,x.customer_name,x.customer,x.item_summary,x.warehouse_summary,x.payment_status,x.fulfillment_status,x.latest_change].some(v=>String(v||'').toLowerCase().includes(q)));
    if(!rows.length){box.html('<div class="empty-card">No matching recent Encoder orders.</div>');return;}
    box.html(rows.map(x=>`<div class="history-card"><div><div class="order">${esc(x.order)} <small>• ${esc(time_text(x.creation))}</small></div><div class="customer">${esc(x.customer_name||x.customer||'')}</div><div class="items">${esc(x.item_summary||'')}</div><div class="meta">Source: ${esc(x.warehouse_summary||'—')} • Payment: ${esc(x.payment_status||'—')}<br>Fulfillment: ${esc(x.fulfillment_status||x.order_status||'—')}</div>${x.latest_change?`<span class="status-chip">${esc(x.latest_change)} • ${esc(x.latest_change_status||'')}</span>`:''}</div><button data-order-load="${esc_attr(x.order)}">Review / Change</button></div>`).join(''));
  }

  function render_context(state){
    const x=state.context||{}; const ch=x.latest_warehouse_change;
    const changeNote=ch?.name?` • Warehouse Change ${esc(ch.name)} ${esc(ch.change_status||'')}`:'';
    $('[data-role="order-summary"]',state.w).html(`<b>${esc(x.order||'')}</b> • ${esc(x.customer_name||x.customer||'')} • Payment ${esc(x.payment_status||'')} • Fulfillment ${esc(x.fulfillment_status||x.status||'')}${changeNote}`);
    const body=$('[data-role="rows"]',state.w); const rows=x.rows||[]; if(!rows.length){body.html('<tr><td colspan="8" class="empty">No fulfillment rows.</td></tr>');return;} body.html(rows.map(r=>{ const partial=!!r.partial_release_change; const status=r.ordinary_change_locked?esc(r.lock_reason):(partial?`<b>${qty(r.released_quantity)}</b> already released from this warehouse. Only remaining <b>${qty(r.remaining_quantity)}</b> can move.`:'Eligible — recall prepared release first'); const label=partial?`Move Remaining ${qty(r.remaining_quantity)}`:'Change Warehouse'; return `<tr><td>${esc(r.idx)}</td><td><b>${esc(r.item)}</b><br><small>${esc(r.item_name||'')}</small></td><td>${esc(label_wh(state,r.source_warehouse))}<br><small>${esc(r.source_type||'')}</small></td><td>${qty(r.quantity)}</td><td>${qty(r.released_quantity)}</td><td>${qty(r.remaining_quantity)}</td><td class="${r.ordinary_change_locked?'nkt-lock':'nkt-open'}">${status}</td><td><button data-change-row="${esc_attr(r.row_name)}" ${r.ordinary_change_locked?'disabled':''}>${label}</button></td></tr>`; }).join(''));
  }

  function open_change(state,rowName){ const r=(state.context?.rows||[]).find(x=>x.row_name===rowName); if(!r||r.ordinary_change_locked)return; const partial=!!r.partial_release_change; const wh=(state.boot?.warehouses||[]).filter(x=>x.name!==r.source_warehouse); const explanation=partial?`<div style="line-height:1.55"><b>${esc(r.item)}</b><br>Current source: <b>${esc(label_wh(state,r.source_warehouse))}</b><br>Already physically released here: <b>${qty(r.released_quantity)}</b><br>Unreleased balance that may move: <b>${qty(r.remaining_quantity)}</b><br><br><b>The released quantity will stay permanently attributed to the old warehouse.</b> Only the remaining balance is split to the new source. The prepared remaining release will enter <b>Recall Pending</b>; warehouse staff must confirm that no further quantity left after the recall request.</div>`:`<div style="line-height:1.55"><b>${esc(r.item)}</b><br>Current source: <b>${esc(label_wh(state,r.source_warehouse))}</b><br>Quantity to move: <b>${qty(r.remaining_quantity)}</b><br><br>A prepared external release will be placed in <b>Recall Pending</b>. The warehouse must confirm that nothing physically left before the source change is applied.</div>`; const d=new frappe.ui.Dialog({title:__(partial?'Move Unreleased Balance':'Controlled Warehouse Change'),fields:[{fieldtype:'HTML',fieldname:'info',options:explanation},{fieldtype:'Select',fieldname:'new_warehouse',label:'New Official Source Warehouse',options:['',...wh.map(x=>x.name)],reqd:1},{fieldtype:'Small Text',fieldname:'reason',label:'Reason',reqd:1}],primary_action_label:__(partial?'Recall Remaining & Move':'Request Recall & Change'),primary_action(values){ const p={customer_order:state.context.order,customer_order_item:r.row_name,new_warehouse:values.new_warehouse,reason:values.reason,request_id:state.requestId}; d.get_primary_btn().prop('disabled',true); frappe.call({method:`${API}.request_warehouse_change`,args:{payload:JSON.stringify(p)},freeze:true}).then(res=>{d.hide(); const x=res.message||{}; state.requestId=uuid(); const recallText=Number(x.released_quantity_before||0)>0?'confirm <b>Recall — No Further Release</b>':'confirm <b>Recall — Nothing Released</b>'; frappe.msgprint({title:__('Warehouse Recall Requested'),indicator:'orange',message:`<div style="line-height:1.55"><div><b>Warehouse Change:</b> ${esc(x.warehouse_change||'')}</div><div><b>Release placed in Recall Pending:</b> ${esc(x.recall_release||'')}</div><div><b>From:</b> ${esc(label_wh(state,x.original_warehouse))}</div><div><b>To:</b> ${esc(label_wh(state,x.new_warehouse))}</div>${Number(x.released_quantity_before||0)>0?`<div><b>Already Released / Locked:</b> ${qty(x.released_quantity_before)}</div>`:''}<div><b>Quantity to Move:</b> ${qty(x.quantity_to_move)}</div><br>Warehouse staff must open the Warehouse Release screen and ${recallText}. Inventory/source changes are not applied until that confirmation.</div>`}); bootstrap(state).then(()=>load_order(state));}).catch(err=>{d.get_primary_btn().prop('disabled',false);show_error(err);}); }}); d.show(); }

  function render_recent(state){ const box=$('[data-role="recent"]',state.w); const rows=state.boot?.recent_changes||[]; if(!rows.length){box.html('<div class="row">No warehouse-change records yet.</div>');return;} box.html(rows.map(x=>`<div class="row"><b>${esc(x.name)}</b> • ${esc(x.change_status)} • ${esc(x.customer_order)} • ${esc(x.original_warehouse)} → ${esc(x.new_warehouse)} • ${qty(x.quantity_to_move)}${x.recall_release?` • Recall ${esc(x.recall_release)}`:''}</div>`).join('')); }
  function label_wh(state,n){const x=(state.boot?.warehouses||[]).find(w=>w.name===n);return x?.label||n||'';}
  function time_text(v){if(!v)return'';try{return frappe.datetime.str_to_user(v).split(' ')[1]||frappe.datetime.str_to_user(v);}catch(_){return String(v).slice(11,16);}}
  function show_error(err){frappe.msgprint({title:__('Warehouse Change Not Completed'),indicator:'red',message:esc(error_text(err)||'Warehouse change could not be completed.')});}
  function error_text(err){if(!err)return'';if(typeof err==='string')return err;if(err._server_messages){try{const a=JSON.parse(err._server_messages);if(a.length){const x=JSON.parse(a[0]);return x.message||String(a[0]);}}catch(_){}}return err.message||err.exc||'';}
  function uuid(){if(window.crypto?.randomUUID)return window.crypto.randomUUID();return'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g,c=>{const r=Math.random()*16|0,v=c==='x'?r:(r&3|8);return v.toString(16);});}
  function qty(v){return Number(v||0).toLocaleString(undefined,{maximumFractionDigits:6});}
  function esc(v){return String(v??'').replace(/[&<>'"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[c]));}
  function esc_attr(v){return esc(v);}
})();
