from __future__ import annotations

import json

import frappe


WORKSPACE = "NKT Account Operations"
MODULE = "NKT Store Operations"
APP = "nkt_operations"


NAV_LINKS = [
    {
        "label": "Receive Account Payment",
        "doctype": "NKT Cashier Account Collection",
        "doc_view": "New",
    },
    {
        "label": "Cashier Account Collections",
        "doctype": "NKT Cashier Account Collection",
        "doc_view": "List",
    },
    {
        "label": "Verify Account Payment",
        "doctype": "NKT Encoder Account Allocation",
        "doc_view": "New",
    },
    {
        "label": "Account Payment Verifications",
        "doctype": "NKT Encoder Account Allocation",
        "doc_view": "List",
    },
    {
        "label": "Create Statement of Account",
        "doctype": "NKT Customer Statement",
        "doc_view": "New",
    },
    {
        "label": "Customer Statements",
        "doctype": "NKT Customer Statement",
        "doc_view": "List",
    },
    {
        "label": "Aging Alerts",
        "doctype": "NKT Account Aging Alert",
        "doc_view": "List",
    },
    {
        "label": "Payment Corrections",
        "doctype": "NKT Account Payment Correction",
        "doc_view": "List",
    },
    {
        "label": "Customer Receivables",
        "doctype": "NKT Customer Receivable",
        "doc_view": "List",
    },
    {
        "label": "Customer Orders",
        "doctype": "NKT Customer Order",
        "doc_view": "List",
    },
    {
        "label": "Customers",
        "doctype": "Customer",
        "doc_view": "List",
    },
]


def _ensure_navigation_properties():
    for doctype in {
        "NKT Cashier Account Collection",
        "NKT Encoder Account Allocation",
        "NKT Customer Receivable",
        "NKT Customer Statement",
        "NKT Account Aging Alert",
        "NKT Account Payment Correction",
    }:
        if not frappe.db.exists("DocType", doctype):
            continue
        values = {"module": MODULE}
        meta = frappe.get_meta("DocType")
        if meta.has_field("hide_from_search"):
            values["hide_from_search"] = 0
        if meta.has_field("description"):
            descriptions = {
                "NKT Cashier Account Collection": "Cashier entry for money received from a customer account.",
                "NKT Encoder Account Allocation": "Independent encoder verification of a customer account payment. Applications are automatic unless an authorized override is required.",
                "NKT Customer Receivable": "Official operational customer account balance created from an approved account sale.",
                "NKT Customer Statement": "Printable statement of account with charges, verified payments, running balance, aging, and delivery audit.",
                "NKT Account Aging Alert": "Customer-level overdue and aging alerts generated from the operational receivable subledger.",
                "NKT Account Payment Correction": "Controlled reversal or reapplication of a matched account payment without creating another cashier receipt or movement.",
            }
            values["description"] = descriptions[doctype]
        frappe.db.set_value("DocType", doctype, values, update_modified=False)
        frappe.clear_cache(doctype=doctype)


def _workspace_content():
    return json.dumps(
        [
            {
                "id": "nkt-account-header",
                "type": "header",
                "data": {
                    "text": '<span class="h4"><b>Customer Account Operations</b></span>',
                    "col": 12,
                },
            },
            {
                "id": "nkt-account-card",
                "type": "card",
                "data": {"card_name": "Account Operations", "col": 6},
            },
        ],
        separators=(",", ":"),
    )


def _populate_workspace(doc):
    doc.title = WORKSPACE
    doc.label = WORKSPACE
    doc.module = MODULE
    doc.app = APP
    doc.type = "Workspace"
    doc.icon = "accounting"
    doc.indicator_color = "blue"
    doc.public = 1
    doc.is_hidden = 0
    doc.hide_custom = 0
    doc.content = _workspace_content()
    doc.set("shortcuts", [])
    doc.set("links", [])
    doc.set("roles", [])
    for role in (
        "System Manager",
        "NKT OWNER",
        "NKT ADMINISTRATOR",
        "NKT Credit Controller",
        "NKT Encoder",
        "NKT Cashier",
    ):
        doc.append("roles", {"role": role})

    for nav in NAV_LINKS:
        doc.append(
            "shortcuts",
            {
                "type": "DocType",
                "link_to": nav["doctype"],
                "doc_view": nav["doc_view"],
                "label": nav["label"],
            },
        )

    doc.append(
        "links",
        {
            "type": "Card Break",
            "label": "Account Operations",
            "icon": "accounting",
            "link_count": len(NAV_LINKS),
            "description": "Receive payments, verify applications, review receivables, and print customer statements.",
        },
    )
    for nav in NAV_LINKS:
        doc.append(
            "links",
            {
                "type": "Link",
                "label": nav["label"],
                "link_type": "DocType",
                "link_to": nav["doctype"],
                "onboard": 0,
            },
        )


def _ensure_workspace():
    frappe.set_user("Administrator")
    old_patch_flag = getattr(frappe.flags, "in_patch", False)
    frappe.flags.in_patch = True
    try:
        if frappe.db.exists("Workspace", WORKSPACE):
            doc = frappe.get_doc("Workspace", WORKSPACE)
        else:
            doc = frappe.new_doc("Workspace")
        _populate_workspace(doc)
        doc.flags.ignore_permissions = True
        if doc.is_new():
            doc.insert(ignore_permissions=True)
        else:
            doc.save(ignore_permissions=True)
    finally:
        frappe.flags.in_patch = old_patch_flag

    from frappe.desk.doctype.desktop_icon.desktop_icon import add_workspace_to_desktop

    add_workspace_to_desktop(WORKSPACE)
    frappe.cache.delete_key("bootinfo")
    frappe.cache.delete_key("desktop_icons")
    return doc.name


def install_navigation():
    _ensure_navigation_properties()
    workspace = _ensure_workspace()
    return {
        "installed": True,
        "workspace": workspace,
        "cashier_label": "Receive Account Payment",
        "encoder_label": "Verify Account Payment",
    }
