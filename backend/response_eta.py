"""Nearest response asset + ETA. Station list is APPROXIMATE public knowledge (ICG station towns), not an official ICG order of battle."""

from geo import haversine_km

# (code, name, lat, lon, capabilities)  — coordinates are harbour/town approximations
STATIONS = [
    ("ICG-OKHA", "ICG Station Okha", 22.47, 69.07, ["patrol"]), ("ICG-PBD", "ICG Station Porbandar", 21.63, 69.60, ["patrol", "dornier"]),
    ("ICG-VAD", "ICG Station Vadinar", 22.44, 69.71, ["patrol", "prv"]), ("ICG-MUM", "ICG Region West HQ Mumbai", 18.93, 72.84, ["patrol", "prv", "dornier", "helicopter"]),
    ("ICG-GOA", "ICG Station Vasco (Goa)", 15.40, 73.80, ["patrol", "dornier"]), ("ICG-NMG", "ICG Station New Mangalore", 12.92, 74.81, ["patrol"]),
    ("ICG-KOC", "ICG Station Kochi", 9.97, 76.27, ["patrol", "dornier", "helicopter"]), ("ICG-TUT", "ICG Station Tuticorin", 8.76, 78.18, ["patrol"]),
    ("ICG-MDU", "ICG Station Mandapam", 9.28, 79.12, ["patrol"]), ("ICG-CHN", "ICG Region East HQ Chennai", 13.10, 80.30, ["patrol", "prv", "dornier", "helicopter"]),
    ("ICG-KKD", "ICG Station Kakinada", 16.94, 82.24, ["patrol"]), ("ICG-VZG", "ICG Station Visakhapatnam", 17.69, 83.29, ["patrol", "dornier"]),
    ("ICG-PDP", "ICG Station Paradip", 20.27, 86.67, ["patrol"]), ("ICG-HLD", "ICG Station Haldia", 22.03, 88.09, ["patrol"]),
    ("ICG-PBL", "ICG Region A&N HQ Port Blair", 11.67, 92.75, ["patrol", "prv", "dornier"]), ("ICG-CBB", "ICG Station Campbell Bay", 7.00, 93.92, ["patrol"]),
    ("ICG-KVT", "ICG Station Kavaratti", 10.57, 72.64, ["patrol"]),
]
SPEEDS_KN = {"patrol": 22.0, "prv": 16.0, "dornier": 300.0, "helicopter": 130.0}
LABELS = {"patrol": "Fast patrol vessel", "prv": "Pollution response vessel", "dornier": "Dornier surveillance aircraft", "helicopter": "Helicopter (ALH/Chetak)"}
MOBILISE_MIN = {"patrol": 45, "prv": 90, "dornier": 40, "helicopter": 30}
MAX_KM = 2500.0


def nearest_response(lat: float, lon: float) -> dict:
    ranked = sorted(((haversine_km(lat, lon, s[2], s[3]), s) for s in STATIONS), key=lambda x: x[0])
    d0, s0 = ranked[0]
    if d0 > MAX_KM:
        return {"available": False, "nearest_station": {"code": s0[0], "name": s0[1], "distance_km": round(d0)},
                "note": "Spill lies outside Indian Coast Guard response range — hand off to the flag/coastal state MRCC (approximate station list)."}
    assets = []
    for kind in ("dornier", "helicopter", "patrol", "prv"):
        cand = [(d, s) for d, s in ranked if kind in s[4]]
        if not cand:
            continue
        d, s = cand[0]
        eta_h = d / (SPEEDS_KN[kind] * 1.852) + MOBILISE_MIN[kind] / 60
        assets.append({"asset": kind, "label": LABELS[kind], "station_code": s[0], "station": s[1], "station_lat": s[2], "station_lon": s[3], "distance_km": round(d, 1),
                       "speed_kn": SPEEDS_KN[kind], "mobilise_min": MOBILISE_MIN[kind], "eta_hours": round(eta_h, 2)})
    first = min(assets, key=lambda a: a["eta_hours"])
    return {"available": True, "nearest_station": {"code": s0[0], "name": s0[1], "lat": s0[2], "lon": s0[3], "distance_km": round(d0, 1)}, "assets": assets,
            "first_on_scene": first, "containment": next((a for a in assets if a["asset"] == "prv"), None),
            "note": "ETA = great-circle distance / cruise speed + mobilisation time; approximate station list, no official readiness data."}
