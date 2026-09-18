import math
from shapely.geometry import shape, Point
from shapely.ops import nearest_points, transform
from shapely.validation import explain_validity

EARTH_KM = 6371.0088


def haversine_km(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_KM * math.asin(math.sqrt(a))


def bearing_deg(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    x = math.sin(dl) * math.cos(p2)
    y = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return (math.degrees(math.atan2(x, y)) + 360) % 360


def destination(lat, lon, bearing, dist_km):
    d = dist_km / EARTH_KM
    b = math.radians(bearing)
    p1, l1 = math.radians(lat), math.radians(lon)
    p2 = math.asin(math.sin(p1) * math.cos(d) + math.cos(p1) * math.sin(d) * math.cos(b))
    l2 = l1 + math.atan2(math.sin(b) * math.sin(d) * math.cos(p1), math.cos(d) - math.sin(p1) * math.sin(p2))
    return math.degrees(p2), ((math.degrees(l2) + 540) % 360) - 180


def validate_polygon(geom: dict):
    if not isinstance(geom, dict) or geom.get("type") not in ("Polygon", "MultiPolygon"):
        raise ValueError("geometry must be a GeoJSON Polygon or MultiPolygon")
    try:
        poly = shape(geom)
    except Exception as e:
        raise ValueError(f"invalid GeoJSON geometry: {e}")
    if poly.is_empty:
        raise ValueError("geometry is empty")
    if not poly.is_valid:
        raise ValueError(f"invalid geometry: {explain_validity(poly)}")
    minx, miny, maxx, maxy = poly.bounds
    if not (-180 <= minx <= 180 and -180 <= maxx <= 180 and -90 <= miny <= 90 and -90 <= maxy <= 90):
        raise ValueError("coordinates out of WGS84 range (expected [lon, lat])")
    return poly


def area_km2(poly):
    lat0 = poly.centroid.y
    kx, ky = 111.32 * math.cos(math.radians(lat0)), 110.574
    return round(transform(lambda x, y, z=None: (x * kx, y * ky), poly).area, 4)


def distance_to_geom_km(poly, lat, lon):
    p = Point(lon, lat)
    if poly.contains(p):
        return 0.0
    q = nearest_points(poly, p)[0]
    return haversine_km(lat, lon, q.y, q.x)


def max_extent_km(poly):
    c = poly.centroid
    return max(haversine_km(c.y, c.x, y, x) for x, y in poly.convex_hull.exterior.coords)


def major_axis_bearing(poly):
    rect = poly.minimum_rotated_rectangle
    coords = list(rect.exterior.coords)[:4]
    if len(coords) < 4:
        return 0.0
    best = (-1, 0.0)
    for i in range(4):
        (x1, y1), (x2, y2) = coords[i], coords[(i + 1) % 4]
        length = haversine_km(y1, x1, y2, x2)
        if length > best[0]:
            best = (length, bearing_deg(y1, x1, y2, x2))
    return round(best[1] % 180, 1)


def circle_polygon(lat, lon, radius_km, n=64):
    ring = [list(reversed(destination(lat, lon, 360 * i / n, radius_km))) for i in range(n)]
    ring.append(ring[0])
    return {"type": "Polygon", "coordinates": [ring]}


def rotated_rect_polygon(lat, lon, bearing, length_km, width_km):
    hl, hw = length_km / 2, width_km / 2
    corners = []
    for along, across in ((hl, hw), (hl, -hw), (-hl, -hw), (-hl, hw)):
        la, lo = destination(lat, lon, bearing, along)
        la, lo = destination(la, lo, bearing + 90, across)
        corners.append([round(lo, 6), round(la, 6)])
    corners.append(corners[0])
    return {"type": "Polygon", "coordinates": [corners]}
