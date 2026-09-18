import hashlib
import io
import json
import zipfile
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.responses import Response

from auth import get_current_user, require_role
from db import db, clean, audit
from models import new_id
from playbook import playbook_for_case

router = APIRouter()


def _j(o):
    return json.dumps(o, indent=2, sort_keys=True, default=str).encode()


def _sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


@router.get("/cases/{case_id}/playbook")
async def get_playbook(case_id: str, user=Depends(get_current_user)):
    pb = await playbook_for_case(case_id)
    if not pb:
        raise HTTPException(404, "case not found")
    return clean(pb)


@router.post("/cases/{case_id}/prosecution-export")
async def prosecution_export(case_id: str, user=Depends(require_role("supervisor"))):
    from routers.cases import _bundle
    from report import build_pdf
    from storage import get_object
    bundle = await _bundle(case_id, None)
    bundle["generated_by"] = f"{user.get('name')} <{user['email']}> ({user['role']})"
    bundle["attachment_images"] = []
    bundle["playbook"] = await playbook_for_case(case_id)
    pdf = build_pdf(bundle)
    case = bundle["case"]
    src = bundle.get("source_references") or {}
    fix_ids = sorted({fid for c in (bundle.get("calculations") or {}).get("candidates", []) for fid in c["evidence"].get("fix_ids", [])} | set(src.get("ais_fix_ids") or []))
    fixes = await db.ais_positions.find({"id": {"$in": fix_ids}}, {"_id": 0, "location": 0}).sort("timestamp", 1).to_list(50000) if fix_ids else []
    results = await db.correlation_results.find({"case_id": case_id}, {"_id": 0}).sort("version", 1).to_list(100)
    exported_at = datetime.now(timezone.utc)
    export_id = new_id()
    files = {
        "evidence.pdf": pdf,
        "case.json": _j(case), "spill_observation.json": _j(src.get("spill_observation")), "scene.json": _j(src.get("scene")), "geometries.json": _j(src.get("geometries")),
        "ais_fixes.json": _j(fixes), "correlation_results.json": _j(results), "analyst_reviews.json": _j(bundle.get("reviews")),
        "audit_history.json": _j(bundle.get("audit_history")), "detector_feedback.json": _j(bundle.get("detector_feedback")), "remediation_playbook.json": _j(bundle["playbook"]),
        "attachments_manifest.json": _j([{k: v for k, v in a.items() if k != "storage_path"} for a in bundle.get("attachments", [])]),
    }
    for a in bundle.get("attachments", [])[:20]:
        try:
            data, _ = await get_object(a["storage_path"])
            files[f"attachments/{a['id']}_{a['original_filename']}"] = data
        except Exception:  # noqa: BLE001
            pass
    manifest = {"export_id": export_id, "case_id": case_id, "case_number": case["case_number"], "exported_at": exported_at.isoformat(), "exporting_officer": {"email": user["email"], "name": user.get("name"), "role": user["role"], "id": user["id"]},
                "algorithm_versions": sorted({r["algorithm_version"] for r in results}), "hash_algorithm": "SHA-256", "files": {name: {"sha256": _sha(b), "bytes": len(b)} for name, b in files.items()},
                "disclaimer": "Decision-support evidence. Correlation indicates possible/probable association; responsibility requires analyst confirmation and corroborating evidence."}
    manifest["content_hash"] = _sha("".join(f"{n}:{f['sha256']}" for n, f in sorted(manifest["files"].items())).encode())
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, b in files.items():
            z.writestr(zipfile.ZipInfo(name, date_time=exported_at.timetuple()[:6]), b)
        z.writestr(zipfile.ZipInfo("MANIFEST.json", date_time=exported_at.timetuple()[:6]), _j(manifest))
    blob = buf.getvalue()
    bundle_hash = _sha(blob)
    rec = {"id": export_id, "case_id": case_id, "case_number": case["case_number"], "bundle_sha256": bundle_hash, "content_hash": manifest["content_hash"], "files": manifest["files"], "bytes": len(blob),
           "exported_at": exported_at, "exporting_officer": manifest["exporting_officer"], "verifications": 0}
    await db.prosecution_exports.insert_one(dict(rec))
    await db.cases.update_one({"id": case_id}, {"$set": {"last_prosecution_export": {"id": export_id, "bundle_sha256": bundle_hash, "at": exported_at, "by": user["email"]}}})
    await audit("case", case_id, "case.prosecution_exported", {"export_id": export_id, "bundle_sha256": bundle_hash, "content_hash": manifest["content_hash"], "files": len(files) + 1, "bytes": len(blob)}, user["email"])
    return Response(content=blob, media_type="application/zip", headers={"Content-Disposition": f'attachment; filename="{case["case_number"]}_prosecution_{export_id[:8]}.zip"', "X-Bundle-SHA256": bundle_hash, "X-Export-Id": export_id})


@router.get("/cases/{case_id}/prosecution-exports")
async def list_exports(case_id: str, user=Depends(get_current_user)):
    return clean(await db.prosecution_exports.find({"case_id": case_id}, {"_id": 0, "files": 0}).sort("exported_at", -1).to_list(50))


@router.get("/verify/{hash_value}")
async def verify_hash(hash_value: str):
    h = hash_value.strip().lower()
    rec = await db.prosecution_exports.find_one({"$or": [{"bundle_sha256": h}, {"content_hash": h}]}, {"_id": 0, "files": 0})
    if not rec:
        return {"verified": False, "status": "unknown_hash", "message": "No prosecution export with this hash is recorded in the Varuna Netra audit ledger."}
    await db.prosecution_exports.update_one({"id": rec["id"]}, {"$inc": {"verifications": 1}, "$set": {"last_verified_at": datetime.now(timezone.utc)}})
    return clean({"verified": True, "status": "hash_recorded", "matched": "bundle" if rec["bundle_sha256"] == h else "content", "export": rec})


@router.post("/verify")
async def verify_upload(file: UploadFile = File(...)):
    data = await file.read()
    if len(data) > 200 * 1024 * 1024:
        raise HTTPException(400, "bundle too large")
    bundle_hash = _sha(data)
    try:
        z = zipfile.ZipFile(io.BytesIO(data))
        manifest = json.loads(z.read("MANIFEST.json"))
    except Exception:  # noqa: BLE001
        return {"verified": False, "status": "invalid_bundle", "bundle_sha256": bundle_hash, "message": "Not a Varuna Netra bundle (MANIFEST.json missing or unreadable)."}
    file_checks = []
    for name, meta in manifest.get("files", {}).items():
        try:
            actual = _sha(z.read(name))
            file_checks.append({"file": name, "expected": meta["sha256"], "actual": actual, "ok": actual == meta["sha256"]})
        except KeyError:
            file_checks.append({"file": name, "expected": meta["sha256"], "actual": None, "ok": False})
    content_hash = _sha("".join(f"{n}:{f['sha256']}" for n, f in sorted(manifest.get("files", {}).items())).encode())
    rec = await db.prosecution_exports.find_one({"id": manifest.get("export_id")}, {"_id": 0, "files": 0})
    files_ok = all(c["ok"] for c in file_checks) and bool(file_checks)
    ledger_ok = bool(rec) and rec["bundle_sha256"] == bundle_hash
    content_ok = bool(rec) and rec["content_hash"] == content_hash == manifest.get("content_hash")
    status = "verified" if files_ok and ledger_ok and content_ok else "content_verified_repackaged" if files_ok and content_ok and not ledger_ok else "tampered" if rec else "unknown_export"
    if rec:
        await db.prosecution_exports.update_one({"id": rec["id"]}, {"$inc": {"verifications": 1}, "$set": {"last_verified_at": datetime.now(timezone.utc)}})
    return clean({"verified": status == "verified", "status": status, "bundle_sha256": bundle_hash, "ledger_bundle_sha256": rec["bundle_sha256"] if rec else None, "content_hash": content_hash,
                  "files": file_checks, "manifest": {k: manifest.get(k) for k in ("export_id", "case_number", "exported_at", "exporting_officer", "algorithm_versions")}, "ledger": rec})
