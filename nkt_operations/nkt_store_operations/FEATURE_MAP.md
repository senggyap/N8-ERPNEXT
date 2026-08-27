# NKT Current Backend Feature Map

**Rule:** edit the canonical implementation below. Do not create new phase/R-number modules. Old dotted paths are compatibility aliases only.

## Fast Screen
- `fast_screen_backend.py` — main backend
- `fast_screen_routing.py` — current Edge/routing wrapper
- `current_client_scripts/fast_screen/encoder.js` — Encoder UI
- `current_client_scripts/fast_screen/cashier.js` — Cashier UI
- `features/fast_screen/fast_customer_creation.py` — fast customer creation

## Cashier
- `features/cashier/encoder_zout.py`
- `features/cashier/reconciliation.py`
- `features/cashier/shift_engine.py`
- `features/cashier/shift_report.py`
- Internal support: `features/cashier/internal/`

## Inventory
- `features/inventory/item_stock_mapping.py`
- `features/inventory/order_fulfillment.py`
- `features/inventory/physical_inventory.py`
- `features/inventory/warehouse_release_fast_sync.py`
- `features/inventory/warehouse_transfer_fast_sync.py`
- Internal support: `features/inventory/internal/`

## Offline Edge
- `features/offline_edge/edge_store.py`
- `features/offline_edge/policy.py`
- `features/offline_edge/read_services.py`
- `features/offline_edge/safe_sync.py`
- `features/offline_edge/sync_transport.py`
- Internal support: `features/offline_edge/internal/`

## Oil
- `features/oil/controls.py`

## Payments Accounts
- `features/payments_accounts/card_surcharge.py`
- `features/payments_accounts/cash_ledger.py`
- `features/payments_accounts/collection.py`
- `features/payments_accounts/credit.py`
- `features/payments_accounts/receivables.py`
- `features/payments_accounts/statement.py`
- Internal support: `features/payments_accounts/internal/`

## Receiving
- `features/receiving/supplier_receiving_edge.py`
- `features/receiving/supplier_receiving_materializer.py`
- `features/receiving/supplier_receiving_physical_intent.py`
- Internal support: `features/receiving/internal/`

## Reports History
- `features/reports_history/receipt_support.py`

## Returns
- `features/returns/matching.py`
- `features/returns/posting.py`
- `features/returns/reversal.py`
- `features/returns/service.py`
- Internal support: `features/returns/internal/`

## Sales
- `features/sales/customer_order_intent.py`
- `features/sales/customer_order_materialization.py`
- `features/sales/manual_match.py`
- `features/sales/matching.py`

## Security
- `features/security/role_hierarchy.py`

## Setup Validation
- Internal support: `features/setup_validation/internal/`

## Trucking
- `features/trucking/access.py`
- `features/trucking/permissions.py`
- `features/trucking/trucking_materializer.py`
- `features/trucking/trucking_offline_contract.py`
- Internal support: `features/trucking/internal/`

