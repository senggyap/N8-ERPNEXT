from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any, Dict

import frappe
from frappe.utils import flt, getdate, now

from nkt_operations.nkt_store_operations.features.offline_edge.safe_sync import (
    NKTIdempotencyConflict,
    mark_primary_committed,
)
from nkt_operations.nkt_store_operations.features.offline_edge.sync_transport import (
    _runtime_role,
    prepare_event_for_primary,
    validate_transport_packet,
)
from nkt_operations.nkt_store_operations.features.inventory.internal.warehouse_transfer_arrival_intent import (
    ACTION,
    FAMILY,
    TOLERANCE,
    canonical_arrival_payload_json,
    normalize_arrival_payload,
)

FOUNDATION_VERSION = "C15C.10G-R10"
PRIMARY_JOURNAL = "NKT Primary Warehouse Transfer Arrival Intent"
PRIMARY_ITEM = "NKT Primary Warehouse Transfer Arrival Intent Item"
MATERIALIZATION_STATE = "Warehouse Transfer Arrival Intent Preserved"
PRIMARY_ACK_NAMESPACE = uuid.UUID("058de763-c14e-4f83-b6d7-61b7680541e3")


def _uuid(value: Any, label: str) -> str:
    try:
        return str(uuid.UUID(str(value)))
    except Exception as exc:
        raise frappe.ValidationError(f"{label} is invalid.") from exc


def _canonical_json(value: Dict[str, Any]) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    )


def _expected_primary_ack_uuid(event_uuid: str, payload_hash: str) -> str:
    event_uuid = _uuid(event_uuid, "Event UUID")
    payload_hash = str(payload_hash or "").lower()
    if len(payload_hash) != 64 or any(c not in "0123456789abcdef" for c in payload_hash):
        raise frappe.ValidationError("Payload hash is invalid.")
    material = FAMILY + "\0" + event_uuid + "\0" + payload_hash
    return str(uuid.uuid5(PRIMARY_ACK_NAMESPACE, material))


def _require_primary():
    if _runtime_role() != "Primary":
        raise frappe.PermissionError("Transfer Arrival Intent Primary receiver unavailable.")


def _claim_name(kind: str, identity: str) -> str:
    return "nkt-10g-arrival-" + hashlib.sha256(
        f"{kind}:{identity}".encode("utf-8")
    ).hexdigest()[:32]


def _release_lock(name: str) -> None:
    try:
        frappe.db.sql("SELECT RELEASE_LOCK(%s)", (name,))
    except Exception:
        pass


def _acquire_claims(event_uuid: str, transfer_name: str) -> list[str]:
    names = [
        _claim_name("event", event_uuid),
        _claim_name("transfer", transfer_name),
    ]
    acquired = []
    try:
        for name in names:
            row = frappe.db.sql("SELECT GET_LOCK(%s, 10)", (name,))
            if not row or int(row[0][0] or 0) != 1:
                raise frappe.ValidationError(
                    "Could not acquire transfer-Arrival preservation lock."
                )
            acquired.append(name)
        return acquired
    except Exception:
        for name in reversed(acquired):
            _release_lock(name)
        raise


def _receipt_for_update(event_uuid: str):
    rows = frappe.db.sql(
        "SELECT name FROM `tabNKT Sync Primary Receipt` WHERE name=%s FOR UPDATE",
        (event_uuid,),
        as_dict=True,
    )
    return frappe.get_doc("NKT Sync Primary Receipt", event_uuid) if rows else None


def _journal_for_update(event_uuid: str):
    rows = frappe.db.sql(
        f"SELECT name FROM `tab{PRIMARY_JOURNAL}` WHERE name=%s FOR UPDATE",
        (event_uuid,),
        as_dict=True,
    )
    return frappe.get_doc(PRIMARY_JOURNAL, event_uuid) if rows else None


def _pending_reserved_qty(
    transfer_name: str,
    item_code: str,
    *,
    exclude_event_uuid: str | None = None,
) -> float:
    args = [transfer_name, item_code]
    extra = ""
    if exclude_event_uuid:
        extra = " AND p.name != %s"
        args.append(exclude_event_uuid)
    rows = frappe.db.sql(
        f"""
        SELECT COALESCE(SUM(i.arrival_quantity), 0)
        FROM `tab{PRIMARY_ITEM}` i
        INNER JOIN `tab{PRIMARY_JOURNAL}` p ON p.name=i.parent
        WHERE p.warehouse_transfer=%s
          AND i.item_code=%s
          AND p.preservation_state='Preserved'
          AND p.downstream_state='Awaiting Destination Arrival Materialization'
          {extra}
        """,
        tuple(args),
    )
    return flt(rows[0][0] if rows else 0)


def _validate_target_identity(
    payload: Dict[str, Any],
    *,
    event_uuid: str,
    business_date: Any,
) -> Dict[str, Any]:
    payload = normalize_arrival_payload(payload)
    name = payload["warehouse_transfer"]
    if not frappe.db.exists("NKT Warehouse Transfer", name):
        raise frappe.DoesNotExistError(
            "Internal Warehouse Transfer no longer exists on Primary."
        )
    doc = frappe.get_doc("NKT Warehouse Transfer", name)
    if str(doc.status or "") != "In Transit":
        raise frappe.ValidationError(
            "Internal Warehouse Transfer is no longer in open destination-Arrival state."
        )
    if (
        not doc.outgoing_stock_entry
        or str(doc.outgoing_stock_entry) != payload["outgoing_stock_entry"]
        or doc.incoming_stock_entry
    ):
        raise NKTIdempotencyConflict(
            "Primary transfer transit lineage conflicts with immutable Arrival intent."
        )
    if getdate(doc.transfer_date) > getdate(business_date):
        raise frappe.ValidationError(
            "Destination Arrival cannot precede the transfer business date."
        )

    exact = {
        "company": doc.company,
        "transfer_date": str(getdate(doc.transfer_date)),
        "source_warehouse": doc.source_warehouse,
        "destination_warehouse": doc.destination_warehouse,
    }
    for field, actual in exact.items():
        if str(payload[field] or "") != str(actual or ""):
            raise NKTIdempotencyConflict(
                f"Primary transfer target {field} conflicts with immutable Arrival intent."
            )

    outgoing = frappe.get_doc("Stock Entry", doc.outgoing_stock_entry)
    transit_rows = {row.t_warehouse for row in outgoing.items if row.t_warehouse}
    if (
        int(outgoing.docstatus or 0) != 1
        or str(outgoing.purpose or "") != "Material Transfer"
        or str(outgoing.stock_entry_type or "") != "Material Transfer"
        or int(outgoing.add_to_transit or 0) != 1
        or len(transit_rows) != 1
    ):
        raise NKTIdempotencyConflict(
            "Primary outgoing Stock Entry is not the accepted Add-to-Transit lineage."
        )
    transit = next(iter(transit_rows))
    if transit != payload["transit_warehouse"]:
        raise NKTIdempotencyConflict(
            "Primary Goods In Transit warehouse conflicts with immutable Arrival intent."
        )

    local_by_item = {row.item_code: row for row in (doc.items or [])}
    payload_by_item = {row["item_code"]: row for row in payload["items"]}

    active = {}
    reservation_snapshot = {}
    for item_code, local in local_by_item.items():
        released = flt(local.released_qty)
        arrived = flt(local.arrived_qty)
        reserved = _pending_reserved_qty(
            doc.name,
            item_code,
            exclude_event_uuid=event_uuid,
        )
        virtual_cumulative = arrived + reserved
        remaining = max(0.0, released - virtual_cumulative)
        reservation_snapshot[item_code] = {
            "canonical_arrived": arrived,
            "already_preserved_pending": reserved,
            "virtual_cumulative_arrived": virtual_cumulative,
            "available_remaining": remaining,
        }
        if remaining > TOLERANCE:
            active[item_code] = {
                "row": local,
                "released": released,
                "virtual_cumulative": virtual_cumulative,
                "remaining": remaining,
            }

    if set(payload_by_item) != set(active):
        raise frappe.ValidationError(
            "Arrival Intent is stale or out of order: its active Item set no longer matches Primary remaining transit."
        )

    for item_code, state in active.items():
        incoming = payload_by_item[item_code]
        local = state["row"]
        if (
            str(local.name) != incoming["warehouse_transfer_item"]
            or str(local.uom) != incoming["uom"]
            or abs(incoming["released_quantity"] - state["released"]) > TOLERANCE
            or abs(incoming["cumulative_arrived_before"] - state["virtual_cumulative"]) > TOLERANCE
            or abs(incoming["remaining_before"] - state["remaining"]) > TOLERANCE
        ):
            raise NKTIdempotencyConflict(
                f"Arrival Intent is stale or out of order for Item {item_code}."
            )
        if incoming["arrival_quantity"] > state["remaining"] + TOLERANCE:
            raise frappe.ValidationError(
                f"Arrival Intent exceeds unreserved Primary transit quantity for Item {item_code}."
            )

    return {
        "transfer": doc.name,
        "status": doc.status,
        "outgoing_stock_entry": doc.outgoing_stock_entry,
        "transit_warehouse": transit,
        "reservation_snapshot": reservation_snapshot,
    }


def _journal_conflicts(
    journal,
    envelope: Dict[str, Any],
    payload: Dict[str, Any],
    envelope_hash: str,
    payload_hash: str,
) -> list[str]:
    bad = []
    checks = {
        "event_family": FAMILY,
        "event_action": ACTION,
        "origin_device": envelope.get("origin_device"),
        "origin_user": envelope.get("origin_user"),
        "operational_context": envelope.get("operational_context"),
        "warehouse_transfer": payload.get("warehouse_transfer"),
        "company": payload.get("company"),
        "source_warehouse": payload.get("source_warehouse"),
        "destination_warehouse": payload.get("destination_warehouse"),
        "outgoing_stock_entry": payload.get("outgoing_stock_entry"),
        "transit_warehouse": payload.get("transit_warehouse"),
        "envelope_sha256": envelope_hash,
        "payload_sha256": payload_hash,
        "preservation_state": "Preserved",
    }
    for field, expected in checks.items():
        if str(journal.get(field) or "") != str(expected or ""):
            bad.append(field)
    if str(getdate(journal.transfer_date)) != str(getdate(payload["transfer_date"])):
        bad.append("transfer_date")
    if (
        abs(flt(journal.total_arrival_quantity) - flt(payload["total_arrival_quantity"]))
        > TOLERANCE
    ):
        bad.append("total_arrival_quantity")
    if str(journal.canonical_payload_json or "") != canonical_arrival_payload_json(payload):
        bad.append("canonical_payload_json")
    if str(journal.canonical_envelope_json or "") != _canonical_json(envelope):
        bad.append("canonical_envelope_json")

    stored = list(journal.items or [])
    incoming = payload["items"]
    if len(stored) != len(incoming):
        bad.append("items")
    else:
        for old, new in zip(stored, incoming):
            checks = {
                "warehouse_transfer_item": new["warehouse_transfer_item"],
                "item_code": new["item_code"],
                "uom": new["uom"],
            }
            if any(str(old.get(k) or "") != str(v or "") for k, v in checks.items()):
                bad.append(f"item_{old.idx}_identity")
                continue
            for field in (
                "released_quantity",
                "cumulative_arrived_before",
                "remaining_before",
                "arrival_quantity",
            ):
                if abs(flt(old.get(field)) - flt(new[field])) > TOLERANCE:
                    bad.append(f"item_{old.idx}_{field}")
    return bad


def _ack(receipt, journal, *, replay: bool) -> Dict[str, Any]:
    return {
        "event_uuid": receipt.name,
        "event_family": FAMILY,
        "primary_ack_uuid": receipt.primary_ack_uuid,
        "payload_sha256": receipt.payload_sha256,
        "result_code": receipt.result_code,
        "committed": True,
        "replay": bool(replay),
        "canonical_doctype": PRIMARY_JOURNAL,
        "canonical_name": journal.name,
        "materialization_state": receipt.materialization_state,
        "warehouse_transfer": journal.warehouse_transfer,
        "source_warehouse": journal.source_warehouse,
        "destination_warehouse": journal.destination_warehouse,
        "outgoing_stock_entry": journal.outgoing_stock_entry,
        "transit_warehouse": journal.transit_warehouse,
        "total_arrival_quantity": flt(journal.total_arrival_quantity),
        "canonical_arrival_posted": False,
        "stock_entry_created": False,
        "edge_projection_must_remain": True,
        "admin_pre_arrival_approval_required": False,
    }


def prepare_arrival_intent_for_primary(event_uuid: str) -> Dict[str, Any]:
    packet = prepare_event_for_primary(
        event_uuid,
        expected_family=FAMILY,
    )
    rows = frappe.get_all(
        "NKT Edge Warehouse Transfer Projection",
        filters={
            "event_uuid": event_uuid,
            "projection_action": "Destination Arrival",
        },
        pluck="name",
        limit_page_length=500,
    )
    if not rows:
        raise frappe.ValidationError(
            "Transfer Arrival Intent cannot replicate without its Edge projection."
        )
    for name in rows:
        frappe.db.set_value(
            "NKT Edge Warehouse Transfer Projection",
            name,
            "projection_state",
            "Awaiting Primary",
            update_modified=False,
        )
    return packet


def receive_arrival_intent_at_primary(packet: Dict[str, Any]) -> Dict[str, Any]:
    _require_primary()
    envelope, payload, envelope_hash, payload_hash = validate_transport_packet(
        packet,
        expected_family=FAMILY,
    )
    payload = normalize_arrival_payload(payload)
    event_uuid = envelope["event_uuid"]
    transfer_name = payload["warehouse_transfer"]
    expected_ack = _expected_primary_ack_uuid(event_uuid, payload_hash)

    locks = _acquire_claims(event_uuid, transfer_name)
    try:
        receipt = _receipt_for_update(event_uuid)
        if receipt:
            if (
                receipt.event_family != FAMILY
                or receipt.primary_ack_uuid != expected_ack
                or receipt.envelope_sha256 != envelope_hash
                or receipt.payload_sha256 != payload_hash
                or receipt.canonical_doctype != PRIMARY_JOURNAL
                or receipt.canonical_name != event_uuid
                or receipt.materialization_state != MATERIALIZATION_STATE
                or receipt.result_code != "Committed"
            ):
                raise NKTIdempotencyConflict(
                    "Transfer Arrival Primary receipt conflicts with immutable content."
                )
            journal = _journal_for_update(event_uuid)
            if not journal:
                raise NKTIdempotencyConflict(
                    "Transfer Arrival receipt exists without preserved journal."
                )
            bad = _journal_conflicts(
                journal, envelope, payload, envelope_hash, payload_hash
            )
            if bad:
                raise NKTIdempotencyConflict(
                    "Preserved Transfer Arrival conflicts with immutable content: "
                    + ", ".join(bad)
                )
            return _ack(receipt, journal, replay=True)

        target = _validate_target_identity(
            payload,
            event_uuid=event_uuid,
            business_date=envelope["business_date"],
        )

        journal = frappe.get_doc(
            {
                "doctype": PRIMARY_JOURNAL,
                "event_uuid": event_uuid,
                "event_family": FAMILY,
                "event_action": ACTION,
                "origin_device": envelope["origin_device"],
                "origin_user": envelope["origin_user"],
                "operational_context": envelope["operational_context"],
                "business_date": envelope["business_date"],
                "settled_at": envelope["settled_at"],
                "client_created_at": envelope.get("client_created_at"),
                "warehouse_transfer": payload["warehouse_transfer"],
                "company": payload["company"],
                "transfer_date": payload["transfer_date"],
                "source_warehouse": payload["source_warehouse"],
                "destination_warehouse": payload["destination_warehouse"],
                "outgoing_stock_entry": payload["outgoing_stock_entry"],
                "transit_warehouse": payload["transit_warehouse"],
                "total_arrival_quantity": payload["total_arrival_quantity"],
                "envelope_sha256": envelope_hash,
                "payload_sha256": payload_hash,
                "canonical_envelope_json": _canonical_json(envelope),
                "canonical_payload_json": canonical_arrival_payload_json(payload),
                "preservation_state": "Preserved",
                "downstream_state": "Awaiting Destination Arrival Materialization",
                "primary_preserved_at": now(),
                "items": [
                    {
                        "warehouse_transfer_item": row["warehouse_transfer_item"],
                        "item_code": row["item_code"],
                        "uom": row["uom"],
                        "released_quantity": row["released_quantity"],
                        "cumulative_arrived_before": row["cumulative_arrived_before"],
                        "remaining_before": row["remaining_before"],
                        "arrival_quantity": row["arrival_quantity"],
                    }
                    for row in payload["items"]
                ],
            }
        )
        journal.insert(ignore_permissions=True)

        receipt = frappe.get_doc(
            {
                "doctype": "NKT Sync Primary Receipt",
                "event_uuid": event_uuid,
                "event_family": FAMILY,
                "primary_ack_uuid": expected_ack,
                "envelope_sha256": envelope_hash,
                "payload_sha256": payload_hash,
                "primary_received_at": now(),
                "primary_committed_at": now(),
                "result_code": "Committed",
                "canonical_doctype": PRIMARY_JOURNAL,
                "canonical_name": event_uuid,
                "materialization_state": MATERIALIZATION_STATE,
            }
        )
        receipt.insert(ignore_permissions=True)
        mark_primary_committed(
            event_uuid,
            PRIMARY_JOURNAL,
            event_uuid,
            primary_ack_uuid=expected_ack,
        )
        return _ack(receipt, journal, replay=False)
    finally:
        for name in reversed(locks):
            _release_lock(name)


def apply_arrival_preservation_ack_at_edge(ack: Dict[str, Any]) -> Dict[str, Any]:
    if _runtime_role() != "Store Edge":
        raise frappe.PermissionError("Transfer Arrival ACK application unavailable.")
    if not isinstance(ack, dict):
        raise frappe.ValidationError("Transfer Arrival Primary ACK is invalid.")

    event_uuid = _uuid(ack.get("event_uuid"), "Event UUID")
    ack_uuid = _uuid(ack.get("primary_ack_uuid"), "Primary ACK UUID")
    payload_hash = str(ack.get("payload_sha256") or "").lower()
    if (
        ack.get("event_family") != FAMILY
        or ack.get("committed") is not True
        or ack.get("result_code") != "Committed"
        or ack.get("materialization_state") != MATERIALIZATION_STATE
        or ack.get("canonical_doctype") != PRIMARY_JOURNAL
        or ack.get("canonical_name") != event_uuid
    ):
        raise frappe.ValidationError(
            "Transfer Arrival Primary ACK is not a preserved committed ACK."
        )
    if ack_uuid != _expected_primary_ack_uuid(event_uuid, payload_hash):
        raise NKTIdempotencyConflict(
            "Transfer Arrival Primary ACK UUID conflicts with immutable binding."
        )

    if not frappe.db.exists("NKT Sync Event", event_uuid):
        raise frappe.DoesNotExistError(
            "Transfer Arrival event is unavailable at Edge."
        )
    event = frappe.get_doc("NKT Sync Event", event_uuid)
    if event.event_family != FAMILY or event.payload_sha256 != payload_hash:
        raise NKTIdempotencyConflict(
            "Transfer Arrival Primary ACK conflicts with immutable Edge event."
        )

    bound = str(event.primary_ack_uuid or "").strip()
    if bound and bound != ack_uuid:
        raise NKTIdempotencyConflict(
            "Transfer Arrival Primary ACK UUID conflicts with the ACK already bound to this event."
        )

    rows = frappe.get_all(
        "NKT Edge Warehouse Transfer Projection",
        filters={
            "event_uuid": event_uuid,
            "projection_action": "Destination Arrival",
        },
        fields=["name", "projection_state", "primary_ack_uuid"],
        limit_page_length=500,
    )
    if not rows:
        raise frappe.ValidationError(
            "Transfer Arrival ACK cannot apply without its Edge projection."
        )

    for row in rows:
        if row.primary_ack_uuid and row.primary_ack_uuid != ack_uuid:
            raise NKTIdempotencyConflict(
                "Transfer Arrival projection is already bound to another ACK."
            )

    pending = frappe.db.exists("NKT Sync Pending Payload", event_uuid)
    if pending:
        pd = frappe.get_doc("NKT Sync Pending Payload", pending)
        if pd.payload_sha256 != payload_hash:
            raise NKTIdempotencyConflict(
                "Transfer Arrival ACK conflicts with pending payload."
            )

        # Separate Store-Edge and Primary sites see Accepted/Awaiting here. The
        # destructive single-site QA intentionally toggles runtime roles on one DB,
        # so the Primary receiver may already have marked this same event Committed.
        # Accept that state only when its canonical/ACK binding is the exact immutable
        # one returned by the Primary receiver.
        if event.sync_state not in (
            "Accepted at Edge",
            "Awaiting Primary",
            "Committed at Primary",
        ):
            raise frappe.ValidationError(
                "Pending Transfer Arrival event is not eligible for Primary ACK."
            )
        if event.sync_state == "Committed at Primary":
            if (
                event.canonical_doctype != PRIMARY_JOURNAL
                or event.canonical_name != event_uuid
                or str(event.primary_ack_uuid or "").strip() != ack_uuid
            ):
                raise NKTIdempotencyConflict(
                    "Already-committed Transfer Arrival event has another canonical ACK binding."
                )

        for row in rows:
            if row.projection_state not in (
                "Pending Edge",
                "Awaiting Primary",
                "Primary Arrival Preserved",
            ):
                raise NKTIdempotencyConflict(
                    "Transfer Arrival projection is not eligible for preservation ACK."
                )

        mark_primary_committed(
            event_uuid,
            PRIMARY_JOURNAL,
            event_uuid,
            primary_ack_uuid=ack_uuid,
        )
        frappe.delete_doc(
            "NKT Sync Pending Payload",
            pd.name,
            ignore_permissions=True,
            force=True,
        )
        for row in rows:
            if row.projection_state != "Primary Arrival Preserved":
                frappe.db.set_value(
                    "NKT Edge Warehouse Transfer Projection",
                    row.name,
                    {
                        "projection_state": "Primary Arrival Preserved",
                        "primary_ack_uuid": ack_uuid,
                    },
                    update_modified=False,
                )

        return {
            "event_uuid": event_uuid,
            "event_family": FAMILY,
            "primary_ack_uuid": ack_uuid,
            "sync_state": "Committed at Primary",
            "projection_state": "Primary Arrival Preserved",
            "pending_payload_purged": True,
            "replay": False,
        }

    event.reload()
    rows = frappe.get_all(
        "NKT Edge Warehouse Transfer Projection",
        filters={
            "event_uuid": event_uuid,
            "projection_action": "Destination Arrival",
        },
        fields=["name", "projection_state", "primary_ack_uuid"],
        limit_page_length=500,
    )
    if (
        event.sync_state == "Committed at Primary"
        and event.canonical_doctype == PRIMARY_JOURNAL
        and event.canonical_name == event_uuid
        and str(event.primary_ack_uuid or "").strip() == ack_uuid
        and rows
        and all(
            row.projection_state
            in (
                "Primary Arrival Preserved",
                "Primary Arrival Materialized",
                "Finalized",
            )
            and str(row.primary_ack_uuid or "") == ack_uuid
            for row in rows
        )
    ):
        return {
            "event_uuid": event_uuid,
            "event_family": FAMILY,
            "primary_ack_uuid": ack_uuid,
            "sync_state": "Committed at Primary",
            "projection_state": rows[0].projection_state,
            "pending_payload_purged": False,
            "replay": True,
        }

    raise frappe.ValidationError(
        "Transfer Arrival Primary ACK arrived without matching pending or committed state."
    )


def installation_probe() -> Dict[str, Any]:
    journal_meta = frappe.get_meta(PRIMARY_JOURNAL)
    item_meta = frappe.get_meta(PRIMARY_ITEM)
    receipt_state = str(
        frappe.get_meta("NKT Sync Primary Receipt")
        .get_field("materialization_state")
        .options
        or ""
    )

    # Dynamic controller-contract probe: validate the exact new receipt state
    # without inserting a row. This catches JSON/Python enum drift immediately.
    receipt_probe = frappe.get_doc(
        {
            "doctype": "NKT Sync Primary Receipt",
            "event_uuid": str(uuid.uuid4()),
            "event_family": FAMILY,
            "primary_ack_uuid": str(uuid.uuid4()),
            "envelope_sha256": "1" * 64,
            "payload_sha256": "2" * 64,
            "result_code": "Committed",
            "canonical_doctype": PRIMARY_JOURNAL,
            "canonical_name": str(uuid.uuid4()),
            "materialization_state": MATERIALIZATION_STATE,
        }
    )
    receipt_probe.run_method("validate")

    parent_items_field = journal_meta.get_field("items")
    return {
        "foundation_version": FOUNDATION_VERSION,
        "event_family": FAMILY,
        "materialization_state": MATERIALIZATION_STATE,
        "journal_items_table_ready": bool(
            parent_items_field and parent_items_field.options == PRIMARY_ITEM
        ),
        "child_doctype_name_ready": item_meta.name == PRIMARY_ITEM,
        "child_doctype_is_table": bool(item_meta.istable),
        "item_reservation_fields_ready": all(
            item_meta.get_field(field)
            for field in (
                "warehouse_transfer_item",
                "item_code",
                "uom",
                "released_quantity",
                "cumulative_arrived_before",
                "remaining_before",
                "arrival_quantity",
            )
        ),
        "sync_receipt_state_present": MATERIALIZATION_STATE in receipt_state,
        "receipt_python_validator_accepts_arrival_state": True,
        "primary_reservation_prevents_overbooking": True,
        "out_of_order_snapshot_rejected": True,
        "multiple_partial_arrival_events_supported": True,
        "canonical_stock_materialization_enabled": False,
        "split_arrival_destructive_matrix_run": False,
    }
