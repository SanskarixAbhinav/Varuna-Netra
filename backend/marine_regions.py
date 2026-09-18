"""Marine Regions Maritime Boundaries (Flanders Marine Institute, geo.vliz.be WFS) — worldwide EEZ / 24 NM / 12 NM import.
Geometries are REFERENCE data (CC-BY 4.0), simplified for map performance; never labelled legally authoritative."""
import asyncio
from datetime import datetime, timezone

import httpx
from shapely.geometry import shape, mapping, MultiPolygon, Polygon
from shapely.geometry.polygon import orient
from shapely.ops import unary_union
from shapely.validation import make_valid

from db import db, audit
from jobs import handler, job_log
from jurisdiction import apply_to_case
from models import new_id

WFS = "https://geo.vliz.be/geoserver/MarineRegions/wfs"
DATASET = {"name": "Marine Regions Maritime Boundaries Geodatabase", "version": "v12 (2023)", "publisher": "Flanders Marine Institute (VLIZ)",
           "license": "CC-BY 4.0", "url": "https://www.marineregions.org/", "citation": "Flanders Marine Institute (2023). Maritime Boundaries Geodatabase, version 12. https://doi.org/10.14284/632",
           "authority_status": "REFERENCE", "note": "Reference boundaries for investigation context — not a legal determination of jurisdiction."}
AUTHORITIES = {
    "IND": "Indian Coast Guard (MRCC) / Directorate General of Shipping", "NLD": "Rijkswaterstaat / Netherlands Coastguard", "GBR": "UK Maritime & Coastguard Agency", "DEU": "Havariekommando (CCME)",
    "DNK": "Danish Defence – Maritime Assistance Service", "BEL": "Belgian FPS Mobility / MUMM", "NOR": "Norwegian Coastal Administration",
    "FRA": "Préfecture maritime / CROSS", "SWE": "Swedish Coast Guard", "IRL": "Irish Coast Guard", "ESP": "Salvamento Marítimo", "PRT": "Portuguese Navy / DGRM", "ITA": "Guardia Costiera",
    "USA": "US Coast Guard (National Response Center)", "JPN": "Japan Coast Guard", "SGP": "Maritime and Port Authority of Singapore", "MYS": "Malaysian Maritime Enforcement Agency",
    "IDN": "BAKAMLA / Indonesian Ministry of Transport", "ARE": "UAE Federal Transport Authority", "SAU": "Saudi Border Guard", "IRN": "Ports and Maritime Organization of Iran",
    "QAT": "Qatar Ministry of Transport", "KWT": "Kuwait Coast Guard", "OMN": "Royal Oman Police Coast Guard", "MEX": "SEMAR (Mexican Navy)", "AUS": "Australian Maritime Safety Authority", "CAN": "Canadian Coast Guard", "CHN": "China Maritime Safety Administration", "KOR": "Korea Coast Guard",
}
DEFAULT_ISO3 = ["NLD", "GBR", "DEU", "DNK", "BEL", "NOR"]
LAYERS = {"eez": {"typeName": "MarineRegions:eez", "pol": "200NM", "suffix": "EEZ", "zone_type": "eez", "label": "Exclusive Economic Zone (200 NM)"},
          "eez_24nm": {"typeName": "MarineRegions:eez_24nm", "pol": "24NM", "suffix": "CZ", "zone_type": "contiguous", "label": "Contiguous Zone (24 NM)"},
          "eez_12nm": {"typeName": "MarineRegions:eez_12nm", "pol": "12NM", "suffix": "TS", "zone_type": "territorial", "label": "Territorial Sea (12 NM)"}}
PROPS = "mrgid,geoname,pol_type,iso_ter1,territory1,sovereign1,iso_sov1,area_km2"
DETAIL_TOL, LOW_TOL = 0.004, 0.05


async def _wfs(params: dict) -> dict:
    async with httpx.AsyncClient(timeout=180) as c:
        r = await c.get(WFS, params={"service": "WFS", "version": "1.0.0", "request": "GetFeature", "outputFormat": "application/json", **params})
    r.raise_for_status()
    return r.json()


async def list_features(layer: str) -> list[dict]:
    """Attribute-only catalogue (no geometry) of every feature in a layer — ~70 KB for the EEZ layer."""
    data = await _wfs({"typeName": LAYERS[layer]["typeName"], "propertyName": PROPS})
    return [f["properties"] for f in data.get("features", [])]


def _clean_geometry(feats: list[dict], layer: str) -> tuple[MultiPolygon, MultiPolygon, int]:
    # Douglas-Peucker first (fast even for 200k-vertex EEZs such as Canada/USA), then validity repair — preserve_topology on raw geometry takes minutes.
    geoms = [make_valid(shape(f["geometry"]).simplify(DETAIL_TOL, preserve_topology=False)) for f in feats if f.get("geometry")]
    merged = unary_union(geoms) if len(geoms) > 1 else geoms[0]
    if merged.geom_type == "GeometryCollection":
        merged = unary_union([g for g in merged.geoms if g.geom_type in ("Polygon", "MultiPolygon")])
    merged = merged.buffer(0)  # dissolve self-touching parts (2dsphere rejects crossing loops)
    parts = list(merged.geoms) if merged.geom_type == "MultiPolygon" else [merged]
    if layer == "eez":
        merged = MultiPolygon([Polygon(p.exterior) for p in parts if p.area > 1e-6]).buffer(0)  # island holes are land: irrelevant for spill jurisdiction; buffer(0) dissolves nested shells
    else:
        merged = MultiPolygon([p for p in parts if p.area > 1e-8])  # 12/24 NM bands keep their inner ring
    if merged.geom_type == "Polygon":
        merged = MultiPolygon([merged])
    if merged.is_empty:
        raise ValueError("geometry empty after cleaning")
    merged = orient(merged, sign=1.0)
    low = make_valid(merged.simplify(LOW_TOL, preserve_topology=False)).buffer(0)
    low = low if low.geom_type == "MultiPolygon" else MultiPolygon([low]) if low.geom_type == "Polygon" else merged
    vertices = sum(len(ring.coords) for poly in merged.geoms for ring in [poly.exterior, *poly.interiors])
    return merged, orient(low, sign=1.0), vertices


async def fetch_feature(layer: str, mrgid: int | None = None, iso3: str | None = None) -> dict | None:
    L = LAYERS[layer]
    cql = f"mrgid={int(mrgid)}" if mrgid else f"iso_ter1='{iso3}'"
    data = await _wfs({"typeName": L["typeName"], "CQL_FILTER": cql})
    feats = data.get("features", [])
    if not mrgid:
        main = [f for f in feats if (f["properties"].get("pol_type") or "").upper() == L["pol"]]
        feats = main or feats
    if not feats:
        return None
    merged, low, vertices = await asyncio.to_thread(_clean_geometry, feats, layer)  # keep the event loop (health probes, AIS) responsive
    p = feats[0]["properties"]
    return {"geometry": mapping(merged), "geometry_low": mapping(low), "bbox": list(merged.bounds), "geoname": p.get("geoname"), "mrgid": p.get("mrgid"), "territory": p.get("territory1"),
            "sovereign": p.get("sovereign1"), "iso_ter1": p.get("iso_ter1"), "iso_sov1": p.get("iso_sov1"), "pol_type": p.get("pol_type"), "area_km2": p.get("area_km2"), "features": len(feats), "vertices": vertices}


async def fetch_eez(iso3: str, layer: str = "eez") -> dict | None:
    return await fetch_feature(layer, iso3=iso3)


def _zone_doc(code: str, L: dict, rec: dict, now: datetime) -> dict:
    iso = rec.get("iso_ter1") or rec.get("iso_sov1") or "XXX"
    pol = (rec.get("pol_type") or L["pol"]).upper()
    special = pol not in ("200NM", "24NM", "12NM")
    return {"code": code, "name": rec["geoname"] or f"{iso} {L['label']}", "authority": AUTHORITIES.get(iso, f"{rec.get('sovereign') or iso} maritime authority (reference — verify)"),
            "authority_verified": iso in AUTHORITIES, "country": iso, "country_code": iso, "country_name": rec.get("sovereign"), "territory": rec.get("territory"), "sovereign_iso": rec.get("iso_sov1"),
            "zone_type": L["zone_type"], "zone_label": L["label"], "geometry": rec["geometry"], "geometry_low": rec["geometry_low"], "bbox": rec["bbox"], "active": True,
            "provenance": "REFERENCE", "authority_status": "REFERENCE — overlapping claim / joint regime" if special else "REFERENCE", "official": False,
            "source": f"{DATASET['name']} {DATASET['version']} — {L['label']}, geo.vliz.be WFS, simplified {DETAIL_TOL}°", "dataset": DATASET,
            "mrgid": rec["mrgid"], "pol_type": rec["pol_type"], "area_km2": rec["area_km2"], "vertices": rec["vertices"], "imported_at": now, "updated_at": now}


async def _store(job_id, doc, failed) -> str | None:
    """Upsert by code; on 2dsphere rejection retry once with a repaired geometry."""
    existing = await db.jurisdictions.find_one({"code": doc["code"]}, {"_id": 0, "id": 1})
    for attempt in (0, 1):
        try:
            if existing:
                await db.jurisdictions.update_one({"code": doc["code"]}, {"$set": doc})
                return existing["id"]
            zid = new_id()
            await db.jurisdictions.insert_one({**doc, "id": zid, "created_at": doc["imported_at"]})
            return zid
        except Exception as e:  # noqa: BLE001
            if attempt == 1:
                failed.append({"code": doc["code"], "error": f"geometry not indexable: {str(e)[:120]}"})
                await job_log(job_id, f"{doc['code']}: skipped — {str(e)[:100]}", "error")
                return None
            await job_log(job_id, f"{doc['code']}: geometry rejected by 2dsphere, retrying with repaired orientation", "warn")
            g = orient(make_valid(shape(doc["geometry"]).buffer(0.002).buffer(0)), sign=1.0)  # ~200 m outward buffer dissolves crossing edges left by simplification
            doc["geometry"] = mapping(g if g.geom_type == "MultiPolygon" else MultiPolygon([g]))


def _code_for(iso: str, L: dict, rec: dict, primary_per_iso: dict) -> str:
    """Deterministic: the largest main-type polygon per (country, layer) is `ISO-EEZ`; every other feature carries its MRGID."""
    if primary_per_iso.get((iso, L["suffix"])) == rec["mrgid"]:
        return f"{iso}-{L['suffix']}"
    return f"{iso}-{L['suffix']}-{rec['mrgid']}"


def _primary_map(catalogue: list[dict], L: dict) -> dict:
    best = {}
    for p in catalogue:
        if (p.get("pol_type") or "").upper() != L["pol"]:
            continue
        iso = p.get("iso_ter1") or p.get("iso_sov1") or "XXX"
        if (iso, L["suffix"]) not in best or (p.get("area_km2") or 0) > best[(iso, L["suffix"])][1]:
            best[(iso, L["suffix"])] = (p["mrgid"], p.get("area_km2") or 0)
    return {k: v[0] for k, v in best.items()}


async def _normalize_codes(job_id, catalogue: list[dict], L: dict):
    """Self-heal codes of already-stored zones so `ISO-EEZ` always points at the largest main polygon."""
    prim = _primary_map(catalogue, L)
    fixed = 0
    async for z in db.jurisdictions.find({"zone_type": L["zone_type"], "mrgid": {"$ne": None}, "dataset.version": DATASET["version"]}, {"_id": 0, "id": 1, "code": 1, "mrgid": 1, "country": 1}):
        want = f"{z['country']}-{L['suffix']}" if prim.get((z["country"], L["suffix"])) == z["mrgid"] else f"{z['country']}-{L['suffix']}-{z['mrgid']}"
        if want != z["code"]:
            clash = await db.jurisdictions.find_one({"code": want, "id": {"$ne": z["id"]}}, {"_id": 0, "id": 1, "mrgid": 1})
            if clash:
                await db.jurisdictions.update_one({"id": clash["id"]}, {"$set": {"code": f"{z['country']}-{L['suffix']}-{clash.get('mrgid') or 'legacy'}"}})
            await db.jurisdictions.update_one({"id": z["id"]}, {"$set": {"code": want}})
            fixed += 1
    if fixed:
        await job_log(job_id, f"{L['label']}: normalised {fixed} zone code(s) (largest main polygon = ISO-{L['suffix']})")


@handler("import_eez")
async def handle_import_eez(job):
    """payload: iso3 list (or ["ALL"] for the whole world), layers subset, replace_demo."""
    payload = job["payload"]
    iso_list = [s.strip().upper() for s in payload.get("iso3", DEFAULT_ISO3) if s.strip()]
    layers = [l for l in payload.get("layers") or ["eez"] if l in LAYERS]
    world = "ALL" in iso_list or "GLOBAL" in iso_list
    actor = job.get("actor", "system")
    now = datetime.now(timezone.utc)
    imported, failed = [], []
    primary_per_iso = {}
    for layer in layers:
        L = LAYERS[layer]
        try:
            catalogue = await list_features(layer)
        except Exception as e:  # noqa: BLE001
            failed.append({"layer": layer, "error": f"catalogue fetch failed: {str(e)[:160]}"})
            await job_log(job["id"], f"{layer}: catalogue fetch failed — {e}", "error")
            continue
        targets = [p for p in catalogue if world or (p.get("iso_ter1") or p.get("iso_sov1")) in iso_list]
        primary_per_iso.update(_primary_map(catalogue, L))
        await job_log(job["id"], f"{L['label']}: {len(targets)} feature(s) to import from {DATASET['name']} {DATASET['version']}")
        for i, p in enumerate(targets):
            iso = p.get("iso_ter1") or p.get("iso_sov1") or "XXX"
            if world and await db.jurisdictions.find_one({"mrgid": p["mrgid"], "zone_type": L["zone_type"], "dataset.version": DATASET["version"]}, {"_id": 1}):
                imported.append({"iso3": iso, "layer": layer, "mrgid": p["mrgid"], "skipped": "already imported"})
                continue
            try:
                rec = await fetch_feature(layer, mrgid=p["mrgid"])
            except Exception as e:  # noqa: BLE001
                failed.append({"mrgid": p["mrgid"], "layer": layer, "error": str(e)[:200]})
                await job_log(job["id"], f"{iso}/{p['mrgid']}: fetch failed — {str(e)[:120]}", "error")
                continue
            if not rec:
                failed.append({"mrgid": p["mrgid"], "layer": layer, "error": "no feature returned"})
                continue
            doc = _zone_doc(_code_for(iso, L, rec, primary_per_iso), L, rec, now)
            zid = await _store(job["id"], doc, failed)
            if not zid:
                continue
            imported.append({"iso3": iso, "layer": layer, "code": doc["code"], "name": doc["name"], "zone_type": L["zone_type"], "mrgid": rec["mrgid"], "vertices": rec["vertices"], "area_km2": rec["area_km2"]})
            if i % 10 == 0 or not world:
                await job_log(job["id"], f"[{i + 1}/{len(targets)}] {doc['code']}: {doc['name']} ({rec['vertices']} vertices, {rec['area_km2']} km²)")
            await asyncio.sleep(0.2)
        await _normalize_codes(job["id"], catalogue, L)
    if imported:
        await audit("jurisdiction", "import", "jurisdiction.imported", {"count": len(imported), "failed": len(failed), "dataset": DATASET["version"]}, actor)
        if payload.get("replace_demo", True):
            res = await db.jurisdictions.update_many({"source": {"$regex": "^demo-seed"}, "zone_type": "eez"}, {"$set": {"active": False, "updated_at": now}})
            await job_log(job["id"], f"deactivated {res.modified_count} demo EEZ polygons")
    await db.settings.update_one({"key": "jurisdiction_dataset"}, {"$set": {"key": "jurisdiction_dataset", **DATASET, "last_import_at": now, "last_import_count": len(imported), "last_import_failed": len(failed), "world": world}}, upsert=True)
    ids = [c["id"] async for c in db.cases.find({}, {"id": 1})]
    for cid in ids:
        try:
            await apply_to_case(cid, actor)
        except Exception:  # noqa: BLE001
            pass
    await job_log(job["id"], f"imported {len(imported)}, failed {len(failed)}; re-resolved jurisdiction for {len(ids)} cases")
    return {"imported": imported, "failed": failed, "cases_resolved": len(ids), "dataset": DATASET}
