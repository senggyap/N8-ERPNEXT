from __future__ import annotations

import hashlib
import json
from typing import Any, Dict

import frappe

from nkt_operations.nkt_store_operations.features.inventory.internal.physical_inventory_offline_intent import (
    ACTION,
    FAMILY,
    canonical_physical_inventory_count_intent_json,
    normalize_physical_inventory_count_intent,
)

FOUNDATION_VERSION = "C15C.10J-R3"

PRIMARY_RECEIPT_STATE = "Physical Inventory Count Intent Preserved"
MATERIALIZATION_STATE = "Pending Physical Inventory Reconciliation"

# R3 is intentionally preservation-only.
PRIMARY_PRESERVATION_ENABLED = True
CANONICAL_STOCK_ADJUSTMENT_ENABLED = False
AUTO_STOCK_RECONCILIATION_ENABLED = False
HISTORICAL_STOCK_RECONCILIATION_ENABLED = False
INTERVENING_MOVEMENT_REBASE_DECIDED = False
STALE_COUNT_AUTO_POST_DECIDED = False

PRESERVATION_ENVELOPE_VERSION = 1


def _text(value: Any) -> str:
    return str(value or "").strip()


def payload_sha256(payload: Dict[str, Any]) -> str:
    canonical = canonical_physical_inventory_count_intent_json(payload)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def preserve_physical_inventory_count_intent(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Return the deterministic Primary preservation envelope.

    This function performs no database write. It normalizes the immutable Edge
    observation, hashes the canonical intent, and produces the state a future
    shared-receipt integration may persist exactly once.
    """
    normalized = normalize_physical_inventory_count_intent(payload)
    canonical = json.dumps(
        normalized,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    return {
        "preservation_envelope_version": PRESERVATION_ENVELOPE_VERSION,
        "family": FAMILY,
        "action": ACTION,
        "submit_request_id": normalized["submit_request_id"],
        "payload_sha256": digest,
        "receipt_state": PRIMARY_RECEIPT_STATE,
        "materialization_state": MATERIALIZATION_STATE,
        "canonical_stock_adjustment_authorized": False,
        "stock_reconciliation_created": False,
        "stock_ledger_mutated": False,
        "bin_mutated": False,
        "requires_primary_reconciliation": True,
        "requires_current_stock_rebase_before_posting": True,
        "intervening_movement_policy_decided": False,
        "stale_count_auto_post_decided": False,
        "immutable_observation": {
            "company": normalized["company"],
            "warehouse": normalized["warehouse"],
            "business_date": normalized["business_date"],
            "count_datetime": normalized["count_datetime"],
            "counted_by": normalized["counted_by"],
            "entry_role": normalized["entry_role"],
            "count_reason": normalized["count_reason"],
            "physical_count_reference": normalized["physical_count_reference"],
            "operator_notes": normalized["operator_notes"],
            "physical_count_confirmed": normalized["physical_count_confirmed"],
            "items": normalized["items"],
        },
    }


def compare_preserved_request(
    existing_submit_request_id: Any,
    existing_payload_sha256: Any,
    incoming_payload: Dict[str, Any],
) -> Dict[str, Any]:
    """Classify exactly-once replay without writing anything.

    Same request id + same canonical payload => safe replay.
    Same request id + different canonical payload => immutable conflict.
    Different request id => a distinct event, not a replay.
    """
    existing_id = _text(existing_submit_request_id)
    existing_sha = _text(existing_payload_sha256)
    incoming = preserve_physical_inventory_count_intent(incoming_payload)

    if not existing_id or not existing_sha:
        raise frappe.ValidationError(
            "Existing preservation identity requires Submit Request ID and payload SHA-256."
        )

    if existing_id != incoming["submit_request_id"]:
        return {
            "classification": "distinct_request",
            "same_submit_request_id": False,
            "same_payload_sha256": existing_sha == incoming["payload_sha256"],
            "incoming": incoming,
        }

    if existing_sha == incoming["payload_sha256"]:
        return {
            "classification": "idempotent_replay",
            "same_submit_request_id": True,
            "same_payload_sha256": True,
            "incoming": incoming,
        }

    return {
        "classification": "immutable_conflict",
        "same_submit_request_id": True,
        "same_payload_sha256": False,
        "incoming": incoming,
    }


def assert_no_auto_materialization(envelope: Dict[str, Any]) -> None:
    forbidden_truthy = {
        "canonical_stock_adjustment_authorized",
        "stock_reconciliation_created",
        "stock_ledger_mutated",
        "bin_mutated",
    }
    bad = sorted(k for k in forbidden_truthy if envelope.get(k))
    if bad:
        raise frappe.ValidationError(
            "R3 preservation envelope illegally authorizes canonical stock effects: "
            + ", ".join(bad)
            + "."
        )


def foundation_status() -> Dict[str, Any]:
    return {
        "foundation_version": FOUNDATION_VERSION,
        "primary_receipt_state": PRIMARY_RECEIPT_STATE,
        "materialization_state": MATERIALIZATION_STATE,
        "primary_preservation_enabled": PRIMARY_PRESERVATION_ENABLED,
        "canonical_stock_adjustment_enabled": CANONICAL_STOCK_ADJUSTMENT_ENABLED,
        "auto_stock_reconciliation_enabled": AUTO_STOCK_RECONCILIATION_ENABLED,
        "historical_stock_reconciliation_enabled": HISTORICAL_STOCK_RECONCILIATION_ENABLED,
        "intervening_movement_rebase_decided": INTERVENING_MOVEMENT_REBASE_DECIDED,
        "stale_count_auto_post_decided": STALE_COUNT_AUTO_POST_DECIDED,
        "contract": (
            "Primary may preserve the immutable physical count exactly once, but "
            "R3 never converts preservation into canonical stock adjustment."
        ),
    }
