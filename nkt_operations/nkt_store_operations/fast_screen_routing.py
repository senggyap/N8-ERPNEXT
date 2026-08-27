from __future__ import annotations

import html
import json
import uuid
from typing import Any, Dict, List, Optional

import frappe
from frappe import _
from frappe.utils import cint, flt

from nkt_operations.nkt_store_operations.features.sales.customer_order_intent import (
    ORDER_INTENT_FAMILY,
    accept_customer_order_intent_at_edge,
    pending_reservation_qty,
)
from nkt_operations.nkt_store_operations.features.payments_accounts.internal.cashier_tender_intent import (
    TENDER_INTENT_FAMILY,
    _normalize_cashier_tender_intent_payload,
    accept_cashier_tender_intent_at_edge,
)
from nkt_operations.nkt_store_operations.features.offline_edge.safe_sync import NKTIdempotencyConflict
from nkt_operations.nkt_store_operations.features.offline_edge.internal.edge_provider import load_configured_edge_snapshot
from nkt_operations.nkt_store_operations.features.offline_edge.policy import device_policy_snapshot, manila_now

FOUNDATION_VERSION = "C15C.10A-R18"
# NKT_MANAGER_PIN_EDGE_WIRING_MP1
PRIMARY_ROLE = "Primary"
EDGE_ROLE = "Store Edge"
ENCODER_MODE = "encoder"
CASHIER_MODE = "cashier"

ENCODER_ROLES = {"NKT Encoder", "NKT OWNER", "NKT ADMINISTRATOR", "System Manager"}
CASHIER_ROLES = {"NKT Cashier", "NKT OWNER", "NKT ADMINISTRATOR", "System Manager"}
TECHNICAL_RESULT_KEYS = {
    "event_uuid", "primary_ack_uuid", "payload_sha256", "envelope_sha256", "sync_state", "queue_state"
}


def _runtime_role() -> str:
    role = str(frappe.conf.get("nkt_runtime_role") or PRIMARY_ROLE).strip()
    if role not in {PRIMARY_ROLE, EDGE_ROLE}:
        raise frappe.PermissionError(_("Fast Screen service unavailable."))
    return role


def _session_user() -> str:
    user = frappe.session.user
    if not user or user == "Guest":
        raise frappe.PermissionError(_("Fast Screen service unavailable."))
    return user


def _require_encoder(user: Optional[str] = None) -> str:
    user = user or _session_user()
    if not (set(frappe.get_roles(user) or []) & ENCODER_ROLES):
        raise frappe.PermissionError(_("Encoder Fast Screen unavailable."))
    return user


def _require_cashier(user: Optional[str] = None) -> str:
    user = user or _session_user()
    if not (set(frappe.get_roles(user) or []) & CASHIER_ROLES):
        raise frappe.PermissionError(_("Cashier Fast Screen unavailable."))
    return user


def _require_mode_user(mode: str, user: Optional[str] = None) -> str:
    mode = str(mode or "").strip().lower()
    if mode == CASHIER_MODE:
        return _require_cashier(user)
    if mode == ENCODER_MODE:
        return _require_encoder(user)
    raise frappe.PermissionError(_("Fast Screen service unavailable."))


def _require_edge_device(
    device_id: Optional[str],
    *,
    user: Optional[str] = None,
    mode: str = ENCODER_MODE,
) -> Dict[str, Any]:
    user = _require_mode_user(mode, user)
    device_id = str(device_id or "").strip()
    if not device_id:
        raise frappe.PermissionError(
            _("This workstation must be registered before transaction entry can continue.")
        )
    policy = device_policy_snapshot(device_id, user=user, requested_context="NKT Retail")
    if policy.get("ui_mode") != "normal":
        raise frappe.PermissionError(_("Fast Screen service unavailable."))
    return policy


def _fast_ui():
    from nkt_operations.nkt_store_operations import fast_screen_backend as nkt_fast_ui_v2
    return nkt_fast_ui_v2


def _fast_customer():
    from nkt_operations.nkt_store_operations.features.fast_screen import fast_customer_creation as nkt_c5_6_fast_customer_creation
    return nkt_c5_6_fast_customer_creation


def _parse_payload(payload: Any) -> Dict[str, Any]:
    if isinstance(payload, str):
        payload = frappe.parse_json(payload)
    if not isinstance(payload, dict):
        frappe.throw(_("Fast Screen payload is invalid."))
    return dict(payload)


def _uuid(value: Any) -> str:
    raw = str(value or "").strip()
    try:
        return str(uuid.UUID(raw))
    except Exception as exc:
        raise frappe.ValidationError(
            _("This transaction identity is invalid. Refresh the Fast Screen and try again.")
        ) from exc


def _edge_snapshot(device_id: Optional[str], *, mode: str = ENCODER_MODE) -> Dict[str, Any]:
    _require_edge_device(device_id, mode=mode)
    snapshot = load_configured_edge_snapshot()
    if snapshot.get("critical_offline_mutations_enabled") is not False:
        raise frappe.PermissionError(_("Fast Screen service unavailable."))
    return snapshot


def _session_mode() -> str:
    roles = set(frappe.get_roles(_session_user()) or [])
    if roles & CASHIER_ROLES and not (roles & ENCODER_ROLES):
        return CASHIER_MODE
    if roles & ENCODER_ROLES:
        return ENCODER_MODE
    if roles & CASHIER_ROLES:
        return CASHIER_MODE
    raise frappe.PermissionError(_("Fast Screen service unavailable."))


def _company_for_store_warehouse(store_warehouse: str, fallback_boot: Optional[Dict[str, Any]] = None) -> str:
    if fallback_boot:
        location = fallback_boot.get("location") or {}
        company = str(location.get("company") or "").strip()
        if company:
            return company
    company = str(frappe.db.get_value("Warehouse", store_warehouse, "company") or "").strip()
    if not company:
        raise frappe.ValidationError(
            _("Local order setup is incomplete. Ask an Administrator to check the Store warehouse setup.")
        )
    return company


def _edge_bootstrap(device_id: Optional[str]) -> Dict[str, Any]:
    user = _require_encoder()
    snapshot = _edge_snapshot(device_id, mode=ENCODER_MODE)
    store_warehouse = str(snapshot.get("store_warehouse") or "").strip()
    if not store_warehouse:
        raise frappe.ValidationError(_("Local Store warehouse is unavailable."))

    base: Dict[str, Any] = {}
    try:
        base = dict(_fast_ui().get_fast_ui_bootstrap(ENCODER_MODE) or {})
    except Exception:
        base = {}

    company = _company_for_store_warehouse(store_warehouse, base)
    location = dict(base.get("location") or {})
    if not location:
        location = {"company": company, "friendly_label": "NKT Store"}
    else:
        location["company"] = location.get("company") or company
        location["friendly_label"] = location.get("friendly_label") or "NKT Store"

    warehouses = list(base.get("warehouses") or [])
    if not any(str(x.get("name") or "") == store_warehouse for x in warehouses):
        warehouses.append({"name": store_warehouse, "label": store_warehouse})

    now_ph = manila_now()
    return {
        "version": FOUNDATION_VERSION,
        "read_only_shell": False,
        "mode": ENCODER_MODE,
        "user": user,
        "full_name": frappe.db.get_value("User", user, "full_name") or user,
        "business_date": now_ph.date().isoformat(),
        "location": location,
        "setup_error": None,
        "warehouses": warehouses,
        "default_warehouse": store_warehouse,
        "open_shift": None,
        "blocked_shift": None,
        "shift_block_reason": None,
        "strict_current_date_shift": True,
        "price_variations": list(base.get("price_variations") or []),
        "shortcuts": {
            "F2": "Customer",
            "F3": "Enter Item",
            "F4": "Customer History",
            "F10": "Finalize and Print",
            "F11": "Payment after connection",
            "F12": "Finalize without Printing",
        },
        "posting_enabled": True,
        "continuity_order_only": True,
        "payment_entry_enabled": False,
        "new_customer_enabled": False,
        "continuity_message": (
            "Local order entry is available. Payment and new Customer creation "
            "will resume when the main connection is restored."
        ),
        "company": company,
        "local_data_generated_at": snapshot.get("generated_at"),
    }



def _edge_cashier_bootstrap(device_id: Optional[str]) -> Dict[str, Any]:
    user = _require_cashier()
    snapshot = _edge_snapshot(device_id, mode=CASHIER_MODE)
    store_warehouse = str(snapshot.get("store_warehouse") or "").strip()
    if not store_warehouse:
        raise frappe.ValidationError(_("Local Store warehouse is unavailable."))

    try:
        base = dict(_fast_ui().get_fast_ui_bootstrap(CASHIER_MODE) or {})
    except Exception:
        base = {}

    location = dict(base.get("location") or {})
    company = str(location.get("company") or _company_for_store_warehouse(store_warehouse, base)).strip()
    default_warehouse = str(base.get("default_warehouse") or store_warehouse).strip()
    open_shift = base.get("open_shift")
    blocked_shift = base.get("blocked_shift")
    setup_error = base.get("setup_error")
    shift_reason = base.get("shift_block_reason")

    if default_warehouse != store_warehouse:
        setup_error = (
            "This Cashier workstation is not aligned with the Store warehouse. "
            "Ask an Administrator to check the operating-location assignment."
        )
        open_shift = None

    posting_enabled = bool(open_shift) and not setup_error
    now_ph = manila_now()
    return {
        "version": FOUNDATION_VERSION,
        "read_only_shell": False,
        "mode": CASHIER_MODE,
        "user": user,
        "full_name": frappe.db.get_value("User", user, "full_name") or user,
        "business_date": now_ph.date().isoformat(),
        "location": location or {"company": company, "friendly_label": "NKT Store"},
        "setup_error": setup_error,
        "warehouses": [{"name": store_warehouse, "label": store_warehouse}],
        "default_warehouse": store_warehouse,
        "open_shift": open_shift,
        "blocked_shift": blocked_shift,
        "shift_block_reason": shift_reason,
        "strict_current_date_shift": True,
        "price_variations": list(base.get("price_variations") or []),
        "shortcuts": {
            "F2": "Customer",
            "F3": "Enter Item",
            "F4": "Customer History",
            "F10": "Finalize and Print",
            "F11": "Take Payment",
            "F12": "Finalize without Printing",
        },
        "posting_enabled": posting_enabled,
        "continuity_tender_enabled": posting_enabled,
        "payment_entry_enabled": posting_enabled,
        "new_customer_enabled": False,
        "continuity_message": (
            "Sales and payment entry are available on this workstation. "
            "New Customer creation requires the main connection."
        ),
        "company": company,
        "local_data_generated_at": snapshot.get("generated_at"),
    }

def _bounded_limit(value: Any, default: int = 12) -> int:
    return max(1, min(cint(value) or default, 25))


def _norm(value: Any) -> str:
    return str(value or "").strip().lower()


def _item_matches(row: Dict[str, Any], text: str) -> bool:
    if not text:
        return True
    hay = [row.get("item_code"), row.get("item_name"), *list(row.get("barcodes") or [])]
    return any(text in _norm(x) for x in hay)


def _item_rank(row: Dict[str, Any], text: str):
    if not text:
        return (2, _norm(row.get("item_name")), _norm(row.get("item_code")))
    code = _norm(row.get("item_code"))
    name = _norm(row.get("item_name"))
    if text in {code, name}:
        rank = 0
    elif code.startswith(text) or name.startswith(text):
        rank = 1
    else:
        rank = 2
    return (rank, name, code)


def _edge_item_row(row: Dict[str, Any], store_warehouse: str) -> Dict[str, Any]:
    item_code = str(row.get("item_code") or "")
    base_available = flt(row.get("available_qty"))
    pending = pending_reservation_qty(item_code, store_warehouse)
    return {
        "item_code": item_code,
        "item_name": row.get("item_name") or item_code,
        "stock_uom": row.get("stock_uom") or "",
        "standard_rate": flt(row.get("current_rate")),
        "actual_qty": flt(row.get("actual_qty")),
        "reserved_qty": flt(row.get("reserved_qty")),
        "available_qty": flt(base_available) - flt(pending),
    }


def _edge_search_items(search_text="", warehouse=None, limit=12, device_id=None):
    snapshot = _edge_snapshot(device_id, mode=_session_mode())
    store_warehouse = str(snapshot.get("store_warehouse") or "").strip()
    requested = str(warehouse or store_warehouse).strip()
    if requested != store_warehouse:
        return []
    text = _norm(search_text)
    rows = [dict(x) for x in list(snapshot.get("items") or []) if _item_matches(dict(x), text)]
    rows.sort(key=lambda x: _item_rank(x, text))
    return [_edge_item_row(x, store_warehouse) for x in rows[:_bounded_limit(limit)]]


def _edge_search_customers(search_text="", limit=12, device_id=None):
    snapshot = _edge_snapshot(device_id, mode=_session_mode())
    text = _norm(search_text)
    rows = []
    for raw in list(snapshot.get("customers") or []):
        row = dict(raw)
        name = str(row.get("customer") or row.get("name") or "").strip()
        customer_name = str(row.get("customer_name") or name).strip()
        if text and text not in name.lower() and text not in customer_name.lower():
            continue
        rank = 0 if text and text in {name.lower(), customer_name.lower()} else (
            1 if text and (name.lower().startswith(text) or customer_name.lower().startswith(text)) else 2
        )
        rows.append((rank, customer_name.lower(), name.lower(), {
            "name": name,
            "customer_name": customer_name,
            "mobile_no": row.get("mobile_no") or "",
            "territory": row.get("territory") or "",
            "current_account_balance": flt(row.get("current_account_balance")),
        }))
    rows.sort(key=lambda x: (x[0], x[1], x[2]))
    return [x[3] for x in rows[:_bounded_limit(limit)]]


def _edge_get_item_context(item_code, warehouse=None, device_id=None):
    item_code = str(item_code or "").strip()
    if not item_code:
        frappe.throw(_("Select an Item."))
    rows = _edge_search_items(item_code, warehouse=warehouse, limit=25, device_id=device_id)
    for row in rows:
        if str(row.get("item_code") or "") == item_code:
            return row
    frappe.throw(_("Saleable item was not found: {0}").format(item_code))


def _has_tender_data(data: Dict[str, Any]) -> bool:
    payments = data.get("payments")
    if isinstance(payments, list) and payments:
        return True
    for key in ("cash_tendered", "change_amount"):
        try:
            if abs(float(data.get(key) or 0)) > 0.000001:
                return True
        except Exception:
            return True
    return False


def _order_reference(event_uuid: str, business_date: str) -> str:
    compact = event_uuid.replace("-", "").upper()
    return f"NKT-{business_date.replace('-', '')}-{compact[:8]}"


def _customer_name(snapshot, customer):
    for row in list(snapshot.get("customers") or []):
        if str(row.get("customer") or row.get("name") or "") == customer:
            return str(row.get("customer_name") or customer)
    return customer


def _item_name_map(snapshot):
    return {
        str(row.get("item_code") or ""): str(row.get("item_name") or row.get("item_code") or "")
        for row in list(snapshot.get("items") or [])
    }


def _print_html(*, company, order_reference, business_date, customer, customer_name, items, item_names):
    total = sum(flt(x.get("qty")) * flt(x.get("rate")) for x in items)
    rows = []
    for line in items:
        item_code = str(line.get("item_code") or "")
        qty = flt(line.get("qty"))
        rate = flt(line.get("rate"))
        amount = qty * rate
        rows.append(
            "<tr>"
            f"<td>{html.escape(item_code)}</td>"
            f"<td>{html.escape(item_names.get(item_code) or item_code)}</td>"
            f"<td>{html.escape(str(line.get('warehouse') or ''))}</td>"
            f"<td class='num'>{qty:,.2f}</td>"
            f"<td class='num'>{rate:,.2f}</td>"
            f"<td class='num'>{amount:,.2f}</td>"
            "</tr>"
        )
    return f"""<!doctype html>
<html><head><meta charset="utf-8"><title>{html.escape(order_reference)}</title>
<style>
@page {{ size: A5 portrait; margin: 9mm; }}
body {{ font-family: Tahoma, Arial, sans-serif; font-size: 11px; color: #111; margin: 0; }}
h1 {{ font-size: 16px; margin: 0 0 2px; }}
.meta {{ display:grid; grid-template-columns:95px 1fr; gap:2px 8px; margin:8px 0; }}
table {{ width:100%; border-collapse:collapse; margin-top:8px; }}
th,td {{ border:1px solid #777; padding:4px 5px; vertical-align:top; }}
th {{ font-size:10px; }} .num {{ text-align:right; white-space:nowrap; }}
.total {{ text-align:right; margin-top:8px; font-size:13px; }}
.sign {{ display:grid; grid-template-columns:1fr 1fr; gap:20px; margin-top:28px; }}
.sign div {{ border-top:1px solid #333; padding-top:4px; text-align:center; }}
</style></head><body>
<h1>{html.escape(company)}</h1><div><b>ORDER / RELEASE PAPER</b></div>
<div class="meta">
<div>Order No.</div><div><b>{html.escape(order_reference)}</b></div>
<div>Date</div><div>{html.escape(business_date)}</div>
<div>Customer</div><div>{html.escape(customer_name)} ({html.escape(customer)})</div>
</div>
<table><thead><tr><th>Item</th><th>Description</th><th>Warehouse</th><th>Qty</th><th>Rate</th><th>Amount</th></tr></thead>
<tbody>{''.join(rows)}</tbody></table>
<div class="total"><b>Total: {total:,.2f}</b></div>
<div class="sign"><div>Prepared by</div><div>Received by</div></div>
</body></html>"""


def _sanitized_local_result(event_uuid, *, ack, payload, snapshot, company, business_date):
    ref = _order_reference(event_uuid, business_date)
    items = [dict(x) for x in payload.get("items") or []]
    customer = str(payload.get("customer") or "")
    result = {
        "ok": True,
        "local_continuity": True,
        "order_reference": ref,
        "status": "Saved for processing",
        "business_date": business_date,
        "customer": customer,
        "customer_name": _customer_name(snapshot, customer),
        "line_count": len(items),
        "total": sum(flt(x.get("qty")) * flt(x.get("rate")) for x in items),
        "replayed": bool(ack.get("replay")),
        "durably_saved": bool(ack.get("durable_ack")),
        "print_html": _print_html(
            company=company,
            order_reference=ref,
            business_date=business_date,
            customer=customer,
            customer_name=_customer_name(snapshot, customer),
            items=items,
            item_names=_item_name_map(snapshot),
        ),
    }
    if set(result) & TECHNICAL_RESULT_KEYS:
        raise AssertionError("Frontline result leaked technical sync fields.")
    return result


def _pending_payload(event_uuid):
    row = frappe.db.get_value(
        "NKT Sync Pending Payload", event_uuid, ["event_family", "payload_json"], as_dict=True
    )
    if not row or row.event_family != ORDER_INTENT_FAMILY:
        return None
    try:
        payload = json.loads(row.payload_json or "{}")
    except Exception:
        return None
    return payload if isinstance(payload, dict) else None


def _saved_local_result(event_uuid, device_id):
    if not frappe.db.exists("NKT Sync Event", event_uuid):
        return None
    event = frappe.get_doc("NKT Sync Event", event_uuid)
    if event.event_family != ORDER_INTENT_FAMILY:
        return None
    payload = _pending_payload(event_uuid)
    if not payload:
        return None
    snapshot = _edge_snapshot(device_id)
    business_date = str(event.business_date or manila_now().date().isoformat())
    return _sanitized_local_result(
        event_uuid,
        ack={"replay": True, "durable_ack": True},
        payload=payload,
        snapshot=snapshot,
        company=str(payload.get("company") or ""),
        business_date=business_date,
    )



def _cashier_reference(event_uuid: str, business_date: str) -> str:
    compact = event_uuid.replace("-", "").upper()
    return f"NKT-PAY-{business_date.replace('-', '')}-{compact[:8]}"


def _cashier_payment_totals(payload: Dict[str, Any]) -> Dict[str, float]:
    merchandise = flt(payload.get("merchandise_total"))
    surcharge = flt(payload.get("card_surcharge_total"))
    collected = flt(payload.get("actual_collected_total"))
    change = sum(flt(x.get("change_amount")) for x in list(payload.get("payments") or []))
    return {
        "merchandise_total": merchandise,
        "card_surcharge_total": surcharge,
        "actual_collected_total": collected,
        "change_amount": change,
    }


def _cashier_payment_description(row: Dict[str, Any]) -> str:
    method = str(row.get("payment_method") or row.get("method") or "").strip()
    ref = str(row.get("reference_number") or row.get("reference") or "").strip()
    if method == "Check":
        bank = str(row.get("bank_or_provider") or row.get("provider") or "").strip()
        return f"Check {ref}" + (f" — {bank}" if bank else "")
    if method in {"GCash", "Maya", "Card", "Bank Transfer", "Online"} and ref:
        return f"{method} — {ref}"
    return method


def _cashier_print_html(*, company, receipt_reference, business_date, customer, customer_name, payload):
    items = [dict(x) for x in list(payload.get("items") or [])]
    payments = [dict(x) for x in list(payload.get("payments") or [])]
    totals = _cashier_payment_totals(payload)
    item_rows = []
    for line in items:
        code = str(line.get("item_code") or line.get("item") or "")
        qty = flt(line.get("qty") if line.get("qty") is not None else line.get("quantity"))
        rate = flt(line.get("rate") if line.get("rate") is not None else line.get("final_rate"))
        amount = flt(line.get("amount")) or qty * rate
        item_rows.append(
            "<tr>"
            f"<td>{html.escape(code)}</td><td class='num'>{qty:,.2f}</td>"
            f"<td class='num'>{rate:,.2f}</td><td class='num'>{amount:,.2f}</td>"
            "</tr>"
        )
    pay_rows = []
    for row in payments:
        method = str(row.get("payment_method") or row.get("method") or "")
        amount = flt(row.get("amount"))
        extra = ""
        if method == "Card":
            extra = f" + 2% {flt(row.get('card_surcharge')):,.2f}"
        if method == "Cash":
            extra = (
                f" | Tendered {flt(row.get('cash_tendered')):,.2f}"
                f" | Change {flt(row.get('change_amount')):,.2f}"
            )
        pay_rows.append(
            f"<tr><td>{html.escape(_cashier_payment_description(row))}</td>"
            f"<td class='num'>{amount:,.2f}{html.escape(extra)}</td></tr>"
        )
    return f"""<!doctype html>
<html><head><meta charset="utf-8"><title>{html.escape(receipt_reference)}</title>
<style>
@page {{ size: A5 portrait; margin: 9mm; }}
body {{ font-family:Tahoma,Arial,sans-serif; font-size:11px; color:#111; margin:0; }}
h1 {{ font-size:16px; margin:0 0 2px; }} .meta {{ margin:8px 0; }}
table {{ width:100%; border-collapse:collapse; margin-top:7px; }}
th,td {{ border:1px solid #777; padding:4px 5px; }} .num {{ text-align:right; white-space:nowrap; }}
.total {{ margin-top:8px; text-align:right; font-size:12px; }}
</style></head><body>
<h1>{html.escape(company)}</h1><div><b>PAYMENT ACKNOWLEDGMENT</b></div>
<div class="meta"><b>Receipt Ref:</b> {html.escape(receipt_reference)}<br>
<b>Date:</b> {html.escape(business_date)}<br>
<b>Customer:</b> {html.escape(customer_name)} ({html.escape(customer)})</div>
<table><thead><tr><th>Item</th><th>Qty</th><th>Rate</th><th>Amount</th></tr></thead>
<tbody>{''.join(item_rows)}</tbody></table>
<table><thead><tr><th>Payment</th><th>Amount</th></tr></thead>
<tbody>{''.join(pay_rows)}</tbody></table>
<div class="total"><b>Merchandise: {totals['merchandise_total']:,.2f}</b><br>
Card surcharge: {totals['card_surcharge_total']:,.2f}<br>
Actual collected: {totals['actual_collected_total']:,.2f}<br>
Change: {totals['change_amount']:,.2f}</div>
</body></html>"""


def _sanitized_cashier_result(event_uuid, *, payload, business_date, snapshot=None, replayed=False):
    customer = str(payload.get("customer") or "")
    if snapshot is not None:
        customer_name = _customer_name(snapshot, customer)
    else:
        customer_name = str(frappe.db.get_value("Customer", customer, "customer_name") or customer)
    ref = _cashier_reference(event_uuid, business_date)
    totals = _cashier_payment_totals(payload)
    result = {
        "ok": True,
        "local_continuity": True,
        "tender_recorded": True,
        "receipt_reference": ref,
        "status": "Payment recorded",
        "business_date": business_date,
        "customer": customer,
        "customer_name": customer_name,
        "line_count": len(list(payload.get("items") or [])),
        "total": totals["merchandise_total"],
        "card_surcharge_total": totals["card_surcharge_total"],
        "actual_collected_total": totals["actual_collected_total"],
        "change_amount": totals["change_amount"],
        "replayed": bool(replayed),
        "print_html": _cashier_print_html(
            company=str(payload.get("company") or ""),
            receipt_reference=ref,
            business_date=business_date,
            customer=customer,
            customer_name=customer_name,
            payload=payload,
        ),
    }
    if set(result) & TECHNICAL_RESULT_KEYS:
        raise AssertionError("Frontline Cashier result leaked technical sync fields.")
    return result


def _pending_tender_payload(event_uuid):
    row = frappe.db.get_value(
        "NKT Sync Pending Payload", event_uuid, ["event_family", "payload_json"], as_dict=True
    )
    if not row or row.event_family != TENDER_INTENT_FAMILY:
        return None
    try:
        payload = json.loads(row.payload_json or "{}")
    except Exception:
        return None
    return payload if isinstance(payload, dict) else None


def _cashier_fast_request_tender_projection(
    data: Dict[str, Any],
    saved_payload: Dict[str, Any],
) -> Dict[str, Any]:
    default_warehouse = str(saved_payload.get("default_warehouse") or "").strip()
    return {
        "company": str(saved_payload.get("company") or "").strip(),
        "customer": str(data.get("customer") or "").strip(),
        "cashier_shift": str(saved_payload.get("cashier_shift") or "").strip(),
        "settlement_location": str(saved_payload.get("settlement_location") or "").strip(),
        "default_warehouse": default_warehouse,
        "client_observed_at": str(saved_payload.get("client_observed_at") or "").strip(),
        "client_ui_version": str(saved_payload.get("client_ui_version") or FOUNDATION_VERSION).strip(),
        "items": [
            {
                "item_code": str(row.get("item_code") or "").strip(),
                "qty": flt(row.get("qty")),
                "rate": flt(row.get("rate")),
                "warehouse": str(row.get("warehouse") or default_warehouse).strip(),
            }
            for row in list(data.get("items") or [])
        ],
        "payments": [
            {
                "payment_method": str(row.get("method") or row.get("payment_method") or "").strip(),
                "amount": flt(row.get("amount")),
                "reference_number": str(
                    row.get("reference") or row.get("reference_number") or ""
                ).strip(),
                "bank_or_provider": str(
                    row.get("provider") or row.get("bank_or_provider") or ""
                ).strip(),
                "check_number": str(
                    row.get("check_number")
                    or row.get("reference")
                    or row.get("reference_number")
                    or ""
                ).strip(),
                "check_date": str(row.get("check_date") or "").strip(),
                "cash_tendered": flt(row.get("cash_tendered")),
                "change_amount": flt(row.get("change_amount")),
                "remarks": str(row.get("remarks") or "").strip(),
            }
            for row in list(data.get("payments") or [])
        ],
    }



def _require_saved_cashier_event_device(event, device_id: Optional[str]) -> None:
    _require_edge_device(device_id, mode=CASHIER_MODE)
    expected = str(getattr(event, "origin_device", "") or "").strip()
    supplied = str(device_id or "").strip()
    if not expected or expected != supplied:
        raise frappe.PermissionError(
            _("This saved payment belongs to another registered Cashier workstation.")
        )


def _canonical_json(value: Dict[str, Any]) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    )


def _replay_saved_edge_cashier_request(
    event_uuid: str,
    device_id: Optional[str],
    data: Dict[str, Any],
):
    if not frappe.db.exists("NKT Sync Event", event_uuid):
        return None
    event = frappe.get_doc("NKT Sync Event", event_uuid)
    if event.event_family != TENDER_INTENT_FAMILY:
        raise NKTIdempotencyConflict(
            _("This Fast Screen request identity is already used by another transaction type.")
        )
    _require_saved_cashier_event_device(event, device_id)

    saved_payload = _pending_tender_payload(event_uuid)
    if not saved_payload:
        # A committed/purged Edge event is intentionally not reconstructed locally.
        # Once Primary is reachable, the Primary route resolves the preserved journal.
        return None

    candidate = _normalize_cashier_tender_intent_payload(
        _cashier_fast_request_tender_projection(data, saved_payload)
    )
    if _canonical_json(candidate) != _canonical_json(saved_payload):
        raise NKTIdempotencyConflict(
            _(
                "This payment request was already saved with different basket or payment details. "
                "Use the original saved transaction instead of reusing its request identity."
            )
        )

    snapshot = _edge_snapshot(device_id, mode=CASHIER_MODE)
    return _sanitized_cashier_result(
        event_uuid,
        payload=saved_payload,
        business_date=str(event.business_date or manila_now().date().isoformat()),
        snapshot=snapshot,
        replayed=True,
    )


def _saved_edge_cashier_result(event_uuid, device_id):
    if not frappe.db.exists("NKT Sync Event", event_uuid):
        return None
    event = frappe.get_doc("NKT Sync Event", event_uuid)
    if event.event_family != TENDER_INTENT_FAMILY:
        return None
    _require_saved_cashier_event_device(event, device_id)
    payload = _pending_tender_payload(event_uuid)
    if not payload:
        return None
    snapshot = _edge_snapshot(device_id, mode=CASHIER_MODE)
    return _sanitized_cashier_result(
        event_uuid,
        payload=payload,
        business_date=str(event.business_date or manila_now().date().isoformat()),
        snapshot=snapshot,
        replayed=True,
    )


def _primary_preserved_cashier_result(event_uuid):
    if not frappe.db.exists("DocType", "NKT Primary Cashier Tender Intent"):
        return None
    if not frappe.db.exists("NKT Primary Cashier Tender Intent", event_uuid):
        return None
    doc = frappe.get_doc("NKT Primary Cashier Tender Intent", event_uuid)
    try:
        payload = json.loads(doc.canonical_payload_json or "{}")
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None
    return _sanitized_cashier_result(
        event_uuid,
        payload=payload,
        business_date=str(doc.business_date or manila_now().date().isoformat()),
        snapshot=None,
        replayed=True,
    )


def _edge_check_preflight(customer, check_number, issuing_bank):
    customer = str(customer or "").strip()
    check_norm = "".join(str(check_number or "").strip().lower().split())
    bank_norm = "".join(str(issuing_bank or "").strip().lower().split())
    if not customer or not check_norm or not bank_norm:
        raise frappe.ValidationError(_("Customer, Check Number, and Issuing Bank are required."))

    posted = frappe.db.sql(
        """
        SELECT pd.parent
        FROM `tabNKT Payment Detail` pd
        INNER JOIN `tabNKT Payment Receipt` pr ON pr.name = pd.parent
        WHERE pd.parenttype='NKT Payment Receipt'
          AND pr.docstatus=1
          AND pr.customer=%s
          AND pd.payment_method='Check'
          AND REPLACE(LOWER(TRIM(COALESCE(pd.bank_or_provider,''))), ' ', '')=%s
          AND REPLACE(LOWER(TRIM(COALESCE(NULLIF(pd.check_number,''),pd.reference_number,''))), ' ', '')=%s
        LIMIT 1
        """,
        (customer, bank_norm, check_norm),
    )
    if posted:
        return {
            "exact_duplicate": True,
            "exact_match": {
                "customer": customer,
                "issuing_bank": issuing_bank,
                "payment_receipt": posted[0][0],
            },
            "other_matches": [],
        }

    pendings = frappe.get_all(
        "NKT Sync Pending Payload",
        filters={"event_family": TENDER_INTENT_FAMILY},
        fields=["name", "payload_json"],
        limit_page_length=500,
    )
    for pending in pendings:
        try:
            payload = json.loads(pending.payload_json or "{}")
        except Exception:
            continue
        if str(payload.get("customer") or "") != customer:
            continue
        for row in list(payload.get("payments") or []):
            if row.get("payment_method") != "Check":
                continue
            other_check = "".join(
                str(row.get("check_number") or row.get("reference_number") or "").strip().lower().split()
            )
            other_bank = "".join(
                str(row.get("bank_or_provider") or "").strip().lower().split()
            )
            if other_check == check_norm and other_bank == bank_norm:
                return {
                    "exact_duplicate": True,
                    "exact_match": {
                        "customer": customer,
                        "issuing_bank": issuing_bank,
                        "payment_receipt": "",
                    },
                    "other_matches": [],
                }
    return {"exact_duplicate": False, "exact_match": None, "other_matches": []}



@frappe.whitelist()
def get_fast_ui_bootstrap(mode: str, device_id=None):
    role = _runtime_role()
    mode = str(mode or "").strip().lower()
    if role == PRIMARY_ROLE:
        return _fast_ui().get_fast_ui_bootstrap(mode)
    if mode == ENCODER_MODE:
        return _edge_bootstrap(device_id)
    if mode == CASHIER_MODE:
        return _edge_cashier_bootstrap(device_id)
    raise frappe.PermissionError(_("Fast Screen service unavailable."))


@frappe.whitelist()
def search_items(search_text="", warehouse=None, limit=12, device_id=None):
    if _runtime_role() == PRIMARY_ROLE:
        return _fast_ui().search_items(search_text, warehouse, limit)
    return _edge_search_items(search_text, warehouse, limit, device_id)


@frappe.whitelist()
def search_customers(search_text="", limit=12, device_id=None):
    if _runtime_role() == PRIMARY_ROLE:
        return _fast_ui().search_customers(search_text, limit)
    return _edge_search_customers(search_text, limit, device_id)


@frappe.whitelist()
def get_item_context(item_code, warehouse=None, device_id=None):
    if _runtime_role() == PRIMARY_ROLE:
        return _fast_ui().get_item_context(item_code, warehouse)
    return _edge_get_item_context(item_code, warehouse, device_id)


@frappe.whitelist()
def create_fast_customer(customer_name, customer_type="Individual", mobile_no=""):
    if _runtime_role() == EDGE_ROLE:
        raise frappe.PermissionError(
            _("New Customer creation will resume when the main connection is restored.")
        )
    return _fast_customer().create_fast_customer(customer_name, customer_type, mobile_no)


@frappe.whitelist()
def preflight_incoming_check(customer, check_number, issuing_bank, mode="cashier", device_id=None):
    if _runtime_role() == EDGE_ROLE:
        mode = str(mode or "").strip().lower()
        if mode != CASHIER_MODE:
            raise frappe.PermissionError(_("Payment entry requires the main connection."))
        _require_edge_device(device_id, mode=CASHIER_MODE)
        return _edge_check_preflight(customer, check_number, issuing_bank)
    return _fast_ui().preflight_incoming_check(customer, check_number, issuing_bank, mode)


@frappe.whitelist()
def finalize_cashier_fast_transaction(payload, device_id=None):
    data = _parse_payload(payload)
    event_uuid = _uuid(data.get("request_id"))

    if _runtime_role() == PRIMARY_ROLE:
        preserved = _primary_preserved_cashier_result(event_uuid)
        if preserved:
            return preserved
        return _fast_ui().finalize_cashier_fast_transaction(payload)

    user = _require_cashier()
    snapshot = _edge_snapshot(device_id, mode=CASHIER_MODE)

    saved_replay = _replay_saved_edge_cashier_request(event_uuid, device_id, data)
    if saved_replay:
        return saved_replay

    boot = _edge_cashier_bootstrap(device_id)
    if not boot.get("posting_enabled") or not boot.get("open_shift"):
        raise frappe.ValidationError(
            boot.get("shift_block_reason")
            or boot.get("setup_error")
            or _("Open today's Cashier Shift before finalizing.")
        )

    customer = str(data.get("customer") or "").strip()
    items = list(data.get("items") or [])
    payments = list(data.get("payments") or [])
    if not customer or not items or not payments:
        frappe.throw(_("Customer, items, and confirmed payment are required."))

    from nkt_operations.nkt_store_operations import manager_authorization as nkt_manager_pin
    price_authorization = nkt_manager_pin.validate_price_authorization_for_finalize(data)

    shift = dict(boot.get("open_shift") or {})
    now_ph = manila_now()
    business_date = now_ph.date().isoformat()
    tender_payload = {
        "company": str(boot.get("company") or ""),
        "customer": customer,
        "cashier_shift": str(shift.get("name") or ""),
        "settlement_location": str(shift.get("settlement_location") or ""),
        "default_warehouse": str(boot.get("default_warehouse") or ""),
        "client_observed_at": now_ph.isoformat(timespec="seconds"),
        "client_ui_version": FOUNDATION_VERSION,
        "items": [
            {
                "item_code": str(row.get("item_code") or "").strip(),
                "qty": flt(row.get("qty")),
                "rate": flt(row.get("rate")),
                "warehouse": str(row.get("warehouse") or boot.get("default_warehouse") or "").strip(),
            }
            for row in items
        ],
        "payments": [
            {
                "payment_method": str(row.get("method") or row.get("payment_method") or "").strip(),
                "amount": flt(row.get("amount")),
                "reference_number": str(row.get("reference") or row.get("reference_number") or "").strip(),
                "bank_or_provider": str(row.get("provider") or row.get("bank_or_provider") or "").strip(),
                "check_number": str(row.get("check_number") or row.get("reference") or row.get("reference_number") or "").strip(),
                "check_date": str(row.get("check_date") or "").strip(),
                "cash_tendered": flt(row.get("cash_tendered")),
                "change_amount": flt(row.get("change_amount")),
                "remarks": str(row.get("remarks") or "").strip(),
            }
            for row in payments
        ],
    }
    if price_authorization:
        tender_payload["price_authorization"] = nkt_manager_pin.offline_evidence(price_authorization)

    ack = accept_cashier_tender_intent_at_edge(
        event_uuid,
        str(device_id),
        business_date,
        now_ph.isoformat(timespec="seconds"),
        tender_payload,
        user=user,
    )
    canonical = _pending_tender_payload(event_uuid)
    if not canonical:
        raise frappe.ValidationError(_("Payment was saved but its local receipt details are unavailable."))
    return _sanitized_cashier_result(
        event_uuid,
        payload=canonical,
        business_date=business_date,
        snapshot=snapshot,
        replayed=bool(ack.get("replay")),
    )


@frappe.whitelist()
def finalize_encoder_fast_transaction(payload, device_id=None):
    if _runtime_role() == PRIMARY_ROLE:
        return _fast_ui().finalize_encoder_fast_transaction(payload)

    user = _require_encoder()
    snapshot = _edge_snapshot(device_id)
    data = _parse_payload(payload)

    if _has_tender_data(data):
        return {
            "ok": False,
            "local_continuity": True,
            "order_only_required": True,
            "message": (
                "Payment was not recorded. Save the order only and record the truthful "
                "payment/account settlement after the main connection is restored."
            ),
        }

    event_uuid = _uuid(data.get("request_id"))
    customer = str(data.get("customer") or "").strip()
    items = list(data.get("items") or [])
    if not customer or not items:
        frappe.throw(_("Customer and at least one item are required."))

    boot = _edge_bootstrap(device_id)
    company = str(boot.get("company") or "").strip()
    default_warehouse = str(boot.get("default_warehouse") or "").strip()
    now_ph = manila_now()
    business_date = now_ph.date().isoformat()

    intent_payload = {
        "company": company,
        "customer": customer,
        "default_warehouse": default_warehouse,
        "account_sale": False,
        "notes": "",
        "client_observed_at": now_ph.isoformat(timespec="seconds"),
        "client_ui_version": FOUNDATION_VERSION,
        "items": [
            {
                "item_code": str(row.get("item_code") or "").strip(),
                "qty": flt(row.get("qty")),
                "rate": flt(row.get("rate")),
                "warehouse": str(row.get("warehouse") or default_warehouse).strip(),
            }
            for row in items
        ],
    }

    ack = accept_customer_order_intent_at_edge(
        event_uuid, str(device_id), business_date, now_ph.isoformat(timespec="seconds"),
        intent_payload, user=user,
    )
    return _sanitized_local_result(
        event_uuid, ack=ack, payload=intent_payload, snapshot=snapshot,
        company=company, business_date=business_date,
    )


@frappe.whitelist()
def get_fast_request_status(mode, request_id, device_id=None):
    mode = str(mode or "").strip().lower()
    event_uuid = _uuid(request_id)

    if _runtime_role() == PRIMARY_ROLE:
        status = _fast_ui().get_fast_request_status(mode, request_id)
        if status and status.get("found"):
            return status
        if mode == CASHIER_MODE:
            preserved = _primary_preserved_cashier_result(event_uuid)
            if preserved:
                return {"found": True, "submitted": True, "result": preserved}
        return status

    if mode == ENCODER_MODE:
        result = _saved_local_result(event_uuid, device_id)
    elif mode == CASHIER_MODE:
        _require_cashier()
        result = _saved_edge_cashier_result(event_uuid, device_id)
    else:
        raise frappe.PermissionError(_("Fast Screen service unavailable."))

    if not result:
        return {"found": False}
    return {"found": True, "submitted": True, "result": result}
