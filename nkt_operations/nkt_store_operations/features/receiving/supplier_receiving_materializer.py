from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any, Dict

import frappe
from frappe.utils import flt, get_datetime, getdate, now

from nkt_operations.nkt_store_operations.features.offline_edge.safe_sync import NKTIdempotencyConflict
from nkt_operations.nkt_store_operations.features.receiving.supplier_receiving_physical_intent import (
    TOLERANCE,
    canonical_supplier_receiving_payload_json,
    normalize_supplier_receiving_payload,
)
from nkt_operations.nkt_store_operations.features.receiving.internal.primary_supplier_receiving_intent import (
    PRIMARY_JOURNAL,
)
from nkt_operations.nkt_store_operations.features.offline_edge.sync_transport import _runtime_role
from nkt_operations.nkt_store_operations.doctype.nkt_supplier_receiving.nkt_supplier_receiving import (
    C15C_SUPPLIER_RECEIVING_OFFLINE_MATERIALIZATION_CONTEXT_FLAG,
)

FOUNDATION_VERSION = "C15C.10H-R6"
MATERIALIZATION_ACK_NAMESPACE = uuid.UUID("811b19dd-f5c1-49be-814c-f0de07168bbc")


def _require_primary():
    if _runtime_role() != "Primary":
        raise frappe.PermissionError(
            "Supplier Receiving canonical materialization is available only at Primary."
        )


def _uuid(value: Any, label: str) -> str:
    try:
        return str(uuid.UUID(str(value)))
    except Exception as exc:
        raise frappe.ValidationError(f"{label} is invalid.") from exc


def _claim_name(kind: str, identity: str) -> str:
    return "nkt-10h-supplier-mat-" + hashlib.sha256(
        f"{kind}:{identity}".encode("utf-8")
    ).hexdigest()[:32]


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
                    "Supplier Receiving materialization is busy. Safe retry is required."
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


def _lock_purchase_order_for_update(purchase_order: str):
    rows = frappe.db.sql(
        "SELECT name FROM `tabPurchase Order` WHERE name=%s FOR UPDATE",
        (purchase_order,),
        as_dict=True,
    )
    if not rows:
        raise frappe.DoesNotExistError(
            "Purchase Order is unavailable for Supplier Receiving materialization."
        )


def _payload_from_journal(journal) -> Dict[str, Any]:
    try:
        raw = json.loads(str(journal.canonical_payload_json or ""))
    except Exception as exc:
        raise NKTIdempotencyConflict(
            "Preserved Supplier Receiving payload JSON is invalid."
        ) from exc
    payload = normalize_supplier_receiving_payload(raw)
    canonical = canonical_supplier_receiving_payload_json(payload)
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    if digest != str(journal.payload_sha256 or "").lower():
        raise NKTIdempotencyConflict(
            "Preserved Supplier Receiving payload hash no longer matches canonical content."
        )
    if canonical != str(journal.canonical_payload_json or ""):
        raise NKTIdempotencyConflict(
            "Preserved Supplier Receiving canonical payload has drifted."
        )
    return payload


def _validate_materialization_order(journal, payload):
    po = frappe.get_doc("Purchase Order", journal.purchase_order)
    if int(po.docstatus or 0) != 1 or str(po.status or "") in ("Closed", "Cancelled", "Completed"):
        raise frappe.ValidationError(
            "Purchase Order is no longer eligible for preserved Supplier Receiving materialization."
        )
    by_name = {row.name: row for row in (po.items or [])}
    for line in payload["items"]:
        row = by_name.get(line["purchase_order_item"])
        if not row or str(row.item_code or "") != line["item_code"]:
            raise NKTIdempotencyConflict(
                "Supplier Receiving PO Item lineage no longer matches canonical Purchase Order."
            )
        remaining = max(flt(row.qty) - flt(row.received_qty), 0.0)
        if abs(remaining - flt(line["expected_qty"])) > TOLERANCE:
            if remaining > flt(line["expected_qty"]) + TOLERANCE:
                raise frappe.ValidationError(
                    "A later preserved Supplier Receiving is waiting for an earlier physical receipt to materialize first. Safe retry is required."
                )
            raise NKTIdempotencyConflict(
                "Canonical Purchase Order received quantity advanced beyond this preserved Supplier Receiving snapshot."
            )
        if flt(line["delivered_qty"]) > remaining + TOLERANCE:
            raise frappe.ValidationError(
                f"Preserved Supplier Receiving exceeds canonical remaining quantity for {line['item_code']}."
            )
    return po


def _receiving_time(journal) -> str:
    value = get_datetime(journal.settled_at)
    return value.strftime("%H:%M:%S")


def _assert_materialized_doc_matches(doc, payload):
    if str(doc.purchase_order or "") != payload["purchase_order"]:
        raise NKTIdempotencyConflict("Materialized Supplier Arrival lost Purchase Order lineage.")
    if str(doc.company or "") != payload["company"] or str(doc.supplier or "") != payload["supplier"]:
        raise NKTIdempotencyConflict("Materialized Supplier Arrival lost Supplier/Company lineage.")
    if str(getdate(doc.receiving_date)) != str(getdate(payload["receiving_date"])):
        raise NKTIdempotencyConflict("Materialized Supplier Arrival lost physical receiving date.")
    if str(doc.receiving_warehouse or "") != payload["receiving_warehouse"]:
        raise NKTIdempotencyConflict("Materialized Supplier Arrival lost receiving Warehouse lineage.")

    rows = {str(row.purchase_order_item): row for row in (doc.items or [])}
    expected = {line["purchase_order_item"]: line for line in payload["items"]}
    if set(rows) != set(expected):
        raise NKTIdempotencyConflict("Materialized Supplier Arrival Item set conflicts with immutable intent.")
    for po_item, line in expected.items():
        row = rows[po_item]
        if str(row.item_code or "") != line["item_code"] or str(row.uom or "") != line["uom"]:
            raise NKTIdempotencyConflict("Materialized Supplier Arrival Item identity conflicts with immutable intent.")
        for field in (
            "expected_qty","delivered_qty","accepted_qty","damaged_qty",
            "other_rejected_qty","rejected_qty","shortage_qty","overdelivery_qty",
        ):
            if abs(flt(row.get(field)) - flt(line[field])) > TOLERANCE:
                raise NKTIdempotencyConflict(
                    f"Materialized Supplier Arrival {field} conflicts for {line['item_code']}."
                )
        if str(row.rejected_warehouse or "") != str(line.get("rejected_warehouse") or ""):
            raise NKTIdempotencyConflict("Materialized Supplier Arrival rejected Warehouse conflicts with immutable intent.")
        if str(row.condition_classification or "") != str(line.get("condition_classification") or ""):
            raise NKTIdempotencyConflict("Materialized Supplier Arrival condition classification conflicts with immutable intent.")


def _verify_purchase_receipt(pr, journal, payload):
    if int(pr.docstatus or 0) != 1:
        raise NKTIdempotencyConflict("Materialized Purchase Receipt is not submitted.")
    if str(pr.supplier or "") != str(journal.supplier or "") or str(pr.company or "") != str(journal.company or ""):
        raise NKTIdempotencyConflict("Materialized Purchase Receipt Supplier/Company lineage is invalid.")
    if str(getdate(pr.posting_date)) != str(getdate(journal.receiving_date)):
        raise NKTIdempotencyConflict("Purchase Receipt did not preserve physical receiving date.")
    if pr.meta.has_field("posting_time"):
        actual = str(pr.posting_time or "").split(".")[0]
        expected = _receiving_time(journal).split(".")[0]
        if actual != expected:
            raise NKTIdempotencyConflict("Purchase Receipt did not preserve physical receiving time.")

    expected = {
        line["purchase_order_item"]: line
        for line in payload["items"]
        if flt(line["delivered_qty"]) > TOLERANCE
    }
    actual = {str(row.purchase_order_item or ""): row for row in (pr.items or [])}
    if set(actual) != set(expected):
        raise NKTIdempotencyConflict(
            "Materialized Purchase Receipt Item set conflicts with immutable Supplier Receiving."
        )
    for po_item, line in expected.items():
        row = actual[po_item]
        if (
            str(row.item_code or "") != line["item_code"]
            or str(row.warehouse or "") != payload["receiving_warehouse"]
            or str(row.rejected_warehouse or "") != str(line.get("rejected_warehouse") or "")
            or abs(flt(row.qty) - flt(line["accepted_qty"])) > TOLERANCE
            or abs(flt(row.rejected_qty) - flt(line["rejected_qty"])) > TOLERANCE
        ):
            raise NKTIdempotencyConflict(
                f"Materialized Purchase Receipt row conflicts for {line['item_code']}."
            )
    return pr


def _expected_exception(payload) -> bool:
    return any(
        flt(line["damaged_qty"]) > TOLERANCE
        or flt(line["other_rejected_qty"]) > TOLERANCE
        or flt(line["shortage_qty"]) > TOLERANCE
        for line in payload["items"]
    )


def _verify_exception(receiving_name: str, pr_name: str, payload):
    name = frappe.db.exists(
        "NKT Supplier Delivery Exception",
        {"supplier_receiving": receiving_name},
    )
    if _expected_exception(payload):
        if not name:
            raise NKTIdempotencyConflict(
                "Materialized Supplier Receiving with physical exception lost its restricted exception record."
            )
        doc = frappe.get_doc("NKT Supplier Delivery Exception", name)
        if str(doc.receiving_posting_reference or "") != str(pr_name or ""):
            raise NKTIdempotencyConflict(
                "Supplier Delivery Exception lost Purchase Receipt lineage."
            )
        return doc
    if name:
        raise NKTIdempotencyConflict(
            "Clean Supplier Receiving unexpectedly created a Supplier Delivery Exception."
        )
    return None


def _ack_uuid(event_uuid: str, payload_hash: str, receiving_name: str, pr_name: str) -> str:
    event_uuid = _uuid(event_uuid, "Supplier Receiving Intent UUID")
    payload_hash = str(payload_hash or "").lower()
    material = (
        "NKT Supplier Receiving Canonical Materialization"
        + "\0" + event_uuid
        + "\0" + payload_hash
        + "\0" + str(receiving_name or "")
        + "\0" + str(pr_name or "")
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


def _stock_effects(payload):
    effects = []
    for line in payload["items"]:
        actual = flt(
            frappe.db.get_value(
                "Bin",
                {
                    "item_code": line["item_code"],
                    "warehouse": payload["receiving_warehouse"],
                },
                "actual_qty",
            )
        )
        effects.append(
            {
                "line_no": line["line_no"],
                "purchase_order_item": line["purchase_order_item"],
                "item_code": line["item_code"],
                "warehouse": payload["receiving_warehouse"],
                "accepted_qty": flt(line["accepted_qty"]),
                "rejected_qty": flt(line["rejected_qty"]),
                "rejected_warehouse": line.get("rejected_warehouse"),
                "primary_post_actual_qty": float(f"{actual:.6f}"),
            }
        )
    return effects


def _build_ack(journal, payload, receiving, pr, exception):
    value = {
        "event_uuid": journal.name,
        "payload_sha256": str(journal.payload_sha256 or "").lower(),
        "materialization_ack_uuid": _ack_uuid(
            journal.name, journal.payload_sha256, receiving.name, pr.name
        ),
        "purchase_order": journal.purchase_order,
        "supplier": journal.supplier,
        "supplier_receiving": receiving.name,
        "purchase_receipt": pr.name,
        "supplier_delivery_exception": exception.name if exception else None,
        "physical_receiving_date": str(journal.receiving_date),
        "physical_receiving_time": str(journal.settled_at),
        "stock_effects": _stock_effects(payload),
    }
    canonical = _canonical_ack_json(value)
    return {
        **value,
        "materialization_ack_sha256": hashlib.sha256(
            canonical.encode("utf-8")
        ).hexdigest(),
    }


def _stored_ack(journal, payload, receiving, pr, exception):
    raw = str(journal.materialization_ack_json or "")
    digest = str(journal.materialization_ack_sha256 or "").lower()
    if not raw or not digest:
        raise NKTIdempotencyConflict(
            "Primary Supplier Receiving journal is missing durable materialization ACK evidence."
        )
    if hashlib.sha256(raw.encode("utf-8")).hexdigest() != digest:
        raise NKTIdempotencyConflict("Supplier Receiving materialization ACK hash is invalid.")
    try:
        ack = json.loads(raw)
    except Exception as exc:
        raise NKTIdempotencyConflict("Supplier Receiving materialization ACK JSON is invalid.") from exc
    if _canonical_ack_json(ack) != raw:
        raise NKTIdempotencyConflict("Supplier Receiving materialization ACK is not canonical.")

    checks = {
        "event_uuid": journal.name,
        "payload_sha256": str(journal.payload_sha256 or "").lower(),
        "materialization_ack_uuid": _ack_uuid(
            journal.name, journal.payload_sha256, receiving.name, pr.name
        ),
        "purchase_order": journal.purchase_order,
        "supplier": journal.supplier,
        "supplier_receiving": receiving.name,
        "purchase_receipt": pr.name,
        "supplier_delivery_exception": exception.name if exception else None,
    }
    for field, expected in checks.items():
        if str(ack.get(field) or "") != str(expected or ""):
            raise NKTIdempotencyConflict(
                f"Supplier Receiving materialization ACK {field} binding is invalid."
            )
    effects = ack.get("stock_effects")
    if not isinstance(effects, list) or len(effects) != len(payload["items"]):
        raise NKTIdempotencyConflict("Supplier Receiving materialization ACK stock effects are incomplete.")
    return {**ack, "materialization_ack_sha256": digest}


def _verify_materialized(journal, payload):
    receiving_name = str(journal.materialized_supplier_receiving or "").strip()
    pr_name = str(journal.materialized_purchase_receipt or "").strip()
    if not receiving_name or not frappe.db.exists("NKT Supplier Receiving", receiving_name):
        raise NKTIdempotencyConflict("Primary Supplier Receiving journal lost canonical Supplier Arrival binding.")
    if not pr_name or not frappe.db.exists("Purchase Receipt", pr_name):
        raise NKTIdempotencyConflict("Primary Supplier Receiving journal lost Purchase Receipt binding.")

    receiving = frappe.get_doc("NKT Supplier Receiving", receiving_name)
    if int(receiving.docstatus or 0) != 1 or str(receiving.posting_status or "") != "Posted":
        raise NKTIdempotencyConflict("Materialized Supplier Arrival is not submitted/posted.")
    _assert_materialized_doc_matches(receiving, payload)
    if str(receiving.underlying_purchase_receipt or "") != pr_name:
        raise NKTIdempotencyConflict("Supplier Arrival lost underlying Purchase Receipt binding.")

    pr = _verify_purchase_receipt(
        frappe.get_doc("Purchase Receipt", pr_name),
        journal,
        payload,
    )
    exception = _verify_exception(receiving.name, pr.name, payload)

    if str(journal.downstream_state or "") != "Supplier Receiving Materialized":
        raise NKTIdempotencyConflict("Primary Supplier Receiving journal is not materialized.")
    expected_ack = _ack_uuid(journal.name, journal.payload_sha256, receiving.name, pr.name)
    if str(journal.materialization_ack_uuid or "") != expected_ack:
        raise NKTIdempotencyConflict("Supplier Receiving materialization ACK UUID binding is invalid.")
    ack = _stored_ack(journal, payload, receiving, pr, exception)
    return {
        **ack,
        "downstream_state": journal.downstream_state,
        "canonical_supplier_receiving_created": True,
        "purchase_receipt_created": True,
        "supplier_delivery_exception_created": bool(exception),
        "edge_projection_may_finalize_only_after_local_stock_rebase": True,
    }


def materialize_supplier_receiving(event_uuid: str) -> Dict[str, Any]:
    _require_primary()
    event_uuid = _uuid(event_uuid, "Supplier Receiving Intent UUID")

    if not frappe.db.exists(PRIMARY_JOURNAL, event_uuid):
        raise frappe.DoesNotExistError(
            "Preserved Supplier Receiving Intent is unavailable at Primary."
        )
    purchase_order = frappe.db.get_value(PRIMARY_JOURNAL, event_uuid, "purchase_order")
    locks = _acquire_claims(event_uuid, purchase_order)
    try:
        journal = _journal_for_update(event_uuid)
        if not journal:
            raise frappe.DoesNotExistError(
                "Preserved Supplier Receiving Intent is unavailable at Primary."
            )
        payload = _payload_from_journal(journal)
        _lock_purchase_order_for_update(journal.purchase_order)

        if str(journal.preservation_state or "") != "Preserved":
            raise NKTIdempotencyConflict("Supplier Receiving Intent is not preserved.")
        if str(journal.downstream_state or "") == "Supplier Receiving Materialized":
            result = _verify_materialized(journal, payload)
            result["replay"] = True
            return result
        if str(journal.downstream_state or "") != "Awaiting Supplier Receiving Materialization":
            raise NKTIdempotencyConflict(
                "Supplier Receiving Intent is not eligible for materialization."
            )

        _validate_materialization_order(journal, payload)

        original_user = frappe.session.user
        origin_user = str(journal.origin_user or "").strip()
        if not origin_user or not frappe.db.exists("User", origin_user):
            raise NKTIdempotencyConflict(
                "Preserved Supplier Receiving origin User is unavailable at Primary."
            )

        previous_context = frappe.flags.get(
            C15C_SUPPLIER_RECEIVING_OFFLINE_MATERIALIZATION_CONTEXT_FLAG
        )
        frappe.set_user(origin_user)
        frappe.flags[C15C_SUPPLIER_RECEIVING_OFFLINE_MATERIALIZATION_CONTEXT_FLAG] = {
            "event_uuid": event_uuid,
            "origin_user": origin_user,
            "purchase_order": payload["purchase_order"],
            "receiving_date": payload["receiving_date"],
        }
        try:
            receiving = frappe.get_doc(
                {
                    "doctype": "NKT Supplier Receiving",
                    "movement_type": "External Supplier Arrival",
                    "purchase_order": payload["purchase_order"],
                    "company": payload["company"],
                    "supplier": payload["supplier"],
                    "receiving_date": payload["receiving_date"],
                    "receiving_time": _receiving_time(journal),
                    "receiving_warehouse": payload["receiving_warehouse"],
                    "bill_of_lading_no": payload.get("bill_of_lading_no"),
                    "supplier_dr_no": payload.get("supplier_dr_no"),
                    "supplier_delivery_reference": payload.get("supplier_delivery_reference"),
                    "delivery_vehicle": payload.get("delivery_vehicle"),
                    "internal_vehicle_no": payload.get("internal_vehicle_no"),
                    "plate_number": payload.get("plate_number"),
                    "driver_name": payload.get("driver_name"),
                    "receiving_notes": payload.get("receiving_notes"),
                    "physical_quantities_confirmed": 1,
                    "items": [
                        {
                            "item_code": line["item_code"],
                            "item_name": line.get("item_name"),
                            "purchase_order_item": line["purchase_order_item"],
                            "uom": line["uom"],
                            "expected_qty": line["expected_qty"],
                            "delivered_qty": line["delivered_qty"],
                            "accepted_qty": line["accepted_qty"],
                            "damaged_qty": line["damaged_qty"],
                            "other_rejected_qty": line["other_rejected_qty"],
                            "rejected_qty": line["rejected_qty"],
                            "shortage_qty": line["shortage_qty"],
                            "overdelivery_qty": 0,
                            "rejected_warehouse": line.get("rejected_warehouse"),
                            "condition_classification": line.get("condition_classification"),
                            "condition_reason": line.get("condition_reason"),
                        }
                        for line in payload["items"]
                    ],
                }
            )
            receiving.flags.ignore_permissions = True
            receiving.insert(ignore_permissions=True)
            _assert_materialized_doc_matches(receiving, payload)
            receiving.submit()
        finally:
            if previous_context is None:
                frappe.flags.pop(
                    C15C_SUPPLIER_RECEIVING_OFFLINE_MATERIALIZATION_CONTEXT_FLAG,
                    None,
                )
            else:
                frappe.flags[
                    C15C_SUPPLIER_RECEIVING_OFFLINE_MATERIALIZATION_CONTEXT_FLAG
                ] = previous_context
            frappe.set_user(original_user)

        if not receiving.name or int(receiving.docstatus or 0) != 1:
            raise NKTIdempotencyConflict(
                "Canonical NKT Supplier Receiving did not submit."
            )
        pr_name = str(receiving.underlying_purchase_receipt or "").strip()
        if not pr_name or not frappe.db.exists("Purchase Receipt", pr_name):
            raise NKTIdempotencyConflict(
                "Canonical Supplier Receiving bridge did not create a Purchase Receipt."
            )
        pr = _verify_purchase_receipt(
            frappe.get_doc("Purchase Receipt", pr_name),
            journal,
            payload,
        )
        exception = _verify_exception(receiving.name, pr.name, payload)

        ack = _build_ack(journal, payload, receiving, pr, exception)
        ack_json = _canonical_ack_json(
            {k: v for k, v in ack.items() if k != "materialization_ack_sha256"}
        )

        frappe.db.set_value(
            PRIMARY_JOURNAL,
            journal.name,
            {
                "downstream_state": "Supplier Receiving Materialized",
                "materialized_supplier_receiving": receiving.name,
                "materialized_purchase_receipt": pr.name,
                "materialized_supplier_exception": exception.name if exception else None,
                "materialized_at": now(),
                "materialization_ack_uuid": ack["materialization_ack_uuid"],
                "materialization_ack_sha256": ack["materialization_ack_sha256"],
                "materialization_ack_json": ack_json,
            },
            update_modified=False,
        )
        journal.reload()
        verified = _verify_materialized(journal, payload)
        verified["replay"] = False
        return verified
    finally:
        _release_claims(locks)


def installation_probe():
    meta = frappe.get_meta(PRIMARY_JOURNAL)
    required = {
        "materialized_supplier_receiving",
        "materialized_purchase_receipt",
        "materialized_supplier_exception",
        "materialized_at",
        "materialization_ack_uuid",
        "materialization_ack_sha256",
        "materialization_ack_json",
    }
    return {
        "foundation_version": FOUNDATION_VERSION,
        "materializer": "canonical NKT Supplier Receiving -> private Purchase Receipt bridge",
        "new_stock_engine": False,
        "journal_materialization_fields_ready": required.issubset(
            {f.fieldname for f in meta.fields}
        ),
        "true_physical_receiving_time_preserved": True,
        "normal_employee_backdating_enabled": False,
        "server_only_cross_midnight_replay_context": True,
        "supplier_delivery_exception_uses_existing_restricted_bridge": True,
        "supplier_money_side_materialized_from_edge": False,
        "exactly_once_claims_present": True,
        "po_materialization_order_guard_present": True,
        "destructive_materialization_qa_run": False,
    }


@frappe.whitelist()
def materialize(event_uuid: str):
    return materialize_supplier_receiving(event_uuid)
