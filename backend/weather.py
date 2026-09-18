import asyncio
from datetime import datetime, timezone

import httpx

ARCHIVE = "https://archive-api.open-meteo.com/v1/archive"
FORECAST = "https://api.open-meteo.com/v1/forecast"
MARINE = "https://marine-api.open-meteo.com/v1/marine"


def _pick(hourly, key, t):
    times = hourly["time"]
    i = min(range(len(times)), key=lambda k: abs(datetime.fromisoformat(times[k]).replace(tzinfo=timezone.utc) - t))
    return hourly[key][i], times[i]


async def fetch_environment(lat: float, lon: float, t: datetime) -> dict:
    """Wind (meteorological FROM) and surface current (TOWARD) at the nearest hour from Open-Meteo."""
    day = t.strftime("%Y-%m-%d")
    age_days = (datetime.now(timezone.utc) - t).days
    wind_url, wind_model = (ARCHIVE, "era5-reanalysis") if age_days >= 6 else (FORECAST, "gfs/icon-forecast")
    base = {"latitude": lat, "longitude": lon, "start_date": day, "end_date": day, "timezone": "UTC"}
    out = {"source": "open-meteo", "wind": None, "current": None, "wind_model": wind_model, "current_model": "copernicus-marine",
           "requested_time": t.isoformat(), "fetched_at": datetime.now(timezone.utc).isoformat(), "errors": []}
    async with httpx.AsyncClient(timeout=25) as c:
        wind_req = c.get(wind_url, params={**base, "hourly": "wind_speed_10m,wind_direction_10m", "wind_speed_unit": "ms"})
        cur_req = c.get(MARINE, params={**base, "hourly": "ocean_current_velocity,ocean_current_direction"})
        wind_res, cur_res = await asyncio.gather(wind_req, cur_req, return_exceptions=True)
    if isinstance(wind_res, Exception):
        out["errors"].append(f"wind: {wind_res}")
    else:
        try:
            h = wind_res.json()["hourly"]
            spd, vt = _pick(h, "wind_speed_10m", t)
            dr, _ = _pick(h, "wind_direction_10m", t)
            if spd is not None and dr is not None:
                out["wind"] = {"speed_ms": round(float(spd), 2), "direction_deg": round(float(dr) % 360, 1), "valid_time": vt}
            else:
                out["errors"].append("wind: no data for requested hour")
        except Exception as e:
            out["errors"].append(f"wind: {wind_res.text[:120] if hasattr(wind_res, 'text') else e}")
    if isinstance(cur_res, Exception):
        out["errors"].append(f"current: {cur_res}")
    else:
        try:
            h = cur_res.json()["hourly"]
            vel, vt = _pick(h, "ocean_current_velocity", t)
            dr, _ = _pick(h, "ocean_current_direction", t)
            if vel is not None and dr is not None:
                out["current"] = {"speed_ms": round(float(vel) / 3.6, 3), "direction_deg": round(float(dr) % 360, 1), "valid_time": vt}
            else:
                out["errors"].append("current: no data for requested hour (land or coverage gap)")
        except Exception as e:
            out["errors"].append(f"current: {cur_res.text[:120] if hasattr(cur_res, 'text') else e}")
    return out
