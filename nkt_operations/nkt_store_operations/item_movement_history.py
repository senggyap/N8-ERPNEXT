from __future__ import annotations

import hashlib
import inspect
import json
import math
from collections import defaultdict
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import frappe
from frappe import _
from frappe.utils import cint, flt, get_datetime, now_datetime

VERSION = "R4-UI5B"
CLIENT_SCRIPT_NAME = "NKT R8A Encoder Frontline Presentation + F6 Item History"
PRINT_EVENT_DOCTYPE = "NKT Item History Print Event"
PRIMARY_ROLE = "Primary"

ELEVATED_ROLES = {"NKT OWNER", "NKT ADMINISTRATOR"}
LIMITED_ROLES = {"NKT Encoder", "NKT Store Manager"}
STORE_MANAGER_ROLE = "NKT Store Manager"
ENCODER_ROLE = "NKT Encoder"

PAGE_LENGTH_DEFAULT = 200
PAGE_LENGTH_MAX = 500
SCREEN_SCAN_LIMIT = 25_000
PRINT_SCAN_LIMIT = 12_000
TOLERANCE = 0.000001
STANDARD_FIELDS = {
    "name", "owner", "creation", "modified", "modified_by", "docstatus",
    "parent", "parenttype", "parentfield", "idx",
}

MOVEMENT_TYPES = (
    "Sale / Release",
    "Supplier Arrival",
    "Supplier Return",
    "Transfer In",
    "Transfer Out",
    "Customer Return",
    "Exchange In",
    "Exchange Out",
    "Physical Count Adjustment",
    "Repack In",
    "Repack Out",
    "Stock Recovery In",
    "Stock Recovery Out",
    "Sample Release",
    "Stock Receipt",
    "Stock Issue",
    "Pending / Unreleased Order",
    "Other Stock In",
    "Other Stock Out",
)

PAPER_OPTIONS = {
    "long": {
        "label": "Long Bond 8.5 x 13 Portrait",
        "page_css": "8.5in 13in portrait",
        "rows_5pt": 92,
        "rows_4pt": 118,
    },
    "short": {
        "label": "Short Bond Letter 8.5 x 11 Portrait",
        "page_css": "letter portrait",
        "rows_5pt": 75,
        "rows_4pt": 96,
    },
    "a4": {
        "label": "A4 210 x 297 mm Portrait",
        "page_css": "A4 portrait",
        "rows_5pt": 82,
        "rows_4pt": 105,
    },
}
DENSITY_OPTIONS = {
    "5": {"label": "5 pt Compact", "font_pt": 5},
    "4": {"label": "4 pt Maximum Density", "font_pt": 4},
}


def _clean(value: Any, max_length: int = 500) -> str:
    text = str(value or "").strip()
    if len(text) > max_length:
        frappe.throw(_("Item Movement History filter text is too long."))
    return text


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


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


def _doctype_exists(doctype: str) -> bool:
    return bool(frappe.db.exists("DocType", doctype))


def _available_fields(doctype: str, candidates: Sequence[str]) -> List[str]:
    if not _doctype_exists(doctype):
        return []
    meta = frappe.get_meta(doctype)
    return [field for field in candidates if field in STANDARD_FIELDS or meta.has_field(field)]


def _field(doctype: str, candidates: Sequence[str]) -> Optional[str]:
    fields = _available_fields(doctype, candidates)
    return fields[0] if fields else None


def _get_all_chunked(
    doctype: str,
    link_field: str,
    values: Iterable[str],
    fields: Sequence[str],
    *,
    extra_filters: Optional[Dict[str, Any]] = None,
) -> List[Dict[str, Any]]:
    if not _doctype_exists(doctype):
        return []
    meta = frappe.get_meta(doctype)
    if link_field not in STANDARD_FIELDS and not meta.has_field(link_field):
        return []
    actual_fields = _available_fields(doctype, fields)
    if "name" not in actual_fields:
        actual_fields.insert(0, "name")
    output: List[Dict[str, Any]] = []
    unique_values = sorted({str(value) for value in values if value})
    for batch in _chunks(unique_values):
        filters = dict(extra_filters or {})
        filters[link_field] = ["in", batch]
        rows = frappe.get_all(
            doctype,
            filters=filters,
            fields=actual_fields,
            limit_page_length=0,
        )
        output.extend(dict(row) for row in rows)
    return output


def _get_many_by_name(doctype: str, names: Iterable[str], fields: Sequence[str]) -> Dict[str, Dict[str, Any]]:
    rows = _get_all_chunked(doctype, "name", names, fields)
    return {str(row.get("name")): row for row in rows if row.get("name")}


def _session_user() -> str:
    user = str(frappe.session.user or "")
    if not user or user == "Guest":
        raise frappe.PermissionError(_("Login is required to view Item Movement History."))
    return user


def _role_flags(user: Optional[str] = None) -> Dict[str, Any]:
    user = user or _session_user()
    roles = set(frappe.get_roles(user) or [])
    elevated = user == "Administrator" or bool(roles.intersection(ELEVATED_ROLES))
    store_manager = STORE_MANAGER_ROLE in roles
    encoder = ENCODER_ROLE in roles
    allowed = elevated or store_manager or encoder
    if not allowed:
        raise frappe.PermissionError(_("Item Movement History is unavailable for this role."))
    return {
        "user": user,
        "roles": roles,
        "elevated": elevated,
        "store_manager": store_manager,
        "encoder": encoder,
        "direct_print": elevated or store_manager,
        "pin_required": encoder and not (elevated or store_manager),
    }


def _warehouse_label(row: Dict[str, Any]) -> str:
    return str(
        row.get("custom_nkt_fast_label")
        or row.get("warehouse_name")
        or row.get("name")
        or ""
    )


def _all_warehouse_rows() -> List[Dict[str, Any]]:
    fields = ["name", "warehouse_name", "company"]
    if frappe.get_meta("Warehouse").has_field("custom_nkt_fast_label"):
        fields.append("custom_nkt_fast_label")
    rows = frappe.get_all(
        "Warehouse",
        filters={"disabled": 0, "is_group": 0},
        fields=fields,
        order_by="warehouse_name asc, name asc",
        limit_page_length=0,
    )
    return [
        {
            "name": str(row.name),
            "label": _warehouse_label(dict(row)),
            "warehouse_name": str(row.get("warehouse_name") or row.name),
            "company": str(row.get("company") or ""),
        }
        for row in rows
    ]


def _retail_warehouse_for_user(user: str) -> Optional[str]:
    # First honor the user's configured operating location. This is the same
    # operational source used by the accepted Fast Screens.
    if frappe.get_meta("User").has_field("custom_nkt_operating_location"):
        location_name = frappe.db.get_value("User", user, "custom_nkt_operating_location")
        if location_name and _doctype_exists("NKT Operating Location"):
            fields = ["name", "default_warehouse"]
            meta = frappe.get_meta("NKT Operating Location")
            if meta.has_field("friendly_label"):
                fields.append("friendly_label")
            location = frappe.db.get_value("NKT Operating Location", location_name, fields, as_dict=True)
            label = str((location or {}).get("friendly_label") or (location or {}).get("name") or "").strip().lower()
            if location and label == "retail store" and location.get("default_warehouse"):
                return str(location.get("default_warehouse"))

    if _doctype_exists("NKT Operating Location"):
        location_meta = frappe.get_meta("NKT Operating Location")
        fields = ["name", "default_warehouse"]
        if location_meta.has_field("friendly_label"):
            fields.append("friendly_label")
        filters: Dict[str, Any] = {}
        if location_meta.has_field("enabled"):
            filters["enabled"] = 1
        for row in frappe.get_all(
            "NKT Operating Location",
            filters=filters,
            fields=fields,
            order_by="creation asc",
            limit_page_length=100,
        ):
            label = str(row.get("friendly_label") or row.get("name") or "").strip().lower()
            if label == "retail store" and row.get("default_warehouse"):
                return str(row.default_warehouse)

    warehouse_meta = frappe.get_meta("Warehouse")
    if warehouse_meta.has_field("custom_nkt_fast_label"):
        warehouse = frappe.db.get_value(
            "Warehouse",
            {"custom_nkt_fast_label": "Retail Store", "disabled": 0, "is_group": 0},
            "name",
        )
        if warehouse:
            return str(warehouse)

    warehouses = _all_warehouse_rows()
    for row in warehouses:
        searchable = f"{row['label']} {row['warehouse_name']} {row['name']}".lower()
        if "retail store" in searchable:
            return row["name"]
    return None


def _access_context(user: Optional[str] = None) -> Dict[str, Any]:
    flags = _role_flags(user)
    all_rows = _all_warehouse_rows()
    if flags["elevated"]:
        allowed = all_rows
        default_warehouse = _retail_warehouse_for_user(flags["user"])
        if not default_warehouse and allowed:
            default_warehouse = allowed[0]["name"]
    else:
        retail = _retail_warehouse_for_user(flags["user"])
        if not retail:
            frappe.throw(
                _("The Retail Store warehouse could not be resolved. Ask an NKT Owner / Administrator to repair the operating-location setup.")
            )
        allowed = [row for row in all_rows if row["name"] == retail]
        if not allowed:
            frappe.throw(_("The configured Retail Store warehouse is disabled or unavailable."))
        default_warehouse = retail
    return {
        **flags,
        "warehouses": allowed,
        "allowed_warehouse_names": {row["name"] for row in allowed},
        "default_warehouse": default_warehouse,
    }


def _assert_warehouse_access(warehouse: str, context: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    context = context or _access_context()
    warehouse = _clean(warehouse, 240)
    if not warehouse:
        frappe.throw(_("Select one Warehouse before loading Item Movement History."))
    if warehouse not in context["allowed_warehouse_names"]:
        # A Manager PIN is intentionally irrelevant here. The warehouse scope
        # is role-bound and cannot be expanded by an override.
        raise frappe.PermissionError(_("This role may not view Item Movement History for the selected warehouse."))
    return context


def _parse_datetime(value: Any, label: str) -> Optional[datetime]:
    text = _clean(value, 80)
    if not text:
        return None
    try:
        return get_datetime(text.replace("T", " "))
    except Exception:
        frappe.throw(_("{0} is not a valid date and time.").format(label))
    return None


def _parse_filters(raw: Any, *, include_paging: bool = True) -> Dict[str, Any]:
    if isinstance(raw, str):
        raw = frappe.parse_json(raw)
    if not isinstance(raw, dict):
        frappe.throw(_("Item Movement History filters are invalid."))

    item_code = _clean(raw.get("item_code"), 240)
    warehouse = _clean(raw.get("warehouse"), 240)
    if not item_code or not frappe.db.exists("Item", item_code):
        frappe.throw(_("Select a valid Item."))
    context = _assert_warehouse_access(warehouse)

    from_dt = _parse_datetime(raw.get("from_encoded_at"), "From Encoded At")
    to_dt = _parse_datetime(raw.get("to_encoded_at"), "To Encoded At")
    if from_dt and to_dt and from_dt > to_dt:
        frappe.throw(_("From Encoded At cannot be after To Encoded At."))

    exact_qty_text = _clean(raw.get("exact_quantity"), 80)
    exact_qty: Optional[float] = None
    if exact_qty_text:
        exact_qty = abs(flt(exact_qty_text))
        if exact_qty <= TOLERANCE:
            frappe.throw(_("Exact Quantity must be greater than zero."))

    direction = _clean(raw.get("direction"), 20) or "All"
    if direction not in {"All", "In", "Out"}:
        frappe.throw(_("Direction must be All, In, or Out."))

    sort_order = _clean(raw.get("sort_order"), 20) or "Newest First"
    if sort_order not in {"Newest First", "Oldest First"}:
        frappe.throw(_("Sort order is invalid."))

    movement_type = _clean(raw.get("movement_type"), 100)
    if movement_type and movement_type not in MOVEMENT_TYPES:
        frappe.throw(_("Movement Type is invalid."))

    filters: Dict[str, Any] = {
        "item_code": item_code,
        "warehouse": warehouse,
        "customer": _clean(raw.get("customer"), 240),
        "from_encoded_at": from_dt,
        "to_encoded_at": to_dt,
        "exact_quantity": exact_qty,
        "direction": direction,
        "movement_type": movement_type,
        "status": _clean(raw.get("status"), 140),
        "return_exchange": _clean(raw.get("return_exchange"), 20),
        "sort_order": sort_order,
        "access": context,
    }
    if filters["return_exchange"] not in {"", "Return", "Exchange", "None"}:
        frappe.throw(_("Return / Exchange filter is invalid."))

    if include_paging:
        filters["page_start"] = max(0, cint(raw.get("page_start")))
        filters["page_length"] = max(
            1,
            min(cint(raw.get("page_length")) or PAGE_LENGTH_DEFAULT, PAGE_LENGTH_MAX),
        )
    return filters


def _fmt_datetime(value: Any) -> str:
    if not value:
        return ""
    try:
        return get_datetime(value).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return str(value)


def _row_datetime(row: Dict[str, Any]) -> datetime:
    value = row.get("encoded_at") or "1900-01-01 00:00:00"
    try:
        return get_datetime(value)
    except Exception:
        return get_datetime("1900-01-01 00:00:00")


def _stock_ledger_rows(item_code: str, warehouse: str, limit: int) -> Tuple[List[Dict[str, Any]], bool]:
    doctype = "Stock Ledger Entry"
    meta = frappe.get_meta(doctype)
    candidates = [
        "name",
        "item_code",
        "warehouse",
        "actual_qty",
        "stock_uom",
        "voucher_type",
        "voucher_no",
        "voucher_detail_no",
        "posting_date",
        "posting_time",
        "creation",
        "modified",
        "owner",
    ]
    fields = [field for field in candidates if field == "name" or meta.has_field(field)]
    filters: Dict[str, Any] = {
        "item_code": item_code,
        "warehouse": warehouse,
        "actual_qty": ["!=", 0],
    }
    if meta.has_field("is_cancelled"):
        filters["is_cancelled"] = 0
    rows = frappe.get_all(
        doctype,
        filters=filters,
        fields=fields,
        order_by="creation desc, name desc",
        limit_page_length=limit + 1,
    )
    truncated = len(rows) > limit
    return [dict(row) for row in rows[:limit]], truncated


def _map_by_link(
    doctype: str,
    link_field: str,
    names: Iterable[str],
    fields: Sequence[str],
) -> Dict[str, Dict[str, Any]]:
    output: Dict[str, Dict[str, Any]] = {}
    for row in _get_all_chunked(doctype, link_field, names, fields):
        key = str(row.get(link_field) or "")
        if key and key not in output:
            output[key] = row
    return output


def _intent_time_map(
    doctype: str,
    link_field: str,
    names: Iterable[str],
) -> Dict[str, Dict[str, Any]]:
    return _map_by_link(
        doctype,
        link_field,
        names,
        ["name", link_field, "settled_at", "client_created_at", "origin_user", "creation"],
    )


def _weighted_rate(rows: Iterable[Dict[str, Any]]) -> Optional[float]:
    total_qty = 0.0
    total_amount = 0.0
    seen = False
    for row in rows:
        qty = abs(flt(row.get("quantity") or row.get("qty")))
        rate = flt(row.get("final_rate") or row.get("rate") or row.get("original_rate"))
        amount = row.get("amount")
        if qty <= TOLERANCE or rate <= TOLERANCE:
            continue
        total_qty += qty
        total_amount += flt(amount) if amount not in (None, "") else qty * rate
        seen = True
    if not seen or total_qty <= TOLERANCE:
        return None
    return total_amount / total_qty


def _order_context(order_names: Iterable[str], item_code: str, warehouse: str) -> Dict[str, Dict[str, Any]]:
    names = sorted({str(name) for name in order_names if name})
    if not names or not _doctype_exists("NKT Customer Order"):
        return {}
    parent_fields = [
        "name",
        "customer",
        "customer_name",
        "status",
        "encoder",
        "creation",
        "source_order_slip",
        "default_warehouse",
        "docstatus",
        "custom_nkt_fulfillment_status",
        "custom_nkt_retail_stock_entry",
    ]
    parents = _get_many_by_name("NKT Customer Order", names, parent_fields)

    item_dt = "NKT Customer Order Item"
    item_meta = frappe.get_meta(item_dt)
    item_field = "item" if item_meta.has_field("item") else "item_code"
    warehouse_field = "source_warehouse" if item_meta.has_field("source_warehouse") else "warehouse"
    row_fields = [
        "name",
        "parent",
        item_field,
        warehouse_field,
        "quantity",
        "uom",
        "final_rate",
        "amount",
        "custom_nkt_released_qty",
        "custom_nkt_reserved_qty",
    ]
    item_rows: List[Dict[str, Any]] = []
    for batch in _chunks(names):
        filters = {
            "parent": ["in", batch],
            "parenttype": "NKT Customer Order",
            item_field: item_code,
        }
        if item_meta.has_field(warehouse_field):
            filters[warehouse_field] = warehouse
        fields = _available_fields(item_dt, row_fields)
        item_rows.extend(
            dict(row)
            for row in frappe.get_all(
                item_dt,
                filters=filters,
                fields=fields,
                limit_page_length=0,
            )
        )
    by_parent: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for row in item_rows:
        by_parent[str(row.get("parent"))].append(row)

    intent_map = _map_by_link(
        "NKT Primary Encoder Settlement Intent",
        "customer_order",
        names,
        ["name", "customer_order", "settled_at", "client_created_at", "origin_user", "creation"],
    )
    result: Dict[str, Dict[str, Any]] = {}
    for name, parent in parents.items():
        rows = by_parent.get(name, [])
        intent = intent_map.get(name) or {}
        result[name] = {
            **parent,
            "item_rows": rows,
            "selling_rate": _weighted_rate(rows),
            "encoded_at": intent.get("settled_at") or intent.get("client_created_at") or parent.get("creation"),
            "operator": intent.get("origin_user") or parent.get("encoder") or parent.get("owner"),
            "release_status": parent.get("custom_nkt_fulfillment_status") or "",
        }
    return result


def _return_item_context(declaration_names: Iterable[str], item_code: str) -> Dict[str, Dict[str, Any]]:
    names = sorted({str(name) for name in declaration_names if name})
    if not names or not _doctype_exists("NKT Return Exchange Returned Item"):
        return {}
    dt = "NKT Return Exchange Returned Item"
    meta = frappe.get_meta(dt)
    item_field = "item" if meta.has_field("item") else "item_code"
    fields = _available_fields(
        dt,
        ["name", "parent", item_field, "quantity", "uom", "original_rate", "credit_amount", "original_source_warehouse", "classification"],
    )
    rows: List[Dict[str, Any]] = []
    for batch in _chunks(names):
        rows.extend(
            dict(row)
            for row in frappe.get_all(
                dt,
                filters={"parent": ["in", batch], "parenttype": "NKT Return Exchange Declaration", item_field: item_code},
                fields=fields,
                limit_page_length=0,
            )
        )
    grouped: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[str(row.get("parent"))].append(row)
    return {
        name: {
            "rows": grouped.get(name, []),
            "selling_rate": _weighted_rate(grouped.get(name, [])),
        }
        for name in names
    }


def _release_context(stock_entry_names: Iterable[str]) -> Dict[str, Dict[str, Any]]:
    names = sorted({str(name) for name in stock_entry_names if name})
    result: Dict[str, Dict[str, Any]] = {}
    if not names:
        return result

    release_meta = frappe.get_meta("NKT Warehouse Release") if _doctype_exists("NKT Warehouse Release") else None
    if release_meta and release_meta.has_field("custom_nkt_stock_entry"):
        result.update(
            _map_by_link(
                "NKT Warehouse Release",
                "custom_nkt_stock_entry",
                names,
                [
                    "name",
                    "custom_nkt_stock_entry",
                    "customer_order",
                    "customer",
                    "customer_name",
                    "release_datetime",
                    "released_by",
                    "release_status",
                    "creation",
                ],
            )
        )

    intent_map = _map_by_link(
        "NKT Primary Warehouse Release Intent",
        "materialized_stock_entry",
        names,
        [
            "name",
            "materialized_stock_entry",
            "warehouse_release",
            "materialized_warehouse_release",
            "customer_order",
            "settled_at",
            "client_created_at",
            "origin_user",
            "creation",
        ],
    )
    for stock_entry, intent in intent_map.items():
        release_name = intent.get("materialized_warehouse_release") or intent.get("warehouse_release")
        if stock_entry not in result and release_name and _doctype_exists("NKT Warehouse Release"):
            row = frappe.db.get_value(
                "NKT Warehouse Release",
                release_name,
                _available_fields(
                    "NKT Warehouse Release",
                    ["name", "customer_order", "customer", "customer_name", "release_datetime", "released_by", "release_status", "creation"],
                ),
                as_dict=True,
            )
            if row:
                result[stock_entry] = dict(row)
        result.setdefault(stock_entry, {})["intent"] = intent
    return result


def _build_maps(sle_rows: List[Dict[str, Any]], item_code: str, warehouse: str) -> Dict[str, Any]:
    voucher_nos = {str(row.get("voucher_no") or "") for row in sle_rows if row.get("voucher_no")}
    by_type: Dict[str, set[str]] = defaultdict(set)
    for row in sle_rows:
        if row.get("voucher_type") and row.get("voucher_no"):
            by_type[str(row["voucher_type"])].add(str(row["voucher_no"]))

    stock_entries = _get_many_by_name(
        "Stock Entry",
        by_type.get("Stock Entry", set()),
        [
            "name",
            "stock_entry_type",
            "purpose",
            "owner",
            "creation",
            "modified",
            "docstatus",
            "remarks",
            "custom_nkt_customer_order",
            "custom_nkt_warehouse_release",
            "custom_nkt_fulfillment_kind",
        ],
    )
    purchase_receipts = _get_many_by_name(
        "Purchase Receipt",
        by_type.get("Purchase Receipt", set()),
        ["name", "owner", "creation", "modified", "docstatus", "status", "is_return", "return_against"],
    )
    stock_reconciliations = _get_many_by_name(
        "Stock Reconciliation",
        by_type.get("Stock Reconciliation", set()),
        ["name", "owner", "creation", "modified", "docstatus", "purpose"],
    )
    delivery_notes = _get_many_by_name(
        "Delivery Note",
        by_type.get("Delivery Note", set()),
        ["name", "customer", "customer_name", "owner", "creation", "modified", "docstatus", "status"],
    )
    sales_invoices = _get_many_by_name(
        "Sales Invoice",
        by_type.get("Sales Invoice", set()),
        ["name", "customer", "customer_name", "owner", "creation", "modified", "docstatus", "status"],
    )

    release_by_stock = _release_context(by_type.get("Stock Entry", set()))
    transfer_out = _map_by_link(
        "NKT Warehouse Transfer",
        "outgoing_stock_entry",
        by_type.get("Stock Entry", set()),
        ["name", "outgoing_stock_entry", "incoming_stock_entry", "status", "source_warehouse", "destination_warehouse", "released_by", "released_at", "requested_by", "requested_at", "creation"],
    )
    transfer_in = _map_by_link(
        "NKT Warehouse Transfer",
        "incoming_stock_entry",
        by_type.get("Stock Entry", set()),
        ["name", "outgoing_stock_entry", "incoming_stock_entry", "status", "source_warehouse", "destination_warehouse", "arrived_by", "arrived_at", "requested_by", "requested_at", "creation"],
    )
    transfer_dispatch_intent = _intent_time_map(
        "NKT Primary Warehouse Transfer Dispatch Intent", "materialized_stock_entry", by_type.get("Stock Entry", set())
    )
    transfer_arrival_intent = _intent_time_map(
        "NKT Primary Warehouse Transfer Arrival Intent", "materialized_stock_entry", by_type.get("Stock Entry", set())
    )

    returns = _map_by_link(
        "NKT Return Exchange Declaration",
        "return_stock_entry",
        by_type.get("Stock Entry", set()),
        [
            "name",
            "return_stock_entry",
            "transaction_type",
            "customer",
            "customer_name",
            "entry_datetime",
            "entry_user",
            "posting_status",
            "return_warehouse",
            "old_customer_order",
            "new_customer_order",
            "creation",
        ],
    )
    return_intents = _intent_time_map(
        "NKT Primary Return Exchange Intent", "return_stock_entry", by_type.get("Stock Entry", set())
    )
    return_item_context = _return_item_context(
        [row.get("name") for row in returns.values()], item_code
    )

    supplier_receiving = _map_by_link(
        "NKT Supplier Receiving",
        "underlying_purchase_receipt",
        by_type.get("Purchase Receipt", set()),
        ["name", "underlying_purchase_receipt", "posting_status", "receiving_date", "receiving_time", "posted_by", "posted_at", "creation"],
    )
    supplier_intents = _intent_time_map(
        "NKT Primary Supplier Receiving Intent", "materialized_purchase_receipt", by_type.get("Purchase Receipt", set())
    )

    physical_adjustments = _map_by_link(
        "NKT Physical Inventory Adjustment",
        "stock_reconciliation",
        by_type.get("Stock Reconciliation", set()),
        ["name", "stock_reconciliation", "adjustment_status", "review_status", "count_datetime", "counted_by", "posted_by", "posted_on", "creation"],
    )
    physical_intents = _intent_time_map(
        "NKT Primary Physical Inventory Count Intent", "materialized_stock_reconciliation", by_type.get("Stock Reconciliation", set())
    )

    oil_repack = _map_by_link(
        "NKT Oil Daily Repack",
        "stock_entry",
        by_type.get("Stock Entry", set()),
        ["name", "stock_entry", "status", "repacking_date", "encoded_by", "creation"],
    )
    stock_recovery = _map_by_link(
        "NKT Stock Recovery",
        "stock_entry",
        by_type.get("Stock Entry", set()),
        ["name", "stock_entry", "status", "recovery_datetime", "prepared_by", "creation"],
    )
    sample_release = _map_by_link(
        "NKT BPI Sample Release",
        "underlying_stock_entry",
        by_type.get("Stock Entry", set()),
        ["name", "underlying_stock_entry", "status", "released_by", "released_at", "stock_posted_by", "stock_posted_at", "creation"],
    )

    order_names: set[str] = set()
    for row in stock_entries.values():
        if row.get("custom_nkt_customer_order"):
            order_names.add(str(row["custom_nkt_customer_order"]))
    for row in release_by_stock.values():
        if row.get("customer_order"):
            order_names.add(str(row["customer_order"]))
    for row in returns.values():
        if row.get("new_customer_order"):
            order_names.add(str(row["new_customer_order"]))
        if row.get("old_customer_order"):
            order_names.add(str(row["old_customer_order"]))

    exchange_out_by_order: Dict[str, Dict[str, Any]] = {}
    if order_names and _doctype_exists("NKT Return Exchange Declaration"):
        rows = _get_all_chunked(
            "NKT Return Exchange Declaration",
            "new_customer_order",
            order_names,
            ["name", "new_customer_order", "transaction_type", "customer", "customer_name", "entry_datetime", "entry_user", "posting_status", "creation"],
        )
        exchange_out_by_order = {
            str(row.get("new_customer_order")): row
            for row in rows
            if row.get("new_customer_order") and str(row.get("transaction_type") or "") == "Exchange"
        }

    orders = _order_context(order_names, item_code, warehouse)
    return {
        "voucher_nos": voucher_nos,
        "stock_entries": stock_entries,
        "purchase_receipts": purchase_receipts,
        "stock_reconciliations": stock_reconciliations,
        "delivery_notes": delivery_notes,
        "sales_invoices": sales_invoices,
        "release_by_stock": release_by_stock,
        "transfer_out": transfer_out,
        "transfer_in": transfer_in,
        "transfer_dispatch_intent": transfer_dispatch_intent,
        "transfer_arrival_intent": transfer_arrival_intent,
        "returns": returns,
        "return_intents": return_intents,
        "return_item_context": return_item_context,
        "supplier_receiving": supplier_receiving,
        "supplier_intents": supplier_intents,
        "physical_adjustments": physical_adjustments,
        "physical_intents": physical_intents,
        "oil_repack": oil_repack,
        "stock_recovery": stock_recovery,
        "sample_release": sample_release,
        "orders": orders,
        "exchange_out_by_order": exchange_out_by_order,
    }


def _base_actual_row(sle: Dict[str, Any]) -> Dict[str, Any]:
    qty = flt(sle.get("actual_qty"))
    return {
        "encoded_at": _fmt_datetime(sle.get("creation") or sle.get("modified")),
        "movement_type": "Other Stock In" if qty > 0 else "Other Stock Out",
        "customer": "",
        "customer_name": "",
        "stock_effect_qty": qty,
        "order_qty": None,
        "uom": str(sle.get("stock_uom") or ""),
        "selling_rate": None,
        "amount": None,
        "reference": str(sle.get("voucher_no") or ""),
        "reference_doctype": str(sle.get("voucher_type") or ""),
        "status": "Posted",
        "release_status": "",
        "return_exchange": "",
        "encoder": str(sle.get("owner") or ""),
        "is_pending": False,
        "stock_ledger_entry": str(sle.get("name") or ""),
    }


def _set_sale_context(row: Dict[str, Any], order: Optional[Dict[str, Any]], qty: float) -> None:
    if not order:
        return
    row["customer"] = str(order.get("customer") or "")
    row["customer_name"] = str(order.get("customer_name") or order.get("customer") or "")
    row["status"] = str(order.get("status") or row.get("status") or "")
    row["release_status"] = str(order.get("release_status") or "")
    row["encoder"] = str(order.get("operator") or order.get("encoder") or row.get("encoder") or "")
    row["encoded_at"] = _fmt_datetime(order.get("encoded_at") or row.get("encoded_at"))
    rate = order.get("selling_rate")
    if rate is not None and flt(rate) > TOLERANCE:
        row["selling_rate"] = flt(rate)
        row["amount"] = qty * flt(rate)


def _actual_movement_rows(sle_rows: List[Dict[str, Any]], item_code: str, warehouse: str) -> List[Dict[str, Any]]:
    maps = _build_maps(sle_rows, item_code, warehouse)
    output: List[Dict[str, Any]] = []
    for sle in sle_rows:
        qty = flt(sle.get("actual_qty"))
        voucher_no = str(sle.get("voucher_no") or "")
        voucher_type = str(sle.get("voucher_type") or "")
        row = _base_actual_row(sle)

        if voucher_no in maps["transfer_out"] or voucher_no in maps["transfer_in"]:
            transfer = maps["transfer_out"].get(voucher_no) or maps["transfer_in"].get(voucher_no) or {}
            incoming = qty > 0
            intent = (maps["transfer_arrival_intent"] if incoming else maps["transfer_dispatch_intent"]).get(voucher_no) or {}
            row.update(
                {
                    "movement_type": "Transfer In" if incoming else "Transfer Out",
                    "reference": str(transfer.get("name") or voucher_no),
                    "reference_doctype": "NKT Warehouse Transfer",
                    "status": str(transfer.get("status") or "Posted"),
                    "encoded_at": _fmt_datetime(
                        intent.get("settled_at")
                        or intent.get("client_created_at")
                        or (transfer.get("arrived_at") if incoming else transfer.get("released_at"))
                        or transfer.get("creation")
                        or row["encoded_at"]
                    ),
                    "encoder": str(
                        intent.get("origin_user")
                        or (transfer.get("arrived_by") if incoming else transfer.get("released_by"))
                        or transfer.get("requested_by")
                        or row["encoder"]
                    ),
                }
            )
        elif voucher_no in maps["returns"]:
            declaration = maps["returns"][voucher_no]
            intent = maps["return_intents"].get(voucher_no) or {}
            transaction_type = str(declaration.get("transaction_type") or "Return")
            row.update(
                {
                    "movement_type": "Exchange In" if transaction_type == "Exchange" else "Customer Return",
                    "customer": str(declaration.get("customer") or ""),
                    "customer_name": str(declaration.get("customer_name") or declaration.get("customer") or ""),
                    "reference": str(declaration.get("name") or voucher_no),
                    "reference_doctype": "NKT Return Exchange Declaration",
                    "status": str(declaration.get("posting_status") or "Posted"),
                    "return_exchange": transaction_type,
                    "encoded_at": _fmt_datetime(
                        intent.get("settled_at")
                        or intent.get("client_created_at")
                        or declaration.get("entry_datetime")
                        or declaration.get("creation")
                        or row["encoded_at"]
                    ),
                    "encoder": str(intent.get("origin_user") or declaration.get("entry_user") or row["encoder"]),
                }
            )
            return_ctx = maps["return_item_context"].get(str(declaration.get("name") or ""), {})
            rate = return_ctx.get("selling_rate")
            if rate is not None and flt(rate) > TOLERANCE:
                row["selling_rate"] = flt(rate)
                row["amount"] = qty * flt(rate)
        elif voucher_no in maps["supplier_receiving"]:
            receiving = maps["supplier_receiving"][voucher_no]
            intent = maps["supplier_intents"].get(voucher_no) or {}
            row.update(
                {
                    "movement_type": "Supplier Arrival" if qty > 0 else "Supplier Return",
                    "reference": str(receiving.get("name") or voucher_no),
                    "reference_doctype": "NKT Supplier Receiving",
                    "status": str(receiving.get("posting_status") or "Posted"),
                    "encoded_at": _fmt_datetime(
                        intent.get("settled_at")
                        or intent.get("client_created_at")
                        or receiving.get("posted_at")
                        or receiving.get("creation")
                        or row["encoded_at"]
                    ),
                    "encoder": str(intent.get("origin_user") or receiving.get("posted_by") or row["encoder"]),
                }
            )
        elif voucher_no in maps["physical_adjustments"]:
            adjustment = maps["physical_adjustments"][voucher_no]
            intent = maps["physical_intents"].get(voucher_no) or {}
            status_parts = [
                str(adjustment.get("adjustment_status") or ""),
                str(adjustment.get("review_status") or ""),
            ]
            row.update(
                {
                    "movement_type": "Physical Count Adjustment",
                    "reference": str(adjustment.get("name") or voucher_no),
                    "reference_doctype": "NKT Physical Inventory Adjustment",
                    "status": " / ".join(part for part in status_parts if part) or "Posted",
                    "encoded_at": _fmt_datetime(
                        intent.get("settled_at")
                        or intent.get("client_created_at")
                        or adjustment.get("count_datetime")
                        or adjustment.get("posted_on")
                        or adjustment.get("creation")
                        or row["encoded_at"]
                    ),
                    "encoder": str(
                        intent.get("origin_user")
                        or adjustment.get("counted_by")
                        or adjustment.get("posted_by")
                        or row["encoder"]
                    ),
                }
            )
        elif voucher_no in maps["oil_repack"]:
            repack = maps["oil_repack"][voucher_no]
            row.update(
                {
                    "movement_type": "Repack In" if qty > 0 else "Repack Out",
                    "reference": str(repack.get("name") or voucher_no),
                    "reference_doctype": "NKT Oil Daily Repack",
                    "status": str(repack.get("status") or "Posted"),
                    "encoded_at": _fmt_datetime(repack.get("creation") or row["encoded_at"]),
                    "encoder": str(repack.get("encoded_by") or row["encoder"]),
                }
            )
        elif voucher_no in maps["stock_recovery"]:
            recovery = maps["stock_recovery"][voucher_no]
            row.update(
                {
                    "movement_type": "Stock Recovery In" if qty > 0 else "Stock Recovery Out",
                    "reference": str(recovery.get("name") or voucher_no),
                    "reference_doctype": "NKT Stock Recovery",
                    "status": str(recovery.get("status") or "Posted"),
                    "encoded_at": _fmt_datetime(recovery.get("recovery_datetime") or recovery.get("creation") or row["encoded_at"]),
                    "encoder": str(recovery.get("prepared_by") or row["encoder"]),
                }
            )
        elif voucher_no in maps["sample_release"]:
            sample = maps["sample_release"][voucher_no]
            row.update(
                {
                    "movement_type": "Sample Release",
                    "reference": str(sample.get("name") or voucher_no),
                    "reference_doctype": "NKT BPI Sample Release",
                    "status": str(sample.get("status") or "Posted"),
                    "encoded_at": _fmt_datetime(sample.get("released_at") or sample.get("stock_posted_at") or sample.get("creation") or row["encoded_at"]),
                    "encoder": str(sample.get("released_by") or sample.get("stock_posted_by") or row["encoder"]),
                }
            )
        else:
            release = maps["release_by_stock"].get(voucher_no) or {}
            stock_entry = maps["stock_entries"].get(voucher_no) or {}
            order_name = str(
                release.get("customer_order")
                or stock_entry.get("custom_nkt_customer_order")
                or ""
            )
            order = maps["orders"].get(order_name)
            if order_name:
                exchange = maps["exchange_out_by_order"].get(order_name)
                row["movement_type"] = "Exchange Out" if exchange else "Sale / Release"
                row["reference"] = order_name
                row["reference_doctype"] = "NKT Customer Order"
                _set_sale_context(row, order, qty)
                if release:
                    intent = release.get("intent") or {}
                    row["encoded_at"] = _fmt_datetime(
                        intent.get("settled_at")
                        or intent.get("client_created_at")
                        or release.get("release_datetime")
                        or row["encoded_at"]
                    )
                    row["encoder"] = str(intent.get("origin_user") or release.get("released_by") or row["encoder"])
                    row["release_status"] = str(release.get("release_status") or row["release_status"])
                if exchange:
                    row["return_exchange"] = "Exchange"
            elif voucher_type == "Delivery Note":
                document = maps["delivery_notes"].get(voucher_no) or {}
                row.update(
                    {
                        "movement_type": "Sale / Release",
                        "customer": str(document.get("customer") or ""),
                        "customer_name": str(document.get("customer_name") or document.get("customer") or ""),
                        "status": str(document.get("status") or "Submitted"),
                        "encoder": str(document.get("owner") or row["encoder"]),
                    }
                )
            elif voucher_type == "Sales Invoice":
                document = maps["sales_invoices"].get(voucher_no) or {}
                row.update(
                    {
                        "movement_type": "Sale / Release",
                        "customer": str(document.get("customer") or ""),
                        "customer_name": str(document.get("customer_name") or document.get("customer") or ""),
                        "status": str(document.get("status") or "Submitted"),
                        "encoder": str(document.get("owner") or row["encoder"]),
                    }
                )
            elif voucher_type == "Purchase Receipt":
                document = maps["purchase_receipts"].get(voucher_no) or {}
                row.update(
                    {
                        "movement_type": "Supplier Return" if qty < 0 or cint(document.get("is_return")) else "Supplier Arrival",
                        "status": str(document.get("status") or "Submitted"),
                        "encoder": str(document.get("owner") or row["encoder"]),
                    }
                )
            elif voucher_type == "Stock Reconciliation":
                document = maps["stock_reconciliations"].get(voucher_no) or {}
                row.update(
                    {
                        "movement_type": "Physical Count Adjustment",
                        "status": "Submitted",
                        "encoder": str(document.get("owner") or row["encoder"]),
                    }
                )
            elif voucher_type == "Stock Entry":
                purpose = str(stock_entry.get("purpose") or stock_entry.get("stock_entry_type") or "")
                if "Transfer" in purpose:
                    row["movement_type"] = "Transfer In" if qty > 0 else "Transfer Out"
                elif "Receipt" in purpose or "Manufacture" in purpose or "Repack" in purpose:
                    row["movement_type"] = "Stock Receipt" if qty > 0 else "Stock Issue"
                elif "Issue" in purpose:
                    row["movement_type"] = "Stock Issue"
                row["encoder"] = str(stock_entry.get("owner") or row["encoder"])
                row["status"] = "Submitted" if cint(stock_entry.get("docstatus")) == 1 else row["status"]

        # Buying rate, valuation rate, supplier money, margin, and stock value
        # are deliberately never loaded or returned by this endpoint.
        output.append(row)
    return output


def _release_quantities_by_order_item(order_item_names: Iterable[str]) -> Dict[str, float]:
    names = sorted({str(name) for name in order_item_names if name})
    if not names or not _doctype_exists("NKT Warehouse Release Item"):
        return {}
    child_dt = "NKT Warehouse Release Item"
    meta = frappe.get_meta(child_dt)
    if not meta.has_field("customer_order_item") or not meta.has_field("release_quantity"):
        return {}
    rows: List[Dict[str, Any]] = []
    for batch in _chunks(names):
        rows.extend(
            dict(row)
            for row in frappe.get_all(
                child_dt,
                filters={"customer_order_item": ["in", batch]},
                fields=["customer_order_item", "release_quantity", "parent"],
                limit_page_length=0,
            )
        )
    parent_names = {str(row.get("parent")) for row in rows if row.get("parent")}
    valid_parents: set[str] = set()
    if parent_names and _doctype_exists("NKT Warehouse Release"):
        for parent in _get_many_by_name("NKT Warehouse Release", parent_names, ["name", "docstatus", "release_status"]).values():
            if cint(parent.get("docstatus")) == 1 and str(parent.get("release_status") or "") != "Cancelled":
                valid_parents.add(str(parent.get("name")))
    totals: Dict[str, float] = defaultdict(float)
    for row in rows:
        if str(row.get("parent") or "") in valid_parents:
            totals[str(row.get("customer_order_item"))] += flt(row.get("release_quantity"))
    return dict(totals)


def _pending_order_rows(item_code: str, warehouse: str) -> List[Dict[str, Any]]:
    parent_dt = "NKT Customer Order"
    child_dt = "NKT Customer Order Item"
    if not (_doctype_exists(parent_dt) and _doctype_exists(child_dt)):
        return []
    item_meta = frappe.get_meta(child_dt)
    item_field = "item" if item_meta.has_field("item") else "item_code"
    warehouse_field = "source_warehouse" if item_meta.has_field("source_warehouse") else "warehouse"
    fields = _available_fields(
        child_dt,
        [
            "name",
            "parent",
            item_field,
            warehouse_field,
            "quantity",
            "uom",
            "final_rate",
            "amount",
            "custom_nkt_released_qty",
        ],
    )
    child_rows = [
        dict(row)
        for row in frappe.get_all(
            child_dt,
            filters={
                item_field: item_code,
                warehouse_field: warehouse,
                "parenttype": parent_dt,
            },
            fields=fields,
            order_by="creation desc",
            limit_page_length=20_000,
        )
    ]
    if not child_rows:
        return []
    parent_names = {str(row.get("parent")) for row in child_rows if row.get("parent")}
    orders = _order_context(parent_names, item_code, warehouse)
    released_by_item = _release_quantities_by_order_item(row.get("name") for row in child_rows)
    output: List[Dict[str, Any]] = []
    for child in child_rows:
        order_name = str(child.get("parent") or "")
        order = orders.get(order_name) or {}
        if cint(order.get("docstatus")) == 2 or str(order.get("status") or "") == "Cancelled":
            continue
        qty = abs(flt(child.get("quantity")))
        if qty <= TOLERANCE:
            continue
        if order.get("custom_nkt_retail_stock_entry"):
            # Retail Store immediate deduction already has an authoritative SLE.
            continue
        released = flt(child.get("custom_nkt_released_qty"))
        if released <= TOLERANCE:
            released = flt(released_by_item.get(str(child.get("name") or "")))
        if str(order.get("status") or "") == "Released":
            released = qty
        remaining = max(qty - released, 0.0)
        if remaining <= TOLERANCE:
            continue
        rate = flt(child.get("final_rate") or order.get("selling_rate"))
        output.append(
            {
                "encoded_at": _fmt_datetime(order.get("encoded_at") or order.get("creation")),
                "movement_type": "Pending / Unreleased Order",
                "customer": str(order.get("customer") or ""),
                "customer_name": str(order.get("customer_name") or order.get("customer") or ""),
                "stock_effect_qty": 0.0,
                "order_qty": remaining,
                "uom": str(child.get("uom") or ""),
                "selling_rate": rate if rate > TOLERANCE else None,
                "amount": 0.0,
                "reference": order_name,
                "reference_doctype": parent_dt,
                "status": str(order.get("status") or "Pending"),
                "release_status": str(order.get("release_status") or "Pending / Unreleased"),
                "return_exchange": "",
                "encoder": str(order.get("operator") or order.get("encoder") or ""),
                "is_pending": True,
                "stock_ledger_entry": "",
            }
        )
    return output


def _matches_filters(row: Dict[str, Any], filters: Dict[str, Any]) -> bool:
    encoded = _row_datetime(row)
    if filters.get("from_encoded_at") and encoded < filters["from_encoded_at"]:
        return False
    if filters.get("to_encoded_at") and encoded > filters["to_encoded_at"]:
        return False
    if filters.get("customer") and str(row.get("customer") or "") != filters["customer"]:
        return False
    if filters.get("movement_type") and row.get("movement_type") != filters["movement_type"]:
        return False
    if filters.get("status") and filters["status"].lower() not in str(row.get("status") or "").lower():
        return False
    if filters.get("return_exchange"):
        marker = str(row.get("return_exchange") or "")
        if filters["return_exchange"] == "None" and marker:
            return False
        if filters["return_exchange"] != "None" and marker != filters["return_exchange"]:
            return False

    stock_qty = flt(row.get("stock_effect_qty"))
    if filters.get("direction") == "In" and stock_qty <= TOLERANCE:
        return False
    if filters.get("direction") == "Out" and stock_qty >= -TOLERANCE:
        return False
    exact = filters.get("exact_quantity")
    if exact is not None:
        relevant_qty = abs(stock_qty) if abs(stock_qty) > TOLERANCE else abs(flt(row.get("order_qty")))
        if abs(relevant_qty - flt(exact)) > TOLERANCE:
            return False
    return True


def _compact_person_name(full_name: Any, fallback: Any = "") -> str:
    text = " ".join(str(full_name or fallback or "").strip().split())
    if not text:
        return ""
    if "@" in text and " " not in text:
        text = text.split("@", 1)[0].replace(".", " ").replace("_", " ").replace("-", " ")
        text = " ".join(part.capitalize() for part in text.split() if part)
    parts = text.split()
    if len(parts) <= 1:
        return text[:24]
    return f"{parts[0][0].upper()}. {parts[-1]}"


def _apply_encoded_by_display(rows: List[Dict[str, Any]]) -> None:
    identifiers = sorted({str(row.get("encoder") or "").strip() for row in rows if row.get("encoder")})
    users: Dict[str, Dict[str, Any]] = {}
    if identifiers:
        for batch in _chunks(identifiers):
            for user in frappe.get_all(
                "User",
                filters={"name": ["in", batch]},
                fields=["name", "full_name", "first_name", "middle_name", "last_name"],
                limit_page_length=0,
            ):
                users[str(user.name)] = dict(user)
    for row in rows:
        identifier = str(row.get("encoder") or "").strip()
        user = users.get(identifier) or {}
        full_name = " ".join(
            str(value or "").strip()
            for value in (
                user.get("full_name"),
            )
            if str(value or "").strip()
        )
        if not full_name:
            full_name = " ".join(
                str(user.get(key) or "").strip()
                for key in ("first_name", "middle_name", "last_name")
                if str(user.get(key) or "").strip()
            )
        full_name = full_name or identifier
        row["encoded_by"] = _compact_person_name(full_name, identifier)
        row["encoded_by_full_name"] = full_name


PUBLIC_ROW_FIELDS = (
    "encoded_at", "movement_type", "customer", "customer_name",
    "stock_effect_qty", "order_qty", "uom", "selling_rate", "amount",
    "status", "return_exchange", "encoded_by", "encoded_by_full_name", "is_pending",
)


def _public_row(row: Dict[str, Any]) -> Dict[str, Any]:
    return {field: row.get(field) for field in PUBLIC_ROW_FIELDS}


def _summary(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    total_in = sum(flt(row.get("stock_effect_qty")) for row in rows if flt(row.get("stock_effect_qty")) > 0)
    total_out = sum(abs(flt(row.get("stock_effect_qty"))) for row in rows if flt(row.get("stock_effect_qty")) < 0)
    net = sum(flt(row.get("stock_effect_qty")) for row in rows)
    signed_amount = sum(flt(row.get("amount")) for row in rows if row.get("amount") not in (None, ""))
    encoded_values = [_row_datetime(row) for row in rows if row.get("encoded_at")]
    return {
        "row_count": len(rows),
        "total_in": total_in,
        "total_out": total_out,
        "net_movement": net,
        "signed_amount": signed_amount,
        "earliest_encoded_at": _fmt_datetime(min(encoded_values)) if encoded_values else "",
        "latest_encoded_at": _fmt_datetime(max(encoded_values)) if encoded_values else "",
    }


def _collect_rows(filters: Dict[str, Any], *, for_print: bool = False) -> Dict[str, Any]:
    scan_limit = PRINT_SCAN_LIMIT if for_print else SCREEN_SCAN_LIMIT
    sle_rows, actual_truncated = _stock_ledger_rows(
        filters["item_code"], filters["warehouse"], scan_limit
    )
    actual = _actual_movement_rows(sle_rows, filters["item_code"], filters["warehouse"])
    pending = _pending_order_rows(filters["item_code"], filters["warehouse"])
    rows = [row for row in actual + pending if _matches_filters(row, filters)]
    reverse = filters.get("sort_order") != "Oldest First"
    rows.sort(key=_row_datetime, reverse=reverse)
    _apply_encoded_by_display(rows)
    return {
        "rows": rows,
        "summary": _summary(rows),
        "truncated": bool(actual_truncated),
        "scan_limit": scan_limit,
    }


def _item_info(item_code: str) -> Dict[str, Any]:
    fields = ["name", "item_name", "stock_uom", "item_group"]
    row = frappe.db.get_value("Item", item_code, fields, as_dict=True)
    return dict(row) if row else {"name": item_code, "item_name": item_code}


def _warehouse_info(warehouse: str) -> Dict[str, Any]:
    for row in _all_warehouse_rows():
        if row["name"] == warehouse:
            return row
    return {"name": warehouse, "label": warehouse, "warehouse_name": warehouse, "company": ""}


def _public_filters(filters: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "item_code": filters["item_code"],
        "warehouse": filters["warehouse"],
        "customer": filters.get("customer") or "",
        "from_encoded_at": _fmt_datetime(filters.get("from_encoded_at")),
        "to_encoded_at": _fmt_datetime(filters.get("to_encoded_at")),
        "exact_quantity": filters.get("exact_quantity"),
        "direction": filters.get("direction") or "All",
        "movement_type": filters.get("movement_type") or "",
        "status": filters.get("status") or "",
        "return_exchange": filters.get("return_exchange") or "",
        "sort_order": filters.get("sort_order") or "Newest First",
    }


def _estimate_pages(row_count: int, paper_size: str, density: str) -> int:
    paper = PAPER_OPTIONS.get(paper_size) or PAPER_OPTIONS["long"]
    key = "rows_4pt" if density == "4" else "rows_5pt"
    rows_per_page = max(cint(paper[key]), 1)
    return max(1, int(math.ceil(max(cint(row_count), 1) / rows_per_page)))


@frappe.whitelist()
def get_item_movement_history_bootstrap(item_code: Optional[str] = None) -> Dict[str, Any]:
    context = _access_context()
    item_code = _clean(item_code, 240)
    item = _item_info(item_code) if item_code and frappe.db.exists("Item", item_code) else None
    return {
        "version": VERSION,
        "title": "Item Movement History",
        "user": context["user"],
        "item": item,
        "warehouses": context["warehouses"],
        "default_warehouse": context["default_warehouse"],
        "movement_types": list(MOVEMENT_TYPES),
        "page_length": PAGE_LENGTH_DEFAULT,
        "can_print_direct": bool(context["direct_print"]),
        "manager_pin_required_for_print": bool(context["pin_required"]),
        "manager_pin_never_expands_warehouse_access": True,
        "paper_options": PAPER_OPTIONS,
        "density_options": DENSITY_OPTIONS,
        "print_max_rows": PRINT_SCAN_LIMIT,
        "buying_cost_exposed": False,
        "supplier_money_exposed": False,
        "running_balance_enabled": False,
    }


@frappe.whitelist()
def get_item_movement_history(filters: Any) -> Dict[str, Any]:
    parsed = _parse_filters(filters, include_paging=True)
    collected = _collect_rows(parsed, for_print=False)
    start = parsed["page_start"]
    length = parsed["page_length"]
    rows = collected["rows"]
    page = [_public_row(row) for row in rows[start : start + length]]
    summary = collected["summary"]
    return {
        "version": VERSION,
        "item": _item_info(parsed["item_code"]),
        "warehouse": _warehouse_info(parsed["warehouse"]),
        "filters": _public_filters(parsed),
        "rows": page,
        "summary": summary,
        "page_start": start,
        "page_length": length,
        "next_start": start + len(page),
        "has_more": start + len(page) < len(rows),
        "truncated": collected["truncated"],
        "scan_limit": collected["scan_limit"],
        "warning": (
            _("More than {0} stock movements match this Item and Warehouse. Narrow the Encoded At filters before printing.").format(collected["scan_limit"])
            if collected["truncated"]
            else ""
        ),
        "estimated_pages": {
            "long_5pt": _estimate_pages(summary["row_count"], "long", "5"),
            "long_4pt": _estimate_pages(summary["row_count"], "long", "4"),
            "short_5pt": _estimate_pages(summary["row_count"], "short", "5"),
            "short_4pt": _estimate_pages(summary["row_count"], "short", "4"),
            "a4_5pt": _estimate_pages(summary["row_count"], "a4", "5"),
            "a4_4pt": _estimate_pages(summary["row_count"], "a4", "4"),
        },
    }


def _verify_manager_pin_for_print(pin: Any, device_id: str, requester: str) -> str:
    from nkt_operations.nkt_store_operations import manager_authorization as nkt_manager_pin

    pin_value = nkt_manager_pin._validate_pin_format(pin)
    device = _clean(device_id, 240) or "[unbound]"
    if not nkt_manager_pin._authorizers_for_runtime():
        frappe.throw(_("No active Manager PIN is configured. Ask an NKT Owner / Administrator."))
    nkt_manager_pin._check_throttle(device)
    authorizer = nkt_manager_pin._find_authorizer_for_pin(pin_value)
    if not authorizer:
        nkt_manager_pin._event(
            "Wrong PIN",
            cashier=requester,
            device_id=device,
            reason="Item Movement History Print",
            remarks="Incorrect five-digit Manager PIN for Item Movement History print.",
        )
        frappe.db.commit()
        raise frappe.ValidationError(_("Incorrect Manager PIN."))
    nkt_manager_pin._event(
        "Authorized",
        cashier=requester,
        device_id=device,
        authorized_by=authorizer,
        reason="Item Movement History Print",
        remarks="Fresh one-print Item Movement History authorization verified. Warehouse access was not expanded.",
    )
    return str(authorizer)


def _insert_print_event(
    *,
    requested_by: str,
    authorized_by: str,
    authorization_mode: str,
    item_code: str,
    warehouse: str,
    filters: Dict[str, Any],
    row_count: int,
    estimated_pages: int,
    paper_size: str,
    density: str,
    device_id: str,
    report_sha256: str,
) -> str:
    if not _doctype_exists(PRINT_EVENT_DOCTYPE):
        frappe.throw(_("Item Movement History print audit is not installed."))
    doc = frappe.new_doc(PRINT_EVENT_DOCTYPE)
    doc.event_datetime = now_datetime()
    doc.event_status = "Print Prepared"
    doc.requested_by = requested_by
    doc.authorized_by = authorized_by
    doc.authorization_mode = authorization_mode
    doc.item_code = item_code
    doc.warehouse = warehouse
    doc.filters_json = _canonical_json(filters)
    doc.row_count = cint(row_count)
    doc.estimated_pages = cint(estimated_pages)
    doc.paper_size = PAPER_OPTIONS[paper_size]["label"]
    doc.density = DENSITY_OPTIONS[density]["label"]
    doc.device_id = _clean(device_id, 240) or None
    doc.report_sha256 = report_sha256
    doc.remarks = "Server-authorized print preview prepared. Physical printer completion is not assumed."
    doc.flags.ignore_permissions = True
    doc.insert(ignore_permissions=True)
    return doc.name


@frappe.whitelist()
def prepare_item_movement_history_print(
    filters: Any,
    paper_size: str = "long",
    density: str = "5",
    pin: Any = "",
    device_id: str = "",
) -> Dict[str, Any]:
    parsed = _parse_filters(filters, include_paging=False)
    context = parsed["access"]
    paper_size = _clean(paper_size, 20) or "long"
    density = _clean(density, 20) or "5"
    if paper_size not in PAPER_OPTIONS:
        frappe.throw(_("Paper size is invalid."))
    if density not in DENSITY_OPTIONS:
        frappe.throw(_("Print density is invalid."))

    collected = _collect_rows(parsed, for_print=True)
    if collected["truncated"]:
        frappe.throw(
            _("The print would exceed {0} movements. Narrow the Encoded At or other filters before printing.").format(PRINT_SCAN_LIMIT)
        )
    rows = [_public_row(row) for row in collected["rows"]]
    requester = context["user"]
    if context["pin_required"]:
        authorized_by = _verify_manager_pin_for_print(pin, device_id, requester)
        authorization_mode = "Manager PIN"
    elif context["direct_print"]:
        authorized_by = requester
        authorization_mode = "Direct Role"
    else:
        raise frappe.PermissionError(_("This role may not print Item Movement History."))

    public_filters = _public_filters(parsed)
    summary = collected["summary"]
    estimated_pages = _estimate_pages(summary["row_count"], paper_size, density)
    report_payload = {
        "version": VERSION,
        "item": _item_info(parsed["item_code"]),
        "warehouse": _warehouse_info(parsed["warehouse"]),
        "filters": public_filters,
        "rows": rows,
        "summary": summary,
        "paper_size": paper_size,
        "density": density,
        "requested_by": requester,
        "authorized_by": authorized_by,
        "authorization_mode": authorization_mode,
        "generated_at": _fmt_datetime(now_datetime()),
    }
    report_sha = _sha256(report_payload)
    event_name = _insert_print_event(
        requested_by=requester,
        authorized_by=authorized_by,
        authorization_mode=authorization_mode,
        item_code=parsed["item_code"],
        warehouse=parsed["warehouse"],
        filters=public_filters,
        row_count=summary["row_count"],
        estimated_pages=estimated_pages,
        paper_size=paper_size,
        density=density,
        device_id=device_id,
        report_sha256=report_sha,
    )
    return {
        **report_payload,
        "report_sha256": report_sha,
        "print_event": event_name,
        "estimated_pages": estimated_pages,
        "paper": PAPER_OPTIONS[paper_size],
        "density_config": DENSITY_OPTIONS[density],
        "warehouse_access_expanded_by_pin": False,
        "buying_cost_exposed": False,
        "supplier_money_exposed": False,
    }


@frappe.whitelist()
def verify_ui5b_installation() -> Dict[str, Any]:
    user = _session_user()
    context = _access_context(user)
    source_ui_path = frappe.get_app_path(
        "nkt_operations", "nkt_store_operations", "nkt_item_movement_history_ui.js"
    )
    source_ui = open(source_ui_path, "r", encoding="utf-8").read()
    db_ui = frappe.db.get_value("Client Script", CLIENT_SCRIPT_NAME, "script") or ""
    checks = {
        "print_event_doctype_exists": _doctype_exists(PRINT_EVENT_DOCTYPE),
        "client_script_exists": bool(frappe.db.exists("Client Script", CLIENT_SCRIPT_NAME)),
        "client_script_enabled": bool(cint(frappe.db.get_value("Client Script", CLIENT_SCRIPT_NAME, "enabled"))),
        "client_script_matches_source": hashlib.sha256(db_ui.encode()).hexdigest() == hashlib.sha256(source_ui.encode()).hexdigest(),
        "warehouse_required": "Select one Warehouse" in open(__file__, "r", encoding="utf-8").read(),
        "manager_pin_cannot_expand_warehouse": "Warehouse access was not expanded" in open(__file__, "r", encoding="utf-8").read(),
        "buying_cost_not_loaded": all(
            token not in inspect.getsource(_stock_ledger_rows)
            for token in ("valuation_rate", "incoming_rate", "stock_value_difference", "purchase_rate", "buying_rate")
        ),
        "long_bond_portrait": PAPER_OPTIONS["long"]["page_css"] == "8.5in 13in portrait",
        "a4_portrait": PAPER_OPTIONS["a4"]["page_css"] == "A4 portrait",
        "five_and_four_point_density": set(DENSITY_OPTIONS) == {"4", "5"},
        "compact_encoded_by": "encoded_by" in PUBLIC_ROW_FIELDS and "encoded_by_full_name" in PUBLIC_ROW_FIELDS,
        "reference_not_public": "reference" not in PUBLIC_ROW_FIELDS,
        "release_status_not_public": "release_status" not in PUBLIC_ROW_FIELDS,
        "obsolete_filters_removed": all(token not in source_ui for token in ('data-filter="reference"', 'data-filter="release_status"')),
        "print_dialog_layer_fix": "nkt-imh-dialog-front" in source_ui,
        "roles_scoped": bool(context["warehouses"]),
        "system_manager_not_implicitly_elevated": "System Manager" not in ELEVATED_ROLES,
    }
    errors = [name for name, passed in checks.items() if not passed]
    report = {
        "version": VERSION,
        "user": user,
        "checks": checks,
        "errors": errors,
        "passed": not errors,
        "allowed_warehouses": context["warehouses"],
        "print_pin_required": context["pin_required"],
        "direct_print": context["direct_print"],
    }
    if errors:
        frappe.throw(json.dumps(report, indent=2, default=str))
    return report


@frappe.whitelist()
def verify_ui5_installation() -> Dict[str, Any]:
    return verify_ui5b_installation()
