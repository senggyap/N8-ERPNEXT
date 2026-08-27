from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

import frappe
from frappe import _
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields
from frappe.utils import cint, flt, get_datetime, now_datetime

VERSION = "MP1A"
AUTHORIZER_DOCTYPE = "NKT Selling Price Authorizer"
EVENT_DOCTYPE = "NKT Selling Price Authorization Event"
CASHIER_SCRIPT = "NKT Cashier Fast Screen V2.0C.3"
ENCODER_SCRIPT = "NKT Encoder Fast Screen V2.0C.3"
CASHIER_SCREEN = "NKT Cashier Fast Screen"
ENCODER_SCREEN = "NKT Encoder Fast Screen"

PRIMARY_ROLE = "Primary"
EDGE_ROLE = "Store Edge"
CASHIER_ROLES = {"NKT Cashier", "NKT OWNER", "NKT ADMINISTRATOR", "System Manager"}
CONFIG_ROLES = {"NKT OWNER", "NKT ADMINISTRATOR", "System Manager"}

PIN_RE = re.compile(r"^[0-9]{5}$")
PBKDF2_ITERATIONS = 180_000
NKT_MANAGER_PIN_MP1A_KEY_RECOVERY = True
TOKEN_TTL_SECONDS = 15 * 60
WRONG_ATTEMPT_WINDOW_SECONDS = 5 * 60
WRONG_ATTEMPT_LIMIT = 5
TEMP_BLOCK_SECONDS = 5 * 60
RATE_TOLERANCE = 0.000001

REASONS = (
    "Customer Negotiated Price",
    "Market Price Adjustment",
    "Bulk Quantity",
    "Damaged Packaging",
    "Management Instruction",
    "Other",
)
DEFAULT_VARIATIONS = (-20.0, -15.0, -10.0, -5.0, 5.0, 10.0, 15.0, 20.0)
FIXED_SELECT_VARIATIONS = DEFAULT_VARIATIONS

# Security rule: this evidence is safe to preserve with the sale/tender.
# It NEVER contains a PIN, credential salt/digest, or signed proof token.
OFFLINE_EVIDENCE_KEYS = (
    "authorized_by",
    "authorized_on",
    "reason",
    "explanation",
    "adjustment_signature",
    "authorization_runtime_role",
    "authorization_device_id",
)


def _runtime_role() -> str:
    role = str(frappe.conf.get("nkt_runtime_role") or PRIMARY_ROLE).strip()
    return role if role in {PRIMARY_ROLE, EDGE_ROLE} else PRIMARY_ROLE


def _session_user() -> str:
    user = frappe.session.user
    if not user or user == "Guest":
        raise frappe.PermissionError(_("Manager price authorization is unavailable."))
    return user


def _require_cashier(user: Optional[str] = None) -> str:
    user = user or _session_user()
    if not (set(frappe.get_roles(user) or []) & CASHIER_ROLES):
        raise frappe.PermissionError(_("Manager price authorization is unavailable."))
    return user


def _require_config_authority(user: Optional[str] = None) -> str:
    user = user or _session_user()
    if not (set(frappe.get_roles(user) or []) & CONFIG_ROLES):
        raise frappe.PermissionError(_("Only NKT Owner / Administrator may manage selling-price PINs."))
    return user


def _clean_text(value: Any, max_len: int = 500) -> str:
    text = str(value or "").strip()
    if len(text) > max_len:
        frappe.throw(_("Price authorization text is too long."))
    return text


def _round6(value: Any) -> float:
    return float(f"{flt(value):.6f}")


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def _sha256_json(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _decode(value: str) -> bytes:
    value = str(value or "")
    return base64.urlsafe_b64decode(value + ("=" * (-len(value) % 4)))


def _hash_pin(pin: str, salt_b64: str, iterations: int) -> str:
    digest = hashlib.pbkdf2_hmac(
        "sha256",
        pin.encode("utf-8"),
        _decode(salt_b64),
        int(iterations),
    )
    return _encode(digest)


def _verify_pin(pin: str, salt_b64: str, digest_b64: str, iterations: int) -> bool:
    try:
        candidate = _hash_pin(pin, salt_b64, iterations)
        return hmac.compare_digest(candidate, str(digest_b64 or ""))
    except Exception:
        return False


def _validate_pin_format(pin: Any) -> str:
    pin = str(pin or "")
    if not PIN_RE.fullmatch(pin):
        frappe.throw(_("Manager PIN must be exactly five numeric digits."))
    return pin


def _token_key() -> bytes:
    """Return a site-local signing key without creating or exposing a new secret.

    Preferred root secret is Frappe's encryption_key. Some development/test
    sites legitimately predate that setting, so fall back to another existing
    site secret (secret_key, then db_password) with strict domain separation.
    No source secret is stored in Manager-PIN records, tokens, audit events, or
    browser bootstrap data. A site-secret rotation intentionally invalidates
    any outstanding short-lived authorization proof.
    """
    site = str(getattr(frappe.local, "site", "") or frappe.conf.get("db_name") or "nkt-site")
    for source_name in ("encryption_key", "secret_key", "db_password"):
        raw = str(frappe.conf.get(source_name) or "").strip()
        if not raw:
            continue
        context = (
            "NKT-MANAGER-PIN-PROOF-V1\0"
            + source_name
            + "\0"
            + site
        ).encode("utf-8")
        return hmac.new(raw.encode("utf-8"), context, hashlib.sha256).digest()

    frappe.throw(
        _(
            "No usable site secret is available; Manager PIN authorization "
            "cannot continue until the site configuration is repaired."
        )
    )


def _issue_token(payload: Dict[str, Any]) -> str:
    body = _canonical_json(payload).encode("utf-8")
    sig = hmac.new(_token_key(), body, hashlib.sha256).digest()
    return _encode(body) + "." + _encode(sig)


def _read_token(token: str) -> Dict[str, Any]:
    try:
        body_part, sig_part = str(token or "").split(".", 1)
        body = _decode(body_part)
        supplied = _decode(sig_part)
        expected = hmac.new(_token_key(), body, hashlib.sha256).digest()
        if not hmac.compare_digest(supplied, expected):
            raise ValueError("signature")
        payload = json.loads(body.decode("utf-8"))
    except Exception as exc:
        raise frappe.ValidationError(_("Price authorization proof is invalid. Authorize the transaction again.")) from exc

    now_ts = int(time.time())
    issued_at = cint(payload.get("issued_at"))
    expires_at = cint(payload.get("expires_at"))
    if not issued_at or not expires_at or expires_at < now_ts or issued_at > now_ts + 60:
        raise frappe.ValidationError(_("Price authorization has expired. Authorize the transaction again."))
    if expires_at - issued_at > TOKEN_TTL_SECONDS + 5:
        raise frappe.ValidationError(_("Price authorization proof has an invalid lifetime."))
    return dict(payload)


def _configured_variations_primary() -> Tuple[float, ...]:
    try:
        from nkt_operations.nkt_store_operations import fast_screen_backend as nkt_fast_ui_v2
        values = tuple(float(x) for x in nkt_fast_ui_v2._configured_price_variations())
        return values or DEFAULT_VARIATIONS
    except Exception:
        return DEFAULT_VARIATIONS


def snapshot_variations() -> List[float]:
    return [_round6(x) for x in _configured_variations_primary()]


def _edge_snapshot() -> Dict[str, Any]:
    from nkt_operations.nkt_store_operations.features.offline_edge.internal.edge_provider import (
        load_configured_edge_snapshot,
    )
    return dict(load_configured_edge_snapshot() or {})


def _edge_variations(snapshot: Optional[Dict[str, Any]] = None) -> Tuple[float, ...]:
    snapshot = snapshot or _edge_snapshot()
    values = []
    for value in list(snapshot.get("selling_price_variations") or []):
        try:
            number = _round6(value)
        except Exception:
            continue
        if abs(number) > RATE_TOLERANCE and number not in values:
            values.append(number)
    return tuple(values) or DEFAULT_VARIATIONS


def _primary_authorizers() -> List[Dict[str, Any]]:
    if not frappe.db.exists("DocType", AUTHORIZER_DOCTYPE):
        return []
    rows = frappe.get_all(
        AUTHORIZER_DOCTYPE,
        filters={
            "can_authorize_selling_price_adjustments": 1,
            "pin_configured": 1,
        },
        fields=[
            "user",
            "pin_salt",
            "pin_hash",
            "pin_iterations",
            "can_authorize_selling_price_adjustments",
        ],
        order_by="user asc",
        limit_page_length=500,
    )
    out = []
    for row in rows:
        enabled = cint(frappe.db.get_value("User", row.user, "enabled"))
        if not enabled:
            continue
        if not row.pin_salt or not row.pin_hash:
            continue
        out.append({
            "user": row.user,
            "credential_salt": row.pin_salt,
            "credential_digest": row.pin_hash,
            "iterations": cint(row.pin_iterations) or PBKDF2_ITERATIONS,
            "enabled": 1,
        })
    return out


def snapshot_authorizers() -> List[Dict[str, Any]]:
    # Called only by the encrypted Store Edge snapshot builder.
    # The browser bootstrap must never receive this list.
    return _primary_authorizers()


def _edge_authorizers(snapshot: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    snapshot = snapshot or _edge_snapshot()
    rows = []
    for raw in list(snapshot.get("selling_price_authorizers") or []):
        row = dict(raw or {})
        if not cint(row.get("enabled")):
            continue
        if not row.get("user") or not row.get("credential_salt") or not row.get("credential_digest"):
            continue
        rows.append({
            "user": str(row.get("user")),
            "credential_salt": str(row.get("credential_salt")),
            "credential_digest": str(row.get("credential_digest")),
            "iterations": cint(row.get("iterations")) or PBKDF2_ITERATIONS,
            "enabled": 1,
        })
    return rows


def _authorizers_for_runtime() -> List[Dict[str, Any]]:
    if _runtime_role() == EDGE_ROLE:
        return _edge_authorizers()
    return _primary_authorizers()


def _primary_standard_rate(item_code: str) -> float:
    from nkt_operations.nkt_store_operations import fast_screen_backend as nkt_fast_ui_v2
    row = nkt_fast_ui_v2._item_master_context(item_code)
    return _round6(row.standard_rate)


def _edge_standard_rate(item_code: str, snapshot: Optional[Dict[str, Any]] = None) -> float:
    snapshot = snapshot or _edge_snapshot()
    for raw in list(snapshot.get("items") or []):
        if str(raw.get("item_code") or "").strip() == item_code:
            return _round6(raw.get("current_rate"))
    frappe.throw(_("Item is unavailable in this Store Edge price snapshot: {0}").format(item_code))


def _standard_rate(item_code: str, snapshot: Optional[Dict[str, Any]] = None) -> float:
    if _runtime_role() == EDGE_ROLE:
        return _edge_standard_rate(item_code, snapshot)
    return _primary_standard_rate(item_code)


def _stable_adjustment_rows(rows: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    stable = []
    for raw in rows:
        row = dict(raw or {})
        stable.append({
            "line_no": cint(row.get("line_no")),
            "item_code": str(row.get("item_code") or ""),
            "qty": _round6(row.get("qty")),
            "warehouse": str(row.get("warehouse") or ""),
            "standard_rate": _round6(row.get("standard_rate")),
            "authorized_rate": _round6(row.get("authorized_rate")),
            "difference": _round6(row.get("difference")),
        })
    return stable


def adjustment_signature(
    request_id: str,
    cashier: str,
    customer: str,
    rows: Iterable[Dict[str, Any]],
) -> str:
    return _sha256_json({
        "request_id": str(request_id or "").strip(),
        "cashier": str(cashier or "").strip(),
        "customer": str(customer or "").strip(),
        "rows": _stable_adjustment_rows(rows),
    })


def _nearest_rates(standard_rate: float, requested_rate: float, variations: Iterable[float]) -> List[float]:
    candidates = sorted(
        {_round6(standard_rate + float(v)) for v in variations if standard_rate + float(v) > 0},
        key=lambda x: (abs(x - requested_rate), x),
    )
    return candidates[:4]


def _context(payload: Any, *, cashier: Optional[str] = None) -> Dict[str, Any]:
    if isinstance(payload, str):
        payload = frappe.parse_json(payload)
    if not isinstance(payload, dict):
        frappe.throw(_("Price authorization payload is invalid."))

    cashier = _require_cashier(cashier)
    request_id = _clean_text(payload.get("request_id"), 120)
    customer = _clean_text(payload.get("customer"), 240)
    if not request_id or not customer:
        frappe.throw(_("Customer and transaction identity are required for price authorization."))

    raw_rows = payload.get("items") or []
    if not isinstance(raw_rows, list) or not raw_rows:
        frappe.throw(_("At least one item is required for price authorization."))

    snapshot = _edge_snapshot() if _runtime_role() == EDGE_ROLE else None
    variations = _edge_variations(snapshot) if snapshot is not None else _configured_variations_primary()
    adjusted = []
    all_rows = []

    for idx, raw in enumerate(raw_rows, start=1):
        if not isinstance(raw, dict):
            frappe.throw(_("Item row {0} is invalid.").format(idx))
        item_code = _clean_text(raw.get("item_code") or raw.get("item"), 240)
        qty = _round6(raw.get("qty") if raw.get("qty") is not None else raw.get("quantity"))
        warehouse = _clean_text(raw.get("warehouse") or raw.get("source_warehouse"), 240)
        if not item_code or qty <= 0 or not warehouse:
            frappe.throw(_("Item, positive quantity and warehouse are required on row {0}.").format(idx))
        standard = _standard_rate(item_code, snapshot)
        if standard <= 0:
            frappe.throw(_("No Standard Selling rate is available for {0}.").format(item_code))
        requested = _round6(raw.get("rate") if raw.get("rate") is not None else raw.get("final_rate"))
        if requested <= 0:
            requested = standard
        difference = _round6(requested - standard)
        base = {
            "line_no": idx,
            "item_code": item_code,
            "qty": qty,
            "warehouse": warehouse,
            "standard_rate": standard,
            "authorized_rate": requested,
            "difference": difference,
        }
        all_rows.append(base)
        if abs(difference) <= RATE_TOLERANCE:
            continue

        preset = any(abs(difference - float(v)) <= RATE_TOLERANCE for v in variations)
        display = dict(base)
        display["classification"] = "Preset" if preset else "Special"
        display["nearest_allowed_rates"] = (
            [] if preset else _nearest_rates(standard, requested, variations)
        )
        adjusted.append(display)

    signature = adjustment_signature(request_id, cashier, customer, adjusted)
    return {
        "request_id": request_id,
        "cashier": cashier,
        "customer": customer,
        "rows": all_rows,
        "adjusted_rows": adjusted,
        "adjustment_signature": signature,
        "variations": [_round6(x) for x in variations],
        "authorization_required": bool(adjusted),
        "runtime_role": _runtime_role(),
    }


def _event(
    event_type: str,
    *,
    cashier: str,
    device_id: str = "",
    request_id: str = "",
    authorized_by: str = "",
    reason: str = "",
    explanation: str = "",
    adjustment_signature_value: str = "",
    adjusted_rows: Optional[List[Dict[str, Any]]] = None,
    remarks: str = "",
) -> str:
    if not frappe.db.exists("DocType", EVENT_DOCTYPE):
        return ""
    doc = frappe.new_doc(EVENT_DOCTYPE)
    doc.event_datetime = now_datetime()
    doc.event_type = event_type
    doc.cashier = cashier
    doc.device_id = _clean_text(device_id, 240)
    doc.request_id = _clean_text(request_id, 140)
    doc.authorization_runtime_role = _runtime_role()
    doc.authorized_by = authorized_by or None
    doc.reason = reason or None
    doc.explanation = explanation or None
    doc.adjustment_signature = adjustment_signature_value or None
    doc.adjustment_summary = _canonical_json(adjusted_rows or []) if adjusted_rows else None
    doc.remarks = _clean_text(remarks, 1000) or None
    doc.flags.ignore_permissions = True
    doc.insert(ignore_permissions=True)
    return doc.name


def _throttle_scope(device_id: str) -> Tuple[str, str]:
    return _session_user(), _clean_text(device_id, 240) or "[unbound]"


def _recent_failure_rows(cashier: str, device_id: str) -> List[Dict[str, Any]]:
    if not frappe.db.exists("DocType", EVENT_DOCTYPE):
        return []
    cutoff = now_datetime() - timedelta(seconds=WRONG_ATTEMPT_WINDOW_SECONDS)
    filters = {
        "cashier": cashier,
        "device_id": device_id,
        "event_type": ["in", ["Wrong PIN", "Blocked"]],
        "event_datetime": [">=", cutoff],
    }
    return frappe.get_all(
        EVENT_DOCTYPE,
        filters=filters,
        fields=["event_datetime", "event_type"],
        order_by="event_datetime desc",
        limit_page_length=100,
    )


def _latest_success(cashier: str, device_id: str):
    if not frappe.db.exists("DocType", EVENT_DOCTYPE):
        return None
    rows = frappe.get_all(
        EVENT_DOCTYPE,
        filters={
            "cashier": cashier,
            "device_id": device_id,
            "event_type": "Authorized",
        },
        fields=["event_datetime"],
        order_by="event_datetime desc",
        limit_page_length=1,
    )
    return rows[0].event_datetime if rows else None


def _check_throttle(device_id: str) -> None:
    cashier, scope_device = _throttle_scope(device_id)
    failures = _recent_failure_rows(cashier, scope_device)
    success_at = _latest_success(cashier, scope_device)
    if success_at:
        failures = [x for x in failures if get_datetime(x.event_datetime) > get_datetime(success_at)]
    wrong = [x for x in failures if x.event_type == "Wrong PIN"]
    if len(wrong) < WRONG_ATTEMPT_LIMIT:
        return
    latest_wrong = max(get_datetime(x.event_datetime) for x in wrong)
    blocked_until = latest_wrong + timedelta(seconds=TEMP_BLOCK_SECONDS)
    if now_datetime() < blocked_until:
        _event(
            "Blocked",
            cashier=cashier,
            device_id=scope_device,
            remarks=f"Temporary Manager PIN block until {blocked_until}.",
        )
        # This endpoint performs no business mutation other than audit. Commit so
        # the blocked attempt remains auditable even though the request raises.
        frappe.db.commit()
        remaining = max(int((blocked_until - now_datetime()).total_seconds()), 1)
        raise frappe.ValidationError(
            _("Too many incorrect Manager PIN attempts. Try again in about {0} minute(s).").format(
                max(1, (remaining + 59) // 60)
            )
        )


def _find_authorizer_for_pin(pin: str) -> Optional[str]:
    # Deliberately no username is supplied by the Cashier. The same small modal
    # works for any active authorized Manager/Owner/Admin/trusted user.
    matched = None
    for row in _authorizers_for_runtime():
        if _verify_pin(
            pin,
            row["credential_salt"],
            row["credential_digest"],
            cint(row["iterations"]) or PBKDF2_ITERATIONS,
        ):
            if matched:
                # Configuration should prevent this. Fail closed if a legacy or
                # manually altered database ever contains duplicate active PINs.
                frappe.throw(_("Manager PIN configuration conflict. Ask an Administrator to reset the PINs."))
            matched = row["user"]
    return matched


@frappe.whitelist()
def authorization_status() -> Dict[str, Any]:
    _require_cashier()
    return {
        "version": VERSION,
        "available": bool(_authorizers_for_runtime()),
        "active_authorizer_count": len(_authorizers_for_runtime()),
        "runtime_role": _runtime_role(),
        "pin_length": 5,
        "wrong_attempt_limit": WRONG_ATTEMPT_LIMIT,
        "temporary_block_seconds": TEMP_BLOCK_SECONDS,
    }


@frappe.whitelist()
def get_price_authorization_context(payload: Any) -> Dict[str, Any]:
    ctx = _context(payload)
    return {
        "authorization_required": ctx["authorization_required"],
        "adjusted_rows": ctx["adjusted_rows"],
        "adjustment_signature": ctx["adjustment_signature"],
        "variations": ctx["variations"],
        "runtime_role": ctx["runtime_role"],
    }


@frappe.whitelist()
def authorize_selling_price_adjustment(
    payload: Any,
    pin: Any,
    reason: str,
    explanation: str = "",
    device_id: str = "",
) -> Dict[str, Any]:
    cashier = _require_cashier()
    pin = _validate_pin_format(pin)
    reason = _clean_text(reason, 140)
    explanation = _clean_text(explanation, 1000)
    device_id = _clean_text(device_id, 240) or "[unbound]"

    if reason not in REASONS:
        frappe.throw(_("Select an approved Manager price-adjustment reason."))

    ctx = _context(payload, cashier=cashier)
    if not ctx["authorization_required"]:
        return {
            "authorized": True,
            "authorization_required": False,
            "token": "",
            "adjustment_signature": ctx["adjustment_signature"],
        }

    if reason == "Other" or any(row.get("classification") == "Special" for row in ctx["adjusted_rows"]):
        if not explanation:
            frappe.throw(_("A typed explanation is required for Other or a special/unrecognized rate."))

    if not _authorizers_for_runtime():
        frappe.throw(
            _("No active Manager selling-price PIN is configured for this runtime. Ask an NKT Owner / Administrator.")
        )

    _check_throttle(device_id)
    authorizer = _find_authorizer_for_pin(pin)
    if not authorizer:
        _event(
            "Wrong PIN",
            cashier=cashier,
            device_id=device_id,
            request_id=ctx["request_id"],
            adjustment_signature_value=ctx["adjustment_signature"],
            adjusted_rows=ctx["adjusted_rows"],
            remarks="Incorrect five-digit Manager PIN.",
        )
        frappe.db.commit()
        raise frappe.ValidationError(_("Incorrect Manager PIN."))

    issued_at = int(time.time())
    proof = {
        "version": VERSION,
        "issued_at": issued_at,
        "expires_at": issued_at + TOKEN_TTL_SECONDS,
        "request_id": ctx["request_id"],
        "cashier": cashier,
        "customer": ctx["customer"],
        "authorized_by": authorizer,
        "reason": reason,
        "explanation": explanation,
        "adjustment_signature": ctx["adjustment_signature"],
        "runtime_role": _runtime_role(),
        "device_id": device_id,
        "authorized_on": str(now_datetime()),
    }
    token = _issue_token(proof)
    _event(
        "Authorized",
        cashier=cashier,
        device_id=device_id,
        request_id=ctx["request_id"],
        authorized_by=authorizer,
        reason=reason,
        explanation=explanation,
        adjustment_signature_value=ctx["adjustment_signature"],
        adjusted_rows=ctx["adjusted_rows"],
        remarks="One-transaction selling-price authorization issued.",
    )
    return {
        "authorized": True,
        "authorization_required": True,
        "authorized_by": authorizer,
        "authorized_on": now_datetime(),
        "reason": reason,
        "explanation": explanation,
        "adjustment_signature": ctx["adjustment_signature"],
        "token": token,
        "expires_in_seconds": TOKEN_TTL_SECONDS,
        "runtime_role": _runtime_role(),
    }


def _token_authorizer_still_active(user: str) -> bool:
    if _runtime_role() == EDGE_ROLE:
        return any(row["user"] == user for row in _edge_authorizers())
    return any(row["user"] == user for row in _primary_authorizers())


def validate_price_authorization_for_finalize(payload: Any) -> Optional[Dict[str, Any]]:
    if isinstance(payload, str):
        payload = frappe.parse_json(payload)
    if not isinstance(payload, dict):
        frappe.throw(_("Fast Screen payload is invalid."))

    ctx = _context(payload)
    if not ctx["authorization_required"]:
        return None

    token = str(payload.get("price_authorization_token") or "").strip()
    if not token:
        frappe.throw(_("Manager PIN authorization is required for the adjusted selling rate."))

    proof = _read_token(token)
    expected = {
        "request_id": ctx["request_id"],
        "cashier": ctx["cashier"],
        "customer": ctx["customer"],
        "adjustment_signature": ctx["adjustment_signature"],
        "runtime_role": _runtime_role(),
    }
    for key, value in expected.items():
        if str(proof.get(key) or "") != str(value or ""):
            frappe.throw(_("Transaction details changed after Manager authorization. Authorize the adjusted rates again."))

    authorizer = str(proof.get("authorized_by") or "")
    if not authorizer or not _token_authorizer_still_active(authorizer):
        frappe.throw(_("The approving Manager PIN is no longer active. Authorize the transaction again."))

    return {
        "authorized_by": authorizer,
        "authorized_on": get_datetime(proof.get("authorized_on")) if proof.get("authorized_on") else now_datetime(),
        "reason": str(proof.get("reason") or ""),
        "explanation": str(proof.get("explanation") or ""),
        "adjustment_signature": ctx["adjustment_signature"],
        "authorization_runtime_role": _runtime_role(),
        "authorization_device_id": str(proof.get("device_id") or ""),
    }


def offline_evidence(evidence: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not evidence:
        return None
    out = {}
    for key in OFFLINE_EVIDENCE_KEYS:
        value = evidence.get(key)
        if value not in (None, ""):
            if isinstance(value, datetime):
                value = value.isoformat(sep=" ", timespec="seconds")
            out[key] = value
    return out or None


def _doc_adjusted_rows(doc) -> List[Dict[str, Any]]:
    rows = []
    for idx, row in enumerate(doc.get("items") or [], start=1):
        standard = _round6(row.get("standard_rate"))
        special = _round6(row.get("custom_nkt_authorized_special_rate"))
        rate = special if special > RATE_TOLERANCE else _round6(standard + flt(row.get("price_adjustment")))
        difference = _round6(rate - standard)
        if abs(difference) <= RATE_TOLERANCE:
            continue
        rows.append({
            "line_no": idx,
            "item_code": str(row.get("item") or ""),
            "qty": _round6(row.get("quantity")),
            "warehouse": str(row.get("source_warehouse") or ""),
            "standard_rate": standard,
            "authorized_rate": rate,
            "difference": difference,
        })
    return rows


def document_adjustment_signature(doc) -> str:
    request_id = str(doc.get("custom_nkt_fast_request_id") or doc.get("name") or "").strip()
    return adjustment_signature(
        request_id,
        str(doc.get("cashier") or ""),
        str(doc.get("customer") or ""),
        _doc_adjusted_rows(doc),
    )


def apply_evidence_to_sale(doc, evidence: Optional[Dict[str, Any]]) -> None:
    if not evidence:
        return
    mapping = {
        "custom_nkt_price_authorized_by": evidence.get("authorized_by"),
        "custom_nkt_price_authorized_on": evidence.get("authorized_on"),
        "custom_nkt_price_authorization_reason": evidence.get("reason"),
        "custom_nkt_price_authorization_explanation": evidence.get("explanation"),
        "custom_nkt_price_authorization_signature": evidence.get("adjustment_signature"),
        "custom_nkt_price_authorization_source": evidence.get("authorization_runtime_role") or _runtime_role(),
        "custom_nkt_price_authorization_device_id": evidence.get("authorization_device_id"),
    }
    for fieldname, value in mapping.items():
        if doc.meta.has_field(fieldname):
            doc.set(fieldname, value)


def validate_document_authorization_evidence(doc) -> None:
    adjusted = _doc_adjusted_rows(doc)
    if not adjusted:
        return

    evidence = doc.flags.get("nkt_price_authorization_evidence")
    if not evidence:
        # C15C offline materializers are server-internal trusted paths. MP1
        # preserves sanitized immutable authorization evidence in the Tender
        # Intent; MP2 will bind that evidence into the final materialized sale
        # and close this intentionally narrow compatibility allowance.
        if doc.flags.get("nkt_c15c_preserve_offline_cashier"):
            return
        frappe.throw(_("Manager PIN authorization is required before an adjusted Cashier Sale can be submitted."))

    expected = document_adjustment_signature(doc)
    if str(evidence.get("adjustment_signature") or "") != expected:
        frappe.throw(_("Adjusted selling-rate details no longer match the Manager authorization evidence."))

    if not evidence.get("authorized_by") or not evidence.get("reason"):
        frappe.throw(_("Manager price authorization evidence is incomplete."))
    apply_evidence_to_sale(doc, evidence)


def _sync_user_flag(user: str, enabled: bool) -> None:
    if frappe.get_meta("User").has_field("custom_nkt_can_authorize_selling_price_adjustments"):
        frappe.db.set_value(
            "User",
            user,
            "custom_nkt_can_authorize_selling_price_adjustments",
            1 if enabled else 0,
            update_modified=False,
        )


def _credential_doc(user: str):
    if frappe.db.exists(AUTHORIZER_DOCTYPE, user):
        return frappe.get_doc(AUTHORIZER_DOCTYPE, user)
    doc = frappe.new_doc(AUTHORIZER_DOCTYPE)
    doc.user = user
    doc.can_authorize_selling_price_adjustments = 0
    doc.pin_configured = 0
    return doc


def _assert_unique_active_pin(pin: str, exclude_user: str = "") -> None:
    for row in _primary_authorizers():
        if row["user"] == exclude_user:
            continue
        if _verify_pin(pin, row["credential_salt"], row["credential_digest"], row["iterations"]):
            frappe.throw(_("That five-digit PIN is already assigned to another active authorizer. Choose a unique PIN."))


@frappe.whitelist()
def set_authorizer_pin(user: str, pin: Any) -> Dict[str, Any]:
    changed_by = _require_config_authority()
    user = _clean_text(user, 240)
    if not user or not frappe.db.exists("User", user):
        frappe.throw(_("Select an existing User."))
    if not cint(frappe.db.get_value("User", user, "enabled")):
        frappe.throw(_("The selected User is disabled. Enable the User account before assigning a Manager PIN."))

    pin = _validate_pin_format(pin)
    _assert_unique_active_pin(pin, exclude_user=user)

    salt = _encode(os.urandom(16))
    digest = _hash_pin(pin, salt, PBKDF2_ITERATIONS)
    doc = _credential_doc(user)
    doc.flags.nkt_manager_pin_internal = True
    doc.can_authorize_selling_price_adjustments = 1
    doc.pin_configured = 1
    doc.pin_salt = salt
    doc.pin_hash = digest
    doc.pin_iterations = PBKDF2_ITERATIONS
    doc.last_pin_change = now_datetime()
    doc.last_changed_by = changed_by
    doc.flags.ignore_permissions = True
    if doc.is_new():
        doc.insert(ignore_permissions=True)
    else:
        doc.save(ignore_permissions=True)
    _sync_user_flag(user, True)

    _event(
        "PIN Changed",
        cashier=changed_by,
        authorized_by=user,
        remarks="Selling-price authorizer PIN configured/changed. PIN value was not stored in audit.",
    )
    return {
        "ok": True,
        "user": user,
        "enabled": True,
        "pin_configured": True,
        "message": _("Five-digit Manager PIN configured. The PIN value is never displayed or stored in plain text."),
    }


@frappe.whitelist()
def disable_authorizer(user: str) -> Dict[str, Any]:
    changed_by = _require_config_authority()
    user = _clean_text(user, 240)
    if not frappe.db.exists(AUTHORIZER_DOCTYPE, user):
        frappe.throw(_("Selling-price authorizer record does not exist."))
    doc = frappe.get_doc(AUTHORIZER_DOCTYPE, user)
    doc.flags.nkt_manager_pin_internal = True
    doc.can_authorize_selling_price_adjustments = 0
    doc.last_changed_by = changed_by
    doc.flags.ignore_permissions = True
    doc.save(ignore_permissions=True)
    _sync_user_flag(user, False)
    _event(
        "Authorizer Disabled",
        cashier=changed_by,
        authorized_by=user,
        remarks="Selling-price PIN authorization disabled. Existing transaction proofs expire normally and are rejected if revalidated.",
    )
    return {"ok": True, "user": user, "enabled": False}


def _install_custom_fields() -> None:
    custom_fields = {
        "User": [
            {
                "fieldname": "custom_nkt_can_authorize_selling_price_adjustments",
                "label": "Can Authorize Selling Price Adjustments",
                "fieldtype": "Check",
                "default": "0",
                "read_only": 1,
                "description": "Controlled by NKT Selling Price Authorizer. Indicates an active five-digit Manager PIN credential.",
            },
        ],
        "NKT Cashier Sale": [
            {
                "fieldname": "custom_nkt_price_authorized_by",
                "label": "Price Authorized By",
                "fieldtype": "Link",
                "options": "User",
                "read_only": 1,
                "hidden": 1,
            },
            {
                "fieldname": "custom_nkt_price_authorized_on",
                "label": "Price Authorized On",
                "fieldtype": "Datetime",
                "read_only": 1,
                "hidden": 1,
            },
            {
                "fieldname": "custom_nkt_price_authorization_reason",
                "label": "Price Authorization Reason",
                "fieldtype": "Data",
                "read_only": 1,
                "hidden": 1,
            },
            {
                "fieldname": "custom_nkt_price_authorization_explanation",
                "label": "Price Authorization Explanation",
                "fieldtype": "Small Text",
                "read_only": 1,
                "hidden": 1,
            },
            {
                "fieldname": "custom_nkt_price_authorization_signature",
                "label": "Price Authorization Signature",
                "fieldtype": "Data",
                "read_only": 1,
                "hidden": 1,
            },
            {
                "fieldname": "custom_nkt_price_authorization_source",
                "label": "Price Authorization Source",
                "fieldtype": "Data",
                "read_only": 1,
                "hidden": 1,
            },
            {
                "fieldname": "custom_nkt_price_authorization_device_id",
                "label": "Price Authorization Device",
                "fieldtype": "Data",
                "read_only": 1,
                "hidden": 1,
            },
        ],
        "NKT Cashier Sale Item": [
            {
                "fieldname": "custom_nkt_authorized_special_rate",
                "label": "Authorized Special Rate",
                "fieldtype": "Currency",
                "default": "0",
                "read_only": 1,
                "hidden": 1,
                "description": "Non-preset Cashier rate authorized through the five-digit Manager PIN flow.",
            },
        ],
        "NKT Customer Order Item": [
            {
                "fieldname": "custom_nkt_authorized_special_rate",
                "label": "Authorized Special Rate",
                "fieldtype": "Currency",
                "default": "0",
                "read_only": 1,
                "hidden": 1,
                "description": "Allows the Encoder to independently reproduce a legitimate special Cashier rate for exact matching.",
            },
        ],
    }
    create_custom_fields(custom_fields, update=True)


def _sync_cashier_client_script() -> None:
    path = Path(__file__).with_name("nkt_cashier_fast_screen_v2.js")
    text = path.read_text(encoding="utf-8")
    values = {
        "dt": CASHIER_SCREEN,
        "view": "Form",
        "enabled": 1,
        "script": text,
    }
    if frappe.db.exists("Client Script", CASHIER_SCRIPT):
        doc = frappe.get_doc("Client Script", CASHIER_SCRIPT)
        doc.update(values)
        doc.flags.ignore_permissions = True
        doc.save(ignore_permissions=True)
    else:
        doc = frappe.new_doc("Client Script")
        doc.name = CASHIER_SCRIPT
        doc.update(values)
        doc.flags.ignore_permissions = True
        doc.insert(ignore_permissions=True)


def _sync_encoder_client_script_without_overwrite() -> None:
    source_path = Path(__file__).with_name("nkt_encoder_fast_screen_v2.js")
    source_text = source_path.read_text(encoding="utf-8")
    if frappe.db.exists("Client Script", ENCODER_SCRIPT):
        doc = frappe.get_doc("Client Script", ENCODER_SCRIPT)
        text = str(doc.script or "")
        old = """    const adjusted = state.rows.find(r => Math.abs(Number(r.rate || 0) - Number(r.standard_rate || 0)) > 0.000001);
    if (adjusted) {
      frappe.msgprint({
        title: __('Price authorization is not live yet'),
        indicator: 'orange',
        message: __('V2.0C.3 posts standard-rate transactions only. Reset adjusted rates to Normal. Manager-PIN authorization is the next controlled subpatch.')
      });
      return;
    }
"""
        marker = """    // NKT_MANAGER_PIN_ENCODER_MP1
    // Encoder independently re-enters/confirms selling rate; Manager PIN is Cashier-side only.
"""
        if old in text:
            text = text.replace(old, marker, 1)
        elif "NKT_MANAGER_PIN_ENCODER_MP1" not in text:
            frappe.throw(_("Current Encoder Fast Screen runtime script does not match the accepted Manager-PIN patch anchor."))
        doc.script = text
        doc.enabled = 1
        doc.flags.ignore_permissions = True
        doc.save(ignore_permissions=True)
    else:
        doc = frappe.new_doc("Client Script")
        doc.name = ENCODER_SCRIPT
        doc.dt = ENCODER_SCREEN
        doc.view = "Form"
        doc.enabled = 1
        doc.script = source_text
        doc.flags.ignore_permissions = True
        doc.insert(ignore_permissions=True)


def install() -> Dict[str, Any]:
    _install_custom_fields()
    _sync_cashier_client_script()
    _sync_encoder_client_script_without_overwrite()
    frappe.clear_cache()
    return {
        "installed": True,
        "version": VERSION,
        "cashier_script": CASHIER_SCRIPT,
        "encoder_script": ENCODER_SCRIPT,
    }


def after_migrate() -> None:
    install()


def _crypto_self_test() -> Dict[str, Any]:
    salt = _encode(b"0123456789abcdef")
    digest = _hash_pin("12345", salt, 10_000)
    pin_ok = _verify_pin("12345", salt, digest, 10_000)
    wrong_rejected = not _verify_pin("54321", salt, digest, 10_000)
    now_ts = int(time.time())
    payload = {
        "version": VERSION,
        "issued_at": now_ts,
        "expires_at": now_ts + 60,
        "request_id": "00000000-0000-4000-8000-000000000001",
        "cashier": "test@example.com",
        "customer": "TEST",
        "authorized_by": "manager@example.com",
        "reason": REASONS[0],
        "explanation": "",
        "adjustment_signature": "a" * 64,
        "runtime_role": _runtime_role(),
        "device_id": "[test]",
    }
    token = _issue_token(payload)
    decoded = _read_token(token)
    token_ok = decoded.get("adjustment_signature") == "a" * 64
    return {
        "pbkdf2_correct_pin": bool(pin_ok),
        "pbkdf2_wrong_pin_rejected": bool(wrong_rejected),
        "signed_proof_round_trip": bool(token_ok),
    }


@frappe.whitelist()
def verify() -> Dict[str, Any]:
    errors = []

    for doctype in (AUTHORIZER_DOCTYPE, EVENT_DOCTYPE):
        if not frappe.db.exists("DocType", doctype):
            errors.append(f"Missing DocType: {doctype}")

    required_fields = {
        "User": ["custom_nkt_can_authorize_selling_price_adjustments"],
        "NKT Cashier Sale": [
            "custom_nkt_price_authorized_by",
            "custom_nkt_price_authorized_on",
            "custom_nkt_price_authorization_reason",
            "custom_nkt_price_authorization_explanation",
            "custom_nkt_price_authorization_signature",
            "custom_nkt_price_authorization_source",
            "custom_nkt_price_authorization_device_id",
        ],
        "NKT Cashier Sale Item": ["custom_nkt_authorized_special_rate"],
        "NKT Customer Order Item": ["custom_nkt_authorized_special_rate"],
    }
    field_status = {}
    for doctype, fields in required_fields.items():
        meta = frappe.get_meta(doctype)
        field_status[doctype] = {}
        for fieldname in fields:
            present = bool(meta.has_field(fieldname))
            field_status[doctype][fieldname] = present
            if not present:
                errors.append(f"Missing field {doctype}.{fieldname}")

    cash_script = str(frappe.db.get_value("Client Script", CASHIER_SCRIPT, "script") or "")
    enc_script = str(frappe.db.get_value("Client Script", ENCODER_SCRIPT, "script") or "")
    client_scripts = {
        "cashier_manager_pin_marker": "NKT_MANAGER_PIN_MP1" in cash_script,
        "cashier_old_block_removed": "Price authorization is not live yet" not in cash_script,
        "encoder_manager_pin_marker": "NKT_MANAGER_PIN_ENCODER_MP1" in enc_script,
        "encoder_old_block_removed": "Price authorization is not live yet" not in enc_script,
    }
    for key, ok in client_scripts.items():
        if not ok:
            errors.append(f"Client Script check failed: {key}")

    event_meta = frappe.get_meta(EVENT_DOCTYPE) if frappe.db.exists("DocType", EVENT_DOCTYPE) else None
    forbidden_event_fields = []
    if event_meta:
        for fieldname in ("pin", "pin_hash", "pin_salt", "token", "credential_digest", "credential_salt"):
            if event_meta.has_field(fieldname):
                forbidden_event_fields.append(fieldname)
    if forbidden_event_fields:
        errors.append("Authorization Event contains forbidden secret fields: " + ", ".join(forbidden_event_fields))

    crypto = _crypto_self_test()
    if not all(crypto.values()):
        errors.append("Cryptographic self-test failed.")

    source_markers = {}
    marker_specs = {
        "fast_screen_backend.py": "NKT_MANAGER_PIN_FAST_UI_MP1",
        "fast_screen_routing.py": "NKT_MANAGER_PIN_EDGE_WIRING_MP1",
        "features/offline_edge/internal/edge_read_model.py": "NKT_MANAGER_PIN_EDGE_SNAPSHOT_MP1",
        "features/payments_accounts/internal/cashier_tender_intent.py": "NKT_MANAGER_PIN_TENDER_EVIDENCE_MP1",
    }
    base = Path(__file__).resolve().parent
    for filename, marker in marker_specs.items():
        path = base / filename
        ok = path.is_file() and marker in path.read_text(encoding="utf-8")
        source_markers[filename] = ok
        if not ok:
            errors.append(f"Source marker missing: {filename}")

    return {
        "passed": not errors,
        "marker": "PASS_MANAGER_PIN_RATE_AUTHORIZATION_MP1_FOUNDATION" if not errors else "FAIL_MANAGER_PIN_RATE_AUTHORIZATION_MP1_FOUNDATION",
        "version": VERSION,
        "errors": errors,
        "crypto": crypto,
        "field_status": field_status,
        "client_scripts": client_scripts,
        "source_markers": source_markers,
        "active_authorizer_count_primary": len(_primary_authorizers()) if _runtime_role() == PRIMARY_ROLE else None,
        "configured_reasons": list(REASONS),
        "wrong_attempt_policy": {
            "limit": WRONG_ATTEMPT_LIMIT,
            "window_seconds": WRONG_ATTEMPT_WINDOW_SECONDS,
            "block_seconds": TEMP_BLOCK_SECONDS,
        },
        "offline_materialization_binding": "DEFERRED_TO_MP2; MP1 preserves sanitized immutable tender evidence and does not block the trusted C15C materializer.",
    }
