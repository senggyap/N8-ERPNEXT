from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any, Dict

import frappe
from frappe.utils import flt, get_datetime, getdate, now

from nkt_operations.nkt_store_operations.features.offline_edge.safe_sync import NKTIdempotencyConflict
from nkt_operations.nkt_store_operations.features.offline_edge.sync_transport import _runtime_role
from nkt_operations.nkt_store_operations.features.inventory.internal.warehouse_transfer_dispatch_intent import (
    canonical_dispatch_payload_json,
    normalize_dispatch_payload,
)
from nkt_operations.nkt_store_operations.features.inventory.internal.primary_warehouse_transfer_dispatch_intent import (
    PRIMARY_JOURNAL,
)
from nkt_operations.nkt_store_operations.doctype.nkt_warehouse_transfer.nkt_warehouse_transfer import (
    C15C_TRANSFER_DISPATCH_CONTEXT_FLAG,
    release_transfer,
)

FOUNDATION_VERSION = "C15C.10G-R8"
TOLERANCE = 0.000001
MATERIALIZATION_ACK_NAMESPACE = uuid.UUID("713fb125-acde-4c3d-a06f-4bd98cbcecad")


def _require_primary():
    if _runtime_role() != "Primary":
        raise frappe.PermissionError(
            "Internal transfer source-dispatch materialization is available only at Primary."
        )


def _uuid(value: Any, label: str) -> str:
    try:
        return str(uuid.UUID(str(value)))
    except Exception as exc:
        raise frappe.ValidationError(f"{label} is invalid.") from exc


def _ack_uuid(event_uuid: str, payload_hash: str, stock_entry: str) -> str:
    event_uuid = _uuid(event_uuid, "Transfer Dispatch Intent UUID")
    payload_hash = str(payload_hash or "").lower()
    if len(payload_hash) != 64 or any(c not in "0123456789abcdef" for c in payload_hash):
        raise frappe.ValidationError("Transfer Dispatch payload hash is invalid.")
    stock_entry = str(stock_entry or "").strip()
    if not stock_entry:
        raise frappe.ValidationError("Materialized outgoing Stock Entry is required.")
    material = (
        "NKT Warehouse Transfer Source Dispatch Materialization"
        + "\0" + event_uuid
        + "\0" + payload_hash
        + "\0" + stock_entry
    )
    return str(uuid.uuid5(MATERIALIZATION_ACK_NAMESPACE, material))


def _claim_name(event_uuid: str) -> str:
    return "nkt-10g-dispatch-stock-" + hashlib.sha256(
        f"event:{event_uuid}".encode("utf-8")
    ).hexdigest()[:32]


def _acquire_claim(event_uuid: str) -> None:
    # Same proven C15C ordering as 10E R5A: named claim BEFORE the first mutable
    # Primary-journal read, avoiding a stale REPEATABLE READ snapshot under race.
    name = _claim_name(event_uuid)
    rows = frappe.db.sql("SELECT GET_LOCK(%s,%s)", (name, 30), as_list=True)
    if not rows or int(rows[0][0] or 0) != 1:
        raise frappe.ValidationError(
            "Transfer dispatch materialization is busy. Safe retry is required."
        )

    state = {"released": False}

    def release_once():
        if state["released"]:
            return
        try:
            frappe.db.sql("SELECT RELEASE_LOCK(%s)", (name,))
        except Exception:
            pass
        state["released"] = True

    frappe.db.after_commit.add(release_once)
    frappe.db.after_rollback.add(release_once)


def _journal_for_update(event_uuid: str):
    rows = frappe.db.sql(
        f"SELECT name FROM `tab{PRIMARY_JOURNAL}` WHERE name=%s FOR UPDATE",
        (event_uuid,),
        as_dict=True,
    )
    return frappe.get_doc(PRIMARY_JOURNAL, event_uuid) if rows else None


def _lock_transfer_for_update(transfer_name: str):
    rows = frappe.db.sql(
        "SELECT name FROM `tabNKT Warehouse Transfer` WHERE name=%s FOR UPDATE",
        (transfer_name,),
        as_dict=True,
    )
    if not rows:
        raise frappe.DoesNotExistError(
            "Internal Warehouse Transfer is unavailable for source-dispatch materialization."
        )


def _payload_from_journal(journal) -> Dict[str, Any]:
    try:
        raw = json.loads(str(journal.canonical_payload_json or ""))
    except Exception as exc:
        raise NKTIdempotencyConflict(
            "Preserved Transfer Dispatch payload JSON is invalid."
        ) from exc
    payload = normalize_dispatch_payload(raw)
    canonical = canonical_dispatch_payload_json(payload)
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    if digest != str(journal.payload_sha256 or "").lower():
        raise NKTIdempotencyConflict(
            "Preserved Transfer Dispatch payload hash no longer matches canonical content."
        )
    if canonical != str(journal.canonical_payload_json or ""):
        raise NKTIdempotencyConflict(
            "Preserved Transfer Dispatch canonical payload has drifted."
        )
    return payload


def _canonical_ack_json(value: Dict[str, Any]) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    )


def _outgoing_entry(transfer):
    name = str(transfer.outgoing_stock_entry or "").strip()
    if not name or not frappe.db.exists("Stock Entry", name):
        raise NKTIdempotencyConflict(
            "Materialized transfer has no authoritative outgoing Stock Entry."
        )
    entry = frappe.get_doc("Stock Entry", name)
    if int(entry.docstatus or 0) != 1:
        raise NKTIdempotencyConflict("Outgoing transfer Stock Entry is not submitted.")
    if str(entry.stock_entry_type or "") != "Material Transfer" or str(entry.purpose or "") != "Material Transfer":
        raise NKTIdempotencyConflict("Outgoing transfer stock transaction is not a Material Transfer.")
    if int(entry.add_to_transit or 0) != 1:
        raise NKTIdempotencyConflict("Outgoing transfer Stock Entry is not Add to Transit.")
    return entry


def _verify_entry_rows(entry, transfer, payload):
    if str(entry.from_warehouse or "") != str(payload["source_warehouse"]):
        raise NKTIdempotencyConflict("Outgoing Stock Entry source warehouse conflicts with preserved dispatch.")
    transit = str(entry.to_warehouse or "").strip()
    if not transit or transit in {payload["source_warehouse"], payload["destination_warehouse"]}:
        raise NKTIdempotencyConflict("Outgoing Stock Entry Goods In Transit lineage is invalid.")

    expected = {row["item_code"]: flt(row["dispatch_quantity"]) for row in payload["items"]}
    actual = {}
    for row in entry.items:
        item = str(row.item_code or "")
        if item not in expected:
            continue
        if str(row.s_warehouse or "") != payload["source_warehouse"] or str(row.t_warehouse or "") != transit:
            raise NKTIdempotencyConflict("Outgoing Stock Entry row warehouse lineage conflicts with preserved dispatch.")
        actual[item] = flt(actual.get(item)) + flt(row.qty)
    if set(actual) != set(expected):
        raise NKTIdempotencyConflict("Outgoing Stock Entry item set conflicts with preserved dispatch.")
    for item, qty in expected.items():
        if abs(flt(actual[item]) - flt(qty)) > TOLERANCE:
            raise NKTIdempotencyConflict(
                f"Outgoing Stock Entry quantity conflicts with preserved dispatch for item {item}."
            )
    return transit


def _stock_effects(payload, transit_warehouse: str):
    effects = []
    for row in payload["items"]:
        item = row["item_code"]
        source = payload["source_warehouse"]
        dispatched = flt(row["dispatch_quantity"])
        source_actual = flt(
            frappe.db.get_value("Bin", {"item_code": item, "warehouse": source}, "actual_qty")
        )
        transit_actual = flt(
            frappe.db.get_value("Bin", {"item_code": item, "warehouse": transit_warehouse}, "actual_qty")
        )
        effects.append(
            {
                "item_code": item,
                "source_warehouse": source,
                "transit_warehouse": transit_warehouse,
                "dispatched_qty": float(f"{dispatched:.6f}"),
                "primary_post_source_actual_qty": float(f"{source_actual:.6f}"),
                "primary_post_transit_actual_qty": float(f"{transit_actual:.6f}"),
            }
        )
    return effects


def _build_ack(journal, payload, entry, transit_warehouse: str, ack_uuid: str):
    value = {
        "event_uuid": journal.name,
        "payload_sha256": str(journal.payload_sha256 or "").lower(),
        "materialization_ack_uuid": ack_uuid,
        "warehouse_transfer": journal.warehouse_transfer,
        "source_warehouse": payload["source_warehouse"],
        "destination_warehouse": payload["destination_warehouse"],
        "transit_warehouse": transit_warehouse,
        "stock_entry": entry.name,
        "stock_effects": _stock_effects(payload, transit_warehouse),
    }
    canonical = _canonical_ack_json(value)
    return {
        **value,
        "materialization_ack_sha256": hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
    }


def _stored_ack(journal, payload, entry, transit_warehouse: str):
    raw = str(journal.materialization_ack_json or "")
    digest = str(journal.materialization_ack_sha256 or "").lower()
    if not raw or not digest:
        raise NKTIdempotencyConflict("Primary transfer journal is missing durable materialization ACK evidence.")
    if hashlib.sha256(raw.encode("utf-8")).hexdigest() != digest:
        raise NKTIdempotencyConflict("Primary transfer materialization ACK hash is invalid.")
    try:
        ack = json.loads(raw)
    except Exception as exc:
        raise NKTIdempotencyConflict("Primary transfer materialization ACK JSON is invalid.") from exc
    if _canonical_ack_json(ack) != raw:
        raise NKTIdempotencyConflict("Primary transfer materialization ACK is not canonical.")

    expected_uuid = _ack_uuid(journal.name, journal.payload_sha256, entry.name)
    checks = {
        "event_uuid": journal.name,
        "payload_sha256": str(journal.payload_sha256 or "").lower(),
        "materialization_ack_uuid": expected_uuid,
        "warehouse_transfer": journal.warehouse_transfer,
        "source_warehouse": payload["source_warehouse"],
        "destination_warehouse": payload["destination_warehouse"],
        "transit_warehouse": transit_warehouse,
        "stock_entry": entry.name,
    }
    for field, expected in checks.items():
        if str(ack.get(field) or "") != str(expected or ""):
            raise NKTIdempotencyConflict(
                f"Primary transfer materialization ACK {field} binding is invalid."
            )
    effects = ack.get("stock_effects")
    if not isinstance(effects, list) or len(effects) != len(payload["items"]):
        raise NKTIdempotencyConflict("Primary transfer materialization ACK stock effects are incomplete.")
    return {**ack, "materialization_ack_sha256": digest}


def _verify_materialized(journal, payload):
    transfer = frappe.get_doc("NKT Warehouse Transfer", journal.warehouse_transfer)
    if str(transfer.status or "") != "In Transit":
        raise NKTIdempotencyConflict("Materialized transfer is not In Transit.")
    if str(transfer.released_by or "") != str(journal.origin_user or ""):
        raise NKTIdempotencyConflict("Materialized transfer lost the original warehouse operator.")
    if get_datetime(transfer.released_at) != get_datetime(journal.settled_at):
        raise NKTIdempotencyConflict("Materialized transfer lost the original physical dispatch time.")

    entry = _outgoing_entry(transfer)
    transit = _verify_entry_rows(entry, transfer, payload)
    posting = get_datetime(f"{entry.posting_date} {entry.posting_time}")
    if posting != get_datetime(journal.settled_at):
        raise NKTIdempotencyConflict(
            "Outgoing Stock Entry posting time does not equal the original physical dispatch time."
        )

    if str(journal.materialized_stock_entry or "") != str(entry.name):
        raise NKTIdempotencyConflict("Primary transfer journal lost Stock Entry binding.")
    if str(journal.transit_warehouse or "") != transit:
        raise NKTIdempotencyConflict("Primary transfer journal lost Goods In Transit binding.")
    expected_ack = _ack_uuid(journal.name, journal.payload_sha256, entry.name)
    if str(journal.materialization_ack_uuid or "") != expected_ack:
        raise NKTIdempotencyConflict("Primary transfer materialization ACK UUID binding is invalid.")
    if str(journal.downstream_state or "") != "Source Dispatch Materialized":
        raise NKTIdempotencyConflict("Primary transfer journal is not Source Dispatch Materialized.")

    ack = _stored_ack(journal, payload, entry, transit)
    return {
        **ack,
        "physical_dispatch_time": str(transfer.released_at),
        "warehouse_operator": transfer.released_by,
        "downstream_state": journal.downstream_state,
        "canonical_transfer_in_transit": True,
        "stock_entry_created": True,
        "edge_projection_may_finalize_only_after_local_stock_rebase": True,
        "admin_pre_release_approval_required": False,
    }


def materialize_source_dispatch(event_uuid: str) -> Dict[str, Any]:
    _require_primary()
    event_uuid = _uuid(event_uuid, "Transfer Dispatch Intent UUID")
    _acquire_claim(event_uuid)

    journal = _journal_for_update(event_uuid)
    if not journal:
        raise frappe.DoesNotExistError(
            "Preserved Transfer Dispatch Intent is unavailable at Primary."
        )
    payload = _payload_from_journal(journal)
    _lock_transfer_for_update(journal.warehouse_transfer)

    if str(journal.preservation_state or "") != "Preserved":
        raise NKTIdempotencyConflict("Transfer Dispatch Intent is not preserved.")

    if str(journal.downstream_state or "") == "Source Dispatch Materialized":
        result = _verify_materialized(journal, payload)
        result["replay"] = True
        return result

    if str(journal.downstream_state or "") != "Awaiting Source Dispatch Materialization":
        raise NKTIdempotencyConflict(
            "Transfer Dispatch Intent is not eligible for source-dispatch materialization."
        )

    # The existing release_transfer() remains the one authoritative stock engine.
    # Only the preserved event UUID is passed process-locally; release_transfer()
    # re-reads trusted operator/time/business fields from the locked Primary journal.
    previous = frappe.flags.get(C15C_TRANSFER_DISPATCH_CONTEXT_FLAG)
    frappe.flags[C15C_TRANSFER_DISPATCH_CONTEXT_FLAG] = {"event_uuid": event_uuid}
    try:
        release_result = release_transfer(journal.warehouse_transfer)
    finally:
        if previous is None:
            frappe.flags.pop(C15C_TRANSFER_DISPATCH_CONTEXT_FLAG, None)
        else:
            frappe.flags[C15C_TRANSFER_DISPATCH_CONTEXT_FLAG] = previous

    transfer = frappe.get_doc("NKT Warehouse Transfer", journal.warehouse_transfer)
    entry = _outgoing_entry(transfer)
    transit = _verify_entry_rows(entry, transfer, payload)
    ack_uuid = _ack_uuid(journal.name, journal.payload_sha256, entry.name)
    ack = _build_ack(journal, payload, entry, transit, ack_uuid)
    ack_json = _canonical_ack_json(
        {k: v for k, v in ack.items() if k != "materialization_ack_sha256"}
    )

    frappe.db.set_value(
        PRIMARY_JOURNAL,
        journal.name,
        {
            "downstream_state": "Source Dispatch Materialized",
            "materialized_stock_entry": entry.name,
            "transit_warehouse": transit,
            "materialized_at": now(),
            "materialization_ack_uuid": ack_uuid,
            "materialization_ack_sha256": ack["materialization_ack_sha256"],
            "materialization_ack_json": ack_json,
        },
        update_modified=False,
    )
    journal.reload()

    result = _verify_materialized(journal, payload)
    result["replay"] = False
    result["release_transfer_result"] = {
        "status": release_result.get("status"),
        "transfer": release_result.get("transfer"),
        "outgoing_stock_entry": release_result.get("outgoing_stock_entry"),
        "transit_warehouse": release_result.get("transit_warehouse"),
        "preserved_offline_dispatch_materialized": release_result.get(
            "preserved_offline_dispatch_materialized"
        ),
    }
    return result


def installation_probe():
    journal_fields = {
        f.fieldname for f in frappe.get_meta(PRIMARY_JOURNAL).fields
    }
    projection_fields = {
        f.fieldname
        for f in frappe.get_meta("NKT Edge Warehouse Transfer Projection").fields
    }
    required_journal = {
        "materialized_stock_entry",
        "transit_warehouse",
        "materialized_at",
        "materialization_ack_uuid",
        "materialization_ack_sha256",
        "materialization_ack_json",
    }
    required_projection = {
        "materialization_ack_uuid",
        "materialization_ack_sha256",
        "primary_post_source_actual_qty",
        "primary_post_transit_actual_qty",
        "primary_materialized_at",
        "finalized_at",
    }
    return {
        "foundation_version": FOUNDATION_VERSION,
        "materializer": "release_transfer",
        "new_stock_engine": False,
        "journal_fields_ready": required_journal.issubset(journal_fields),
        "projection_fields_ready": required_projection.issubset(projection_fields),
        "original_edge_operator_preserved": True,
        "original_physical_time_preserved": True,
        "stock_entry_posting_time_preserved": True,
        "online_duplicate_block_after_primary_preservation": True,
        "destination_arrival_enabled": False,
        "destructive_fixture_executed": False,
    }


@frappe.whitelist()
def materialize(event_uuid: str):
    return materialize_source_dispatch(event_uuid)
