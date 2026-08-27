from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any, Dict

import frappe
from frappe.utils import flt, get_datetime, now

from nkt_operations.nkt_store_operations.features.offline_edge.safe_sync import NKTIdempotencyConflict
from nkt_operations.nkt_store_operations.features.offline_edge.sync_transport import _runtime_role
from nkt_operations.nkt_store_operations.features.inventory.internal.warehouse_transfer_arrival_intent import (
    TOLERANCE,
    canonical_arrival_payload_json,
    normalize_arrival_payload,
)
from nkt_operations.nkt_store_operations.features.inventory.internal.primary_warehouse_transfer_arrival_intent import (
    PRIMARY_JOURNAL,
)
from nkt_operations.nkt_store_operations.doctype.nkt_warehouse_transfer.nkt_warehouse_transfer import (
    C15C_TRANSFER_ARRIVAL_CONTEXT_FLAG,
    receive_transfer,
)

FOUNDATION_VERSION = "C15C.10G-R11"
MATERIALIZATION_ACK_NAMESPACE = uuid.UUID("b3df0a45-c8f5-4ef6-85d8-a89afc70c6f1")


def _require_primary():
    if _runtime_role() != "Primary":
        raise frappe.PermissionError(
            "Internal transfer destination-Arrival materialization is available only at Primary."
        )


def _uuid(value: Any, label: str) -> str:
    try:
        return str(uuid.UUID(str(value)))
    except Exception as exc:
        raise frappe.ValidationError(f"{label} is invalid.") from exc


def _claim_name(kind: str, identity: str) -> str:
    return "nkt-10g-arrival-mat-" + hashlib.sha256(
        f"{kind}:{identity}".encode("utf-8")
    ).hexdigest()[:32]


def _acquire_claims(event_uuid: str, transfer_name: str) -> list[str]:
    names = [
        _claim_name("event", event_uuid),
        _claim_name("transfer", transfer_name),
    ]
    acquired = []
    try:
        for name in names:
            rows = frappe.db.sql("SELECT GET_LOCK(%s,%s)", (name, 30), as_list=True)
            if not rows or int(rows[0][0] or 0) != 1:
                raise frappe.ValidationError(
                    "Transfer Arrival materialization is busy. Safe retry is required."
                )
            acquired.append(name)
        return acquired
    except Exception:
        for name in reversed(acquired):
            try:
                frappe.db.sql("SELECT RELEASE_LOCK(%s)", (name,))
            except Exception:
                pass
        raise


def _release_claims(names):
    for name in reversed(names):
        try:
            frappe.db.sql("SELECT RELEASE_LOCK(%s)", (name,))
        except Exception:
            pass


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
            "Internal Warehouse Transfer is unavailable for Arrival materialization."
        )


def _payload_from_journal(journal) -> Dict[str, Any]:
    try:
        raw = json.loads(str(journal.canonical_payload_json or ""))
    except Exception as exc:
        raise NKTIdempotencyConflict(
            "Preserved Transfer Arrival payload JSON is invalid."
        ) from exc
    payload = normalize_arrival_payload(raw)
    canonical = canonical_arrival_payload_json(payload)
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    if digest != str(journal.payload_sha256 or "").lower():
        raise NKTIdempotencyConflict(
            "Preserved Transfer Arrival payload hash no longer matches canonical content."
        )
    if canonical != str(journal.canonical_payload_json or ""):
        raise NKTIdempotencyConflict(
            "Preserved Transfer Arrival canonical payload has drifted."
        )
    return payload


def _validate_materialization_order(journal, payload):
    transfer = frappe.get_doc("NKT Warehouse Transfer", journal.warehouse_transfer)
    by_item = {row.item_code: row for row in (transfer.items or [])}
    for line in payload["items"]:
        local = by_item.get(line["item_code"])
        if not local or str(local.name) != str(line["warehouse_transfer_item"]):
            raise NKTIdempotencyConflict(
                "Transfer Arrival Item lineage no longer matches canonical transfer."
            )
        canonical_arrived = flt(local.arrived_qty)
        expected_before = flt(line["cumulative_arrived_before"])
        if abs(canonical_arrived - expected_before) > TOLERANCE:
            if canonical_arrived < expected_before - TOLERANCE:
                raise frappe.ValidationError(
                    "A later preserved Arrival is waiting for an earlier physical Arrival to materialize first. Safe retry is required."
                )
            raise NKTIdempotencyConflict(
                "Canonical arrived quantity has already advanced beyond this Arrival snapshot."
            )
        canonical_remaining = flt(local.released_qty) - canonical_arrived
        if abs(canonical_remaining - flt(line["remaining_before"])) > TOLERANCE:
            raise NKTIdempotencyConflict(
                "Canonical remaining transit quantity conflicts with preserved Arrival snapshot."
            )
        if flt(line["arrival_quantity"]) > canonical_remaining + TOLERANCE:
            raise frappe.ValidationError(
                "Preserved Arrival quantity exceeds canonical remaining transit quantity."
            )
    return transfer


def _ack_uuid(event_uuid: str, payload_hash: str, stock_entry: str) -> str:
    event_uuid = _uuid(event_uuid, "Transfer Arrival Intent UUID")
    payload_hash = str(payload_hash or "").lower()
    if len(payload_hash) != 64 or any(c not in "0123456789abcdef" for c in payload_hash):
        raise frappe.ValidationError("Transfer Arrival payload hash is invalid.")
    stock_entry = str(stock_entry or "").strip()
    if not stock_entry:
        raise frappe.ValidationError("Materialized incoming Stock Entry is required.")
    material = (
        "NKT Warehouse Transfer Destination Arrival Materialization"
        + "\0" + event_uuid
        + "\0" + payload_hash
        + "\0" + stock_entry
    )
    return str(uuid.uuid5(MATERIALIZATION_ACK_NAMESPACE, material))


def _canonical_ack_json(value: Dict[str, Any]) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    )


def _verify_incoming(entry, journal, payload):
    if int(entry.docstatus or 0) != 1:
        raise NKTIdempotencyConflict("Materialized incoming Stock Entry is not submitted.")
    if (
        str(entry.stock_entry_type or "") != "Material Transfer"
        or str(entry.purpose or "") != "Material Transfer"
        or str(entry.outgoing_stock_entry or "") != str(journal.outgoing_stock_entry or "")
    ):
        raise NKTIdempotencyConflict(
            "Materialized incoming Stock Entry lost outgoing transit lineage."
        )
    if (
        str(entry.from_warehouse or "") != str(journal.transit_warehouse or "")
        or str(entry.to_warehouse or "") != str(journal.destination_warehouse or "")
    ):
        raise NKTIdempotencyConflict(
            "Materialized incoming Stock Entry warehouse lineage is invalid."
        )

    expected = {
        row["item_code"]: flt(row["arrival_quantity"])
        for row in payload["items"]
    }
    actual = {}
    for row in entry.items:
        item = str(row.item_code or "")
        if item in actual:
            raise NKTIdempotencyConflict(
                "Materialized incoming Stock Entry contains duplicate Item lineage."
            )
        actual[item] = row
    if set(actual) != set(expected):
        raise NKTIdempotencyConflict(
            "Materialized incoming Stock Entry Item set conflicts with Arrival intent."
        )
    for item, qty in expected.items():
        row = actual[item]
        if (
            str(row.s_warehouse or "") != str(journal.transit_warehouse or "")
            or str(row.t_warehouse or "") != str(journal.destination_warehouse or "")
            or str(row.against_stock_entry or "") != str(journal.outgoing_stock_entry or "")
            or abs(flt(row.qty) - qty) > TOLERANCE
        ):
            raise NKTIdempotencyConflict(
                f"Materialized incoming Stock Entry row conflicts with Arrival intent for Item {item}."
            )

    posting = get_datetime(f"{entry.posting_date} {entry.posting_time}")
    if posting != get_datetime(journal.settled_at):
        raise NKTIdempotencyConflict(
            "Incoming Stock Entry did not preserve the original physical Arrival time."
        )
    return entry


def _stock_effects(journal, payload):
    effects = []
    for line in payload["items"]:
        item = line["item_code"]
        qty = flt(line["arrival_quantity"])
        transit_actual = flt(
            frappe.db.get_value(
                "Bin",
                {"item_code": item, "warehouse": journal.transit_warehouse},
                "actual_qty",
            )
        )
        destination_actual = flt(
            frappe.db.get_value(
                "Bin",
                {"item_code": item, "warehouse": journal.destination_warehouse},
                "actual_qty",
            )
        )
        effects.append(
            {
                "item_code": item,
                "transit_warehouse": journal.transit_warehouse,
                "destination_warehouse": journal.destination_warehouse,
                "arrival_qty": float(f"{qty:.6f}"),
                "cumulative_arrived_after": float(
                    f"{flt(line['cumulative_arrived_before']) + qty:.6f}"
                ),
                "remaining_in_transit_after": float(
                    f"{max(0.0, flt(line['remaining_before']) - qty):.6f}"
                ),
                "primary_post_transit_actual_qty": float(f"{transit_actual:.6f}"),
                "primary_post_destination_actual_qty": float(f"{destination_actual:.6f}"),
            }
        )
    return effects


def _build_ack(journal, payload, entry, result):
    ack_uuid = _ack_uuid(journal.name, journal.payload_sha256, entry.name)
    value = {
        "event_uuid": journal.name,
        "payload_sha256": str(journal.payload_sha256 or "").lower(),
        "materialization_ack_uuid": ack_uuid,
        "warehouse_transfer": journal.warehouse_transfer,
        "outgoing_stock_entry": journal.outgoing_stock_entry,
        "incoming_stock_entry": entry.name,
        "transit_warehouse": journal.transit_warehouse,
        "destination_warehouse": journal.destination_warehouse,
        "completed": bool(result.get("completed")),
        "transfer_status_after": result.get("status"),
        "stock_effects": _stock_effects(journal, payload),
    }
    canonical = _canonical_ack_json(value)
    return {
        **value,
        "materialization_ack_sha256": hashlib.sha256(
            canonical.encode("utf-8")
        ).hexdigest(),
    }


def _stored_ack(journal, payload, entry):
    raw = str(journal.materialization_ack_json or "")
    digest = str(journal.materialization_ack_sha256 or "").lower()
    if not raw or not digest:
        raise NKTIdempotencyConflict(
            "Primary Arrival journal is missing durable materialization ACK evidence."
        )
    if hashlib.sha256(raw.encode("utf-8")).hexdigest() != digest:
        raise NKTIdempotencyConflict(
            "Primary Arrival materialization ACK hash is invalid."
        )
    try:
        ack = json.loads(raw)
    except Exception as exc:
        raise NKTIdempotencyConflict(
            "Primary Arrival materialization ACK JSON is invalid."
        ) from exc
    if _canonical_ack_json(ack) != raw:
        raise NKTIdempotencyConflict(
            "Primary Arrival materialization ACK is not canonical."
        )

    checks = {
        "event_uuid": journal.name,
        "payload_sha256": str(journal.payload_sha256 or "").lower(),
        "materialization_ack_uuid": _ack_uuid(
            journal.name, journal.payload_sha256, entry.name
        ),
        "warehouse_transfer": journal.warehouse_transfer,
        "outgoing_stock_entry": journal.outgoing_stock_entry,
        "incoming_stock_entry": entry.name,
        "transit_warehouse": journal.transit_warehouse,
        "destination_warehouse": journal.destination_warehouse,
    }
    for field, expected in checks.items():
        if str(ack.get(field) or "") != str(expected or ""):
            raise NKTIdempotencyConflict(
                f"Primary Arrival materialization ACK {field} binding is invalid."
            )
    effects = ack.get("stock_effects")
    if not isinstance(effects, list) or len(effects) != len(payload["items"]):
        raise NKTIdempotencyConflict(
            "Primary Arrival materialization ACK stock effects are incomplete."
        )
    return {**ack, "materialization_ack_sha256": digest}


def _verify_materialized(journal, payload):
    entry_name = str(journal.materialized_stock_entry or "").strip()
    if not entry_name or not frappe.db.exists("Stock Entry", entry_name):
        raise NKTIdempotencyConflict(
            "Primary Arrival journal lost its incoming Stock Entry binding."
        )
    entry = _verify_incoming(
        frappe.get_doc("Stock Entry", entry_name),
        journal,
        payload,
    )
    if str(journal.downstream_state or "") != "Destination Arrival Materialized":
        raise NKTIdempotencyConflict(
            "Primary Arrival journal is not Destination Arrival Materialized."
        )
    expected_ack = _ack_uuid(journal.name, journal.payload_sha256, entry.name)
    if str(journal.materialization_ack_uuid or "") != expected_ack:
        raise NKTIdempotencyConflict(
            "Primary Arrival materialization ACK UUID binding is invalid."
        )
    ack = _stored_ack(journal, payload, entry)

    # Later partial Arrival events may have advanced canonical transfer quantities.
    # Replay of this event therefore proves this event's own Stock Entry + durable
    # journal/ACK, not that the transfer is still at this event's historical snapshot.
    return {
        **ack,
        "physical_arrival_time": str(journal.settled_at),
        "warehouse_operator": journal.origin_user,
        "downstream_state": journal.downstream_state,
        "canonical_arrival_posted": True,
        "stock_entry_created": True,
        "edge_projection_may_finalize_only_after_local_stock_rebase": True,
    }


def materialize_destination_arrival(event_uuid: str) -> Dict[str, Any]:
    _require_primary()
    event_uuid = _uuid(event_uuid, "Transfer Arrival Intent UUID")

    # Read immutable transfer identity first, then claim both event + transfer.
    if not frappe.db.exists(PRIMARY_JOURNAL, event_uuid):
        raise frappe.DoesNotExistError(
            "Preserved Transfer Arrival Intent is unavailable at Primary."
        )
    transfer_name = frappe.db.get_value(
        PRIMARY_JOURNAL, event_uuid, "warehouse_transfer"
    )
    locks = _acquire_claims(event_uuid, transfer_name)
    try:
        journal = _journal_for_update(event_uuid)
        if not journal:
            raise frappe.DoesNotExistError(
                "Preserved Transfer Arrival Intent is unavailable at Primary."
            )
        payload = _payload_from_journal(journal)
        _lock_transfer_for_update(journal.warehouse_transfer)

        if str(journal.preservation_state or "") != "Preserved":
            raise NKTIdempotencyConflict(
                "Transfer Arrival Intent is not preserved."
            )

        if str(journal.downstream_state or "") == "Destination Arrival Materialized":
            result = _verify_materialized(journal, payload)
            result["replay"] = True
            return result

        if str(journal.downstream_state or "") != "Awaiting Destination Arrival Materialization":
            raise NKTIdempotencyConflict(
                "Transfer Arrival Intent is not eligible for materialization."
            )

        _validate_materialization_order(journal, payload)

        quantities = {
            line["item_code"]: line["arrival_quantity"]
            for line in payload["items"]
        }
        previous = frappe.flags.get(C15C_TRANSFER_ARRIVAL_CONTEXT_FLAG)
        frappe.flags[C15C_TRANSFER_ARRIVAL_CONTEXT_FLAG] = {
            "event_uuid": event_uuid
        }
        try:
            receive_result = receive_transfer(
                journal.warehouse_transfer,
                arrival_quantities=quantities,
            )
        finally:
            if previous is None:
                frappe.flags.pop(C15C_TRANSFER_ARRIVAL_CONTEXT_FLAG, None)
            else:
                frappe.flags[C15C_TRANSFER_ARRIVAL_CONTEXT_FLAG] = previous

        entry_name = str(receive_result.get("incoming_stock_entry") or "").strip()
        if not entry_name or not frappe.db.exists("Stock Entry", entry_name):
            raise NKTIdempotencyConflict(
                "Accepted receive_transfer() did not return its incoming Stock Entry."
            )
        entry = _verify_incoming(
            frappe.get_doc("Stock Entry", entry_name),
            journal,
            payload,
        )
        ack = _build_ack(journal, payload, entry, receive_result)
        ack_json = _canonical_ack_json(
            {k: v for k, v in ack.items() if k != "materialization_ack_sha256"}
        )

        frappe.db.set_value(
            PRIMARY_JOURNAL,
            journal.name,
            {
                "downstream_state": "Destination Arrival Materialized",
                "materialized_stock_entry": entry.name,
                "materialized_at": now(),
                "materialization_ack_uuid": ack["materialization_ack_uuid"],
                "materialization_ack_sha256": ack[
                    "materialization_ack_sha256"
                ],
                "materialization_ack_json": ack_json,
            },
            update_modified=False,
        )
        journal.reload()

        verified = _verify_materialized(journal, payload)
        verified["replay"] = False
        verified["receive_transfer_result"] = {
            "status": receive_result.get("status"),
            "transfer": receive_result.get("transfer"),
            "outgoing_stock_entry": receive_result.get("outgoing_stock_entry"),
            "incoming_stock_entry": receive_result.get("incoming_stock_entry"),
            "arrival_quantities": receive_result.get("arrival_quantities"),
            "cumulative_arrived": receive_result.get("cumulative_arrived"),
            "remaining_in_transit": receive_result.get("remaining_in_transit"),
            "completed": receive_result.get("completed"),
            "per_transferred_before": receive_result.get("per_transferred_before"),
            "per_transferred_after": receive_result.get("per_transferred_after"),
            "preserved_offline_arrival_materialized": receive_result.get(
                "preserved_offline_arrival_materialized"
            ),
        }
        return verified
    finally:
        _release_claims(locks)


def installation_probe():
    journal_meta = frappe.get_meta(PRIMARY_JOURNAL)
    required = {
        "materialized_stock_entry",
        "materialized_at",
        "materialization_ack_uuid",
        "materialization_ack_sha256",
        "materialization_ack_json",
    }
    return {
        "foundation_version": FOUNDATION_VERSION,
        "materializer": "receive_transfer",
        "new_stock_engine": False,
        "journal_materialization_fields_ready": required.issubset(
            {f.fieldname for f in journal_meta.fields}
        ),
        "original_edge_operator_preserved": True,
        "original_physical_arrival_time_preserved": True,
        "incoming_stock_entry_posting_time_preserved": True,
        "online_duplicate_block_after_primary_preservation": True,
        "materialization_order_uses_canonical_arrived_snapshot": True,
        "partial_arrival_supported": True,
        "split_10_6_4_destructive_matrix_run": False,
    }


@frappe.whitelist()
def materialize(event_uuid: str):
    return materialize_destination_arrival(event_uuid)
