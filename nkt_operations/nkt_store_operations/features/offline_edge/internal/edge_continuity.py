from __future__ import annotations

import copy
import hashlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

import frappe

from nkt_operations.nkt_store_operations.features.offline_edge.internal.edge_provider import (
    EdgeProviderConfigurationError,
    _assert_snapshot_edge_identity,
    _read_key_file,
    edge_provider_config,
    load_configured_edge_snapshot,
)
from nkt_operations.nkt_store_operations.features.offline_edge.internal.edge_read_model import (
    validate_snapshot,
)
from nkt_operations.nkt_store_operations.features.offline_edge.edge_store import (
    save_encrypted_snapshot,
)

FOUNDATION_VERSION = "C15C.7C-R1"


class EdgeRefreshError(Exception):
    pass


def _parse_generated_at(value: Any) -> datetime:
    if not value:
        raise EdgeRefreshError("Edge snapshot generation time is missing.")

    text = str(value).strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except Exception as exc:
        raise EdgeRefreshError("Edge snapshot generation time is invalid.") from exc

    if parsed.tzinfo is None:
        # Accepted snapshots are timezone-aware, but fail safely if an older
        # snapshot ever omitted the offset by treating it as UTC observation
        # rather than silently guessing local business time.
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def snapshot_age_observation(
    snapshot: Dict[str, Any],
    *,
    now: Optional[datetime] = None,
    max_snapshot_age_seconds: Optional[int] = None,
) -> Dict[str, Any]:
    generated = _parse_generated_at(snapshot.get("generated_at"))
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    age_seconds = max(
        0,
        int((now.astimezone(timezone.utc) - generated.astimezone(timezone.utc)).total_seconds()),
    )

    result = {
        "generated_at": generated.isoformat(),
        "observed_at": now.isoformat(),
        "age_seconds": age_seconds,
        "freshness_threshold_configured": max_snapshot_age_seconds is not None,
        "freshness_policy": "observation_only",
        "freshness_status": "observed",
        "requires_policy_action": False,
    }

    if max_snapshot_age_seconds is not None:
        threshold = int(max_snapshot_age_seconds)
        if threshold <= 0:
            raise frappe.ValidationError("Snapshot freshness threshold must be positive.")
        result["freshness_policy"] = "configured_threshold"
        result["max_snapshot_age_seconds"] = threshold
        result["freshness_status"] = "fresh" if age_seconds <= threshold else "stale"
        result["requires_policy_action"] = age_seconds > threshold

    return result


def continuity_decision(
    *,
    primary_reachable: bool,
    edge_reachable: bool,
    edge_snapshot: Optional[Dict[str, Any]] = None,
    now: Optional[datetime] = None,
    max_snapshot_age_seconds: Optional[int] = None,
) -> Dict[str, Any]:
    """
    Source-selection contract for the future desktop shell/router.

    This function does not perform network probing itself and does not invent
    heartbeat or timeout values. It consumes reachability observations supplied
    by the eventual shell/router.

    Primary is preferred whenever reachable.
    """
    if primary_reachable:
        return {
            "source": "Primary",
            "degraded": False,
            "broad_history_available": True,
            "edge_freshness": None,
            "reason": "Primary reachable.",
        }

    if edge_reachable and edge_snapshot is not None:
        freshness = snapshot_age_observation(
            edge_snapshot,
            now=now,
            max_snapshot_age_seconds=max_snapshot_age_seconds,
        )
        return {
            "source": "Edge",
            "degraded": True,
            "broad_history_available": False,
            "edge_freshness": freshness,
            "requires_policy_action": freshness["requires_policy_action"],
            "reason": "Primary unavailable; authenticated Store Edge snapshot available.",
        }

    return {
        "source": "Unavailable",
        "degraded": True,
        "broad_history_available": False,
        "edge_freshness": None,
        "requires_policy_action": False,
        "reason": "Primary and Store Edge read origins unavailable.",
    }


def configured_continuity_status(
    *,
    primary_reachable: bool,
    edge_reachable: bool,
    max_snapshot_age_seconds: Optional[int] = None,
) -> Dict[str, Any]:
    """
    Internal status helper for a Store Edge runtime.

    If Edge is reported reachable, the configured encrypted snapshot must also
    load and authenticate successfully or this call fails closed.
    """
    snapshot = None
    if edge_reachable:
        snapshot = load_configured_edge_snapshot()

    return continuity_decision(
        primary_reachable=primary_reachable,
        edge_reachable=edge_reachable,
        edge_snapshot=snapshot,
        max_snapshot_age_seconds=max_snapshot_age_seconds,
    )


def _validate_refresh_candidate(
    snapshot: Dict[str, Any],
    *,
    configured_device_id: str,
) -> Dict[str, Any]:
    if not isinstance(snapshot, dict):
        raise EdgeRefreshError("Edge refresh candidate is invalid.")

    validate_snapshot(snapshot)
    _assert_snapshot_edge_identity(snapshot, configured_device_id)

    if snapshot.get("critical_offline_mutations_enabled") is not False:
        raise EdgeRefreshError(
            "Edge refresh candidate does not preserve the critical-offline-write lock."
        )

    return snapshot


def install_refreshed_snapshot(
    snapshot: Dict[str, Any],
) -> Dict[str, Any]:
    """
    Atomic local install primitive for a snapshot that has already been fetched
    by a future authenticated Primary->Edge transport.

    C15C.7C deliberately does NOT implement or invent that transport credential.
    It only proves that once a candidate snapshot is supplied, validation,
    encryption, atomic replacement and post-install verification are safe.
    """
    config = edge_provider_config()
    candidate = copy.deepcopy(snapshot)
    _validate_refresh_candidate(
        candidate,
        configured_device_id=config["device_id"],
    )

    key = _read_key_file(config["key_path"])
    try:
        before_sha256 = (
            hashlib.sha256(config["snapshot_path"].read_bytes()).hexdigest()
            if config["snapshot_path"].exists()
            else None
        )

        save_meta = save_encrypted_snapshot(
            config["snapshot_path"],
            candidate,
            key=key,
            device_id=config["device_id"],
        )

        # Verify the just-installed file through the same production provider
        # path used by routed reads.
        loaded = load_configured_edge_snapshot()
        loaded_plaintext_sha256 = save_meta["plaintext_sha256"]

        return {
            "foundation_version": FOUNDATION_VERSION,
            "installed": True,
            "before_encrypted_sha256": before_sha256,
            "after_encrypted_sha256": save_meta["encrypted_sha256"],
            "plaintext_sha256": loaded_plaintext_sha256,
            "generated_at": loaded.get("generated_at"),
            "detail_cutoff": loaded.get("detail_cutoff"),
            "store_warehouse": loaded.get("store_warehouse"),
            "critical_offline_mutations_enabled": loaded.get(
                "critical_offline_mutations_enabled"
            ),
            "key_material_returned": False,
            "business_rows_returned": False,
        }
    finally:
        key = b""


def local_snapshot_observation(
    *,
    max_snapshot_age_seconds: Optional[int] = None,
) -> Dict[str, Any]:
    """
    Safe admin/Owner diagnostic: no business rows, no key/path details.
    """
    snapshot = load_configured_edge_snapshot()
    freshness = snapshot_age_observation(
        snapshot,
        max_snapshot_age_seconds=max_snapshot_age_seconds,
    )
    return {
        "foundation_version": FOUNDATION_VERSION,
        "generated_at": snapshot.get("generated_at"),
        "detail_cutoff": snapshot.get("detail_cutoff"),
        "store_warehouse": snapshot.get("store_warehouse"),
        "freshness": freshness,
        "critical_offline_mutations_enabled": snapshot.get(
            "critical_offline_mutations_enabled"
        ),
        "business_rows_returned": False,
        "key_material_returned": False,
    }
