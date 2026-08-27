from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional

import frappe
from frappe.utils import now

from nkt_operations.nkt_store_operations.features.offline_edge.policy import event_family_policy, validate_business_time
from nkt_operations.nkt_store_operations.features.offline_edge.internal.probe_adapter import PROBE_ACTION, PROBE_FAMILY, _canonical_payload_json as _probe_canonical_json, _normalize_probe_payload
from nkt_operations.nkt_store_operations.features.sales.customer_order_intent import ORDER_INTENT_ACTION, ORDER_INTENT_FAMILY, _canonical_order_intent_json, _normalize_order_intent_payload
from nkt_operations.nkt_store_operations.features.payments_accounts.internal.cashier_tender_intent import TENDER_INTENT_ACTION, TENDER_INTENT_FAMILY, _canonical_cashier_tender_intent_json, _normalize_cashier_tender_intent_payload
from nkt_operations.nkt_store_operations.features.payments_accounts.internal.encoder_settlement_intent import ENCODER_SETTLEMENT_ACTION, ENCODER_SETTLEMENT_FAMILY, _canonical_encoder_settlement_intent_json, _normalize_encoder_settlement_intent_payload
from nkt_operations.nkt_store_operations.features.inventory.internal.warehouse_release_intent import WAREHOUSE_RELEASE_INTENT_ACTION, WAREHOUSE_RELEASE_INTENT_FAMILY, _canonical_warehouse_release_intent_json, _normalize_warehouse_release_intent_payload
from nkt_operations.nkt_store_operations.features.cashier.internal.cash_drawer_adjustment_intent import CASH_DRAWER_ADJUSTMENT_INTENT_ACTION, CASH_DRAWER_ADJUSTMENT_INTENT_FAMILY, _canonical_cash_drawer_adjustment_intent_json, _normalize_cash_drawer_adjustment_intent_payload
from nkt_operations.nkt_store_operations.features.inventory.internal.warehouse_transfer_dispatch_intent import ACTION as WAREHOUSE_TRANSFER_DISPATCH_INTENT_ACTION, FAMILY as WAREHOUSE_TRANSFER_DISPATCH_INTENT_FAMILY, canonical_dispatch_payload_json, normalize_dispatch_payload
from nkt_operations.nkt_store_operations.features.inventory.internal.warehouse_transfer_arrival_intent import ACTION as WAREHOUSE_TRANSFER_ARRIVAL_INTENT_ACTION, FAMILY as WAREHOUSE_TRANSFER_ARRIVAL_INTENT_FAMILY, canonical_arrival_payload_json, normalize_arrival_payload
from nkt_operations.nkt_store_operations.features.receiving.supplier_receiving_physical_intent import ACTION as SUPPLIER_RECEIVING_PHYSICAL_INTENT_ACTION, FAMILY as SUPPLIER_RECEIVING_PHYSICAL_INTENT_FAMILY, canonical_supplier_receiving_payload_json, normalize_supplier_receiving_payload
from nkt_operations.nkt_store_operations.features.returns.internal.return_exchange_offline_intent import ACTION as RETURN_EXCHANGE_DECLARATION_INTENT_ACTION, FAMILY as RETURN_EXCHANGE_DECLARATION_INTENT_FAMILY, canonical_return_exchange_intent_json, normalize_return_exchange_intent
from nkt_operations.nkt_store_operations.features.inventory.internal.physical_inventory_offline_intent import ACTION as PHYSICAL_INVENTORY_COUNT_INTENT_ACTION, FAMILY as PHYSICAL_INVENTORY_COUNT_INTENT_FAMILY, canonical_physical_inventory_count_intent_json, normalize_physical_inventory_count_intent
from nkt_operations.nkt_store_operations.features.cashier.internal.shift_close_zout_offline_intent import CASHIER_SHIFT_OPEN_ACTION, CASHIER_SHIFT_OPEN_FAMILY, CASHIER_SHIFT_CLOSE_ACTION, CASHIER_SHIFT_CLOSE_FAMILY, ENCODER_ZOUT_FINALIZE_ACTION, ENCODER_ZOUT_FINALIZE_FAMILY, canonical_cashier_shift_open_intent_json, canonical_cashier_shift_close_intent_json, canonical_encoder_zout_finalization_intent_json, normalize_cashier_shift_open_intent, normalize_cashier_shift_close_intent, normalize_encoder_zout_finalization_intent
from nkt_operations.nkt_store_operations.features.trucking.trucking_offline_contract import TRIP_LIFECYCLE_ACTION, TRIP_LIFECYCLE_FAMILY, canonical_trucking_trip_lifecycle_intent_json, normalize_trucking_trip_lifecycle_intent
from nkt_operations.nkt_store_operations.features.offline_edge.safe_sync import NKTIdempotencyConflict, canonical_payload_hash, mark_awaiting_primary, mark_primary_committed

FOUNDATION_VERSION='C15C.9C-R1'
PACKET_KEYS={'envelope','payload'}
ENVELOPE_KEYS={'event_uuid','event_family','event_action','operational_context','origin_device','origin_user','business_date','settled_at','client_created_at','payload_sha256'}

@dataclass(frozen=True)
class SyncFamilySpec:
    event_family: str
    event_action: str
    normalize_payload: Callable[[Dict[str,Any]],Dict[str,Any]]
    canonical_payload_json: Callable[[Dict[str,Any]],str]
    generic_primary_receipt_allowed: bool = True

_FAMILY_REGISTRY={
    PROBE_FAMILY: SyncFamilySpec(
        PROBE_FAMILY,
        PROBE_ACTION,
        _normalize_probe_payload,
        _probe_canonical_json,
        True,
    ),
    ORDER_INTENT_FAMILY: SyncFamilySpec(
        ORDER_INTENT_FAMILY,
        ORDER_INTENT_ACTION,
        _normalize_order_intent_payload,
        _canonical_order_intent_json,
        False,
    ),
    TENDER_INTENT_FAMILY: SyncFamilySpec(
        TENDER_INTENT_FAMILY,
        TENDER_INTENT_ACTION,
        _normalize_cashier_tender_intent_payload,
        _canonical_cashier_tender_intent_json,
        False,
    ),
    ENCODER_SETTLEMENT_FAMILY: SyncFamilySpec(
        ENCODER_SETTLEMENT_FAMILY,
        ENCODER_SETTLEMENT_ACTION,
        _normalize_encoder_settlement_intent_payload,
        _canonical_encoder_settlement_intent_json,
        False,
    ),
    WAREHOUSE_RELEASE_INTENT_FAMILY: SyncFamilySpec(
        WAREHOUSE_RELEASE_INTENT_FAMILY,
        WAREHOUSE_RELEASE_INTENT_ACTION,
        _normalize_warehouse_release_intent_payload,
        _canonical_warehouse_release_intent_json,
        False,
    ),
    CASH_DRAWER_ADJUSTMENT_INTENT_FAMILY: SyncFamilySpec(
        CASH_DRAWER_ADJUSTMENT_INTENT_FAMILY,
        CASH_DRAWER_ADJUSTMENT_INTENT_ACTION,
        _normalize_cash_drawer_adjustment_intent_payload,
        _canonical_cash_drawer_adjustment_intent_json,
        False,
    ),
    WAREHOUSE_TRANSFER_DISPATCH_INTENT_FAMILY: SyncFamilySpec(
        WAREHOUSE_TRANSFER_DISPATCH_INTENT_FAMILY,
        WAREHOUSE_TRANSFER_DISPATCH_INTENT_ACTION,
        normalize_dispatch_payload,
        canonical_dispatch_payload_json,
        False,
    ),
    WAREHOUSE_TRANSFER_ARRIVAL_INTENT_FAMILY: SyncFamilySpec(
        WAREHOUSE_TRANSFER_ARRIVAL_INTENT_FAMILY,
        WAREHOUSE_TRANSFER_ARRIVAL_INTENT_ACTION,
        normalize_arrival_payload,
        canonical_arrival_payload_json,
        False,
    ),
    SUPPLIER_RECEIVING_PHYSICAL_INTENT_FAMILY: SyncFamilySpec(
        SUPPLIER_RECEIVING_PHYSICAL_INTENT_FAMILY,
        SUPPLIER_RECEIVING_PHYSICAL_INTENT_ACTION,
        normalize_supplier_receiving_payload,
        canonical_supplier_receiving_payload_json,
        False,
    ),
    RETURN_EXCHANGE_DECLARATION_INTENT_FAMILY: SyncFamilySpec(
        RETURN_EXCHANGE_DECLARATION_INTENT_FAMILY,
        RETURN_EXCHANGE_DECLARATION_INTENT_ACTION,
        normalize_return_exchange_intent,
        canonical_return_exchange_intent_json,
        False,
    ),
    PHYSICAL_INVENTORY_COUNT_INTENT_FAMILY: SyncFamilySpec(
        PHYSICAL_INVENTORY_COUNT_INTENT_FAMILY,
        PHYSICAL_INVENTORY_COUNT_INTENT_ACTION,
        normalize_physical_inventory_count_intent,
        canonical_physical_inventory_count_intent_json,
        False,
    ),
    CASHIER_SHIFT_OPEN_FAMILY: SyncFamilySpec(
        CASHIER_SHIFT_OPEN_FAMILY,
        CASHIER_SHIFT_OPEN_ACTION,
        normalize_cashier_shift_open_intent,
        canonical_cashier_shift_open_intent_json,
        False,
    ),
    CASHIER_SHIFT_CLOSE_FAMILY: SyncFamilySpec(
        CASHIER_SHIFT_CLOSE_FAMILY,
        CASHIER_SHIFT_CLOSE_ACTION,
        normalize_cashier_shift_close_intent,
        canonical_cashier_shift_close_intent_json,
        False,
    ),
    ENCODER_ZOUT_FINALIZE_FAMILY: SyncFamilySpec(
        ENCODER_ZOUT_FINALIZE_FAMILY,
        ENCODER_ZOUT_FINALIZE_ACTION,
        normalize_encoder_zout_finalization_intent,
        canonical_encoder_zout_finalization_intent_json,
        False,
    ),
    TRIP_LIFECYCLE_FAMILY: SyncFamilySpec(
        TRIP_LIFECYCLE_FAMILY,
        TRIP_LIFECYCLE_ACTION,
        normalize_trucking_trip_lifecycle_intent,
        canonical_trucking_trip_lifecycle_intent_json,
        False,
    ),
}

def registered_transport_families():
    return tuple(sorted(_FAMILY_REGISTRY))

def _runtime_role():
    return str(frappe.conf.get('nkt_runtime_role') or 'Primary').strip()

def _uuid(value,label):
    try:return str(uuid.UUID(str(value)))
    except Exception as exc:raise frappe.ValidationError(f'{label} must be a valid UUID.') from exc

def _family_spec(event_family):
    family=str(event_family or '').strip();spec=_FAMILY_REGISTRY.get(family)
    if not spec:raise frappe.ValidationError('Safe-sync family is not registered for transport.')
    if event_family_policy(family).get('offline_write_allowed') is not True:
        raise frappe.PermissionError('Safe-sync family is not enabled for offline transport.')
    return spec

def _event_envelope(event):
    return {'event_uuid':event.event_uuid,'event_family':event.event_family,'event_action':event.event_action,'operational_context':event.operational_context,'origin_device':event.origin_device,'origin_user':event.origin_user,'business_date':str(event.business_date),'settled_at':str(event.settled_at),'client_created_at':str(event.client_created_at) if event.client_created_at else None,'payload_sha256':event.payload_sha256}

def prepare_event_for_primary(event_uuid,*,expected_family=None):
    if _runtime_role()!='Store Edge':raise frappe.PermissionError('Safe-sync replication unavailable.')
    event_uuid=_uuid(event_uuid,'Event UUID')
    if not frappe.db.exists('NKT Sync Event',event_uuid):raise frappe.DoesNotExistError('Safe-sync event is unavailable.')
    if not frappe.db.exists('NKT Sync Pending Payload',event_uuid):raise frappe.DoesNotExistError('Safe-sync pending payload is unavailable.')
    event=frappe.get_doc('NKT Sync Event',event_uuid);pending=frappe.get_doc('NKT Sync Pending Payload',event_uuid);spec=_family_spec(event.event_family)
    if expected_family and event.event_family!=expected_family:raise frappe.ValidationError('Safe-sync event family does not match the requested adapter.')
    if event.event_action!=spec.event_action:raise frappe.ValidationError('Safe-sync event action is not registered for this family.')
    if event.sync_state not in ('Accepted at Edge','Awaiting Primary'):raise frappe.ValidationError('Safe-sync event is not pending Primary replication.')
    if pending.event_family!=event.event_family:raise frappe.ValidationError('Pending payload family mismatch.')
    if pending.payload_sha256!=event.payload_sha256:raise NKTIdempotencyConflict('Pending payload hash does not match immutable event.')
    payload=json.loads(pending.payload_json);normalized=spec.normalize_payload(payload)
    if spec.canonical_payload_json(normalized)!=pending.payload_json:raise NKTIdempotencyConflict('Pending payload canonical form changed.')
    if canonical_payload_hash(normalized)!=event.payload_sha256:raise NKTIdempotencyConflict('Pending payload content does not match immutable event.')
    mark_awaiting_primary(event_uuid)
    frappe.db.set_value('NKT Sync Pending Payload',event_uuid,{'queue_state':'Awaiting Primary','attempt_count':int(pending.attempt_count or 0)+1,'last_attempt_at':now()},update_modified=False)
    event.reload();return {'envelope':_event_envelope(event),'payload':normalized}

def validate_transport_packet(packet,*,expected_family=None):
    if not isinstance(packet,dict) or set(packet)!=PACKET_KEYS:raise frappe.ValidationError('Safe-sync packet shape is invalid.')
    envelope=packet.get('envelope');payload=packet.get('payload')
    if not isinstance(envelope,dict) or set(envelope)!=ENVELOPE_KEYS:raise frappe.ValidationError('Safe-sync envelope shape is invalid.')
    event_uuid=_uuid(envelope.get('event_uuid'),'Event UUID');spec=_family_spec(envelope.get('event_family'))
    if expected_family and spec.event_family!=expected_family:raise frappe.ValidationError('Primary receiver family does not match the requested adapter.')
    if envelope.get('event_action')!=spec.event_action:raise frappe.ValidationError('Primary packet action is not registered for this family.')
    normalized=spec.normalize_payload(payload);payload_hash=canonical_payload_hash(normalized)
    if payload_hash!=str(envelope.get('payload_sha256') or '').lower():raise NKTIdempotencyConflict('Primary packet payload hash mismatch.')
    validate_business_time(envelope.get('business_date'),envelope.get('settled_at'))
    env_norm=dict(envelope);env_norm['event_uuid']=event_uuid;env_norm['payload_sha256']=payload_hash
    return env_norm,normalized,canonical_payload_hash(env_norm),payload_hash

def receive_at_primary(packet,*,expected_family=None):
    if _runtime_role()!='Primary':raise frappe.PermissionError('Primary safe-sync receiver unavailable.')
    envelope,payload,envelope_hash,payload_hash=validate_transport_packet(packet,expected_family=expected_family)
    spec=_family_spec(envelope['event_family'])
    if not spec.generic_primary_receipt_allowed:
        raise frappe.ValidationError('This safe-sync family requires a dedicated Primary materializer.')
    event_uuid=envelope['event_uuid'];family=envelope['event_family'];existing=frappe.db.exists('NKT Sync Primary Receipt',event_uuid)
    if existing:
        receipt=frappe.get_doc('NKT Sync Primary Receipt',existing);bad=[]
        if receipt.event_family!=family:bad.append('event_family')
        if receipt.envelope_sha256!=envelope_hash:bad.append('envelope_sha256')
        if receipt.payload_sha256!=payload_hash:bad.append('payload_sha256')
        if bad:raise NKTIdempotencyConflict('Primary receipt replay conflicts with immutable content: '+', '.join(bad))
        return {'event_uuid':event_uuid,'event_family':family,'primary_ack_uuid':receipt.primary_ack_uuid,'payload_sha256':receipt.payload_sha256,'result_code':receipt.result_code,'committed':True,'replay':True,'critical_business_record_created':False}
    receipt=frappe.get_doc({'doctype':'NKT Sync Primary Receipt','event_uuid':event_uuid,'event_family':family,'primary_ack_uuid':str(uuid.uuid4()),'envelope_sha256':envelope_hash,'payload_sha256':payload_hash,'primary_received_at':now(),'primary_committed_at':now(),'result_code':'Committed','materialization_state':'Technical Receipt Only'})
    try:receipt.insert(ignore_permissions=True);replay=False
    except frappe.DuplicateEntryError:
        receipt=frappe.get_doc('NKT Sync Primary Receipt',event_uuid)
        if receipt.event_family!=family or receipt.envelope_sha256!=envelope_hash or receipt.payload_sha256!=payload_hash:raise NKTIdempotencyConflict('Concurrent Primary receipt collision with different content.')
        replay=True
    return {'event_uuid':event_uuid,'event_family':family,'primary_ack_uuid':receipt.primary_ack_uuid,'payload_sha256':receipt.payload_sha256,'result_code':receipt.result_code,'committed':True,'replay':replay,'critical_business_record_created':False}

def apply_primary_ack_at_edge(ack,*,expected_family=None):
    if _runtime_role()!='Store Edge':raise frappe.PermissionError('Safe-sync ACK application unavailable.')
    if not isinstance(ack,dict):raise frappe.ValidationError('Primary ACK is invalid.')
    event_uuid=_uuid(ack.get('event_uuid'),'Event UUID');ack_uuid=_uuid(ack.get('primary_ack_uuid'),'Primary ACK UUID');payload_hash=str(ack.get('payload_sha256') or '').lower()
    if ack.get('committed') is not True or ack.get('result_code')!='Committed':raise frappe.ValidationError('Primary ACK is not committed.')
    if not frappe.db.exists('NKT Sync Event',event_uuid):raise frappe.DoesNotExistError('Safe-sync event is unavailable.')
    event=frappe.get_doc('NKT Sync Event',event_uuid);spec=_family_spec(event.event_family)
    if expected_family and event.event_family!=expected_family:raise frappe.ValidationError('Primary ACK family does not match the requested adapter.')
    if event.event_action!=spec.event_action:raise frappe.ValidationError('Primary ACK event action is not registered.')
    if event.payload_sha256!=payload_hash:raise NKTIdempotencyConflict('Primary ACK payload hash conflicts with immutable event.')
    bound=str(event.primary_ack_uuid or '').strip()
    if bound and bound!=ack_uuid:raise NKTIdempotencyConflict('Primary ACK UUID conflicts with the ACK already bound to this event.')
    pending=frappe.db.exists('NKT Sync Pending Payload',event_uuid)
    if pending:
        pd=frappe.get_doc('NKT Sync Pending Payload',pending)
        if pd.payload_sha256!=payload_hash:raise NKTIdempotencyConflict('Primary ACK conflicts with pending payload.')
        if event.sync_state not in ('Accepted at Edge','Awaiting Primary'):raise frappe.ValidationError('Pending event is not eligible for Primary ACK.')
        mark_primary_committed(event_uuid,'NKT Sync Primary Receipt',event_uuid,primary_ack_uuid=ack_uuid)
        frappe.delete_doc('NKT Sync Pending Payload',pd.name,ignore_permissions=True,force=True)
        return {'event_uuid':event_uuid,'event_family':event.event_family,'primary_ack_uuid':ack_uuid,'sync_state':'Committed at Primary','pending_payload_purged':True,'replay':False}
    event.reload()
    if event.sync_state=='Committed at Primary' and event.canonical_doctype=='NKT Sync Primary Receipt' and event.canonical_name==event_uuid and str(event.primary_ack_uuid or '').strip()==ack_uuid:
        return {'event_uuid':event_uuid,'event_family':event.event_family,'primary_ack_uuid':ack_uuid,'sync_state':'Committed at Primary','pending_payload_purged':False,'replay':True}
    raise frappe.ValidationError('Primary ACK arrived without a matching pending payload.')
