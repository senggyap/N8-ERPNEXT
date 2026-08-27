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
