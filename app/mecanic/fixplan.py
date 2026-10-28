"""Stage 7 — Fix plan (doc 15): parts list + labor estimate + procedure from
the pattern library; parts priced in MAD for the local market (gap #5: the
pricing table is a bootstrap — sourced from catalogs, calibrated over time).
"""

from __future__ import annotations

from .patterns import load_parts_pricing

LABOR_MAD_PER_H = 100.0  # local garage rate — market calibration open (doc 15 stage 7)


def build_fix_plan(cause_id: str, library, verdict: dict | None = None) -> dict | None:
    pb, cause = library.find_cause(cause_id)
    if not cause:
        return None
    fix = cause.get("fix", {}) or {}
    pricing = load_parts_pricing()
    parts = []
    for p in fix.get("parts", []):
        ref = p.get("ref", "?")
        entry = pricing.get(ref) or {}
        parts.append({
            "ref": ref,
            "name_fr": entry.get("name_fr", ref),
            "qty": p.get("qty", 1),
            "price_mad_est": entry.get("price_mad"),
        })
    parts_total = sum((pt["price_mad_est"] or 0) * pt["qty"] for pt in parts
                      if pt["price_mad_est"])
    labor_h = float(fix.get("labor_h", 0))
    return {
        "cause": cause_id,
        "playbook": pb.id if pb else None,
        "parts": parts,
        "parts_total_mad_est": parts_total if parts_total else None,
        "labor_h": labor_h,
        "labor_total_mad_est": round(labor_h * LABOR_MAD_PER_H),
        "procedure": list(fix.get("procedure", [])),
        "post_repair_steps": list(fix.get("post_repair_steps", [])),
        "notes_fr": fix.get("notes_fr", ""),
        "pricing_disclaimer": ("Estimations locales indicatives (MAD) — tarif catalogue à "
                               "affiner par le marché (doc 17, écart #5)."),
    }
