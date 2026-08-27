from __future__ import annotations

from typing import Any, Optional

import frappe
from frappe.utils import flt

FOUNDATION_VERSION = "C15C.10I-R4"
ACTIVE_RETURN_EXCHANGE_STATES = (
    "Pending Edge",
    "Awaiting Primary",
    "Primary Preserved",
    "Primary Materialized",
)


def _sum(query: str, args: tuple[Any, ...]) -> float:
    rows = frappe.db.sql(query, args)
    return flt(rows[0][0] if rows else 0)


def projected_customer_credit_amount(
    customer: str,
    *,
    exclude_event_uuid: Optional[str] = None,
) -> float:
    args: list[Any] = [customer]
    extra = ""
    if exclude_event_uuid:
        extra = " AND event_uuid != %s"
        args.append(exclude_event_uuid)
    placeholders = ",".join(["%s"] * len(ACTIVE_RETURN_EXCHANGE_STATES))
    args.extend(ACTIVE_RETURN_EXCHANGE_STATES)
    return _sum(
        f"""
        SELECT COALESCE(SUM(projected_customer_credit), 0)
        FROM `tabNKT Edge Return Exchange Projection`
        WHERE customer=%s
          AND side='Encoder'
          {extra}
          AND projection_state IN ({placeholders})
        """,
        tuple(args),
    )


def projected_account_adjustment_amount(
    old_customer_order: str,
    *,
    exclude_event_uuid: Optional[str] = None,
) -> float:
    args: list[Any] = [old_customer_order]
    extra = ""
    if exclude_event_uuid:
        extra = " AND event_uuid != %s"
        args.append(exclude_event_uuid)
    placeholders = ",".join(["%s"] * len(ACTIVE_RETURN_EXCHANGE_STATES))
    args.extend(ACTIVE_RETURN_EXCHANGE_STATES)
    return _sum(
        f"""
        SELECT COALESCE(SUM(projected_account_adjustment), 0)
        FROM `tabNKT Edge Return Exchange Projection`
        WHERE old_customer_order=%s
          AND side='Encoder'
          {extra}
          AND projection_state IN ({placeholders})
        """,
        tuple(args),
    )


def effective_customer_credit(
    canonical_available_credit: Any,
    customer: str,
    *,
    exclude_event_uuid: Optional[str] = None,
) -> float:
    return flt(canonical_available_credit) + projected_customer_credit_amount(
        customer,
        exclude_event_uuid=exclude_event_uuid,
    )


def effective_old_order_outstanding(
    canonical_outstanding: Any,
    old_customer_order: str,
    *,
    exclude_event_uuid: Optional[str] = None,
) -> float:
    return max(
        flt(canonical_outstanding)
        - projected_account_adjustment_amount(
            old_customer_order,
            exclude_event_uuid=exclude_event_uuid,
        ),
        0,
    )


def canonical_return_credit_available(customer: str) -> float:
    """
    Canonical Primary-synchronized Return Credit available to this customer.
    This intentionally excludes ordinary Payment Advances; only Return-origin
    Customer Credit is included.
    """
    customer = str(customer or "").strip()
    if not customer:
        raise frappe.ValidationError("Customer is required for Return Credit.")

    meta = frappe.get_meta("NKT Customer Advance")
    if not meta.has_field("custom_nkt_credit_origin"):
        return 0.0

    rows = frappe.db.sql(
        """
        SELECT COALESCE(SUM(available_advance_amount), 0)
        FROM `tabNKT Customer Advance`
        WHERE customer=%s
          AND docstatus=1
          AND advance_status='Available'
          AND custom_nkt_credit_origin='Return Credit'
        """,
        (customer,),
    )
    return flt(rows[0][0] if rows else 0)


def pending_return_credit_consumption_amount(
    customer: str,
    *,
    exclude_event_uuid: Optional[str] = None,
    locking_current_read: bool = False,
) -> float:
    customer = str(customer or "").strip()
    if not customer:
        return 0.0

    args: list[Any] = [customer]
    extra = ""
    if exclude_event_uuid:
        extra = " AND event_uuid != %s"
        args.append(exclude_event_uuid)
    placeholders = ",".join(["%s"] * len(ACTIVE_RETURN_EXCHANGE_STATES))
    args.extend(ACTIVE_RETURN_EXCHANGE_STATES)
    lock = " FOR UPDATE" if locking_current_read else ""

    rows = frappe.db.sql(
        f"""
        SELECT COALESCE(SUM(reserved_amount), 0)
        FROM `tabNKT Edge Return Credit Consumption Projection`
        WHERE customer=%s
          {extra}
          AND projection_state IN ({placeholders})
        {lock}
        """,
        tuple(args),
    )
    return flt(rows[0][0] if rows else 0)


def effective_return_credit_available(
    customer: str,
    *,
    exclude_consumption_event_uuid: Optional[str] = None,
    locking_current_read: bool = False,
) -> float:
    canonical = canonical_return_credit_available(customer)
    projected = projected_customer_credit_amount(customer)
    consumed = pending_return_credit_consumption_amount(
        customer,
        exclude_event_uuid=exclude_consumption_event_uuid,
        locking_current_read=locking_current_read,
    )
    return max(canonical + projected - consumed, 0)


def foundation_status():
    return {
        "foundation_version": FOUNDATION_VERSION,
        "encoder_customer_credit_counts_as_local_edge_credit": True,
        "projected_return_credit_can_be_consumed_by_offline_cashier_tender": True,
        "pending_return_credit_consumption_reduces_effective_credit": True,
        "encoder_account_adjustment_reduces_effective_old_order_outstanding": True,
        "cashier_side_does_not_create_local_customer_credit_projection": True,
        "canonical_customer_advance_written_at_edge": False,
        "canonical_receivable_mutated_at_edge": False,
        "canonical_return_account_adjustment_written_at_edge": False,
    }
