"""Charge groups: how month-end close thinks about a shipment's freight costs.

A shipment is billed in parts, often by different vendors: the ocean freight by the forwarder, the
destination port and customs charges by the forwarder or a customs broker, the delivery by a trucker.
Accruals ask, per shipment, which of these parts has been invoiced. Every invoice line is named with the
charge codes of apps/rates/charges.py, and each code belongs to one group below.
"""
from __future__ import annotations

FREIGHT, DESTINATION, DELIVERY, OTHER, GOODS = "freight", "destination", "delivery", "other", "goods"

GROUPS = {
    FREIGHT: "Ocean freight and surcharges",
    DESTINATION: "Destination port and customs",
    DELIVERY: "Delivery (trucking)",
    OTHER: "Other charges",
    GOODS: "Goods (supplier invoice)",
}
# Groups a shipment can be expected to carry, in the order they are billed.
EXPECTABLE = [FREIGHT, DESTINATION, DELIVERY]

CODE_GROUP = {
    # paid to the carrier or forwarder for the sea leg
    "ocean_freight": FREIGHT, "baf": FREIGHT, "caf": FREIGHT, "thc_origin": FREIGHT, "documentation": FREIGHT,
    "bl_fee": FREIGHT, "security_filing": FREIGHT, "insurance": FREIGHT, "congestion": FREIGHT, "hazmat": FREIGHT,
    # at the port of discharge
    "thc_destination": DESTINATION, "customs_clearance": DESTINATION, "exam": DESTINATION, "demurrage": DESTINATION,
    "storage": DESTINATION,
    # getting the box to the warehouse
    "trucking": DELIVERY, "chassis": DELIVERY, "fuel_surcharge": DELIVERY, "detention": DELIVERY,
    "per_diem": DELIVERY, "waiting_time": DELIVERY, "redelivery": DELIVERY, "pre_pull": DELIVERY,
    "chassis_split": DELIVERY, "overweight": DELIVERY,
    # anything else
    "admin_fee": OTHER, "other": OTHER,
}


def group_for(code: str) -> str:
    return CODE_GROUP.get(code or "", OTHER)


def label(group: str) -> str:
    return GROUPS.get(group, (group or "").replace("_", " ").capitalize())


def codes_in(group: str) -> set[str]:
    return {code for code, g in CODE_GROUP.items() if g == group}
