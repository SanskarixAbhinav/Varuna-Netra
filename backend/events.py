import asyncio
import json
import logging
from datetime import datetime, timezone

logger = logging.getLogger("events")
_subscribers: set = set()


def _ser(o):
    return o.isoformat() if isinstance(o, datetime) else str(o)


def publish(event_type: str, data: dict):
    msg = f"event: {event_type}\ndata: {json.dumps({**data, 'event': event_type, 'at': datetime.now(timezone.utc).isoformat()}, default=_ser)}\n\n"
    for q in list(_subscribers):
        try:
            q.put_nowait(msg)
        except asyncio.QueueFull:
            _subscribers.discard(q)


def subscribe() -> asyncio.Queue:
    q = asyncio.Queue(maxsize=200)
    _subscribers.add(q)
    return q


def unsubscribe(q):
    _subscribers.discard(q)


def subscriber_count():
    return len(_subscribers)
