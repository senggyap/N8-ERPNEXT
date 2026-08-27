from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime
from typing import Any, Dict

import frappe
from frappe.utils import flt, get_datetime, now

from nkt_operations.nkt_store_operations.features.offline_edge.safe_sync import (
    NKTIdempotencyConflict,
)
from nkt_operations.nkt_store_operations.features.offline_edge.sync_transport import _runtime_role
from nkt_operations.nkt_store_operations.features.inventory.internal.warehouse_release_intent import (
    _canonical_warehouse_release_intent_json,
    _normalize_warehouse_release_intent_payload,
)
from nkt_operations.nkt_store_operations.features.inventory.internal.primary_warehouse_release_intent import (
    PRIMARY_JOURNAL,
)
from nkt_operations.nkt_store_operations.features.oil.controls import (
    is_configured_finished_oil_item as _nkt_c15d_is_finished_oil_item,
)

FOUNDATION_VERSION = "C15C.10E-R4"
TOLERANCE = 0.000001
MATERIALIZATION_ACK_NAMESPACE = uuid.UUID("d22d5339-acde-4a22-a95b-6cc137f40f5a")


def _require_primary():
    if _runtime_role() != "Primary":
        raise frappe.PermissionError("Physical stock materialization is available only at Primary.")


def _uuid(value: Any, label: str) -> str:
    try:
        return str(uuid.UUID(str(value)))
    except Exception as exc:
        raise frappe.ValidationError(f"{label} is invalid.") from exc


def _materialization_ack_uuid(event_uuid: str, payload_hash: str, stock_entry: str) -> str:
    event_uuid = _uuid(event_uuid, "Warehouse Release Intent UUID")
    payload_hash = str(payload_hash or "").lower()
    if len(payload_hash) != 64 or any(c not in "0123456789abcdef" for c in payload_hash):
        raise frappe.ValidationError("Warehouse Release Intent payload hash is invalid.")
    stock_entry = str(stock_entry or "").strip()
    if not stock_entry:
        raise frappe.ValidationError("Materialized Stock Entry is required.")
    material = (
        "NKT Warehouse Release Physical Stock Materialization"
        + "\0" + event_uuid
        + "\0" + payload_hash
        + "\0" + stock_entry
    )
    return str(uuid.uuid5(MATERIALIZATION_ACK_NAMESPACE, material))


def _materialization_claim_name(event_uuid: str) -> str:
    return "nkt-10e-stock-" + hashlib.sha256(
        f"event:{event_uuid}".encode("utf-8")
    ).hexdigest()[:36]


def _acquire_materialization_claim(event_uuid: str) -> None:
    """
    Serialize physical-stock materialization BEFORE the first read of the
    mutable Primary journal row.

    R5 proved that reading warehouse_release from the journal first and only
    then waiting on a named claim can leave a competing REPEATABLE READ
    transaction with a stale row version. MariaDB then raises error 1020 when
    SELECT ... FOR UPDATE sees the winner's committed journal update.

    The event UUID already has a one-to-one Warehouse Release binding in the
    preserved Primary journal, so an event-level claim is the correct outer
    serialization boundary. The canonical Warehouse Release row is locked
    separately after the journal is obtained.
    """
    name = _materialization_claim_name(event_uuid)
    rows = frappe.db.sql("SELECT GET_LOCK(%s,%s)", (name, 30), as_list=True)
    if not rows or int(rows[0][0] or 0) != 1:
        raise frappe.ValidationError(
            "Physical-stock materialization is busy. Safe retry is required."
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


def _lock_release_for_update(release_name: str) -> None:
    rows = frappe.db.sql(
        "SELECT name FROM `tabNKT Warehouse Release` WHERE name=%s FOR UPDATE",
        (release_name,),
        as_dict=True,
    )
    if not rows:
        raise frappe.DoesNotExistError(
            "Warehouse Release is unavailable for physical-stock materialization."
        )


def _journal_for_update(event_uuid: str):
    rows = frappe.db.sql(
        f"SELECT name FROM `tab{PRIMARY_JOURNAL}` WHERE name=%s FOR UPDATE",
        (event_uuid,),
        as_dict=True,
    )
    return frappe.get_doc(PRIMARY_JOURNAL, event_uuid) if rows else None


def _payload_from_journal(journal) -> Dict[str, Any]:
    try:
        raw = json.loads(str(journal.canonical_payload_json or ""))
    except Exception as exc:
        raise NKTIdempotencyConflict(
            "Preserved Warehouse Release Intent payload JSON is invalid."
        ) from exc
    payload = _normalize_warehouse_release_intent_payload(raw)
    canonical = _canonical_warehouse_release_intent_json(payload)
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    if digest != str(journal.payload_sha256 or "").lower():
        raise NKTIdempotencyConflict(
            "Preserved Warehouse Release Intent payload hash no longer matches canonical content."
        )
    if canonical != str(journal.canonical_payload_json or ""):
        raise NKTIdempotencyConflict(
            "Preserved Warehouse Release Intent canonical payload has drifted."
        )
    return payload


def _assert_release_identity(release, journal, payload):
    checks = {
        "customer_order": release.customer_order,
        "company": release.company,
        "customer": release.customer,
        "source_warehouse": release.get("custom_nkt_source_warehouse"),
    }
    for field, actual in checks.items():
        expected = payload[field]
        if str(actual or "") != str(expected or ""):
            raise NKTIdempotencyConflict(
                f"Warehouse Release {field} conflicts with preserved physical-release intent."
            )
    if str(release.name or "") != str(journal.warehouse_release or ""):
        raise NKTIdempotencyConflict("Primary journal is bound to another Warehouse Release.")


def _project_payload_onto_draft(release, journal, payload):
    if int(release.docstatus or 0) != 0:
        raise NKTIdempotencyConflict(
            "Warehouse Release is no longer Draft before physical-stock materialization."
        )
    if str(release.get("release_status") or "Draft") != "Draft":
        raise NKTIdempotencyConflict(
            "Warehouse Release is no longer in Draft release state."
        )

    _assert_release_identity(release, journal, payload)

    payload_rows = {
        str(line["warehouse_release_item"]): line
        for line in payload["items"]
    }
    release_rows = {row.name: row for row in (release.get("items") or [])}
    if set(payload_rows) - set(release_rows):
        raise NKTIdempotencyConflict(
            "Preserved physical release references a row missing from the current draft."
        )

    for row in release.get("items") or []:
        line = payload_rows.get(row.name)
        if not line:
            row.release_quantity = 0
            continue
        checks = {
            "customer_order_item": row.customer_order_item,
            "item_code": row.item,
            "uom": row.uom,
            "source_warehouse": row.source_warehouse,
        }
        for field, actual in checks.items():
            if str(actual or "") != str(line[field] or ""):
                raise NKTIdempotencyConflict(
                    f"Warehouse Release row {field} conflicts with preserved physical release."
                )
        row.release_quantity = flt(line["release_quantity"])

    release.custom_nkt_mother_release_reference = payload["release_reference"]
    release.custom_nkt_driver_name = payload["driver_name"]
    release.custom_nkt_plate_number = payload["plate_number"]
    # C15C.10E R8: preserve the Fast Screen request/Event UUID on the
    # canonical Warehouse Release after Primary materialization. This gives
    # one durable identity from the operator's F10/F12 action through Edge,
    # Primary Warehouse Release, Material Issue, and retry/status recovery.
    if frappe.get_meta("NKT Warehouse Release").has_field(
        "custom_nkt_fast_release_request_id"
    ):
        existing_request = str(
            release.get("custom_nkt_fast_release_request_id") or ""
        ).strip()
        if existing_request and existing_request != journal.name:
            raise NKTIdempotencyConflict(
                "Warehouse Release fast-request identity conflicts with preserved Release Intent."
            )
        release.custom_nkt_fast_release_request_id = journal.name
    release.release_datetime = get_datetime(journal.settled_at)
    release.released_by = journal.origin_user
    release.flags.nkt_c15c_preserve_offline_release = True
    release.flags.ignore_permissions = True


def _stock_entry_for_release(release_name: str):
    names = frappe.get_all(
        "Stock Entry",
        filters={
            "custom_nkt_warehouse_release": release_name,
            "custom_nkt_fulfillment_kind": "Warehouse Release",
            "docstatus": ["!=", 2],
        },
        pluck="name",
        limit_page_length=10,
    )
    if len(names) != 1:
        raise NKTIdempotencyConflict(
            f"Physical Warehouse Release must have exactly one live Material Issue; found {len(names)}."
        )
    entry = frappe.get_doc("Stock Entry", names[0])
    if int(entry.docstatus or 0) != 1:
        raise NKTIdempotencyConflict(
            "Physical Warehouse Release Material Issue is not submitted."
        )
    if str(entry.purpose or "") != "Material Issue":
        raise NKTIdempotencyConflict(
            "Physical Warehouse Release stock transaction is not a Material Issue."
        )
    return entry


def _verify_release_rows(release, payload):
    payload_rows = {
        str(line["warehouse_release_item"]): line
        for line in payload["items"]
    }
    total = 0.0
    for row in release.get("items") or []:
        expected = flt(payload_rows.get(row.name, {}).get("release_quantity"))
        actual = flt(row.release_quantity)
        if abs(actual - expected) > TOLERANCE:
            raise NKTIdempotencyConflict(
                "Submitted Warehouse Release quantity conflicts with preserved physical release."
            )
        if expected > TOLERANCE:
            if abs(flt(row.get("custom_nkt_reservation_consumed_qty")) - expected) > TOLERANCE:
                raise NKTIdempotencyConflict(
                    "Reservation consumption does not equal the physical release quantity."
                )
            total += actual
    if abs(total - flt(payload["total_release_quantity"])) > TOLERANCE:
        raise NKTIdempotencyConflict(
            "Submitted Warehouse Release total conflicts with preserved physical release."
        )


def _canonical_ack_json(value: Dict[str, Any]) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    )


def _payload_release_aggregates(payload: Dict[str, Any]) -> Dict[tuple[str, str], float]:
    out: Dict[tuple[str, str], float] = {}
    for line in payload["items"]:
        key = (str(line["item_code"]), str(line["source_warehouse"]))
        out[key] = flt(out.get(key)) + flt(line["release_quantity"])
    return out


def _materialization_stock_effects(payload: Dict[str, Any]) -> list[Dict[str, Any]]:
    effects = []
    for (item_code, warehouse), released_qty in sorted(
        _payload_release_aggregates(payload).items()
    ):
        actual_qty = flt(
            frappe.db.get_value(
                "Bin",
                {"item_code": item_code, "warehouse": warehouse},
                "actual_qty",
            )
        )
        effects.append(
            {
                "item_code": item_code,
                "warehouse": warehouse,
                "released_qty": float(f"{flt(released_qty):.6f}"),
                "primary_post_actual_qty": float(f"{actual_qty:.6f}"),
            }
        )
    return effects


def _build_materialization_ack(journal, payload, entry, ack_uuid: str) -> Dict[str, Any]:
    ack = {
        "event_uuid": journal.name,
        "payload_sha256": str(journal.payload_sha256 or "").lower(),
        "materialization_ack_uuid": ack_uuid,
        "warehouse_release": journal.warehouse_release,
        "customer_order": payload["customer_order"],
        "source_warehouse": payload["source_warehouse"],
        "stock_entry": entry.name,
        "stock_effects": _materialization_stock_effects(payload),
    }
    canonical = _canonical_ack_json(ack)
    return {
        **ack,
        "materialization_ack_sha256": hashlib.sha256(
            canonical.encode("utf-8")
        ).hexdigest(),
    }


def _verified_stored_materialization_ack(journal, payload, entry) -> Dict[str, Any]:
    raw = str(journal.materialization_ack_json or "")
    digest = str(journal.materialization_ack_sha256 or "").lower()
    if not raw or not digest:
        raise NKTIdempotencyConflict(
            "Primary journal is missing durable materialization ACK evidence."
        )
    if hashlib.sha256(raw.encode("utf-8")).hexdigest() != digest:
        raise NKTIdempotencyConflict(
            "Primary journal materialization ACK hash is invalid."
        )
    try:
        ack = json.loads(raw)
    except Exception as exc:
        raise NKTIdempotencyConflict(
            "Primary journal materialization ACK JSON is invalid."
        ) from exc
    if _canonical_ack_json(ack) != raw:
        raise NKTIdempotencyConflict(
            "Primary journal materialization ACK is not canonical."
        )

    expected_uuid = _materialization_ack_uuid(
        journal.name,
        journal.payload_sha256,
        entry.name,
    )
    checks = {
        "event_uuid": journal.name,
        "payload_sha256": str(journal.payload_sha256 or "").lower(),
        "materialization_ack_uuid": expected_uuid,
        "warehouse_release": journal.warehouse_release,
        "customer_order": payload["customer_order"],
        "source_warehouse": payload["source_warehouse"],
        "stock_entry": entry.name,
    }
    for field, expected in checks.items():
        if str(ack.get(field) or "") != str(expected or ""):
            raise NKTIdempotencyConflict(
                f"Primary materialization ACK {field} binding is invalid."
            )

    expected_agg = _payload_release_aggregates(payload)
    got_agg = {}
    effects = ack.get("stock_effects")
    if not isinstance(effects, list) or not effects:
        raise NKTIdempotencyConflict(
            "Primary materialization ACK has no authoritative stock effects."
        )
    for effect in effects:
        if not isinstance(effect, dict):
            raise NKTIdempotencyConflict("Primary materialization ACK stock effect is invalid.")
        item = str(effect.get("item_code") or "")
        warehouse = str(effect.get("warehouse") or "")
        key = (item, warehouse)
        if key in got_agg:
            raise NKTIdempotencyConflict(
                "Primary materialization ACK repeats an item/warehouse stock effect."
            )
        released = flt(effect.get("released_qty"))
        post_actual = flt(effect.get("primary_post_actual_qty"))
        if released <= 0 or (
            post_actual < 0
            and not _nkt_c15d_is_finished_oil_item(item)
        ):
            raise NKTIdempotencyConflict(
                "Primary materialization ACK stock quantities are invalid."
            )
        got_agg[key] = released
    if set(got_agg) != set(expected_agg):
        raise NKTIdempotencyConflict(
            "Primary materialization ACK stock effects do not match immutable release rows."
        )
    for key, expected in expected_agg.items():
        if abs(flt(got_agg[key]) - flt(expected)) > TOLERANCE:
            raise NKTIdempotencyConflict(
                "Primary materialization ACK released quantity conflicts with immutable payload."
            )
    return {**ack, "materialization_ack_sha256": digest}


def _verify_materialized(journal, payload) -> Dict[str, Any]:
    release_name = str(journal.materialized_warehouse_release or journal.warehouse_release or "")
    if release_name != str(journal.warehouse_release or ""):
        raise NKTIdempotencyConflict(
            "Physical-stock materialization points to another Warehouse Release."
        )
    if not frappe.db.exists("NKT Warehouse Release", release_name):
        raise NKTIdempotencyConflict("Materialized Warehouse Release is missing.")

    release = frappe.get_doc("NKT Warehouse Release", release_name)
    _assert_release_identity(release, journal, payload)
    if int(release.docstatus or 0) != 1 or str(release.release_status or "") != "Released":
        raise NKTIdempotencyConflict("Materialized Warehouse Release is not submitted Released.")

    if str(release.released_by or "") != str(journal.origin_user or ""):
        raise NKTIdempotencyConflict(
            "Materialized Warehouse Release lost the original warehouse operator."
        )
    if get_datetime(release.release_datetime) != get_datetime(journal.settled_at):
        raise NKTIdempotencyConflict(
            "Materialized Warehouse Release lost the original physical release time."
        )
    if str(release.get("custom_nkt_mother_release_reference") or "").strip().upper() != str(payload["release_reference"]).strip().upper():
        raise NKTIdempotencyConflict(
            "Materialized Warehouse Release reference conflicts with preserved intent."
        )
    if str(release.get("custom_nkt_authorized_by") or "") != str(journal.origin_user or ""):
        raise NKTIdempotencyConflict(
            "Materialized Warehouse Release audit operator conflicts with preserved intent."
        )
    if get_datetime(release.get("custom_nkt_authorized_on")) != get_datetime(journal.settled_at):
        raise NKTIdempotencyConflict(
            "Materialized Warehouse Release audit time conflicts with preserved intent."
        )

    _verify_release_rows(release, payload)
    entry = _stock_entry_for_release(release.name)

    if str(release.get("custom_nkt_stock_entry") or "") != entry.name:
        raise NKTIdempotencyConflict(
            "Warehouse Release linked Stock Entry conflicts with authoritative Material Issue."
        )
    if str(entry.get("custom_nkt_customer_order") or "") != str(payload["customer_order"] or ""):
        raise NKTIdempotencyConflict("Material Issue is linked to another Customer Order.")

    expected_ack = _materialization_ack_uuid(
        journal.name,
        journal.payload_sha256,
        entry.name,
    )
    if str(journal.materialized_stock_entry or "") != entry.name:
        raise NKTIdempotencyConflict("Primary journal lost materialized Stock Entry binding.")
    if str(journal.materialization_ack_uuid or "") != expected_ack:
        raise NKTIdempotencyConflict("Primary journal materialization ACK binding is invalid.")
    if str(journal.downstream_state or "") != "Physical Stock Materialized":
        raise NKTIdempotencyConflict("Primary journal is not in Physical Stock Materialized state.")

    stored_ack = _verified_stored_materialization_ack(journal, payload, entry)
    return {
        **stored_ack,
        "physical_release_time": str(release.release_datetime),
        "warehouse_operator": release.released_by,
        "downstream_state": journal.downstream_state,
        "warehouse_release_submitted": True,
        "stock_entry_created": True,
        "reservation_reduced": True,
        "admin_pre_release_approval_required": False,
        "edge_projection_may_finalize_only_after_local_stock_rebase": True,
    }


def materialize_physical_stock(event_uuid: str) -> Dict[str, Any]:
    _require_primary()
    event_uuid = _uuid(event_uuid, "Warehouse Release Intent UUID")

    # R5A: claim BEFORE the first mutable Primary-journal read. This removes
    # the stale REPEATABLE READ snapshot window exposed by the R5 true race.
    _acquire_materialization_claim(event_uuid)

    journal = _journal_for_update(event_uuid)
    if not journal:
        raise frappe.DoesNotExistError(
            "Preserved Warehouse Release Intent is unavailable at Primary."
        )
    payload = _payload_from_journal(journal)

    # Lock the canonical physical-release row for the rest of this transaction.
    # This also makes an online-vs-offline write race fail closed/serialize at
    # the authoritative release document rather than producing two stock paths.
    _lock_release_for_update(journal.warehouse_release)

    if str(journal.preservation_state or "") != "Preserved":
        raise NKTIdempotencyConflict("Warehouse Release Intent is not preserved.")
    if str(journal.downstream_state or "") == "Physical Stock Materialized":
        result = _verify_materialized(journal, payload)
        result["replay"] = True
        return result
    if str(journal.downstream_state or "") != "Awaiting Physical Stock Materialization":
        raise NKTIdempotencyConflict(
            "Warehouse Release Intent is not eligible for physical-stock materialization."
        )

    release = frappe.get_doc("NKT Warehouse Release", journal.warehouse_release)
    _project_payload_onto_draft(release, journal, payload)

    # Existing controller remains authoritative:
    # validate release/order/reservation/actual stock, submit release,
    # create one Material Issue, reduce reservation, update order fulfillment,
    # and create/sync the next partial draft when required.
    release.save(ignore_permissions=True)
    release.flags.nkt_c15c_preserve_offline_release = True
    release.flags.ignore_permissions = True
    release.submit()

    release.reload()
    entry = _stock_entry_for_release(release.name)
    ack_uuid = _materialization_ack_uuid(
        journal.name,
        journal.payload_sha256,
        entry.name,
    )
    ack = _build_materialization_ack(journal, payload, entry, ack_uuid)
    ack_json = _canonical_ack_json(
        {
            key: value
            for key, value in ack.items()
            if key != "materialization_ack_sha256"
        }
    )

    frappe.db.set_value(
        PRIMARY_JOURNAL,
        journal.name,
        {
            "downstream_state": "Physical Stock Materialized",
            "materialized_warehouse_release": release.name,
            "materialized_stock_entry": entry.name,
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
    return result


@frappe.whitelist()
def materialize_physical_stock_from_intent(event_uuid: str):
    return materialize_physical_stock(event_uuid)
