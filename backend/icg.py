"""Indian Coast Guard regional commands / districts — APPROXIMATE sea-area polygons for alert routing only.
No official public GeoJSON exists; boxes are derived from coastal lat/lon limits. Admins may overwrite geometry (e.g. official shapefiles)."""
from datetime import datetime, timezone
from typing import Optional

from db import db, audit
from models import new_id

ICG_VERSION = "icg-approx-0.1.0"
APPROX_NOTE = "approximate, for routing only — not an official boundary"
REGIONS = {
    "NW": {"name": "Coast Guard Region (North-West)", "hq": "Gandhinagar"},
    "W": {"name": "Coast Guard Region (West)", "hq": "Mumbai"},
    "E": {"name": "Coast Guard Region (East)", "hq": "Chennai"},
    "NE": {"name": "Coast Guard Region (North-East)", "hq": "Kolkata"},
    "AN": {"name": "Coast Guard Region (Andaman & Nicobar)", "hq": "Port Blair"},
}


def _box(w, s, e, n):
    return {"type": "Polygon", "coordinates": [[[w, s], [e, s], [e, n], [w, n], [w, s]]]}


# code, name, region, HQ, approximate sea box (west, south, east, north)
DISTRICTS = [
    ("ICG-OKHA", "CG District HQ Okha / Vadinar (Gulf of Kutch)", "NW", "Okha", (66.0, 22.2, 70.6, 24.5)),
    ("ICG-PBD", "CG District HQ No. 1 Porbandar (Saurashtra)", "NW", "Porbandar", (66.0, 19.8, 71.5, 22.2)),
    ("ICG-DMN", "CG District HQ Daman (South Gujarat / Gulf of Khambhat)", "NW", "Daman", (71.5, 19.8, 73.2, 22.2)),
    ("ICG-MUM", "CG District HQ No. 2 Mumbai (Maharashtra north)", "W", "Mumbai", (66.0, 17.0, 73.5, 19.8)),
    ("ICG-GOA", "CG District HQ No. 11 Goa (Ratnagiri – Karwar)", "W", "Goa", (66.0, 14.6, 74.3, 17.0)),
    ("ICG-NMG", "CG District HQ No. 3 New Mangalore (Karnataka)", "W", "New Mangalore", (66.0, 12.4, 75.0, 14.6)),
    ("ICG-KOC", "CG District HQ No. 4 Kochi (Kerala & Lakshadweep)", "W", "Kochi", (66.0, 7.0, 77.4, 12.4)),
    ("ICG-TUT", "CG District HQ No. 9 Tuticorin (Gulf of Mannar / Palk Bay)", "E", "Tuticorin", (77.4, 6.0, 80.4, 10.4)),
    ("ICG-CHN", "CG District HQ No. 5 Chennai (Tamil Nadu north & Puducherry)", "E", "Chennai", (78.5, 10.4, 86.0, 14.2)),
    ("ICG-VTZ", "CG District HQ No. 6 Visakhapatnam (Andhra Pradesh)", "E", "Visakhapatnam", (79.5, 14.2, 88.0, 18.6)),
    ("ICG-PDP", "CG District HQ No. 7 Paradip (Odisha)", "NE", "Paradip", (83.5, 18.6, 89.5, 21.2)),
    ("ICG-HLD", "CG District HQ No. 8 Haldia (West Bengal / Sundarbans)", "NE", "Haldia", (86.5, 21.2, 90.0, 23.0)),
    ("ICG-PBL", "CG District HQ No. 10 Port Blair (Andaman Islands)", "AN", "Port Blair", (90.0, 10.0, 95.5, 15.0)),
    ("ICG-CBB", "CG District HQ Campbell Bay (Nicobar Islands)", "AN", "Campbell Bay", (90.0, 5.5, 95.5, 10.0)),
]


async def seed_icg() -> int:
    await db.icg_districts.create_index([("geometry", "2dsphere")])
    await db.icg_districts.create_index("code", unique=True)
    if await db.icg_districts.count_documents({}) > 0:
        return 0
    now = datetime.now(timezone.utc)
    docs = [{"id": new_id(), "code": code, "name": name, "region_code": reg, "region": REGIONS[reg]["name"], "region_hq": REGIONS[reg]["hq"], "district_hq": hq,
             "geometry": _box(*box), "approximate": True, "note": APPROX_NOTE, "source": f"Varuna Netra seed {ICG_VERSION}", "active": True, "created_at": now, "updated_at": now}
            for code, name, reg, hq, box in DISTRICTS]
    await db.icg_districts.insert_many(docs)
    return len(docs)


def _public(d: dict) -> dict:
    return {"code": d["code"], "name": d["name"], "region_code": d["region_code"], "region": d["region"], "region_hq": d["region_hq"], "district_hq": d["district_hq"],
            "approximate": d.get("approximate", True), "note": d.get("note", APPROX_NOTE)}


async def resolve_icg(lat: float, lon: float) -> Optional[dict]:
    """Smallest active district polygon containing the point, or None (outside Indian ICG areas)."""
    rows = await db.icg_districts.find({"active": True, "geometry": {"$geoIntersects": {"$geometry": {"type": "Point", "coordinates": [lon, lat]}}}}, {"_id": 0}).to_list(20)
    if not rows:
        return None
    rows.sort(key=lambda d: d.get("area_rank", 0))
    return _public(rows[0])


async def apply_icg_to_case(case_id: str, actor: str = "system") -> Optional[dict]:
    case = await db.cases.find_one({"id": case_id}, {"_id": 0, "centroid": 1})
    if not case or not case.get("centroid"):
        return None
    lon, lat = case["centroid"]["coordinates"]
    icg = await resolve_icg(lat, lon)
    await db.cases.update_one({"id": case_id}, {"$set": {"icg": icg, "icg_resolved_at": datetime.now(timezone.utc)}})
    await audit("case", case_id, "case.icg_routed", {"district": icg["code"] if icg else None}, actor)
    return icg
