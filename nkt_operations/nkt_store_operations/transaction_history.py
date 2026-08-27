from __future__ import annotations

import hashlib
import inspect
import json
import uuid
from collections import defaultdict
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import frappe
from frappe import _
from frappe.utils import cint, flt, get_datetime, now_datetime, today

VERSION = "R4-UI7A"
PRINT_EVENT_DOCTYPE = "NKT Transaction History Print Event"
CASHIER_CLIENT_SCRIPT = "NKT R4 UI6 Cashier F8 Transaction History"
ENCODER_CLIENT_SCRIPT = "NKT R4 UI6 Encoder F8 Transaction History"
F6_CLIENT_SCRIPT = "NKT R8A Encoder Frontline Presentation + F6 Item History"

ELEVATED_ROLES = {"NKT Store Manager", "NKT OWNER", "NKT ADMINISTRATOR"}
MODE_ROLES = {"cashier": "NKT Cashier", "encoder": "NKT Encoder"}
MODE_DOCTYPES = {"cashier": "NKT Cashier Sale", "encoder": "NKT Customer Order"}
PAGE_LENGTH_DEFAULT = 100
PAGE_LENGTH_MAX = 300
SCREEN_SCAN_LIMIT = 10_000
PRINT_SCAN_LIMIT = 12_000
TOLERANCE = 0.000001
STANDARD_FIELDS = {
    "name", "owner", "creation", "modified", "modified_by", "docstatus",
    "parent", "parenttype", "parentfield", "idx",
}

PAPER_OPTIONS = {
    "long": {"label": "Long Bond 8.5 x 13 Portrait", "page_css": "8.5in 13in portrait", "rows_5pt": 86, "rows_4pt": 110},
    "short": {"label": "Short Bond Letter 8.5 x 11 Portrait", "page_css": "letter portrait", "rows_5pt": 70, "rows_4pt": 90},
    "a4": {"label": "A4 210 x 297 mm Portrait", "page_css": "A4 portrait", "rows_5pt": 78, "rows_4pt": 100},
}
DENSITY_OPTIONS = {
    "5": {"label": "5 pt Compact", "font_pt": 5},
    "4": {"label": "4 pt Maximum Density", "font_pt": 4},
}
DETAIL_OPTIONS = {
    "summary": "Summary Only",
    "details": "Include Item and Payment Details",
}
PAYMENT_LABELS = ("CASH", "CHECK", "GCASH", "MAYA", "CARD", "BANK TRANSFER", "ONLINE", "ACCOUNT", "RETURN CREDIT", "SPLIT")


REPRINT_ROLES = {"NKT Encoder", "NKT Store Manager", "NKT OWNER", "NKT ADMINISTRATOR"}
DEVICE_REGISTRATION_ROLES = {"NKT OWNER", "NKT ADMINISTRATOR"}
TERMINAL_DEVICE_STATUSES = {"Revoked", "Lost/Stolen", "Retired"}
RECEIPT_PAPER_OPTIONS = {
    "half_short": {"label": "Half Short Bond 8.5 x 5.5 Landscape", "page_css": "8.5in 5.5in landscape"},
    "a5": {"label": "A5 210 x 148 mm Landscape", "page_css": "A5 landscape"},
}


def _clean(value: Any, max_length: int = 500) -> str:
    text = str(value or "").strip()
    if len(text) > max_length:
        frappe.throw(_("Transaction History filter text is too long."))
    return text


def _public_payment_number(value: Any) -> str:
    """Discreet frontline/customer payment number; canonical link stays server-side."""
    text = str(value or "").strip()
    prefix = "NKT-PAY-"
    if text.startswith(prefix):
        suffix = text[len(prefix):]
        if suffix.isdigit():
            return f"P{int(suffix):06d}"
    return text


def _public_payment_remarks(value: Any) -> str:
    """Strip machine reconciliation prose from customer/frontline receipt views."""
    text = str(value or "").strip()
    if text.startswith("Cashier account collection NKT-COL-CASH-"):
        return ""
    if text.startswith("Verified Customer Account Collection NKT-COL-CASH-"):
        return ""
    return text


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _doctype_exists(doctype: str) -> bool:
    return bool(frappe.db.exists("DocType", doctype))


def _available_fields(doctype: str, candidates: Sequence[str]) -> List[str]:
    if not _doctype_exists(doctype):
        return []
    meta = frappe.get_meta(doctype)
    return [field for field in candidates if field in STANDARD_FIELDS or meta.has_field(field)]


def _first_field(doctype: str, candidates: Sequence[str]) -> Optional[str]:
    fields = _available_fields(doctype, candidates)
    return fields[0] if fields else None


def _chunks(values: Iterable[str], size: int = 500) -> Iterable[List[str]]:
    batch: List[str] = []
    for value in values:
        if not value:
            continue
        batch.append(str(value))
        if len(batch) >= size:
            yield batch
            batch = []
    if batch:
        yield batch


def _session_user() -> str:
    user = str(frappe.session.user or "")
    if not user or user == "Guest":
        raise frappe.PermissionError(_("Login is required to view Transaction History."))
    return user


def _normalize_mode(mode: Any) -> str:
    mode = _clean(mode, 20).lower()
    if mode not in MODE_DOCTYPES:
        frappe.throw(_("Transaction History mode is invalid."))
    return mode


def _role_context(mode: Any, user: Optional[str] = None) -> Dict[str, Any]:
    mode = _normalize_mode(mode)
    user = user or _session_user()
    roles = set(frappe.get_roles(user) or [])
    elevated = user == "Administrator" or bool(roles.intersection(ELEVATED_ROLES))
    own_role = MODE_ROLES[mode] in roles
    if not (elevated or own_role):
        raise frappe.PermissionError(_("Transaction History is unavailable for this role."))
    return {
        "mode": mode,
        "user": user,
        "roles": roles,
        "elevated": elevated,
        "own_only": not elevated,
        "direct_print": True,
        "title": "Transaction History" if elevated else "My Transactions",
    }


def _user_identity(user: str) -> Dict[str, str]:
    user = str(user or "").strip()
    if not user:
        return {"user": "", "full_name": "", "display_name": ""}
    fields = _available_fields("User", ["name", "full_name", "first_name", "last_name", "email"])
    row = frappe.db.get_value("User", user, fields, as_dict=True) if fields else None
    row = dict(row or {})
    full_name = str(row.get("full_name") or "").strip()
    if not full_name:
        full_name = " ".join(part for part in [str(row.get("first_name") or "").strip(), str(row.get("last_name") or "").strip()] if part).strip()
    if not full_name:
        full_name = str(row.get("email") or user).split("@")[0]
    parts = [part for part in full_name.replace(",", " ").split() if part]
    if len(parts) >= 2:
        display = f"{parts[0][0].upper()}. {parts[-1]}"
    else:
        display = full_name[:20]
    return {"user": user, "full_name": full_name, "display_name": display}


def _identity_map(users: Iterable[str]) -> Dict[str, Dict[str, str]]:
    return {user: _user_identity(user) for user in sorted({str(user) for user in users if user})}


def _parse_datetime(value: Any, label: str) -> Optional[datetime]:
    text = _clean(value, 80)
    if not text:
        return None
    try:
        return get_datetime(text.replace("T", " "))
    except Exception:
        frappe.throw(_("{0} is not a valid date and time.").format(label))
    return None


def _fmt_datetime(value: Any) -> str:
    if not value:
        return ""
    try:
        return get_datetime(value).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return str(value)


def _fmt_date(value: Any) -> str:
    if not value:
        return ""
    try:
        return get_datetime(value).strftime("%Y-%m-%d")
    except Exception:
        return str(value)


def _datetime_value(row: Dict[str, Any], doctype: str, candidates: Sequence[str]) -> Any:
    for field in candidates:
        if field in row and row.get(field):
            return row.get(field)
    return row.get("creation")


def _get_children(doctype: str, parents: Iterable[str], fields: Sequence[str]) -> Dict[str, List[Dict[str, Any]]]:
    output: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    if not _doctype_exists(doctype):
        return output
    actual_fields = _available_fields(doctype, ["parent", "idx", *fields])
    if "parent" not in actual_fields:
        return output
    names = sorted({str(name) for name in parents if name})
    for batch in _chunks(names):
        rows = frappe.get_all(
            doctype,
            filters={"parent": ["in", batch]},
            fields=actual_fields,
            order_by="parent asc, idx asc",
            limit_page_length=0,
        )
        for row in rows:
            output[str(row.parent)].append(dict(row))
    return output


def _shift_map(names: Iterable[str]) -> Dict[str, Dict[str, Any]]:
    names = sorted({str(name) for name in names if name})
    if not names or not _doctype_exists("NKT Cashier Shift"):
        return {}
    fields = _available_fields("NKT Cashier Shift", ["name", "shift_start", "shift_end", "status", "cashier", "settlement_location"])
    rows = frappe.get_all("NKT Cashier Shift", filters={"name": ["in", names]}, fields=fields, limit_page_length=0)
    return {str(row.name): dict(row) for row in rows}


def _payment_method_label(method: Any) -> str:
    method = str(method or "").strip()
    if not method:
        return ""
    upper = method.upper()
    if upper == "BANK TRANSFER":
        return "BANK TRANSFER"
    if upper in {"OTHER ONLINE", "ONLINE"}:
        return "ONLINE"
    return upper


def _payment_summary(payments: Sequence[Dict[str, Any]], fallback: str = "", include_cash_tender: bool = False) -> Tuple[str, float, List[Dict[str, Any]]]:
    public: List[Dict[str, Any]] = []
    methods: List[str] = []
    account_amount = 0.0
    for row in payments:
        amount = flt(row.get("amount"))
        method = _payment_method_label(row.get("payment_method"))
        if amount <= TOLERANCE and not method:
            continue
        if method and method not in methods:
            methods.append(method)
        if method == "ACCOUNT":
            account_amount += amount
        payment_public = {
            "method": method,
            "amount": amount,
            "collected_amount": flt(row.get("collected_amount") or amount),
            "card_surcharge": flt(row.get("card_surcharge")),
            "reference_number": str(row.get("reference_number") or ""),
            "reference_datetime": _fmt_datetime(row.get("reference_datetime")),
            "bank_or_provider": str(row.get("bank_or_provider") or ""),
            "check_number": str(row.get("check_number") or ""),
            "check_date": _fmt_date(row.get("check_date")),
            "remarks": str(row.get("remarks") or ""),
        }
        if include_cash_tender and method == "CASH":
            cash_tendered = flt(row.get("cash_tendered"))
            change_amount = flt(row.get("change_amount"))
            payment_public.update({
                "cash_tendered": cash_tendered,
                "change_amount": change_amount,
                "cash_tender_recorded": cash_tendered > TOLERANCE or change_amount > TOLERANCE,
            })
        public.append(payment_public)
    if len(methods) > 1:
        label = "SPLIT"
    elif methods:
        label = methods[0]
    else:
        label = _payment_method_label(fallback)
    return label, account_amount, public


def _item_public(rows: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    output = []
    for row in rows:
        qty = flt(row.get("quantity") or row.get("qty"))
        standard = flt(row.get("standard_rate"))
        rate = flt(row.get("final_rate") or row.get("rate"))
        amount = flt(row.get("amount") or qty * rate)
        output.append({
            "item_code": str(row.get("item") or row.get("item_code") or ""),
            "item_name": str(row.get("item_name") or row.get("description") or row.get("item") or row.get("item_code") or ""),
            "quantity": qty,
            "uom": str(row.get("uom") or ""),
            "standard_rate": standard,
            "rate": rate,
            "amount": amount,
            "warehouse": str(row.get("source_warehouse") or row.get("warehouse") or ""),
            "remarks": str(row.get("remarks") or ""),
        })
    return output


def _gross_and_adjustment(items: Sequence[Dict[str, Any]], net_amount: float) -> Tuple[float, float]:
    gross = 0.0
    for row in items:
        qty = flt(row.get("quantity"))
        standard = flt(row.get("standard_rate"))
        rate = flt(row.get("rate"))
        gross += qty * (standard if standard > TOLERANCE else rate)
    if gross <= TOLERANCE:
        gross = flt(net_amount)
    return gross, gross - flt(net_amount)


def _return_context(mode: str, source_names: Iterable[str], source_items: Dict[str, List[Dict[str, Any]]]) -> Dict[str, Dict[str, Any]]:
    source_names = sorted({str(name) for name in source_names if name})
    if not source_names or not _doctype_exists("NKT Return Exchange Declaration"):
        return {}
    field = "old_cashier_sale" if mode == "cashier" else "old_customer_order"
    if not frappe.get_meta("NKT Return Exchange Declaration").has_field(field):
        return {}
    fields = _available_fields("NKT Return Exchange Declaration", ["name", field, "transaction_type", "posting_status", "docstatus", "status", "entry_datetime"])
    rows = frappe.get_all(
        "NKT Return Exchange Declaration",
        filters={field: ["in", source_names]},
        fields=fields,
        order_by="entry_datetime asc, creation asc",
        limit_page_length=0,
    )
    returned = _get_children("NKT Return Exchange Returned Item", [row.name for row in rows], ["quantity", "item", "uom"])
    output: Dict[str, Dict[str, Any]] = {}
    for declaration in rows:
        if cint(declaration.get("docstatus")) == 2 or str(declaration.get("posting_status") or "") == "Cancelled":
            continue
        source = str(declaration.get(field) or "")
        info = output.setdefault(source, {"has_return": False, "has_exchange": False, "returned_qty": 0.0, "declarations": []})
        tx_type = str(declaration.get("transaction_type") or "")
        info["has_return"] = info["has_return"] or tx_type == "Return"
        info["has_exchange"] = info["has_exchange"] or tx_type == "Exchange"
        qty = sum(abs(flt(row.get("quantity"))) for row in returned.get(str(declaration.name), []))
        info["returned_qty"] += qty
        info["declarations"].append({"name": str(declaration.name), "transaction_type": tx_type, "quantity": qty})
    for source, info in output.items():
        original_qty = sum(abs(flt(row.get("quantity"))) for row in source_items.get(source, []))
        info["original_qty"] = original_qty
    return output


def _normalized_status(docstatus: int, source_status: str, return_info: Optional[Dict[str, Any]], *, encoder_unmatched: bool = False) -> str:
    source_status = str(source_status or "")
    if cint(docstatus) == 2 or "cancel" in source_status.lower() or "revers" in source_status.lower():
        return "Cancelled/Reversed"
    info = return_info or {}
    if info.get("has_exchange"):
        return "Exchanged"
    if info.get("has_return"):
        returned = flt(info.get("returned_qty"))
        original = flt(info.get("original_qty"))
        if original > TOLERANCE and returned + TOLERANCE >= original:
            return "Fully Returned"
        return "Partially Returned"
    if encoder_unmatched:
        return "Unmatched"
    lower = source_status.lower()
    if cint(docstatus) == 0 or any(token in lower for token in ("draft", "pending", "awaiting", "unpaid", "partially")):
        return source_status or "Pending"
    return "Active"


def _date_filters(field: str, parsed: Dict[str, Any]) -> Dict[str, Any]:
    filters: Dict[str, Any] = {}
    start = parsed.get("from_datetime")
    end = parsed.get("to_datetime")
    if start and end:
        filters[field] = ["between", [start, end]]
    elif start:
        filters[field] = [">=", start]
    elif end:
        filters[field] = ["<=", end]
    return filters


def _primary_rows(parsed: Dict[str, Any]) -> List[Dict[str, Any]]:
    mode = parsed["mode"]
    dt = MODE_DOCTYPES[mode]
    if not _doctype_exists(dt):
        return []
    user_field = "cashier" if mode == "cashier" else "encoder"
    datetime_candidates = (
        ["custom_nkt_offline_physical_settled_at", "custom_nkt_offline_captured_at", "sale_datetime", "creation"]
        if mode == "cashier"
        else ["custom_nkt_offline_physical_settled_at", "custom_nkt_offline_captured_at", "creation"]
    )
    datetime_field = _first_field(dt, datetime_candidates) or "creation"
    common = [
        "name", "owner", "creation", "modified", "docstatus", user_field, "customer", "customer_name", "status",
        "grand_total", "notes", "source_order_slip", "cashier_shift", "default_warehouse",
        "total_account_charge", "total_collected", "total_payment", "linked_payment_receipt", "matched_customer_order",
        "declared_account", "declared_total_collected", "declared_payment_total", "payment_arrangement", "payment_status",
        "cashier_reconciliation_status", "matched_cashier_sale", "account_sale", "custom_nkt_plate_number",
        *datetime_candidates,
    ]
    fields = _available_fields(dt, common)
    filters = _date_filters(datetime_field, parsed)
    if parsed.get("customer") and "customer" in fields:
        filters["customer"] = parsed["customer"]
    if parsed.get("scope_user") and user_field in fields:
        filters[user_field] = parsed["scope_user"]
    rows = frappe.get_all(dt, filters=filters, fields=fields, order_by=f"{datetime_field} desc, creation desc", limit_page_length=SCREEN_SCAN_LIMIT)
    names = [str(row.name) for row in rows]
    item_dt = "NKT Cashier Sale Item" if mode == "cashier" else "NKT Customer Order Item"
    payment_dt = "NKT Payment Detail" if mode == "cashier" else "NKT Declared Payment"
    item_fields = ["item", "item_code", "item_name", "description", "quantity", "uom", "standard_rate", "final_rate", "rate", "amount", "source_warehouse", "warehouse", "remarks"]
    payment_fields = ["payment_method", "amount", "collected_amount", "cash_tendered", "change_amount", "card_surcharge", "reference_number", "reference_datetime", "bank_or_provider", "check_number", "check_date", "remarks"]
    items_map_raw = _get_children(item_dt, names, item_fields)
    payment_map = _get_children(payment_dt, names, payment_fields)
    items_map = {name: _item_public(items) for name, items in items_map_raw.items()}
    shifts = _shift_map([row.get("cashier_shift") for row in rows])
    matched_sales = [str(row.get("matched_cashier_sale") or "") for row in rows if row.get("matched_cashier_sale")]
    matched_sale_map: Dict[str, Dict[str, Any]] = {}
    if mode == "encoder" and matched_sales and _doctype_exists("NKT Cashier Sale"):
        sf = _available_fields("NKT Cashier Sale", ["name", "cashier_shift"])
        for row in frappe.get_all("NKT Cashier Sale", filters={"name": ["in", matched_sales]}, fields=sf, limit_page_length=0):
            matched_sale_map[str(row.name)] = dict(row)
        shifts.update(_shift_map([row.get("cashier_shift") for row in matched_sale_map.values()]))
    return_map = _return_context(mode, names, items_map)
    user_ids = set()
    output: List[Dict[str, Any]] = []
    for raw in rows:
        row = dict(raw)
        name = str(row.get("name") or "")
        operator = str(row.get(user_field) or row.get("owner") or "")
        user_ids.add(operator)
        items = items_map.get(name, [])
        payments = payment_map.get(name, [])
        fallback = str(row.get("payment_arrangement") or "")
        payment_label, account_from_rows, payment_public = _payment_summary(payments, fallback, include_cash_tender=(mode == "cashier"))
        net = flt(row.get("grand_total"))
        gross, adjustment = _gross_and_adjustment(items, net)
        account_amount = flt(row.get("total_account_charge") or row.get("declared_account"))
        if account_amount <= TOLERANCE:
            account_amount = account_from_rows
        if mode == "encoder" and cint(row.get("account_sale")) and account_amount <= TOLERANCE:
            account_amount = max(net - flt(row.get("declared_total_collected")), 0.0)
        timestamp = _datetime_value(row, dt, datetime_candidates)
        shift_name = str(row.get("cashier_shift") or "")
        if mode == "encoder" and not shift_name:
            matched = matched_sale_map.get(str(row.get("matched_cashier_sale") or "")) or {}
            shift_name = str(matched.get("cashier_shift") or "")
        shift = shifts.get(shift_name) or {}
        return_info = return_map.get(name) or {}
        encoder_unmatched = mode == "encoder" and str(row.get("cashier_reconciliation_status") or "").lower() == "unmatched" and cint(row.get("docstatus")) == 1
        status = _normalized_status(cint(row.get("docstatus")), str(row.get("status") or row.get("payment_status") or ""), return_info, encoder_unmatched=encoder_unmatched)
        output.append({
            "row_id": f"{mode}:{name}",
            "kind": "Sale" if mode == "cashier" else "Order",
            "source_doctype": dt,
            "source_name": name,
            "transaction_datetime": _fmt_datetime(timestamp),
            "secondary_date": _fmt_date(shift.get("shift_start") if mode == "cashier" else timestamp),
            "secondary_date_label": "Shift Date" if mode == "cashier" else "Encoded Date",
            "customer": str(row.get("customer") or ""),
            "customer_name": str(row.get("customer_name") or row.get("customer") or ""),
            "gross_amount": gross,
            "price_adjustment": adjustment,
            "net_amount": net,
            "account_amount": account_amount,
            "account_flag": account_amount > TOLERANCE,
            "payment_label": payment_label,
            "user": operator,
            "status": status,
            "source_status": str(row.get("status") or row.get("payment_status") or ""),
            "cashier_shift": shift_name,
            "os_no": str(row.get("source_order_slip") or ""),
            "plate_number": str(row.get("custom_nkt_plate_number") or ""),
            "remarks": str(row.get("notes") or ""),
            "items": items,
            "payments": payment_public,
            "return_exchange": return_info,
            "account_details": {
                "account_amount": account_amount,
                "payment_status": str(row.get("payment_status") or ""),
                "linked_payment_receipt": str(row.get("linked_payment_receipt") or ""),
                "matched_cashier_sale": str(row.get("matched_cashier_sale") or ""),
                "matched_customer_order": str(row.get("matched_customer_order") or ""),
            },
        })
    identities = _identity_map(user_ids)
    for row in output:
        identity = identities.get(row["user"]) or {"display_name": row["user"], "full_name": row["user"]}
        row["user_display"] = identity["display_name"]
        row["user_full_name"] = identity["full_name"]
    return output


def _standalone_payment_rows(parsed: Dict[str, Any]) -> List[Dict[str, Any]]:
    if not _doctype_exists("NKT Payment Receipt"):
        return []
    mode = parsed["mode"]
    user_field = "received_by" if mode == "cashier" else "encoded_by"
    fields = _available_fields("NKT Payment Receipt", [
        "name", "owner", "creation", "docstatus", "receipt_datetime", "payment_purpose", "customer", "customer_name",
        "customer_order", "source_cashier_sale", "received_by", "encoded_by", "receipt_status", "cashier_shift",
        "total_payment", "total_collected", "total_account_charge", "customer_advance_amount", "remaining_balance", "remarks",
        "custom_nkt_plate_number", "custom_nkt_source_order_slip",
    ])
    filters = _date_filters("receipt_datetime" if "receipt_datetime" in fields else "creation", parsed)
    if parsed.get("customer") and "customer" in fields:
        filters["customer"] = parsed["customer"]
    rows = frappe.get_all("NKT Payment Receipt", filters=filters, fields=fields, order_by="receipt_datetime desc, creation desc", limit_page_length=SCREEN_SCAN_LIMIT)
    candidate = []
    for raw in rows:
        row = dict(raw)
        purpose = str(row.get("payment_purpose") or "")
        standalone = purpose == "Account Collection" or (
            not row.get("source_cashier_sale") and not row.get("customer_order") and flt(row.get("customer_advance_amount")) > TOLERANCE
        )
        if not standalone:
            continue
        operator = str(row.get(user_field) or row.get("owner") or "")
        if parsed.get("scope_user") and operator != parsed["scope_user"]:
            continue
        row["_operator"] = operator
        candidate.append(row)
    names = [str(row.get("name")) for row in candidate]
    payments_map = _get_children("NKT Payment Detail", names, ["payment_method", "amount", "collected_amount", "cash_tendered", "change_amount", "card_surcharge", "reference_number", "reference_datetime", "bank_or_provider", "check_number", "check_date", "remarks"])
    shifts = _shift_map([row.get("cashier_shift") for row in candidate])
    identities = _identity_map(row.get("_operator") for row in candidate)
    output = []
    for row in candidate:
        name = str(row.get("name") or "")
        payment_label, account_from_rows, payment_public = _payment_summary(payments_map.get(name, []), include_cash_tender=(mode == "cashier"))
        total = flt(row.get("total_payment") or row.get("total_collected"))
        account_amount = flt(row.get("total_account_charge")) or account_from_rows
        operator = row["_operator"]
        identity = identities.get(operator) or {"display_name": operator, "full_name": operator}
        shift_name = str(row.get("cashier_shift") or "")
        shift = shifts.get(shift_name) or {}
        status_source = str(row.get("receipt_status") or "")
        status = "Cancelled/Reversed" if cint(row.get("docstatus")) == 2 or "cancel" in status_source.lower() or "reject" in status_source.lower() else ("Active" if status_source == "Completed" else status_source or "Pending")
        output.append({
            "row_id": f"payment:{mode}:{name}",
            "kind": "Account Payment",
            "source_doctype": "NKT Payment Receipt",
            "source_name": name,
            "display_source_name": _public_payment_number(name),
            "transaction_datetime": _fmt_datetime(row.get("receipt_datetime") or row.get("creation")),
            "secondary_date": _fmt_date(shift.get("shift_start") if mode == "cashier" else row.get("receipt_datetime") or row.get("creation")),
            "secondary_date_label": "Shift Date" if mode == "cashier" else "Encoded Date",
            "customer": str(row.get("customer") or ""),
            "customer_name": str(row.get("customer_name") or row.get("customer") or ""),
            "gross_amount": total,
            "price_adjustment": 0.0,
            "net_amount": total,
            "account_amount": max(account_amount, total if str(row.get("payment_purpose")) == "Account Collection" else 0.0),
            "account_flag": True,
            "payment_label": payment_label or "ACCOUNT",
            "user": operator,
            "user_display": identity["display_name"],
            "user_full_name": identity["full_name"],
            "status": status,
            "source_status": status_source,
            "cashier_shift": shift_name,
            "os_no": str(row.get("custom_nkt_source_order_slip") or ""),
            "plate_number": str(row.get("custom_nkt_plate_number") or ""),
            "remarks": _public_payment_remarks(row.get("remarks")),
            "items": [],
            "payments": payment_public,
            "return_exchange": {},
            "account_details": {
                "payment_purpose": str(row.get("payment_purpose") or ""),
                "remaining_balance": flt(row.get("remaining_balance")),
                "customer_advance_amount": flt(row.get("customer_advance_amount")),
            },
        })
    return output


def _advance_rows(parsed: Dict[str, Any]) -> List[Dict[str, Any]]:
    if not _doctype_exists("NKT Customer Advance"):
        return []
    fields = _available_fields("NKT Customer Advance", [
        "name", "owner", "creation", "docstatus", "posting_datetime", "customer", "customer_name", "source_payment_receipt",
        "source_customer_order", "original_advance_amount", "applied_amount", "available_advance_amount", "advance_status", "approved_by", "remarks",
    ])
    filters = _date_filters("posting_datetime" if "posting_datetime" in fields else "creation", parsed)
    if parsed.get("customer") and "customer" in fields:
        filters["customer"] = parsed["customer"]
    rows = [dict(row) for row in frappe.get_all("NKT Customer Advance", filters=filters, fields=fields, order_by="posting_datetime desc, creation desc", limit_page_length=SCREEN_SCAN_LIMIT)]
    receipt_names = [str(row.get("source_payment_receipt")) for row in rows if row.get("source_payment_receipt")]
    receipt_fields = _available_fields("NKT Payment Receipt", ["name", "received_by", "encoded_by", "cashier_shift"])
    receipt_map = {}
    if receipt_names and receipt_fields:
        receipt_map = {str(row.name): dict(row) for row in frappe.get_all("NKT Payment Receipt", filters={"name": ["in", receipt_names]}, fields=receipt_fields, limit_page_length=0)}
    shifts = _shift_map([receipt.get("cashier_shift") for receipt in receipt_map.values()])
    payment_map = _get_children("NKT Payment Detail", receipt_names, ["payment_method", "amount", "collected_amount", "cash_tendered", "change_amount", "card_surcharge", "reference_number", "reference_datetime", "bank_or_provider", "check_number", "check_date", "remarks"])
    mode = parsed["mode"]
    user_field = "received_by" if mode == "cashier" else "encoded_by"
    candidates = []
    for row in rows:
        receipt = receipt_map.get(str(row.get("source_payment_receipt") or "")) or {}
        operator = str(receipt.get(user_field) or row.get("owner") or "")
        if parsed.get("scope_user") and operator != parsed["scope_user"]:
            continue
        row["_operator"] = operator
        row["_receipt"] = receipt
        candidates.append(row)
    identities = _identity_map(row.get("_operator") for row in candidates)
    output = []
    for row in candidates:
        name = str(row.get("name") or "")
        receipt_name = str(row.get("source_payment_receipt") or "")
        payment_label, _, payment_public = _payment_summary(payment_map.get(receipt_name, []), "ADVANCE", include_cash_tender=(mode == "cashier"))
        operator = row["_operator"]
        identity = identities.get(operator) or {"display_name": operator, "full_name": operator}
        receipt = row.get("_receipt") or {}
        shift_name = str(receipt.get("cashier_shift") or "")
        shift = shifts.get(shift_name) or {}
        amount = flt(row.get("original_advance_amount"))
        source_status = str(row.get("advance_status") or "")
        if cint(row.get("docstatus")) == 2 or source_status == "Cancelled":
            status = "Cancelled/Reversed"
        elif source_status in {"Available", "Partially Used"}:
            status = "Active"
        else:
            status = source_status or "Active"
        timestamp = row.get("posting_datetime") or row.get("creation")
        output.append({
            "row_id": f"advance:{mode}:{name}",
            "kind": "Customer Advance",
            "source_doctype": "NKT Customer Advance",
            "source_name": name,
            "transaction_datetime": _fmt_datetime(timestamp),
            "secondary_date": _fmt_date(shift.get("shift_start") if mode == "cashier" else timestamp),
            "secondary_date_label": "Shift Date" if mode == "cashier" else "Encoded Date",
            "customer": str(row.get("customer") or ""),
            "customer_name": str(row.get("customer_name") or row.get("customer") or ""),
            "gross_amount": amount,
            "price_adjustment": 0.0,
            "net_amount": amount,
            "account_amount": 0.0,
            "account_flag": False,
            "payment_label": payment_label or "ADVANCE",
            "user": operator,
            "user_display": identity["display_name"],
            "user_full_name": identity["full_name"],
            "status": status,
            "source_status": source_status,
            "cashier_shift": shift_name,
            "os_no": "",
            "plate_number": "",
            "remarks": str(row.get("remarks") or ""),
            "items": [],
            "payments": payment_public,
            "return_exchange": {},
            "account_details": {
                "source_payment_receipt": receipt_name,
                "source_customer_order": str(row.get("source_customer_order") or ""),
                "original_advance_amount": amount,
                "applied_amount": flt(row.get("applied_amount")),
                "available_advance_amount": flt(row.get("available_advance_amount")),
            },
        })
    return output


def _parse_filters(filters: Any, mode: Any, include_paging: bool = True) -> Dict[str, Any]:
    context = _role_context(mode)
    if isinstance(filters, str):
        try:
            filters = json.loads(filters or "{}")
        except Exception:
            frappe.throw(_("Transaction History filters are invalid."))
    filters = dict(filters or {})
    requested_user = _clean(filters.get("user"), 180)
    if context["own_only"]:
        if requested_user and requested_user != context["user"]:
            raise frappe.PermissionError(_("Employees may view only their own transactions."))
        scope_user = context["user"]
    else:
        scope_user = requested_user
    account_filter = _clean(filters.get("account"), 10) or "All"
    if account_filter not in {"All", "Yes", "No"}:
        frappe.throw(_("Account filter is invalid."))
    sort_order = _clean(filters.get("sort_order"), 20) or "Newest First"
    if sort_order not in {"Newest First", "Oldest First"}:
        frappe.throw(_("Sort order is invalid."))
    payment = _clean(filters.get("payment"), 40).upper()
    if payment and payment not in PAYMENT_LABELS:
        frappe.throw(_("Payment filter is invalid."))
    parsed = {
        "mode": context["mode"],
        "access": context,
        "scope_user": scope_user,
        "customer": _clean(filters.get("customer"), 180),
        "item_code": _clean(filters.get("item_code"), 180),
        "plate_number": _clean(filters.get("plate_number"), 80),
        "os_no": _clean(filters.get("os_no"), 80),
        "from_datetime": _parse_datetime(filters.get("from_datetime"), "From Date / Time"),
        "to_datetime": _parse_datetime(filters.get("to_datetime"), "To Date / Time"),
        "amount_from": flt(filters.get("amount_from")) if str(filters.get("amount_from") or "").strip() else None,
        "amount_to": flt(filters.get("amount_to")) if str(filters.get("amount_to") or "").strip() else None,
        "account": account_filter,
        "payment": payment,
        "status": _clean(filters.get("status"), 100),
        "secondary_date": _clean(filters.get("secondary_date"), 20),
        "sort_order": sort_order,
    }
    if parsed["from_datetime"] and parsed["to_datetime"] and parsed["from_datetime"] > parsed["to_datetime"]:
        frappe.throw(_("From Date / Time must not be later than To Date / Time."))
    if parsed["amount_from"] is not None and parsed["amount_to"] is not None and parsed["amount_from"] > parsed["amount_to"]:
        frappe.throw(_("Amount From must not exceed Amount To."))
    if include_paging:
        parsed["page_start"] = max(0, cint(filters.get("page_start")))
        parsed["page_length"] = max(1, min(cint(filters.get("page_length")) or PAGE_LENGTH_DEFAULT, PAGE_LENGTH_MAX))
    return parsed


def _matches(row: Dict[str, Any], parsed: Dict[str, Any]) -> bool:
    if parsed.get("item_code"):
        if not any(str(item.get("item_code") or "") == parsed["item_code"] for item in row.get("items") or []):
            return False
    if parsed.get("plate_number") and parsed["plate_number"].lower() not in str(row.get("plate_number") or "").lower():
        return False
    if parsed.get("os_no") and parsed["os_no"].lower() not in str(row.get("os_no") or "").lower():
        return False
    if parsed.get("amount_from") is not None and flt(row.get("net_amount")) < parsed["amount_from"] - TOLERANCE:
        return False
    if parsed.get("amount_to") is not None and flt(row.get("net_amount")) > parsed["amount_to"] + TOLERANCE:
        return False
    if parsed.get("account") == "Yes" and not row.get("account_flag"):
        return False
    if parsed.get("account") == "No" and row.get("account_flag"):
        return False
    if parsed.get("payment") and str(row.get("payment_label") or "").upper() != parsed["payment"]:
        return False
    if parsed.get("status") and parsed["status"].lower() not in str(row.get("status") or "").lower():
        return False
    if parsed.get("secondary_date") and str(row.get("secondary_date") or "") != parsed["secondary_date"]:
        return False
    return True


def _collect(parsed: Dict[str, Any], *, for_print: bool = False) -> Dict[str, Any]:
    rows = _primary_rows(parsed) + _standalone_payment_rows(parsed) + _advance_rows(parsed)
    rows = [row for row in rows if _matches(row, parsed)]
    reverse = parsed["sort_order"] == "Newest First"
    rows.sort(key=lambda row: (str(row.get("transaction_datetime") or ""), str(row.get("source_name") or "")), reverse=reverse)
    limit = PRINT_SCAN_LIMIT if for_print else SCREEN_SCAN_LIMIT
    truncated = len(rows) > limit
    rows = rows[:limit]
    summary = {
        "row_count": len(rows),
        "gross_total": sum(flt(row.get("gross_amount")) for row in rows),
        "price_adjustment_total": sum(flt(row.get("price_adjustment")) for row in rows),
        "net_total": sum(flt(row.get("net_amount")) for row in rows),
        "account_total": sum(flt(row.get("account_amount")) for row in rows),
    }
    return {"rows": rows, "summary": summary, "truncated": truncated, "scan_limit": limit}


def _public_filters(parsed: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "user": parsed.get("scope_user") or "",
        "customer": parsed.get("customer") or "",
        "item_code": parsed.get("item_code") or "",
        "plate_number": parsed.get("plate_number") or "",
        "os_no": parsed.get("os_no") or "",
        "from_datetime": _fmt_datetime(parsed.get("from_datetime")),
        "to_datetime": _fmt_datetime(parsed.get("to_datetime")),
        "amount_from": parsed.get("amount_from"),
        "amount_to": parsed.get("amount_to"),
        "account": parsed.get("account"),
        "payment": parsed.get("payment"),
        "status": parsed.get("status"),
        "secondary_date": parsed.get("secondary_date"),
        "sort_order": parsed.get("sort_order"),
    }


def _relevant_users(mode: str) -> List[Dict[str, str]]:
    role = MODE_ROLES[mode]
    users = set()
    if _doctype_exists("Has Role"):
        for row in frappe.get_all("Has Role", filters={"role": role}, fields=["parent"], limit_page_length=0):
            users.add(str(row.parent))
    dt = MODE_DOCTYPES[mode]
    user_field = "cashier" if mode == "cashier" else "encoder"
    if _doctype_exists(dt) and frappe.get_meta(dt).has_field(user_field):
        for row in frappe.get_all(dt, fields=[user_field], group_by=user_field, limit_page_length=500):
            if row.get(user_field):
                users.add(str(row.get(user_field)))
    identities = _identity_map(users)
    return sorted(identities.values(), key=lambda row: (row["full_name"].lower(), row["user"].lower()))


def _default_period(context: Dict[str, Any]) -> Dict[str, str]:
    now = now_datetime()
    start = get_datetime(f"{today()} 00:00:00")
    shift_name = ""
    secondary_date = ""
    if context["mode"] == "cashier" and context["own_only"] and _doctype_exists("NKT Cashier Shift"):
        fields = _available_fields("NKT Cashier Shift", ["name", "shift_start", "status"])
        filters: Dict[str, Any] = {"cashier": context["user"]}
        if frappe.get_meta("NKT Cashier Shift").has_field("status"):
            filters["status"] = ["in", ["Open", "Turned Over - Awaiting Review", "Counted - Awaiting Approval"]]
        row = frappe.get_all("NKT Cashier Shift", filters=filters, fields=fields, order_by="shift_start desc, creation desc", limit_page_length=1)
        if row:
            shift = dict(row[0])
            shift_name = str(shift.get("name") or "")
            if shift.get("shift_start"):
                start = get_datetime(shift.get("shift_start"))
                secondary_date = _fmt_date(shift.get("shift_start"))
    return {
        "from_datetime": _fmt_datetime(start),
        "to_datetime": _fmt_datetime(now),
        "shift_name": shift_name,
        "secondary_date": secondary_date,
    }


def _estimate_pages(row_count: int, paper_size: str, density: str, detail_mode: str) -> int:
    config = PAPER_OPTIONS[paper_size]
    per_page = config["rows_4pt" if density == "4" else "rows_5pt"]
    if detail_mode == "details":
        per_page = max(8, int(per_page * 0.35))
    return max(1, (max(0, cint(row_count)) + per_page - 1) // per_page)


@frappe.whitelist()
def get_transaction_history_bootstrap(mode: str) -> Dict[str, Any]:
    context = _role_context(mode)
    default = _default_period(context)
    identity = _user_identity(context["user"])
    return {
        "version": VERSION,
        "mode": context["mode"],
        "title": context["title"],
        "own_only": context["own_only"],
        "scope_user": context["user"] if context["own_only"] else "",
        "current_user": identity,
        "users": [] if context["own_only"] else _relevant_users(context["mode"]),
        "default_period": default,
        "secondary_date_label": "Shift Date" if context["mode"] == "cashier" else "Encoded Date",
        "paper_options": PAPER_OPTIONS,
        "density_options": DENSITY_OPTIONS,
        "detail_options": DETAIL_OPTIONS,
        "payment_options": list(PAYMENT_LABELS),
        "all_statuses_default": True,
        "direct_print": True,
        "print_pin_required": False,
    }


@frappe.whitelist()
def get_transaction_history(filters: Any, mode: str) -> Dict[str, Any]:
    parsed = _parse_filters(filters, mode, include_paging=True)
    collected = _collect(parsed, for_print=False)
    start = parsed["page_start"]
    length = parsed["page_length"]
    rows = collected["rows"]
    page = rows[start:start + length]
    return {
        "version": VERSION,
        "mode": parsed["mode"],
        "filters": _public_filters(parsed),
        "rows": page,
        "summary": collected["summary"],
        "page_start": start,
        "page_length": length,
        "next_start": start + len(page),
        "has_more": start + len(page) < len(rows),
        "truncated": collected["truncated"],
        "warning": _("More transactions match than can be shown. Narrow the filters.") if collected["truncated"] else "",
    }


def _insert_print_event(*, context: Dict[str, Any], parsed: Dict[str, Any], row_count: int, detail_mode: str, paper_size: str, density: str, device_id: str, report_sha256: str) -> str:
    if not _doctype_exists(PRINT_EVENT_DOCTYPE):
        frappe.throw(_("Transaction History print audit is not installed."))
    doc = frappe.new_doc(PRINT_EVENT_DOCTYPE)
    doc.event_datetime = now_datetime()
    doc.event_status = "Print Prepared"
    doc.history_mode = "Cashier" if context["mode"] == "cashier" else "Encoder"
    doc.requested_by = context["user"]
    doc.scope_user = parsed.get("scope_user") or None
    doc.filters_json = _canonical_json(_public_filters(parsed))
    doc.row_count = cint(row_count)
    doc.detail_mode = DETAIL_OPTIONS[detail_mode]
    doc.paper_size = PAPER_OPTIONS[paper_size]["label"]
    doc.density = DENSITY_OPTIONS[density]["label"]
    doc.device_id = _clean(device_id, 240) or None
    doc.report_sha256 = report_sha256
    doc.remarks = "Direct role-authorized Transaction History print preview prepared. Physical printer completion is not assumed."
    doc.flags.ignore_permissions = True
    doc.insert(ignore_permissions=True)
    return str(doc.name)


@frappe.whitelist()
def prepare_transaction_history_print(filters: Any, mode: str, paper_size: str = "long", density: str = "5", detail_mode: str = "summary", device_id: str = "", include_plate_number: int = 0, include_os_no: int = 0) -> Dict[str, Any]:
    parsed = _parse_filters(filters, mode, include_paging=False)
    context = parsed["access"]
    paper_size = _clean(paper_size, 20) or "long"
    density = _clean(density, 20) or "5"
    detail_mode = _clean(detail_mode, 20) or "summary"
    if paper_size not in PAPER_OPTIONS:
        frappe.throw(_("Paper size is invalid."))
    if density not in DENSITY_OPTIONS:
        frappe.throw(_("Print density is invalid."))
    if detail_mode not in DETAIL_OPTIONS:
        frappe.throw(_("Print detail mode is invalid."))
    collected = _collect(parsed, for_print=True)
    if collected["truncated"]:
        frappe.throw(_("The print would exceed {0} transactions. Narrow the filters first.").format(PRINT_SCAN_LIMIT))
    payload = {
        "version": VERSION,
        "mode": parsed["mode"],
        "title": "Cashier Transaction History" if parsed["mode"] == "cashier" else "Encoder Transaction History",
        "filters": _public_filters(parsed),
        "rows": collected["rows"],
        "summary": collected["summary"],
        "paper_size": paper_size,
        "density": density,
        "detail_mode": detail_mode,
        "include_plate_number": bool(cint(include_plate_number)),
        "include_os_no": bool(cint(include_os_no)),
        "requested_by": context["user"],
        "requested_by_identity": _user_identity(context["user"]),
        "generated_at": _fmt_datetime(now_datetime()),
    }
    report_sha = _sha256(payload)
    event_name = _insert_print_event(
        context=context,
        parsed=parsed,
        row_count=collected["summary"]["row_count"],
        detail_mode=detail_mode,
        paper_size=paper_size,
        density=density,
        device_id=device_id,
        report_sha256=report_sha,
    )
    return {
        **payload,
        "report_sha256": report_sha,
        "print_event": event_name,
        "estimated_pages": _estimate_pages(collected["summary"]["row_count"], paper_size, density, detail_mode),
        "paper": PAPER_OPTIONS[paper_size],
        "density_config": DENSITY_OPTIONS[density],
        "detail_label": DETAIL_OPTIONS[detail_mode],
        "print_pin_required": False,
        "scope_expanded_by_pin": False,
    }





def _customer_history_print_event(*, customer: str, filters: dict[str, Any], row_count: int, paper_size: str, density: str, device_id: str, report_sha256: str) -> str:
    if not _doctype_exists(PRINT_EVENT_DOCTYPE):
        frappe.throw(_("Transaction History print audit is not installed."))
    doc = frappe.new_doc(PRINT_EVENT_DOCTYPE)
    doc.event_datetime = now_datetime()
    doc.event_status = "Print Prepared"
    doc.history_mode = "Customer"
    doc.requested_by = frappe.session.user
    doc.filters_json = _canonical_json({"customer": customer, **filters})
    doc.row_count = cint(row_count)
    doc.detail_mode = "Summary Only"
    doc.paper_size = PAPER_OPTIONS[paper_size]["label"]
    doc.density = DENSITY_OPTIONS[density]["label"]
    doc.device_id = _clean(device_id, 240) or None
    doc.report_sha256 = report_sha256
    doc.remarks = "Role/device-authorized Customer History print preview prepared. Physical printer completion is not assumed."
    doc.flags.ignore_permissions = True
    doc.insert(ignore_permissions=True)
    return str(doc.name)


@frappe.whitelist()
def prepare_customer_history_print(customer: str, device_id: str, item: str = "", plate_number: str = "", os_no: str = "", from_date: str = "", to_date: str = "", paper_size: str = "long", density: str = "5", include_plate_number: int = 0, include_os_no: int = 0) -> Dict[str, Any]:
    from nkt_operations.nkt_store_operations.features.offline_edge.internal.routed_reads import get_encoder_customer_history
    paper_size = _clean(paper_size, 20) or "long"
    density = _clean(density, 20) or "5"
    if paper_size not in PAPER_OPTIONS:
        frappe.throw(_("Paper size is invalid."))
    if density not in DENSITY_OPTIONS:
        frappe.throw(_("Print density is invalid."))
    all_rows = []
    offset = 0
    while len(all_rows) < PRINT_SCAN_LIMIT:
        result = get_encoder_customer_history(
            customer=customer,
            device_id=device_id,
            item=item or None,
            plate_number=plate_number or None,
            os_no=os_no or None,
            from_date=from_date or None,
            to_date=to_date or None,
            limit=200,
            offset=offset,
        )
        rows = list(result.get("rows") or [])
        all_rows.extend(rows)
        if len(rows) < 200:
            break
        offset += len(rows)
    if len(all_rows) >= PRINT_SCAN_LIMIT:
        frappe.throw(_("Customer History print is too large. Narrow the filters first."))
    filters = {
        "item": item or "",
        "plate_number": plate_number or "",
        "os_no": os_no or "",
        "from_date": from_date or "",
        "to_date": to_date or "",
        "include_plate_number": bool(cint(include_plate_number)),
        "include_os_no": bool(cint(include_os_no)),
    }
    payload = {
        "title": "Customer History",
        "customer": customer,
        "filters": filters,
        "rows": all_rows,
        "paper_size": paper_size,
        "density": density,
        "include_plate_number": bool(cint(include_plate_number)),
        "include_os_no": bool(cint(include_os_no)),
        "requested_by": frappe.session.user,
        "requested_by_identity": _user_identity(frappe.session.user),
        "generated_at": _fmt_datetime(now_datetime()),
    }
    report_sha = _sha256(payload)
    event = _customer_history_print_event(customer=customer, filters=filters, row_count=len(all_rows), paper_size=paper_size, density=density, device_id=device_id, report_sha256=report_sha)
    return {**payload, "report_sha256": report_sha, "print_event": event, "paper": PAPER_OPTIONS[paper_size], "density_config": DENSITY_OPTIONS[density]}


def _registration_authorized(user: str, roles: Optional[set[str]] = None) -> bool:
    roles = roles if roles is not None else set(frappe.get_roles(user) or [])
    return user == "Administrator" or bool(roles.intersection(DEVICE_REGISTRATION_ROLES))


def _reprint_authorized(user: str, roles: Optional[set[str]] = None) -> bool:
    roles = roles if roles is not None else set(frappe.get_roles(user) or [])
    return user == "Administrator" or bool(roles.intersection(REPRINT_ROLES))


def _valid_uuid(value: Any) -> bool:
    try:
        uuid.UUID(str(value or ""))
        return True
    except Exception:
        return False


@frappe.whitelist()
def get_customer_history_workstation_status(device_id: str = "") -> Dict[str, Any]:
    user = _session_user()
    roles = set(frappe.get_roles(user) or [])
    can_register = _registration_authorized(user, roles)
    device_id = _clean(device_id, 80)
    if not device_id or not _valid_uuid(device_id):
        return {
            "registered": False,
            "can_register": can_register,
            "device_id": "",
            "status": "Unregistered",
            "message": _("This workstation is not registered for Customer History yet."),
        }
    if not _doctype_exists("NKT Device Registry") or not frappe.db.exists("NKT Device Registry", device_id):
        return {
            "registered": False,
            "can_register": can_register,
            "device_id": device_id,
            "status": "Unregistered",
            "message": _("This workstation is not registered for Customer History yet."),
        }
    row = frappe.db.get_value(
        "NKT Device Registry",
        device_id,
        ["device_id", "device_label", "operational_context", "assigned_user", "status", "policy_version"],
        as_dict=True,
    ) or frappe._dict()
    status = str(row.get("status") or "")
    assigned = str(row.get("assigned_user") or "")
    if assigned and assigned != user and not can_register:
        return {
            "registered": False,
            "can_register": False,
            "device_id": device_id,
            "status": status or "Unavailable",
            "message": _("This workstation is assigned to another user."),
        }
    if status in TERMINAL_DEVICE_STATUSES:
        return {
            "registered": False,
            "can_register": False,
            "device_id": device_id,
            "status": status,
            "message": _("Device access unavailable."),
        }
    if status == "Restricted":
        return {
            "registered": False,
            "can_register": False,
            "device_id": device_id,
            "status": status,
            "message": _("Customer History is unavailable while this workstation is restricted."),
        }
    return {
        "registered": status == "Active",
        "can_register": False,
        "device_id": device_id,
        "device_label": str(row.get("device_label") or ""),
        "operational_context": str(row.get("operational_context") or ""),
        "status": status,
        "policy_version": cint(row.get("policy_version")),
        "message": _("Workstation registered.") if status == "Active" else _("Device access unavailable."),
    }


@frappe.whitelist()
def register_customer_history_workstation(device_id: str, device_label: str = "NKT Retail Shared Workstation") -> Dict[str, Any]:
    user = _session_user()
    roles = set(frappe.get_roles(user) or [])
    if not _registration_authorized(user, roles):
        raise frappe.PermissionError(_("Only NKT Owner or NKT Administrator may register this workstation."))
    device_id = _clean(device_id, 80)
    if not _valid_uuid(device_id):
        frappe.throw(_("A valid workstation identity is required."))
    label = _clean(device_label, 140) or "NKT Retail Shared Workstation"
    if frappe.db.exists("NKT Device Registry", device_id):
        row = frappe.db.get_value("NKT Device Registry", device_id, ["status", "device_label", "operational_context"], as_dict=True) or frappe._dict()
        status = str(row.get("status") or "")
        if status in TERMINAL_DEVICE_STATUSES:
            raise frappe.PermissionError(_("A revoked, lost, or retired device identity cannot be re-trusted. Register a new workstation identity."))
        if status == "Restricted":
            raise frappe.PermissionError(_("Restore this restricted workstation through Owner/Admin Security Control before using Customer History."))
        return {
            "registered": status == "Active",
            "device_id": device_id,
            "device_label": str(row.get("device_label") or label),
            "operational_context": str(row.get("operational_context") or "NKT Retail"),
            "status": status,
        }
    if not _doctype_exists("NKT Device Registry"):
        frappe.throw(_("NKT Device Registry is not installed."))
    doc = frappe.get_doc({
        "doctype": "NKT Device Registry",
        "device_id": device_id,
        "device_label": label,
        "device_class": "Frontline Laptop",
        "operational_context": "NKT Retail",
        "assigned_user": None,
        "status": "Active",
        "policy_version": 1,
        "notes": "Shared NKT Retail workstation registered from F4 Customer History.",
    })
    doc.flags.ignore_permissions = True
    doc.insert(ignore_permissions=True)
    return {
        "registered": True,
        "device_id": device_id,
        "device_label": label,
        "operational_context": "NKT Retail",
        "status": "Active",
        "registered_by": user,
    }


def _parse_source_row_id(mode: str, row_id: str) -> Dict[str, str]:
    mode = _normalize_mode(mode)
    row_id = _clean(row_id, 300)
    if row_id.startswith(f"{mode}:"):
        return {"kind": "primary", "name": row_id.split(":", 1)[1], "doctype": MODE_DOCTYPES[mode], "mode": mode}
    prefix = f"payment:{mode}:"
    if row_id.startswith(prefix):
        return {"kind": "payment", "name": row_id[len(prefix):], "doctype": "NKT Payment Receipt", "mode": mode}
    prefix = f"advance:{mode}:"
    if row_id.startswith(prefix):
        return {"kind": "advance", "name": row_id[len(prefix):], "doctype": "NKT Customer Advance", "mode": mode}
    frappe.throw(_("The selected Transaction History row is invalid."))
    return {}


def _source_operator(source: Dict[str, str]) -> str:
    mode, kind, name = source["mode"], source["kind"], source["name"]
    if kind == "primary":
        field = "cashier" if mode == "cashier" else "encoder"
        row = frappe.db.get_value(source["doctype"], name, _available_fields(source["doctype"], [field, "owner"]), as_dict=True) or frappe._dict()
        return str(row.get(field) or row.get("owner") or "")
    if kind == "payment":
        field = "received_by" if mode == "cashier" else "encoded_by"
        row = frappe.db.get_value("NKT Payment Receipt", name, _available_fields("NKT Payment Receipt", [field, "owner"]), as_dict=True) or frappe._dict()
        return str(row.get(field) or row.get("owner") or "")
    row = frappe.db.get_value("NKT Customer Advance", name, _available_fields("NKT Customer Advance", ["source_payment_receipt", "owner"]), as_dict=True) or frappe._dict()
    receipt_name = str(row.get("source_payment_receipt") or "")
    if receipt_name and frappe.db.exists("NKT Payment Receipt", receipt_name):
        field = "received_by" if mode == "cashier" else "encoded_by"
        receipt = frappe.db.get_value("NKT Payment Receipt", receipt_name, _available_fields("NKT Payment Receipt", [field, "owner"]), as_dict=True) or frappe._dict()
        return str(receipt.get(field) or receipt.get("owner") or row.get("owner") or "")
    return str(row.get("owner") or "")


def _authorize_source(mode: str, row_id: str) -> Tuple[Dict[str, Any], Dict[str, str]]:
    context = _role_context(mode)
    source = _parse_source_row_id(mode, row_id)
    if not frappe.db.exists(source["doctype"], source["name"]):
        frappe.throw(_("The selected transaction no longer exists."))
    operator = _source_operator(source)
    if context["own_only"] and operator != context["user"]:
        raise frappe.PermissionError(_("Employees may view only their own transactions."))
    source["operator"] = operator
    return context, source


def _return_exchange_eligibility(source: Dict[str, str]) -> Dict[str, Any]:
    if source["kind"] != "primary":
        return {"eligible": False, "reason": _("Returns and exchanges apply only to sales or orders.")}
    doc = frappe.get_doc(source["doctype"], source["name"])
    status = str(doc.get("status") or doc.get("payment_status") or "")
    if cint(doc.docstatus) != 1 or any(token in status.lower() for token in ("cancel", "revers")):
        return {"eligible": False, "reason": _("This transaction is cancelled, reversed, or not posted.")}
    try:
        from nkt_operations.nkt_store_operations.features.returns import service as rx
        detail = rx._detail(source["mode"], source["name"])
        available = sum(max(flt(row.get("available_qty")), 0.0) for row in detail.get("items") or [])
    except Exception:
        available = 0.0
    if available <= TOLERANCE:
        return {"eligible": False, "reason": _("No returnable quantity remains on this transaction.")}
    return {"eligible": True, "side": source["mode"], "source_name": source["name"], "available_quantity": available, "reason": ""}


@frappe.whitelist()
def get_transaction_view_access(mode: str, row_id: str) -> Dict[str, Any]:
    context, source = _authorize_source(mode, row_id)
    roles = set(context["roles"] or [])
    return_info = _return_exchange_eligibility(source)
    can_reprint = _reprint_authorized(context["user"], roles)
    view = _transaction_receipt_payload(source)
    view.update({
        "source_kind": source["kind"],
        "source_doctype": source["doctype"],
        "source_name": source["name"],
        "operator": source.get("operator") or "",
        "operator_identity": _user_identity(source.get("operator") or ""),
        "return_exchange": return_info,
    })
    return {
        "version": VERSION,
        "mode": context["mode"],
        "row_id": row_id,
        "source_kind": source["kind"],
        "source_doctype": source["doctype"],
        "source_name": source["name"],
        "operator": source.get("operator") or "",
        "view_only": True,
        "view": view,
        "can_reprint": can_reprint,
        "reprint_message": "" if can_reprint else _("Cashier receipt reprinting is unavailable. Ask the Encoder, Store Manager, Owner, or NKT Administrator."),
        "can_start_return_exchange": bool(return_info.get("eligible")),
        "return_exchange": return_info,
        "receipt_papers": [{"value": key, **value} for key, value in RECEIPT_PAPER_OPTIONS.items()],
    }


def _receipt_record(order_name: str) -> Dict[str, Any]:
    if not order_name or not _doctype_exists("NKT Customer Receipt Record") or not frappe.db.exists("NKT Customer Receipt Record", order_name):
        return {}
    fields = _available_fields("NKT Customer Receipt Record", ["customer_order", "customer", "customer_name", "order_date", "previous_account_balance", "account_charge", "account_balance_after_order", "plate_reference", "dr_reference", "snapshot_at"])
    return dict(frappe.db.get_value("NKT Customer Receipt Record", order_name, fields, as_dict=True) or {})


def _receipt_payments(parent: str, child_dt: str, include_cash_tender: bool) -> List[Dict[str, Any]]:
    rows = _get_children(child_dt, [parent], ["payment_method", "amount", "collected_amount", "cash_tendered", "change_amount", "card_surcharge", "reference_number", "reference_datetime", "bank_or_provider", "check_number", "check_date", "remarks"]).get(parent, [])
    return _payment_summary(rows, include_cash_tender=include_cash_tender)[2]


def _primary_receipt_payload(source: Dict[str, str]) -> Dict[str, Any]:
    mode = source["mode"]
    doc = frappe.get_doc(source["doctype"], source["name"])
    items = _item_public(doc.get("items") or [])
    payments = _receipt_payments(doc.name, "NKT Payment Detail" if mode == "cashier" else "NKT Declared Payment", mode == "cashier")
    order_name = str(doc.get("matched_customer_order") or "") if mode == "cashier" else doc.name
    rr = _receipt_record(order_name)
    linked_receipt = str(doc.get("linked_payment_receipt") or "")
    if not linked_receipt and order_name and frappe.db.exists("NKT Customer Order", order_name):
        receipt_fields = _available_fields("NKT Customer Order", ["linked_payment_receipt"])
        if receipt_fields:
            linked_receipt = str(frappe.db.get_value("NKT Customer Order", order_name, receipt_fields[0]) or "")
    timestamp = doc.get("sale_datetime") if mode == "cashier" else doc.get("custom_nkt_offline_physical_settled_at") or doc.creation
    gross, adjustment = _gross_and_adjustment(items, flt(doc.get("grand_total")))
    account_from_rows = sum(flt(row.get("amount")) for row in payments if str(row.get("method") or "").upper() == "ACCOUNT")
    return {
        "receipt_kind": "sale_order",
        "source_doctype": source["doctype"], "source_name": doc.name,
        "receipt_number": order_name or linked_receipt or doc.name,
        "transaction_date": _fmt_date(timestamp), "transaction_datetime": _fmt_datetime(timestamp),
        "customer": str(doc.get("customer") or ""), "customer_name": str(doc.get("customer_name") or doc.get("customer") or ""),
        "items": items, "payments": payments,
        "receipt_total": flt(doc.get("grand_total")), "gross_total": gross, "price_adjustment": adjustment,
        "previous_account_balance": flt(rr.get("previous_account_balance")),
        "account_amount": flt(doc.get("total_account_charge") or doc.get("declared_account") or account_from_rows),
        "total_account_balance": flt(rr.get("account_balance_after_order")),
        "plate_reference": str(rr.get("plate_reference") or doc.get("custom_nkt_plate_number") or ""),
        "dr_reference": str(rr.get("dr_reference") or ""),
        "os_no": str(doc.get("source_order_slip") or ""), "remarks": str(doc.get("notes") or doc.get("remarks") or ""),
        "status": str(doc.get("status") or doc.get("payment_status") or ""), "cashier_shift": str(doc.get("cashier_shift") or ""),
        "operator": source.get("operator") or "",
    }


def _payment_receipt_payload(receipt_name: str, source: Dict[str, str], receipt_kind: str) -> Dict[str, Any]:
    doc = frappe.get_doc("NKT Payment Receipt", receipt_name)
    payments = _receipt_payments(doc.name, "NKT Payment Detail", source["mode"] == "cashier")
    total = flt(doc.get("total_payment") or doc.get("total_collected"))
    label = "Customer Advance" if receipt_kind == "customer_advance" else "Payment On Account"
    return {
        "receipt_kind": receipt_kind,
        "source_doctype": source["doctype"], "source_name": source["name"],
        "display_source_name": _public_payment_number(doc.name),
        "receipt_number": _public_payment_number(doc.name),
        "transaction_date": _fmt_date(doc.get("receipt_datetime") or doc.creation), "transaction_datetime": _fmt_datetime(doc.get("receipt_datetime") or doc.creation),
        "customer": str(doc.get("customer") or ""), "customer_name": str(doc.get("customer_name") or doc.get("customer") or ""),
        "items": [{"item_code": "", "item_name": label, "quantity": 1.0, "uom": "", "rate": total, "amount": total, "warehouse": ""}],
        "payments": payments, "receipt_total": total, "gross_total": total, "price_adjustment": 0.0,
        "previous_account_balance": flt(doc.get("amount_due_before_receipt")),
        "account_amount": -total if receipt_kind == "account_payment" else 0.0,
        "total_account_balance": flt(doc.get("remaining_balance")),
        "plate_reference": str(doc.get("custom_nkt_plate_number") or ""), "dr_reference": "",
        "os_no": str(doc.get("custom_nkt_source_order_slip") or ""), "remarks": _public_payment_remarks(doc.get("remarks")),
        "status": str(doc.get("receipt_status") or ""), "cashier_shift": str(doc.get("cashier_shift") or ""),
        "operator": source.get("operator") or "", "customer_advance_amount": flt(doc.get("customer_advance_amount")),
    }


def _transaction_receipt_payload(source: Dict[str, str]) -> Dict[str, Any]:
    if source["kind"] == "primary":
        return _primary_receipt_payload(source)
    if source["kind"] == "payment":
        return _payment_receipt_payload(source["name"], source, "account_payment")
    advance = frappe.get_doc("NKT Customer Advance", source["name"])
    receipt_name = str(advance.get("source_payment_receipt") or "")
    amount = flt(advance.get("original_advance_amount"))
    if receipt_name and frappe.db.exists("NKT Payment Receipt", receipt_name):
        payload = _payment_receipt_payload(receipt_name, source, "customer_advance")
        payload.update({
            "source_name": source["name"], "receipt_total": amount or flt(payload.get("receipt_total")), "gross_total": amount or flt(payload.get("gross_total")),
            "customer_advance_amount": amount, "advance_applied_amount": flt(advance.get("applied_amount")), "advance_available_amount": flt(advance.get("available_advance_amount")),
            "status": str(advance.get("advance_status") or payload.get("status") or ""),
        })
        amount = flt(payload.get("receipt_total"))
        payload["items"] = [{"item_code": "", "item_name": "Customer Advance", "quantity": 1.0, "uom": "", "rate": amount, "amount": amount, "warehouse": ""}]
        return payload
    return {
        "receipt_kind": "customer_advance", "source_doctype": source["doctype"], "source_name": source["name"], "receipt_number": source["name"],
        "transaction_date": _fmt_date(advance.get("posting_datetime") or advance.creation), "transaction_datetime": _fmt_datetime(advance.get("posting_datetime") or advance.creation),
        "customer": str(advance.get("customer") or ""), "customer_name": str(advance.get("customer_name") or advance.get("customer") or ""),
        "items": [{"item_code": "", "item_name": "Customer Advance", "quantity": 1.0, "uom": "", "rate": amount, "amount": amount, "warehouse": ""}],
        "payments": [], "receipt_total": amount, "gross_total": amount, "price_adjustment": 0.0,
        "previous_account_balance": 0.0, "account_amount": 0.0, "total_account_balance": 0.0,
        "plate_reference": "", "dr_reference": "", "os_no": "", "remarks": str(advance.get("remarks") or ""),
        "status": str(advance.get("advance_status") or ""), "cashier_shift": "", "operator": source.get("operator") or "",
        "customer_advance_amount": amount, "advance_applied_amount": flt(advance.get("applied_amount")), "advance_available_amount": flt(advance.get("available_advance_amount")),
    }


def _insert_receipt_reprint_event(context: Dict[str, Any], source: Dict[str, str], row_id: str, paper_size: str, device_id: str, report_sha256: str) -> str:
    if not _doctype_exists(PRINT_EVENT_DOCTYPE):
        frappe.throw(_("Transaction History print audit is not installed."))
    doc = frappe.new_doc(PRINT_EVENT_DOCTYPE)
    doc.event_datetime = now_datetime(); doc.event_status = "Print Prepared"
    doc.history_mode = "Cashier" if context["mode"] == "cashier" else "Encoder"
    doc.requested_by = context["user"]; doc.scope_user = source.get("operator") or None
    doc.filters_json = _canonical_json({"row_id": row_id, "source_doctype": source["doctype"], "source_name": source["name"]})
    doc.row_count = 1; doc.detail_mode = DETAIL_OPTIONS["details"]
    doc.paper_size = RECEIPT_PAPER_OPTIONS[paper_size]["label"]; doc.density = "Receipt Reprint"
    doc.device_id = _clean(device_id, 240) or None; doc.report_sha256 = report_sha256
    doc.remarks = "Historical receipt reprint preview prepared from the view-only Fast Screen. Physical printer completion is not assumed."
    doc.flags.ignore_permissions = True; doc.insert(ignore_permissions=True)
    return str(doc.name)


@frappe.whitelist()
def prepare_transaction_receipt_reprint(
    mode: str, row_id: str, paper_size: str = "half_short", device_id: str = "",
    include_plate_number: int = 1, include_os_no: int = 1,
) -> Dict[str, Any]:
    context, source = _authorize_source(mode, row_id)
    if not _reprint_authorized(context["user"], set(context["roles"] or [])):
        raise frappe.PermissionError(_("Cashier receipt reprinting is unavailable. Ask the Encoder, Store Manager, Owner, or NKT Administrator."))
    paper_size = _clean(paper_size, 30) or "half_short"
    if paper_size not in RECEIPT_PAPER_OPTIONS:
        frappe.throw(_("Receipt paper size is invalid."))
    receipt = _transaction_receipt_payload(source)
    include_plate = bool(cint(include_plate_number))
    include_os = bool(cint(include_os_no))
    if not include_plate:
        receipt["plate_reference"] = ""
    if not include_os:
        receipt["os_no"] = ""
    payload = {
        "version": VERSION, "mode": context["mode"], "row_id": row_id,
        "receipt": receipt, "paper_size": paper_size, "paper": RECEIPT_PAPER_OPTIONS[paper_size],
        "include_plate_number": include_plate, "include_os_no": include_os,
        "requested_by": context["user"], "requested_by_identity": _user_identity(context["user"]), "generated_at": _fmt_datetime(now_datetime()), "reprinted": True,
    }
    report_sha = _sha256(payload)
    event_name = _insert_receipt_reprint_event(context, source, row_id, paper_size, device_id, report_sha)
    return {**payload, "report_sha256": report_sha, "print_event": event_name}


@frappe.whitelist()
def verify_ui7a_installation() -> Dict[str, Any]:
    user = _session_user()
    errors: List[str] = []
    checks: Dict[str, bool] = {}
    source_ui_path = frappe.get_app_path("nkt_operations", "nkt_store_operations", "nkt_transaction_history_ui.js")
    source_ui = open(source_ui_path, "r", encoding="utf-8").read()
    for name in (CASHIER_CLIENT_SCRIPT, ENCODER_CLIENT_SCRIPT):
        script = frappe.db.get_value("Client Script", name, "script") or ""
        enabled = cint(frappe.db.get_value("Client Script", name, "enabled"))
        checks[f"{name}_exists"] = bool(frappe.db.exists("Client Script", name))
        checks[f"{name}_enabled"] = bool(enabled)
        checks[f"{name}_matches_source"] = hashlib.sha256(script.encode()).hexdigest() == hashlib.sha256(source_ui.encode()).hexdigest()
    checks.update({
        "print_event_doctype_exists": _doctype_exists(PRINT_EVENT_DOCTYPE),
        "f8_shortcut_present": "F8" in source_ui and "transaction-history" in source_ui,
        "f7_not_assigned": "F7 My Transactions" not in source_ui,
        "f1_f7_reserved": "isReservedFunctionKey" in source_ui,
        "own_user_enforced_server_side": "Employees may view only their own transactions" in open(__file__, "r", encoding="utf-8").read(),
        "manager_owner_admin_all_users": ELEVATED_ROLES == {"NKT Store Manager", "NKT OWNER", "NKT ADMINISTRATOR"},
        "direct_employee_print": "print_pin_required\": False" in inspect.getsource(get_transaction_history_bootstrap),
        "a4_long_short": set(PAPER_OPTIONS) == {"a4", "long", "short"},
        "summary_and_detail_print": set(DETAIL_OPTIONS) == {"summary", "details"},
        "standalone_payments_and_advances": all(token in open(__file__, "r", encoding="utf-8").read() for token in ("_standalone_payment_rows", "_advance_rows")),
        "split_payment_expansion": "SPLIT" in source_ui and "payments" in source_ui,
        "all_statuses_default": "all_statuses_default" in inspect.getsource(get_transaction_history_bootstrap),
        "item_filter": "item_code" in inspect.getsource(_matches),
        "system_manager_not_implicitly_elevated": "System Manager" not in ELEVATED_ROLES,
        "view_only_drilldown": "get_transaction_view_access" in open(__file__, "r", encoding="utf-8").read(),
        "receipt_reprint_endpoint": "prepare_transaction_receipt_reprint" in open(__file__, "r", encoding="utf-8").read(),
        "cashier_reprint_denied": "NKT Cashier" not in REPRINT_ROLES,
        "encoder_reprint_allowed": "NKT Encoder" in REPRINT_ROLES,
        "half_short_and_a5_receipt": set(RECEIPT_PAPER_OPTIONS) == {"half_short", "a5"},
        "owner_admin_registration": DEVICE_REGISTRATION_ROLES == {"NKT OWNER", "NKT ADMINISTRATOR"},
        "shared_workstation_registration": "assigned_user" in inspect.getsource(register_customer_history_workstation),
    })
    for name, passed in checks.items():
        if not passed:
            errors.append(name)
    report = {"version": VERSION, "user": user, "checks": checks, "errors": errors, "passed": not errors}
    if errors:
        frappe.throw(json.dumps(report, indent=2, default=str))
    return report
