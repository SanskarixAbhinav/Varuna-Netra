import io
import math
import os
from datetime import datetime, timezone

os.environ.setdefault("MPLCONFIGDIR", "/tmp/mplconfig")
from PIL import Image as PILImage
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import Image, KeepTogether, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

RANK_COLORS = ["#FF2A6D", "#FFB703", "#00B8C4", "#9D4EDD", "#38BDF8", "#10B981"]


def _fmt(v):
    if isinstance(v, datetime):
        return v.strftime("%Y-%m-%d %H:%MZ")
    if isinstance(v, str) and len(v) >= 19 and v[10] == "T":
        return v[:16].replace("T", " ") + "Z"
    return "—" if v is None else str(v)


def render_map_png(geometries: dict) -> bytes:
    import matplotlib  # lazy: avoids font-cache build at process start
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    feats = geometries.get("features", [])
    fig, ax = plt.subplots(figsize=(7.2, 5.2), dpi=130)
    fig.patch.set_facecolor("#0A0E17")
    ax.set_facecolor("#0d1522")
    for f in feats:
        g, p = f["geometry"], f["properties"]
        layer = p.get("layer")
        if layer == "corridor":
            xs, ys = zip(*g["coordinates"][0])
            ax.plot(xs, ys, color="#00F0FF", lw=0.8, ls="--", alpha=0.8)
        elif layer == "spill":
            rings = g["coordinates"] if g["type"] == "Polygon" else [r for poly in g["coordinates"] for r in poly]
            for ring in rings:
                xs, ys = zip(*ring)
                ax.fill(xs, ys, color="#FF2A6D", alpha=0.45)
                ax.plot(xs, ys, color="#FF2A6D", lw=1.4, ls="--")
    for f in feats:
        g, p = f["geometry"], f["properties"]
        col = RANK_COLORS[min((p.get("rank") or 1) - 1, len(RANK_COLORS) - 1)]
        if p.get("layer") == "track":
            xs, ys = zip(*g["coordinates"])
            ax.plot(xs, ys, color=col, lw=1.6, alpha=0.9, label=f"#{p['rank']} {p.get('vessel_name') or p['mmsi']}")
        elif p.get("layer") == "closest_fix":
            ax.scatter([g["coordinates"][0]], [g["coordinates"][1]], s=40, color=col, edgecolors="white", linewidths=0.6, zorder=5)
        elif p.get("layer") == "backprojected_centroid":
            ax.scatter([g["coordinates"][0]], [g["coordinates"][1]], s=22, facecolors="none", edgecolors=col, linewidths=1.2, zorder=5)
    ymid = sum(ax.get_ylim()) / 2
    ax.set_aspect(1 / max(math.cos(math.radians(ymid)), 0.1))
    ax.tick_params(colors="#94A3B8", labelsize=7)
    for s in ax.spines.values():
        s.set_color("#334155")
    ax.set_xlabel("Longitude", color="#94A3B8", fontsize=8)
    ax.set_ylabel("Latitude", color="#94A3B8", fontsize=8)
    ax.grid(color="#1E293B", lw=0.5)
    if ax.get_legend_handles_labels()[0]:
        ax.legend(loc="upper left", fontsize=6.5, facecolor="#111827", edgecolor="#334155", labelcolor="#F8FAFC")
    buf = io.BytesIO()
    fig.tight_layout()
    fig.savefig(buf, format="png", facecolor=fig.get_facecolor())
    plt.close(fig)
    return buf.getvalue()



class _Doc:
    """Shared styles + table helper for the evidence PDF sections."""

    def __init__(self):
        styles = getSampleStyleSheet()
        self.h1 = ParagraphStyle("h1", parent=styles["Title"], fontSize=18, alignment=0, spaceAfter=4)
        self.h2 = ParagraphStyle("h2", parent=styles["Heading2"], fontSize=12, spaceBefore=10, spaceAfter=4, textColor=colors.HexColor("#0B3B4F"))
        self.body = ParagraphStyle("b", parent=styles["BodyText"], fontSize=8.5, leading=11)
        self.small = ParagraphStyle("s", parent=self.body, fontSize=7.5, leading=9.5, textColor=colors.HexColor("#475569"))
        self.mono = ParagraphStyle("m", parent=self.body, fontName="Courier", fontSize=7.5, leading=9.5)
        self.W = A4[0] - 32 * mm

    def bold(self, name, **kw):
        return ParagraphStyle(name, parent=self.body, fontName="Helvetica-Bold", **kw)

    def table(self, rows, widths=None, header=True):
        t = Table([[Paragraph(str(c), self.body) if not isinstance(c, Paragraph) else c for c in r] for r in rows], colWidths=widths, repeatRows=1 if header else 0)
        st = [("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#CBD5E1")), ("VALIGN", (0, 0), (-1, -1), "TOP"),
              ("LEFTPADDING", (0, 0), (-1, -1), 4), ("RIGHTPADDING", (0, 0), (-1, -1), 4), ("TOPPADDING", (0, 0), (-1, -1), 2), ("BOTTOMPADDING", (0, 0), (-1, -1), 2)]
        if header:
            st += [("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#E2E8F0"))]
        t.setStyle(TableStyle(st))
        return t


def _wind(v, label="m/s"):
    return f"{v['speed_ms']} {label} @ {v['direction_deg']}°" if v else "—"


def _sec_header(d, bundle, case):
    return [Paragraph(f"Evidence Package — {case['case_number']}", d.h1),
            Paragraph(f"Generated {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')}Z by Varuna Netra · {bundle.get('generated_by', 'system')}", d.small),
            Paragraph(f"<b>Disclaimer.</b> {bundle['disclaimer']}", d.small), Spacer(1, 6)]


def _sec_summary(d, case):
    W = d.W
    pj = case.get("primary_jurisdiction")
    return [Paragraph("1. Case summary", d.h2), d.table([
        ["Case number", case["case_number"], "Attribution status", case["attribution_status"]],
        ["Automated status", _fmt(case.get("automated_status")), "Confidence band", _fmt(case.get("confidence_band"))],
        ["Review state", case["review_state"], "Confirmed vessel MMSI", _fmt(case.get("confirmed_vessel_mmsi"))],
        ["Acquisition (UTC)", _fmt(case["acquisition_time"]), "Result version", str(case.get("latest_result_version", 0))],
        ["Degraded (no drift inputs)", _fmt(case.get("degraded")), "Case status", case["status"]],
        ["Primary jurisdiction", f"{pj['code']} — {pj['authority']}" if pj else "unassigned",
         "Other zones intersected", ", ".join(z["code"] for z in (case.get("jurisdictions") or []) if not pj or z["code"] != pj["code"]) or "—"],
    ], [W * 0.2, W * 0.3, W * 0.22, W * 0.28], header=False)]


def _sec_observation(d, bundle, spill, scene):
    W, refs = d.W, bundle["source_references"]
    return [Paragraph("2. Spill observation & source references", d.h2), d.table([
        ["Observation ID", spill["id"], "Source", spill["source"]],
        ["Processing version", spill["processing_version"], "Detection confidence", f"{spill['detection_confidence']:.2f}"],
        ["Estimated area", f"{spill['estimated_area_km2']} km²", "Quality flags", ", ".join(spill.get("quality_flags") or []) or "none"],
        ["Centroid (lat, lon)", f"{spill['centroid']['coordinates'][1]:.4f}, {spill['centroid']['coordinates'][0]:.4f}", "Estimated age", _fmt(spill.get("estimated_age_hours"))],
        ["Scene", f"{scene['provider']} · {scene['provider_scene_id']}" if scene else "— (external polygon)", "Storage reference", _fmt(refs.get("storage_ref"))],
        ["Wind (FROM)", _wind(spill.get("wind")), "Current (TOWARD)", _wind(spill.get("current"))],
        ["Environment source", _fmt((spill.get("environment") or {}).get("source")), "AIS fixes referenced", str(len(refs.get("ais_fix_ids", [])))],
    ], [W * 0.2, W * 0.3, W * 0.22, W * 0.28], header=False)]


def _sec_map(d, bundle):
    el = [Paragraph("3. Map snapshot", d.h2)]
    try:
        png = render_map_png(bundle["geometries"])
        el.append(KeepTogether([Image(io.BytesIO(png), width=d.W * 0.9, height=d.W * 0.9 * 5.2 / 7.2),
                                Paragraph("Crimson dashed: spill polygon · cyan dashed: search corridor · coloured lines: candidate AIS tracks (by rank) · filled dots: closest approach · rings: drift back-projection of slick centroid", d.small)]))
    except Exception as e:  # noqa: BLE001
        el.append(Paragraph(f"Map rendering failed: {e}", d.small))
    return el


def _candidate_breakdown(d, c):
    W = d.W
    el = [Spacer(1, 6), Paragraph(f"#{c['rank']} {c.get('vessel_name') or c['mmsi']} — factor breakdown", d.bold("h4"))]
    frows = [["Factor", "Score", "Weight", "Contribution", "Detail"]] + [[k, f"{f['score']:.3f}", f["weight"], f"{f['contribution']:.3f}", f["detail"]] for k, f in c["factors"].items()]
    el.append(d.table(frows, [W * 0.14, W * 0.09, W * 0.09, W * 0.12, W * 0.56]))
    if c.get("notes"):
        el.append(Paragraph("Notes: " + " · ".join(c["notes"]), d.small))
    if c.get("ais_flags"):
        el.append(Paragraph("AIS flags: " + ", ".join(c["ais_flags"]), d.small))
    segs = [s for s in c["evidence"].get("gap_segments") or [] if s["interpolated_points"] or s["spoof_suspect"]]
    if segs:
        el.append(Paragraph("AIS gaps: " + " · ".join(f"{s['gap_hours']}h ({s['distance_km']} km, needs {s['required_speed_kn']} kn vs max {s['vessel_max_kn']}) → " + ("SPOOF SUSPECT" if s["spoof_suspect"] else f"{s['interpolated_points']} interpolated pts") for s in segs), d.small))
    return el


def _sec_calculations(d, calc):
    W, env = d.W, calc.get("environment") or {}
    el = [Paragraph("4. Correlation calculations", d.h2), d.table([
        ["Algorithm version", calc["algorithm_version"], "Input hash", Paragraph(calc["input_hash"], d.mono)],
        ["Corridor / window", f"{calc['params']['corridor_km']} km · −{calc['params']['window_hours_before']}h / +{calc['params']['window_hours_after']}h", "Weights", ", ".join(f"{k}={v}" for k, v in calc["params"]["weights"].items())],
        ["Wind used", _wind(env.get("wind")) if env.get("wind") else "none", "Current used", _wind(env.get("current")) if env.get("current") else "none"],
        ["Degraded", str(calc["degraded"]), "Spill axis bearing", f"{calc['spill_axis_bearing']}°"],
    ], [W * 0.2, W * 0.3, W * 0.18, W * 0.32], header=False)]
    dm, gf = calc.get("drift_model"), calc.get("gap_fill") or {}
    if dm:
        el += [Spacer(1, 4), Paragraph(f"Backward drift model {dm['version']}: {dm['hours']:.0f} h Lagrangian back-track at {dm['drift_speed_ms']} m/s (3% wind + current), hourly steps; "
                                       f"origin envelope = {dm['k_sigma']}σ region growing to {dm['sigma_km'][-1] * dm['k_sigma']:.1f} km (diffusivity {dm['diffusivity_m2s']} m²/s, velocity uncertainty {dm['velocity_uncertainty_frac'] * 100:.0f}%); "
                                       f"most-likely origin window {dm['likely_window_hours'][0]}–{dm['likely_window_hours'][1]} h before acquisition. Drift factor = exp(−z²/2) with z = distance/σ at the fix time.", d.small)]
    if gf.get("enabled"):
        el.append(Paragraph(f"AIS gap filling {gf['version']}: gaps > {gf['threshold_min']} min dead-reckoned from last SOG/COG (blended to the next real fix); synthetic points are dashed on the map, excluded from continuity, and penalise spatial score proportionally to gap length; kinematically impossible transits are flagged spoof_suspect and NOT interpolated.", d.small))
    el += [Spacer(1, 6), Paragraph("Ranked candidates", d.bold("h3", fontSize=10, spaceAfter=3))]
    rows = [["#", "Vessel", "MMSI / IMO", "Type", "Score", "Status", "Dist km", "Gap h", "Fixes"]]
    for c in calc["candidates"]:
        rows.append([c["rank"], c.get("vessel_name") or "UNKNOWN", f"{c['mmsi']}{' / ' + c['imo'] if c.get('imo') else ''}", c.get("vessel_type") or "—", f"{c['score']:.3f}", c["status"],
                     c["evidence"]["distance_km"], c["evidence"]["time_gap_hours"], c["evidence"]["fix_count"]])
    el.append(d.table(rows, [W * 0.04, W * 0.2, W * 0.18, W * 0.1, W * 0.08, W * 0.17, W * 0.08, W * 0.07, W * 0.08]))
    for c in calc["candidates"]:
        el += _candidate_breakdown(d, c)
    el += [Spacer(1, 6), Paragraph("Processing log", d.bold("h3b"))]
    el += [Paragraph(f"{l['t'][11:19]} [{l['level']}] {l['msg']}", d.mono) for l in calc.get("processing_log", [])]
    return el


def _sec_versions(d, bundle):
    W = d.W
    vrows = [["Version", "Created", "Algorithm", "Overall status", "Degraded", "Input hash"]]
    for v in bundle.get("result_versions", []):
        vrows.append([v["version"], _fmt(v["created_at"]), v["algorithm_version"], v["overall_status"], str(v["degraded"]), Paragraph(v["input_hash"][:24] + "…", d.mono)])
    return [Paragraph("5. Result versions", d.h2), d.table(vrows, [W * 0.08, W * 0.17, W * 0.14, W * 0.2, W * 0.1, W * 0.31])]


def _sec_reviews(d, bundle):
    W = d.W
    el = [Paragraph("6. Analyst decisions (immutable)", d.h2)]
    if bundle.get("reviews"):
        rrows = [["When", "Analyst", "Decision", "Vessel", "Reason codes", "Notes"]]
        for r in bundle["reviews"]:
            rrows.append([_fmt(r["created_at"]), f"{r.get('analyst')}{' (' + r['analyst_role'] + ')' if r.get('analyst_role') else ''}", r["decision"], _fmt(r.get("vessel_mmsi")), ", ".join(r.get("reason_codes") or []), r.get("notes") or ""])
        el.append(d.table(rrows, [W * 0.14, W * 0.18, W * 0.1, W * 0.1, W * 0.2, W * 0.28]))
    else:
        el.append(Paragraph("No analyst decisions recorded.", d.body))
    if bundle.get("detector_feedback"):
        el.append(Paragraph("Detector feedback (human-in-the-loop validation, immutable)", d.bold("h3c", spaceBefore=6)))
        drows = [["When", "Analyst", "Verdict", "FP reason", "Detector version", "Notes"]]
        for f in bundle["detector_feedback"]:
            drows.append([_fmt(f["created_at"]), f"{f['user_email']} ({f['user_role']})", f["verdict"], f.get("reason") or "—", f["detector_version"], f.get("notes") or ""])
        el.append(d.table(drows, [W * 0.14, W * 0.2, W * 0.12, W * 0.12, W * 0.18, W * 0.24]))
    return el


def _sec_audit(d, bundle):
    W = d.W
    arows = [["When", "Actor", "Action", "Entity", "Payload"]]
    for e in bundle.get("audit_history", [])[-60:]:
        arows.append([_fmt(e["created_at"]), e["actor"], e["action"], f"{e['entity_type']} {e['entity_id'][:8]}", Paragraph(str(e.get("payload", {}))[:220], d.small)])
    return [Paragraph("7. Audit history", d.h2), d.table(arows, [W * 0.14, W * 0.18, W * 0.16, W * 0.16, W * 0.36])]


def _attachment_image(d, img):
    if not img.get("bytes"):
        return [Paragraph(f"{img['caption']} — {img['meta']}", d.small)]
    try:
        pil = PILImage.open(io.BytesIO(img["bytes"]))
        ratio = pil.height / pil.width
        h = min(d.W * 0.9 * ratio, 150 * mm)
        return [Spacer(1, 6), KeepTogether([Image(io.BytesIO(img["bytes"]), width=h / ratio, height=h), Paragraph(f"<b>{img['caption']}</b> · {img['meta']}", d.small)])]
    except Exception as e:  # noqa: BLE001
        return [Paragraph(f"{img['caption']} — image could not be rendered: {e}", d.small)]


def _sec_attachments(d, bundle):
    W = d.W
    frows = [["Uploaded", "Kind", "File", "Caption", "By", "Size"]]
    for a in bundle["attachments"]:
        frows.append([_fmt(a["created_at"]), a["kind"], a["original_filename"], a.get("caption") or "—", a["uploaded_by"], f"{a['size'] / 1024:.0f} KB"])
    el = [Paragraph("8. Attached source imagery & evidence files", d.h2), d.table(frows, [W * 0.14, W * 0.12, W * 0.24, W * 0.24, W * 0.18, W * 0.08])]
    for img in bundle.get("attachment_images", []):
        el += _attachment_image(d, img)
    return el


def _sec_playbook(d, pb):
    W, inp = d.W, pb["inputs"]
    el = [Paragraph("9. Remediation playbook — ADVISORY", d.h2), Paragraph(pb["disclaimer"], d.small),
          d.table([["Area km²", inp["area_km2"], "Volume est. (thin/thick) t", f"{inp['estimated_volume_tonnes']['thin']} / {inp['estimated_volume_tonnes']['thick']}"],
                   ["Coast distance km", f"{inp['coast_distance_km']} ({inp['coast_note']})", "Depth class", inp["depth_class"]],
                   ["Wind / sea state", f"{inp['wind_ms']} m/s · {inp['sea_state']}", "Drift", f"{inp['drift_speed_ms']} m/s → {inp['drift_bearing_deg']}° · ETA coast {inp['eta_to_coast_hours']} h"]], [W * 0.18, W * 0.32, W * 0.2, W * 0.3], header=False)]
    for t in pb["tiers"]:
        el.append(Paragraph(f"Tier {t['tier']} — {t['title']} ({t['priority']})", d.bold("h4b", spaceBefore=4)))
        el += [Paragraph("• " + a, d.small) for a in t.get("actions", [])]
        el += [Paragraph(f"• {k.replace('_', ' ')}: {'SUITABLE' if t[k]['suitable'] else 'NOT SUITABLE'} — {t[k]['reason']}", d.small) for k in ("dispersant", "in_situ_burning", "bioremediation") if k in t]
        el += [Paragraph(f"• {s['when'][:16]}Z — {s['task']}", d.small) for s in t.get("schedule", [])]
    if pb["tactical_coordinates"]:
        el.append(d.table([["ID", "Lat", "Lon", "Role"]] + [[c["id"], c["lat"], c["lon"], c["role"]] for c in pb["tactical_coordinates"]], [W * 0.12, W * 0.14, W * 0.14, W * 0.6]))
    return el


def _sec_vulnerability(d, v, case):
    W = d.W
    el = [Paragraph("10. Shoreline vulnerability — ADVISORY forward projection", d.h2), Paragraph(v["disclaimer"], d.small)]
    icg = case.get("icg")
    if icg:
        el.append(Paragraph(f"Alert routing: {icg['name']} · {icg['region']} (HQ {icg['region_hq']}) — {icg['note']}", d.small))
    if v["sites"]:
        rows = [["Site", "Type", "State", "Dist km", "ETA h", "Priority", "Source"]] + [[s["name"][:48], s["type_label"], s.get("state") or "—", s["distance_km"], s["eta_hours"] if s["eta_hours"] is not None else "—", s["priority"], s.get("origin", "curated")] for s in v["sites"][:25]]
        el.append(d.table(rows, [W * 0.34, W * 0.16, W * 0.14, W * 0.08, W * 0.08, W * 0.1, W * 0.1]))
    else:
        el.append(Paragraph("No sensitive sites inside the 72 h forward envelope / search radius.", d.small))
    return el


def build_pdf(bundle: dict) -> bytes:
    """Compose the evidence package from independent sections (1–10)."""
    case, refs = bundle["case"], bundle["source_references"]
    spill, scene, calc = refs["spill_observation"], refs.get("scene"), bundle.get("calculations")
    d = _Doc()
    el = _sec_header(d, bundle, case) + _sec_summary(d, case) + _sec_observation(d, bundle, spill, scene) + _sec_map(d, bundle)
    if calc:
        el += _sec_calculations(d, calc)
    el += _sec_versions(d, bundle) + _sec_reviews(d, bundle) + _sec_audit(d, bundle)
    if bundle.get("attachments"):
        el += _sec_attachments(d, bundle)
    if bundle.get("playbook"):
        el += _sec_playbook(d, bundle["playbook"])
    if bundle.get("vulnerability"):
        el += _sec_vulnerability(d, bundle["vulnerability"], case)
    buf = io.BytesIO()
    SimpleDocTemplate(buf, pagesize=A4, leftMargin=16 * mm, rightMargin=16 * mm, topMargin=14 * mm, bottomMargin=14 * mm,
                      title=f"Evidence package {case['case_number']}", author="Varuna Netra").build(el)
    return buf.getvalue()
