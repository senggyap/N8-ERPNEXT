from __future__ import annotations

from typing import Any, Optional

import frappe
from frappe.utils import flt

FOUNDATION_VERSION = "C15C.10I-R8"
ACTIVE_STATES = (
    "Pending Edge",
    "Awaiting Primary",
    "Primary Preserved",
    "Primary Materialized",
)


def projected_cash_drawer_delta(
    cashier_shift: str,
    *,
    exclude_event_uuid: Optional[str] = None,
) -> float:
    args: list[Any] = [cashier_shift]
    extra = ""
    if exclude_event_uuid:
        extra = " AND event_uuid != %s"
        args.append(exclude_event_uuid)
    placeholders = ",".join(["%s"] * len(ACTIVE_STATES))
    args.extend(ACTIVE_STATES)
    rows = frappe.db.sql(
        f"""
        SELECT COALESCE(SUM(cash_drawer_delta), 0)
        FROM `tabNKT Edge Return Exchange Cash Projection`
        WHERE cashier_shift=%s
          {extra}
          AND projection_state IN ({placeholders})
        """,
        tuple(args),
    )
    return flt(rows[0][0] if rows else 0)


def effective_cash_drawer_amount(
    canonical_drawer_amount: Any,
    cashier_shift: str,
) -> float:
    return flt(canonical_drawer_amount) + projected_cash_drawer_delta(cashier_shift)


def foundation_status():
    return {
        "foundation_version": FOUNDATION_VERSION,
        "cash_return_exchange_effects_are_local_projections": True,
        "cash_payment_increases_effective_drawer": True,
        "cash_refund_reduces_effective_drawer": True,
        "noncash_methods_do_not_change_cash_drawer": True,
        "card_surcharge_is_projected_on_card_collections": True,
        "maya_has_no_card_surcharge": True,
        "canonical_cashier_movement_written_at_edge": False,
    }
