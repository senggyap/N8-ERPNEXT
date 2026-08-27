from __future__ import annotations

import hashlib
import json
import math
import uuid
from datetime import datetime
from typing import Any, Dict
from zoneinfo import ZoneInfo

import frappe
from frappe.utils import flt, getdate

FOUNDATION_VERSION = "C15C.10K-R4C"
PH_TZ = ZoneInfo("Asia/Manila")
TOLERANCE = 0.005

CASHIER_SHIFT_OPEN_FAMILY = "NKT Cashier Shift Open Intent"
CASHIER_SHIFT_OPEN_ACTION = "Open Cashier Shift Offline"

CASHIER_SHIFT_CLOSE_FAMILY = "NKT Cashier Shift Close Intent"
CASHIER_SHIFT_CLOSE_ACTION = "Close Cashier Shift Offline"

ENCODER_ZOUT_FINALIZE_FAMILY = "NKT Encoder Z-Out Finalization Intent"
ENCODER_ZOUT_FINALIZE_ACTION = "Finalize Official Encoder Z-Out Offline"

CASHIER_ROLES = {
    "NKT Cashier",
    "NKT OWNER",
    "NKT ADMINISTRATOR",
    "System Manager",
}
ENCODER_ROLES = {
    "NKT Encoder",
    "NKT OWNER",
    "NKT ADMINISTRATOR",
    "System Manager",
}

DENOMINATIONS = {
    "bill_1000_qty": 1000.0,
    "bill_500_qty": 500.0,
    "bill_200_qty": 200.0,
    "bill_100_qty": 100.0,
    "bill_50_qty": 50.0,
    "bill_20_qty": 20.0,
    "coin_20_qty": 20.0,
    "coin_10_qty": 10.0,
    "coin_5_qty": 5.0,
    "coin_1_qty": 1.0,
    "coin_025_qty": 0.25,
}

OPEN_ALLOWED = {
    "submit_request_id",
    "edge_shift_uuid",
    "company",
    "settlement_location",
    "cashier",
    "shift_business_date",
    "shift_start",
    "opening_cash",
    "client_ui_version",
}

CLOSE_ALLOWED = {
    "submit_request_id",
    "edge_shift_uuid",
    "primary_shift_name",
    "company",
    "settlement_location",
    "cashier",
    "shift_business_date",
    "shift_start",
    "physical_close_datetime",
    "denominations",
    "actual_cash",
    "provisional_expected_cash",
    "provisional_over_short",
    "provisional_movement_count",
    "provisional_summary",
    "count_notes",
    "client_ui_version",
}

ZOUT_ALLOWED = {
    "submit_request_id",
    "edge_zout_uuid",
    "company",
    "encoder",
    "business_date",
    "start_datetime",
    "effective_end_datetime",
    "finalized_on",
    "snapshot_json",
    "snapshot_sha256",
    "client_ui_version",
}

OPEN_DERIVED_FIELDS = {
    "operational_effect",
    "primary_shift_name_authoritative_at_edge",
}

CLOSE_DERIVED_FIELDS = {
    "physical_close_date",
    "provisional_summary_sha256",
    "physical_count_is_immutable_truth",
    "edge_expected_cash_is_provisional",
    "primary_may_recompute_expected_cash",
    "primary_may_rewrite_physical_count",
    "cross_midnight_close_allowed",
    "employee_manual_backdate_allowed",
}

ZOUT_DERIVED_FIELDS = {
    "official_finalized_offline",
    "snapshot_is_immutable_after_sync",
    "primary_may_attach_reconciliation",
    "primary_may_rewrite_official_snapshot",
    "employee_manual_backdate_allowed",
}

FORBIDDEN_PRIMARY_RECONCILIATION_KEYS = {
    "primary_expected_cash",
    "primary_over_short",
    "primary_movement_count",
    "primary_reconciliation_status",
    "primary_reconciliation_delta",
    "primary_reconciled_on",
    "primary_reconciled_by",
    "canonical_shift_status",
    "canonical_zout_name",
    "official_recomputed_snapshot_json",
    "official_recomputed_snapshot_sha256",
}


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


def _money(value: Any, label: str, *, nonnegative: bool = True) -> float:
    try:
        out = float(value or 0)
    except Exception as exc:
        raise frappe.ValidationError(f"{label} must be numeric.") from exc
    if not math.isfinite(out):
        raise frappe.ValidationError(f"{label} must be finite.")
    if nonnegative and out < -TOLERANCE:
        raise frappe.ValidationError(f"{label} cannot be negative.")
    return float(f"{out:.6f}")


def _int_nonnegative(value: Any, label: str) -> int:
    try:
        out = int(value or 0)
    except Exception as exc:
        raise frappe.ValidationError(f"{label} must be a whole number.") from exc
    if out < 0:
        raise frappe.ValidationError(f"{label} cannot be negative.")
    return out


def _manila_datetime(value: Any, label: str) -> datetime:
    raw = str(value or "").strip()
    if not raw:
        raise frappe.ValidationError(f"{label} is required.")
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        dt = value if isinstance(value, datetime) else datetime.fromisoformat(raw)
    except Exception as exc:
        raise frappe.ValidationError(f"{label} is invalid.") from exc
    if dt.tzinfo is None:
        return dt.replace(tzinfo=PH_TZ)
    return dt.astimezone(PH_TZ)


def _iso(dt: datetime) -> str:
    return dt.replace(microsecond=0).isoformat()


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    )


def _canonical_normalized_or_raw(
    payload: Dict[str, Any],
    *,
    raw_allowed: set[str],
    derived_allowed: set[str],
    normalizer,
    label: str,
) -> str:
    """Canonicalize raw OR already-normalized immutable intent safely.

    Shared safe-sync transport normalizes first and then asks the family
    canonicalizer to prove that the stored payload is still in canonical form.
    Therefore the canonicalizer must accept that normalized form, but it must
    not trust its derived fields.

    For normalized input we reconstruct only the original raw contract fields,
    normalize them again, and require byte-equivalent canonical content. Any
    forged/changed derived field or unknown extra field is rejected.
    """
    if not isinstance(payload, dict):
        raise frappe.ValidationError(f"{label} payload must be an object.")

    keys = set(payload)
    unknown = keys - raw_allowed - derived_allowed
    if unknown:
        raise frappe.ValidationError(
            f"{label} contains unsupported fields: {', '.join(sorted(unknown))}."
        )

    if keys & derived_allowed:
        raw_projection = {key: payload[key] for key in raw_allowed if key in payload}
        renormalized = normalizer(raw_projection)
        if _canonical_json(renormalized) != _canonical_json(payload):
            raise frappe.ValidationError(
                f"{label} normalized payload contains changed or forged derived content."
            )
        return _canonical_json(renormalized)

    return _canonical_json(normalizer(payload))


def _reject_keys(payload: Dict[str, Any], allowed: set[str], label: str) -> None:
    if not isinstance(payload, dict):
        raise frappe.ValidationError(f"{label} payload must be an object.")
    forbidden = set(payload) & FORBIDDEN_PRIMARY_RECONCILIATION_KEYS
    if forbidden:
        raise frappe.ValidationError(
            f"{label} cannot contain Primary reconciliation/final-truth fields."
        )
    extra = set(payload) - allowed
    if extra:
        raise frappe.ValidationError(
            f"{label} contains unsupported fields: {', '.join(sorted(extra))}."
        )


def _denominations(raw: Any) -> tuple[Dict[str, int], float]:
    if not isinstance(raw, dict) or set(raw) != set(DENOMINATIONS):
        raise frappe.ValidationError(
            "Cashier Shift Close denominations must contain the complete accepted denomination grid."
        )
    clean: Dict[str, int] = {}
    total = 0.0
    for field, value in DENOMINATIONS.items():
        qty = _int_nonnegative(raw.get(field), field)
        clean[field] = qty
        total += qty * value
    return clean, float(f"{total:.6f}")


def _normalize_cashier_shift_open_intent_raw(payload: Dict[str, Any]) -> Dict[str, Any]:
    _reject_keys(payload, OPEN_ALLOWED, "Cashier Shift Open Intent")

    shift_start = _manila_datetime(payload.get("shift_start"), "Shift Start")
    business_date = getdate(payload.get("shift_business_date"))
    if business_date != shift_start.date():
        raise frappe.ValidationError(
            "Cashier Shift Business Date must equal the true Store Edge shift-open date. "
            "Employees do not receive a manual backdate override."
        )

    return {
        "submit_request_id": _text(
            payload.get("submit_request_id"), "Submit Request ID", 180, required=True
        ),
        "edge_shift_uuid": _uuid(payload.get("edge_shift_uuid"), "Edge Shift UUID"),
        "company": _text(payload.get("company"), "Company", 180, required=True),
        "settlement_location": _text(
            payload.get("settlement_location"),
            "Cashier / Settlement Location",
            180,
            required=True,
        ),
        "cashier": _text(payload.get("cashier"), "Cashier", 180, required=True),
        "shift_business_date": business_date.isoformat(),
        "shift_start": _iso(shift_start),
        "opening_cash": _money(payload.get("opening_cash"), "Opening Cash"),
        "client_ui_version": _text(
            payload.get("client_ui_version"), "Client UI Version", 120
        ),
        "operational_effect": "Official Store Edge shift opened for offline continuity",
        "primary_shift_name_authoritative_at_edge": False,
    }


def _normalize_cashier_shift_close_intent_raw(payload: Dict[str, Any]) -> Dict[str, Any]:
    _reject_keys(payload, CLOSE_ALLOWED, "Cashier Shift Close Intent")

    shift_start = _manila_datetime(payload.get("shift_start"), "Shift Start")
    close_dt = _manila_datetime(
        payload.get("physical_close_datetime"), "Physical Close Date / Time"
    )
    shift_business_date = getdate(payload.get("shift_business_date"))

    if shift_business_date != shift_start.date():
        raise frappe.ValidationError(
            "Original Shift Business Date must remain tied to the true shift-open date."
        )
    if close_dt < shift_start:
        raise frappe.ValidationError("Physical Close Date / Time cannot precede Shift Start.")

    denoms, denomination_total = _denominations(payload.get("denominations"))
    actual_cash = _money(payload.get("actual_cash"), "Actual Cash")
    if abs(actual_cash - denomination_total) > TOLERANCE:
        raise frappe.ValidationError(
            "Actual Cash conflicts with the immutable physical denomination count."
        )

    provisional_expected = _money(
        payload.get("provisional_expected_cash"),
        "Provisional Store Edge Expected Cash",
        nonnegative=False,
    )
    calculated_provisional_variance = float(
        f"{actual_cash - provisional_expected:.6f}"
    )
    if "provisional_over_short" in payload and abs(
        flt(payload.get("provisional_over_short")) - calculated_provisional_variance
    ) > TOLERANCE:
        raise frappe.ValidationError(
            "Provisional Over / Short conflicts with Actual Cash minus provisional Expected Cash."
        )

    notes = _text(payload.get("count_notes"), "Closing Notes", 2000)
    if abs(calculated_provisional_variance) > TOLERANCE and not notes:
        raise frappe.ValidationError(
            "A cash difference explanation is required when the provisional drawer is over or short."
        )

    summary = payload.get("provisional_summary")
    if not isinstance(summary, dict):
        raise frappe.ValidationError(
            "Provisional Store Edge closing summary must be an object."
        )

    return {
        "submit_request_id": _text(
            payload.get("submit_request_id"), "Submit Request ID", 180, required=True
        ),
        "edge_shift_uuid": _uuid(payload.get("edge_shift_uuid"), "Edge Shift UUID"),
        "primary_shift_name": _text(
            payload.get("primary_shift_name"), "Primary Shift Name", 180
        ),
        "company": _text(payload.get("company"), "Company", 180, required=True),
        "settlement_location": _text(
            payload.get("settlement_location"),
            "Cashier / Settlement Location",
            180,
            required=True,
        ),
        "cashier": _text(payload.get("cashier"), "Cashier", 180, required=True),
        "shift_business_date": shift_business_date.isoformat(),
        "shift_start": _iso(shift_start),
        "physical_close_datetime": _iso(close_dt),
        "physical_close_date": close_dt.date().isoformat(),
        "denominations": denoms,
        "actual_cash": actual_cash,
        "provisional_expected_cash": provisional_expected,
        "provisional_over_short": calculated_provisional_variance,
        "provisional_movement_count": _int_nonnegative(
            payload.get("provisional_movement_count"),
            "Provisional Movement Count",
        ),
        "provisional_summary": summary,
        "provisional_summary_sha256": hashlib.sha256(
            _canonical_json(summary).encode("utf-8")
        ).hexdigest(),
        "count_notes": notes,
        "client_ui_version": _text(
            payload.get("client_ui_version"), "Client UI Version", 120
        ),
        "physical_count_is_immutable_truth": True,
        "edge_expected_cash_is_provisional": True,
        "primary_may_recompute_expected_cash": True,
        "primary_may_rewrite_physical_count": False,
        "cross_midnight_close_allowed": True,
        "employee_manual_backdate_allowed": False,
    }


def _normalize_encoder_zout_finalization_intent_raw(payload: Dict[str, Any]) -> Dict[str, Any]:
    _reject_keys(payload, ZOUT_ALLOWED, "Encoder Z-Out Finalization Intent")

    business_date = getdate(payload.get("business_date"))
    start_dt = _manila_datetime(payload.get("start_datetime"), "Z-Out Start Date / Time")
    end_dt = _manila_datetime(
        payload.get("effective_end_datetime"), "Z-Out Effective End Date / Time"
    )
    finalized_on = _manila_datetime(payload.get("finalized_on"), "Z-Out Finalized On")

    if start_dt.date() != business_date or end_dt.date() != business_date:
        raise frappe.ValidationError(
            "Official offline Encoder Z-Out must use its live business date/time window. "
            "Employees do not receive a manual backdate override."
        )
    if end_dt < start_dt:
        raise frappe.ValidationError("Z-Out Effective End cannot precede Start.")
    if finalized_on.date() != business_date:
        raise frappe.ValidationError(
            "Official offline Encoder Z-Out Finalized On must remain on its live business date."
        )
    if end_dt > finalized_on:
        raise frappe.ValidationError(
            "Official offline Z-Out cannot claim transactions after its true finalization time."
        )

    snapshot_text = str(payload.get("snapshot_json") or "")
    if not snapshot_text:
        raise frappe.ValidationError("Frozen Z-Out Snapshot is required.")
    try:
        snapshot = json.loads(snapshot_text)
    except Exception as exc:
        raise frappe.ValidationError("Frozen Z-Out Snapshot must contain valid JSON.") from exc

    canonical_snapshot = _canonical_json(snapshot)
    calculated_hash = hashlib.sha256(canonical_snapshot.encode("utf-8")).hexdigest()
    supplied_hash = str(payload.get("snapshot_sha256") or "").strip().lower()
    if supplied_hash != calculated_hash:
        raise frappe.ValidationError(
            "Frozen Z-Out Snapshot Hash conflicts with the immutable official offline snapshot."
        )

    scope = snapshot.get("scope") if isinstance(snapshot, dict) else None
    if not isinstance(scope, dict):
        raise frappe.ValidationError("Official Z-Out Snapshot must contain its frozen scope.")

    expected_scope = {
        "company": str(payload.get("company") or "").strip(),
        "business_date": business_date.isoformat(),
        "encoder": str(payload.get("encoder") or "").strip(),
    }
    for key, expected in expected_scope.items():
        actual = str(scope.get(key) or "").strip()
        if actual != expected:
            raise frappe.ValidationError(
                f"Frozen Z-Out Snapshot scope conflicts with {key}."
            )

    return {
        "submit_request_id": _text(
            payload.get("submit_request_id"), "Submit Request ID", 180, required=True
        ),
        "edge_zout_uuid": _uuid(payload.get("edge_zout_uuid"), "Edge Z-Out UUID"),
        "company": _text(payload.get("company"), "Company", 180, required=True),
        "encoder": _text(payload.get("encoder"), "Encoder", 180, required=True),
        "business_date": business_date.isoformat(),
        "start_datetime": _iso(start_dt),
        "effective_end_datetime": _iso(end_dt),
        "finalized_on": _iso(finalized_on),
        "snapshot_json": canonical_snapshot,
        "snapshot_sha256": calculated_hash,
        "client_ui_version": _text(
            payload.get("client_ui_version"), "Client UI Version", 120
        ),
        "official_finalized_offline": True,
        "snapshot_is_immutable_after_sync": True,
        "primary_may_attach_reconciliation": True,
        "primary_may_rewrite_official_snapshot": False,
        "employee_manual_backdate_allowed": False,
    }


def _normalize_raw_or_normalized(
    payload: Dict[str, Any],
    *,
    raw_allowed: set[str],
    derived_allowed: set[str],
    raw_normalizer,
    label: str,
) -> Dict[str, Any]:
    """Accept raw input or the exact immutable normalized representation.

    The shared transport intentionally re-normalizes the already-normalized
    pending payload. Derived fields are never trusted: they are discarded,
    raw fields are normalized again, and the exact normalized object must
    match byte-for-byte in canonical JSON.
    """
    if not isinstance(payload, dict):
        raise frappe.ValidationError(f"{label} payload must be an object.")

    keys = set(payload)
    unknown = keys - raw_allowed - derived_allowed
    if unknown:
        raise frappe.ValidationError(
            f"{label} contains unsupported fields: {', '.join(sorted(unknown))}."
        )

    if keys & derived_allowed:
        raw_projection = {key: payload[key] for key in raw_allowed if key in payload}
        renormalized = raw_normalizer(raw_projection)
        if _canonical_json(renormalized) != _canonical_json(payload):
            raise frappe.ValidationError(
                f"{label} normalized payload contains changed or forged derived content."
            )
        return renormalized

    return raw_normalizer(payload)


def normalize_cashier_shift_open_intent(payload: Dict[str, Any]) -> Dict[str, Any]:
    return _normalize_raw_or_normalized(
        payload,
        raw_allowed=OPEN_ALLOWED,
        derived_allowed=OPEN_DERIVED_FIELDS,
        raw_normalizer=_normalize_cashier_shift_open_intent_raw,
        label="Cashier Shift Open Intent",
    )


def normalize_cashier_shift_close_intent(payload: Dict[str, Any]) -> Dict[str, Any]:
    return _normalize_raw_or_normalized(
        payload,
        raw_allowed=CLOSE_ALLOWED,
        derived_allowed=CLOSE_DERIVED_FIELDS,
        raw_normalizer=_normalize_cashier_shift_close_intent_raw,
        label="Cashier Shift Close Intent",
    )


def normalize_encoder_zout_finalization_intent(payload: Dict[str, Any]) -> Dict[str, Any]:
    return _normalize_raw_or_normalized(
        payload,
        raw_allowed=ZOUT_ALLOWED,
        derived_allowed=ZOUT_DERIVED_FIELDS,
        raw_normalizer=_normalize_encoder_zout_finalization_intent_raw,
        label="Encoder Z-Out Finalization Intent",
    )


def canonical_cashier_shift_open_intent_json(payload: Dict[str, Any]) -> str:
    return _canonical_normalized_or_raw(
        payload,
        raw_allowed=OPEN_ALLOWED,
        derived_allowed=OPEN_DERIVED_FIELDS,
        normalizer=normalize_cashier_shift_open_intent,
        label="Cashier Shift Open Intent",
    )


def canonical_cashier_shift_close_intent_json(payload: Dict[str, Any]) -> str:
    return _canonical_normalized_or_raw(
        payload,
        raw_allowed=CLOSE_ALLOWED,
        derived_allowed=CLOSE_DERIVED_FIELDS,
        normalizer=normalize_cashier_shift_close_intent,
        label="Cashier Shift Close Intent",
    )


def canonical_encoder_zout_finalization_intent_json(payload: Dict[str, Any]) -> str:
    return _canonical_normalized_or_raw(
        payload,
        raw_allowed=ZOUT_ALLOWED,
        derived_allowed=ZOUT_DERIVED_FIELDS,
        normalizer=normalize_encoder_zout_finalization_intent,
        label="Encoder Z-Out Finalization Intent",
    )


def contract_status() -> Dict[str, Any]:
    return {
        "foundation_version": FOUNDATION_VERSION,
        "cashier_shift_open_offline": True,
        "next_cashier_may_continue_offline_after_prior_physical_close": True,
        "cashier_physical_count_immutable": True,
        "edge_expected_cash_provisional": True,
        "primary_expected_cash_recalculation_allowed": True,
        "primary_physical_count_rewrite_allowed": False,
        "cross_midnight_cashier_close_preserves_true_close_time": True,
        "official_encoder_zout_offline": True,
        "official_encoder_zout_snapshot_rewrite_allowed": False,
        "primary_zout_reconciliation_attachment_allowed": True,
        "employee_manual_backdate_allowed": False,
        "primary_materialization_enabled_at_r2": False,
        "shared_transport_registered_at_r2": False,
    }
