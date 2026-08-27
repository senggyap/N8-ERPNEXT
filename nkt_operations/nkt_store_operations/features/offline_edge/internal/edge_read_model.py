from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta
from typing import Any, Dict, Iterable, List, Optional

import frappe
from frappe.utils import getdate

from nkt_operations.nkt_store_operations.features.offline_edge.policy import (
    PH_TZ,
    device_policy_snapshot,
    is_privileged_user,
)

FOUNDATION_VERSION = "C15C.6B-R1"
SNAPSHOT_SCHEMA_VERSION = 1
# NKT_MANAGER_PIN_EDGE_SNAPSHOT_MP1
DETAIL_DAYS = 30
ITEM_MOVEMENT_TRACE_DAYS = 14
STORE_WAREHOUSE_NAME = "NKT Retail Store"

FORBIDDEN_TOP_LEVEL_SECTIONS = {
    "suppliers",
    "purchasing_history",
    "payroll",
    "general_ledger",
    "gl_entries",
    "accounting_history",
    "backup_archives",
    "all_time_customer_history",
}

FORBIDDEN_SENSITIVE_KEYS = {
    "reference_number",
    "reference_datetime",
    "check_number",
    "check_date",
    "bank_or_provider",
    "mariadb_password",
    "db_password",
    "database_password",
}


def _session_user(user: Optional[str] = None) -> str:
    user = user or frappe.session.user
    if not user or user == "Guest":
        raise frappe.PermissionError("Store Edge snapshot unavailable.")
    return user


def _edge_device(device_id: str, *, user: Optional[str] = None) -> Dict[str, Any]:
    user = _session_user(user)
    if not is_privileged_user(user):
        # C15C.6B deliberately does not invent a production Edge service role.
        # That service identity is a later deployment/hardening choice.
        raise frappe.PermissionError("Store Edge snapshot unavailable.")

    policy = device_policy_snapshot(device_id, user=user)
    row = frappe.db.get_value(
        "NKT Device Registry",
        device_id,
        ["device_id", "device_class", "operational_context", "status", "policy_version"],
        as_dict=True,
    )
    if not row or row.device_class != "Store Edge":
        raise frappe.PermissionError("Store Edge snapshot unavailable.")
    if row.status != "Active":
        raise frappe.PermissionError("Store Edge snapshot unavailable.")
    if row.operational_context not in {"NKT Retail", "Infrastructure"}:
        raise frappe.PermissionError("Store Edge snapshot unavailable.")

    return {
        "device_id": row.device_id,
        "device_class": row.device_class,
        "operational_context": row.operational_context,
        "policy_version": int(row.policy_version or 1),
        "ui_mode": policy["ui_mode"],
    }


def _today_manila():
    return datetime.now(PH_TZ).date()


def _detail_cutoff(today=None):
    today = getdate(today) if today else _today_manila()
    # Matches the already-accepted C15C discovery/read-service convention:
    # records with business date >= today - 30 days are inside the bounded detail set.
    return today - timedelta(days=DETAIL_DAYS)


def _resolve_store_warehouse() -> str:
    rows = frappe.get_all(
        "Warehouse",
        filters={"warehouse_name": STORE_WAREHOUSE_NAME, "is_group": 0, "disabled": 0},
        fields=["name"],
        limit_page_length=5,
    )
    if len(rows) != 1:
        raise frappe.ValidationError(
            f"Expected exactly one active leaf Warehouse named {STORE_WAREHOUSE_NAME!r}; found {len(rows)}."
        )
    return rows[0]["name"]


def _saleable_condition(alias: str = "i") -> str:
    # Keep Edge item eligibility aligned with the accepted Fast UI.
    if frappe.get_meta("Item").has_field("nkt_stock_form"):
        return f" AND COALESCE({alias}.nkt_stock_form, '') = 'Saleable Sack'"
    return ""


def _standard_rate_subquery(alias: str = "i") -> str:
    # Exact accepted Fast UI price rule:
    # active Standard Selling Item Price -> Item.standard_rate -> 0.
    return f"""
        COALESCE((
            SELECT ip.price_list_rate
              FROM `tabItem Price` ip
             WHERE ip.item_code = {alias}.name
               AND ip.price_list = 'Standard Selling'
               AND (ip.valid_from IS NULL OR ip.valid_from <= CURDATE())
               AND (ip.valid_upto IS NULL OR ip.valid_upto >= CURDATE())
             ORDER BY ip.valid_from DESC, ip.modified DESC
             LIMIT 1
        ), {alias}.standard_rate, 0)
    """


def _approved_balance_subquery(customer_alias: str = "c") -> str:
    # Mirrors the role-safe operational balance logic used by current Fast UI:
    # approved AR, plus matched Pending Approval AR only.
    return f"""
        COALESCE((
            SELECT SUM(r.outstanding_amount)
              FROM `tabNKT Customer Receivable` r
             WHERE r.customer = {customer_alias}.name
               AND r.docstatus <> 2
               AND COALESCE(r.status, '') <> 'Cancelled'
               AND COALESCE(r.outstanding_amount, 0) > 0
               AND (
                    r.credit_control_status = 'Approved'
                    OR (
                        r.credit_control_status = 'Pending Approval'
                        AND EXISTS (
                            SELECT 1
                              FROM `tabNKT Customer Order` o
                             WHERE o.name = r.customer_order
                               AND o.docstatus = 1
                               AND COALESCE(o.matched_cashier_sale, '') <> ''
                               AND COALESCE(o.cashier_reconciliation_status, '') LIKE 'Matched%%'
                        )
                    )
               )
        ), 0)
    """


def _item_barcodes(item_codes: List[str]) -> Dict[str, List[str]]:
    out = {item: [] for item in item_codes}
    if not item_codes or not frappe.db.exists("DocType", "Item Barcode"):
        return out
    placeholders = ", ".join(["%s"] * len(item_codes))
    rows = frappe.db.sql(
        f"""select parent, barcode
              from `tabItem Barcode`
             where parent in ({placeholders})
               and coalesce(barcode, '') != ''
             order by parent, idx""",
        tuple(item_codes),
        as_dict=True,
    )
    for row in rows:
        out.setdefault(row.parent, []).append(row.barcode)
    return out


def _current_items(store_warehouse: str) -> List[Dict[str, Any]]:
    saleable = _saleable_condition("i")
    rate_sql = _standard_rate_subquery("i")
    rows = frappe.db.sql(
        f"""select
                i.name as item_code,
                i.item_name,
                i.stock_uom,
                {rate_sql} as current_rate,
                coalesce(b.actual_qty, 0) as actual_qty,
                coalesce(b.reserved_qty, 0) as reserved_qty,
                coalesce(b.actual_qty, 0) - coalesce(b.reserved_qty, 0) as available_qty
              from `tabItem` i
              left join `tabBin` b
                on b.item_code=i.name and b.warehouse=%s
             where i.disabled=0
               and i.is_sales_item=1
               {saleable}
             order by i.item_name asc, i.name asc""",
        (store_warehouse,),
        as_dict=True,
    )
    codes = [r.item_code for r in rows]
    barcode_map = _item_barcodes(codes)
    for row in rows:
        row["barcodes"] = barcode_map.get(row.item_code, [])
    return rows


def _recent_customer_ids(cutoff) -> List[str]:
    rows = frappe.db.sql(
        """select distinct customer from (
               select customer
                 from `tabNKT Customer Order`
                where customer is not null and customer != '' and order_date >= %s
               union
               select customer
                 from `tabNKT Cashier Sale`
                where customer is not null and customer != '' and business_date >= %s
               union
               select customer
                 from `tabNKT Warehouse Release`
                where customer is not null and customer != '' and date(release_datetime) >= %s
               union
               select customer
                 from `tabNKT Customer Receivable`
                where customer is not null and customer != ''
                  and status in ('Open', 'Partially Paid')
                  and coalesce(outstanding_amount,0) > 0
           ) x
           order by customer""",
        (cutoff, cutoff, cutoff),
        as_dict=True,
    )
    return [r.customer for r in rows if r.customer]


def _current_customers(cutoff) -> List[Dict[str, Any]]:
    ids = _recent_customer_ids(cutoff)
    if not ids:
        return []
    placeholders = ", ".join(["%s"] * len(ids))
    balance_sql = _approved_balance_subquery("c")
    rows = frappe.db.sql(
        f"""select
                c.name as customer,
                c.customer_name,
                {balance_sql} as current_account_balance
              from `tabCustomer` c
             where c.name in ({placeholders})
             order by c.customer_name asc, c.name asc""",
        tuple(ids),
        as_dict=True,
    )
    return rows


def _order_items(order_names: List[str]) -> Dict[str, List[Dict[str, Any]]]:
    out = {name: [] for name in order_names}
    if not order_names:
        return out
    placeholders = ", ".join(["%s"] * len(order_names))
    rows = frappe.db.sql(
        f"""select parent, idx, item, item_name, quantity, uom, final_rate, amount,
                   source_warehouse, remarks
              from `tabNKT Customer Order Item`
             where parenttype='NKT Customer Order'
               and parent in ({placeholders})
             order by parent, idx""",
        tuple(order_names),
        as_dict=True,
    )
    for row in rows:
        parent = row.pop("parent")
        row.pop("idx", None)
        out.setdefault(parent, []).append(row)
    return out


def _sale_items(sale_names: List[str]) -> Dict[str, List[Dict[str, Any]]]:
    out = {name: [] for name in sale_names}
    if not sale_names:
        return out
    placeholders = ", ".join(["%s"] * len(sale_names))
    rows = frappe.db.sql(
        f"""select parent, idx, item, item_name, quantity, uom, source_warehouse,
                   final_rate, amount, remarks
              from `tabNKT Cashier Sale Item`
             where parenttype='NKT Cashier Sale'
               and parent in ({placeholders})
             order by parent, idx""",
        tuple(sale_names),
        as_dict=True,
    )
    for row in rows:
        parent = row.pop("parent")
        row.pop("idx", None)
        out.setdefault(parent, []).append(row)
    return out


def _sale_payment_methods(sale_names: List[str]) -> Dict[str, List[str]]:
    out = {name: [] for name in sale_names}
    if not sale_names:
        return out
    placeholders = ", ".join(["%s"] * len(sale_names))
    rows = frappe.db.sql(
        f"""select parent, idx, payment_method
              from `tabNKT Payment Detail`
             where parenttype='NKT Cashier Sale'
               and parent in ({placeholders})
             order by parent, idx""",
        tuple(sale_names),
        as_dict=True,
    )
    for row in rows:
        method = (row.payment_method or "").strip()
        if method and method not in out.setdefault(row.parent, []):
            out[row.parent].append(method)
    return out


def _recent_orders(cutoff) -> List[Dict[str, Any]]:
    rows = frappe.db.sql(
        """select
                name as order_no, order_date, creation as encoded_at,
                customer, customer_name, encoder, status, payment_status,
                grand_total, default_warehouse, notes as remarks,
                matched_cashier_sale, custom_nkt_customer_receivable
              from `tabNKT Customer Order`
             where order_date >= %s
               and docstatus <> 2
             order by order_date desc, creation desc, name desc""",
        (cutoff,),
        as_dict=True,
    )
    item_map = _order_items([r.order_no for r in rows])
    for row in rows:
        row["items"] = item_map.get(row.order_no, [])
    return rows


def _recent_sales(cutoff) -> List[Dict[str, Any]]:
    rows = frappe.db.sql(
        """select
                name as sale_no, sale_datetime, business_date,
                customer, customer_name, cashier, cashier_shift,
                settlement_location, status, grand_total,
                total_cash, total_non_cash, card_surcharge_total,
                total_account_charge, matched_customer_order,
                linked_payment_receipt, notes as remarks
              from `tabNKT Cashier Sale`
             where business_date >= %s
               and docstatus <> 2
             order by business_date desc, sale_datetime desc, name desc""",
        (cutoff,),
        as_dict=True,
    )
    names = [r.sale_no for r in rows]
    items = _sale_items(names)
    methods = _sale_payment_methods(names)
    for row in rows:
        row["items"] = items.get(row.sale_no, [])
        row["payment_methods"] = methods.get(row.sale_no, [])
    return rows


def _release_items(release_names: List[str]) -> Dict[str, List[Dict[str, Any]]]:
    out = {name: [] for name in release_names}
    if not release_names:
        return out
    placeholders = ", ".join(["%s"] * len(release_names))
    meta = frappe.get_meta("NKT Warehouse Release Item")
    available = {f.fieldname for f in meta.fields}
    candidates = [
        "item", "item_name", "release_quantity", "uom",
        "source_warehouse", "remarks",
    ]
    fields = [f for f in candidates if f in available]
    if not fields:
        return out
    sql_fields = ", ".join(f"`{f}`" for f in fields)
    rows = frappe.db.sql(
        f"""select parent, idx, {sql_fields}
              from `tabNKT Warehouse Release Item`
             where parenttype='NKT Warehouse Release'
               and parent in ({placeholders})
             order by parent, idx""",
        tuple(release_names),
        as_dict=True,
    )
    for row in rows:
        parent = row.pop("parent")
        row.pop("idx", None)
        out.setdefault(parent, []).append(row)
    return out


def _recent_releases(cutoff) -> List[Dict[str, Any]]:
    rows = frappe.db.sql(
        """select
                name as release_no, release_datetime, customer, customer_name,
                customer_order, custom_nkt_source_warehouse as source_warehouse,
                release_status, total_release_quantity, is_partial_release, remarks
              from `tabNKT Warehouse Release`
             where date(release_datetime) >= %s
               and docstatus <> 2
             order by release_datetime desc, name desc""",
        (cutoff,),
        as_dict=True,
    )
    item_map = _release_items([r.release_no for r in rows])
    for row in rows:
        row["items"] = item_map.get(row.release_no, [])
    return rows


def _recent_returns(cutoff) -> List[Dict[str, Any]]:
    if not frappe.db.exists("DocType", "NKT Customer Return"):
        return []
    return frappe.db.sql(
        """select
                name as return_no, return_datetime, customer, customer_name,
                warehouse_release, customer_order, return_warehouse,
                return_status, warehouse_receipt_status, total_return_quantity,
                settlement_type, settlement_amount, calculated_return_credit,
                replacement_value, customer_pays, refund_due,
                return_reason, remarks
              from `tabNKT Customer Return`
             where date(return_datetime) >= %s
               and docstatus <> 2
             order by return_datetime desc, name desc""",
        (cutoff,),
        as_dict=True,
    )


def _open_receivables() -> List[Dict[str, Any]]:
    return frappe.db.sql(
        """select
                name, customer, customer_name, customer_order,
                posting_date, due_date, original_amount, amount_paid,
                outstanding_amount, status, credit_control_status
              from `tabNKT Customer Receivable`
             where status in ('Open','Partially Paid')
               and coalesce(outstanding_amount,0) > 0
               and docstatus <> 2
             order by posting_date asc, creation asc, name asc""",
        as_dict=True,
    )


def _active_reservations(store_warehouse: str) -> List[Dict[str, Any]]:
    if not frappe.db.exists("DocType", "Stock Reservation Entry"):
        return []
    return frappe.get_all(
        "Stock Reservation Entry",
        filters={"warehouse": store_warehouse, "status": "Reserved"},
        fields=["name", "item_code", "warehouse", "reserved_qty", "status", "voucher_type", "voucher_no", "modified"],
        order_by="modified asc",
        limit_page_length=5000,
    )


def _pending_releases(store_warehouse: str) -> List[Dict[str, Any]]:
    return frappe.db.sql(
        """select
                name as release_no, release_datetime, customer, customer_name,
                customer_order, custom_nkt_source_warehouse as source_warehouse,
                release_status, total_release_quantity, remarks
              from `tabNKT Warehouse Release`
             where custom_nkt_source_warehouse=%s
               and release_status not in ('Released','Cancelled','Recalled')
               and docstatus <> 2
             order by release_datetime asc, name asc""",
        (store_warehouse,),
        as_dict=True,
    )


def _device_policies() -> List[Dict[str, Any]]:
    rows = frappe.get_all(
        "NKT Device Registry",
        filters={"status": ["in", ["Active", "Restricted"]]},
        fields=[
            "device_id", "device_class", "operational_context",
            "assigned_user", "status", "policy_version",
        ],
        order_by="device_id asc",
        limit_page_length=5000,
    )
    for row in rows:
        user = row.get("assigned_user")
        row["assigned_roles"] = sorted(frappe.get_roles(user) or []) if user else []

        user_status = "Active"
        user_policy_version = 0
        if user and frappe.db.exists("DocType", "NKT User Security State"):
            state = frappe.db.get_value(
                "NKT User Security State",
                user,
                ["status", "policy_version"],
                as_dict=True,
            )
            if state:
                user_status = state.status or "Active"
                user_policy_version = int(state.policy_version or 1)

        row["user_security_status"] = user_status
        row["user_security_policy_version"] = user_policy_version
        row["effective_ui_mode"] = (
            "limited"
            if row.get("status") == "Restricted" or user_status == "Restricted"
            else "normal"
        )
    return rows


def _canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    ).encode("utf-8")


def section_sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_json_bytes(value)).hexdigest()


def snapshot_summary(snapshot: Dict[str, Any]) -> Dict[str, Any]:
    sections = {}
    for key, value in snapshot.items():
        if isinstance(value, list):
            sections[key] = {
                "count": len(value),
                "sha256": section_sha256(value),
            }
    raw = _canonical_json_bytes(snapshot)
    return {
        "schema_version": snapshot["schema_version"],
        "generated_at": snapshot["generated_at"],
        "detail_cutoff": snapshot["detail_cutoff"],
        "store_warehouse": snapshot["store_warehouse"],
        "approx_json_bytes": len(raw),
        "snapshot_sha256": hashlib.sha256(raw).hexdigest(),
        "sections": sections,
    }


def _assert_no_sensitive_keys(value: Any, path: str = "$") -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            if key in FORBIDDEN_SENSITIVE_KEYS:
                raise frappe.ValidationError(f"Sensitive Edge key is forbidden at {path}.{key}")
            _assert_no_sensitive_keys(child, f"{path}.{key}")
    elif isinstance(value, list):
        for idx, child in enumerate(value):
            _assert_no_sensitive_keys(child, f"{path}[{idx}]")


def validate_snapshot(snapshot: Dict[str, Any], *, today=None) -> Dict[str, Any]:
    cutoff = _detail_cutoff(today)
    for section in FORBIDDEN_TOP_LEVEL_SECTIONS:
        if section in snapshot:
            raise frappe.ValidationError(f"Forbidden Edge section present: {section}")

    _assert_no_sensitive_keys(snapshot)

    date_specs = {
        "orders": "order_date",
        "sales": "business_date",
        "releases": "release_datetime",
        "returns": "return_datetime",
    }
    for section, field in date_specs.items():
        for row in snapshot.get(section, []):
            raw = row.get(field)
            if not raw:
                continue
            if getdate(raw) < cutoff:
                raise frappe.ValidationError(
                    f"Edge detail retention violation: {section}.{field}={raw} older than {cutoff}"
                )

    # Manager-PIN verifiers are allowed only inside the encrypted Store Edge
    # snapshot. They are salted digests, never plaintext PINs, and are not
    # returned through the frontline Fast Screen bootstrap.
    for row in snapshot.get("selling_price_authorizers", []):
        required = {"user", "credential_salt", "credential_digest", "iterations", "enabled"}
        extra = set(row) - required
        if extra or not required.issubset(set(row)):
            raise frappe.ValidationError("Store Edge selling-price authorizer verifier shape is invalid.")
        if any(key in row for key in ("pin", "token", "password")):
            raise frappe.ValidationError("Plain Manager PIN material is forbidden in the Store Edge snapshot.")

    # Open receivables are intentionally NOT age-trimmed.
    return {
        "valid": True,
        "detail_cutoff": cutoff.isoformat(),
        "critical_offline_mutations_enabled": False,
    }


def build_store_edge_snapshot(
    device_id: str,
    *,
    user: Optional[str] = None,
    today=None,
) -> Dict[str, Any]:
    user = _session_user(user)
    device = _edge_device(device_id, user=user)
    cutoff = _detail_cutoff(today)
    store_warehouse = _resolve_store_warehouse()

    from nkt_operations.nkt_store_operations import manager_authorization as nkt_manager_pin

    snapshot = {
        "schema_version": SNAPSHOT_SCHEMA_VERSION,
        "foundation_version": FOUNDATION_VERSION,
        "generated_at": datetime.now(PH_TZ).isoformat(),
        "business_timezone": "Asia/Manila",
        "detail_days": DETAIL_DAYS,
        "detail_cutoff": cutoff.isoformat(),
        "item_movement_trace_days": ITEM_MOVEMENT_TRACE_DAYS,
        "store_warehouse": store_warehouse,
        "device_policy_version": device["policy_version"],
        "critical_offline_mutations_enabled": False,
        "items": _current_items(store_warehouse),
        "customers": _current_customers(cutoff),
        "orders": _recent_orders(cutoff),
        "sales": _recent_sales(cutoff),
        "releases": _recent_releases(cutoff),
        "returns": _recent_returns(cutoff),
        "open_receivables": _open_receivables(),
        "active_store_reservations": _active_reservations(store_warehouse),
        "pending_store_releases": _pending_releases(store_warehouse),
        "device_policies": _device_policies(),
        "selling_price_authorizers": nkt_manager_pin.snapshot_authorizers(),
        "selling_price_variations": nkt_manager_pin.snapshot_variations(),
    }

    validate_snapshot(snapshot, today=today)
    return snapshot


@frappe.whitelist()
def get_store_edge_snapshot(device_id: str):
    """
    Bounded Store Edge bootstrap endpoint.

    Deliberately not a frontline API:
    - caller must be privileged at this foundation stage;
    - Device Registry record must be an Active Store Edge device;
    - no supplier/payroll/GL/all-time-history payload;
    - no payment reference fields;
    - critical offline mutations remain disabled.
    """
    return build_store_edge_snapshot(device_id, user=_session_user())


@frappe.whitelist()
def get_store_edge_snapshot_summary(device_id: str):
    snapshot = build_store_edge_snapshot(device_id, user=_session_user())
    return snapshot_summary(snapshot)
