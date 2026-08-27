from __future__ import annotations

from typing import Any, Optional

import frappe
from frappe.utils import flt

FOUNDATION_VERSION = "C15C.10H-R4"
TOLERANCE = 0.000001

ACTIVE_SUPPLIER_RECEIVING_STATES = (
    "Pending Edge",
    "Awaiting Primary",
    "Primary Preserved",
    "Primary Stock Materialized",
)
ACTIVE_WAREHOUSE_RELEASE_STATES = (
    "Pending Edge",
    "Awaiting Primary",
    "Primary Preserved",
    "Primary Stock Materialized",
)
ACTIVE_TRANSFER_DISPATCH_STATES = (
    "Pending Edge",
    "Awaiting Primary",
    "Primary Preserved",
    "Primary Dispatch Materialized",
)
ACTIVE_RETURN_EXCHANGE_STATES = (
    "Pending Edge",
    "Awaiting Primary",
    "Primary Preserved",
    "Primary Materialized",
)


def _sum_sql(query: str, args: tuple[Any, ...]) -> float:
    rows = frappe.db.sql(query, args)
    return flt(rows[0][0] if rows else 0)


def projected_supplier_accepted_qty(
    item_code: str,
    warehouse: str,
    *,
    exclude_event_uuid: Optional[str] = None,
) -> float:
    args: list[Any] = [item_code, warehouse]
    extra = ""
    if exclude_event_uuid:
        extra = " AND event_uuid != %s"
        args.append(exclude_event_uuid)
    placeholders = ",".join(["%s"] * len(ACTIVE_SUPPLIER_RECEIVING_STATES))
    args.extend(ACTIVE_SUPPLIER_RECEIVING_STATES)
    return _sum_sql(
        f"""
        SELECT COALESCE(SUM(accepted_qty), 0)
        FROM `tabNKT Edge Supplier Receiving Projection`
        WHERE item_code=%s
          AND warehouse=%s
          {extra}
          AND projection_state IN ({placeholders})
        """,
        tuple(args),
    )


def projected_return_saleable_qty(
    item_code: str,
    warehouse: str,
    *,
    exclude_event_uuid: Optional[str] = None,
) -> float:
    args: list[Any] = [item_code, warehouse]
    extra = ""
    if exclude_event_uuid:
        extra = " AND event_uuid != %s"
        args.append(exclude_event_uuid)
    placeholders = ",".join(["%s"] * len(ACTIVE_RETURN_EXCHANGE_STATES))
    args.extend(ACTIVE_RETURN_EXCHANGE_STATES)
    return _sum_sql(
        f"""
        SELECT COALESCE(SUM(local_saleable_qty), 0)
        FROM `tabNKT Edge Return Exchange Stock Projection`
        WHERE item_code=%s
          AND return_warehouse=%s
          AND side='Encoder'
          AND classification='Saleable'
          {extra}
          AND projection_state IN ({placeholders})
        """,
        tuple(args),
    )


def projected_return_exchange_new_order_qty(
    item_code: str,
    warehouse: str,
    *,
    exclude_event_uuid: Optional[str] = None,
) -> float:
    args: list[Any] = [item_code, warehouse]
    extra = ""
    if exclude_event_uuid:
        extra = " AND event_uuid != %s"
        args.append(exclude_event_uuid)
    placeholders = ",".join(["%s"] * len(ACTIVE_RETURN_EXCHANGE_STATES))
    args.extend(ACTIVE_RETURN_EXCHANGE_STATES)
    return _sum_sql(
        f"""
        SELECT COALESCE(SUM(local_reserved_qty), 0)
        FROM `tabNKT Edge Return Exchange New Item Projection`
        WHERE item_code=%s
          AND source_warehouse=%s
          AND side='Encoder'
          {extra}
          AND projection_state IN ({placeholders})
        """,
        tuple(args),
    )


def projected_warehouse_release_qty(
    item_code: str,
    warehouse: str,
    *,
    exclude_event_uuid: Optional[str] = None,
) -> float:
    args: list[Any] = [item_code, warehouse]
    extra = ""
    if exclude_event_uuid:
        extra = " AND event_uuid != %s"
        args.append(exclude_event_uuid)
    placeholders = ",".join(["%s"] * len(ACTIVE_WAREHOUSE_RELEASE_STATES))
    args.extend(ACTIVE_WAREHOUSE_RELEASE_STATES)
    return _sum_sql(
        f"""
        SELECT COALESCE(SUM(released_qty), 0)
        FROM `tabNKT Edge Warehouse Release Projection`
        WHERE item_code=%s
          AND warehouse=%s
          {extra}
          AND projection_state IN ({placeholders})
        """,
        tuple(args),
    )


def projected_transfer_dispatch_qty(
    item_code: str,
    warehouse: str,
    *,
    exclude_event_uuid: Optional[str] = None,
) -> float:
    args: list[Any] = [item_code, warehouse]
    extra = ""
    if exclude_event_uuid:
        extra = " AND event_uuid != %s"
        args.append(exclude_event_uuid)
    placeholders = ",".join(["%s"] * len(ACTIVE_TRANSFER_DISPATCH_STATES))
    args.extend(ACTIVE_TRANSFER_DISPATCH_STATES)
    return _sum_sql(
        f"""
        SELECT COALESCE(SUM(dispatched_qty), 0)
        FROM `tabNKT Edge Warehouse Transfer Projection`
        WHERE item_code=%s
          AND source_warehouse=%s
          AND projection_action='Source Dispatch'
          {extra}
          AND projection_state IN ({placeholders})
        """,
        tuple(args),
    )


def compute_effective_edge_stock_qty(
    base_actual_qty: Any,
    *,
    supplier_inbound_qty: Any = 0,
    return_saleable_inbound_qty: Any = 0,
    return_exchange_new_order_outbound_qty: Any = 0,
    warehouse_release_outbound_qty: Any = 0,
    transfer_dispatch_outbound_qty: Any = 0,
) -> float:
    return (
        flt(base_actual_qty)
        + flt(supplier_inbound_qty)
        + flt(return_saleable_inbound_qty)
        - flt(return_exchange_new_order_outbound_qty)
        - flt(warehouse_release_outbound_qty)
        - flt(transfer_dispatch_outbound_qty)
    )


def effective_edge_stock_qty(
    base_actual_qty: Any,
    item_code: str,
    warehouse: str,
    *,
    exclude_supplier_event_uuid: Optional[str] = None,
    exclude_return_exchange_new_order_event_uuid: Optional[str] = None,
    exclude_release_event_uuid: Optional[str] = None,
    exclude_transfer_dispatch_event_uuid: Optional[str] = None,
) -> float:
    return compute_effective_edge_stock_qty(
        base_actual_qty,
        supplier_inbound_qty=projected_supplier_accepted_qty(
            item_code,
            warehouse,
            exclude_event_uuid=exclude_supplier_event_uuid,
        ),
        return_saleable_inbound_qty=projected_return_saleable_qty(
            item_code,
            warehouse,
        ),
        return_exchange_new_order_outbound_qty=projected_return_exchange_new_order_qty(
            item_code,
            warehouse,
            exclude_event_uuid=exclude_return_exchange_new_order_event_uuid,
        ),
        warehouse_release_outbound_qty=projected_warehouse_release_qty(
            item_code,
            warehouse,
            exclude_event_uuid=exclude_release_event_uuid,
        ),
        transfer_dispatch_outbound_qty=projected_transfer_dispatch_qty(
            item_code,
            warehouse,
            exclude_event_uuid=exclude_transfer_dispatch_event_uuid,
        ),
    )


def foundation_status():
    return {
        "foundation_version": FOUNDATION_VERSION,
        "accepted_supplier_goods_count_as_local_edge_stock": True,
        "saleable_customer_returns_count_as_local_edge_stock": True,
        "encoder_exchange_new_order_reserves_local_edge_stock": True,
        "cashier_exchange_new_sale_does_not_double_reserve_stock": True,
        "damaged_fraction_rejected_returns_count_as_normal_saleable_stock": False,
        "damaged_problem_goods_count_as_local_edge_stock": False,
        "warehouse_release_transfer_dispatch_and_returns_share_one_effective_stock_view": True,
        "canonical_erpnext_stock_ledger_written_at_edge": False,
        "canonical_purchase_receipt_written_at_edge": False,
    }
