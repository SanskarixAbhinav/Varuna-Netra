import math
from typing import Optional, Tuple

WIND_FACTOR = 0.03


def drift_vector_ms(wind: Optional[dict], current: Optional[dict]) -> Tuple[float, float]:
    """Surface drift (east, north) in m/s: 3% of wind (downwind) plus current."""
    vx = vy = 0.0
    if wind:
        to = math.radians((wind["direction_deg"] + 180) % 360)
        vx += WIND_FACTOR * wind["speed_ms"] * math.sin(to)
        vy += WIND_FACTOR * wind["speed_ms"] * math.cos(to)
    if current:
        d = math.radians(current["direction_deg"])
        vx += current["speed_ms"] * math.sin(d)
        vy += current["speed_ms"] * math.cos(d)
    return vx, vy
