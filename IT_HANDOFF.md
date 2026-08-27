# NKT ERPNext — IT Developer Handoff

**Repository:** `https://github.com/senggyap/N8-ERPNEXT`  
**Baseline tag:** `NKT-CONSOLIDATED-R1`  
**Working branch:** `develop`  
**Accepted/production-quality branch:** `main`

## 1. What this repository is

This repository is the **clean consolidated source of the currently working NKT ERPNext custom app**. It is the source to continue development from.

Baseline validated on 2026-08-27:

- App: `nkt_operations`
- Consolidated source files: **617**
- Aggregate source SHA-256: `b11ee20a25ff6008036f7a5e93af0cde882860305229d96358c94576f8e15155`
- Source DocTypes validated: **113**
- Canonical Client Scripts: **23**
- Legacy compatibility modules validated: **110**
- Whitelisted API functions preserved: **239**
- Whole consolidated structural/runtime regression: **PASS**

This is **not** the old patch-history tree. Historical backup files, recovery copies, disabled Client Scripts, temporary QA modules, and superseded implementation files were intentionally removed from the developer baseline.

## 2. Git workflow — do not develop directly on `main`

Clone and begin from `develop`:

```bash
git clone https://github.com/senggyap/N8-ERPNEXT.git
cd N8-ERPNEXT
git checkout develop
git pull origin develop
```

For each change:

```bash
git checkout develop
git pull origin develop
git checkout -b feature/short-description
```

Push the feature branch, test/review it, then merge through a Pull Request into `develop`. Merge `develop` into `main` only after NKT acceptance/regression testing.

**Never rewrite or delete the tag `NKT-CONSOLIDATED-R1`.** It is the frozen recovery point for this handoff.

## 3. Where to edit

Start with these canonical owners. Do **not** recreate C5/C7/C15/R1/R2-style phase files.

| Need to change | Start here |
|---|---|
| Encoder/Cashier Fast Screen backend | `nkt_operations/nkt_store_operations/fast_screen_backend.py` |
| Fast Screen routing / compatibility bridge | `nkt_operations/nkt_store_operations/fast_screen_routing.py` |
| Fast Screen UI | `nkt_operations/nkt_store_operations/current_client_scripts/fast_screen/` |
| Payments / customer accounts / advances | `nkt_operations/nkt_store_operations/features/payments_accounts/` |
| Sales / order matching | `nkt_operations/nkt_store_operations/features/sales/` |
| Cashier / shifts / Z-out / reconciliation | `nkt_operations/nkt_store_operations/features/cashier/` |
| Inventory / release / transfer / physical inventory | `nkt_operations/nkt_store_operations/features/inventory/` |
| Returns / exchanges / reversals | `nkt_operations/nkt_store_operations/features/returns/` |
| Supplier receiving | `nkt_operations/nkt_store_operations/features/receiving/` |
| Trucking | `nkt_operations/nkt_store_operations/features/trucking/` |
| Offline / Store Edge / safe sync | `nkt_operations/nkt_store_operations/features/offline_edge/` |
| Cooking oil | `nkt_operations/nkt_store_operations/features/oil/` |
| Security / role hierarchy | `nkt_operations/nkt_store_operations/features/security/` |
| Reports / receipt support | `nkt_operations/nkt_store_operations/features/reports_history/` |
| Item movement history | `nkt_operations/nkt_store_operations/item_movement_history.py` |
| Transaction history | `nkt_operations/nkt_store_operations/transaction_history.py` |
| Manager authorization | `nkt_operations/nkt_store_operations/manager_authorization.py` |
| Canonical Client Script synchronization | `nkt_operations/nkt_store_operations/client_scripts_sync.py` |
| Frappe hooks | `nkt_operations/hooks.py` |
| Legacy path compatibility only | `nkt_operations/nkt_store_operations/compat_aliases.py` |

Also read `nkt_operations/nkt_store_operations/FEATURE_MAP.md` before making a large change.

### Important architecture rule

- Put **business truth server-side**.
- UI/Fast Screen validation may assist the user but must not be the only enforcement.
- Low-level sync/materializer code belongs under the feature's `internal/` folder.
- `compat_aliases.py` is a compatibility layer only. **Do not add new business logic there.**

## 4. Business rules that must not be casually changed

These are intentional NKT operating rules. Change them only with explicit owner approval and regression testing.

### Fast Screen / sale flow

- Keyboard-first workflow; minimal clicks; no product-image UI dependency.
- **F10 = Complete + Print**.
- **F11 = Payment only**.
- **F12 = Complete without print**.
- Payment opens focused for cashier entry; payment confirmation and sale-complete Enter behavior must remain keyboard-safe.
- Every buyer must be a real Customer; do not introduce a generic Walk-in customer shortcut.
- Encoder may set items, quantities, rates and source warehouse; Cashier must not gain authority to change those official sale details.

### Payments

- Cash, GCash, Maya, Bank Transfer, Card, Check and approved account flows are supported according to existing implementation.
- **Card only** carries the current 2% surcharge rule. **Maya does not.**
- Preserve duplicate-reference/check controls and existing overpayment controls.
- Split payments are supported and must remain compatible with the normal single-payment flow.

### OS# / Plate / references

- `OS#` is the **optional number on the physical paper Order Slip**.
- OS# is reference/audit data only. It must never become required for payment, posting, matching, stock movement or release.
- Plate Number is optional and may be added/known later according to the current workflow.
- Payment IDs, references, OS# and Plate information must remain auditable.

### Roles / privacy

- Preserve Encoder, Cashier, Store Manager, Owner/Admin and other existing role boundaries.
- External-carrier trucking/commercial records are restricted to **NKT OWNER / NKT ADMINISTRATOR** in normal operations.
- Do not expose supplier prices, payables, margins, sensitive carrier financials or owner-only controls to frontline roles.
- Do not bypass permission checks from Fast Screens, reports or offline code.

### Returns / stock / receiving

- Returns/exchanges do not edit the original sale; preserve the controlled return/exchange workflow.
- Preserve source-warehouse, release, transfer and physical-inventory controls.
- Supplier Receiving must retain the sanitized employee-facing workflow and the current separation of supplier-commercial information.

### Offline / Store Edge

- Preserve true physical event date/time captured offline, including cross-midnight cases where implemented.
- Preserve idempotent/replay-safe behavior and existing local continuity rules.
- Do not simplify offline code by removing primary-intent/materialization safeguards unless the replacement has equivalent regression proof.

### Cooking oil

- Oil controls and repacking authority remain restricted according to the current implementation.
- Preserve true physical repacking date versus later ERP creation/encoding time.

## 5. Client Scripts

The current live UI customization was consolidated from many database Client Script layers into **23 canonical source-controlled scripts** under:

`nkt_operations/nkt_store_operations/current_client_scripts/`

When changing one of these scripts:

1. Edit the canonical source file.
2. Use the existing synchronization path in `client_scripts_sync.py`.
3. Verify database/source equality after synchronization.
4. Test the actual affected screen in the browser.

Do not create another stack of overlapping database-only Client Scripts unless there is a deliberate documented reason.

## 6. Validation required before merge

At minimum, after relevant changes:

1. Compile/check Python.
2. Syntax-check JavaScript.
3. Verify hook targets and DocType controllers load.
4. Verify Client Script method targets resolve.
5. Run targeted feature tests.
6. Hands-on test the affected employee workflow.
7. If the change touches money, stock, permissions or offline/sync, explicitly regression-test those resulting effects and retry/replay paths.

For broad changes, repeat the whole-system regression standard used to validate the consolidated baseline.

## 7. Installation / environment

Known validation environment:

- Bench root used during development: `/workspace/development/frappe-bench`
- Validation site: `development.localhost`
- App: `nkt_operations`

Use a clone/staging environment first. Install/place the app in a compatible Frappe/ERPNext bench and perform the normal migration/build/cache steps for that environment.

**Important:** this Git repository contains application source, not the NKT production database, credentials, site secrets or business-data backups. Current-site parity therefore requires the compatible NKT database/configuration. A brand-new empty site should not be called production-equivalent until a clean install/restore rehearsal is completed.

## 8. What not to do

- Do not develop directly on `main`.
- Do not delete `NKT-CONSOLIDATED-R1`.
- Do not recreate hundreds of patch/recovery modules.
- Do not move new business logic into `compat_aliases.py`.
- Do not silently change locked role/security/payment/stock/offline rules.
- Do not use production business data as disposable QA fixtures.
- Do not remove compatibility paths until all callers are deliberately migrated and regression-tested.

## 9. First files to read

1. `IT_HANDOFF.md` — this file.
2. `nkt_operations/nkt_store_operations/FEATURE_MAP.md` — backend ownership map.
3. `nkt_operations/hooks.py` — runtime hooks/overrides.
4. `nkt_operations/nkt_store_operations/current_client_scripts/manifest.json` — canonical Client Script map.
5. `nkt_operations/nkt_store_operations/compat_aliases.py` — old-path compatibility map; reference only for new development.

---

**Handoff principle:** continue development from **what works now**. Improve the implementation forward; do not reconstruct the historical patch journey.
