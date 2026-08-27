
import frappe
from frappe.utils import flt, getdate, today

TOLERANCE = 0.005

ADVANCE_DOCTYPE = "NKT Customer Advance"
APPLICATION_DOCTYPE = "NKT Customer Advance Application"

ENTRY_ADVANCE_RECEIVED = "Customer Advance Received"
ENTRY_ADVANCE_APPLIED = "Customer Advance Applied"
ENTRY_ADVANCE_REVERSED = "Advance Application Reversed"
ENTRY_ADVANCE_BALANCE = "Available Customer Advance"


def _as_float(value):
    return round(flt(value), 9)


def _date(value):
    if not value:
        return None
    return getdate(value)


def _aging_field_for_due_date(due_date, as_of_date):
    """
    Return the SOA aging-summary field for an approved Receivable as of the
    statement date. This mirrors nkt_account_statement._bucket_for without a
    circular module import.
    """
    if not due_date:
        return "aging_current"
    days = (getdate(as_of_date) - getdate(due_date)).days
    if days <= 0:
        return "aging_current"
    if days <= 30:
        return "aging_1_30"
    if days <= 60:
        return "aging_31_60"
    if days <= 90:
        return "aging_61_90"
    return "aging_over_90"


def _line(
    posting_date,
    entry_type,
    reference_doctype="",
    reference_name="",
    customer_order="",
    description="",
    debit=0,
    credit=0,
    due_date=None,
):
    return {
        "posting_date": posting_date,
        "entry_type": entry_type,
        "reference_doctype": reference_doctype or "",
        "reference_name": reference_name or "",
        "customer_order": customer_order or "",
        "due_date": due_date,
        "description": description,
        "debit": _as_float(debit),
        "credit": _as_float(credit),
        "running_balance": 0,
        "days_overdue": 0,
        "aging_bucket": "",
    }


def _load_advances(company, customer, to_date):
    return frappe.get_all(
        ADVANCE_DOCTYPE,
        filters={
            "company": company,
            "customer": customer,
            "docstatus": 1,
            "posting_datetime": ["<=", str(to_date) + " 23:59:59"],
        },
        fields=[
            "name",
            "posting_datetime",
            "source_payment_receipt",
            "source_customer_order",
            "original_advance_amount",
            "applied_amount",
            "available_advance_amount",
            "advance_status",
            "creation",
        ],
        order_by="posting_datetime asc, creation asc, name asc",
        limit_page_length=10000,
    )


def _load_applications(company, customer, to_date):
    if not frappe.db.exists("DocType", APPLICATION_DOCTYPE):
        return []

    meta = frappe.get_meta(APPLICATION_DOCTYPE)

    fields = [
        "name",
        "posting_datetime",
        "customer_advance",
        "source_payment_receipt",
        "customer_order",
        "applied_amount",
        "application_status",
        "applied_by",
        "remarks",
        "creation",
        "modified",
    ]

    for fieldname in (
        "custom_nkt_reversed_on",
        "custom_nkt_reversed_by",
        "custom_nkt_reversal_reason",
    ):
        if meta.has_field(fieldname):
            fields.append(fieldname)

    return frappe.get_all(
        APPLICATION_DOCTYPE,
        filters={
            "company": company,
            "customer": customer,
            "docstatus": 1,
            "posting_datetime": ["<=", str(to_date) + " 23:59:59"],
        },
        fields=fields,
        order_by="posting_datetime asc, creation asc, name asc",
        limit_page_length=10000,
    )


def _approved_receivable_for_order(customer_order):
    if not customer_order:
        return None

    rows = frappe.get_all(
        "NKT Customer Receivable",
        filters={
            "customer_order": customer_order,
            "docstatus": ["!=", 2],
            "credit_control_status": "Approved",
        },
        fields=[
            "name",
            "customer_order",
            "original_amount",
            "amount_paid",
            "outstanding_amount",
            "status",
            "credit_control_status",
            "due_date",
        ],
        order_by="creation asc",
        limit_page_length=1,
    )

    return rows[0] if rows else None


def _available_as_of(advances, applications, to_date):
    received = {}

    for advance in advances:
        posting_date = _date(advance.posting_datetime)
        if posting_date and posting_date <= to_date:
            received[advance.name] = max(
                _as_float(advance.original_advance_amount),
                0,
            )

    used = {name: 0.0 for name in received}

    for app in applications:
        if app.customer_advance not in used:
            continue

        posting_date = _date(app.posting_datetime)
        if not posting_date or posting_date > to_date:
            continue

        if (app.application_status or "").strip() == "Applied":
            used[app.customer_advance] += max(
                _as_float(app.applied_amount),
                0,
            )

    return _as_float(
        sum(
            max(original - used.get(name, 0.0), 0)
            for name, original in received.items()
        )
    )


def _priority(entry_type):
    return {
        "Account Sale": 20,
        "Account Payment": 30,
        ENTRY_ADVANCE_RECEIVED: 35,
        ENTRY_ADVANCE_REVERSED: 38,
        ENTRY_ADVANCE_APPLIED: 40,
        ENTRY_ADVANCE_BALANCE: 99,
    }.get(entry_type, 50)


def _sort(lines):
    indexed = list(enumerate(lines))

    return [
        row
        for _, row in sorted(
            indexed,
            key=lambda pair: (
                str(_date(pair[1].get("posting_date")) or ""),
                _priority(pair[1].get("entry_type")),
                pair[0],
            ),
        )
    ]


def augment_statement(
    data,
    company,
    customer,
    from_date_value,
    to_date_value,
):
    if not data:
        return data

    from_date = getdate(from_date_value)
    to_date = getdate(to_date_value)

    advances = _load_advances(company, customer, to_date)
    applications = _load_applications(company, customer, to_date)

    base_lines = [dict(row) for row in (data.get("lines") or [])]
    extra_lines = []

    opening_advance_credit_to_ar = 0.0
    period_advance_received = 0.0
    period_advance_applied_total = 0.0
    period_advance_applied_to_ar = 0.0
    reversed_count = 0

    for advance in advances:
        posting_date = _date(advance.posting_datetime)
        if not posting_date:
            continue

        amount = max(_as_float(advance.original_advance_amount), 0)

        if from_date <= posting_date <= to_date:
            period_advance_received += amount

            extra_lines.append(
                _line(
                    posting_date=posting_date,
                    entry_type=ENTRY_ADVANCE_RECEIVED,
                    reference_doctype="NKT Payment Receipt",
                    reference_name=advance.source_payment_receipt,
                    customer_order=advance.source_customer_order,
                    description=(
                        f"Customer Advance {advance.name} received from "
                        f"Payment Receipt {advance.source_payment_receipt or '-'}: "
                        f"{amount:,.2f}. Held as Customer Advance; this does "
                        f"not reduce approved Accounts Receivable until the "
                        f"advance is applied to an approved Account order."
                    ),
                )
            )

    for app in applications:
        posting_date = _date(app.posting_datetime)
        if not posting_date:
            continue

        amount = max(_as_float(app.applied_amount), 0)
        status = (app.application_status or "").strip()
        approved_receivable = _approved_receivable_for_order(
            app.customer_order
        )

        if status == "Applied":
            period_advance_applied_total += (
                amount
                if from_date <= posting_date <= to_date
                else 0
            )

            ar_credit = amount if approved_receivable else 0.0

            if approved_receivable:
                if posting_date < from_date:
                    opening_advance_credit_to_ar += amount
                elif posting_date <= to_date:
                    period_advance_applied_to_ar += amount

            if from_date <= posting_date <= to_date:
                if approved_receivable:
                    effect_text = (
                        f"Applied against approved receivable "
                        f"{approved_receivable.name}; this reduces Accounts "
                        f"Receivable by {amount:,.2f}."
                    )
                else:
                    effect_text = (
                        "Applied to an order that is not represented by an "
                        "approved Customer Receivable in this SOA; shown for "
                        "history only and does not change the AR running balance."
                    )

                extra_lines.append(
                    _line(
                        posting_date=posting_date,
                        entry_type=ENTRY_ADVANCE_APPLIED,
                        reference_doctype=APPLICATION_DOCTYPE,
                        reference_name=app.name,
                        customer_order=app.customer_order,
                        description=(
                            f"Customer Advance {app.customer_advance} applied "
                            f"to {app.customer_order or 'Customer Order'}: "
                            f"{amount:,.2f}. Source Payment Receipt: "
                            f"{app.source_payment_receipt or '-'}. "
                            f"{effect_text} No new Payment Receipt or Cashier "
                            f"Movement was created."
                        ),
                        credit=ar_credit,
                        due_date=(
                            approved_receivable.due_date
                            if approved_receivable
                            else None
                        ),
                    )
                )

        elif status == "Reversed":
            reversed_count += 1

            if from_date <= posting_date <= to_date:
                reason = (
                    app.get("custom_nkt_reversal_reason")
                    or app.remarks
                    or "Reversed application retained for audit."
                )

                extra_lines.append(
                    _line(
                        posting_date=posting_date,
                        entry_type=ENTRY_ADVANCE_REVERSED,
                        reference_doctype=APPLICATION_DOCTYPE,
                        reference_name=app.name,
                        customer_order=app.customer_order,
                        description=(
                            f"Reversed Customer Advance application {app.name}: "
                            f"{amount:,.2f}. Advance: {app.customer_advance}. "
                            f"Source Payment Receipt: "
                            f"{app.source_payment_receipt or '-'}. {reason} "
                            f"Legacy reversed rows have no separate reliable "
                            f"reversal posting timestamp, so this audit row "
                            f"does not alter the AR running balance."
                        ),
                    )
                )

    available_advance = _available_as_of(
        advances,
        applications,
        to_date,
    )

    extra_lines.append(
        _line(
            posting_date=to_date,
            entry_type=ENTRY_ADVANCE_BALANCE,
            reference_doctype="Customer",
            reference_name=customer,
            description=(
                f"Available Customer Advance as of {to_date}: "
                f"{available_advance:,.2f}. This is separate from approved "
                f"Accounts Receivable."
            ),
        )
    )

    opening_balance = _as_float(
        flt(data.get("opening_balance"))
        - opening_advance_credit_to_ar
    )

    combined = _sort(base_lines + extra_lines)

    running = opening_balance
    rebuilt = []

    for row in combined:
        row = dict(row)
        running = _as_float(
            running
            + flt(row.get("debit"))
            - flt(row.get("credit"))
        )
        row["running_balance"] = running
        rebuilt.append(row)

    data["opening_balance"] = opening_balance
    data["lines"] = rebuilt
    data["closing_balance"] = running

    # The base SOA aging calculation knows regular matched account-payment
    # allocations, but C5.3 Customer Advance applications are layered on after
    # that calculation. Therefore an Applied Advance that reduces approved AR
    # must also reduce the SAME Receivable due-date bucket. Otherwise the SOA
    # closing balance is correct while aging still shows the pre-Advance AR.
    #
    # Use ALL applied-to-approved-AR applications through to_date, including
    # applications before from_date, because aging is an as-of snapshot.
    # Pending/unapproved orders do not produce approved_receivable and therefore
    # remain separate Customer Advance; Reversed applications are not Applied.
    advance_aging_credit = {
        "aging_current": 0.0,
        "aging_1_30": 0.0,
        "aging_31_60": 0.0,
        "aging_61_90": 0.0,
        "aging_over_90": 0.0,
    }

    for app in applications:
        if (app.application_status or "").strip() != "Applied":
            continue

        posting_date = _date(app.posting_datetime)
        if not posting_date or posting_date > to_date:
            continue

        approved_receivable = _approved_receivable_for_order(
            app.customer_order
        )
        if not approved_receivable:
            continue

        bucket_field = _aging_field_for_due_date(
            approved_receivable.due_date,
            to_date,
        )
        advance_aging_credit[bucket_field] += max(
            _as_float(app.applied_amount),
            0,
        )

    for fieldname, credit in advance_aging_credit.items():
        base_value = _as_float(data.get(fieldname))
        data[fieldname] = _as_float(
            max(base_value - credit, 0)
        )

    data["period_advance_received"] = _as_float(
        period_advance_received
    )
    data["period_advance_applied"] = _as_float(
        period_advance_applied_total
    )
    data["period_advance_applied_to_accounts_receivable"] = _as_float(
        period_advance_applied_to_ar
    )
    data["available_customer_advance"] = available_advance
    data["reversed_advance_application_count"] = reversed_count

    return data


@frappe.whitelist()
def get_customer_advance_history(
    company,
    customer,
    from_date=None,
    to_date=None,
):
    if not to_date:
        to_date = today()
    if not from_date:
        from_date = "1900-01-01"

    return augment_statement(
        {
            "lines": [],
            "opening_balance": 0,
            "period_charges": 0,
            "period_payments": 0,
            "closing_balance": 0,
        },
        company,
        customer,
        from_date,
        to_date,
    )


def ensure_statement_entry_types():
    if not frappe.db.exists(
        "DocType",
        "NKT Customer Statement Line",
    ):
        return {
            "updated": False,
            "reason": "statement line doctype missing",
        }

    row = frappe.db.get_value(
        "DocField",
        {
            "parent": "NKT Customer Statement Line",
            "fieldname": "entry_type",
        },
        ["name", "fieldtype", "options"],
        as_dict=True,
    )

    if not row:
        return {
            "updated": False,
            "reason": "entry_type DocField missing",
        }

    if row.fieldtype != "Select":
        return {
            "updated": False,
            "reason": f"entry_type is {row.fieldtype}; no options update needed",
        }

    options = [
        value
        for value in (row.options or "").splitlines()
        if value.strip()
    ]

    required = [
        ENTRY_ADVANCE_RECEIVED,
        ENTRY_ADVANCE_APPLIED,
        ENTRY_ADVANCE_REVERSED,
        ENTRY_ADVANCE_BALANCE,
    ]

    changed = False

    for value in required:
        if value not in options:
            options.append(value)
            changed = True

    if changed:
        frappe.db.set_value(
            "DocField",
            row.name,
            "options",
            "\n".join(options),
            update_modified=False,
        )
        frappe.db.commit()
        frappe.clear_cache(
            doctype="NKT Customer Statement Line"
        )

    return {
        "updated": changed,
        "fieldtype": row.fieldtype,
        "options": options,
    }


def verify():
    statement_module = (
        "nkt_operations.nkt_store_operations.features.payments_accounts.statement"
    )

    data = frappe.get_attr(
        statement_module + ".get_statement_data"
    )(
        company="NKT (Dev)",
        customer="TEST - ACCOUNT CUSTOMER",
        from_date="2026-08-01",
        to_date="2026-08-11",
    )

    lines = data.get("lines") or []

    applied_00048 = [
        row
        for row in lines
        if row.get("entry_type") == ENTRY_ADVANCE_APPLIED
        and row.get("customer_order") == "NKT-ORD-00048"
    ]

    applied_00030 = [
        row
        for row in lines
        if row.get("entry_type") == ENTRY_ADVANCE_APPLIED
        and row.get("customer_order") == "NKT-ORD-00030"
    ]

    reversed_rows = [
        row
        for row in lines
        if row.get("entry_type") == ENTRY_ADVANCE_REVERSED
        and row.get("customer_order") == "NKT-ORD-00048"
    ]

    available_rows = [
        row
        for row in lines
        if row.get("entry_type") == ENTRY_ADVANCE_BALANCE
    ]

    account_credit_00048 = _as_float(
        sum(flt(row.get("credit")) for row in applied_00048)
    )
    nonaccount_credit_00030 = _as_float(
        sum(flt(row.get("credit")) for row in applied_00030)
    )

    opening_balance = flt(data.get("opening_balance"))
    calculated_closing = _as_float(
        opening_balance
        + sum(
            flt(row.get("debit")) - flt(row.get("credit"))
            for row in lines
        )
    )
    reported_closing = _as_float(data.get("closing_balance"))

    available_advance = _as_float(
        data.get("available_customer_advance")
    )
    period_received = _as_float(
        data.get("period_advance_received")
    )
    period_applied = _as_float(
        data.get("period_advance_applied")
    )
    period_applied_to_ar = _as_float(
        data.get(
            "period_advance_applied_to_accounts_receivable"
        )
    )

    balance_line_matches_summary = any(
        f"{available_advance:,.2f}"
        in (row.get("description") or "")
        for row in available_rows
    )

    checks = {
        # Dynamic statement invariants.
        "wrapper_active": (
            data.get("available_customer_advance") is not None
        ),
        "closing_balance_matches_statement_math": (
            abs(reported_closing - calculated_closing)
            <= TOLERANCE
        ),
        "available_advance_is_nonnegative": (
            available_advance >= -TOLERANCE
        ),
        "available_advance_line_visible": bool(available_rows),
        "available_advance_line_matches_summary": (
            balance_line_matches_summary
        ),
        "period_advance_totals_are_nonnegative": (
            period_received >= -TOLERANCE
            and period_applied >= -TOLERANCE
            and period_applied_to_ar >= -TOLERANCE
        ),
        "advance_applied_to_ar_not_more_than_total_applied": (
            period_applied_to_ar
            <= period_applied + TOLERANCE
        ),

        # Frozen C5 acceptance evidence.
        "order_00048_advance_reduces_ar_by_2750": (
            abs(account_credit_00048 - 2750.0)
            <= TOLERANCE
        ),
        "order_00030_nonaccount_application_does_not_reduce_ar": (
            abs(nonaccount_credit_00030)
            <= TOLERANCE
        ),
        "reversed_history_visible": len(reversed_rows) >= 2,
        "receipt_00050_traceable": any(
            "NKT-PAY-00050" in (row.get("description") or "")
            for row in lines
        ),
        "receipt_00051_traceable": any(
            "NKT-PAY-00051" in (row.get("description") or "")
            for row in lines
        ),
        "historical_advance_received_at_least_5000": (
            period_received + TOLERANCE >= 5000.0
        ),
        "historical_advance_applied_to_ar_at_least_2750": (
            period_applied_to_ar + TOLERANCE >= 2750.0
        ),
    }

    return {
        "version": "V2.0C.5.3-FUTURE-SAFE",
        "summary": {
            "opening_balance": data.get("opening_balance"),
            "period_charges": data.get("period_charges"),
            "period_payments": data.get("period_payments"),
            "period_advance_received": data.get(
                "period_advance_received"
            ),
            "period_advance_applied": data.get(
                "period_advance_applied"
            ),
            "period_advance_applied_to_accounts_receivable": data.get(
                "period_advance_applied_to_accounts_receivable"
            ),
            "closing_balance": data.get("closing_balance"),
            "available_customer_advance": data.get(
                "available_customer_advance"
            ),
        },
        "checks": checks,
        "passed": all(checks.values()),
    }
