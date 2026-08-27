# NKT Operations — consolidated effective hooks
# Current working baseline + source-controlled Client Script sync.
# Historical hook mutations remain removed; only FINAL effective state is declared.

app_name = 'nkt_operations'

app_title = 'NKT Operations'

app_publisher = 'NKT Grains Trading'

app_description = 'Integrated ERP, POS, inventory, trucking and compliance system for NKT'

app_email = 'admin@test.com'

app_license = 'mit'

app_include_js = ['/assets/nkt_operations/js/item_nkt_mapping.js']

doc_events = {'NKT Customer Receivable': {'before_insert': ['nkt_operations.nkt_store_operations.features.payments_accounts.receivables.validate_new_receivable']},
 'NKT Cashier Shift': {'before_insert': ['nkt_operations.nkt_store_operations.features.cashier.shift_engine.validate_shift_before_insert'],
                       'validate': ['nkt_operations.nkt_store_operations.features.cashier.shift_engine.validate_shift'],
                       'before_submit': ['nkt_operations.nkt_store_operations.features.cashier.shift_engine.validate_shift_before_submit'],
                       'before_cancel': ['nkt_operations.nkt_store_operations.features.cashier.shift_engine.prevent_shift_cancel']},
 'NKT Cashier Movement': {'before_insert': ['nkt_operations.nkt_store_operations.features.cashier.shift_engine.validate_movement_shift_open'],
                          'before_submit': ['nkt_operations.nkt_store_operations.features.cashier.shift_engine.validate_movement_shift_open']},
 'NKT Cash Drawer Adjustment': {'before_insert': ['nkt_operations.nkt_store_operations.features.cashier.shift_engine.validate_adjustment_before_insert'],
                                'validate': ['nkt_operations.nkt_store_operations.features.cashier.shift_engine.validate_adjustment'],
                                'before_submit': ['nkt_operations.nkt_store_operations.features.cashier.shift_engine.before_submit_adjustment'],
                                'on_submit': ['nkt_operations.nkt_store_operations.features.cashier.shift_engine.post_adjustment_movement'],
                                'before_cancel': ['nkt_operations.nkt_store_operations.features.cashier.shift_engine.prevent_adjustment_cancel']},
 'NKT Customer Order': {'before_validate': 'nkt_operations.nkt_store_operations.features.sales.customer_order_materialization.guard_offline_materialized_order_price_drift'},
 'NKT Trucking Trip': {'validate': 'nkt_operations.nkt_store_operations.features.trucking.access.validate_employee_trip_scope'}}

scheduler_events = {'daily': ['nkt_operations.nkt_store_operations.features.payments_accounts.receivables.scheduled_refresh_aging_alerts']}

permission_query_conditions = {'NKT Cashier Shift': 'nkt_operations.nkt_store_operations.features.cashier.shift_engine.get_shift_permission_query_conditions',
 'NKT Cashier Movement': 'nkt_operations.nkt_store_operations.features.cashier.shift_engine.get_movement_permission_query_conditions',
 'NKT Cash Drawer Adjustment': 'nkt_operations.nkt_store_operations.features.cashier.shift_engine.get_adjustment_permission_query_conditions',
 'User': 'nkt_operations.nkt_store_operations.features.cashier.shift_engine.get_user_permission_query_conditions',
 'NKT Trucking Trip': 'nkt_operations.nkt_store_operations.features.trucking.access.get_trip_permission_query_conditions',
 'NKT Trucking Waybill': 'nkt_operations.nkt_store_operations.features.trucking.access.get_waybill_permission_query_conditions',
 'NKT Trucking Job': 'nkt_operations.nkt_store_operations.features.trucking.access.get_job_permission_query_conditions',
 'NKT Trucker SOA': 'nkt_operations.nkt_store_operations.features.trucking.access.deny_external_commercial_query',
 'NKT Trucker Payment': 'nkt_operations.nkt_store_operations.features.trucking.access.deny_external_commercial_query',
 'NKT Trucker Adjustment': 'nkt_operations.nkt_store_operations.features.trucking.access.deny_external_commercial_query'}

has_permission = {'NKT Cashier Shift': 'nkt_operations.nkt_store_operations.features.cashier.shift_engine.has_shift_permission',
 'NKT Cashier Movement': 'nkt_operations.nkt_store_operations.features.cashier.shift_engine.has_movement_permission',
 'NKT Cash Drawer Adjustment': 'nkt_operations.nkt_store_operations.features.cashier.shift_engine.has_adjustment_permission',
 'User': 'nkt_operations.nkt_store_operations.features.cashier.shift_engine.has_user_permission',
 'NKT Trucking Trip': 'nkt_operations.nkt_store_operations.features.trucking.access.has_trip_permission',
 'NKT Trucking Waybill': 'nkt_operations.nkt_store_operations.features.trucking.access.has_waybill_permission',
 'NKT Trucking Job': 'nkt_operations.nkt_store_operations.features.trucking.access.has_job_permission',
 'NKT Trucker SOA': 'nkt_operations.nkt_store_operations.features.trucking.access.has_external_commercial_permission',
 'NKT Trucker Payment': 'nkt_operations.nkt_store_operations.features.trucking.access.has_external_commercial_permission',
 'NKT Trucker Adjustment': 'nkt_operations.nkt_store_operations.features.trucking.access.has_external_commercial_permission'}

override_whitelisted_methods = {'nkt_operations.nkt_store_operations.fast_screen_backend.get_fast_ui_bootstrap': 'nkt_operations.nkt_store_operations.fast_screen_routing.get_fast_ui_bootstrap',
 'nkt_operations.nkt_store_operations.fast_screen_backend.search_items': 'nkt_operations.nkt_store_operations.fast_screen_routing.search_items',
 'nkt_operations.nkt_store_operations.fast_screen_backend.search_customers': 'nkt_operations.nkt_store_operations.fast_screen_routing.search_customers',
 'nkt_operations.nkt_store_operations.fast_screen_backend.get_item_context': 'nkt_operations.nkt_store_operations.fast_screen_routing.get_item_context',
 'nkt_operations.nkt_store_operations.fast_screen_backend.preflight_incoming_check': 'nkt_operations.nkt_store_operations.fast_screen_routing.preflight_incoming_check',
 'nkt_operations.nkt_store_operations.fast_screen_backend.finalize_encoder_fast_transaction': 'nkt_operations.nkt_store_operations.fast_screen_routing.finalize_encoder_fast_transaction',
 'nkt_operations.nkt_store_operations.fast_screen_backend.finalize_cashier_fast_transaction': 'nkt_operations.nkt_store_operations.fast_screen_routing.finalize_cashier_fast_transaction',
 'nkt_operations.nkt_store_operations.fast_screen_backend.get_fast_request_status': 'nkt_operations.nkt_store_operations.fast_screen_routing.get_fast_request_status',
 'nkt_operations.nkt_store_operations.features.fast_screen.fast_customer_creation.create_fast_customer': 'nkt_operations.nkt_store_operations.fast_screen_routing.create_fast_customer'}

after_migrate = ['nkt_operations.nkt_store_operations.features.trucking.permissions.after_migrate',
 'nkt_operations.nkt_store_operations.manager_authorization.after_migrate',
 'nkt_operations.nkt_store_operations.client_scripts_sync.after_migrate']

# ITC5 compatibility: old public Fast Screen API paths still route exactly as before.
override_whitelisted_methods.update({
    "nkt_operations.nkt_store_operations.nkt_fast_ui_v2.get_fast_ui_bootstrap": "nkt_operations.nkt_store_operations.fast_screen_routing.get_fast_ui_bootstrap",
    "nkt_operations.nkt_store_operations.nkt_fast_ui_v2.search_items": "nkt_operations.nkt_store_operations.fast_screen_routing.search_items",
    "nkt_operations.nkt_store_operations.nkt_fast_ui_v2.search_customers": "nkt_operations.nkt_store_operations.fast_screen_routing.search_customers",
    "nkt_operations.nkt_store_operations.nkt_fast_ui_v2.get_item_context": "nkt_operations.nkt_store_operations.fast_screen_routing.get_item_context",
    "nkt_operations.nkt_store_operations.nkt_fast_ui_v2.preflight_incoming_check": "nkt_operations.nkt_store_operations.fast_screen_routing.preflight_incoming_check",
    "nkt_operations.nkt_store_operations.nkt_fast_ui_v2.finalize_encoder_fast_transaction": "nkt_operations.nkt_store_operations.fast_screen_routing.finalize_encoder_fast_transaction",
    "nkt_operations.nkt_store_operations.nkt_fast_ui_v2.finalize_cashier_fast_transaction": "nkt_operations.nkt_store_operations.fast_screen_routing.finalize_cashier_fast_transaction",
    "nkt_operations.nkt_store_operations.nkt_fast_ui_v2.get_fast_request_status": "nkt_operations.nkt_store_operations.fast_screen_routing.get_fast_request_status",
    "nkt_operations.nkt_store_operations.nkt_c5_6_fast_customer_creation.create_fast_customer": "nkt_operations.nkt_store_operations.fast_screen_routing.create_fast_customer",
})
