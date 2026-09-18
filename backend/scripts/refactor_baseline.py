"""Baseline/compare harness for pure refactors of playbook.build_playbook and report.build_pdf.
usage: python scripts/refactor_baseline.py capture|compare"""
import asyncio
import json
import os
import re
import sys

import pymupdf

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from db import db  # noqa: E402
from playbook import playbook_for_case  # noqa: E402
from routers.cases import _bundle  # noqa: E402
from report import build_pdf  # noqa: E402

OUT = "/app/backend/tests/fixtures/refactor_baseline.json"
TS = re.compile(r"Generated \d{4}-\d{2}-\d{2} \d{2}:\d{2}Z")


async def snapshot():
    ids = [c["id"] async for c in db.cases.find({}, {"id": 1}).sort("case_number", 1)]
    pb = {cid: json.loads(json.dumps(await playbook_for_case(cid), default=str)) for cid in ids}
    pdf = {}
    for cid in ids[:6]:
        b = await _bundle(cid, "baseline@test")
        text = "".join(p.get_text() for p in pymupdf.open(stream=build_pdf(b), filetype="pdf"))
        pdf[cid] = TS.sub("Generated <ts>", text)
    return {"playbook": pb, "pdf": pdf}


async def main():
    snap = await snapshot()
    if sys.argv[1] == "capture":
        json.dump(snap, open(OUT, "w"))
        print("captured", len(snap["playbook"]), "playbooks,", len(snap["pdf"]), "pdfs")
    else:
        base = json.load(open(OUT))
        bad = [k for k in base["playbook"] if base["playbook"][k] != snap["playbook"].get(k)] + [k for k in base["pdf"] if base["pdf"][k] != snap["pdf"].get(k)]
        print("IDENTICAL" if not bad else f"DRIFT in {bad}")
        sys.exit(1 if bad else 0)


asyncio.run(main())
