import frappe


def run():
    email = "cashier@example.com"
    role_name = "NKT Cashier"

    # Enable Desk access for the role
    role = frappe.get_doc("Role", role_name)
    role.desk_access = 1
    role.disabled = 0
    role.flags.ignore_permissions = True
    role.save()

    # Ensure the user is enabled and has the role
    user = frappe.get_doc("User", email)
    user.enabled = 1
    user.user_type = "System User"

    if role_name not in [row.role for row in user.roles]:
        user.append("roles", {"role": role_name})

    user.flags.ignore_permissions = True
    user.save()

    def set_permission(doctype_name, rights):
        doctype = frappe.get_doc("DocType", doctype_name)

        permission = next(
            (
                row
                for row in doctype.permissions
                if row.role == role_name
                and int(row.permlevel or 0) == 0
            ),
            None,
        )

        if not permission:
            permission = doctype.append(
                "permissions",
                {
                    "role": role_name,
                    "permlevel": 0,
                },
            )

        for fieldname in (
            "read",
            "write",
            "create",
            "submit",
            "cancel",
            "delete",
            "amend",
            "report",
            "export",
            "share",
            "email",
            "if_owner",
        ):
            permission.set(fieldname, int(rights.get(fieldname, 0)))

        permission.set("print", int(rights.get("print", 0)))

        doctype.flags.ignore_permissions = True
        doctype.save()

    set_permission(
        "NKT Payment Receipt",
        {
            "read": 1,
            "write": 1,
            "create": 1,
            "submit": 1,
            "print": 1,
        },
    )

    set_permission(
        "NKT Customer Order",
        {
            "read": 1,
            "print": 1,
        },
    )

    frappe.db.commit()
    frappe.clear_cache(user=email)

    print("Cashier roles:", frappe.get_roles(email))
    print(
        "Payment Receipt read:",
        frappe.has_permission(
            "NKT Payment Receipt",
            "read",
            user=email,
        ),
    )
    print(
        "Payment Receipt create:",
        frappe.has_permission(
            "NKT Payment Receipt",
            "create",
            user=email,
        ),
    )
    print(
        "Customer Order read:",
        frappe.has_permission(
            "NKT Customer Order",
            "read",
            user=email,
        ),
    )
