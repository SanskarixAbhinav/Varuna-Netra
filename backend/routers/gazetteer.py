from datetime import datetime, timezone
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from rapidfuzz import fuzz

from auth import get_current_user, require_role
from db import db, clean, audit
from models import new_id

router = APIRouter()
# name, type, country, operator, bbox [w,s,e,n]
SEED = [
    ("Bombay High (Mumbai High) oil field", "oil_field", "India", "ONGC", [72.1, 18.9, 72.9, 19.9]), ("Mumbai Offshore Basin", "basin", "India", "ONGC / DGH", [70.8, 17.5, 73.2, 21.5]),
    ("Krishna-Godavari (KG) Basin", "basin", "India", "ONGC / Reliance", [80.5, 15.0, 83.5, 17.5]), ("KG-D6 deepwater block", "oil_field", "India", "Reliance / bp", [81.8, 15.6, 82.9, 16.6]),
    ("Cambay Basin", "basin", "India", "ONGC / Cairn", [71.5, 21.0, 73.5, 24.0]), ("Assam-Arakan Basin", "basin", "India", "Oil India / ONGC", [91.0, 24.5, 96.0, 28.0]), ("Digboi oil field", "oil_field", "India", "Oil India", [95.5, 27.2, 95.8, 27.5]),
    ("Cauvery Basin", "basin", "India", "ONGC", [78.5, 9.5, 81.0, 12.0]), ("Ratnagiri / Kerala-Konkan Basin", "basin", "India", "DGH", [70.0, 12.0, 74.5, 17.5]), ("Mahanadi Basin", "basin", "India", "ONGC", [85.0, 18.5, 88.0, 21.0]),
    ("Barmer Basin (Mangala)", "oil_field", "India", "Cairn / Vedanta", [71.0, 25.5, 72.0, 26.5]), ("Andaman offshore", "basin", "India", "DGH", [91.0, 6.0, 95.0, 14.0]),
    ("Ekofisk", "oil_field", "Norway", "ConocoPhillips", [3.0, 56.3, 3.5, 56.7]), ("Troll field", "oil_field", "Norway", "Equinor", [3.4, 60.5, 3.9, 60.9]), ("Johan Sverdrup", "oil_field", "Norway", "Equinor", [2.5, 58.7, 2.9, 59.0]),
    ("Forties field", "oil_field", "United Kingdom", "Apache", [0.8, 57.6, 1.2, 57.8]), ("Brent field", "oil_field", "United Kingdom", "Shell", [1.5, 60.9, 1.9, 61.2]), ("Buzzard field", "oil_field", "United Kingdom", "CNOOC", [-0.2, 57.8, 0.2, 58.0]),
    ("Groningen / Dutch offshore (K-L blocks)", "basin", "Netherlands", "NAM / Wintershall", [3.0, 52.8, 5.5, 54.2]), ("Danish North Sea (Halfdan/Dan)", "oil_field", "Denmark", "TotalEnergies", [4.4, 55.3, 5.1, 55.8]),
    ("Mittelplate", "oil_field", "Germany", "Wintershall Dea", [8.6, 54.0, 8.9, 54.2]),
    ("Gulf of Mexico deepwater (Mississippi Canyon)", "basin", "United States", "various", [-91.0, 27.0, -87.5, 29.5]), ("Thunder Horse", "oil_field", "United States", "bp", [-88.6, 28.0, -88.3, 28.3]), ("Bay of Campeche (Cantarell / Ku-Maloob-Zaap)", "oil_field", "Mexico", "Pemex", [-92.6, 18.8, -91.6, 19.9]),
    ("Safaniya", "oil_field", "Saudi Arabia", "Saudi Aramco", [48.5, 27.7, 49.3, 28.3]), ("Upper Zakum", "oil_field", "UAE", "ADNOC", [53.4, 24.7, 53.9, 25.1]), ("Ras Tanura terminal", "terminal", "Saudi Arabia", "Saudi Aramco", [50.1, 26.6, 50.3, 26.8]), ("Kharg Island terminal", "terminal", "Iran", "NIOC", [50.2, 29.1, 50.4, 29.3]),
    ("Bonga (Nigeria deepwater)", "oil_field", "Nigeria", "Shell", [4.4, 4.4, 4.8, 4.8]), ("Niger Delta offshore", "basin", "Nigeria", "various", [3.5, 3.5, 8.5, 5.5]), ("Jubilee field", "oil_field", "Ghana", "Tullow", [-3.1, 4.4, -2.7, 4.7]), ("Girassol / Block 17", "oil_field", "Angola", "TotalEnergies", [11.3, -8.0, 12.0, -7.4]),
    ("Tapis / Malay Basin", "oil_field", "Malaysia", "Petronas / ExxonMobil", [103.5, 5.0, 105.5, 7.0]), ("Natuna Sea", "basin", "Indonesia", "Medco / Premier", [105.0, 2.0, 109.0, 6.0]), ("Cuu Long Basin (Bach Ho)", "oil_field", "Vietnam", "Vietsovpetro", [107.5, 9.5, 109.0, 10.5]), ("Bohai Bay", "basin", "China", "CNOOC", [117.5, 37.5, 121.0, 40.5]),
    ("Campos Basin", "basin", "Brazil", "Petrobras", [-41.5, -23.5, -39.0, -21.0]), ("Santos Basin pre-salt (Lula/Tupi)", "oil_field", "Brazil", "Petrobras", [-44.5, -26.5, -41.0, -24.0]), ("Stabroek block", "oil_field", "Guyana", "ExxonMobil", [-58.0, 7.3, -56.0, 8.6]),
    ("Sakhalin-II", "oil_field", "Russia", "Sakhalin Energy", [143.0, 51.0, 144.5, 53.5]), ("Kashagan", "oil_field", "Kazakhstan", "NCOC", [50.5, 45.8, 51.5, 46.6]), ("Prirazlomnoye", "oil_field", "Russia", "Gazprom Neft", [56.5, 69.0, 57.5, 69.5]),
    ("Hibernia", "oil_field", "Canada", "ExxonMobil", [-49.3, 46.6, -48.6, 46.9]), ("Bass Strait", "basin", "Australia", "Esso / Woodside", [146.5, -39.5, 149.0, -38.0]), ("North West Shelf", "basin", "Australia", "Woodside", [114.0, -21.0, 118.5, -18.5]),
    ("Strait of Malacca", "shipping_lane", "Malaysia / Indonesia / Singapore", None, [98.0, 1.0, 104.0, 6.0]), ("Strait of Hormuz", "shipping_lane", "Iran / Oman", None, [55.5, 25.5, 57.5, 27.0]), ("Suez Canal / Gulf of Suez", "shipping_lane", "Egypt", None, [32.2, 27.5, 34.0, 31.3]), ("English Channel / Dover Strait", "shipping_lane", "UK / France", None, [-2.0, 49.5, 2.0, 51.5]), ("Bab-el-Mandeb", "shipping_lane", "Yemen / Djibouti", None, [42.5, 12.0, 44.0, 13.5]),
    ("Gulf of Kutch (Kandla / Mundra / Vadinar)", "terminal", "India", "IOCL / Adani", [68.5, 22.3, 70.5, 23.3]), ("Jamnagar refinery & SPM (Sikka)", "terminal", "India", "Reliance", [69.5, 22.2, 70.2, 22.6]), ("Paradip port & refinery", "terminal", "India", "IOCL", [86.5, 20.0, 87.0, 20.5]), ("Chennai / Ennore ports", "terminal", "India", "Kamarajar Port", [80.1, 12.9, 80.5, 13.4]), ("Visakhapatnam port", "terminal", "India", "VPT / HPCL", [83.1, 17.5, 83.5, 17.9]), ("Mumbai / JNPT port complex", "terminal", "India", "MbPT / JNPA", [72.7, 18.8, 73.1, 19.1]),
    ("Rotterdam / Maasvlakte", "terminal", "Netherlands", "Port of Rotterdam", [3.9, 51.85, 4.6, 52.05]), ("Singapore Strait", "shipping_lane", "Singapore", None, [103.4, 1.0, 104.5, 1.5]), ("Houston Ship Channel / Galveston", "terminal", "United States", "Port Houston", [-95.3, 29.2, -94.6, 29.8]), ("Fujairah anchorage", "terminal", "UAE", "Port of Fujairah", [56.2, 25.0, 56.7, 25.5]),
    ("India", "country", "India", None, [68.0, 6.5, 97.5, 35.5]), ("Norway", "country", "Norway", None, [4.0, 57.5, 31.5, 71.5]), ("United Kingdom", "country", "United Kingdom", None, [-8.5, 49.8, 2.0, 61.0]), ("Netherlands", "country", "Netherlands", None, [3.3, 50.7, 7.3, 53.7]), ("Nigeria", "country", "Nigeria", None, [2.5, 4.0, 14.7, 14.0]), ("Brazil", "country", "Brazil", None, [-74.0, -34.0, -34.5, 5.5]), ("Saudi Arabia", "country", "Saudi Arabia", None, [34.5, 16.0, 55.7, 32.2]), ("Malaysia", "country", "Malaysia", None, [99.5, 0.8, 119.5, 7.5]), ("United States (Gulf Coast)", "country", "United States", None, [-98.0, 24.0, -80.0, 31.0]), ("Sri Lanka", "country", "Sri Lanka", None, [79.5, 5.8, 82.0, 10.0]), ("Bangladesh", "country", "Bangladesh", None, [88.0, 20.5, 92.7, 26.7]),
]


def _doc(name, typ, country, operator, bbox, actor="seed", **extra):
    w, s, e, n = bbox
    return {"id": new_id(), "name": name, "type": typ, "country": country, "operator": operator, "bbox": bbox, "center": [round((w + e) / 2, 4), round((s + n) / 2, 4)],
            "geometry": {"type": "Polygon", "coordinates": [[[w, s], [e, s], [e, n], [w, n], [w, s]]]}, "source": "seed" if actor == "seed" else "admin", "created_by": actor, "created_at": datetime.now(timezone.utc), **extra}


async def seed_gazetteer():
    if await db.gazetteer.count_documents({"source": "seed"}):
        return
    await db.gazetteer.insert_many([_doc(*row) for row in SEED])
    await db.gazetteer.create_index("name")


async def _entries():
    rows = await db.gazetteer.find({}, {"_id": 0}).to_list(5000)
    for z in await db.jurisdictions.find({"active": True}, {"_id": 0, "code": 1, "name": 1, "authority": 1, "bbox": 1, "provenance": 1, "official": 1}).to_list(2000):
        bb = z.get("bbox")
        if not bb:
            continue
        rows.append({"id": f"zone:{z['code']}", "name": f"{z['name']} ({z['code']})", "type": "eez", "country": z.get("authority"), "operator": None, "bbox": bb, "center": [(bb[0] + bb[2]) / 2, (bb[1] + bb[3]) / 2], "geometry": None, "zone_code": z["code"], "source": (z.get("provenance") or ("reference" if z.get("official") else "zones")).lower()})
    return rows


@router.get("/gazetteer/search")
async def search(q: str = Query("", max_length=80), limit: int = Query(8, ge=1, le=30), type: Optional[str] = None, user=Depends(get_current_user)):
    rows = await _entries()
    if type:
        rows = [r for r in rows if r["type"] == type]
    if not q.strip():
        return clean(rows[:limit])
    ql = q.strip().lower()
    scored = []
    for i, r in enumerate(rows):
        name = r["name"].lower()
        s = max(fuzz.WRatio(ql, name), 0.85 * fuzz.WRatio(ql, f"{r.get('country') or ''} {r.get('operator') or ''} {r['type']}".lower()))
        if ql in name:
            s = max(s, 90 + (10 if name.startswith(ql) else 0))
        toks = [t for t in ql.split() if len(t) >= 2]
        if toks and all(t in name for t in toks):
            s = max(s, 95)
        if s >= 55:
            scored.append((s, i))
    scored.sort(key=lambda x: (-x[0], rows[x[1]]["name"]))
    return clean([{**rows[i], "match": round(s, 1)} for s, i in scored[:limit]])


@router.get("/gazetteer/types")
async def types(user=Depends(get_current_user)):
    return {"types": ["oil_field", "basin", "terminal", "shipping_lane", "country", "eez"], "count": await db.gazetteer.count_documents({})}


class AssetIn(BaseModel):
    name: str = Field(min_length=2, max_length=120)
    type: str
    country: Optional[str] = None
    operator: Optional[str] = None
    bbox: List[float] = Field(min_length=4, max_length=4)
    notes: Optional[str] = None


@router.post("/gazetteer", status_code=201)
async def create_asset(body: AssetIn, user=Depends(require_role("admin"))):
    if body.type not in ("oil_field", "basin", "terminal", "shipping_lane", "country", "other"):
        raise HTTPException(400, "invalid type")
    w, s, e, n = body.bbox
    if not (-180 <= w < e <= 180 and -90 <= s < n <= 90):
        raise HTTPException(400, "bbox must be [west, south, east, north]")
    doc = _doc(body.name, body.type, body.country, body.operator, body.bbox, user["email"], notes=body.notes)
    await db.gazetteer.insert_one(dict(doc))
    await audit("gazetteer", doc["id"], "gazetteer.created", {"name": body.name, "type": body.type}, user["email"])
    return clean(doc)


@router.delete("/gazetteer/{asset_id}")
async def delete_asset(asset_id: str, user=Depends(require_role("admin"))):
    if not (await db.gazetteer.delete_one({"id": asset_id, "source": {"$ne": "seed"}})).deleted_count:
        raise HTTPException(404, "asset not found or is a protected seed entry")
    return {"ok": True}
