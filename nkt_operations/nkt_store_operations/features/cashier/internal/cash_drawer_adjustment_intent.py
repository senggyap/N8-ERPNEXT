from __future__ import annotations

import json
import math
import uuid
from datetime import datetime
from typing import Any, Dict, Optional
from zoneinfo import ZoneInfo

import frappe
from frappe.utils import cint, flt, now

from nkt_operations.nkt_store_operations.features.cashier.internal.cashier_shift_alias import edge_alias_expected_cash, is_edge_shift_reference, validate_edge_shift_for_money
from nkt_operations.nkt_store_operations.features.offline_edge.policy import (
    device_policy_snapshot,
    event_family_policy,
    validate_business_time,
)
from nkt_operations.nkt_store_operations.features.offline_edge.safe_sync import (
    NKTIdempotencyConflict,
    begin_event,
    canonical_payload_hash,
    mark_edge_accepted,
)

FOUNDATION_VERSION = "C15C.10F-R3"
PH_TZ = ZoneInfo("Asia/Manila")

CASH_DRAWER_ADJUSTMENT_INTENT_FAMILY = "NKT Cash Drawer Adjustment Intent"
CASH_DRAWER_ADJUSTMENT_INTENT_ACTION = "Record Cash Drawer Adjustment"

CASHIER_ROLES = {
    "NKT Cashier",
    "NKT OWNER",
    "NKT ADMINISTRATOR",
    "System Manager",
}
PRIVILEGED_ROLES = {"NKT OWNER", "NKT ADMINISTRATOR", "System Manager"}

ADJUSTMENT_MAP = {
    "Petty Cash Release": "Out",
    "Petty Cash Return": "In",
    "Cash Drop": "Out",
    "Advance / Mid-Shift Deposit": "Out",
    "Other Cash In": "In",
    "Other Cash Out": "Out",
}

# Advance Deposit Reversal is deliberately absent. It is system-derived by the
# existing Admin-only controlled reversal flow and is not a frontline Edge choice.
DENOMINATIONS = {
    "deposit_bill_1000_qty": 1000.0,
    "deposit_bill_500_qty": 500.0,
    "deposit_bill_200_qty": 200.0,
    "deposit_bill_100_qty": 100.0,
    "deposit_bill_50_qty": 50.0,
    "deposit_bill_20_qty": 20.0,
    "deposit_coin_20_qty": 20.0,
    "deposit_coin_10_qty": 10.0,
    "deposit_coin_5_qty": 5.0,
    "deposit_coin_1_qty": 1.0,
    "deposit_coin_025_qty": 0.25,
}
ALLOWED_KEYS = {
    "cashier_shift",
    "adjustment_type",
    "amount",
    "party_name",
    "purpose",
    "supporting_document",
    "client_observed_at",
    "client_ui_version",
    *DENOMINATIONS.keys(),
}
TOLERANCE = 0.000001


def _runtime_role() -> str:
    return str(frappe.conf.get("nkt_runtime_role") or "Primary").strip()


def _session_user(user: Optional[str] = None) -> str:
    user = user or frappe.session.user
    if not user or user == "Guest":
        raise frappe.PermissionError("Offline cash drawer adjustment unavailable.")
    return user


def _roles(user: str) -> set[str]:
    return set(frappe.get_roles(user) or [])


def _require_cashier_authority(user: str) -> None:
    if not (_roles(user) & CASHIER_ROLES):
        raise frappe.PermissionError("Offline cash drawer adjustment unavailable.")


def _uuid(value: Any, label: str) -> str:
    try:
        return str(uuid.UUID(str(value)))
    except Exception as exc:
        raise frappe.ValidationError(f"{label} is invalid.") from exc


def _text(value: Any, label: str, max_len: int, *, required: bool = False) -> str:
    out = str(value or "").strip()
    if required and not out:
        raise frappe.ValidationError(f"{label} is required.")
    if len(out) > max_len:
        raise frappe.ValidationError(f"{label} is too long.")
    return out


def _positive(value: Any, label: str) -> float:
    try:
        out = float(value)
    except Exception as exc:
        raise frappe.ValidationError(f"{label} must be numeric.") from exc
    if not math.isfinite(out) or out <= 0:
        raise frappe.ValidationError(f"{label} must be greater than zero.")
    return float(f"{out:.6f}")


def _manila_sql_datetime(value: Any, label: str) -> str:
    if isinstance(value, datetime):
        dt = value
    else:
        raw = str(value or "").strip()
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(raw)
        except Exception as exc:
            raise frappe.ValidationError(f"{label} is not a valid datetime.") from exc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=PH_TZ)
    else:
        dt = dt.astimezone(PH_TZ)
    return dt.replace(tzinfo=None).strftime("%Y-%m-%d %H:%M:%S")


def _normalize_raw_cash_drawer_adjustment_intent_payload(
    payload: Dict[str, Any],
) -> Dict[str, Any]:
    if not isinstance(payload, dict):
        raise frappe.ValidationError("Cash Drawer Adjustment Intent payload must be an object.")
    extra = set(payload) - ALLOWED_KEYS
    if extra:
        raise frappe.ValidationError(
            "Cash Drawer Adjustment Intent contains unsupported fields."
        )

    adjustment_type = _text(
        payload.get("adjustment_type"),
        "Adjustment Type",
        120,
        required=True,
    )
    direction = ADJUSTMENT_MAP.get(adjustment_type)
    if not direction:
        raise frappe.ValidationError(
            "Adjustment Type is not permitted for offline cash drawer recording."
        )

    denominations = {}
    denomination_total = 0.0
    for field, denomination in DENOMINATIONS.items():
        qty = cint(payload.get(field) or 0)
        if qty < 0:
            raise frappe.ValidationError("Denomination quantities cannot be negative.")
        denominations[field] = qty
        denomination_total += qty * denomination
    denomination_total = float(f"{denomination_total:.6f}")

    if adjustment_type == "Advance / Mid-Shift Deposit":
        if denomination_total <= TOLERANCE:
            raise frappe.ValidationError(
                "Enter at least one denomination for an Advance / Mid-Shift Deposit."
            )
        supplied = flt(payload.get("amount"))
        if supplied and abs(supplied - denomination_total) > TOLERANCE:
            raise NKTIdempotencyConflict(
                "Advance deposit amount conflicts with immutable denomination total."
            )
        amount = denomination_total
        purpose = _text(payload.get("purpose"), "Purpose", 1000)
    else:
        amount = _positive(payload.get("amount"), "Amount")
        purpose = _text(
            payload.get("purpose"),
            "Remarks / Explanation",
            1000,
            required=True,
        )
        if any(denominations.values()):
            raise frappe.ValidationError(
                "Denominations are only allowed for Advance / Mid-Shift Deposit."
            )

    return {
        "cashier_shift": _text(
            payload.get("cashier_shift"), "Cashier Shift", 140, required=True
        ),
        "adjustment_type": adjustment_type,
        "direction": direction,
        "amount": float(f"{amount:.6f}"),
        "party_name": _text(payload.get("party_name"), "Paid To / Received From", 240),
        "purpose": purpose,
        "supporting_document": _text(
            payload.get("supporting_document"), "Supporting Document", 1000
        ),
        "denominations": denominations,
        "denomination_total": denomination_total,
        "client_observed_at": _text(
            payload.get("client_observed_at"),
            "Client observed time",
            80,
            required=True,
        ),
        "client_ui_version": _text(payload.get("client_ui_version"), "Client UI Version", 120),
    }



CANONICAL_KEYS = {
    "cashier_shift",
    "adjustment_type",
    "direction",
    "amount",
    "party_name",
    "purpose",
    "supporting_document",
    "denominations",
    "denomination_total",
    "client_observed_at",
    "client_ui_version",
}


def _normalize_canonical_cash_drawer_adjustment_intent_payload(
    payload: Dict[str, Any],
) -> Dict[str, Any]:
    """
    Safe-sync transport stores the canonical normalized payload. Therefore the
    family normalizer must be idempotent across:
      raw Edge payload -> normalized canonical payload -> transport -> Primary.

    Derived fields are never trusted. Reconstruct the raw business payload,
    normalize it again, and require every derived value to agree.
    """
    if not isinstance(payload, dict) or set(payload) != CANONICAL_KEYS:
        raise frappe.ValidationError(
            "Canonical Cash Drawer Adjustment Intent payload shape is invalid."
        )

    denominations = payload.get("denominations")
    if not isinstance(denominations, dict) or set(denominations) != set(DENOMINATIONS):
        raise frappe.ValidationError(
            "Canonical Cash Drawer Adjustment Intent denominations are invalid."
        )

    raw = {
        "cashier_shift": payload.get("cashier_shift"),
        "adjustment_type": payload.get("adjustment_type"),
        "amount": payload.get("amount"),
        "party_name": payload.get("party_name"),
        "purpose": payload.get("purpose"),
        "supporting_document": payload.get("supporting_document"),
        "client_observed_at": payload.get("client_observed_at"),
        "client_ui_version": payload.get("client_ui_version"),
        **denominations,
    }
    normalized = _normalize_raw_cash_drawer_adjustment_intent_payload(raw)

    if str(payload.get("direction") or "") != normalized["direction"]:
        raise NKTIdempotencyConflict(
            "Canonical Cash Drawer Adjustment Intent direction conflicts with business payload."
        )
    if abs(flt(payload.get("amount")) - flt(normalized["amount"])) > TOLERANCE:
        raise NKTIdempotencyConflict(
            "Canonical Cash Drawer Adjustment Intent amount conflicts with business payload."
        )
    if (
        abs(
            flt(payload.get("denomination_total"))
            - flt(normalized["denomination_total"])
        )
        > TOLERANCE
    ):
        raise NKTIdempotencyConflict(
            "Canonical Cash Drawer Adjustment Intent denomination total conflicts with business payload."
        )

    for field in DENOMINATIONS:
        if cint(denominations.get(field)) != cint(normalized["denominations"].get(field)):
            raise NKTIdempotencyConflict(
                "Canonical Cash Drawer Adjustment Intent denominations conflict with business payload."
            )

    return normalized


def _normalize_cash_drawer_adjustment_intent_payload(
    payload: Dict[str, Any],
) -> Dict[str, Any]:
    if not isinstance(payload, dict):
        raise frappe.ValidationError(
            "Cash Drawer Adjustment Intent payload must be an object."
        )

    # Raw frontline payloads never contain derived fields. Canonical safe-sync
    # payloads do. Canonical values are re-derived and cross-checked above.
    canonical_markers = {"direction", "denominations", "denomination_total"}
    if set(payload) & canonical_markers:
        return _normalize_canonical_cash_drawer_adjustment_intent_payload(payload)

    return _normalize_raw_cash_drawer_adjustment_intent_payload(payload)

def _canonical_cash_drawer_adjustment_intent_json(payload: Dict[str, Any]) -> str:
    normalized = _normalize_cash_drawer_adjustment_intent_payload(payload)
    return json.dumps(
        normalized,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    )


def _pending_projection_effect(
    cashier_shift: str, *, exclude_event_uuid: Optional[str] = None
) -> float:
    args = [cashier_shift]
    extra = ""
    if exclude_event_uuid:
        extra = " AND event_uuid != %s"
        args.append(exclude_event_uuid)
    value = frappe.db.sql(
        f"""
        SELECT COALESCE(SUM(signed_cash_effect),0)
        FROM `tabNKT Edge Cash Drawer Adjustment Projection`
        WHERE cashier_shift=%s
          AND projection_state IN (
              'Pending Edge','Awaiting Primary','Primary Preserved','Primary Cash Materialized'
          )
          {extra}
        """,
        tuple(args),
    )[0][0]
    return flt(value)


def effective_edge_expected_cash(
    base_expected_cash: Any,
    cashier_shift: str,
    *,
    exclude_event_uuid: Optional[str] = None,
) -> float:
    return flt(base_expected_cash) + _pending_projection_effect(
        cashier_shift, exclude_event_uuid=exclude_event_uuid
    )


def _validate_edge_target(
    event_uuid: str,
    normalized: Dict[str, Any],
    user: str,
) -> Dict[str, Any]:
    shift_name = normalized["cashier_shift"]

    if is_edge_shift_reference(shift_name):
        shift_identity = validate_edge_shift_for_money(
            shift_name,
            company=(
                frappe.db.get_value(
                    "NKT Edge Cashier Shift Projection",
                    shift_name,
                    "company",
                ) or ""
            ),
            settlement_location=(
                frappe.db.get_value(
                    "NKT Edge Cashier Shift Projection",
                    shift_name,
                    "settlement_location",
                ) or ""
            ),
            cashier=user,
            require_open=True,
        )
        if normalized["adjustment_type"] == "Advance / Mid-Shift Deposit":
            available = edge_alias_expected_cash(
                shift_name,
                exclude_drawer_event_uuid=event_uuid,
            )
            if available < -TOLERANCE:
                raise frappe.ValidationError("This Store Edge shift already has negative expected cash.")
            if normalized["amount"] - available > TOLERANCE:
                raise frappe.ValidationError(
                    "Advance / Mid-Shift Deposit exceeds Store-Edge provisional expected drawer cash."
                )
        return {
            "company": str(shift_identity["company"] or ""),
            "settlement_location": str(shift_identity["settlement_location"] or ""),
            "cashier": str(shift_identity["cashier"] or ""),
            "shift_status": str(shift_identity["local_status"] or ""),
        }

    if not frappe.db.exists("NKT Cashier Shift", shift_name):
        raise frappe.DoesNotExistError("Cashier Shift is unavailable on this Store Edge.")
    shift = frappe.get_doc("NKT Cashier Shift", shift_name)

    if int(shift.docstatus or 0) != 0 or str(shift.status or "") != "Open":
        raise frappe.ValidationError("Cashier Shift is not open.")

    privileged = bool(_roles(user) & PRIVILEGED_ROLES) or user == "Administrator"
    if not privileged and str(shift.cashier or "") != user:
        raise frappe.PermissionError("Offline cash drawer adjustment unavailable.")

    other = frappe.db.get_value(
        "NKT Edge Cash Drawer Adjustment Projection",
        {"event_uuid": ["!=", event_uuid], "cashier_shift": shift_name},
        "event_uuid",
        order_by="creation desc",
    )
    _ = other

    if normalized["adjustment_type"] == "Advance / Mid-Shift Deposit":
        from nkt_operations.nkt_store_operations.features.cashier.shift_engine import (
            calculate_shift_summary,
        )
        summary = calculate_shift_summary(shift_name)
        base_expected_cash = flt(summary["expected_cash"])
        available = effective_edge_expected_cash(
            base_expected_cash,
            shift_name,
            exclude_event_uuid=event_uuid,
        )
        if available < -TOLERANCE:
            raise frappe.ValidationError("This shift already has negative expected cash.")
        if normalized["amount"] - available > TOLERANCE:
            raise frappe.ValidationError(
                "Advance / Mid-Shift Deposit exceeds Store-Edge expected drawer cash."
            )

    return {
        "company": str(shift.company or ""),
        "settlement_location": str(shift.settlement_location or ""),
        "cashier": str(shift.cashier or ""),
        "shift_status": str(shift.status or ""),
    }


def _verify_or_insert_projection(
    event_uuid: str,
    business_date: str,
    normalized: Dict[str, Any],
    shift_identity: Dict[str, Any],
) -> Dict[str, Any]:
    signed = normalized["amount"] if normalized["direction"] == "In" else -normalized["amount"]
    expected = {
        "event_uuid": event_uuid,
        "cashier_shift": normalized["cashier_shift"],
        "company": shift_identity["company"],
        "settlement_location": shift_identity["settlement_location"],
        "cashier": shift_identity["cashier"],
        "adjustment_type": normalized["adjustment_type"],
        "direction": normalized["direction"],
        "amount": normalized["amount"],
        "signed_cash_effect": float(f"{signed:.6f}"),
        "business_date": business_date,
        "projection_state": "Pending Edge",
    }

    if frappe.db.exists("NKT Edge Cash Drawer Adjustment Projection", event_uuid):
        got = frappe.get_doc("NKT Edge Cash Drawer Adjustment Projection", event_uuid)
        checks = (
            "event_uuid","cashier_shift","company","settlement_location","cashier",
            "adjustment_type","direction","business_date",
        )
        for field in checks:
            if str(got.get(field) or "") != str(expected[field] or ""):
                raise NKTIdempotencyConflict(
                    "Edge cash-drawer projection conflicts with immutable event."
                )
        if (
            abs(flt(got.amount) - flt(expected["amount"])) > TOLERANCE
            or abs(flt(got.signed_cash_effect) - flt(expected["signed_cash_effect"])) > TOLERANCE
            or got.projection_state not in (
                "Pending Edge","Awaiting Primary","Primary Preserved","Primary Cash Materialized","Finalized"
            )
        ):
            raise NKTIdempotencyConflict(
                "Edge cash-drawer projection conflicts with immutable event."
            )
        return {
            "projection_state": got.projection_state,
            "signed_cash_effect": flt(got.signed_cash_effect),
            "replay": True,
        }

    frappe.get_doc(
        {
            "doctype": "NKT Edge Cash Drawer Adjustment Projection",
            **expected,
        }
    ).insert(ignore_permissions=True)
    return {
        "projection_state": "Pending Edge",
        "signed_cash_effect": flt(expected["signed_cash_effect"]),
        "replay": False,
    }


def accept_cash_drawer_adjustment_intent_at_edge(
    event_uuid: str,
    device_id: str,
    business_date: Any,
    settled_at: Any,
    payload: Dict[str, Any],
    *,
    user: Optional[str] = None,
) -> Dict[str, Any]:
    if _runtime_role() != "Store Edge":
        raise frappe.PermissionError("Offline cash drawer adjustment unavailable.")

    event_uuid = _uuid(event_uuid, "Cash Drawer Adjustment Event UUID")
    user = _session_user(user)
    _require_cashier_authority(user)

    device = device_policy_snapshot(
        device_id,
        user=user,
        requested_context="NKT Retail",
    )
    if device.get("ui_mode") != "normal":
        raise frappe.PermissionError("Offline cash drawer adjustment unavailable.")

    if event_family_policy(CASH_DRAWER_ADJUSTMENT_INTENT_FAMILY).get(
        "offline_write_allowed"
    ) is not True:
        raise frappe.PermissionError("Offline cash drawer adjustment unavailable.")

    business = validate_business_time(business_date, settled_at)
    normalized = _normalize_cash_drawer_adjustment_intent_payload(payload)
    shift_identity = _validate_edge_target(event_uuid, normalized, user)

    digest = canonical_payload_hash(normalized)
    envelope = {
        "event_uuid": event_uuid,
        "event_family": CASH_DRAWER_ADJUSTMENT_INTENT_FAMILY,
        "event_action": CASH_DRAWER_ADJUSTMENT_INTENT_ACTION,
        "operational_context": device.get("operational_context") or "NKT Retail",
        "origin_device": device_id,
        "origin_user": user,
        "business_date": business["business_date"],
        "settled_at": _manila_sql_datetime(
            business["settled_at_manila"], "Business / Settled Time"
        ),
        "client_created_at": _manila_sql_datetime(
            normalized["client_observed_at"], "Client observed time"
        ),
        "payload_sha256": digest,
    }

    event, replay = begin_event(envelope)
    canonical_json = _canonical_cash_drawer_adjustment_intent_json(normalized)
    pending_name = frappe.db.exists("NKT Sync Pending Payload", event.event_uuid)

    if pending_name:
        pending = frappe.get_doc("NKT Sync Pending Payload", pending_name)
        if (
            pending.event_family != CASH_DRAWER_ADJUSTMENT_INTENT_FAMILY
            or pending.payload_sha256 != digest
            or pending.payload_json != canonical_json
        ):
            raise NKTIdempotencyConflict(
                "Pending cash-drawer payload conflicts with immutable Event UUID."
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
                "Cash-drawer event exists but durable pending payload is unavailable."
            )
        frappe.get_doc(
            {
                "doctype": "NKT Sync Pending Payload",
                "event_uuid": event.event_uuid,
                "event_family": CASH_DRAWER_ADJUSTMENT_INTENT_FAMILY,
                "payload_sha256": digest,
                "payload_json": canonical_json,
                "queue_state": "Accepted at Edge",
                "edge_accepted_at": now(),
                "attempt_count": 1,
                "last_attempt_at": now(),
            }
        ).insert(ignore_permissions=True)

    projection = _verify_or_insert_projection(
        event.event_uuid,
        business["business_date"],
        normalized,
        shift_identity,
    )

    if event.sync_state not in ("Committed at Primary", "Conflict", "Failed"):
        mark_edge_accepted(event.event_uuid)
        event.reload()

    return {
        "event_uuid": event.event_uuid,
        "event_family": CASH_DRAWER_ADJUSTMENT_INTENT_FAMILY,
        "sync_state": event.sync_state,
        "durable_ack": True,
        "replay": bool(replay),
        "payload_sha256": digest,
        "cashier_shift": normalized["cashier_shift"],
        "adjustment_type": normalized["adjustment_type"],
        "direction": normalized["direction"],
        "amount": normalized["amount"],
        "edge_projected_cash_effect": projection["signed_cash_effect"],
        "projection_state": projection["projection_state"],
        "primary_preservation_required": True,
        "cash_drawer_adjustment_created": False,
        "cashier_movement_created": False,
        "controlled_reversal_supported_offline": False,
    }


@frappe.whitelist()
def submit_cash_drawer_adjustment_intent(
    event_uuid: str,
    device_id: str,
    business_date: str,
    settled_at: str,
    payload: Any,
):
    if isinstance(payload, str):
        payload = frappe.parse_json(payload)
    return accept_cash_drawer_adjustment_intent_at_edge(
        event_uuid,
        device_id,
        business_date,
        settled_at,
        payload,
        user=_session_user(),
    )
