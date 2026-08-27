from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional

import frappe
from frappe.utils import cint, getdate

from nkt_operations.nkt_store_operations.features.offline_edge.policy import PRIVILEGED_ROLES
from nkt_operations.nkt_store_operations.features.offline_edge.read_services import (
    cashier_sale_lookup as primary_cashier_sale_lookup,
    cashier_sale_search as primary_cashier_sale_search,
    customer_open_receivables as primary_customer_open_receivables,
    encoder_customer_history as primary_encoder_customer_history,
    item_movement_history as primary_item_movement_history,
)
from nkt_operations.nkt_store_operations.features.offline_edge.edge_store import (
    route_read_source,
)

FOUNDATION_VERSION = "C15C.7A-R1"

ENCODER_HISTORY_ROLES = {"NKT Encoder", "NKT Credit Controller"}
ENCODER_ACCOUNT_ROLES = {"NKT Encoder", "NKT Credit Controller"}
CASHIER_LOOKUP_ROLES = {"NKT Cashier"}
ITEM_MOVEMENT_ROLES = {"NKT Encoder"}

EDGE_ENCODER_HISTORY_DAYS = 30
EDGE_CASHIER_HISTORY_DAYS = 30
EDGE_ITEM_MOVEMENT_DAYS = 14


def _session_user(user: Optional[str] = None) -> str:
    user = user or frappe.session.user
    if not user or user == "Guest":
        raise frappe.PermissionError("Read service unavailable.")
    return user


def _bounded_limit(value: Any, default: int = 50, maximum: int = 100) -> int:
    value = cint(value or default)
    if value <= 0:
        value = default
    return min(value, maximum)


def _bounded_offset(value: Any) -> int:
    value = cint(value or 0)
    return max(0, min(value, 10000))


def _runtime_backend() -> str:
    """
    The browser never chooses the data source.

    Today the normal site is Primary. A future physical Store Edge will set
    `nkt_runtime_role = "Store Edge"` and C15C.7B will attach the encrypted
    local snapshot provider. Keeping the public API stable now prevents a later
    UI rewrite.
    """
    role = str(frappe.conf.get("nkt_runtime_role") or "Primary").strip()
    if role == "Primary":
        return "Primary"
    if role == "Store Edge":
        return "Store Edge"
    raise frappe.PermissionError("Read service unavailable.")


def _edge_device_policy(snapshot: Dict[str, Any], device_id: str, user: str) -> Dict[str, Any]:
    for row in snapshot.get("device_policies", []) or []:
        if row.get("device_id") != device_id:
            continue

        status = row.get("status")
        effective_ui_mode = row.get("effective_ui_mode") or (
            "limited" if status == "Restricted" else "normal"
        )
        if status != "Active" or effective_ui_mode != "normal":
            # Device restriction OR centrally restricted assigned user denies
            # sensitive Edge history. UI hiding is never the security boundary.
            raise frappe.PermissionError("Read service unavailable.")

        assigned = row.get("assigned_user")
        if assigned and assigned != user:
            raise frappe.PermissionError("Read service unavailable.")

        roles = set(row.get("assigned_roles") or [])
        return {
            "device_id": device_id,
            "assigned_user": assigned,
            "roles": roles,
            "status": status,
        }
    raise frappe.PermissionError("Read service unavailable.")


def _edge_require_role(policy: Dict[str, Any], allowed: Iterable[str]) -> None:
    roles = set(policy.get("roles") or [])
    if roles & PRIVILEGED_ROLES:
        return
    if not (roles & set(allowed)):
        raise frappe.PermissionError("Read service unavailable.")


def _edge_customer_history(
    snapshot: Dict[str, Any],
    customer: str,
    device_id: str,
    *,
    item: Optional[str] = None,
    from_date: Optional[Any] = None,
    to_date: Optional[Any] = None,
    limit: int = 50,
    offset: int = 0,
    user: Optional[str] = None,
) -> Dict[str, Any]:
    user = _session_user(user)
    policy = _edge_device_policy(snapshot, device_id, user)
    _edge_require_role(policy, ENCODER_HISTORY_ROLES)

    customer = (customer or "").strip()
    if not customer:
        raise frappe.ValidationError("Customer is required.")

    start = getdate(from_date) if from_date else None
    end = getdate(to_date) if to_date else None
    if start and end and start > end:
        raise frappe.ValidationError("From Date cannot be later than To Date.")

    limit = _bounded_limit(limit)
    offset = _bounded_offset(offset)

    sales = {r.get("sale_no"): r for r in snapshot.get("sales", []) or []}
    rows = []
    for order in snapshot.get("orders", []) or []:
        if order.get("customer") != customer:
            continue
        od = getdate(order.get("order_date"))
        if start and od < start:
            continue
        if end and od > end:
            continue
        if item and not any(i.get("item") == item for i in order.get("items", []) or []):
            continue

        sale = sales.get(order.get("matched_cashier_sale")) or {}
        rows.append({
            "order_no": order.get("order_no"),
            "order_date": order.get("order_date"),
            "encoded_at": order.get("encoded_at"),
            "customer": order.get("customer"),
            "customer_name": order.get("customer_name"),
            "encoder": order.get("encoder"),
            "order_status": order.get("status"),
            "payment_status": order.get("payment_status"),
            "grand_total": order.get("grand_total"),
            "default_warehouse": order.get("default_warehouse"),
            "remarks": order.get("remarks"),
            "matched_cashier_sale": order.get("matched_cashier_sale"),
            "sale_datetime": sale.get("sale_datetime"),
            "cashier_business_date": sale.get("business_date"),
            "cashier": sale.get("cashier"),
            "linked_payment_receipt": sale.get("linked_payment_receipt"),
            "items": order.get("items", []) or [],
            "payment_methods": sale.get("payment_methods", []) or [],
        })

    rows.sort(
        key=lambda r: (
            str(r.get("order_date") or ""),
            str(r.get("encoded_at") or ""),
            str(r.get("order_no") or ""),
        ),
        reverse=True,
    )
    rows = rows[offset:offset + limit]

    return {
        "customer": customer,
        "scope": "bounded_30d_on_edge",
        "edge_detail_days": EDGE_ENCODER_HISTORY_DAYS,
        "bulk_export_allowed": False,
        "limit": limit,
        "offset": offset,
        "rows": rows,
        "served_by": "Edge",
    }


def _edge_open_receivables(
    snapshot: Dict[str, Any],
    customer: str,
    device_id: str,
    *,
    user: Optional[str] = None,
) -> Dict[str, Any]:
    user = _session_user(user)
    policy = _edge_device_policy(snapshot, device_id, user)
    _edge_require_role(policy, ENCODER_ACCOUNT_ROLES)

    customer = (customer or "").strip()
    if not customer:
        raise frappe.ValidationError("Customer is required.")

    rows = [
        dict(r)
        for r in (snapshot.get("open_receivables", []) or [])
        if r.get("customer") == customer
        and r.get("status") in {"Open", "Partially Paid"}
        and float(r.get("outstanding_amount") or 0) > 0
    ]
    rows.sort(key=lambda r: (str(r.get("posting_date") or ""), str(r.get("name") or "")))

    return {
        "customer": customer,
        "age_limit_days": None,
        "rows": rows,
        "total_outstanding": sum(float(r.get("outstanding_amount") or 0) for r in rows),
        "served_by": "Edge",
    }


def _edge_cashier_sale_search(
    snapshot: Dict[str, Any],
    device_id: str,
    *,
    customer: Optional[str] = None,
    item: Optional[str] = None,
    sale_name: Optional[str] = None,
    limit: int = 50,
    user: Optional[str] = None,
) -> Dict[str, Any]:
    user = _session_user(user)
    policy = _edge_device_policy(snapshot, device_id, user)
    _edge_require_role(policy, CASHIER_LOOKUP_ROLES)

    limit = _bounded_limit(limit)
    rows = []
    for sale in snapshot.get("sales", []) or []:
        if customer and sale.get("customer") != customer:
            continue
        if sale_name and sale.get("sale_no") != sale_name:
            continue
        if item and not any(i.get("item") == item for i in sale.get("items", []) or []):
            continue
        rows.append({
            "name": sale.get("sale_no"),
            "sale_datetime": sale.get("sale_datetime"),
            "business_date": sale.get("business_date"),
            "customer": sale.get("customer"),
            "customer_name": sale.get("customer_name"),
            "grand_total": sale.get("grand_total"),
            "status": sale.get("status"),
            "matched_customer_order": sale.get("matched_customer_order"),
        })

    rows.sort(
        key=lambda r: (
            str(r.get("business_date") or ""),
            str(r.get("sale_datetime") or ""),
            str(r.get("name") or ""),
        ),
        reverse=True,
    )

    return {
        "normal_cashier_lookup_days": 45,
        "edge_detail_days": EDGE_CASHIER_HISTORY_DAYS,
        "cashier_days_31_to_45_during_primary_outage": "wait_for_primary",
        "bulk_export_allowed": False,
        "rows": rows[:limit],
        "served_by": "Edge",
    }


def _edge_cashier_sale_lookup(
    snapshot: Dict[str, Any],
    sale_name: str,
    device_id: str,
    *,
    user: Optional[str] = None,
) -> Dict[str, Any]:
    user = _session_user(user)
    policy = _edge_device_policy(snapshot, device_id, user)
    _edge_require_role(policy, CASHIER_LOOKUP_ROLES)

    sale_name = (sale_name or "").strip()
    if not sale_name:
        raise frappe.ValidationError("Sale / Receipt No. is required.")

    sale = next(
        (r for r in snapshot.get("sales", []) or [] if r.get("sale_no") == sale_name),
        None,
    )
    if not sale:
        raise frappe.DoesNotExistError("Sale lookup unavailable.")

    out = dict(sale)
    out["name"] = out.pop("sale_no")
    # Edge intentionally does not retain payment reference/check/provider details.
    out["payments"] = [
        {"payment_method": method}
        for method in (sale.get("payment_methods") or [])
    ]
    out["edge_redacted_payment_detail"] = True
    out["normal_cashier_45_day_entitlement"] = True
    out["older_specific_sale_grant_model"] = "not_enabled_yet"
    out["served_by"] = "Edge"
    return out


def _edge_item_movement_history(
    snapshot: Dict[str, Any],
    item: str,
    device_id: str,
    *,
    from_date: Any,
    to_date: Optional[Any] = None,
    user: Optional[str] = None,
) -> Dict[str, Any]:
    user = _session_user(user)
    policy = _edge_device_policy(snapshot, device_id, user)
    _edge_require_role(policy, ITEM_MOVEMENT_ROLES)

    item = (item or "").strip()
    if not item:
        raise frappe.ValidationError("Item is required.")

    start = getdate(from_date)
    end = getdate(to_date or from_date)
    if start > end:
        raise frappe.ValidationError("From Date cannot be later than To Date.")
    if (end - start).days > EDGE_ITEM_MOVEMENT_DAYS - 1:
        raise frappe.ValidationError(
            f"Edge Item Movement range may not exceed {EDGE_ITEM_MOVEMENT_DAYS} calendar dates."
        )

    orders = {r.get("order_no"): r for r in snapshot.get("orders", []) or []}
    sales = {r.get("sale_no"): r for r in snapshot.get("sales", []) or []}
    rows = []

    for release in snapshot.get("releases", []) or []:
        if release.get("release_status") != "Released":
            continue
        rd = getdate(release.get("release_datetime"))
        if rd < start or rd > end:
            continue

        order = orders.get(release.get("customer_order")) or {}
        sale = sales.get(order.get("matched_cashier_sale")) or {}

        for line in release.get("items", []) or []:
            if line.get("item") != item:
                continue
            methods = sale.get("payment_methods", []) or []
            dt = release.get("release_datetime")
            rows.append({
                "release_no": release.get("release_no"),
                "release_datetime": dt,
                "movement_date": getdate(dt),
                "movement_time": str(dt)[11:19] if dt else None,
                "customer": release.get("customer"),
                "customer_name": release.get("customer_name"),
                "item": line.get("item"),
                "item_name": line.get("item_name"),
                "qty": line.get("release_quantity"),
                "uom": line.get("uom"),
                "customer_order": release.get("customer_order"),
                "matched_cashier_sale": order.get("matched_cashier_sale"),
                "cashier_sale": sale.get("sale_no"),
                "payment_methods": methods,
                "payment_method": " + ".join(methods) if methods else None,
                "order_receipt_no": sale.get("sale_no") or release.get("customer_order"),
                "open_doctype": "NKT Cashier Sale" if sale.get("sale_no") else "NKT Customer Order",
                "open_name": sale.get("sale_no") or release.get("customer_order"),
            })

    rows.sort(key=lambda r: (str(r.get("release_datetime") or ""), str(r.get("release_no") or "")))

    return {
        "item": item,
        "from_date": start,
        "to_date": end,
        "trace_window_days": EDGE_ITEM_MOVEMENT_DAYS,
        "api_max_days": EDGE_ITEM_MOVEMENT_DAYS,
        "bulk_export_allowed": False,
        "rows": rows,
        "served_by": "Edge",
    }


def routed_encoder_customer_history(
    customer: str,
    device_id: str,
    *,
    backend: str,
    edge_snapshot: Optional[Dict[str, Any]] = None,
    item: Optional[str] = None,
    plate_number: Optional[str] = None,
    os_no: Optional[str] = None,
    from_date: Optional[Any] = None,
    to_date: Optional[Any] = None,
    limit: int = 50,
    offset: int = 0,
    user: Optional[str] = None,
):
    if backend == "Primary":
        out = primary_encoder_customer_history(
            customer, device_id, item=item, plate_number=plate_number, os_no=os_no, from_date=from_date, to_date=to_date,
            limit=limit, offset=offset, user=user,
        )
        out["served_by"] = "Primary"
        return out
    if backend == "Edge" and edge_snapshot is not None:
        if str(plate_number or "").strip() or str(os_no or "").strip():
            out = _edge_customer_history(
                edge_snapshot, customer, device_id, item=item, from_date=from_date,
                to_date=to_date, limit=limit, offset=offset, user=user,
            )
            out["rows"] = []
            out["warning"] = "Plate Number / OS# search requires Primary ERP while the current Edge snapshot does not contain those fields."
            return out
        return _edge_customer_history(
            edge_snapshot, customer, device_id, item=item, from_date=from_date,
            to_date=to_date, limit=limit, offset=offset, user=user,
        )
    raise frappe.PermissionError("Read service unavailable.")


def routed_customer_open_receivables(
    customer: str,
    device_id: str,
    *,
    backend: str,
    edge_snapshot: Optional[Dict[str, Any]] = None,
    user: Optional[str] = None,
):
    if backend == "Primary":
        out = primary_customer_open_receivables(customer, device_id, user=user)
        out["served_by"] = "Primary"
        return out
    if backend == "Edge" and edge_snapshot is not None:
        return _edge_open_receivables(edge_snapshot, customer, device_id, user=user)
    raise frappe.PermissionError("Read service unavailable.")


def routed_cashier_sale_search(
    device_id: str,
    *,
    backend: str,
    edge_snapshot: Optional[Dict[str, Any]] = None,
    customer: Optional[str] = None,
    item: Optional[str] = None,
    sale_name: Optional[str] = None,
    limit: int = 50,
    user: Optional[str] = None,
):
    if backend == "Primary":
        out = primary_cashier_sale_search(
            device_id, customer=customer, item=item, sale_name=sale_name,
            limit=limit, user=user,
        )
        out["served_by"] = "Primary"
        return out
    if backend == "Edge" and edge_snapshot is not None:
        return _edge_cashier_sale_search(
            edge_snapshot, device_id, customer=customer, item=item,
            sale_name=sale_name, limit=limit, user=user,
        )
    raise frappe.PermissionError("Read service unavailable.")


def routed_cashier_sale_lookup(
    sale_name: str,
    device_id: str,
    *,
    backend: str,
    edge_snapshot: Optional[Dict[str, Any]] = None,
    user: Optional[str] = None,
):
    if backend == "Primary":
        out = primary_cashier_sale_lookup(sale_name, device_id, user=user)
        out["served_by"] = "Primary"
        return out
    if backend == "Edge" and edge_snapshot is not None:
        return _edge_cashier_sale_lookup(
            edge_snapshot, sale_name, device_id, user=user,
        )
    raise frappe.PermissionError("Read service unavailable.")


def routed_item_movement_history(
    item: str,
    device_id: str,
    *,
    backend: str,
    edge_snapshot: Optional[Dict[str, Any]] = None,
    from_date: Any,
    to_date: Optional[Any] = None,
    user: Optional[str] = None,
):
    if backend == "Primary":
        out = primary_item_movement_history(
            item, device_id, from_date=from_date, to_date=to_date, user=user,
        )
        out["served_by"] = "Primary"
        return out
    if backend == "Edge" and edge_snapshot is not None:
        return _edge_item_movement_history(
            edge_snapshot, item, device_id, from_date=from_date,
            to_date=to_date, user=user,
        )
    raise frappe.PermissionError("Read service unavailable.")


def simulate_failover(
    read_family: str,
    *,
    primary_available: bool,
    edge_available: bool,
) -> Dict[str, Any]:
    return route_read_source(
        read_family,
        primary_available=primary_available,
        edge_available=edge_available,
    )


def _public_routed_runtime(read_call, *args, **kwargs):
    backend = _runtime_backend()
    if backend == "Primary":
        return read_call(*args, backend="Primary", **kwargs)

    if backend == "Store Edge":
        from nkt_operations.nkt_store_operations.features.offline_edge.internal.edge_provider import (
            load_configured_edge_snapshot,
        )
        snapshot = load_configured_edge_snapshot()
        return read_call(
            *args,
            backend="Edge",
            edge_snapshot=snapshot,
            **kwargs,
        )

    raise frappe.PermissionError("Read service unavailable.")


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
    return _public_routed_runtime(
        routed_encoder_customer_history,
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
    return _public_routed_runtime(
        routed_customer_open_receivables,
        customer,
        device_id,
    )


@frappe.whitelist()
def search_cashier_sales(
    device_id: str,
    customer: Optional[str] = None,
    item: Optional[str] = None,
    sale_name: Optional[str] = None,
    limit: int = 50,
):
    return _public_routed_runtime(
        routed_cashier_sale_search,
        device_id,
        customer=customer,
        item=item,
        sale_name=sale_name,
        limit=limit,
    )


@frappe.whitelist()
def get_cashier_sale_lookup(sale_name: str, device_id: str):
    return _public_routed_runtime(
        routed_cashier_sale_lookup,
        sale_name,
        device_id,
    )


@frappe.whitelist()
def get_item_movement_history(
    item: str,
    device_id: str,
    from_date: str,
    to_date: Optional[str] = None,
):
    return _public_routed_runtime(
        routed_item_movement_history,
        item,
        device_id,
        from_date=from_date,
        to_date=to_date,
    )
