from __future__ import annotations

import os
import stat
from pathlib import Path
from typing import Any, Dict

import frappe

from nkt_operations.nkt_store_operations.features.offline_edge.edge_store import (
    EdgeStoreError,
    load_encrypted_snapshot,
)
from nkt_operations.nkt_store_operations.features.offline_edge.internal.edge_read_model import (
    validate_snapshot,
)

FOUNDATION_VERSION = "C15C.7B-R1"
RUNTIME_ROLE = "Store Edge"
KEY_SIZE = 32


class EdgeProviderConfigurationError(Exception):
    pass


def _runtime_role() -> str:
    return str(frappe.conf.get("nkt_runtime_role") or "Primary").strip()


def _required_conf(name: str) -> str:
    value = str(frappe.conf.get(name) or "").strip()
    if not value:
        raise EdgeProviderConfigurationError("Store Edge local provider is not configured.")
    return value


def _absolute_existing_file(name: str, value: str) -> Path:
    path = Path(value)
    if not path.is_absolute():
        raise EdgeProviderConfigurationError(f"{name} must be an absolute path.")
    if not path.exists() or not path.is_file():
        raise EdgeProviderConfigurationError(f"{name} is unavailable.")
    return path


def _read_key_file(path: Path) -> bytes:
    if os.name == "posix":
        mode = stat.S_IMODE(path.stat().st_mode)
        if mode & 0o077:
            raise EdgeProviderConfigurationError(
                "Store Edge key file permissions are too broad."
            )

    data = path.read_bytes()
    if len(data) != KEY_SIZE:
        raise EdgeProviderConfigurationError(
            "Store Edge key file must contain exactly 32 raw bytes."
        )
    return data


def edge_provider_config() -> Dict[str, Any]:
    if _runtime_role() != RUNTIME_ROLE:
        raise EdgeProviderConfigurationError("Local Edge provider is not active on this runtime.")

    device_id = _required_conf("nkt_edge_device_id")
    snapshot_path = _absolute_existing_file(
        "nkt_edge_snapshot_path",
        _required_conf("nkt_edge_snapshot_path"),
    )
    key_path = _absolute_existing_file(
        "nkt_edge_key_file",
        _required_conf("nkt_edge_key_file"),
    )

    try:
        if snapshot_path.resolve() == key_path.resolve():
            raise EdgeProviderConfigurationError(
                "Store Edge snapshot and key file must be separate."
            )
    except OSError as exc:
        raise EdgeProviderConfigurationError("Store Edge provider paths are invalid.") from exc

    return {
        "runtime_role": RUNTIME_ROLE,
        "device_id": device_id,
        "snapshot_path": snapshot_path,
        "key_path": key_path,
    }


def _assert_snapshot_edge_identity(snapshot: Dict[str, Any], device_id: str) -> None:
    rows = snapshot.get("device_policies", []) or []
    edge = next((row for row in rows if row.get("device_id") == device_id), None)
    if not edge:
        raise EdgeProviderConfigurationError("Configured Store Edge identity is absent from snapshot policy.")

    if edge.get("device_class") != "Store Edge":
        raise EdgeProviderConfigurationError("Configured device is not a Store Edge device.")

    if edge.get("status") != "Active":
        raise EdgeProviderConfigurationError("Configured Store Edge device is not Active.")

    if edge.get("operational_context") not in {"NKT Retail", "Infrastructure"}:
        raise EdgeProviderConfigurationError("Configured Store Edge context is not permitted.")


def load_configured_edge_snapshot() -> Dict[str, Any]:
    """
    Load one authenticated local Edge snapshot using server-side configuration.

    Browser/client callers do not supply:
    - backend selection;
    - snapshot path;
    - key path;
    - Store Edge device UUID.

    No key bytes are returned.
    """
    config = edge_provider_config()
    key = _read_key_file(config["key_path"])

    try:
        snapshot = load_encrypted_snapshot(
            config["snapshot_path"],
            key=key,
            device_id=config["device_id"],
        )
    except EdgeStoreError:
        raise
    except Exception as exc:
        raise EdgeProviderConfigurationError("Store Edge local snapshot is unavailable.") from exc
    finally:
        # Python cannot guarantee physical memory erasure, but do not retain a module/global key.
        key = b""

    validate_snapshot(snapshot)
    _assert_snapshot_edge_identity(snapshot, config["device_id"])
    return snapshot


def edge_provider_status() -> Dict[str, Any]:
    """
    Internal diagnostic with no key material and no business rows.
    """
    config = edge_provider_config()
    snapshot = load_configured_edge_snapshot()
    return {
        "foundation_version": FOUNDATION_VERSION,
        "runtime_role": config["runtime_role"],
        "device_id": config["device_id"],
        "snapshot_schema_version": snapshot.get("schema_version"),
        "generated_at": snapshot.get("generated_at"),
        "detail_cutoff": snapshot.get("detail_cutoff"),
        "store_warehouse": snapshot.get("store_warehouse"),
        "critical_offline_mutations_enabled": snapshot.get(
            "critical_offline_mutations_enabled"
        ),
        "snapshot_path_configured": True,
        "key_file_configured": True,
        "key_material_returned": False,
    }
