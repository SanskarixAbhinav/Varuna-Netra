import asyncio
import logging
import os
from pathlib import Path

logger = logging.getLogger("storage")
STORAGE_ROOT = Path(os.environ.get("STORAGE_DIR", "/tmp/varuna-netra-storage"))

def init_storage(force: bool = False):
    STORAGE_ROOT.mkdir(parents=True, exist_ok=True)
    return str(STORAGE_ROOT)

def _safe_path(path: str) -> Path:
    clean = path.lstrip("/").replace("\\", "/")
    target = (STORAGE_ROOT / clean).resolve()
    root = STORAGE_ROOT.resolve()
    if root not in target.parents and target != root:
        raise ValueError("invalid storage path")
    return target

def _put(path: str, data: bytes, content_type: str) -> dict:
    target = _safe_path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    return {"path": path, "size": len(data), "content_type": content_type}

def _get(path: str):
    target = _safe_path(path)
    if not target.exists():
        raise FileNotFoundError(path)
    return target.read_bytes(), "application/octet-stream"

async def put_object(path: str, data: bytes, content_type: str) -> dict:
    return await asyncio.to_thread(_put, path, data, content_type)

async def get_object(path: str):
    return await asyncio.to_thread(_get, path)

def storage_available() -> bool:
    try:
        STORAGE_ROOT.mkdir(parents=True, exist_ok=True)
        return True
    except OSError:
        return False
