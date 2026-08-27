from __future__ import annotations

import copy
import hashlib
import json
import os
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Optional

import frappe
from frappe.utils import getdate

from nkt_operations.nkt_store_operations.features.offline_edge.internal.edge_read_model import (
    DETAIL_DAYS,
    ITEM_MOVEMENT_TRACE_DAYS,
    SNAPSHOT_SCHEMA_VERSION,
    section_sha256,
    validate_snapshot,
)

FOUNDATION_VERSION = "C15C.6C-R1"
FILE_MAGIC = b"NKTEDGE1"
NONCE_SIZE = 12
KEY_SIZE = 32

DETAIL_DATE_FIELDS = {
    "orders": "order_date",
    "sales": "business_date",
    "releases": "release_datetime",
    "returns": "return_datetime",
}

READ_FAMILY_CONTRACT = {
    "store_current": {
        "primary": "full_current",
        "edge": "bounded_current",
        "isolated": "unavailable",
    },
    "encoder_customer_history": {
        "primary": "all_time",
        "edge": "bounded_30d",
        "isolated": "unavailable",
    },
    "cashier_sale_history": {
        "primary": "normal_45d",
        "edge": "bounded_30d",
        "isolated": "unavailable",
        "note": "days_31_to_45_wait_for_primary",
    },
    "open_receivables": {
        "primary": "all_open",
        "edge": "all_open",
        "isolated": "unavailable",
    },
    "item_movement": {
        "primary": "requested_authorized_range",
        "edge": "bounded_14d_trace",
        "isolated": "unavailable",
    },
}


class EdgeStoreError(Exception):
    pass


class EdgeStoreIntegrityError(EdgeStoreError):
    pass


class EdgeStoreKeyError(EdgeStoreError):
    pass


def generate_edge_data_key() -> bytes:
    return os.urandom(KEY_SIZE)


def _require_key(key: bytes) -> bytes:
    if not isinstance(key, (bytes, bytearray)) or len(key) != KEY_SIZE:
        raise EdgeStoreKeyError("Edge data key must be exactly 32 bytes.")
    return bytes(key)


def _canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    ).encode("utf-8")


def snapshot_plaintext_sha256(snapshot: Dict[str, Any]) -> str:
    return hashlib.sha256(_canonical_json_bytes(snapshot)).hexdigest()


def _aad(device_id: str, schema_version: int) -> bytes:
    return f"NKT|EDGE|{device_id}|SCHEMA:{schema_version}".encode("utf-8")


def purge_expired_detail(
    snapshot: Dict[str, Any],
    *,
    today=None,
) -> Dict[str, Any]:
    """
    Return a deep-copied snapshot with detailed operational rows outside
    the accepted 30-day window removed.

    Open receivables are intentionally preserved regardless of age while open.
    """
    out = copy.deepcopy(snapshot)
    today_date = getdate(today) if today else datetime.now().date()
    cutoff = today_date - timedelta(days=DETAIL_DAYS)

    for section, field in DETAIL_DATE_FIELDS.items():
        kept = []
        for row in out.get(section, []) or []:
            raw = row.get(field)
            if not raw:
                kept.append(row)
                continue
            if getdate(raw) >= cutoff:
                kept.append(row)
        out[section] = kept

    out["detail_days"] = DETAIL_DAYS
    out["detail_cutoff"] = cutoff.isoformat()
    out["item_movement_trace_days"] = ITEM_MOVEMENT_TRACE_DAYS
    return out


def _encrypt_bytes(plaintext: bytes, key: bytes, aad: bytes) -> bytes:
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    except Exception as exc:
        raise EdgeStoreError(
            "AES-GCM support is unavailable in this environment."
        ) from exc

    nonce = os.urandom(NONCE_SIZE)
    ciphertext = AESGCM(_require_key(key)).encrypt(nonce, plaintext, aad)
    return FILE_MAGIC + nonce + ciphertext


def _decrypt_bytes(blob: bytes, key: bytes, aad: bytes) -> bytes:
    if not blob.startswith(FILE_MAGIC):
        raise EdgeStoreIntegrityError("Invalid Edge store file header.")
    if len(blob) <= len(FILE_MAGIC) + NONCE_SIZE:
        raise EdgeStoreIntegrityError("Truncated Edge store file.")

    nonce_start = len(FILE_MAGIC)
    nonce = blob[nonce_start:nonce_start + NONCE_SIZE]
    ciphertext = blob[nonce_start + NONCE_SIZE:]

    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        return AESGCM(_require_key(key)).decrypt(nonce, ciphertext, aad)
    except EdgeStoreKeyError:
        raise
    except Exception as exc:
        raise EdgeStoreIntegrityError(
            "Edge store authentication failed."
        ) from exc


def save_encrypted_snapshot(
    path: str | Path,
    snapshot: Dict[str, Any],
    *,
    key: bytes,
    device_id: str,
    today=None,
) -> Dict[str, Any]:
    """
    Purge to the approved retention window, validate, and atomically persist
    one encrypted snapshot.

    The key is supplied by the caller and is NOT stored beside the file.
    """
    if not device_id:
        raise EdgeStoreError("Device ID is required.")

    bounded = purge_expired_detail(snapshot, today=today)
    validate_snapshot(bounded, today=today)

    plaintext = _canonical_json_bytes(bounded)
    aad = _aad(device_id, int(bounded.get("schema_version") or SNAPSHOT_SCHEMA_VERSION))
    blob = _encrypt_bytes(plaintext, key, aad)

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    fd, temp_name = tempfile.mkstemp(
        prefix=path.name + ".",
        suffix=".tmp",
        dir=str(path.parent),
    )
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(blob)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)

    return {
        "path": str(path),
        "schema_version": int(bounded.get("schema_version") or SNAPSHOT_SCHEMA_VERSION),
        "plaintext_sha256": hashlib.sha256(plaintext).hexdigest(),
        "encrypted_sha256": hashlib.sha256(blob).hexdigest(),
        "encrypted_bytes": len(blob),
        "detail_cutoff": bounded.get("detail_cutoff"),
        "section_counts": {
            k: len(v)
            for k, v in bounded.items()
            if isinstance(v, list)
        },
        "section_hashes": {
            k: section_sha256(v)
            for k, v in bounded.items()
            if isinstance(v, list)
        },
    }


def load_encrypted_snapshot(
    path: str | Path,
    *,
    key: bytes,
    device_id: str,
) -> Dict[str, Any]:
    path = Path(path)
    blob = path.read_bytes()

    # Schema 1 is the only accepted schema in this foundation package.
    aad = _aad(device_id, SNAPSHOT_SCHEMA_VERSION)
    plaintext = _decrypt_bytes(blob, key, aad)

    try:
        snapshot = json.loads(plaintext.decode("utf-8"))
    except Exception as exc:
        raise EdgeStoreIntegrityError("Edge store JSON is invalid.") from exc

    if int(snapshot.get("schema_version") or 0) != SNAPSHOT_SCHEMA_VERSION:
        raise EdgeStoreIntegrityError("Unsupported Edge snapshot schema.")

    validate_snapshot(snapshot)
    return snapshot


def inspect_encrypted_file(path: str | Path) -> Dict[str, Any]:
    blob = Path(path).read_bytes()
    return {
        "starts_with_magic": blob.startswith(FILE_MAGIC),
        "encrypted_bytes": len(blob),
        "encrypted_sha256": hashlib.sha256(blob).hexdigest(),
    }


def route_read_source(
    read_family: str,
    *,
    primary_available: bool,
    edge_available: bool,
) -> Dict[str, Any]:
    """
    Deterministic read routing contract.

    No guessed heartbeat/freshness threshold is introduced here.
    The caller supplies current reachability state.
    """
    contract = READ_FAMILY_CONTRACT.get(read_family)
    if not contract:
        raise frappe.ValidationError(f"Unsupported read family: {read_family}")

    if primary_available:
        return {
            "source": "Primary",
            "scope": contract["primary"],
            "read_family": read_family,
            "degraded": False,
        }

    if edge_available:
        result = {
            "source": "Edge",
            "scope": contract["edge"],
            "read_family": read_family,
            "degraded": True,
        }
        if contract.get("note"):
            result["note"] = contract["note"]
        return result

    return {
        "source": "Unavailable",
        "scope": contract["isolated"],
        "read_family": read_family,
        "degraded": True,
        "note": "No broad history fallback is allowed on an isolated frontline laptop.",
    }


def routing_matrix() -> Dict[str, Any]:
    out = {}
    for family in READ_FAMILY_CONTRACT:
        out[family] = {
            "primary_up_edge_up": route_read_source(
                family, primary_available=True, edge_available=True
            ),
            "primary_down_edge_up": route_read_source(
                family, primary_available=False, edge_available=True
            ),
            "primary_down_edge_down": route_read_source(
                family, primary_available=False, edge_available=False
            ),
        }
    return out
