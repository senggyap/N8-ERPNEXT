from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Dict, Iterable, List, Optional

import frappe
from frappe.utils import cint, getdate

from nkt_operations.nkt_store_operations.features.offline_edge.policy import (
    PH_TZ,
    device_policy_snapshot,
    is_privileged_user,
)

FOUNDATION_VERSION = "C15C.4-R1"

ENCODER_HISTORY_ROLES = {"NKT Encoder", "NKT Credit Controller"}
ENCODER_ACCOUNT_ROLES = {"NKT Encoder", "NKT Credit Controller"}
CASHIER_LOOKUP_ROLES = {"NKT Cashier"}
ITEM_MOVEMENT_ROLES = {"NKT Encoder"}

READ_CONTRACT = {
    "edge_detailed_history_days": 30,
    "item_movement_trace_days": 14,
    "item_movement_api_max_days": 30,
    "cashier_normal_sale_lookup_days": 45,
    "open_receivables_age_limit_days": None,
    "encoder_all_time_history_when_primary_reachable": True,
    "cashier_edge_detail_days": 30,
    "cashier_days_31_to_45_during_primary_outage": "wait_for_primary",
    "bulk_export_allowed": False,
    "critical_offline_mutations_enabled": False,
}


def _session_user(user: Optional[str] = None) -> str:
    user = user or frappe.session.user
    if not user or user == "Guest":
        raise frappe.PermissionError("Read service unavailable.")
    return user


def _roles(user: str) -> set[str]:
    return set(frappe.get_roles(user) or [])


def _require_role(user: str, allowed: Iterable[str]) -> None:
    if is_privileged_user(user):
        return
    if not (_roles(user) & set(allowed)):
        raise frappe.PermissionError("Read service unavailable.")


def _authorize_device(
    device_id: str,
    *,
    user: str,
    operational_context: str = "NKT Retail",
) -> Dict[str, Any]:
    return device_policy_snapshot(
        device_id,
        user=user,
        requested_context=operational_context,
    )


def _authorize_sensitive_read_device(
    device_id: str,
    *,
    user: str,
    operational_context: str = "NKT Retail",
) -> Dict[str, Any]:
    policy = _authorize_device(
        device_id,
        user=user,
        operational_context=operational_context,
    )
    # Restricted mode is deliberately a limited/tally operating surface.
    # Hiding a UI button is not the security boundary: sensitive history
    # must refuse server-side as well.
    if policy.get("ui_mode") != "normal":
        raise frappe.PermissionError("Read service unavailable.")
    return policy


def _today_manila():
    return datetime.now(PH_TZ).date()


def cashier_sale_age_allowed(business_date: Any, *, today: Any = None) -> bool:
    business = getdate(business_date)
    today_date = getdate(today) if today else _today_manila()
    age_days = (today_date - business).days
    return 0 <= age_days <= READ_CONTRACT["cashier_normal_sale_lookup_days"]


def _bounded_page_size(value: Any, *, default: int = 50, maximum: int = 100) -> int:
    value = cint(value or default)
    if value <= 0:
        value = default
    return min(value, maximum)


def _bounded_offset(value: Any) -> int:
    value = cint(value or 0)
    if value < 0:
        value = 0
    # This API is per-customer/per-purpose, not a bulk database dump.
    return min(value, 10000)


def _date_filter(
    from_date: Optional[Any],
    to_date: Optional[Any],
    *,
    max_days: Optional[int] = None,
) -> tuple[Optional[Any], Optional[Any]]:
    start = getdate(from_date) if from_date else None
    end = getdate(to_date) if to_date else None
    if start and end and start > end:
        raise frappe.ValidationError("From Date cannot be later than To Date.")
    if max_days is not None and start and end:
        if (end - start).days > max_days:
            raise frappe.ValidationError(
                f"Date range may not exceed {max_days + 1} calendar dates."
            )
    return start, end


def _payment_methods_for_sale(sale_name: Optional[str]) -> List[str]:
    if not sale_name:
        return []
    rows = frappe.db.sql(
        """select payment_method
             from `tabNKT Payment Detail`
            where parent=%s and parenttype='NKT Cashier Sale'
            order by idx""",
        sale_name,
        as_dict=True,
    )
    out = []
    for row in rows:
        method = (row.get("payment_method") or "").strip()
        if method and method not in out:
            out.append(method)
    return out


def _declared_payment_methods_for_order(order_name: Optional[str]) -> List[str]:
    if not order_name:
        return []
    rows = frappe.db.sql(
        """select payment_method
             from `tabNKT Declared Payment`
            where parent=%s and parenttype='NKT Customer Order'
            order by idx""",
        order_name,
        as_dict=True,
    )
    out = []
    for row in rows:
        method = (row.get("payment_method") or "").strip()
        if method and method not in out:
            out.append(method)
    return out


def _order_items(order_names: List[str]) -> Dict[str, List[Dict[str, Any]]]:
    if not order_names:
        return {}
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
    out = {name: [] for name in order_names}
    for row in rows:
        parent = row.pop("parent")
        row.pop("idx", None)
        out.setdefault(parent, []).append(row)
    return out


def _sale_items(sale_name: str) -> List[Dict[str, Any]]:
    return frappe.db.sql(
        """select item, item_name, quantity, uom, source_warehouse,
                  final_rate, amount, remarks
             from `tabNKT Cashier Sale Item`
            where parent=%s and parenttype='NKT Cashier Sale'
            order by idx""",
        sale_name,
        as_dict=True,
    )


def _sale_payments(sale_name: str) -> List[Dict[str, Any]]:
    # Exact transaction view may show its own approved payment details, but this
    # deliberately does not expose generic/bulk payment history.
    return frappe.db.sql(
        """select payment_method, amount, card_surcharge, collected_amount,
                  cash_tendered, change_amount, reference_number,
                  reference_datetime, bank_or_provider, check_number, check_date,
                  verification_status
             from `tabNKT Payment Detail`
            where parent=%s and parenttype='NKT Cashier Sale'
            order by idx""",
        sale_name,
        as_dict=True,
    )


def read_contract(
    device_id: str,
    *,
    user: Optional[str] = None,
) -> Dict[str, Any]:
    user = _session_user(user)
    policy = _authorize_device(device_id, user=user)
    return {
        "foundation_version": FOUNDATION_VERSION,
        "device_ui_mode": policy["ui_mode"],
        **READ_CONTRACT,
    }


def encoder_customer_history(
    customer: str,
    device_id: str,
    *,
    item: Optional[str] = None,
    plate_number: Optional[str] = None,
    os_no: Optional[str] = None,
    from_date: Optional[Any] = None,
    to_date: Optional[Any] = None,
    limit: int = 50,
    offset: int = 0,
    user: Optional[str] = None,
) -> Dict[str, Any]:
    user = _session_user(user)
    _authorize_sensitive_read_device(device_id, user=user)
    _require_role(user, ENCODER_HISTORY_ROLES)

    customer = (customer or "").strip()
    if not customer:
        raise frappe.ValidationError("Customer is required.")

    start, end = _date_filter(from_date, to_date)
    limit = _bounded_page_size(limit)
    offset = _bounded_offset(offset)

    conditions = ["co.customer=%s"]
    params: List[Any] = [customer]

    if start:
        conditions.append("co.order_date >= %s")
        params.append(start)
    if end:
        conditions.append("co.order_date <= %s")
        params.append(end)
    if item:
        conditions.append(
            """exists (
                 select 1
                   from `tabNKT Customer Order Item` oi
                  where oi.parent=co.name
                    and oi.parenttype='NKT Customer Order'
                    and oi.item=%s
               )"""
        )
        params.append(item)

    order_meta = frappe.get_meta("NKT Customer Order")
    plate_field = "plate_number" if order_meta.has_field("plate_number") else ("custom_nkt_plate_number" if order_meta.has_field("custom_nkt_plate_number") else None)
    os_field = "physical_order_slip_no" if order_meta.has_field("physical_order_slip_no") else ("source_order_slip" if order_meta.has_field("source_order_slip") else None)
    plate_number = str(plate_number or "").strip()
    os_no = str(os_no or "").strip()
    if plate_number:
        if plate_field:
            conditions.append(f"COALESCE(co.`{plate_field}`, '') LIKE %s")
            params.append(f"%{plate_number}%")
        else:
            conditions.append("1=0")
    if os_no:
        if os_field:
            conditions.append(f"COALESCE(co.`{os_field}`, '') LIKE %s")
            params.append(f"%{os_no}%")
        else:
            conditions.append("1=0")
    plate_select = f"co.`{plate_field}`" if plate_field else "''"
    os_select = f"co.`{os_field}`" if os_field else "''"

    params.extend([limit, offset])
    rows = frappe.db.sql(
        f"""select
                co.name as order_no,
                co.order_date,
                co.creation as encoded_at,
                co.customer,
                co.customer_name,
                co.encoder,
                co.status as order_status,
                co.payment_status,
                co.grand_total,
                co.default_warehouse,
                co.notes as remarks,
                {plate_select} as plate_number,
                {os_select} as os_no,
                co.matched_cashier_sale,
                cs.sale_datetime,
                cs.business_date as cashier_business_date,
                cs.cashier,
                cs.linked_payment_receipt
              from `tabNKT Customer Order` co
              left join `tabNKT Cashier Sale` cs
                on cs.name=co.matched_cashier_sale
             where {" and ".join(conditions)}
             order by co.order_date desc, co.creation desc, co.name desc
             limit %s offset %s""",
        tuple(params),
        as_dict=True,
    )

    item_map = _order_items([r["order_no"] for r in rows])
    for row in rows:
        row["items"] = item_map.get(row["order_no"], [])
        row["payment_methods"] = _payment_methods_for_sale(row.get("matched_cashier_sale"))
        if not row["payment_methods"]:
            row["payment_methods"] = _declared_payment_methods_for_order(row["order_no"])

    return {
        "customer": customer,
        "scope": "all_time_on_primary",
        "bulk_export_allowed": False,
        "limit": limit,
        "offset": offset,
        "rows": rows,
    }


def customer_open_receivables(
    customer: str,
    device_id: str,
    *,
    user: Optional[str] = None,
) -> Dict[str, Any]:
    user = _session_user(user)
    _authorize_sensitive_read_device(device_id, user=user)
    _require_role(user, ENCODER_ACCOUNT_ROLES)

    customer = (customer or "").strip()
    if not customer:
        raise frappe.ValidationError("Customer is required.")

    rows = frappe.db.sql(
        """select name, customer, customer_name, customer_order, posting_date,
                  due_date, original_amount, amount_paid, outstanding_amount,
                  status, custom_nkt_last_collection_on
             from `tabNKT Customer Receivable`
            where customer=%s
              and status in ('Open', 'Partially Paid')
              and coalesce(outstanding_amount, 0) > 0
            order by posting_date asc, creation asc""",
        customer,
        as_dict=True,
    )
    return {
        "customer": customer,
        "age_limit_days": None,
        "rows": rows,
        "total_outstanding": sum(float(r.get("outstanding_amount") or 0) for r in rows),
    }


def cashier_sale_lookup(
    sale_name: str,
    device_id: str,
    *,
    user: Optional[str] = None,
) -> Dict[str, Any]:
    user = _session_user(user)
    _authorize_sensitive_read_device(device_id, user=user)
    _require_role(user, CASHIER_LOOKUP_ROLES)

    sale_name = (sale_name or "").strip()
    if not sale_name:
        raise frappe.ValidationError("Sale / Receipt No. is required.")

    row = frappe.db.get_value(
        "NKT Cashier Sale",
        sale_name,
        [
            "name", "sale_datetime", "business_date", "cashier", "cashier_shift",
            "settlement_location", "customer", "customer_name", "status",
            "total_quantity", "grand_total", "total_payment", "total_cash",
            "total_non_cash", "card_surcharge_total", "total_collected",
            "total_account_charge", "matched_customer_order",
            "linked_payment_receipt", "notes",
        ],
        as_dict=True,
    )
    if not row:
        raise frappe.DoesNotExistError("Sale lookup unavailable.")

    privileged = is_privileged_user(user)
    age_days = (_today_manila() - getdate(row["business_date"])).days
    if not privileged and not cashier_sale_age_allowed(row["business_date"]):
        raise frappe.PermissionError(
            "Older sale requires a specific Owner/Admin grant."
        )

    row["items"] = _sale_items(sale_name)
    row["payments"] = _sale_payments(sale_name)
    row["age_days"] = age_days
    row["normal_cashier_45_day_entitlement"] = cashier_sale_age_allowed(row["business_date"])
    row["older_specific_sale_grant_model"] = "not_enabled_yet"
    return row


def cashier_sale_search(
    device_id: str,
    *,
    customer: Optional[str] = None,
    item: Optional[str] = None,
    sale_name: Optional[str] = None,
    limit: int = 50,
    user: Optional[str] = None,
) -> Dict[str, Any]:
    user = _session_user(user)
    _authorize_sensitive_read_device(device_id, user=user)
    _require_role(user, CASHIER_LOOKUP_ROLES)

    limit = _bounded_page_size(limit)
    privileged = is_privileged_user(user)

    conditions = ["1=1"]
    params: List[Any] = []

    if not privileged:
        cutoff = _today_manila() - timedelta(
            days=READ_CONTRACT["cashier_normal_sale_lookup_days"]
        )
        conditions.append("cs.business_date >= %s")
        params.append(cutoff)

    if customer:
        conditions.append("cs.customer=%s")
        params.append(customer)
    if sale_name:
        conditions.append("cs.name=%s")
        params.append(sale_name)
    if item:
        conditions.append(
            """exists (
                 select 1
                   from `tabNKT Cashier Sale Item` si
                  where si.parent=cs.name
                    and si.parenttype='NKT Cashier Sale'
                    and si.item=%s
               )"""
        )
        params.append(item)

    params.append(limit)
    rows = frappe.db.sql(
        f"""select cs.name, cs.sale_datetime, cs.business_date,
                   cs.customer, cs.customer_name, cs.grand_total,
                   cs.status, cs.matched_customer_order
              from `tabNKT Cashier Sale` cs
             where {" and ".join(conditions)}
             order by cs.business_date desc, cs.sale_datetime desc, cs.name desc
             limit %s""",
        tuple(params),
        as_dict=True,
    )
    return {
        "normal_cashier_lookup_days": READ_CONTRACT["cashier_normal_sale_lookup_days"],
        "bulk_export_allowed": False,
        "rows": rows,
    }


def item_movement_history(
    item: str,
    device_id: str,
    *,
    from_date: Any,
    to_date: Optional[Any] = None,
    user: Optional[str] = None,
) -> Dict[str, Any]:
    user = _session_user(user)
    _authorize_sensitive_read_device(device_id, user=user)
    _require_role(user, ITEM_MOVEMENT_ROLES)

    item = (item or "").strip()
    if not item:
        raise frappe.ValidationError("Item is required.")

    start, end = _date_filter(
        from_date,
        to_date or from_date,
        max_days=READ_CONTRACT["item_movement_api_max_days"] - 1,
    )
    assert start and end

    rows = frappe.db.sql(
        """select
                wr.name as release_no,
                wr.release_datetime,
                date(wr.release_datetime) as movement_date,
                time(wr.release_datetime) as movement_time,
                wr.customer,
                wr.customer_name,
                wri.item,
                wri.item_name,
                wri.release_quantity as qty,
                wri.uom,
                wr.customer_order,
                co.matched_cashier_sale,
                cs.name as cashier_sale
              from `tabNKT Warehouse Release` wr
              join `tabNKT Warehouse Release Item` wri
                on wri.parent=wr.name
               and wri.parenttype='NKT Warehouse Release'
              left join `tabNKT Customer Order` co
                on co.name=wr.customer_order
              left join `tabNKT Cashier Sale` cs
                on cs.name=co.matched_cashier_sale
             where wr.release_status='Released'
               and wri.item=%s
               and date(wr.release_datetime) between %s and %s
             order by wr.release_datetime asc, wr.name asc, wri.idx asc""",
        (item, start, end),
        as_dict=True,
    )

    for row in rows:
        methods = _payment_methods_for_sale(row.get("cashier_sale"))
        if not methods:
            methods = _declared_payment_methods_for_order(row.get("customer_order"))
        row["payment_methods"] = methods
        row["payment_method"] = " + ".join(methods) if methods else None
        row["order_receipt_no"] = row.get("cashier_sale") or row.get("customer_order")
        row["open_doctype"] = (
            "NKT Cashier Sale" if row.get("cashier_sale") else "NKT Customer Order"
        )
        row["open_name"] = row.get("cashier_sale") or row.get("customer_order")

    return {
        "item": item,
        "from_date": start,
        "to_date": end,
        "trace_window_days": READ_CONTRACT["item_movement_trace_days"],
        "api_max_days": READ_CONTRACT["item_movement_api_max_days"],
        "bulk_export_allowed": False,
        "rows": rows,
    }


@frappe.whitelist()
def get_read_contract(device_id: str):
    return read_contract(device_id)


@frappe.whitelist()
def get_encoder_customer_history(
    customer: str,
    device_id: str,
    item: Optional[str] = None,
    plate_number: Optional[str] = None,
    os_no: Optional[str] = None,
    from_date: Optional[str] = None,
    to_date: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
):
    return encoder_customer_history(
        customer,
        device_id,
        item=item,
        plate_number=plate_number,
        os_no=os_no,
        from_date=from_date,
        to_date=to_date,
        limit=limit,
        offset=offset,
    )


@frappe.whitelist()
def get_customer_open_receivables(customer: str, device_id: str):
    return customer_open_receivables(customer, device_id)


@frappe.whitelist()
def get_cashier_sale_lookup(sale_name: str, device_id: str):
    return cashier_sale_lookup(sale_name, device_id)


@frappe.whitelist()
def search_cashier_sales(
    device_id: str,
    customer: Optional[str] = None,
    item: Optional[str] = None,
    sale_name: Optional[str] = None,
    limit: int = 50,
):
    return cashier_sale_search(
        device_id,
        customer=customer,
        item=item,
        sale_name=sale_name,
        limit=limit,
    )


@frappe.whitelist()
def get_item_movement_history(
    item: str,
    device_id: str,
    from_date: str,
    to_date: Optional[str] = None,
):
    return item_movement_history(
        item,
        device_id,
        from_date=from_date,
        to_date=to_date,
    )
