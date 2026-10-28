"""Dataset soak harness (doc 17 §4.2, doc 08 §1): run the funnel over every
trip of a public dataset and measure what 15 scripted scenarios can't —

  * flag rate across a presumably-mostly-healthy real fleet (the §4.2
    false-flag metric — each flagged trip listed for manual review, because
    real faults live inside hundreds of thousands of real miles),
  * trim distributions vs our thresholds (±10/±15 — calibrated against
    reality, not against our own emulator),
  * MAF-vs-RPM rationality ratios vs the 0.75/1.35 thresholds,
  * window derivation on non-scripted data (the windows must come from the
    stream, never labels — doc 10 §1.1).

VED (University of Michigan): 383 vehicles, 1 Hz, the only large public set
with fuel trims + MAF + RPM. Part 1 alone = 22 week files, ~1.28 GB.
Static metadata (engine displacement, model) is joined from the dataset's
xlsx — parsed with stdlib zip+xml, no openpyxl dependency.
"""

from __future__ import annotations

import csv
import re
import xml.etree.ElementTree as ET
import zipfile
from collections import defaultdict
from pathlib import Path

from .funnel import maf_expected_idle, run_funnel, total_trims, window_map
from .ledger import fold

# exact VED header -> canonical pid (VED headers are stable; no fuzzy match)
VED_COLUMNS = {
    "Engine RPM[RPM]": "RPM",
    "Vehicle Speed[km/h]": "SPEED",
    "MAF[g/sec]": "MAF",
    "Absolute Load[%]": "LOAD",
    "Short Term Fuel Trim Bank 1[%]": "STFT_B1",
    "Short Term Fuel Trim Bank 2[%]": "STFT_B2",
    "Long Term Fuel Trim Bank 1[%]": "LTFT_B1",
    "Long Term Fuel Trim Bank 2[%]": "LTFT_B2",
}

MIN_TRIP_ROWS = 120          # >= ~2 min of driving to build windows at all
MIN_IDLE_SAMPLES = 8         # the funnel's own rule-robustness thresholds


# -- static metadata (xlsx via stdlib zip+xml — no openpyxl) -------------------

def parse_ved_static(path: str | Path) -> dict[str, dict]:
    """{VehId: {make, model, year, displacement_l, vtype}} from the VED xlsx."""
    NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
    z = zipfile.ZipFile(str(path))
    strings = []
    if "xl/sharedStrings.xml" in z.namelist():
        ss = ET.fromstring(z.read("xl/sharedStrings.xml"))
        strings = ["".join(t.text or "" for t in si.iter(NS + "t")) for si in ss]
    sheet = ET.fromstring(z.read("xl/worksheets/sheet1.xml"))
    rows: list[list[str]] = []
    for row in sheet.iter(NS + "row"):
        vals = []
        for c in row:
            v = c.find(NS + "v")
            if v is None:
                vals.append("")
                continue
            vals.append(strings[int(v.text)] if c.get("t") == "s" else v.text)
        rows.append(vals)
    if len(rows) < 2:
        return {}
    header = [str(h or "").strip().lower() for h in rows[0]]

    def col(*needles: str) -> int | None:
        for i, h in enumerate(header):
            if all(n in h for n in needles):
                return i
        return None

    i_id = col("vehid")
    i_make = col("make")
    i_model = col("class") if col("class") is not None else col("model")
    i_year, i_type = col("year"), col("type")
    i_engine = col("engine") if col("engine") else None
    out: dict[str, dict] = {}
    for r in rows[1:]:
        get = lambda i: ((r[i].strip() if i is not None and i < len(r) else "") or "").replace("NO DATA", "")  # noqa: E731
        vid = get(i_id)
        if not vid:
            continue
        disp = None
        eng = get(i_engine)
        m = re.search(r"(\d+(?:\.\d+)?)\s*[- ]?\s*(?:l\b|liter|litre)", eng, re.I)
        if m:
            disp = float(m.group(1))
        out[vid] = {"make": get(i_make), "model": get(i_model),
                    "year": get(i_year), "vtype": get(i_type),
                    "displacement_l": disp}
    return out


# -- trip iteration (streaming, reopen-safe) -----------------------------------

def iter_ved_trips(path: str | Path):
    """Yield (key, {pid: [(ts, value), ...]}) per (VehId, DayNum, Trip).

    Compact numeric storage only. Reopen-safe: if a trip key reappears
    non-contiguously (unsorted files), its rows merge into the same bucket.
    """
    with open(path, newline="", encoding="utf-8", errors="replace") as fh:
        reader = csv.reader(fh)
        try:
            header = next(reader)
        except StopIteration:
            return
        idx = {name: i for i, name in enumerate(header)}
        cols = [(pid, idx[name]) for name, pid in VED_COLUMNS.items() if name in idx]
        i_veh, i_day, i_trip = idx.get("VehId"), idx.get("DayNum"), idx.get("Trip")
        i_ts = idx.get("Timestamp(ms)")
        if not (i_veh is not None and i_trip is not None):
            return
        buckets: dict[tuple, dict] = {}

        def bucket(key):
            b = buckets.get(key)
            if b is None:
                b = buckets[key] = {pid: [] for pid, _ in cols}
            return b

        for row in reader:
            try:
                key = (row[i_veh], row[i_day] if i_day is not None else "",
                       row[i_trip])
                ts = float(row[i_ts]) / 1000.0 if i_ts is not None else 0.0
            except (IndexError, ValueError):
                continue
            b = bucket(key)
            for pid, i in cols:
                if i >= len(row):
                    continue
                raw = row[i]
                if raw in ("", "NaN"):
                    continue
                try:
                    b[pid].append((ts, float(raw)))
                except ValueError:
                    continue
        for key, series in buckets.items():
            n = max((len(v) for v in series.values()), default=0)
            if n >= MIN_TRIP_ROWS:
                yield key, series


# -- one trip through the funnel -----------------------------------------------

def trip_events(series: dict) -> list[dict]:
    """Synthetic pid_sample event stream for one trip (foldable)."""
    events = []
    seq = 0
    for pid, pts in series.items():
        for ts, v in pts:
            events.append({"seq": seq, "ts": round(ts, 3), "type": "pid_sample",
                           "source": "ved", "data": {"pid": pid, "name": pid,
                                                     "value": v, "unit": ""}})
            seq += 1
    events.sort(key=lambda e: e["ts"])
    for i, ev in enumerate(events):
        ev["seq"] = i
    return events


def run_ved_trip(key, series, vehicle: dict) -> dict:
    """Fold a synthetic event stream for one trip, run the funnel, extract
    the soak metrics. No ledger file — pure in-memory, discarded after."""
    events = trip_events(series)
    state = fold(events)
    windows, flags, _samples = run_funnel(state)
    wmap = window_map(windows)

    idle_t1 = total_trims(wmap, 1, "idle")
    idle_t2 = total_trims(wmap, 2, "idle")
    rpm_idle = wmap.get(("RPM", "idle"))
    maf_idle = wmap.get(("MAF", "idle"))
    load_idle = wmap.get(("LOAD", "idle"))
    maf_ratio = None
    if rpm_idle and maf_idle and load_idle and vehicle.get("displacement_l"):
        expected = maf_expected_idle(rpm_idle["mean"], float(vehicle["displacement_l"]),
                                      load_idle["mean"])
        if expected > 0 and maf_idle["n"] >= MIN_IDLE_SAMPLES:
            maf_ratio = round(maf_idle["mean"] / expected, 3)

    return {
        "key": key,
        "vehicle": vehicle,
        "n_samples": len(events),
        "duration_s": round(events[-1]["ts"] - events[0]["ts"], 1) if events else 0,
        "flags": sorted({f["rule"] for f in flags}),
        "severities": [f["severity"] for f in flags],
        "idle_trim_b1": idle_t1,
        "idle_trim_b2": idle_t2,
        "idle_rpm_std": (rpm_idle or {}).get("stdev"),
        "maf_idle_ratio": maf_ratio,
    }


# -- the soak ------------------------------------------------------------------

def diagnose_vehicle(paths, veh_id: str, static_files=None,
                     max_trips: int = 6) -> list[dict]:
    """The 'bring your own log' flow on REAL data (doc 14 §6.5): the full
    stack — funnel -> triage -> ranker — on one flagged vehicle's trips.
    The soak finds the car; this asks the agent what's wrong with it."""
    from .patterns import PatternLibrary
    from .ranker import Ranker
    from .triage import Triage

    meta: dict[str, dict] = {}
    for sf in static_files or []:
        meta.update(parse_ved_static(sf))
    lib = PatternLibrary.load()
    ranker = Ranker(lib)
    triage = Triage()
    out: list[dict] = []
    for path in paths:
        if len(out) >= max_trips:
            break
        for key, series in iter_ved_trips(path):
            if len(out) >= max_trips:
                break
            if str(key[0]) != str(veh_id):
                continue
            veh = meta.get(str(veh_id), {})
            events = trip_events(series)
            state = fold(events)
            windows, flags, _ = run_funnel(state)
            # merge the funnel's derived evidence into the folded state —
            # exactly what session.analyze() does for a live dossier
            for w in windows:
                state.windows.append({**w, "_ev": 0, "_ts": 0})
            for f in flags:
                state.flags.append({**f, "_ev": 0, "_ts": 0})
            out.append({
                "key": key,
                "vehicle": veh,
                "duration_s": round(events[-1]["ts"] - events[0]["ts"], 1) if events else 0,
                "flags": sorted({f["rule"] for f in flags}),
                "findings": [{"title": f["title"], "detail": f["detail"],
                              "severity": f.get("severity", "info")}
                             for f in triage.analyze(state)],
                "ranking": ranker.analyze(state),
            })
    return out


def _percentile(values: list[float], p: float) -> float:
    if not values:
        return float("nan")
    s = sorted(values)
    k = min(len(s) - 1, max(0, int(round(p / 100 * (len(s) - 1)))))
    return s[k]


def soak(paths, static_files=None, limit: int | None = None) -> dict:
    """Run every trip through the funnel; return the aggregate report dict."""
    meta: dict[str, dict] = {}
    for sf in static_files or []:
        meta.update(parse_ved_static(sf))

    n_trips = n_flagged = n_skipped = 0
    rule_counts: dict[str, int] = defaultdict(int)
    trim_b1: list[float] = []
    trim_b2: list[float] = []
    maf_ratios: list[float] = []
    idle_sigmas: list[float] = []
    vehicles: set[str] = set()
    flagged: list[dict] = []

    for path in paths:
        for key, series in iter_ved_trips(path):
            if limit and n_trips + n_skipped >= limit:
                break
            veh = meta.get(str(key[0]), {})
            res = run_ved_trip(key, series, veh)
            if res["n_samples"] < 60:
                n_skipped += 1
                continue
            n_trips += 1
            vehicles.add(str(key[0]))
            if res["idle_rpm_std"] is not None:
                idle_sigmas.append(res["idle_rpm_std"])
            if res["flags"]:
                n_flagged += 1
                for r in res["flags"]:
                    rule_counts[r] += 1
                flagged.append(res)
            if res["idle_trim_b1"] is not None:
                trim_b1.append(res["idle_trim_b1"])
            if res["idle_trim_b2"] is not None:
                trim_b2.append(res["idle_trim_b2"])
            if res["maf_idle_ratio"]:
                maf_ratios.append(res["maf_idle_ratio"])
        if limit and n_trips + n_skipped >= limit:
            break

    def pct(vals, p):
        return round(_percentile(vals, p), 2) if vals else None

    flagged.sort(key=lambda r: (-len(r["flags"]), -(r["idle_trim_b1"] or 0)))
    return {
        "trips": n_trips,
        "skipped": n_skipped,
        "vehicles": len(vehicles),
        "flagged": n_flagged,
        "flag_rate_pct": round(n_flagged * 100 / n_trips, 2) if n_trips else None,
        "rule_counts": dict(sorted(rule_counts.items(), key=lambda kv: -kv[1])),
        "trim_b1_pct": {p: pct(trim_b1, p) for p in (5, 50, 95, 99)},
        "trim_b2_pct": {p: pct(trim_b2, p) for p in (5, 50, 95, 99)},
        "n_trim_trips": len(trim_b1),
        "idle_sigma_pct": {p: pct(idle_sigmas, p) for p in (50, 95, 99)},
        "n_idle_trips": len(idle_sigmas),
        "maf_ratio_pct": {p: pct(maf_ratios, p) for p in (1, 5, 50, 95, 99)},
        "n_maf_trips": len(maf_ratios),
        "flagged_trips": flagged,
    }


def render_soak_report(agg: dict, sources: list[str]) -> str:
    L: list[str] = []
    L.append("# Soak VED — funnel sur données réelles (doc 17 §4.2)")
    L.append("")
    L.append("**Sources:** " + ", ".join(Path(s).name for s in sources))
    L.append("")
    L.append(f"- Trajets traités: **{agg['trips']}** (ignorés <2 min: {agg['skipped']}) "
              f"sur **{agg['vehicles']} véhicules**")
    rate = agg["flag_rate_pct"]
    L.append(f"- Taux de flag: **{rate}%** des trajets déclenchent ≥1 règle "
             f"({agg['flagged']}/{agg['trips']}) — un agent honnête doit être silencieux "
             f"sur un moteur sain; chaque trajet flaggé est listé pour relecture manuelle "
             f"(des vraies pannes vivent dans ces données).")
    L.append("")
    L.append("## Règles déclenchées")
    if agg["rule_counts"]:
        for rule, n in agg["rule_counts"].items():
            L.append(f"- `{rule}`: {n} trajets")
    else:
        L.append("- aucune")
    L.append("")
    L.append("## Calibration des seuils vs le parc réel")
    t = agg["trim_b1_pct"]
    if t.get(50) is not None:
        L.append(f"- Trims totaux à vide (B1): p5={t[5]} · p50={t[50]} · "
                 f"p95={t[95]} · p99={t[99]} — nos seuils: ±10% (fuite/débitmètre), "
                 f"±15% (confirmé pauvre). Le p95/p99 réel situe notre marge de "
                 f"déclenchement.")
    m = agg["maf_ratio_pct"]
    if m.get(50) is not None:
        L.append(f"- Ratio MAF/attendu au ralenti: p1={m[1]} · p5={m[5]} · p50={m[50]} · "
                 f"p95={m[95]} · p99={m[99]} — seuils calibrés 0.75/1.35 "
                 f"({agg['n_maf_trips']} trajets évaluables, dépend du ralenti capturé).")
    s = agg.get("idle_sigma_pct") or {}
    if s.get(50) is not None:
        L.append(f"- σ RPM au ralenti: p50={s[50]} · p95={s[95]} · p99={s[99]} — "
                 f"seuil `idle_unstable` calibré à 100 (p95 du parc réel).")
    L.append("")
    L.append(f"## Trajets flaggués ({len(agg['flagged_trips'])} — relecture manuelle)")
    L.append("")
    L.append("| véhicule | modèle | flags | trims B1 | trims B2 | MAF ratio | durée |")
    L.append("|---|---|---|---|---|---|---|")
    for r in agg["flagged_trips"][:40]:
        v = r["vehicle"] or {}
        L.append(f"| {r['key'][0]} | {(v.get('make','') + ' ' + v.get('model','')).strip() or '?'}"
                 f" | {', '.join(r['flags'])} | {r['idle_trim_b1']} | {r['idle_trim_b2']}"
                 f" | {r['maf_idle_ratio']} | {r['duration_s']} s |")
    if len(agg["flagged_trips"]) > 40:
        L.append(f"| … | (+{len(agg['flagged_trips']) - 40} autres) | | | | | |")
    L.append("")
    return "\n".join(L)
