from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime
from typing import Any, Dict, Optional
from zoneinfo import ZoneInfo

import frappe
from frappe.utils import flt, getdate, now

from nkt_operations.nkt_store_operations.features.payments_accounts.card_surcharge import (
    apply_payment_row_card_fields,
    normalize_payment_method,
)

from nkt_operations.nkt_store_operations.features.offline_edge.policy import (
    device_policy_snapshot,
    event_family_policy,
    validate_business_time,
)
from nkt_operations.nkt_store_operations.features.returns.internal.return_exchange_offline_intent import (
    ACTION,
    FAMILY,
    canonical_return_exchange_intent_json,
    normalize_return_exchange_intent,
)
from nkt_operations.nkt_store_operations.features.offline_edge.safe_sync import (
    NKTIdempotencyConflict,
    begin_event,
    canonical_payload_hash,
    mark_edge_accepted,
)
from nkt_operations.nkt_store_operations.features.offline_edge.sync_transport import _runtime_role

FOUNDATION_VERSION = "C15C.10I-R4"
PH_TZ = ZoneInfo("Asia/Manila")
TOLERANCE = 0.000001

ACTIVE_STATES = (
    "Pending Edge",
    "Awaiting Primary",
    "Primary Preserved",
    "Primary Materialized",
)

ADMIN_ROLES = {"System Manager", "NKT OWNER", "NKT ADMINISTRATOR"}
SIDE_ROLE = {"Cashier": "NKT Cashier", "Encoder": "NKT Encoder"}


def _uuid(value: Any, label: str) -> str:
    try:
        return str(uuid.UUID(str(value)))
    except Exception as exc:
        raise frappe.ValidationError(f"{label} is invalid.") from exc


def _session_user(user: Optional[str] = None) -> str:
    user = user or frappe.session.user
    if not user or user == "Guest":
        raise frappe.PermissionError("Offline Return/Exchange unavailable.")
    return user


def _require_side_authority(side: str, user: str) -> None:
    if user == "Administrator":
        return
    roles = set(frappe.get_roles(user) or [])
    if not (roles & ADMIN_ROLES) and SIDE_ROLE.get(side) not in roles:
        raise frappe.PermissionError(
            f"Offline {side} Return/Exchange unavailable for this user."
        )


def _manila_datetime(value: Any, label: str) -> datetime:
    raw = str(value or "").strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        dt = value if isinstance(value, datetime) else datetime.fromisoformat(raw)
    except Exception as exc:
        raise frappe.ValidationError(f"{label} is invalid.") from exc
    if dt.tzinfo is None:
        return dt.replace(tzinfo=PH_TZ)
    return dt.astimezone(PH_TZ)


def _manila_sql_datetime(value: Any, label: str) -> str:
    return _manila_datetime(value, label).replace(tzinfo=None).strftime(
        "%Y-%m-%d %H:%M:%S"
    )


def _source_name(normalized: Dict[str, Any]) -> str:
    return (
        normalized["old_cashier_sale"]
        if normalized["side"] == "Cashier"
        else normalized["old_customer_order"]
    )


def _matching():
    from nkt_operations.nkt_store_operations.features.returns import matching as nkt_c7_return_exchange_matching
    return nkt_c7_return_exchange_matching


def _c7_payload(normalized: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "source_name": _source_name(normalized),
        "transaction_type": normalized["transaction_type"],
        "settlement_destination": normalized["settlement_destination"],
        "settlement_method": normalized["settlement_method"],
        "settlement_reference": normalized["settlement_reference"],
        "settlement_payments": [
            {
                "payment_method": row["payment_method"],
                "amount": row["amount"],
                "cash_tendered": row["cash_tendered"],
                "reference_number": row["reference_number"],
                "bank_or_provider": row["bank_or_provider"],
                "check_date": row["check_date"],
            }
            for row in normalized["settlement_payments"]
        ],
        "return_warehouse": normalized["return_warehouse"] or "",
        "returned_items": [
            {
                "item": row["item"],
                "quantity": row["quantity"],
                "original_source_warehouse": row["original_source_warehouse"],
                "classification": row["classification"],
                "actual_kg_returned": row["actual_kg_returned"],
                "return_value_treatment": row["return_value_treatment"],
                "manual_deduction": row["manual_deduction"],
            }
            for row in normalized["returned_items"]
        ],
        "new_items": [
            {
                "item": row["item"],
                "quantity": row["quantity"],
                "rate": row["rate"],
                "source_warehouse": row["source_warehouse"],
            }
            for row in normalized["new_items"]
        ],
    }


def _active_pending_qty(
    side: str,
    old_cashier_sale: str,
    old_customer_order: str,
    item_code: str,
    source_warehouse: str,
    *,
    exclude_event_uuid: Optional[str] = None,
) -> float:
    args: list[Any] = [
        side,
        old_cashier_sale,
        old_customer_order,
        item_code,
        source_warehouse,
    ]
    extra = ""
    if exclude_event_uuid:
        extra = " AND event_uuid != %s"
        args.append(exclude_event_uuid)
    placeholders = ",".join(["%s"] * len(ACTIVE_STATES))
    args.extend(ACTIVE_STATES)
    # Locking/current read is required here. Under InnoDB REPEATABLE READ,
    # a transaction may have established an older snapshot before it waits on
    # the C7 source-row lock. SELECT ... FOR UPDATE sees the latest committed
    # pending reservations after that wait.
    rows = frappe.db.sql(
        f"""
        SELECT returned_qty
        FROM `tabNKT Edge Return Exchange Stock Projection`
        WHERE side=%s
          AND old_cashier_sale=%s
          AND old_customer_order=%s
          AND item_code=%s
          AND original_source_warehouse=%s
          {extra}
          AND projection_state IN ({placeholders})
        FOR UPDATE
        """,
        tuple(args),
    )
    return sum(flt(row[0]) for row in rows)


def _active_pending_return_credit(
    side: str,
    old_cashier_sale: str,
    old_customer_order: str,
    *,
    exclude_event_uuid: Optional[str] = None,
) -> float:
    args: list[Any] = [side, old_cashier_sale, old_customer_order]
    extra = ""
    if exclude_event_uuid:
        extra = " AND event_uuid != %s"
        args.append(exclude_event_uuid)
    placeholders = ",".join(["%s"] * len(ACTIVE_STATES))
    args.extend(ACTIVE_STATES)
    rows = frappe.db.sql(
        f"""
        SELECT return_credit
        FROM `tabNKT Edge Return Exchange Projection`
        WHERE side=%s
          AND old_cashier_sale=%s
          AND old_customer_order=%s
          {extra}
          AND projection_state IN ({placeholders})
        FOR UPDATE
        """,
        tuple(args),
    )
    return sum(flt(row[0]) for row in rows)


def _active_pending_account_adjustment(
    old_customer_order: str,
    *,
    exclude_event_uuid: Optional[str] = None,
) -> float:
    args: list[Any] = [old_customer_order]
    extra = ""
    if exclude_event_uuid:
        extra = " AND event_uuid != %s"
        args.append(exclude_event_uuid)
    placeholders = ",".join(["%s"] * len(ACTIVE_STATES))
    args.extend(ACTIVE_STATES)
    rows = frappe.db.sql(
        f"""
        SELECT projected_account_adjustment
        FROM `tabNKT Edge Return Exchange Projection`
        WHERE old_customer_order=%s
          AND side='Encoder'
          {extra}
          AND projection_state IN ({placeholders})
        FOR UPDATE
        """,
        tuple(args),
    )
    return sum(flt(row[0]) for row in rows)


def _validate_lineage_and_pending_quantity(
    event_uuid: str,
    normalized: Dict[str, Any],
) -> Dict[str, Any]:
    matching = _matching()
    side = normalized["side"]
    source_name = _source_name(normalized)
    old_cash, old_order, detail = matching._source_pair(side, source_name)

    if old_cash != normalized["old_cashier_sale"]:
        raise frappe.ValidationError("OLD Cashier Sale lineage changed on Store Edge.")
    if old_order != normalized["old_customer_order"]:
        raise frappe.ValidationError("OLD Customer Order lineage changed on Store Edge.")
    if str(detail.get("customer") or "") != normalized["customer"]:
        raise frappe.ValidationError("OLD ORDER belongs to a different Customer.")
    if str(frappe.db.get_value("NKT Customer Order", old_order, "company") or "") != normalized["company"]:
        raise frappe.ValidationError("OLD ORDER belongs to a different Company.")
    if int(detail.get("generation") or 0) != int(normalized["source_generation"] or 0):
        raise frappe.ValidationError("OLD ORDER generation changed on Store Edge.")

    source_map = {
        (str(x.get("item") or ""), str(x.get("source_warehouse") or "")): x
        for x in (detail.get("items") or [])
    }

    requested = {}
    for row in normalized["returned_items"]:
        key = (row["item"], row["original_source_warehouse"])
        requested[key] = requested.get(key, 0.0) + flt(row["quantity"])

    for (item, warehouse), qty in requested.items():
        source = source_map.get((item, warehouse))
        if not source:
            same_item = [x for (it, _wh), x in source_map.items() if it == item]
            if len(same_item) == 1:
                source = same_item[0]
                warehouse = str(source.get("source_warehouse") or "")
        if not source:
            raise frappe.ValidationError(
                f"Returned item {item} is not part of the OLD ORDER."
            )

        legacy = matching._legacy_returned_qty(old_order, item, warehouse)
        canonical_prior = matching._declared_returned_qty(
            side, old_cash, old_order, item, warehouse
        )
        pending_prior = _active_pending_qty(
            side,
            old_cash,
            old_order,
            item,
            warehouse,
            exclude_event_uuid=event_uuid,
        )
        available = max(
            flt(source.get("original_qty")) - legacy - canonical_prior - pending_prior,
            0,
        )
        if qty > available + TOLERANCE:
            raise frappe.ValidationError(
                f"Offline Return Qty for {item} exceeds the effective remaining "
                f"returnable quantity {available} after pending Edge returns."
            )

    return detail


def compute_effective_financial_projection(
    *,
    return_credit: Any,
    new_order_value: Any,
    source_total: Any,
    source_money: Any,
    source_account: Any,
    canonical_prior_return_credit: Any,
    pending_prior_return_credit: Any,
    settlement_destination: str,
    canonical_receivable_outstanding: Any,
    pending_account_adjustment: Any,
) -> Dict[str, float]:
    credit = round(max(flt(return_credit), 0), 2)
    new_value = round(max(flt(new_order_value), 0), 2)
    total = max(flt(source_total), 0)
    money = max(flt(source_money), 0)
    account = max(flt(source_account), 0)

    basis_total = money + account
    if total <= TOLERANCE:
        raise frappe.ValidationError("OLD ORDER has no valid value basis.")
    if basis_total <= TOLERANCE:
        account = total
        basis_total = total
    if abs(basis_total - total) > TOLERANCE:
        account = max(total - money, 0)

    prior_credit = max(
        flt(canonical_prior_return_credit) + flt(pending_prior_return_credit),
        0,
    )
    prior_ratio = min(max(prior_credit / total, 0), 1)
    remaining_money = max(money * (1 - prior_ratio), 0)
    remaining_account = max(account * (1 - prior_ratio), 0)

    money_share = money / total if total else 0
    return_money = min(credit * money_share, remaining_money)
    return_account = min(max(credit - return_money, 0), remaining_account)
    missing = max(credit - return_money - return_account, 0)
    if missing > TOLERANCE:
        return_account += missing

    customer_pays = round(max(new_value - credit, 0), 2)
    difference_back = round(max(credit - new_value, 0), 2)

    refund_money = 0.0
    requested_adj = 0.0
    requested_credit = 0.0

    if customer_pays <= TOLERANCE and difference_back > TOLERANCE:
        if settlement_destination == "Refund Money":
            refund_money = round(min(difference_back, return_money), 2)
            remaining = round(difference_back - refund_money, 2)
            requested_adj = round(min(remaining, return_account), 2)
            requested_credit = round(remaining - requested_adj, 2)
        elif settlement_destination == "Account Adjustment":
            requested_adj = round(min(difference_back, return_account), 2)
            requested_credit = round(difference_back - requested_adj, 2)
        elif settlement_destination == "Customer Credit":
            requested_credit = difference_back
        else:
            raise frappe.ValidationError(
                "Choose Refund Money, Customer Credit, or Account Adjustment "
                "for the amount due back."
            )

    effective_outstanding = max(
        flt(canonical_receivable_outstanding) - flt(pending_account_adjustment),
        0,
    )
    actual_adj = round(min(requested_adj, effective_outstanding), 2)
    actual_credit = round(
        requested_credit + max(requested_adj - actual_adj, 0),
        2,
    )

    return {
        "return_credit": credit,
        "new_order_value": new_value,
        "customer_pays": customer_pays,
        "difference_back": difference_back,
        "return_money_basis": round(return_money, 2),
        "return_account_basis": round(return_account, 2),
        "refund_money": refund_money,
        "requested_account_adjustment": requested_adj,
        "projected_account_adjustment": actual_adj,
        "requested_customer_credit": requested_credit,
        "projected_customer_credit": actual_credit,
        "effective_old_order_outstanding_before_this_event": round(
            effective_outstanding, 2
        ),
    }


def _financial_projection(
    event_uuid: str,
    normalized: Dict[str, Any],
    detail: Dict[str, Any],
) -> Dict[str, float]:
    matching = _matching()
    preview = matching.preview_payload(
        normalized["side"].lower(),
        json.dumps(_c7_payload(normalized)),
    )

    canonical_prior = matching._prior_return_credit(
        normalized["side"],
        normalized["old_cashier_sale"],
        normalized["old_customer_order"],
    )
    pending_prior = _active_pending_return_credit(
        normalized["side"],
        normalized["old_cashier_sale"],
        normalized["old_customer_order"],
        exclude_event_uuid=event_uuid,
    )

    outstanding = flt(
        frappe.db.get_value(
            "NKT Customer Receivable",
            {
                "customer_order": normalized["old_customer_order"],
                "status": ["in", ["Open", "Partially Paid"]],
            },
            "outstanding_amount",
        )
        or 0
    )

    pending_adj = _active_pending_account_adjustment(
        normalized["old_customer_order"],
        exclude_event_uuid=event_uuid,
    )

    result = compute_effective_financial_projection(
        return_credit=preview["return_credit"],
        new_order_value=preview["new_order_value"],
        source_total=detail.get("total"),
        source_money=detail.get("money_basis"),
        source_account=detail.get("account_basis"),
        canonical_prior_return_credit=canonical_prior,
        pending_prior_return_credit=pending_prior,
        settlement_destination=normalized["settlement_destination"],
        canonical_receivable_outstanding=outstanding,
        pending_account_adjustment=pending_adj,
    )

    # Only Encoder owns locally usable Account/Credit effects.
    if normalized["side"] != "Encoder":
        result["projected_account_adjustment"] = 0.0
        result["projected_customer_credit"] = 0.0

    return result


def _parent_projection_key(event_uuid: str) -> str:
    return hashlib.sha256(
        f"{event_uuid}|return-exchange|summary".encode("utf-8")
    ).hexdigest()


def _stock_projection_key(event_uuid: str, line_no: int) -> str:
    return hashlib.sha256(
        f"{event_uuid}|return-exchange|stock|{int(line_no)}".encode("utf-8")
    ).hexdigest()


def _new_item_projection_key(event_uuid: str, line_no: int) -> str:
    return hashlib.sha256(
        f"{event_uuid}|return-exchange|new-item|{int(line_no)}".encode("utf-8")
    ).hexdigest()


def _cash_projection_key(event_uuid: str, line_no: int, movement_kind: str) -> str:
    return hashlib.sha256(
        f"{event_uuid}|return-exchange|cash|{int(line_no)}|{movement_kind}".encode("utf-8")
    ).hexdigest()


def _verify_or_insert_parent_projection(
    event_uuid: str,
    physical_at: str,
    normalized: Dict[str, Any],
    financial: Dict[str, float],
) -> None:
    expected = {
        "projection_key": _parent_projection_key(event_uuid),
        "event_uuid": event_uuid,
        "side": normalized["side"],
        "submit_request_id": normalized["submit_request_id"],
        "company": normalized["company"],
        "customer": normalized["customer"],
        "old_cashier_sale": normalized["old_cashier_sale"],
        "old_customer_order": normalized["old_customer_order"],
        "source_generation": normalized["source_generation"],
        "business_date": normalized["business_date"],
        "physical_entry_at": physical_at,
        "cashier_shift": normalized["cashier_shift"],
        "transaction_type": normalized["transaction_type"],
        "return_credit": financial["return_credit"],
        "new_order_value": financial["new_order_value"],
        "customer_pays": financial["customer_pays"],
        "refund_money": financial["refund_money"],
        "requested_account_adjustment": financial["requested_account_adjustment"],
        "projected_account_adjustment": financial["projected_account_adjustment"],
        "requested_customer_credit": financial["requested_customer_credit"],
        "projected_customer_credit": financial["projected_customer_credit"],
        "settlement_destination": normalized["settlement_destination"],
        "settlement_method": normalized["settlement_method"],
        "settlement_reference": normalized["settlement_reference"],
        "projected_cash_drawer_delta": 0.0,
        "replacement_new_sale_projected": 1 if normalized["side"] == "Cashier" and normalized["transaction_type"] == "Exchange" else 0,
        "replacement_new_order_projected": 1 if normalized["side"] == "Encoder" and normalized["transaction_type"] == "Exchange" else 0,
        "projection_state": "Pending Edge",
    }

    existing = frappe.db.exists("NKT Edge Return Exchange Projection", expected["projection_key"])
    if existing:
        got = frappe.get_doc("NKT Edge Return Exchange Projection", existing)
        compare_text = (
            "event_uuid","side","submit_request_id","company","customer",
            "old_cashier_sale","old_customer_order","business_date","cashier_shift",
            "transaction_type","settlement_destination","settlement_method","settlement_reference",
        )
        for field in compare_text:
            if str(got.get(field) or "") != str(expected[field] or ""):
                raise NKTIdempotencyConflict(
                    "Edge Return/Exchange projection conflicts with immutable Event UUID."
                )
        compare_num = (
            "source_generation","return_credit","new_order_value","customer_pays",
            "refund_money","requested_account_adjustment","projected_account_adjustment",
            "requested_customer_credit","projected_customer_credit","projected_cash_drawer_delta",
            "replacement_new_sale_projected","replacement_new_order_projected",
        )
        for field in compare_num:
            if abs(flt(got.get(field)) - flt(expected[field])) > 0.005:
                raise NKTIdempotencyConflict(
                    "Edge Return/Exchange financial projection conflicts with immutable Event UUID."
                )
        return

    frappe.get_doc(
        {"doctype": "NKT Edge Return Exchange Projection", **expected}
    ).insert(ignore_permissions=True)


def _verify_or_insert_stock_projections(
    event_uuid: str,
    physical_at: str,
    normalized: Dict[str, Any],
) -> int:
    expected = []
    for row in normalized["returned_items"]:
        local_saleable = (
            flt(row["quantity"])
            if normalized["side"] == "Encoder" and row["classification"] == "Saleable"
            else 0.0
        )
        expected.append({
            "projection_key": _stock_projection_key(event_uuid, row["line_no"]),
            "event_uuid": event_uuid,
            "side": normalized["side"],
            "line_no": row["line_no"],
            "old_cashier_sale": normalized["old_cashier_sale"],
            "old_customer_order": normalized["old_customer_order"],
            "item_code": row["item"],
            "original_source_warehouse": row["original_source_warehouse"],
            "return_warehouse": (
                normalized["return_warehouse"]
                if normalized["side"] == "Encoder"
                else None
            ),
            "classification": row["classification"],
            "returned_qty": row["quantity"],
            "local_saleable_qty": local_saleable,
            "business_date": normalized["business_date"],
            "physical_entry_at": physical_at,
            "projection_state": "Pending Edge",
        })

    existing = frappe.get_all(
        "NKT Edge Return Exchange Stock Projection",
        filters={"event_uuid": event_uuid},
        fields=[
            "projection_key","event_uuid","side","line_no",
            "old_cashier_sale","old_customer_order","item_code",
            "original_source_warehouse","return_warehouse","classification",
            "returned_qty","local_saleable_qty","business_date","physical_entry_at",
            "projection_state",
        ],
        order_by="line_no asc",
        limit_page_length=500,
    )
    if existing:
        if len(existing) != len(expected):
            raise NKTIdempotencyConflict(
                "Edge Return/Exchange stock projection count conflicts with immutable Event UUID."
            )
        for got, want in zip(existing, expected):
            for field in (
                "projection_key","event_uuid","side","old_cashier_sale",
                "old_customer_order","item_code","original_source_warehouse",
                "return_warehouse","classification","business_date",
            ):
                if str(got.get(field) or "") != str(want[field] or ""):
                    raise NKTIdempotencyConflict(
                        "Edge Return/Exchange stock projection conflicts with immutable Event UUID."
                    )
            for field in ("returned_qty","local_saleable_qty"):
                if abs(flt(got.get(field)) - flt(want[field])) > TOLERANCE:
                    raise NKTIdempotencyConflict(
                        "Edge Return/Exchange quantity projection conflicts with immutable Event UUID."
                    )
        return len(existing)

    for row in expected:
        frappe.get_doc(
            {"doctype": "NKT Edge Return Exchange Stock Projection", **row}
        ).insert(ignore_permissions=True)
    return len(expected)


def _verify_or_insert_new_item_projections(
    event_uuid: str,
    physical_at: str,
    normalized: Dict[str, Any],
) -> int:
    expected = []
    for row in normalized["new_items"]:
        source_warehouse = row["source_warehouse"] if normalized["side"] == "Encoder" else None
        local_reserved = flt(row["quantity"]) if normalized["side"] == "Encoder" else 0.0
        expected.append({
            "projection_key": _new_item_projection_key(event_uuid, row["line_no"]),
            "event_uuid": event_uuid,
            "side": normalized["side"],
            "line_no": row["line_no"],
            "old_cashier_sale": normalized["old_cashier_sale"],
            "old_customer_order": normalized["old_customer_order"],
            "item_code": row["item"],
            "quantity": row["quantity"],
            "rate": row["rate"],
            "source_warehouse": source_warehouse,
            "local_reserved_qty": local_reserved,
            "physical_entry_at": physical_at,
            "projection_state": "Pending Edge",
        })

    existing = frappe.get_all(
        "NKT Edge Return Exchange New Item Projection",
        filters={"event_uuid": event_uuid},
        fields=[
            "projection_key","event_uuid","side","line_no","old_cashier_sale",
            "old_customer_order","item_code","quantity","rate","source_warehouse",
            "local_reserved_qty","projection_state",
        ],
        order_by="line_no asc",
        limit_page_length=500,
    )
    if existing:
        if len(existing) != len(expected):
            raise NKTIdempotencyConflict(
                "Return/Exchange replacement-item projection count conflicts with immutable Event UUID."
            )
        for got, want in zip(existing, expected):
            for field in (
                "projection_key","event_uuid","side","old_cashier_sale",
                "old_customer_order","item_code","source_warehouse",
            ):
                if str(got.get(field) or "") != str(want[field] or ""):
                    raise NKTIdempotencyConflict(
                        "Return/Exchange replacement-item projection conflicts with immutable Event UUID."
                    )
            for field in ("quantity","rate","local_reserved_qty"):
                if abs(flt(got.get(field)) - flt(want[field])) > TOLERANCE:
                    raise NKTIdempotencyConflict(
                        "Return/Exchange replacement-item value conflicts with immutable Event UUID."
                    )
        return len(existing)

    for row in expected:
        frappe.get_doc(
            {"doctype": "NKT Edge Return Exchange New Item Projection", **row}
        ).insert(ignore_permissions=True)
    return len(expected)


def _cash_projection_rows(
    event_uuid: str,
    physical_at: str,
    normalized: Dict[str, Any],
    financial: Dict[str, float],
) -> list[Dict[str, Any]]:
    if normalized["side"] != "Cashier":
        return []

    rows = []
    for idx, payment in enumerate(normalized["settlement_payments"], start=1):
        method = normalize_payment_method(payment["payment_method"])
        derived = {"payment_method": method, "amount": flt(payment["amount"])}
        apply_payment_row_card_fields(derived)
        surcharge = flt(derived.get("card_surcharge"))
        collected = flt(derived.get("collected_amount"))
        if method == "Account":
            collected = 0.0
            surcharge = 0.0
        drawer_delta = flt(payment["amount"]) if method == "Cash" else 0.0
        rows.append({
            "projection_key": _cash_projection_key(
                event_uuid, idx, "Exchange Difference Collected"
            ),
            "event_uuid": event_uuid,
            "line_no": idx,
            "company": normalized["company"],
            "customer": normalized["customer"],
            "cashier_shift": normalized["cashier_shift"],
            "physical_entry_at": physical_at,
            "movement_kind": "Exchange Difference Collected",
            "direction": "In",
            "payment_method": method,
            "settlement_amount": flt(payment["amount"]),
            "card_surcharge": surcharge,
            "collected_amount": collected,
            "cash_drawer_delta": drawer_delta,
            "reference_number": payment.get("reference_number") or "",
            "projection_state": "Pending Edge",
        })

    refund = flt(financial.get("refund_money"))
    if refund > TOLERANCE:
        method = normalize_payment_method(normalized.get("settlement_method"))
        drawer_delta = -refund if method == "Cash" else 0.0
        rows.append({
            "projection_key": _cash_projection_key(
                event_uuid, 1000, "Return/Exchange Refund"
            ),
            "event_uuid": event_uuid,
            "line_no": 1000,
            "company": normalized["company"],
            "customer": normalized["customer"],
            "cashier_shift": normalized["cashier_shift"],
            "physical_entry_at": physical_at,
            "movement_kind": (
                "Customer Return Refund"
                if normalized["transaction_type"] == "Return"
                else "Exchange Difference Refunded"
            ),
            "direction": "Out",
            "payment_method": method,
            "settlement_amount": refund,
            "card_surcharge": 0.0,
            "collected_amount": refund,
            "cash_drawer_delta": drawer_delta,
            "reference_number": normalized.get("settlement_reference") or "",
            "projection_state": "Pending Edge",
        })
    return rows


def _verify_or_insert_cash_projections(
    event_uuid: str,
    physical_at: str,
    normalized: Dict[str, Any],
    financial: Dict[str, float],
) -> tuple[int, float]:
    expected = _cash_projection_rows(event_uuid, physical_at, normalized, financial)
    existing = frappe.get_all(
        "NKT Edge Return Exchange Cash Projection",
        filters={"event_uuid": event_uuid},
        fields=[
            "projection_key","event_uuid","line_no","company","customer","cashier_shift",
            "movement_kind","direction","payment_method","settlement_amount",
            "card_surcharge","collected_amount","cash_drawer_delta","reference_number",
            "projection_state",
        ],
        order_by="line_no asc",
        limit_page_length=500,
    )
    if existing:
        if len(existing) != len(expected):
            raise NKTIdempotencyConflict(
                "Return/Exchange cash projection count conflicts with immutable Event UUID."
            )
        for got, want in zip(existing, expected):
            for field in (
                "projection_key","event_uuid","company","customer","cashier_shift",
                "movement_kind","direction","payment_method","reference_number",
            ):
                if str(got.get(field) or "") != str(want[field] or ""):
                    raise NKTIdempotencyConflict(
                        "Return/Exchange cash projection conflicts with immutable Event UUID."
                    )
            for field in (
                "settlement_amount","card_surcharge","collected_amount","cash_drawer_delta"
            ):
                if abs(flt(got.get(field)) - flt(want[field])) > TOLERANCE:
                    raise NKTIdempotencyConflict(
                        "Return/Exchange cash projection amount conflicts with immutable Event UUID."
                    )
        return len(existing), sum(flt(x.get("cash_drawer_delta")) for x in existing)

    for row in expected:
        frappe.get_doc(
            {"doctype": "NKT Edge Return Exchange Cash Projection", **row}
        ).insert(ignore_permissions=True)
    return len(expected), sum(flt(x["cash_drawer_delta"]) for x in expected)


def _is_retryable_return_exchange_concurrency_error(exc: Exception) -> bool:
    """Return True only for retryable MariaDB/Frappe concurrency failures."""
    type_name = type(exc).__name__.lower()
    message = str(exc).lower()
    return (
        "querydeadlock" in type_name
        or "deadlock" in type_name
        or "record has changed since last read" in message
        or "(1020," in message
        or "(1205," in message
        or "(1213," in message
        or "lock wait timeout" in message
        or "deadlock found" in message
    )


def accept_return_exchange_intent_at_edge_with_retry(
    event_uuid: str,
    device_id: str,
    business_date: Any,
    settled_at: Any,
    payload: Dict[str, Any],
    *,
    user: Optional[str] = None,
    max_attempts: int = 3,
) -> Dict[str, Any]:
    """
    Retry wrapper for one Store-Edge Return/Exchange request only.

    Retryable DB concurrency failures roll back the failed single-operation
    request transaction and retry from a fresh transaction snapshot. Internal
    callers that combine unrelated writes in one transaction should use the
    low-level function and own their transaction/retry policy themselves.
    """
    attempts = max(int(max_attempts or 1), 1)
    last_exc: Optional[Exception] = None

    for attempt in range(1, attempts + 1):
        try:
            result = accept_return_exchange_intent_at_edge(
                event_uuid,
                device_id,
                business_date,
                settled_at,
                payload,
                user=user,
            )
            result["concurrency_retry_attempt"] = attempt
            result["concurrency_retry_used"] = attempt > 1
            return result
        except Exception as exc:
            last_exc = exc
            if (
                attempt >= attempts
                or not _is_retryable_return_exchange_concurrency_error(exc)
            ):
                raise
            frappe.db.rollback()

    if last_exc is not None:
        raise last_exc
    raise frappe.ValidationError(
        "Return/Exchange concurrency retry failed unexpectedly."
    )


def accept_return_exchange_intent_at_edge(
    event_uuid: str,
    device_id: str,
    business_date: Any,
    settled_at: Any,
    payload: Dict[str, Any],
    *,
    user: Optional[str] = None,
) -> Dict[str, Any]:
    if _runtime_role() != "Store Edge":
        raise frappe.PermissionError("Offline Return/Exchange unavailable.")

    event_uuid = _uuid(event_uuid, "Return/Exchange Event UUID")
    user = _session_user(user)
    normalized = normalize_return_exchange_intent(payload)
    _require_side_authority(normalized["side"], user)

    device = device_policy_snapshot(
        device_id,
        user=user,
        requested_context="NKT Retail",
    )
    if device.get("ui_mode") != "normal":
        raise frappe.PermissionError("Offline Return/Exchange unavailable.")

    if event_family_policy(FAMILY).get("offline_write_allowed") is not True:
        raise frappe.PermissionError("Offline Return/Exchange unavailable.")

    business = validate_business_time(business_date, settled_at)
    if normalized["business_date"] != business["business_date"]:
        raise frappe.ValidationError(
            "Return/Exchange Business Date must equal the immutable Asia/Manila physical event date."
        )
    entry_dt = _manila_datetime(
        normalized["entry_datetime"],
        "Return/Exchange entry time",
    )
    if entry_dt.date().isoformat() != business["business_date"]:
        raise frappe.ValidationError(
            "Return/Exchange entry time must match the immutable physical event date."
        )
    if normalized["entry_user"] != user:
        raise frappe.ValidationError(
            "Return/Exchange Entered By must match the Store-Edge session user."
        )

    # Reuse the accepted C7 same-lineage atomicity guard. This serializes
    # concurrent Return/Exchange submissions for the same OLD Cashier Sale /
    # OLD Customer Order before remaining quantity and value basis are read.
    # Store Edge additionally uses locking/current reads for its pending
    # projection reservations so a transaction that waited here cannot continue
    # from a stale REPEATABLE-READ snapshot.
    matching = _matching()
    matching._lock_submission_source(
        normalized["old_cashier_sale"],
        normalized["old_customer_order"],
    )

    detail = _validate_lineage_and_pending_quantity(
        event_uuid,
        normalized,
    )
    financial = _financial_projection(
        event_uuid,
        normalized,
        detail,
    )

    digest = canonical_payload_hash(normalized)
    settled_sql = _manila_sql_datetime(
        business["settled_at_manila"],
        "Business / Settled Time",
    )
    envelope = {
        "event_uuid": event_uuid,
        "event_family": FAMILY,
        "event_action": ACTION,
        "operational_context": device.get("operational_context") or "NKT Retail",
        "origin_device": device_id,
        "origin_user": user,
        "business_date": business["business_date"],
        "settled_at": settled_sql,
        "client_created_at": _manila_sql_datetime(
            normalized["entry_datetime"],
            "Return/Exchange entry time",
        ),
        "payload_sha256": digest,
    }

    event, replay = begin_event(envelope)
    canonical_json = canonical_return_exchange_intent_json(normalized)

    pending_name = frappe.db.exists("NKT Sync Pending Payload", event.event_uuid)
    if pending_name:
        pending = frappe.get_doc("NKT Sync Pending Payload", pending_name)
        if (
            pending.event_family != FAMILY
            or pending.payload_sha256 != digest
            or pending.payload_json != canonical_json
        ):
            raise NKTIdempotencyConflict(
                "Pending Return/Exchange payload conflicts with immutable Event UUID."
            )
        frappe.db.set_value(
            "NKT Sync Pending Payload",
            pending.name,
            {
                "attempt_count": int(pending.attempt_count or 0) + 1,
                "last_attempt_at": now(),
            },
            update_modified=False,
        )
    elif event.sync_state not in ("Committed at Primary", "Conflict", "Failed"):
        if replay:
            raise frappe.ValidationError(
                "Return/Exchange Event exists but durable pending payload is unavailable."
            )
        frappe.get_doc({
            "doctype": "NKT Sync Pending Payload",
            "event_uuid": event.event_uuid,
            "event_family": FAMILY,
            "payload_sha256": digest,
            "payload_json": canonical_json,
            "queue_state": "Accepted at Edge",
            "edge_accepted_at": now(),
            "attempt_count": 1,
            "last_attempt_at": now(),
        }).insert(ignore_permissions=True)

    _verify_or_insert_parent_projection(
        event.event_uuid,
        settled_sql,
        normalized,
        financial,
    )
    stock_rows = _verify_or_insert_stock_projections(
        event.event_uuid,
        settled_sql,
        normalized,
    )
    new_item_rows = _verify_or_insert_new_item_projections(
        event.event_uuid,
        settled_sql,
        normalized,
    )
    cash_rows, cash_drawer_delta = _verify_or_insert_cash_projections(
        event.event_uuid,
        settled_sql,
        normalized,
        financial,
    )
    frappe.db.set_value(
        "NKT Edge Return Exchange Projection",
        _parent_projection_key(event.event_uuid),
        "projected_cash_drawer_delta",
        cash_drawer_delta,
        update_modified=False,
    )

    if event.sync_state not in ("Committed at Primary", "Conflict", "Failed"):
        mark_edge_accepted(event.event_uuid)
        event.reload()

    return {
        "event_uuid": event.event_uuid,
        "event_family": FAMILY,
        "side": normalized["side"],
        "sync_state": event.sync_state,
        "durable_ack": True,
        "replay": bool(replay),
        "payload_sha256": digest,
        "physical_business_date": business["business_date"],
        "physical_entry_at": settled_sql,
        "old_cashier_sale": normalized["old_cashier_sale"],
        "old_customer_order": normalized["old_customer_order"],
        "return_credit": financial["return_credit"],
        "new_order_value": financial["new_order_value"],
        "customer_pays": financial["customer_pays"],
        "refund_money_not_yet_locally_posted": financial["refund_money"],
        "projected_account_adjustment": financial["projected_account_adjustment"],
        "projected_customer_credit": financial["projected_customer_credit"],
        "stock_projection_rows": stock_rows,
        "replacement_new_item_projection_rows": new_item_rows,
        "cash_projection_rows": cash_rows,
        "projected_cash_drawer_delta": cash_drawer_delta,
        "saleable_return_stock_locally_available": normalized["side"] == "Encoder",
        "customer_credit_account_adjustment_locally_projected": normalized["side"] == "Encoder",
        "cashier_money_projection_enabled": normalized["side"] == "Cashier",
        "replacement_new_sale_projection_enabled": normalized["side"] == "Cashier" and normalized["transaction_type"] == "Exchange",
        "replacement_new_order_projection_enabled": normalized["side"] == "Encoder" and normalized["transaction_type"] == "Exchange",
        "canonical_return_exchange_declaration_created": False,
        "canonical_stock_entry_created": False,
        "canonical_cashier_movement_created": False,
        "canonical_customer_advance_created": False,
        "canonical_account_adjustment_created": False,
        "controlled_reversal_offline_enabled": False,
        "primary_preservation_required": True,
    }


@frappe.whitelist()
def submit_return_exchange_intent(
    event_uuid: str,
    device_id: str,
    business_date: str,
    settled_at: str,
    payload: Any,
):
    if isinstance(payload, str):
        payload = frappe.parse_json(payload)
    return accept_return_exchange_intent_at_edge_with_retry(
        event_uuid,
        device_id,
        business_date,
        settled_at,
        payload,
        user=frappe.session.user,
        max_attempts=3,
    )


def foundation_status() -> Dict[str, Any]:
    return {
        "foundation_version": FOUNDATION_VERSION,
        "edge_acceptance_enabled": True,
        "saleable_return_local_stock_projection_enabled": True,
        "damaged_fraction_rejected_normal_saleable_stock": False,
        "encoder_customer_credit_local_projection_enabled": True,
        "encoder_account_adjustment_local_projection_enabled": True,
        "pending_offline_return_quantity_reserved": True,
        "pending_offline_return_value_basis_reserved": True,
        "same_lineage_reservation_lock_enabled": True,
        "same_lineage_reservation_lock_reuses_accepted_c7_lock": True,
        "pending_reservation_reads_are_locking_current_reads": True,
        "pending_account_adjustment_read_is_locking_current_read": True,
        "retryable_database_concurrency_is_retried_at_request_boundary": True,
        "concurrency_retry_max_attempts": 3,
        "cashier_money_projection_enabled": True,
        "replacement_new_sale_projection_enabled": True,
        "replacement_new_order_projection_enabled": True,
        "canonical_return_exchange_posting_at_edge": False,
        "controlled_reversal_offline_enabled": False,
        "matching_is_operational_gate": False,
    }


def installation_probe() -> Dict[str, Any]:
    parent = frappe.get_meta("NKT Edge Return Exchange Projection")
    stock = frappe.get_meta("NKT Edge Return Exchange Stock Projection")
    cash = frappe.get_meta("NKT Edge Return Exchange Cash Projection")
    new_item = frappe.get_meta("NKT Edge Return Exchange New Item Projection")
    required_states = {
        "Pending Edge","Awaiting Primary","Primary Preserved",
        "Primary Materialized","Finalized","Conflict",
    }
    parent_states = {
        x.strip() for x in str(parent.get_field("projection_state").options or "").splitlines()
        if x.strip()
    }
    stock_states = {
        x.strip() for x in str(stock.get_field("projection_state").options or "").splitlines()
        if x.strip()
    }
    cash_states = {
        x.strip() for x in str(cash.get_field("projection_state").options or "").splitlines()
        if x.strip()
    }
    new_item_states = {
        x.strip() for x in str(new_item.get_field("projection_state").options or "").splitlines()
        if x.strip()
    }
    return {
        "parent_projection_doctype_present": parent.name == "NKT Edge Return Exchange Projection",
        "stock_projection_doctype_present": stock.name == "NKT Edge Return Exchange Stock Projection",
        "parent_projection_states_present": required_states.issubset(parent_states),
        "stock_projection_states_present": required_states.issubset(stock_states),
        "cash_projection_doctype_present": cash.name == "NKT Edge Return Exchange Cash Projection",
        "new_item_projection_doctype_present": new_item.name == "NKT Edge Return Exchange New Item Projection",
        "cash_projection_states_present": required_states.issubset(cash_states),
        "new_item_projection_states_present": required_states.issubset(new_item_states),
        "technical_projection_direct_frontline_write_enabled": False,
    }
