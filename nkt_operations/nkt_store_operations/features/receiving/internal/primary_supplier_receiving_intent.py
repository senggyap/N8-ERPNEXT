from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime
from typing import Any, Dict
from zoneinfo import ZoneInfo

import frappe
from frappe.utils import cint, flt, getdate, now

from nkt_operations.nkt_store_operations.features.offline_edge.safe_sync import (
    NKTIdempotencyConflict,
    mark_primary_committed,
)
from nkt_operations.nkt_store_operations.features.receiving.supplier_receiving_physical_intent import (
    ACTION,
    FAMILY,
    TOLERANCE,
    canonical_supplier_receiving_payload_json,
    normalize_supplier_receiving_payload,
)
from nkt_operations.nkt_store_operations.features.offline_edge.sync_transport import (
    _runtime_role,
    prepare_event_for_primary,
    validate_transport_packet,
)

FOUNDATION_VERSION = "C15C.10H-R8"
PH_TZ = ZoneInfo("Asia/Manila")
PRIMARY_JOURNAL = "NKT Primary Supplier Receiving Intent"
PRIMARY_ITEM = "NKT Primary Supplier Receiving Intent Item"
MATERIALIZATION_STATE = "Supplier Receiving Intent Preserved"
PRIMARY_ACK_NAMESPACE = uuid.UUID("668b394f-ce67-44cb-bb33-304e3647de5b")

PRIMARY_MATERIALIZATION_ENABLED = True
EDGE_ACCEPTED_STOCK_PROJECTION_ENABLED = True
LOCKED_CROSS_MIDNIGHT_RULE = True
LOCKED_ACCEPTED_GOODS_LOCAL_AVAILABILITY = True


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


def _manila_datetime(value: Any, label: str) -> datetime:
    raw = str(value or "").strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        value_dt = value if isinstance(value, datetime) else datetime.fromisoformat(raw)
    except Exception as exc:
        raise frappe.ValidationError(f"{label} is not a valid datetime.") from exc
    if value_dt.tzinfo is None:
        return value_dt.replace(tzinfo=PH_TZ)
    return value_dt.astimezone(PH_TZ)


def validate_primary_supplier_receiving_contract(packet: Dict[str, Any]) -> Dict[str, Any]:
    envelope, payload, envelope_hash, payload_hash = validate_transport_packet(
        packet,
        expected_family=FAMILY,
    )
    payload = normalize_supplier_receiving_payload(payload)
    if envelope.get("event_action") != ACTION:
        raise frappe.ValidationError("Supplier Receiving Primary contract action is invalid.")

    physical_date = getdate(envelope.get("business_date"))
    receiving_date = getdate(payload.get("receiving_date"))
    settled_at = _manila_datetime(envelope.get("settled_at"), "Physical settled time")
    client_observed_at = _manila_datetime(
        payload.get("client_observed_at"),
        "Client observed time",
    )

    if receiving_date != physical_date:
        raise frappe.ValidationError(
            "Supplier Receiving date must match the immutable Store-Edge business date."
        )
    if settled_at.date() != physical_date:
        raise frappe.ValidationError(
            "Supplier Receiving settled time must match the immutable Store-Edge business date."
        )
    if client_observed_at.date() != physical_date:
        raise frappe.ValidationError(
            "Supplier Receiving client time must match the immutable physical receiving date."
        )

    return {
        "event_uuid": envelope["event_uuid"],
        "event_family": FAMILY,
        "event_action": ACTION,
        "payload_sha256": payload_hash,
        "envelope_sha256": envelope_hash,
        "purchase_order": payload["purchase_order"],
        "supplier": payload["supplier"],
        "receiving_date": physical_date.isoformat(),
        "physical_settled_at_manila": settled_at.isoformat(),
        "client_observed_at_manila": client_observed_at.isoformat(),
        "total_delivered_qty": payload["total_delivered_qty"],
        "total_accepted_qty": payload["total_accepted_qty"],
        "total_rejected_qty": payload["total_rejected_qty"],
        "total_shortage_qty": payload["total_shortage_qty"],
        "primary_materialization_enabled": PRIMARY_MATERIALIZATION_ENABLED,
        "edge_accepted_stock_projection_enabled": EDGE_ACCEPTED_STOCK_PROJECTION_ENABLED,
        "cross_midnight_physical_time_preserved": LOCKED_CROSS_MIDNIGHT_RULE,
        "accepted_goods_local_availability_locked_for_next_stage": LOCKED_ACCEPTED_GOODS_LOCAL_AVAILABILITY,
    }


def _expected_primary_ack_uuid(event_uuid: str, payload_hash: str) -> str:
    event_uuid = _uuid(event_uuid, "Event UUID")
    payload_hash = str(payload_hash or "").lower()
    if len(payload_hash) != 64 or any(c not in "0123456789abcdef" for c in payload_hash):
        raise frappe.ValidationError("Payload hash is invalid.")
    material = FAMILY + "\0" + event_uuid + "\0" + payload_hash
    return str(uuid.uuid5(PRIMARY_ACK_NAMESPACE, material))


def _require_primary():
    if _runtime_role() != "Primary":
        raise frappe.PermissionError("Supplier Receiving Primary receiver unavailable.")


def _claim_name(kind: str, identity: str) -> str:
    return "nkt-10h-supplier-" + hashlib.sha256(
        f"{kind}:{identity}".encode("utf-8")
    ).hexdigest()[:32]


def _release_lock(name: str) -> None:
    try:
        frappe.db.sql("SELECT RELEASE_LOCK(%s)", (name,))
    except Exception:
        pass


def _acquire_claims(event_uuid: str, purchase_order: str) -> list[str]:
    names = [
        _claim_name("event", event_uuid),
        _claim_name("purchase-order", purchase_order),
    ]
    acquired = []
    try:
        for name in names:
            rows = frappe.db.sql("SELECT GET_LOCK(%s,%s)", (name, 30), as_list=True)
            if not rows or int(rows[0][0] or 0) != 1:
                raise frappe.ValidationError(
                    "Supplier Receiving preservation is busy. Safe retry is required."
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


def _lock_purchase_order_for_update(purchase_order: str) -> None:
    rows = frappe.db.sql(
        "SELECT name FROM `tabPurchase Order` WHERE name=%s FOR UPDATE",
        (purchase_order,),
        as_dict=True,
    )
    if not rows:
        raise frappe.DoesNotExistError("Purchase Order is unavailable at Primary.")


def _pending_reserved_delivered_qty(
    purchase_order_item: str,
    *,
    exclude_event_uuid: str | None = None,
) -> float:
    args = [purchase_order_item]
    extra = ""
    if exclude_event_uuid:
        extra = " AND p.name != %s"
        args.append(exclude_event_uuid)
    rows = frappe.db.sql(
        f"""
        SELECT COALESCE(SUM(i.delivered_qty), 0)
        FROM `tab{PRIMARY_ITEM}` i
        INNER JOIN `tab{PRIMARY_JOURNAL}` p ON p.name=i.parent
        WHERE i.purchase_order_item=%s
          AND p.preservation_state='Preserved'
          AND p.downstream_state='Awaiting Supplier Receiving Materialization'
          {extra}
        """,
        tuple(args),
    )
    return flt(rows[0][0] if rows else 0)


def _warehouse_check(warehouse: str, company: str, label: str) -> None:
    row = frappe.db.get_value(
        "Warehouse",
        warehouse,
        ["company", "is_group", "disabled"],
        as_dict=True,
    )
    if not row:
        raise frappe.ValidationError(f"{label} does not exist.")
    if str(row.company or "") != str(company or ""):
        raise frappe.ValidationError(f"{label} must belong to Company {company}.")
    if cint(row.is_group):
        raise frappe.ValidationError(f"{label} cannot be a group Warehouse.")
    if cint(row.disabled):
        raise frappe.ValidationError(f"{label} is disabled.")


def _validate_target_identity(
    payload: Dict[str, Any],
    *,
    event_uuid: str,
    business_date: Any,
) -> Dict[str, Any]:
    payload = normalize_supplier_receiving_payload(payload)
    po = frappe.db.get_value(
        "Purchase Order",
        payload["purchase_order"],
        ["name", "supplier", "company", "docstatus", "status"],
        as_dict=True,
    )
    if not po or int(po.docstatus or 0) != 1:
        raise frappe.ValidationError("Purchase Order must be a submitted live Purchase Order at Primary.")
    if str(po.status or "") in ("Closed", "Cancelled", "Completed"):
        raise frappe.ValidationError("Purchase Order is no longer open for Supplier Receiving.")
    if str(po.company or "") != payload["company"]:
        raise NKTIdempotencyConflict(
            "Primary Purchase Order Company conflicts with immutable Supplier Receiving intent."
        )
    if str(po.supplier or "") != payload["supplier"]:
        raise NKTIdempotencyConflict(
            "Primary Purchase Order Supplier conflicts with immutable Supplier Receiving intent."
        )
    if str(getdate(payload["receiving_date"])) != str(getdate(business_date)):
        raise NKTIdempotencyConflict(
            "Supplier Receiving physical date conflicts with immutable business date."
        )

    _warehouse_check(
        payload["receiving_warehouse"],
        payload["company"],
        "Accepted / Receiving Warehouse",
    )

    if payload.get("delivery_vehicle"):
        vehicle = frappe.db.get_value(
            "NKT Vehicle",
            payload["delivery_vehicle"],
            ["name", "plate_number", "internal_vehicle_no", "status"],
            as_dict=True,
        )
        if not vehicle or str(vehicle.status or "") != "Active":
            raise frappe.ValidationError("Selected NKT Vehicle is unavailable at Primary.")
        from nkt_operations.nkt_store_operations.doctype.nkt_vehicle.nkt_vehicle import normalize_plate
        expected_plate = normalize_plate(vehicle.plate_number) or None
        expected_internal = str(vehicle.internal_vehicle_no or "").strip() or None
        if expected_plate and payload.get("plate_number") != expected_plate:
            raise NKTIdempotencyConflict("Primary vehicle Plate Number conflicts with immutable intent.")
        if expected_internal and payload.get("internal_vehicle_no") != expected_internal:
            raise NKTIdempotencyConflict("Primary vehicle internal number conflicts with immutable intent.")

    po_items = {
        row.name: row
        for row in frappe.get_all(
            "Purchase Order Item",
            filters={
                "parent": payload["purchase_order"],
                "parenttype": "Purchase Order",
            },
            fields=["name", "item_code", "qty", "received_qty", "uom", "stock_uom"],
            limit_page_length=5000,
        )
    }

    reservation_snapshot = {}
    for line in payload["items"]:
        source = po_items.get(line["purchase_order_item"])
        if not source:
            raise NKTIdempotencyConflict(
                "Supplier Receiving row is no longer part of the Primary Purchase Order."
            )
        if str(source.item_code or "") != line["item_code"]:
            raise NKTIdempotencyConflict(
                "Primary Purchase Order Item conflicts with immutable Supplier Receiving intent."
            )
        uom = str(source.uom or source.stock_uom or "")
        if uom != line["uom"]:
            raise NKTIdempotencyConflict(
                "Primary Purchase Order UOM conflicts with immutable Supplier Receiving intent."
            )

        tracking = frappe.db.get_value(
            "Item",
            line["item_code"],
            ["has_serial_no", "has_batch_no"],
            as_dict=True,
        )
        if tracking and (cint(tracking.has_serial_no) or cint(tracking.has_batch_no)):
            raise frappe.ValidationError(
                f"Serialized/batched Item {line['item_code']} is not unlocked for 10H offline receiving."
            )

        pending_delivered = _pending_reserved_delivered_qty(
            line["purchase_order_item"],
            exclude_event_uuid=event_uuid,
        )
        effective_remaining = max(
            flt(source.qty) - flt(source.received_qty) - pending_delivered,
            0.0,
        )
        reservation_snapshot[line["purchase_order_item"]] = {
            "po_qty": flt(source.qty),
            "canonical_received_qty": flt(source.received_qty),
            "already_preserved_pending_delivered": pending_delivered,
            "effective_remaining": effective_remaining,
        }
        if abs(flt(line["expected_qty"]) - effective_remaining) > TOLERANCE:
            raise NKTIdempotencyConflict(
                f"Supplier Receiving intent is stale or out of order for {line['item_code']}."
            )
        if flt(line["delivered_qty"]) > effective_remaining + TOLERANCE:
            raise frappe.ValidationError(
                f"Supplier Receiving intent exceeds effective remaining PO quantity for {line['item_code']}."
            )
        if flt(line["rejected_qty"]) > TOLERANCE:
            _warehouse_check(
                line["rejected_warehouse"],
                payload["company"],
                "Problem-Bag Holding Warehouse",
            )
            if line["rejected_warehouse"] == payload["receiving_warehouse"]:
                raise frappe.ValidationError(
                    f"Problem Bags for {line['item_code']} must use a separate damage/inspection warehouse."
                )

    return {
        "purchase_order": po.name,
        "supplier": po.supplier,
        "company": po.company,
        "reservation_snapshot": reservation_snapshot,
    }


def _journal_conflicts(journal, envelope, payload, envelope_hash, payload_hash) -> list[str]:
    bad = []
    checks = {
        "event_family": FAMILY,
        "event_action": ACTION,
        "origin_device": envelope.get("origin_device"),
        "origin_user": envelope.get("origin_user"),
        "operational_context": envelope.get("operational_context"),
        "purchase_order": payload.get("purchase_order"),
        "company": payload.get("company"),
        "supplier": payload.get("supplier"),
        "receiving_warehouse": payload.get("receiving_warehouse"),
        "bill_of_lading_no": payload.get("bill_of_lading_no"),
        "supplier_dr_no": payload.get("supplier_dr_no"),
        "supplier_delivery_reference": payload.get("supplier_delivery_reference"),
        "delivery_vehicle": payload.get("delivery_vehicle"),
        "internal_vehicle_no": payload.get("internal_vehicle_no"),
        "plate_number": payload.get("plate_number"),
        "driver_name": payload.get("driver_name"),
        "envelope_sha256": envelope_hash,
        "payload_sha256": payload_hash,
        "preservation_state": "Preserved",
    }
    for field, expected in checks.items():
        if str(journal.get(field) or "") != str(expected or ""):
            bad.append(field)
    if str(getdate(journal.receiving_date)) != str(getdate(payload["receiving_date"])):
        bad.append("receiving_date")
    if str(journal.canonical_payload_json or "") != canonical_supplier_receiving_payload_json(payload):
        bad.append("canonical_payload_json")
    if str(journal.canonical_envelope_json or "") != _canonical_json(envelope):
        bad.append("canonical_envelope_json")

    for field in (
        "total_expected_qty","total_delivered_qty","total_accepted_qty",
        "total_damaged_qty","total_other_rejected_qty","total_rejected_qty",
        "total_shortage_qty",
    ):
        if abs(flt(journal.get(field)) - flt(payload[field])) > TOLERANCE:
            bad.append(field)

    stored = list(journal.items or [])
    incoming = payload["items"]
    if len(stored) != len(incoming):
        bad.append("items")
    else:
        for old, new in zip(stored, incoming):
            for field in (
                "line_no","purchase_order_item","item_code","item_name","uom",
                "rejected_warehouse","condition_classification","condition_reason",
            ):
                if str(old.get(field) or "") != str(new.get(field) or ""):
                    bad.append(f"item_{old.idx}_{field}")
            for field in (
                "expected_qty","delivered_qty","accepted_qty","damaged_qty",
                "other_rejected_qty","rejected_qty","shortage_qty",
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
        "purchase_order": journal.purchase_order,
        "supplier": journal.supplier,
        "receiving_warehouse": journal.receiving_warehouse,
        "physical_receiving_date": str(journal.receiving_date),
        "physical_receiving_time": str(journal.settled_at),
        "total_delivered_qty": flt(journal.total_delivered_qty),
        "total_accepted_qty": flt(journal.total_accepted_qty),
        "total_rejected_qty": flt(journal.total_rejected_qty),
        "canonical_supplier_receiving_created": False,
        "purchase_receipt_created": False,
        "edge_projection_must_remain": True,
        "admin_pre_receiving_approval_required": False,
    }


def prepare_supplier_receiving_intent_for_primary(event_uuid: str) -> Dict[str, Any]:
    packet = prepare_event_for_primary(event_uuid, expected_family=FAMILY)
    rows = frappe.get_all(
        "NKT Edge Supplier Receiving Projection",
        filters={"event_uuid": event_uuid},
        pluck="name",
        limit_page_length=500,
    )
    if not rows:
        raise frappe.ValidationError(
            "Supplier Receiving intent cannot replicate without its Edge projection."
        )
    for name in rows:
        frappe.db.set_value(
            "NKT Edge Supplier Receiving Projection",
            name,
            "projection_state",
            "Awaiting Primary",
            update_modified=False,
        )
    return packet


def receive_supplier_receiving_intent_at_primary(packet: Dict[str, Any]) -> Dict[str, Any]:
    _require_primary()
    validate_primary_supplier_receiving_contract(packet)
    envelope, payload, envelope_hash, payload_hash = validate_transport_packet(
        packet,
        expected_family=FAMILY,
    )
    payload = normalize_supplier_receiving_payload(payload)
    event_uuid = envelope["event_uuid"]
    purchase_order = payload["purchase_order"]
    expected_ack = _expected_primary_ack_uuid(event_uuid, payload_hash)

    locks = _acquire_claims(event_uuid, purchase_order)
    try:
        _lock_purchase_order_for_update(purchase_order)
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
                    "Supplier Receiving Primary receipt conflicts with immutable content."
                )
            journal = _journal_for_update(event_uuid)
            if not journal:
                raise NKTIdempotencyConflict(
                    "Supplier Receiving receipt exists without preserved journal."
                )
            bad = _journal_conflicts(journal, envelope, payload, envelope_hash, payload_hash)
            if bad:
                raise NKTIdempotencyConflict(
                    "Preserved Supplier Receiving conflicts with immutable content: "
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
                "purchase_order": payload["purchase_order"],
                "company": payload["company"],
                "supplier": payload["supplier"],
                "receiving_date": payload["receiving_date"],
                "receiving_warehouse": payload["receiving_warehouse"],
                "bill_of_lading_no": payload.get("bill_of_lading_no"),
                "supplier_dr_no": payload.get("supplier_dr_no"),
                "supplier_delivery_reference": payload.get("supplier_delivery_reference"),
                "delivery_vehicle": payload.get("delivery_vehicle"),
                "internal_vehicle_no": payload.get("internal_vehicle_no"),
                "plate_number": payload.get("plate_number"),
                "driver_name": payload.get("driver_name"),
                "receiving_notes": payload.get("receiving_notes"),
                "total_expected_qty": payload["total_expected_qty"],
                "total_delivered_qty": payload["total_delivered_qty"],
                "total_accepted_qty": payload["total_accepted_qty"],
                "total_damaged_qty": payload["total_damaged_qty"],
                "total_other_rejected_qty": payload["total_other_rejected_qty"],
                "total_rejected_qty": payload["total_rejected_qty"],
                "total_shortage_qty": payload["total_shortage_qty"],
                "envelope_sha256": envelope_hash,
                "payload_sha256": payload_hash,
                "canonical_envelope_json": _canonical_json(envelope),
                "canonical_payload_json": canonical_supplier_receiving_payload_json(payload),
                "preservation_state": "Preserved",
                "downstream_state": "Awaiting Supplier Receiving Materialization",
                "primary_preserved_at": now(),
                "items": [
                    {
                        "line_no": row["line_no"],
                        "purchase_order_item": row["purchase_order_item"],
                        "item_code": row["item_code"],
                        "item_name": row.get("item_name"),
                        "uom": row["uom"],
                        "expected_qty": row["expected_qty"],
                        "delivered_qty": row["delivered_qty"],
                        "accepted_qty": row["accepted_qty"],
                        "damaged_qty": row["damaged_qty"],
                        "other_rejected_qty": row["other_rejected_qty"],
                        "rejected_qty": row["rejected_qty"],
                        "shortage_qty": row["shortage_qty"],
                        "rejected_warehouse": row.get("rejected_warehouse"),
                        "condition_classification": row.get("condition_classification"),
                        "condition_reason": row.get("condition_reason"),
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


def apply_supplier_receiving_preservation_ack_at_edge(ack: Dict[str, Any]) -> Dict[str, Any]:
    if _runtime_role() != "Store Edge":
        raise frappe.PermissionError("Supplier Receiving ACK application unavailable.")
    if not isinstance(ack, dict):
        raise frappe.ValidationError("Supplier Receiving Primary ACK is invalid.")

    event_uuid = _uuid(ack.get("event_uuid"), "Event UUID")
    ack_uuid = _uuid(ack.get("primary_ack_uuid"), "Primary ACK UUID")
    payload_hash = str(ack.get("payload_sha256") or "").lower()
    if (
        ack.get("committed") is not True
        or ack.get("result_code") != "Committed"
        or ack.get("materialization_state") != MATERIALIZATION_STATE
        or ack.get("canonical_doctype") != PRIMARY_JOURNAL
        or ack.get("canonical_name") != event_uuid
    ):
        raise frappe.ValidationError(
            "Supplier Receiving Primary ACK is not a preserved committed ACK."
        )

    if not frappe.db.exists("NKT Sync Event", event_uuid):
        raise frappe.DoesNotExistError("Supplier Receiving event is unavailable at Edge.")
    event = frappe.get_doc("NKT Sync Event", event_uuid)
    if event.event_family != FAMILY or str(event.payload_sha256 or "").lower() != payload_hash:
        raise NKTIdempotencyConflict(
            "Supplier Receiving Primary ACK conflicts with immutable Edge event."
        )

    bound = str(event.primary_ack_uuid or "").strip()
    if bound and bound != ack_uuid:
        raise NKTIdempotencyConflict(
            "Supplier Receiving Primary ACK UUID conflicts with the ACK already bound to this event."
        )

    pending = frappe.db.exists("NKT Sync Pending Payload", event_uuid)
    rows = frappe.get_all(
        "NKT Edge Supplier Receiving Projection",
        filters={"event_uuid": event_uuid},
        fields=["name", "projection_state", "primary_ack_uuid"],
        limit_page_length=500,
    )
    if not rows:
        raise frappe.ValidationError(
            "Supplier Receiving ACK cannot apply without its Edge projection."
        )

    if pending:
        pd = frappe.get_doc("NKT Sync Pending Payload", pending)
        if str(pd.payload_sha256 or "").lower() != payload_hash:
            raise NKTIdempotencyConflict(
                "Supplier Receiving ACK conflicts with pending payload."
            )
        if event.sync_state in ("Accepted at Edge", "Awaiting Primary"):
            mark_primary_committed(
                event_uuid,
                PRIMARY_JOURNAL,
                event_uuid,
                primary_ack_uuid=ack_uuid,
            )
        elif event.sync_state == "Committed at Primary":
            if (
                str(event.canonical_doctype or "") != PRIMARY_JOURNAL
                or str(event.canonical_name or "") != event_uuid
                or str(event.primary_ack_uuid or "").strip() != ack_uuid
            ):
                raise NKTIdempotencyConflict(
                    "Pending Supplier Receiving event is already committed with "
                    "different Primary canonical/ACK bindings."
                )
        else:
            raise frappe.ValidationError(
                "Pending Supplier Receiving event is not eligible for Primary ACK."
            )

        frappe.delete_doc(
            "NKT Sync Pending Payload",
            pd.name,
            ignore_permissions=True,
            force=True,
        )
        for row in rows:
            if row.projection_state not in ("Pending Edge", "Awaiting Primary"):
                raise NKTIdempotencyConflict(
                    "Supplier Receiving projection is not eligible for preservation ACK."
                )
            frappe.db.set_value(
                "NKT Edge Supplier Receiving Projection",
                row.name,
                {
                    "projection_state": "Primary Preserved",
                    "primary_ack_uuid": ack_uuid,
                },
                update_modified=False,
            )
        return {
            "event_uuid": event_uuid,
            "event_family": FAMILY,
            "primary_ack_uuid": ack_uuid,
            "sync_state": "Committed at Primary",
            "projection_state": "Primary Preserved",
            "pending_payload_purged": True,
            "replay": False,
        }

    event.reload()
    if (
        event.sync_state == "Committed at Primary"
        and event.canonical_doctype == PRIMARY_JOURNAL
        and event.canonical_name == event_uuid
        and str(event.primary_ack_uuid or "").strip() == ack_uuid
        and all(
            row.projection_state
            in ("Primary Preserved", "Primary Stock Materialized", "Finalized")
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

    raise NKTIdempotencyConflict(
        "Supplier Receiving Primary ACK replay state is incomplete or inconsistent."
    )



MATERIALIZATION_ACK_SIGNED_FIELDS = (
    "event_uuid",
    "payload_sha256",
    "materialization_ack_uuid",
    "purchase_order",
    "supplier",
    "supplier_receiving",
    "purchase_receipt",
    "supplier_delivery_exception",
    "physical_receiving_date",
    "physical_receiving_time",
    "stock_effects",
)


def _canonical_materialization_ack_json(ack: Dict[str, Any]) -> str:
    signed = {field: ack.get(field) for field in MATERIALIZATION_ACK_SIGNED_FIELDS}
    return json.dumps(
        signed,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    )


def _validate_materialization_ack(ack: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(ack, dict):
        raise frappe.ValidationError("Supplier Receiving materialization ACK is invalid.")

    event_uuid = _uuid(ack.get("event_uuid"), "Event UUID")
    materialization_ack_uuid = _uuid(
        ack.get("materialization_ack_uuid"),
        "Materialization ACK UUID",
    )
    payload_hash = str(ack.get("payload_sha256") or "").lower()
    digest = str(ack.get("materialization_ack_sha256") or "").lower()
    if len(payload_hash) != 64 or any(c not in "0123456789abcdef" for c in payload_hash):
        raise frappe.ValidationError("Supplier Receiving materialization payload hash is invalid.")
    if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
        raise frappe.ValidationError("Supplier Receiving materialization ACK hash is invalid.")

    canonical = _canonical_materialization_ack_json(ack)
    expected_digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    if digest != expected_digest:
        raise NKTIdempotencyConflict(
            "Supplier Receiving materialization ACK hash conflicts with signed content."
        )

    required = (
        "purchase_order",
        "supplier",
        "supplier_receiving",
        "purchase_receipt",
        "physical_receiving_date",
        "physical_receiving_time",
    )
    for field in required:
        if not str(ack.get(field) or "").strip():
            raise frappe.ValidationError(
                f"Supplier Receiving materialization ACK {field} is required."
            )
    effects = ack.get("stock_effects")
    if not isinstance(effects, list) or not effects:
        raise frappe.ValidationError(
            "Supplier Receiving materialization ACK stock effects are required."
        )
    return {
        "event_uuid": event_uuid,
        "materialization_ack_uuid": materialization_ack_uuid,
        "payload_sha256": payload_hash,
        "materialization_ack_sha256": digest,
        "canonical_ack_json": canonical,
    }


def _materialization_projection_rows(event_uuid: str):
    rows = frappe.get_all(
        "NKT Edge Supplier Receiving Projection",
        filters={"event_uuid": event_uuid},
        fields=[
            "name", "event_uuid", "line_no", "purchase_order",
            "purchase_order_item", "company", "supplier", "item_code", "uom",
            "warehouse", "delivered_qty", "accepted_qty", "rejected_qty",
            "rejected_warehouse", "projection_state", "primary_ack_uuid",
            "materialization_ack_uuid", "materialization_ack_sha256",
            "primary_supplier_receiving", "primary_purchase_receipt",
            "primary_post_actual_qty", "primary_materialized_at", "finalized_at",
        ],
        order_by="line_no asc",
        limit_page_length=500,
    )
    if not rows:
        raise frappe.ValidationError(
            "Supplier Receiving materialization ACK cannot apply without its Edge projection."
        )
    return rows


def _validate_materialization_projection_bindings(
    ack: Dict[str, Any],
    event,
    rows,
    validated: Dict[str, Any],
) -> Dict[str, Dict[str, Any]]:
    if event.event_family != FAMILY:
        raise NKTIdempotencyConflict(
            "Supplier Receiving materialization ACK event family conflicts with Edge event."
        )
    if str(event.payload_sha256 or "").lower() != validated["payload_sha256"]:
        raise NKTIdempotencyConflict(
            "Supplier Receiving materialization ACK payload conflicts with Edge event."
        )
    if (
        event.sync_state != "Committed at Primary"
        or str(event.canonical_doctype or "") != PRIMARY_JOURNAL
        or str(event.canonical_name or "") != validated["event_uuid"]
        or not str(event.primary_ack_uuid or "").strip()
    ):
        raise NKTIdempotencyConflict(
            "Supplier Receiving Edge event is not safely preserved at Primary."
        )
    if frappe.db.exists("NKT Sync Pending Payload", validated["event_uuid"]):
        raise NKTIdempotencyConflict(
            "Supplier Receiving materialization ACK cannot finalize while a pending payload remains."
        )

    effects = {}
    for effect in ack["stock_effects"]:
        if not isinstance(effect, dict):
            raise frappe.ValidationError(
                "Supplier Receiving materialization ACK stock effect is invalid."
            )
        key = str(effect.get("purchase_order_item") or "").strip()
        if not key or key in effects:
            raise NKTIdempotencyConflict(
                "Supplier Receiving materialization ACK has duplicate/missing PO Item effects."
            )
        effects[key] = effect

    if len(effects) != len(rows):
        raise NKTIdempotencyConflict(
            "Supplier Receiving materialization ACK stock-effect count conflicts with Edge projection."
        )

    for row in rows:
        if str(row.purchase_order or "") != str(ack.get("purchase_order") or ""):
            raise NKTIdempotencyConflict(
                "Supplier Receiving materialization ACK Purchase Order conflicts with Edge projection."
            )
        if str(row.supplier or "") != str(ack.get("supplier") or ""):
            raise NKTIdempotencyConflict(
                "Supplier Receiving materialization ACK Supplier conflicts with Edge projection."
            )
        if str(row.primary_ack_uuid or "") != str(event.primary_ack_uuid or ""):
            raise NKTIdempotencyConflict(
                "Supplier Receiving projection lost its Primary preservation ACK binding."
            )
        if row.projection_state not in (
            "Primary Preserved", "Primary Stock Materialized", "Finalized"
        ):
            raise NKTIdempotencyConflict(
                "Supplier Receiving projection is not eligible for materialization ACK."
            )

        effect = effects.get(str(row.purchase_order_item or ""))
        if not effect:
            raise NKTIdempotencyConflict(
                "Supplier Receiving materialization ACK is missing a projected PO Item."
            )
        string_checks = {
            "item_code": row.item_code,
            "warehouse": row.warehouse,
            "rejected_warehouse": row.rejected_warehouse,
        }
        for field, expected in string_checks.items():
            if str(effect.get(field) or "") != str(expected or ""):
                raise NKTIdempotencyConflict(
                    f"Supplier Receiving materialization ACK {field} conflicts with Edge projection."
                )
        if (
            int(effect.get("line_no") or 0) != int(row.line_no or 0)
            or abs(flt(effect.get("accepted_qty")) - flt(row.accepted_qty)) > TOLERANCE
            or abs(flt(effect.get("rejected_qty")) - flt(row.rejected_qty)) > TOLERANCE
        ):
            raise NKTIdempotencyConflict(
                "Supplier Receiving materialization ACK quantities conflict with Edge projection."
            )
    return effects


def _local_supplier_receiving_canonical_stock_evidence(
    ack: Dict[str, Any],
    rows,
    effects: Dict[str, Dict[str, Any]],
) -> Dict[str, Any]:
    receiving_name = str(ack.get("supplier_receiving") or "").strip()
    pr_name = str(ack.get("purchase_receipt") or "").strip()
    if not frappe.db.exists("NKT Supplier Receiving", receiving_name):
        return {"visible": False, "reason": "canonical_supplier_receiving_not_local"}
    if not frappe.db.exists("Purchase Receipt", pr_name):
        return {"visible": False, "reason": "canonical_purchase_receipt_not_local"}

    receiving = frappe.get_doc("NKT Supplier Receiving", receiving_name)
    pr = frappe.get_doc("Purchase Receipt", pr_name)
    if (
        int(receiving.docstatus or 0) != 1
        or str(receiving.posting_status or "") != "Posted"
        or str(receiving.underlying_purchase_receipt or "") != pr_name
        or str(receiving.purchase_order or "") != str(ack.get("purchase_order") or "")
        or str(receiving.supplier or "") != str(ack.get("supplier") or "")
    ):
        raise NKTIdempotencyConflict(
            "Local canonical Supplier Receiving conflicts with materialization ACK."
        )
    if (
        int(pr.docstatus or 0) != 1
        or str(pr.supplier or "") != str(ack.get("supplier") or "")
        or str(getdate(pr.posting_date)) != str(getdate(ack.get("physical_receiving_date")))
    ):
        raise NKTIdempotencyConflict(
            "Local canonical Purchase Receipt conflicts with materialization ACK."
        )

    pr_rows = {str(r.purchase_order_item or ""): r for r in (pr.items or [])}
    evidence = []
    for row in rows:
        po_item = str(row.purchase_order_item or "")
        pr_row = pr_rows.get(po_item)
        effect = effects[po_item]
        if not pr_row:
            return {
                "visible": False,
                "reason": "canonical_purchase_receipt_row_not_local",
                "purchase_order_item": po_item,
            }
        if (
            str(pr_row.item_code or "") != str(row.item_code or "")
            or str(pr_row.warehouse or "") != str(row.warehouse or "")
            or str(pr_row.rejected_warehouse or "") != str(row.rejected_warehouse or "")
            or abs(flt(pr_row.qty) - flt(row.accepted_qty)) > TOLERANCE
            or abs(flt(pr_row.rejected_qty) - flt(row.rejected_qty)) > TOLERANCE
        ):
            raise NKTIdempotencyConflict(
                "Local canonical Purchase Receipt row conflicts with Edge projection."
            )

        accepted_sle = flt(
            frappe.db.sql(
                """
                SELECT COALESCE(SUM(actual_qty),0)
                FROM `tabStock Ledger Entry`
                WHERE voucher_type='Purchase Receipt'
                  AND voucher_no=%s
                  AND item_code=%s
                  AND warehouse=%s
                """,
                (pr_name, row.item_code, row.warehouse),
            )[0][0]
        )
        if abs(accepted_sle - flt(row.accepted_qty)) > TOLERANCE:
            return {
                "visible": False,
                "reason": "accepted_stock_ledger_effect_not_local",
                "purchase_order_item": po_item,
                "expected": flt(row.accepted_qty),
                "observed": accepted_sle,
            }

        rejected_sle = 0.0
        if flt(row.rejected_qty) > TOLERANCE:
            rejected_sle = flt(
                frappe.db.sql(
                    """
                    SELECT COALESCE(SUM(actual_qty),0)
                    FROM `tabStock Ledger Entry`
                    WHERE voucher_type='Purchase Receipt'
                      AND voucher_no=%s
                      AND item_code=%s
                      AND warehouse=%s
                    """,
                    (pr_name, row.item_code, row.rejected_warehouse),
                )[0][0]
            )
            if abs(rejected_sle - flt(row.rejected_qty)) > TOLERANCE:
                return {
                    "visible": False,
                    "reason": "rejected_stock_ledger_effect_not_local",
                    "purchase_order_item": po_item,
                    "expected": flt(row.rejected_qty),
                    "observed": rejected_sle,
                }

        evidence.append(
            {
                "purchase_order_item": po_item,
                "item_code": row.item_code,
                "accepted_sle_qty": accepted_sle,
                "rejected_sle_qty": rejected_sle,
                "primary_post_actual_qty": flt(effect.get("primary_post_actual_qty")),
            }
        )

    return {
        "visible": True,
        "reason": "canonical_purchase_receipt_and_stock_ledger_visible",
        "supplier_receiving": receiving_name,
        "purchase_receipt": pr_name,
        "stock_evidence": evidence,
    }


def apply_supplier_receiving_materialization_ack_at_edge(
    ack: Dict[str, Any],
) -> Dict[str, Any]:
    if _runtime_role() != "Store Edge":
        raise frappe.PermissionError(
            "Supplier Receiving materialization ACK application unavailable."
        )

    validated = _validate_materialization_ack(ack)
    event_uuid = validated["event_uuid"]
    if not frappe.db.exists("NKT Sync Event", event_uuid):
        raise frappe.DoesNotExistError(
            "Supplier Receiving event is unavailable at Edge."
        )
    event = frappe.get_doc("NKT Sync Event", event_uuid)
    rows = _materialization_projection_rows(event_uuid)
    effects = _validate_materialization_projection_bindings(
        ack, event, rows, validated
    )

    existing_states = {str(row.projection_state or "") for row in rows}
    if "Finalized" in existing_states and existing_states != {"Finalized"}:
        raise NKTIdempotencyConflict(
            "Supplier Receiving projection finalization is partially applied."
        )

    # Once a materialization ACK is bound, all replays must be byte-equivalent
    # in their signed/hash identity and canonical document bindings.
    for row in rows:
        bound_ack = str(row.materialization_ack_uuid or "").strip()
        bound_hash = str(row.materialization_ack_sha256 or "").lower()
        if bound_ack and bound_ack != validated["materialization_ack_uuid"]:
            raise NKTIdempotencyConflict(
                "Supplier Receiving projection already has a different materialization ACK UUID."
            )
        if bound_hash and bound_hash != validated["materialization_ack_sha256"]:
            raise NKTIdempotencyConflict(
                "Supplier Receiving projection already has a different materialization ACK hash."
            )
        if row.primary_supplier_receiving and str(row.primary_supplier_receiving) != str(ack["supplier_receiving"]):
            raise NKTIdempotencyConflict(
                "Supplier Receiving projection already has a different canonical Supplier Arrival."
            )
        if row.primary_purchase_receipt and str(row.primary_purchase_receipt) != str(ack["purchase_receipt"]):
            raise NKTIdempotencyConflict(
                "Supplier Receiving projection already has a different canonical Purchase Receipt."
            )

    if existing_states == {"Finalized"}:
        return {
            "event_uuid": event_uuid,
            "event_family": FAMILY,
            "materialization_ack_uuid": validated["materialization_ack_uuid"],
            "projection_state": "Finalized",
            "canonical_stock_visible": True,
            "finalized": True,
            "replay": True,
        }

    evidence = _local_supplier_receiving_canonical_stock_evidence(
        ack, rows, effects
    )

    # Always bind the canonical materialization ACK first. If its Stock Ledger
    # effect has not replicated locally yet, keep the temporary accepted-goods
    # projection active as Primary Stock Materialized. This avoids a stock dip.
    for row in rows:
        effect = effects[str(row.purchase_order_item or "")]
        values = {
            "projection_state": (
                "Finalized" if evidence.get("visible") else "Primary Stock Materialized"
            ),
            "materialization_ack_uuid": validated["materialization_ack_uuid"],
            "materialization_ack_sha256": validated["materialization_ack_sha256"],
            "primary_supplier_receiving": ack["supplier_receiving"],
            "primary_purchase_receipt": ack["purchase_receipt"],
            "primary_post_actual_qty": flt(effect.get("primary_post_actual_qty")),
        }
        if evidence.get("visible"):
            values["finalized_at"] = now()
        frappe.db.set_value(
            "NKT Edge Supplier Receiving Projection",
            row.name,
            values,
            update_modified=False,
        )

    return {
        "event_uuid": event_uuid,
        "event_family": FAMILY,
        "materialization_ack_uuid": validated["materialization_ack_uuid"],
        "projection_state": (
            "Finalized" if evidence.get("visible") else "Primary Stock Materialized"
        ),
        "canonical_stock_visible": bool(evidence.get("visible")),
        "canonical_stock_evidence": evidence,
        "finalized": bool(evidence.get("visible")),
        "replay": False,
    }

def contract_status() -> Dict[str, Any]:
    return {
        "foundation_version": FOUNDATION_VERSION,
        "event_family": FAMILY,
        "event_action": ACTION,
        "transport_registered": True,
        "generic_primary_receipt_allowed": False,
        "primary_preservation_enabled": True,
        "primary_materialization_enabled": PRIMARY_MATERIALIZATION_ENABLED,
        "edge_acceptance_enabled": True,
        "edge_accepted_stock_projection_enabled": EDGE_ACCEPTED_STOCK_PROJECTION_ENABLED,
        "cross_midnight_physical_time_preserved": LOCKED_CROSS_MIDNIGHT_RULE,
        "employee_backdate_control_enabled": False,
        "accepted_goods_local_availability_enabled": LOCKED_ACCEPTED_GOODS_LOCAL_AVAILABILITY,
        "po_remaining_reserves_all_physically_delivered_bags": True,
        "supplier_money_side_offline_enabled": False,
        "canonical_purchase_receipt_at_edge": False,
        "edge_materialization_ack_rebase_enabled": True,
        "projection_finalization_requires_local_canonical_purchase_receipt": True,
        "projection_finalization_requires_local_stock_ledger_evidence": True,
        "sync_lag_keeps_accepted_goods_projection_active": True,
    }


@frappe.whitelist()
def receive_supplier_receiving_intent(packet: Dict[str, Any]):
    if isinstance(packet, str):
        packet = frappe.parse_json(packet)
    return receive_supplier_receiving_intent_at_primary(packet)

@frappe.whitelist()
def apply_supplier_receiving_materialization_ack(ack: Dict[str, Any]):
    if isinstance(ack, str):
        ack = frappe.parse_json(ack)
    return apply_supplier_receiving_materialization_ack_at_edge(ack)

