from datetime import datetime, timezone
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from rapidfuzz import fuzz

from auth import get_current_user, require_role
from db import db, clean, audit, to_utc
from geo import haversine_km
from models import new_id

router = APIRouter()
# name, date, lat, lon, volume_t, oil_type, cause, vessel_facility, ecosystems, remediation, lessons, country
SEED = [
    ("Deepwater Horizon (Macondo)", "2010-04-20", 28.74, -88.37, 600000, "light crude", "Well blowout after cement/BOP failure on Macondo prospect", "Deepwater Horizon rig (Transocean / BP)", ["Gulf of Mexico marshes", "deepwater corals", "pelagic fisheries", "Louisiana beaches"], ["subsea dispersant injection (Corexit)", "in-situ burning", "skimming fleet", "shoreline booms", "relief wells"], "Subsea dispersant use was controversial; source control took 87 days; long-term deepwater ecosystem monitoring essential.", "United States"),
    ("Exxon Valdez", "1989-03-24", 60.84, -146.87, 37000, "Alaska North Slope crude", "Tanker grounding on Bligh Reef (navigation/fatigue)", "MT Exxon Valdez", ["Prince William Sound shorelines", "sea otters", "herring & salmon fisheries", "seabirds"], ["hot-water high-pressure washing", "mechanical skimmers", "bioremediation (Inipol fertiliser)", "manual shoreline cleanup"], "Aggressive hot-water washing damaged intertidal life; led to OPA-90 and double-hull mandates.", "United States"),
    ("Prestige", "2002-11-13", 42.25, -12.05, 63000, "heavy fuel oil (M-100)", "Hull failure in storm; refused port of refuge; broke in two", "MT Prestige", ["Galician coast (Costa da Morte)", "shellfish beds", "seabirds"], ["shoreline manual cleanup", "offshore skimming", "wreck oil pumping by submersible"], "Denying a place of refuge worsened the spill; heavy fuel oil persistence demands prolonged shoreline work.", "Spain"),
    ("Erika", "1999-12-12", 47.2, -4.4, 20000, "heavy fuel oil", "Structural failure and break-up in heavy weather", "MT Erika", ["Brittany coast", "seabirds (>60,000)", "oyster farms"], ["shoreline cleanup", "wreck pumping", "booms at estuaries"], "Triggered EU Erika I/II/III packages: port state control, phase-out of single hulls, EMSA.", "France"),
    ("Sanchi", "2018-01-06", 30.85, 124.95, 111000, "condensate (light)", "Collision with bulk carrier CF Crystal, fire, sinking", "MT Sanchi", ["East China Sea fisheries", "Ryukyu coast"], ["monitoring of condensate evaporation", "limited skimming (condensate mostly burned/evaporated)", "fisheries closure"], "Condensate behaves unlike crude — rapid evaporation & toxicity; drift modelling guided fisheries advisories.", "China / Japan"),
    ("MV Wakashio", "2020-07-25", -20.44, 57.75, 1000, "VLSFO", "Grounding on reef at Pointe d'Esny (deviation for phone signal)", "MV Wakashio (bulk carrier)", ["Blue Bay Marine Park", "mangroves", "coral reef", "Île aux Aigrettes"], ["improvised sugar-cane/hair booms by volunteers", "skimming", "hull oil pumping", "mangrove cleanup"], "VLSFO (new low-sulphur fuel) weathering poorly understood; community response critical in small island states.", "Mauritius"),
    ("MT Dawn Kanchipuram – Chennai (Ennore) collision", "2017-01-28", 13.27, 80.35, 200, "heavy fuel oil", "Collision of MT Dawn Kanchipuram with MT BW Maple off Kamarajar Port, Ennore", "MT Dawn Kanchipuram / MT BW Maple", ["Ennore–Marina beaches (~34 km)", "olive ridley turtles", "artisanal fishing"], ["manual bucket & sludge removal by volunteers", "super-sucker trucks", "limited skimmers", "Coast Guard boom deployment"], "Initial under-reporting delayed response; need for pre-positioned Tier-1 equipment at Indian ports and rapid volume estimation.", "India"),
    ("Ennore / Kamarajar Port spill (Cyclone Michaung)", "2023-12-04", 13.23, 80.32, 100, "crude & product residue", "Flooding of CPCL refinery area during Cyclone Michaung carried oil into Buckingham Canal & Ennore creek", "CPCL Manali refinery", ["Ennore creek mangroves", "Kosasthalaiyar estuary", "fishing hamlets"], ["booms in creek", "manual removal", "bioremediation trials", "compensation to fishers"], "Climate-driven flooding is an emerging spill pathway; refinery drainage design matters.", "India"),
    ("Hebei Spirit", "2007-12-07", 36.9, 126.0, 10900, "crude (mixed)", "Drifting crane barge (Samsung No.1) struck anchored tanker", "MT Hebei Spirit", ["Taean coast", "aquaculture", "tidal flats"], ["massive volunteer shoreline cleanup (1.2 M people)", "skimming", "booms"], "Anchored VLCCs near tow routes are vulnerable; volunteer coordination and health protection needed.", "South Korea"),
    ("Amoco Cadiz", "1978-03-16", 48.6, -4.77, 223000, "light crude", "Steering gear failure, grounding at Portsall", "MT Amoco Cadiz", ["Brittany coast (360 km)", "oyster beds", "salt marshes"], ["shoreline scraping", "detergents (later restricted)", "manual removal"], "Detergent use harmed recovery; established case law on liability and led to French POLMAR plan.", "France"),
    ("Torrey Canyon", "1967-03-18", 49.95, -6.4, 119000, "Kuwait crude", "Grounding on Seven Stones reef (navigation)", "SS Torrey Canyon", ["Cornwall & Brittany coasts", "seabirds"], ["first-generation detergents (highly toxic)", "bombing of wreck to burn oil"], "Founding disaster of modern spill response; toxic detergents did more harm than oil; led to CLC 1969 / OPRC.", "United Kingdom"),
    ("X-Press Pearl", "2021-05-20", 7.05, 79.75, 350, "bunker fuel + plastic nurdles + chemicals", "Container fire (nitric acid leak) and sinking off Colombo", "MV X-Press Pearl (container ship)", ["Negombo lagoon", "west Sri Lanka beaches", "fisheries", "turtles & dolphins"], ["nurdle beach cleanup", "booms at lagoon mouths", "fisheries ban", "wreck monitoring"], "Hazardous cargo + plastic pollution compounded oil impacts; multi-hazard response planning needed for container ships.", "Sri Lanka"),
    ("MSC ELSA 3 — Kochi oil slick", "2025-05-25", 9.3125, 76.136, 451.5, "furnace oil + marine diesel (bunker fuel)", "Container ship developed a 26° starboard list en route Vizhinjam→Kochi and capsized/sank ~38 nm SW of Kochi; bunker fuel released and some hazardous containers (calcium carbide) drifted ashore", "MSC ELSA 3 (IMO 9123221, container ship)", ["Kerala coast (Alappuzha, Kollam, Thiruvananthapuram)", "Arabian Sea fisheries", "backwaters/estuaries", "beaches"], ["Indian Coast Guard aerial oil-slick mapping (Dornier)", "offshore dispersant spraying", "shoreline container & debris recovery", "wreck bunker-oil removal by hot-tapping (completed Oct 2025)"], "Sunken-wreck bunker fuel (~451 t onboard: 367.1 t furnace oil + 84.44 t diesel) is a prolonged pollution risk; hazardous container cargo (calcium carbide reacts with water to release acetylene) compounds the oil hazard; hot-tapping later removed the wreck's oil to eliminate the residual risk. Exact spilled volume was not precisely quantified — figure shown is bunker fuel at risk.", "India"),
    ("MV Wan Hai 503 fire off Kerala (Beypore)", "2025-06-09", 10.7, 74.6, 2128, "heavy fuel oil + diesel (bunker, at risk)", "Explosions and fire aboard a Singapore-flag container ship en route Colombo→Mumbai, ~78 nm off Beypore; ~50 containers fell overboard and hydrocarbon release was detected near fuel tanks (major oil-spill risk); the vessel drifted toward the Kerala coast", "MV Wan Hai 503 (IMO 9294862, container ship)", ["Kerala coast (Kozhikode–Kochi)", "Arabian Sea fisheries", "beaches", "backwaters"], ["ICG intensive firefighting & boundary cooling", "IAF MI-17 dry-chemical drops", "salvage & offshore tow", "INCOIS drift projection + container-beaching alerts"], "Container-ship fires combine fuel-oil, hazardous-cargo and floating-container hazards; INCOIS drift modelling drove shoreline alerts (Kozhikode–Kochi); ~2,128 t bunker fuel and 143–157 hazardous containers at risk (hydrocarbon release detected) — spilled volume not precisely quantified. Position is approximate (drifting).", "India"),
    ("2010 Mumbai oil spill (MSC Chitra – MV Khalijia III collision)", "2010-08-07", 18.864, 72.820, 800, "heavy fuel oil", "Collision between outbound container ship MSC Chitra and inbound bulk carrier MV Khalijia III near Mumbai harbour approach; MSC Chitra listed ~80° and released bunker oil, and ~400 containers (31 hazardous, incl. organophosphate pesticides) fell into the sea", "MSC Chitra / MV Khalijia III", ["Mumbai/Thane/Raigad coast (~110 km)", "mangroves", "Juhu & Uran beaches", "Arabian Sea fisheries"], ["Indian Coast Guard dispersant spraying (ships + helicopters)", "manual shoreline recovery (gunny bags)", "hot-water jet washing of rocks", "bioremediation (oil-zapper)", "container & wreck removal", "harbour traffic suspension"], "VTS–bridge miscommunication in a congested harbour approach; hazardous-container recovery and a ~110 km shoreline response strained Tier-1 capacity; leak plugged by 9 Aug 2010 but monitoring continued for months. Reported spillage ranged 400–879 t.", "India"),
    ("MV Rak Carrier sinking off Mumbai", "2011-08-04", 18.775, 72.489, 290, "fuel oil + diesel (bunker)", "Panama-flag bulk carrier (coal cargo, Indonesia→Dahej) foundered ~20 nm off Mumbai after machinery failure and water ingress (poor maintenance); bunker fuel leaked from the wreck at ~1.5–2 t/hr", "MV Rak Carrier (bulk carrier)", ["Mumbai & Raigad coast", "Juhu/Bandra/Gorai/Alibaug beaches", "mangroves", "Arabian Sea fisheries"], ["ICG 'Operation Paryavaran Suraksha 02/2011' dispersant spraying", "shoreline monitoring & manual cleanup", "wreck leak monitoring"], "Sub-standard vessels transiting in monsoon are a spill risk; ~340 t bunker onboard leaked steadily from the wreck; owner/monitoring failures drove the sinking (captain & chief engineer arrested).", "India"),
    ("Bombay High North platform fire (ONGC)", "2005-07-27", 19.6, 71.5, 100, "crude oil + gas (offshore production complex, hydrocarbon release)", "MSV Samudra Suraksha collided with the Mumbai High North complex during a monsoon medevac, severing unprotected gas-export risers; the release ignited into a jet fire that destroyed the four-platform complex, which collapsed within ~2 hours", "Mumbai High North complex / MSV Samudra Suraksha (ONGC/SCI)", ["Arabian Sea offshore (~100 km off Mumbai)", "offshore production zone"], ["emergency evacuation (362 of 384 rescued)", "firefighting & platform isolation", "OISD offshore-safety regime overhaul"], "Primarily an offshore fire/structural disaster (22 fatalities, ~US$370 m loss) with associated hydrocarbon release — unprotected risers and a faulty dynamic-positioning system were key factors; led to stronger OISD offshore-safety powers.", "India"),
    ("MV River Princess grounding — Goa (Candolim)", "2000-06-06", 15.518, 73.762, 50, "residual/bunker oil (localized leaks from a grounded tanker)", "Oil tanker MV River Princess dragged anchor in a cyclone and ran aground off Candolim–Sinquerim beach; the wreck stayed ~12 years, leaking residual oil and acting as a breakwater that drove severe coastal erosion", "MV River Princess (oil tanker, grounded wreck)", ["Candolim–Sinquerim beaches (North Goa)", "nearshore sand dunes", "Arabian Sea coastal fisheries"], ["in-situ dismantling / ship-breaking (2011–12)", "NIO/GSL seabed debris survey & removal (to 2014)", "localized oil cleanup"], "A grounded wreck is a long-term hazard: 12+ years to remove, ~0.13 km² beach loss from breakwater-induced erosion, and disputed ₹166.4 cr cost-recovery from the charterer — early wreck removal and cost-recovery clarity matter.", "India"),
    ("Ennore ammonia subsea-pipeline leak (Coromandel International)", "2023-12-26", 13.23, 80.33, 67.6, "ammonia (liquid industrial chemical — NOT oil)", "A pressure drop ruptured an 8-inch HDPE ship-to-shore subsea ammonia pipeline at a fertiliser plant — reportedly after Cyclone Michaung shifted seabed boulders — releasing ~67.6 t of ammonia into nearshore waters in ~15 minutes", "Coromandel International Ltd fertiliser plant, Ennore", ["Ennore fishing hamlets (Periyakuppam, Chinnakuppam)", "nearshore Bay of Bengal", "artisanal fisheries"], ["TNPCB suspension of operations", "pipeline inspection & recertification (TN Maritime Board / IRS)", "medical response (~60 hospitalised)", "NGT suo-motu monitoring"], "CHEMICAL (ammonia) marine release — NOT an oil spill: seawater ammonia reached 49 mg/L (vs 5 mg/L standard) with fish kills; illustrates non-oil hazardous-substance pathways and cyclone damage to subsea infrastructure. Included for regional context.", "India"),
]


def _doc(row, actor="seed"):
    name, date, lat, lon, vol, oil, cause, vf, eco, rem, les, country = row
    return {"id": new_id(), "name": name, "date": datetime.fromisoformat(date).replace(tzinfo=timezone.utc), "lat": lat, "lon": lon, "location": {"type": "Point", "coordinates": [lon, lat]}, "volume_tonnes": vol,
            "oil_type": oil, "cause": cause, "vessel_facility": vf, "ecosystems": eco, "remediation": rem, "lessons": les, "country": country, "source": "seed" if actor == "seed" else "supervisor", "created_by": actor, "created_at": datetime.now(timezone.utc)}


async def seed_archive():
    existing = {r["name"] for r in await db.historical_spills.find({"source": "seed"}, {"_id": 0, "name": 1}).to_list(500)}
    missing = [_doc(r) for r in SEED if r[0] not in existing]
    if missing:
        await db.historical_spills.insert_many(missing)
    await db.historical_spills.create_index([("location", "2dsphere")])


@router.get("/archive")
async def list_archive(q: str = Query("", max_length=80), limit: int = Query(50, ge=1, le=200), user=Depends(get_current_user)):
    rows = await db.historical_spills.find({}, {"_id": 0}).sort("date", -1).to_list(500)
    if q.strip():
        ql = q.strip().lower()
        scored = []
        for r in rows:
            hay = f"{r['name']} {r['country']} {r['oil_type']} {r['cause']} {r['vessel_facility']} {' '.join(r['ecosystems'])}"
            s = fuzz.WRatio(q, hay)
            if ql in hay.lower():  # substring match (e.g. "elsa", "kochi") — boost above the fuzzy threshold
                s = max(s, 90)
            scored.append((s, r))
        rows = [r for s, r in sorted(scored, key=lambda x: -x[0]) if s >= 50]
    return clean(rows[:limit])


class SpillIn(BaseModel):
    name: str = Field(min_length=3, max_length=140)
    date: str
    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)
    volume_tonnes: Optional[float] = Field(default=None, ge=0)
    oil_type: str = "unknown"
    cause: str = ""
    vessel_facility: Optional[str] = None
    ecosystems: List[str] = []
    remediation: List[str] = []
    lessons: str = ""
    country: Optional[str] = None


@router.get("/archive/lessons")
async def archive_lessons(user=Depends(get_current_user)):
    """Cross-incident synthesis: what past spills did to the marine environment/economy and which responses worked."""
    rows = await db.historical_spills.find({}, {"_id": 0}).sort("date", 1).to_list(500)
    eco, rem = {}, {}
    for r in rows:
        for e in r.get("ecosystems") or []:
            eco.setdefault(e, []).append(r["name"])
        for m in r.get("remediation") or []:
            rem.setdefault(m, []).append(r["name"])
    total_vol = sum(r.get("volume_tonnes") or 0 for r in rows)
    causes = {}
    for r in rows:
        k = (r.get("cause") or "unknown").split(" ")[0].lower()
        causes[k] = causes.get(k, 0) + 1
    tactics = [
        {"phase": "0–2 h · Detect & verify", "actions": ["Confirm SAR dark spot with a second sensor/pass or optical (Sentinel-2/VIIRS) — low wind, algae and upwelling look alike", "Estimate area/thickness (Bonn Agreement appearance code) and drift with wind (3 %) + surface current", "Pull AIS ±6 h in the corridor; rank candidate vessels — never name a 'responsible' vessel without confirmation"]},
        {"phase": "2–12 h · Contain", "actions": ["Deploy booms at the leading edge and around sensitive intakes/mangrove mouths first", "Mechanical recovery (skimmers) while oil is fresh and thick", "Dispersant only offshore, with NOSDCP/ICG approval, never over reefs or shallow fisheries"]},
        {"phase": "12–72 h · Protect shoreline", "actions": ["Prioritise shoreline segments by vulnerability (mangroves, turtle nesting, aquaculture, intakes)", "Pre-position sorbents and shoreline crews; close fishing zones with clear public advisories", "Start wildlife triage and sampling for evidence (chain of custody)"]},
        {"phase": "Days–months · Recover & prosecute", "actions": ["Shoreline Cleanup Assessment Technique (SCAT) surveys and end-point criteria", "Bioremediation for residual oil in sediments; avoid aggressive washing of mangroves", "Evidence package: scene IDs, AIS tracks, drift model, sampling → MARPOL/Merchant Shipping Act claims (CLC/Fund)"]},
    ]
    return clean({"incidents": len(rows), "total_volume_tonnes": total_vol, "date_range": [rows[0]["date"], rows[-1]["date"]] if rows else None,
                  "problems": {"ecosystems_affected": sorted(({"ecosystem": k, "incidents": len(v), "examples": v[:4]} for k, v in eco.items()), key=lambda x: -x["incidents"]),
                               "causes": sorted(({"cause": k, "incidents": v} for k, v in causes.items()), key=lambda x: -x["incidents"]),
                               "impacts": ["Fisheries closures and income loss for coastal communities", "Mangrove/coral mortality with decade-scale recovery", "Seabird, turtle and marine-mammal casualties", "Tourism/port disruption and cleanup costs in the hundreds of crores", "Long litigation when the source vessel is not identified early"]},
                  "solutions": {"remediation_used": sorted(({"method": k, "incidents": len(v), "examples": v[:4]} for k, v in rem.items()), key=lambda x: -x["incidents"]),
                                "lessons": [{"incident": r["name"], "date": r["date"], "lesson": r["lessons"]} for r in rows if r.get("lessons")],
                                "response_tactics": tactics},
                  "note": "Synthesised from the curated historical archive (public incident records); tactics follow NOSDCP/IMO/ITOPF guidance — decision support, not a legal instrument."})


@router.get("/archive/{entry_id}/vault")
async def vault(entry_id: str, user=Depends(get_current_user)):
    from vault import VAULT, reconstruct_frames
    e = await db.historical_spills.find_one({"id": entry_id}, {"_id": 0, "location": 0})
    if not e:
        raise HTTPException(404, "entry not found")
    e["date"] = to_utc(e["date"])
    v = VAULT.get(e["name"])
    return clean({"entry": e, "evidence": v, "frames": reconstruct_frames(e), "reconstructed": True,
                  "note": "Footprint replay is RECONSTRUCTED from recorded volume/duration (√t spreading, then weathering) — illustrative, not an archived SAR footprint. Legal/ecological facts summarised from the cited public sources; verify before citation."})


@router.post("/archive", status_code=201)
async def create_entry(body: SpillIn, user=Depends(require_role("supervisor"))):
    try:
        row = (body.name, body.date, body.lat, body.lon, body.volume_tonnes, body.oil_type, body.cause, body.vessel_facility, body.ecosystems, body.remediation, body.lessons, body.country)
        doc = _doc(row, user["email"])
    except ValueError:
        raise HTTPException(400, "date must be ISO YYYY-MM-DD")
    await db.historical_spills.insert_one(dict(doc))
    await audit("archive", doc["id"], "archive.created", {"name": body.name}, user["email"])
    return clean(doc)


@router.delete("/archive/{entry_id}")
async def delete_entry(entry_id: str, user=Depends(require_role("supervisor"))):
    if not (await db.historical_spills.delete_one({"id": entry_id, "source": {"$ne": "seed"}})).deleted_count:
        raise HTTPException(404, "entry not found or protected seed")
    return {"ok": True}


def similarity(case_lat, case_lon, case_vol_t, case_oil, entry):
    d = haversine_km(case_lat, case_lon, entry["lat"], entry["lon"])
    s_dist = max(0.0, 1 - d / 5000)
    v = entry.get("volume_tonnes") or 1
    s_vol = max(0.0, 1 - abs(__import__("math").log10(max(case_vol_t, 0.1)) - __import__("math").log10(v)) / 3)
    s_oil = 1.0 if case_oil and case_oil.split()[0].lower() in (entry.get("oil_type") or "").lower() else 0.4
    return round(0.5 * s_dist + 0.3 * s_vol + 0.2 * s_oil, 3), round(d, 0)


@router.get("/cases/{case_id}/precedents")
async def precedents(case_id: str, limit: int = Query(3, ge=1, le=10), user=Depends(get_current_user)):
    case = await db.cases.find_one({"id": case_id}, {"_id": 0})
    if not case:
        raise HTTPException(404, "case not found")
    spill = await db.spill_observations.find_one({"id": case["spill_observation_id"]}, {"_id": 0, "estimated_area_km2": 1, "centroid": 1, "oil_type": 1})
    lon, lat = spill["centroid"]["coordinates"]
    vol_t = (spill.get("estimated_area_km2") or 0) * 1e6 * 1e-6 * 0.9 * 1000 / 1000  # area × 1 µm thickness → m³ → tonnes (≈0.9 t/m³)
    rows = await db.historical_spills.find({}, {"_id": 0}).to_list(500)
    out = []
    for r in rows:
        s, d = similarity(lat, lon, vol_t, spill.get("oil_type"), r)
        out.append({**r, "similarity": s, "distance_km": d})
    out.sort(key=lambda r: -r["similarity"])
    return clean({"case_number": case["case_number"], "estimated_volume_tonnes": round(vol_t, 2), "weights": {"distance": 0.5, "volume": 0.3, "oil_type": 0.2}, "precedents": out[:limit]})
