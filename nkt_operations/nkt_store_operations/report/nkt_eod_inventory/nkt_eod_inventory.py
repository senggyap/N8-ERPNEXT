from __future__ import annotations

from collections import defaultdict

import frappe
from frappe import _
from frappe.utils import cint, flt, getdate, nowdate

TOLERANCE = 0.000001


def execute(filters=None):
	filters = frappe._dict(filters or {})
	company = filters.get("company") or frappe.defaults.get_user_default("Company")
	business_date = getdate(filters.get("business_date") or nowdate())
	warehouse = (filters.get("warehouse") or "").strip()
	only_eod_selected = cint(filters.get("only_eod_selected", 1))
	include_zero_balances = cint(filters.get("include_zero_balances", 1))

	if not company:
		frappe.throw(_("Company is required."))

	if warehouse:
		warehouse_row = frappe.db.get_value(
			"Warehouse",
			warehouse,
			["company", "is_group", "disabled"],
			as_dict=True,
		)
		if not warehouse_row:
			frappe.throw(_("Warehouse {0} does not exist.").format(warehouse))
		if warehouse_row.company != company:
			frappe.throw(_("Warehouse {0} does not belong to Company {1}.").format(warehouse, company))
		if cint(warehouse_row.is_group):
			frappe.throw(_("Select a leaf Warehouse, not a warehouse group."))
		if cint(warehouse_row.disabled):
			frappe.throw(_("Warehouse {0} is disabled.").format(warehouse))

	rows = _inventory_rows(
		company=company,
		business_date=business_date,
		warehouse=warehouse,
		only_eod_selected=only_eod_selected,
		include_zero_balances=include_zero_balances,
	)

	columns = _columns()
	data = _with_subtotals(rows)

	negative_count = sum(
		1 for row in rows if flt(row.get("system_inventory")) < -TOLERANCE
	)
	warehouse_count = len({row.get("warehouse") for row in rows})
	detail_count = len(rows)

	report_summary = [
		{
			"label": _("Inventory Lines"),
			"value": detail_count,
			"datatype": "Int",
		},
		{
			"label": _("Warehouses"),
			"value": warehouse_count,
			"datatype": "Int",
		},
		{
			"label": _("Negative System Balances"),
			"value": negative_count,
			"datatype": "Int",
			"indicator": "Red" if negative_count else "Green",
		},
	]

	message = None
	if negative_count:
		message = _(
			"{0} inventory line(s) have negative System EOD balances. "
			"Keep them visible and reconcile the physical/business cause; "
			"the report does not hide or normalize negative stock."
		).format(negative_count)

	return columns, data, message, None, report_summary, 1


def _columns():
	return [
		{
			"fieldname": "warehouse",
			"label": _("Warehouse"),
			"fieldtype": "Link",
			"options": "Warehouse",
			"width": 170,
		},
		{
			"fieldname": "category",
			"label": _("Category"),
			"fieldtype": "Data",
			"width": 120,
		},
		{
			"fieldname": "item_display",
			"label": _("Item"),
			"fieldtype": "Data",
			"width": 220,
		},
		{
			"fieldname": "uom",
			"label": _("UOM"),
			"fieldtype": "Link",
			"options": "UOM",
			"width": 70,
		},
		{
			"fieldname": "opening_inventory",
			"label": _("Opening"),
			"fieldtype": "Float",
			"precision": 3,
			"width": 85,
		},
		{
			"fieldname": "inward_movement",
			"label": _("In"),
			"fieldtype": "Float",
			"precision": 3,
			"width": 75,
		},
		{
			"fieldname": "outward_movement",
			"label": _("Out"),
			"fieldtype": "Float",
			"precision": 3,
			"width": 75,
		},
		{
			"fieldname": "ledger_adjustment",
			"label": _("Adj."),
			"fieldtype": "Float",
			"precision": 3,
			"width": 75,
		},
		{
			"fieldname": "system_inventory",
			"label": _("System EOD"),
			"fieldtype": "Float",
			"precision": 3,
			"width": 95,
		},
		{
			"fieldname": "physical_count",
			"label": _("Physical Count"),
			"fieldtype": "Data",
			"width": 95,
		},
		{
			"fieldname": "variance",
			"label": _("Variance"),
			"fieldtype": "Data",
			"width": 80,
		},
		{
			"fieldname": "remarks",
			"label": _("Remarks"),
			"fieldtype": "Data",
			"width": 120,
		},
	]


def _inventory_rows(
	*,
	company,
	business_date,
	warehouse,
	only_eod_selected,
	include_zero_balances,
):
	params = {
		"company": company,
		"business_date": business_date,
	}

	warehouse_clause = ""
	if warehouse:
		params["warehouse"] = warehouse
		warehouse_clause = " AND sle.warehouse = %(warehouse)s "

	selected_clause = ""
	if only_eod_selected:
		selected_clause = " AND IFNULL(item.custom_nkt_include_in_zout_inventory, 0) = 1 "

	# IMPORTANT:
	# - Opening and System EOD use the latest authoritative qty_after_transaction.
	# - In/Out use signed Stock Ledger actual_qty during the target day.
	# - Stock Reconciliation can change qty_after_transaction while actual_qty is
	#   zero in this ERPNext build, so the residual is surfaced as Adjustment.
	query = f"""
		SELECT
			sle.item_code,
			sle.warehouse,
			item.item_name,
			item.item_group AS category,
			item.stock_uom AS uom,
			item.nkt_stock_form,
			item.nkt_base_saleable_item,
			IFNULL(item.custom_nkt_include_in_zout_inventory, 0) AS eod_selected,

			COALESCE(
				(
					SELECT opening_sle.qty_after_transaction
					FROM `tabStock Ledger Entry` opening_sle
					WHERE opening_sle.item_code = sle.item_code
					  AND opening_sle.warehouse = sle.warehouse
					  AND IFNULL(opening_sle.is_cancelled, 0) = 0
					  AND opening_sle.posting_date < %(business_date)s
					ORDER BY
						opening_sle.posting_datetime DESC,
						opening_sle.creation DESC,
						opening_sle.name DESC
					LIMIT 1
				),
				0
			) AS opening_inventory,

			SUM(
				CASE
					WHEN sle.posting_date = %(business_date)s
					 AND sle.actual_qty > 0
					THEN sle.actual_qty
					ELSE 0
				END
			) AS inward_movement,

			SUM(
				CASE
					WHEN sle.posting_date = %(business_date)s
					 AND sle.actual_qty < 0
					THEN -sle.actual_qty
					ELSE 0
				END
			) AS outward_movement,

			COALESCE(
				(
					SELECT closing_sle.qty_after_transaction
					FROM `tabStock Ledger Entry` closing_sle
					WHERE closing_sle.item_code = sle.item_code
					  AND closing_sle.warehouse = sle.warehouse
					  AND IFNULL(closing_sle.is_cancelled, 0) = 0
					  AND closing_sle.posting_date <= %(business_date)s
					ORDER BY
						closing_sle.posting_datetime DESC,
						closing_sle.creation DESC,
						closing_sle.name DESC
					LIMIT 1
				),
				0
			) AS system_inventory

		FROM `tabStock Ledger Entry` sle
		INNER JOIN `tabItem` item
			ON item.name = sle.item_code
		INNER JOIN `tabWarehouse` wh
			ON wh.name = sle.warehouse
		WHERE IFNULL(sle.is_cancelled, 0) = 0
		  AND sle.posting_date <= %(business_date)s
		  AND IFNULL(item.is_stock_item, 0) = 1
		  AND IFNULL(item.disabled, 0) = 0
		  AND wh.company = %(company)s
		  AND IFNULL(wh.is_group, 0) = 0
		  AND IFNULL(wh.disabled, 0) = 0
		  {warehouse_clause}
		  {selected_clause}
		GROUP BY
			sle.item_code,
			sle.warehouse,
			item.item_name,
			item.item_group,
			item.stock_uom,
			item.nkt_stock_form,
			item.nkt_base_saleable_item,
			item.custom_nkt_include_in_zout_inventory
		ORDER BY
			sle.warehouse,
			item.item_group,
			item.item_name,
			sle.item_code
	"""

	raw = frappe.db.sql(query, params, as_dict=True)

	out = []
	for row in raw:
		opening = flt(row.opening_inventory, 6)
		inward = flt(row.inward_movement, 6)
		outward = flt(row.outward_movement, 6)
		system = flt(row.system_inventory, 6)
		adjustment = flt(system - opening - inward + outward, 6)

		if (
			not include_zero_balances
			and abs(opening) <= TOLERANCE
			and abs(inward) <= TOLERANCE
			and abs(outward) <= TOLERANCE
			and abs(adjustment) <= TOLERANCE
			and abs(system) <= TOLERANCE
		):
			continue

		item_name = (row.item_name or row.item_code or "").strip()
		item_code = (row.item_code or "").strip()
		item_display = item_name
		if item_code and item_name and item_code != item_name:
			item_display = "{0} ({1})".format(item_name, item_code)

		out.append(
			{
				"row_type": "Detail",
				"warehouse": row.warehouse,
				"category": row.category or "",
				"item_code": row.item_code,
				"item_display": item_display,
				"uom": row.uom or "",
				"opening_inventory": opening,
				"inward_movement": inward,
				"outward_movement": outward,
				"ledger_adjustment": adjustment,
				"system_inventory": system,
				"physical_count": "",
				"variance": "",
				"remarks": "",
				"negative_system_balance": int(system < -TOLERANCE),
				"eod_selected": cint(row.eod_selected),
				"nkt_stock_form": row.nkt_stock_form or "",
				"nkt_base_saleable_item": row.nkt_base_saleable_item or "",
			}
		)

	return out


def _with_subtotals(rows):
	grouped = defaultdict(list)
	for row in rows:
		grouped[row["warehouse"]].append(row)

	out = []
	for warehouse in sorted(grouped):
		detail_rows = grouped[warehouse]
		out.extend(detail_rows)

		uom_totals = defaultdict(
			lambda: {
				"opening_inventory": 0.0,
				"inward_movement": 0.0,
				"outward_movement": 0.0,
				"ledger_adjustment": 0.0,
				"system_inventory": 0.0,
			}
		)

		for row in detail_rows:
			totals = uom_totals[row["uom"] or "(No UOM)"]
			for fieldname in totals:
				totals[fieldname] += flt(row.get(fieldname), 6)

		for uom in sorted(uom_totals):
			totals = uom_totals[uom]
			out.append(
				{
					"row_type": "Subtotal",
					"warehouse": warehouse,
					"category": _("TOTAL"),
					"item_code": "",
					"item_display": _("Warehouse subtotal"),
					"uom": uom,
					"opening_inventory": flt(totals["opening_inventory"], 6),
					"inward_movement": flt(totals["inward_movement"], 6),
					"outward_movement": flt(totals["outward_movement"], 6),
					"ledger_adjustment": flt(totals["ledger_adjustment"], 6),
					"system_inventory": flt(totals["system_inventory"], 6),
					"physical_count": "",
					"variance": "",
					"remarks": "",
					"negative_system_balance": 0,
				}
			)

	return out
