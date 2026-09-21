import asyncio
import logging
import mimetypes
import os
from pathlib import Path

import requests

logger = logging.getLogger("storage")
STORAGE_BASE = (os.environ.get("INTEGRATION_PROXY_URL") or "").strip()
STORAGE_URL = (STORAGE_BASE.rstrip("/") + "/objstore/api/v1/storage") if STORAGE_BASE else ""
APP_NAME = "sentinelmar"
UPLOAD_DIR = Path(os.environ.get("UPLOAD_DIR") or (Path(__file__).parent / ".uploads"))
_storage_key = None


def _is_remote() -> bool:
    return bool(STORAGE_URL and os.environ.get("EMERGENT_LLM_KEY"))


def init_storage(force: bool = False):
    global _storage_key
    if _is_remote():
        if _storage_key and not force:
            return _storage_key
        resp = requests.post(f"{STORAGE_URL}/init", json={"emergent_key": os.environ["EMERGENT_LLM_KEY"]}, timeout=30)
        resp.raise_for_status()
        _storage_key = resp.json()["storage_key"]
        return _storage_key
    # Local filesystem storage
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    return "local"


def _put(path: str, data: bytes, content_type: str) -> dict:
    if _is_remote():
        for attempt in range(2):
            resp = requests.put(f"{STORAGE_URL}/objects/{path}", headers={"X-Storage-Key": init_storage(force=attempt > 0), "Content-Type": content_type}, data=data, timeout=180)
            if resp.status_code == 404 and attempt == 0:
                continue
            resp.raise_for_status()
            return resp.json()
    # Local storage fallback
    full_path = UPLOAD_DIR / path
    full_path.parent.mkdir(parents=True, exist_ok=True)
    full_path.write_bytes(data)
    return {"path": path, "size": len(data), "status": "stored_local"}


def _get(path: str):
    if _is_remote():
        for attempt in range(2):
            resp = requests.get(f"{STORAGE_URL}/objects/{path}", headers={"X-Storage-Key": init_storage(force=attempt > 0)}, timeout=120)
            if resp.status_code == 404 and attempt == 0:
                continue
            resp.raise_for_status()
            return resp.content, resp.headers.get("Content-Type", "application/octet-stream")
    # Local storage fallback
    full_path = UPLOAD_DIR / path
    if not full_path.exists():
        raise FileNotFoundError(f"Stored object not found: {path}")
    ctype, _ = mimetypes.guess_type(str(full_path))
    return full_path.read_bytes(), ctype or "application/octet-stream"


async def put_object(path: str, data: bytes, content_type: str) -> dict:
    return await asyncio.to_thread(_put, path, data, content_type)


async def get_object(path: str):
    return await asyncio.to_thread(_get, path)


def storage_available() -> bool:
    return True
