from __future__ import annotations

import json
import uuid
from typing import Any, Dict

import frappe
from frappe.utils import cint, flt, getdate, now

from nkt_operations.nkt_store_operations.features.sales.customer_order_intent import (
    ORDER_INTENT_FAMILY,
)
from nkt_operations.nkt_store_operations.features.offline_edge.safe_sync import (
    NKTIdempotencyConflict,
    mark_primary_committed,
)
from nkt_operations.nkt_store_operations.features.offline_edge.sync_transport import (
    _runtime_role,
    _uuid,
    prepare_event_for_primary,
    validate_transport_packet,
)

FOUNDATION_VERSION = "C15C.9I-R6"


def _require_primary():
    if _runtime_role() != "Primary":
        raise frappe.PermissionError("Customer Order materialization unavailable.")


def _field(meta, candidates, *, required=True):
    for name in candidates:
        if meta.has_field(name):
            return name
    if required:
        raise frappe.ValidationError(
            "Customer Order schema is missing a required materialization field."
        )
    return None


def _master_is_disabled(doctype: str, name: str) -> bool:
    meta = frappe.get_meta(doctype)
    if not meta.has_field("disabled"):
        return False
    return bool(cint(frappe.db.get_value(doctype, name, "disabled") or 0))


def _validate_master_links(payload: Dict[str, Any]) -> None:
    if not frappe.db.exists("Company", payload["company"]):
        raise frappe.ValidationError("Offline order Company is unavailable on Primary.")
    if _master_is_disabled("Company", payload["company"]):
        raise frappe.ValidationError(
            f"Offline order Company is disabled on Primary: {payload['company']}"
        )

    if not frappe.db.exists("Customer", payload["customer"]):
        raise frappe.ValidationError("Offline order Customer is unavailable on Primary.")
    if _master_is_disabled("Customer", payload["customer"]):
        raise frappe.ValidationError(
            f"Offline order Customer is disabled on Primary: {payload['customer']}"
        )

    warehouses = {payload["default_warehouse"]}
    warehouses.update(line["warehouse"] for line in payload["items"])
    for warehouse in warehouses:
        if not frappe.db.exists("Warehouse", warehouse):
            raise frappe.ValidationError(
                f"Offline order Warehouse is unavailable on Primary: {warehouse}"
            )
        if _master_is_disabled("Warehouse", warehouse):
            raise frappe.ValidationError(
                f"Offline order Warehouse is disabled on Primary: {warehouse}"
            )

    from nkt_operations.nkt_store_operations.features.inventory.order_fulfillment import (
        validate_normal_sale_item,
    )

    for idx, line in enumerate(payload["items"], start=1):
        if not frappe.db.exists("Item", line["item_code"]):
            raise frappe.ValidationError(
                f"Offline order Item is unavailable on Primary: {line['item_code']}"
            )
        if _master_is_disabled("Item", line["item_code"]):
            raise frappe.ValidationError(
                f"Offline order Item is disabled on Primary: {line['item_code']}"
            )
        validate_normal_sale_item(line["item_code"], idx)
        if flt(line["qty"]) <= 0:
            raise frappe.ValidationError("Offline order Qty must be greater than zero.")
        if flt(line["rate"]) < 0:
            raise frappe.ValidationError("Offline order Rate cannot be negative.")


def _current_primary_selling_rate(item_code: str) -> Dict[str, Any]:
    """Resolve the same accepted C15C.6A/R7 Primary selling-price precedence."""
    today = getdate()
    meta = frappe.get_meta("Item Price")
    filters = {"item_code": item_code}
    if meta.has_field("price_list"):
        filters["price_list"] = "Standard Selling"
    if meta.has_field("selling"):
        filters["selling"] = 1

    fields = ["name", "price_list_rate", "modified"]
    for field in ("valid_from", "valid_upto", "uom", "currency", "price_list", "selling"):
        if meta.has_field(field) and field not in fields:
            fields.append(field)

    order_by = (
        "valid_from desc, modified desc"
        if meta.has_field("valid_from")
        else "modified desc"
    )
    rows = frappe.get_all(
        "Item Price",
        filters=filters,
        fields=fields,
        order_by=order_by,
        limit_page_length=1000,
    )
    for row in rows:
        valid_from = getattr(row, "valid_from", None)
        valid_upto = getattr(row, "valid_upto", None)
        if valid_from and getdate(valid_from) > today:
            continue
        if valid_upto and getdate(valid_upto) < today:
            continue
        return {
            "rate": flt(row.price_list_rate),
            "source": "Active Standard Selling Item Price",
            "source_name": row.name,
        }

    standard_rate = flt(
        frappe.db.get_value("Item", item_code, "standard_rate") or 0
    )
    if standard_rate:
        return {
            "rate": standard_rate,
            "source": "Item.standard_rate",
            "source_name": item_code,
        }
    return {"rate": 0.0, "source": "Zero fallback", "source_name": item_code}


def _validate_reconnect_prices(payload: Dict[str, Any]) -> None:
    for idx, line in enumerate(payload["items"], start=1):
        current = _current_primary_selling_rate(line["item_code"])
        intended = flt(line["rate"])
        authoritative = flt(current["rate"])
        if abs(intended - authoritative) > 0.000001:
            raise frappe.ValidationError(
                "Offline price conflict after reconnect on row "
                f"{idx} ({line['item_code']}): offline agreed rate "
                f"{intended:,.2f} does not match current Primary rate "
                f"{authoritative:,.2f}. Manager-authorized price resolution is required; "
                "the system will not silently reprice the customer's offline order."
            )


def _assert_materialized_prices_match_intent(doc, payload: Dict[str, Any]) -> None:
    mapping = customer_order_item_schema_map()
    rows = list(doc.get(mapping["table_field"]) or [])
    if len(rows) != len(payload["items"]):
        raise frappe.ValidationError(
            "Offline price conflict: materialized order lines no longer match the immutable intent."
        )
    for idx, (row, intended_line) in enumerate(zip(rows, payload["items"]), start=1):
        actual = flt(row.get(mapping["rate"]))
        intended = flt(intended_line["rate"])
        if abs(actual - intended) > 0.000001:
            raise frappe.ValidationError(
                "Offline price conflict during Primary materialization on row "
                f"{idx} ({intended_line['item_code']}): immutable offline rate "
                f"{intended:,.2f} would be rewritten to {actual:,.2f}. "
                "Manager-authorized price resolution is required."
            )


def _existing_order_for_event(event_uuid: str):
    meta = frappe.get_meta("NKT Customer Order")
    if not meta.has_field("custom_nkt_fast_request_id"):
        raise frappe.ValidationError(
            "NKT Customer Order is missing the accepted unique Fast Request ID field."
        )
    name = frappe.db.get_value(
        "NKT Customer Order",
        {"custom_nkt_fast_request_id": event_uuid},
        "name",
    )
    return frappe.get_doc("NKT Customer Order", name) if name else None


def customer_order_item_schema_map() -> Dict[str, Any]:
    """
    Resolve the accepted live NKT Customer Order Item schema.

    The current live child table uses:
      item, item_name, quantity, uom, final_rate, amount,
      source_warehouse, remarks

    Candidate fallbacks are retained only for forward schema tolerance.
    """
    parent_meta = frappe.get_meta("NKT Customer Order")
    items_field = _field(parent_meta, ["items"])
    table_df = parent_meta.get_field(items_field)
    child_meta = frappe.get_meta(table_df.options)

    mapping = {
        "table_field": items_field,
        "child_doctype": table_df.options,
        "item": _field(child_meta, ["item", "item_code"]),
        "quantity": _field(child_meta, ["quantity", "qty"]),
        "rate": _field(child_meta, ["final_rate", "rate"]),
        "warehouse": _field(
            child_meta,
            ["source_warehouse", "warehouse"],
            required=False,
        ),
        "item_name": _field(child_meta, ["item_name"], required=False),
        "uom": _field(child_meta, ["uom"], required=False),
        "amount": _field(child_meta, ["amount"], required=False),
        "remarks": _field(child_meta, ["remarks"], required=False),
    }
    return mapping


def _append_materialized_items(doc, payload):
    mapping = customer_order_item_schema_map()

    for line in payload["items"]:
        row = {
            mapping["item"]: line["item_code"],
            mapping["quantity"]: flt(line["qty"]),
            mapping["rate"]: flt(line["rate"]),
        }
        if mapping["warehouse"]:
            row[mapping["warehouse"]] = line["warehouse"]

        # item_name / uom / amount are intentionally NOT fabricated here.
        # The accepted NKT Customer Order.before_validate() owns
        # populate_item_details() + calculate_totals().
        doc.append(mapping["table_field"], row)


def _create_controlled_draft(event_uuid: str, envelope: Dict[str, Any], payload: Dict[str, Any]):
    existing = _existing_order_for_event(event_uuid)
    if existing:
        if int(existing.docstatus or 0) != 0:
            raise NKTIdempotencyConflict(
                "Offline materialization Event UUID is already bound to a non-draft order."
            )
        return existing, True

    _validate_master_links(payload)
    _validate_reconnect_prices(payload)

    meta = frappe.get_meta("NKT Customer Order")
    doc = frappe.new_doc("NKT Customer Order")

    values = {
        "company": payload["company"],
        "order_date": envelope["business_date"],
        "customer": payload["customer"],
        "default_warehouse": payload["default_warehouse"],
        "account_sale": 1 if payload["account_sale"] else 0,
        "notes": payload.get("notes") or "",
        "encoder": envelope.get("origin_user"),
        "custom_nkt_fast_request_id": event_uuid,
        "custom_nkt_fast_ui_version": payload.get("client_ui_version")
        or "C15C9H-OFFLINE-INTENT",
        "status": "Draft",
    }
    for field, value in values.items():
        if meta.has_field(field):
            doc.set(field, value)

    _append_materialized_items(doc, payload)

    # The accepted live Customer Order schema makes declared_payments mandatory.
    # 9G deliberately excludes tender/payment data. This controlled Primary draft
    # bypasses ONLY the schema-level mandatory-table check. Frappe/controller Draft
    # validation still runs normally. It is NOT submitted and cannot run
    # fulfillment/AR/matching until real tender context is later supplied.
    doc.flags.ignore_mandatory = True
    doc.flags.nkt_c15c_offline_intent_materialization = True
    # C15C.10D R4: preserve the immutable Store Edge Encoder identity while
    # the controlled Primary service account inserts this offline Draft.
    # Normal online Customer Order drafts still derive Encoder from the
    # logged-in user; this server-only flag exists only for this materializer.
    doc.flags.nkt_c15c_preserve_offline_encoder = True
    try:
        doc.insert(ignore_permissions=True, ignore_mandatory=True)
        _assert_materialized_prices_match_intent(doc, payload)
    finally:
        # Both bypass flags are intentionally transient and server-only.
        # Neither may remain on the in-memory Document after insertion.
        doc.flags.nkt_c15c_offline_intent_materialization = False
        doc.flags.nkt_c15c_preserve_offline_encoder = False

    return doc, False



def guard_offline_materialized_order_price_drift(doc, method=None):
    """
    Fail explicitly if normal Customer Order before_validate() would silently
    reprice an already materialized offline Draft after reconnect.

    This hook is intentionally narrow:
    - existing NKT Customer Order only;
    - exact Sync Primary Receipt must bind the Event UUID to this order;
    - compares persisted pre-save rates to the rates produced by normal
      before_validate pricing;
    - no bypass or automatic repricing is performed here.
    """
    if getattr(doc, "is_new", lambda: False)():
        return

    event_uuid = str(doc.get("custom_nkt_fast_request_id") or "").strip()
    if not event_uuid or not doc.name:
        return

    receipt = frappe.db.get_value(
        "NKT Sync Primary Receipt",
        event_uuid,
        ["canonical_doctype", "canonical_name", "materialization_state"],
        as_dict=True,
    )
    if (
        not receipt
        or receipt.canonical_doctype != "NKT Customer Order"
        or receipt.canonical_name != doc.name
        or receipt.materialization_state != "Canonical Draft Materialized"
    ):
        return

    before = doc.get_doc_before_save()
    if not before:
        return

    mapping = customer_order_item_schema_map()
    old_rows = list(before.get(mapping["table_field"]) or [])
    new_rows = list(doc.get(mapping["table_field"]) or [])
    if len(old_rows) != len(new_rows):
        return

    for idx, (old_row, new_row) in enumerate(zip(old_rows, new_rows), start=1):
        old_item = str(old_row.get(mapping["item"]) or "")
        new_item = str(new_row.get(mapping["item"]) or "")
        if old_item != new_item:
            continue
        old_rate = flt(old_row.get(mapping["rate"]))
        new_rate = flt(new_row.get(mapping["rate"]))
        if abs(old_rate - new_rate) > 0.000001:
            raise frappe.ValidationError(
                "Offline price conflict after reconnect on row "
                f"{idx} ({new_item}): agreed Draft rate {old_rate:,.2f} "
                f"would be rewritten to current Primary rate {new_rate:,.2f}. "
                "Manager-authorized price resolution is required before finalization."
            )


def prepare_customer_order_intent_for_primary(event_uuid: str) -> Dict[str, Any]:
    packet = prepare_event_for_primary(
        event_uuid,
        expected_family=ORDER_INTENT_FAMILY,
    )

    # The Edge pending quantity projection follows the packet into Awaiting Primary.
    rows = frappe.get_all(
        "NKT Edge Order Reservation Projection",
        filters={"event_uuid": event_uuid},
        pluck="name",
        limit_page_length=500,
    )
    if not rows:
        raise frappe.ValidationError(
            "Customer Order Intent cannot replicate without its pending reservation projection."
        )
    for name in rows:
        frappe.db.set_value(
            "NKT Edge Order Reservation Projection",
            name,
            "projection_state",
            "Awaiting Primary",
            update_modified=False,
        )
    return packet


def _receipt_conflict_fields(receipt, envelope_hash: str, payload_hash: str):
    mismatches = []
    if receipt.event_family != ORDER_INTENT_FAMILY:
        mismatches.append("event_family")
    if receipt.envelope_sha256 != envelope_hash:
        mismatches.append("envelope_sha256")
    if receipt.payload_sha256 != payload_hash:
        mismatches.append("payload_sha256")
    if receipt.materialization_state != "Canonical Draft Materialized":
        mismatches.append("materialization_state")
    if receipt.canonical_doctype != "NKT Customer Order":
        mismatches.append("canonical_doctype")
    if not str(receipt.canonical_name or "").strip():
        mismatches.append("canonical_name")
    return mismatches


def _ack_from_materialized_receipt(receipt, *, replay: bool) -> Dict[str, Any]:
    order = frappe.get_doc("NKT Customer Order", receipt.canonical_name)
    event_uuid = str(receipt.name)
    if str(order.get("custom_nkt_fast_request_id") or "") != event_uuid:
        raise NKTIdempotencyConflict(
            "Materialized Customer Order is not bound to the immutable Event UUID."
        )
    return {
        "event_uuid": event_uuid,
        "event_family": ORDER_INTENT_FAMILY,
        "primary_ack_uuid": receipt.primary_ack_uuid,
        "payload_sha256": receipt.payload_sha256,
        "result_code": receipt.result_code,
        "committed": True,
        "replay": bool(replay),
        "canonical_doctype": "NKT Customer Order",
        "canonical_name": order.name,
        "canonical_docstatus": int(order.docstatus or 0),
        "materialization_state": receipt.materialization_state,
        "normal_submit_lifecycle_executed": False,
    }


def _current_receipt_row_for_update(event_uuid: str):
    rows = frappe.db.sql(
        """
        SELECT
            name, event_uuid, event_family, primary_ack_uuid,
            envelope_sha256, payload_sha256,
            canonical_doctype, canonical_name, materialization_state,
            primary_received_at, primary_committed_at, result_code
        FROM `tabNKT Sync Primary Receipt`
        WHERE name=%s
        FOR UPDATE
        """,
        (event_uuid,),
        as_dict=True,
    )
    return rows[0] if rows else None


def _ack_from_current_receipt_row(
    row, envelope_hash: str, payload_hash: str
) -> Dict[str, Any]:
    mismatches = _receipt_conflict_fields(row, envelope_hash, payload_hash)
    if mismatches:
        raise NKTIdempotencyConflict(
            "Concurrent Customer Order materialization receipt conflicts with immutable content: "
            + ", ".join(mismatches)
        )

    order_rows = frappe.db.sql(
        """
        SELECT name, docstatus, custom_nkt_fast_request_id
        FROM `tabNKT Customer Order`
        WHERE name=%s
        FOR UPDATE
        """,
        (row.canonical_name,),
        as_dict=True,
    )
    if not order_rows:
        raise NKTIdempotencyConflict(
            "Concurrent Customer Order materialization receipt has no canonical order."
        )
    order = order_rows[0]
    if str(order.custom_nkt_fast_request_id or "") != str(row.name):
        raise NKTIdempotencyConflict(
            "Concurrent materialized Customer Order is not bound to the immutable Event UUID."
        )
    return {
        "event_uuid": str(row.name),
        "event_family": ORDER_INTENT_FAMILY,
        "primary_ack_uuid": row.primary_ack_uuid,
        "payload_sha256": row.payload_sha256,
        "result_code": row.result_code,
        "committed": True,
        "replay": True,
        "canonical_doctype": "NKT Customer Order",
        "canonical_name": row.canonical_name,
        "canonical_docstatus": int(order.docstatus or 0),
        "materialization_state": row.materialization_state,
        "normal_submit_lifecycle_executed": False,
    }


def _materialization_claim_lock_name(event_uuid: str) -> str:
    # MariaDB user-level lock names are connection scoped. Keep the name deterministic
    # and comfortably short while preserving one lock namespace per immutable event.
    return f"nkt-c15c9i-co-{event_uuid}"


def _release_materialization_claim_lock(lock_name: str) -> None:
    try:
        frappe.db.sql("SELECT RELEASE_LOCK(%s)", (lock_name,))
    except Exception:
        # Never turn a successfully committed business transaction into an apparent
        # failure solely because lock cleanup raised. MariaDB releases user locks when
        # the connection ends; the torture gate separately proves normal callback
        # release by requiring IS_FREE_LOCK() after both workers finish.
        pass


def _acquire_materialization_claim_lock(event_uuid: str) -> None:
    lock_name = _materialization_claim_lock_name(event_uuid)
    rows = frappe.db.sql(
        "SELECT GET_LOCK(%s, %s)",
        (lock_name, 30),
        as_list=True,
    )
    acquired = rows and int(rows[0][0] or 0) == 1
    if not acquired:
        raise frappe.ValidationError(
            "Customer Order sync is busy for this immutable Event UUID. "
            "Safe retry is required; no duplicate order was created."
        )

    state = {"released": False}

    def release_once():
        if state["released"]:
            return
        _release_materialization_claim_lock(lock_name)
        state["released"] = True

    # Frappe owns the transaction boundary. Hold the same-Event lock through that exact
    # boundary so the waiting worker cannot enter before the winner is committed.
    frappe.db.after_commit.add(release_once)
    frappe.db.after_rollback.add(release_once)


def receive_customer_order_intent_at_primary(packet: Dict[str, Any]) -> Dict[str, Any]:
    _require_primary()
    envelope, payload, envelope_hash, payload_hash = validate_transport_packet(
        packet,
        expected_family=ORDER_INTENT_FAMILY,
    )
    event_uuid = envelope["event_uuid"]

    # R6 correction: serialize the SAME immutable Event UUID immediately after
    # transport validation and BEFORE the first receipt-table read.
    # R5's package guard correctly rejected its own payload because the intended
    # ordering had been described but not actually applied to the source payload.
    _acquire_materialization_claim_lock(event_uuid)

    # This is deliberately the FIRST Primary Receipt table access in this function.
    # A worker that waited for another worker's commit performs a locking current read
    # and must converge on that winner's immutable receipt/Draft/ACK.
    current_receipt = _current_receipt_row_for_update(event_uuid)
    if current_receipt:
        return _ack_from_current_receipt_row(
            current_receipt,
            envelope_hash,
            payload_hash,
        )

    # No committed receipt exists for this Event UUID. Only now validate new-event
    # reconnect master data and price truth before creating the technical receipt.
    # Exact replays of already-committed events are resolved above from the receipt
    # and are not retroactively invalidated by later master-data changes.
    _validate_master_links(payload)
    _validate_reconnect_prices(payload)

    # No committed winner exists. This transaction owns the same-Event claim until its
    # own commit/rollback boundary, so inserting the immutable receipt and canonical
    # Draft cannot race another worker for this Event UUID.
    claim = frappe.get_doc(
        {
            "doctype": "NKT Sync Primary Receipt",
            "event_uuid": event_uuid,
            "event_family": ORDER_INTENT_FAMILY,
            "primary_ack_uuid": str(uuid.uuid4()),
            "envelope_sha256": envelope_hash,
            "payload_sha256": payload_hash,
            "canonical_doctype": "",
            "canonical_name": "",
            "materialization_state": "Technical Receipt Only",
            "primary_received_at": now(),
            "primary_committed_at": None,
            "result_code": "Committed",
        }
    )
    claim.insert(ignore_permissions=True)

    order, order_replay = _create_controlled_draft(event_uuid, envelope, payload)

    frappe.db.set_value(
        "NKT Sync Primary Receipt",
        event_uuid,
        {
            "canonical_doctype": "NKT Customer Order",
            "canonical_name": order.name,
            "materialization_state": "Canonical Draft Materialized",
            "primary_committed_at": now(),
        },
        update_modified=False,
    )
    claim = frappe.get_doc("NKT Sync Primary Receipt", event_uuid)
    return _ack_from_materialized_receipt(claim, replay=bool(order_replay))


def apply_customer_order_materialization_ack_at_edge(ack: Dict[str, Any]) -> Dict[str, Any]:
    if _runtime_role() != "Store Edge":
        raise frappe.PermissionError("Customer Order materialization ACK unavailable.")
    if not isinstance(ack, dict):
        raise frappe.ValidationError("Customer Order materialization ACK is invalid.")

    event_uuid = _uuid(ack.get("event_uuid"), "Event UUID")
    ack_uuid = _uuid(ack.get("primary_ack_uuid"), "Primary ACK UUID")
    payload_hash = str(ack.get("payload_sha256") or "").lower()
    canonical_name = str(ack.get("canonical_name") or "").strip()

    if ack.get("committed") is not True or ack.get("result_code") != "Committed":
        raise frappe.ValidationError("Customer Order materialization ACK is not committed.")
    if ack.get("event_family") != ORDER_INTENT_FAMILY:
        raise frappe.ValidationError("Customer Order materialization ACK family is invalid.")
    if ack.get("canonical_doctype") != "NKT Customer Order" or not canonical_name:
        raise frappe.ValidationError("Customer Order materialization ACK canonical identity is invalid.")
    if ack.get("materialization_state") != "Canonical Draft Materialized":
        raise frappe.ValidationError("Customer Order materialization ACK state is invalid.")
    if int(ack.get("canonical_docstatus") or 0) != 0:
        raise frappe.ValidationError("Offline Customer Order materialization ACK must reference a Draft.")

    if not frappe.db.exists("NKT Sync Event", event_uuid):
        raise frappe.DoesNotExistError("Customer Order Intent event is unavailable.")

    event = frappe.get_doc("NKT Sync Event", event_uuid)
    if event.event_family != ORDER_INTENT_FAMILY:
        raise frappe.ValidationError("Customer Order Intent event family mismatch.")
    if event.payload_sha256 != payload_hash:
        raise NKTIdempotencyConflict(
            "Customer Order materialization ACK payload hash conflicts with immutable intent."
        )

    bound_ack = str(event.primary_ack_uuid or "").strip()
    if bound_ack and bound_ack != ack_uuid:
        raise NKTIdempotencyConflict(
            "Customer Order materialization ACK UUID conflicts with the ACK already bound to this event."
        )
    if event.sync_state == "Committed at Primary":
        if (
            bound_ack == ack_uuid
            and event.canonical_doctype == "NKT Customer Order"
            and event.canonical_name == canonical_name
        ):
            return {
                "event_uuid": event_uuid,
                "primary_ack_uuid": ack_uuid,
                "canonical_name": canonical_name,
                "sync_state": "Committed at Primary",
                "pending_payload_purged": False,
                "reservation_projection_purged": False,
                "replay": True,
            }
        raise NKTIdempotencyConflict(
            "Committed Customer Order Intent conflicts with the supplied materialization ACK."
        )

    pending_name = frappe.db.exists("NKT Sync Pending Payload", event_uuid)
    if not pending_name:
        raise frappe.ValidationError(
            "Customer Order materialization ACK arrived without its pending payload."
        )
    pending = frappe.get_doc("NKT Sync Pending Payload", pending_name)
    if pending.event_family != ORDER_INTENT_FAMILY or pending.payload_sha256 != payload_hash:
        raise NKTIdempotencyConflict(
            "Customer Order materialization ACK conflicts with pending intent."
        )

    projection_names = frappe.get_all(
        "NKT Edge Order Reservation Projection",
        filters={"event_uuid": event_uuid},
        pluck="name",
        limit_page_length=500,
    )
    if not projection_names:
        raise frappe.ValidationError(
            "Customer Order materialization ACK cannot purge without a matching reservation projection."
        )

    mark_primary_committed(
        event_uuid,
        "NKT Customer Order",
        canonical_name,
        primary_ack_uuid=ack_uuid,
    )
    frappe.delete_doc(
        "NKT Sync Pending Payload",
        pending.name,
        ignore_permissions=True,
        force=True,
    )
    for name in projection_names:
        frappe.delete_doc(
            "NKT Edge Order Reservation Projection",
            name,
            ignore_permissions=True,
            force=True,
        )

    return {
        "event_uuid": event_uuid,
        "primary_ack_uuid": ack_uuid,
        "canonical_name": canonical_name,
        "sync_state": "Committed at Primary",
        "pending_payload_purged": True,
        "reservation_projection_purged": True,
        "replay": False,
    }
