import frappe
from frappe import _
from frappe.custom.doctype.custom_field.custom_field import (
    create_custom_fields,
)


# Lower number = higher NKT approval authority.
NKT_ROLE_LEVELS = {
    "NKT OWNER": 0,
    "NKT ADMINISTRATOR": 10,
    "NKT Warehouse": 20,
    "NKT Encoder": 30,
    "NKT Cashier": 40,
}

SYSTEM_ADMIN_LEVEL = -10


def get_nkt_role_level(user=None):
    """
    Return the user's highest NKT authority.

    A user may hold several roles. The lowest numerical
    level is treated as the user's effective authority.
    """
    user = user or frappe.session.user

    if user == "Administrator":
        return SYSTEM_ADMIN_LEVEL

    user_roles = set(frappe.get_roles(user))

    levels = [
        level
        for role, level in NKT_ROLE_LEVELS.items()
        if role in user_roles
    ]

    if not levels:
        return None

    return min(levels)


def has_nkt_authority(max_level, user=None):
    """
    Example:
        max_level=10 permits:
        - Administrator
        - NKT OWNER
        - NKT ADMINISTRATOR

    It does not permit Warehouse, Encoder or Cashier.
    """
    level = get_nkt_role_level(user)

    return (
        level is not None
        and level <= int(max_level)
    )


def require_nkt_authority(
    max_level,
    user=None,
    action="perform this action",
):
    user = user or frappe.session.user

    if not has_nkt_authority(max_level, user):
        frappe.throw(
            _(
                "{0} is not authorized to {1}."
            ).format(
                user,
                action,
            ),
            frappe.PermissionError,
        )

    return get_nkt_role_level(user)


def install_role_levels():
    """
    One-time setup.

    This adds metadata only. It does not change existing
    DocType permissions or user-role assignments.
    """
    missing_roles = [
        role
        for role in NKT_ROLE_LEVELS
        if not frappe.db.exists("Role", role)
    ]

    if missing_roles:
        frappe.throw(
            _(
                "These required NKT roles do not exist: {0}"
            ).format(", ".join(missing_roles))
        )

    create_custom_fields(
        {
            "Role": [
                {
                    "fieldname": "nkt_authority_level",
                    "label": "NKT Authority Level",
                    "fieldtype": "Select",
                    "options": "\n0\n10\n20\n30\n40",
                    "insert_after": "desk_access",
                    "read_only": 1,
                    "in_list_view": 1,
                    "in_standard_filter": 1,
                    "no_copy": 1,
                    "description": (
                        "Lower number means higher NKT approval "
                        "authority. This does not grant or inherit "
                        "DocType permissions."
                    ),
                }
            ]
        },
        update=True,
    )

    for role, level in NKT_ROLE_LEVELS.items():
        frappe.db.set_value(
            "Role",
            role,
            "nkt_authority_level",
            str(level),
            update_modified=False,
        )

    frappe.db.commit()
    frappe.clear_cache()

    return NKT_ROLE_LEVELS
