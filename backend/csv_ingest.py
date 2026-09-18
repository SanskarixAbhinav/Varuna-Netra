import csv
import io
import re
from datetime import datetime, timezone

from models import AISPositionIn

ALIASES = {
    "mmsi": ["mmsi", "mmsi_number", "userid", "user_id"],
    "timestamp": ["timestamp", "basedatetime", "base_date_time", "time", "datetime", "date_time", "ts", "received", "received_at", "position_timestamp", "time_utc", "utc"],
    "lat": ["lat", "latitude", "y"],
    "lon": ["lon", "long", "longitude", "lng", "x"],
    "sog_kn": ["sog", "sog_kn", "speed", "speedoverground", "speed_over_ground", "speed_kn"],
    "cog_deg": ["cog", "cog_deg", "course", "courseoverground", "course_over_ground"],
    "heading_deg": ["heading", "heading_deg", "hdg", "trueheading", "true_heading"],
    "vessel_name": ["vesselname", "vessel_name", "name", "shipname", "ship_name", "vessel"],
    "imo": ["imo", "imo_number"],
    "vessel_type": ["vesseltype", "vessel_type", "type", "shiptype", "ship_type"],
    "source": ["source", "provider", "receiver"],
}
REQUIRED = ["mmsi", "timestamp", "lat", "lon"]


def _norm(h: str) -> str:
    return re.sub(r"[^a-z0-9_]", "", h.strip().lower().replace(" ", "_"))


def detect_mapping(headers):
    normed = {_norm(h): h for h in headers}
    mapping = {}
    for field, names in ALIASES.items():
        for n in names:
            if n in normed:
                mapping[field] = normed[n]
                break
    return mapping


def parse_timestamp(v: str):
    v = v.strip()
    if not v:
        raise ValueError("empty timestamp")
    if re.fullmatch(r"\d{9,13}", v):
        n = int(v)
        return datetime.fromtimestamp(n / 1000 if n > 1e11 else n, tz=timezone.utc)
    s = v.replace("Z", "+00:00")
    for fmt in (None, "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M", "%d/%m/%Y %H:%M:%S", "%m/%d/%Y %H:%M:%S", "%Y%m%dT%H%M%S"):
        try:
            return datetime.fromisoformat(s) if fmt is None else datetime.strptime(v, fmt)
        except ValueError:
            continue
    raise ValueError(f"unparseable timestamp '{v}'")


def read_csv(data: bytes):
    text = data.decode("utf-8-sig", errors="replace")
    sample = text[:4096]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel
    reader = csv.DictReader(io.StringIO(text), dialect=dialect)
    return reader.fieldnames or [], list(reader)


def _f(v):
    v = (v or "").strip()
    return None if v in ("", "NA", "N/A", "null", "None", "-") else float(v)


def _clean_mmsi(raw):
    mmsi = (raw or "").strip()
    if not mmsi:
        raise ValueError("empty MMSI")
    return mmsi[:-2] if re.fullmatch(r"\d+\.0", mmsi) else mmsi


def _str_or_none(v):
    return (v or "").strip() or None


def _row_to_position(row, mapping, default_source):
    g = lambda k: (row.get(mapping[k]) if mapping.get(k) else None)  # noqa: E731
    heading = _f(g("heading_deg"))
    if heading is not None and heading > 511:
        heading = None
    return AISPositionIn(
        mmsi=_clean_mmsi(g("mmsi")), imo=_str_or_none(g("imo")), vessel_name=_str_or_none(g("vessel_name")),
        vessel_type=_str_or_none(g("vessel_type")), timestamp=parse_timestamp(g("timestamp") or ""),
        lat=_f(g("lat")), lon=_f(g("lon")), sog_kn=_f(g("sog_kn")), cog_deg=_f(g("cog_deg")), heading_deg=heading,
        source=_str_or_none(g("source")) or default_source)


def rows_to_positions(rows, mapping, default_source="csv-upload"):
    missing = [r for r in REQUIRED if r not in mapping]
    if missing:
        raise ValueError(f"missing required column mapping: {missing}")
    positions, errors = [], []
    for i, row in enumerate(rows, start=2):
        try:
            positions.append(_row_to_position(row, mapping, default_source))
        except Exception as e:  # noqa: BLE001
            errors.append({"row": i, "error": str(e)[:160]})
    return positions, errors
