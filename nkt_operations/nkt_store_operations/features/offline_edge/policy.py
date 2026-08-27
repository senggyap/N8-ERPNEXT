from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, Optional
from uuid import UUID
from zoneinfo import ZoneInfo

import frappe
from frappe.utils import getdate

FOUNDATION_VERSION = "C15C.2-R1"
BUSINESS_TIMEZONE = "Asia/Manila"
PH_TZ = ZoneInfo(BUSINESS_TIMEZONE)

# These are implementation-stage facts, not a permanent statement that a
# family can never become offline-capable. C15C.2 enables NO offline writes.
KNOWN_CRITICAL_FAMILIES = {
    "NKT Payment Receipt",
    "NKT Cashier Movement",
    "NKT Customer Receivable",
    "NKT Warehouse Release",
    "NKT Trucker Payment",
    "NKT Trucking Customer Collection",
    "NKT Driver Incentive Batch",
}
KNOWN_HIGH_RISK_FAMILIES = {
    "NKT Customer Order",
    "NKT Cashier Sale",
}

KNOWN_NONCRITICAL_OFFLINE_FAMILIES = {
    "NKT Safe Sync Probe",
}

KNOWN_OFFLINE_INTENT_FAMILIES = {
    "NKT Customer Order Intent",
}

KNOWN_OFFLINE_TENDER_INTENT_FAMILIES = {
    "NKT Cashier Tender Intent",
}

KNOWN_OFFLINE_ENCODER_SETTLEMENT_INTENT_FAMILIES = {
    "NKT Encoder Settlement Intent",
}

KNOWN_OFFLINE_WAREHOUSE_RELEASE_INTENT_FAMILIES = {
    "NKT Warehouse Release Intent",
}

KNOWN_OFFLINE_CASH_DRAWER_ADJUSTMENT_INTENT_FAMILIES = {
    "NKT Cash Drawer Adjustment Intent",
}

KNOWN_OFFLINE_WAREHOUSE_TRANSFER_DISPATCH_INTENT_FAMILIES = {
    "NKT Warehouse Transfer Dispatch Intent",
}

KNOWN_OFFLINE_WAREHOUSE_TRANSFER_ARRIVAL_INTENT_FAMILIES = {
    "NKT Warehouse Transfer Arrival Intent",
}

KNOWN_OFFLINE_SUPPLIER_RECEIVING_PHYSICAL_INTENT_FAMILIES = {
    "NKT Supplier Receiving Physical Intent",
}

KNOWN_OFFLINE_RETURN_EXCHANGE_DECLARATION_INTENT_FAMILIES = {
    "NKT Return Exchange Declaration Intent",
}

KNOWN_OFFLINE_PHYSICAL_INVENTORY_COUNT_INTENT_FAMILIES = {
    "NKT Physical Inventory Count Intent",
}

KNOWN_OFFLINE_SHIFT_CLOSE_ZOUT_INTENT_FAMILIES = {
    "NKT Cashier Shift Open Intent",
    "NKT Cashier Shift Close Intent",
    "NKT Encoder Z-Out Finalization Intent",
}

KNOWN_OFFLINE_TRUCKING_TRIP_LIFECYCLE_INTENT_FAMILIES = {
    "NKT Trucking Trip Lifecycle Intent",
}

TERMINAL_DEVICE_STATUSES = {"Revoked", "Lost/Stolen", "Retired"}
PRIVILEGED_ROLES = {"NKT OWNER", "NKT ADMINISTRATOR", "System Manager"}


def _valid_uuid(value: str) -> bool:
    try:
        UUID(str(value))
        return True
    except Exception:
        return False


def _require_authenticated(user: Optional[str] = None) -> str:
    user = user or frappe.session.user
    if not user or user == "Guest":
        raise frappe.PermissionError("Device access unavailable.")
    return user


def _roles(user: str) -> set[str]:
    return set(frappe.get_roles(user) or [])


def is_privileged_user(user: Optional[str] = None) -> bool:
    user = _require_authenticated(user)
    return bool(_roles(user) & PRIVILEGED_ROLES)


def _device_row(device_id: str) -> Dict[str, Any]:
    if not _valid_uuid(device_id):
        raise frappe.PermissionError("Device access unavailable.")
    row = frappe.db.get_value(
        "NKT Device Registry",
        device_id,
        [
            "device_id",
            "device_class",
            "operational_context",
            "assigned_user",
            "status",
            "policy_version",
        ],
        as_dict=True,
    )
    if not row:
        raise frappe.PermissionError("Device access unavailable.")
    return dict(row)


def user_security_snapshot(user: Optional[str] = None) -> Dict[str, Any]:
    user = _require_authenticated(user)
    if not frappe.db.exists("DocType", "NKT User Security State"):
        return {
            "user": user,
            "status": "Active",
            "policy_version": 0,
        }

    row = frappe.db.get_value(
        "NKT User Security State",
        user,
        ["user", "status", "policy_version"],
        as_dict=True,
    )
    if not row:
        return {
            "user": user,
            "status": "Active",
            "policy_version": 0,
        }

    return {
        "user": row.user,
        "status": row.status or "Active",
        "policy_version": int(row.policy_version or 1),
    }


def device_policy_snapshot(
    device_id: str,
    *,
    user: Optional[str] = None,
    requested_context: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Return only the minimum policy state needed by an approved NKT client.

    This deliberately does not return restriction/revocation reasons,
    credential fingerprints, notes, or business datasets.
    """
    user = _require_authenticated(user)
    row = _device_row(device_id)
    privileged = is_privileged_user(user)

    if row["status"] in TERMINAL_DEVICE_STATUSES:
        # Generic denial is deliberate: do not explain theft/revocation state
        # to a person holding the device.
        raise frappe.PermissionError("Device access unavailable.")

    assigned = row.get("assigned_user")
    if assigned and assigned != user and not privileged:
        raise frappe.PermissionError("Device access unavailable.")

    requested_context = requested_context or row.get("operational_context")
    if (
        requested_context
        and requested_context != row.get("operational_context")
        and not (privileged and row.get("operational_context") == "Owner/Admin")
    ):
        raise frappe.PermissionError("Device access unavailable.")

    user_policy = user_security_snapshot(user)
    limited = (
        row.get("status") == "Restricted"
        or user_policy.get("status") == "Restricted"
    )

    return {
        "device_id": row["device_id"],
        "device_class": row.get("device_class"),
        "operational_context": row.get("operational_context"),
        "policy_version": int(row.get("policy_version") or 1),
        "user_policy_version": int(user_policy.get("policy_version") or 0),
        # User-facing clients should render this simply as the permitted
        # limited interface; they should not announce the security rationale.
        "ui_mode": "limited" if limited else "normal",
        "authenticated": True,
    }


def touch_last_seen(device_id: str, *, user: Optional[str] = None) -> None:
    device_policy_snapshot(device_id, user=user)
    frappe.db.set_value(
        "NKT Device Registry",
        device_id,
        "last_seen_at",
        frappe.utils.now(),
        update_modified=False,
    )


def _coerce_manila(value: Any) -> datetime:
    if isinstance(value, datetime):
        dt = value
    else:
        raw = str(value).strip()
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(raw)
        except Exception as exc:
            raise frappe.ValidationError("Invalid business-time value.") from exc

    if dt.tzinfo is None:
        return dt.replace(tzinfo=PH_TZ)
    return dt.astimezone(PH_TZ)


def manila_now() -> datetime:
    return datetime.now(PH_TZ)


def validate_business_time(business_date: Any, settled_at: Any) -> Dict[str, Any]:
    settled = _coerce_manila(settled_at)
    business = getdate(business_date)
    if business != settled.date():
        raise frappe.ValidationError(
            "Business Date must equal the Asia/Manila date of Business / Settled Time."
        )
    return {
        "business_date": business.isoformat(),
        "settled_at_manila": settled.isoformat(),
        "timezone": BUSINESS_TIMEZONE,
    }


def observe_clock_delta_seconds(client_observed_at: Any, trusted_observed_at: Any = None) -> float:
    """
    Measure, but do not hard-block on, clock difference.

    The final warning/block threshold is intentionally deferred. A queued
    offline event can arrive much later than its settled time, so upload delay
    must never be misclassified as clock tampering.
    """
    client = _coerce_manila(client_observed_at)
    trusted = _coerce_manila(trusted_observed_at) if trusted_observed_at else manila_now()
    return (client - trusted).total_seconds()


def event_family_policy(event_family: str) -> Dict[str, Any]:
    family = str(event_family or "").strip()
    if family in KNOWN_CRITICAL_FAMILIES:
        risk = "Critical"
        offline_allowed = False
        stage = FOUNDATION_VERSION
    elif family in KNOWN_HIGH_RISK_FAMILIES:
        risk = "High"
        offline_allowed = False
        stage = FOUNDATION_VERSION
    elif family in KNOWN_NONCRITICAL_OFFLINE_FAMILIES:
        risk = "Non-Critical Probe"
        offline_allowed = True
        stage = "C15C.9A-R1"
    elif family in KNOWN_OFFLINE_INTENT_FAMILIES:
        risk = "High - Intent Only"
        offline_allowed = True
        stage = "C15C.9G-R1"
    elif family in KNOWN_OFFLINE_TENDER_INTENT_FAMILIES:
        # C15C.10A promotes only the immutable tender-intent journal/queue.
        # It does NOT permit canonical Cashier Sale, Payment Receipt,
        # Cashier Movement, Receivable, matching, or stock mutation at Edge.
        risk = "Critical - Tender Intent Only"
        offline_allowed = True
        stage = "C15C.10A-R4"
    elif family in KNOWN_OFFLINE_ENCODER_SETTLEMENT_INTENT_FAMILIES:
        # C15C.10D R2 promotes only the Encoder's independent settlement/account
        # declaration plus Primary Draft hydration. It does NOT submit the
        # Customer Order, create Receivable/Advance, match, release, or move stock.
        risk = "Critical - Encoder Settlement Intent Only"
        offline_allowed = True
        stage = "C15C.10D-R2"
    elif family in KNOWN_OFFLINE_WAREHOUSE_RELEASE_INTENT_FAMILIES:
        # C15C.10E R2 records the warehouse operator's immutable physical-release
        # declaration plus a technical Store-Edge quantity projection. It does
        # NOT submit NKT Warehouse Release or create Stock Entry at Edge.
        risk = "Critical - Physical Warehouse Release Intent Only"
        offline_allowed = True
        stage = "C15C.10E-R2"
    elif family in KNOWN_OFFLINE_CASH_DRAWER_ADJUSTMENT_INTENT_FAMILIES:
        # C15C.10F R3 records only the Cashier's immutable manual drawer
        # adjustment declaration plus a technical Store-Edge cash projection.
        # It does NOT create/submit NKT Cash Drawer Adjustment or NKT Cashier
        # Movement at Edge. Controlled reversals remain Primary/Admin only.
        risk = "Critical - Cash Drawer Adjustment Intent Only"
        offline_allowed = True
        stage = "C15C.10F-R3"
    elif family in KNOWN_OFFLINE_WAREHOUSE_TRANSFER_DISPATCH_INTENT_FAMILIES:
        # C15C.10G promotes only the immutable source physical-dispatch intent
        # plus source/GIT Edge projection. Canonical NKT Warehouse Transfer and
        # ERPNext Stock Entry materialization remain Primary-owned and must use
        # the accepted release_transfer() business controller.
        risk = "Critical - Internal Transfer Source Dispatch Intent Only"
        offline_allowed = True
        stage = "C15C.10G-R7"
    elif family in KNOWN_OFFLINE_WAREHOUSE_TRANSFER_ARRIVAL_INTENT_FAMILIES:
        # Destination Arrival remains a physical Warehouse action. Store Edge
        # may preserve only the immutable arrival declaration + GIT/Destination
        # projection. Canonical incoming Stock Entry remains Primary-owned and
        # must later materialize through the accepted receive_transfer() engine.
        risk = "Critical - Internal Transfer Destination Arrival Intent Only"
        offline_allowed = True
        stage = "C15C.10G-R10"
    elif family in KNOWN_OFFLINE_SUPPLIER_RECEIVING_PHYSICAL_INTENT_FAMILIES:
        # C15C.10H R3 registers only the immutable physical supplier-receiving
        # intent for safe-sync transport. The Edge may preserve the true physical
        # Manila receiving date/time even when replication reaches Primary later.
        # This does NOT grant an employee backdate control: receiving_date must
        # match the immutable Edge event business date / settled timestamp.
        # Supplier money-side consequences and canonical Purchase Receipt/stock
        # remain Primary-owned. Accepted-goods Edge availability is a locked
        # operating rule but is not enabled until the dedicated projection stage.
        risk = "Critical - Physical Supplier Receiving Intent Only"
        offline_allowed = True
        stage = "C15C.10H-R3"
    elif family in KNOWN_OFFLINE_RETURN_EXCHANGE_DECLARATION_INTENT_FAMILIES:
        # C15C.10I R3 registers only the immutable side-aware Cashier/Encoder
        # Return/Exchange declaration intent. Matching remains reconciliation
        # after operation, not an operating permission gate.
        #
        # Canonical C7 posting remains Primary-owned: no NKT Return Exchange
        # Declaration, NEW Cashier Sale, NEW Customer Order, Stock Entry,
        # Cashier Movement/refund, Customer Credit, Account Adjustment, GL/SLE,
        # matching result, or controlled reversal is created by generic Edge
        # transport. Controlled reversal remains Primary/online-only.
        #
        # True physical cross-midnight event time and the Cashier's original
        # shift may be preserved automatically by Store Edge; this does not
        # introduce employee manual backdating.
        risk = "Critical - Return/Exchange Declaration Intent Only"
        offline_allowed = True
        stage = "C15C.10I-R3"
    elif family in KNOWN_OFFLINE_PHYSICAL_INVENTORY_COUNT_INTENT_FAMILIES:
        # C15C.10J R5 permits only an immutable physical-count observation.
        # Store Edge does not author system/book quantity, variance, valuation,
        # Stock Reconciliation, Stock Ledger Entry, Bin truth, or a historical
        # stock adjustment. Primary preserves the observation exactly once and
        # leaves it pending reconciliation until the later rebase/materialization
        # policy explicitly proves it is safe to post.
        risk = "Critical - Physical Inventory Count Intent Only"
        offline_allowed = True
        stage = "C15C.10J-R5"
    elif family in KNOWN_OFFLINE_SHIFT_CLOSE_ZOUT_INTENT_FAMILIES:
        # C15C.10K R3 permits immutable lifecycle intent only:
        # - Cashier shift open at Store Edge,
        # - Cashier physical close/count at Store Edge,
        # - official finalized Encoder Z-Out frozen snapshot at Store Edge.
        #
        # Edge Expected Cash remains provisional. Primary may later reconcile
        # expected cash/Z-Out differences but may never rewrite the physical
        # denomination count or the official offline Z-Out snapshot.
        risk = "Critical - Shift Close / Official Z-Out Intent Only"
        offline_allowed = True
        stage = "C15C.10K-R3"
    elif family in KNOWN_OFFLINE_TRUCKING_TRIP_LIFECYCLE_INTENT_FAMILIES:
        # C15C.10L R3 enables only the immutable physical Trucking Trip
        # lifecycle intent. Store Edge may preserve new trip creation and
        # physical status/paperwork observations, but it may not author
        # customer billing, external-carrier payable/SOA/payment, or driver
        # incentive payout truth. External supplier-arrival receiving remains
        # the already-accepted C15C.10H physical receipt truth.
        risk = "Critical - Trucking Physical Lifecycle Intent Only"
        offline_allowed = True
        stage = "C15C.10L-R3"
    else:
        risk = "Unclassified / not enabled"
        offline_allowed = False
        stage = FOUNDATION_VERSION

    # Secure default remains deny. Only an explicitly named family can become
    # offline-capable, and critical/high-risk business families remain false.
    return {
        "event_family": family,
        "risk": risk,
        "offline_write_allowed": offline_allowed,
        "stage": stage,
    }


def submission_contract() -> Dict[str, Any]:
    return {
        "business_commit_unit": "one atomic business transaction",
        "frontline_ack": "immediate durable acknowledgement",
        "frontline_must_not_wait_for_replication_batch": True,
        "background_replication_may_batch_transport": True,
        "event_identity_remains_per_transaction": True,
        "critical_offline_mutations_enabled": False,
    }
