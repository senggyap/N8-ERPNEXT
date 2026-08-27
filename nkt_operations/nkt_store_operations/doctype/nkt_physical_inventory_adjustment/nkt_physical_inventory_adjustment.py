import frappe
from frappe.model.document import Document

from nkt_operations.nkt_store_operations.features.inventory.physical_inventory import (
    _assert_operational_authority,
    execute_adjustment,
    prepare_adjustment_document,
    validate_adjustment_document,
)


class NKTPhysicalInventoryAdjustment(Document):
    def before_validate(self):
        _assert_operational_authority()
        if self.docstatus == 0:
            prepare_adjustment_document(self)

    def validate(self):
        _assert_operational_authority()
        validate_adjustment_document(self, for_submit=False)

    def before_submit(self):
        # C8C production unlock after C8B R2 full rollback acceptance.
        # The NKT document remains the operational front door; the underlying
        # Stock Reconciliation is still created server-side under the narrow
        # Administrator posting context.
        _nkt_c15f_r4b_lock_physical_inventory_bins(self)
        validate_adjustment_document(self, for_submit=True)
        execute_adjustment(self)


# NKT_C15F_R4B_PHYSICAL_INVENTORY_BIN_SERIALIZATION
def _nkt_c15f_r4b_lock_physical_inventory_bins(doc):
    """Serialize the physical-count snapshot/post window against stock-release writers."""
    import frappe
    pairs = set()
    header_warehouse = (doc.get("warehouse") or doc.get("source_warehouse") or "").strip()
    for row in (doc.get("items") or []):
        item = (row.get("item") or row.get("item_code") or "").strip()
        warehouse = (row.get("warehouse") or row.get("source_warehouse") or header_warehouse or "").strip()
        if item and warehouse:
            pairs.add((warehouse, item))
    for warehouse, item in sorted(pairs):
        frappe.db.sql(
            "SELECT name, actual_qty FROM `tabBin` WHERE item_code=%s AND warehouse=%s FOR UPDATE",
            (item, warehouse),
            as_dict=True,
        )
