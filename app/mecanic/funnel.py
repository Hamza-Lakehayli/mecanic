"""The attention funnel (doc 16) — token efficiency without diagnostic loss.

    RAW LEDGER (all events, stored — never deleted)
      L0 · delta-sampling & flatline suppression
      L1 · windowed features + deterministic rules/SPC (zero tokens, certain)
      L2 · anomaly models — INTENTIONALLY ABSENT in Phase 1 (zero-model
           decision, doc 17 canon); the interface below leaves the slot.
      L3 · digest builder (~1–3K tokens)
      L4 · LLM — reads only the digest, drills back down via tools.

The LLM never reads the stream. It reads structured digests, and it queries
the stream through drill-down tools when it needs more (get_samples /
get_mode06 / replay_segment). Windows are derived from the data itself
(idle / cruise / rev_hold / load / warm_up / snap — doc 10 §1.1), never from
scenario labels. Rules are recall-biased: a false flag costs a few tokens,
a missed signal costs a diagnosis.
"""

from __future__ import annotations

import json
import math
from collections import defaultdict

# ---------------------------------------------------------------------------
# L0 — acquisition compression (a lens over the ledger, never a garbage bin)
# ---------------------------------------------------------------------------

def delta_compress(samples: list[dict], eps: float = 1e-9) -> list[dict]:
    """Flatline suppression: keep the first sample of each pid and every
    change (kills 80–95% of a flat stream; the raw ledger keeps everything)."""
    out, last = [], {}
    for s in samples:
        pid = s["pid"]
        prev = last.get(pid)
        if prev is None or abs(float(s["value"]) - prev) > eps:
            out.append(s)
        last[pid] = float(s["value"])
    return out


# ---------------------------------------------------------------------------
# Windowed features (per PID, per operating window derived from the data)
# ---------------------------------------------------------------------------

WINDOW_ORDER = ("warm_up", "idle", "rev_hold", "cruise", "load", "snap", "other")


def _series_by_pid(samples: list[dict]) -> dict[str, list[tuple[float, float]]]:
    series: dict[str, list[tuple[float, float]]] = defaultdict(list)
    for s in samples:
        series[s["pid"]].append((float(s["ts"]), float(s["value"])))
    for v in series.values():
        v.sort()
    return series


def _value_at(series: dict[str, list], pid: str, ts: float, tol: float = 2.5):
    pts = series.get(pid)
    if not pts:
        return None
    lo, hi, best = 0, len(pts) - 1, None
    while lo <= hi:
        mid = (lo + hi) // 2
        if pts[mid][0] <= ts:
            best = mid
            lo = mid + 1
        else:
            hi = mid - 1
    if best is None:
        return None
    if abs(pts[best][0] - ts) <= tol:
        return pts[best][1]
    nxt = pts[best + 1] if best + 1 < len(pts) else None
    if nxt and abs(nxt[0] - ts) <= tol:
        return nxt[1]
    return None


def classify_samples(samples: list[dict]) -> list[dict]:
    """Tag each sample with an operating window, derived from the stream
    (RPM/speed/load/coolant), never from ground-truth labels."""
    series = _series_by_pid(samples)
    rpm_prev = None  # (ts, value) for snap detection
    for s in samples:
        ts = float(s["ts"])
        rpm = _value_at(series, "RPM", ts)
        speed = _value_at(series, "SPEED", ts)
        load = _value_at(series, "LOAD", ts)
        ect = _value_at(series, "ECT", ts)

        win = "other"
        if rpm is None:
            win = "other"
        # engine-must-be-running gate (VED soak, Oct 22 2026): hybrid
        # stop-start inserts rpm≈0 periods into stopped-car samples — without
        # the gate they poison idle windows (σ explodes, MAF mean collapses).
        elif ect is not None and ect < 70 and (speed is None or speed <= 5) and 300 <= rpm <= 1200:
            win = "warm_up"
        elif speed is not None and speed >= 30 and 1300 <= rpm <= 3200:
            win = "cruise"
        elif load is not None and load >= 55:
            win = "load"
        elif speed is not None and speed <= 5 and 1900 <= rpm <= 3200:
            win = "rev_hold"
        elif speed is not None and speed <= 5 and 300 <= rpm <= 1100:
            win = "idle"
        elif rpm > 3200:
            win = "load" if (load is not None and load >= 50) else "other"

        if rpm is not None and rpm_prev is not None:
            dt = ts - rpm_prev[0]
            if dt > 0 and (rpm - rpm_prev[1]) / dt > 1200:  # rpm/s
                win = "snap"
        if rpm is not None:
            rpm_prev = (ts, rpm)
        s["window"] = win
    return samples


def _slope(points: list[tuple[float, float]]) -> float:
    """Ordinary least-squares slope (per second), robust to short windows."""
    n = len(points)
    if n < 2:
        return 0.0
    t0 = points[0][0]
    xs = [p[0] - t0 for p in points]
    ys = [p[1] for p in points]
    mx = sum(xs) / n
    my = sum(ys) / n
    var = sum((x - mx) ** 2 for x in xs)
    if var < 1e-9:
        return 0.0
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / var


def ewma(values: list[float], lam: float = 0.3) -> list[float]:
    """EWMA control-chart smoothing (doc 16 L1 SPC helper)."""
    out, z = [], None
    for v in values:
        z = v if z is None else lam * v + (1 - lam) * z
        out.append(z)
    return out


def build_windows(samples: list[dict]) -> list[dict]:
    """Per (pid, window): n / min / max / mean / stdev / slope. These are the
    L1 feature vectors (and the exact inputs iForest would get in Phase 2).

    Classification runs over the RAW co-temporal stream (a suppressed flatline
    reference would orphan later samples — see test_maf discriminator):
    delta-compression is a *token-cost* lens for digests/CSV, not a prerequisite
    for local arithmetic (doc 16: the funnel decides what deserves tokens)."""
    tagged = classify_samples([dict(s) for s in samples])
    groups: dict[tuple[str, str], list[tuple[float, float]]] = defaultdict(list)
    for s in tagged:
        groups[(s["pid"], s["window"])].append((float(s["ts"]), float(s["value"])))
    out = []
    for (pid, win), pts in sorted(groups.items()):
        vals = [v for _, v in pts]
        n = len(vals)
        mean = sum(vals) / n
        stdev = math.sqrt(sum((v - mean) ** 2 for v in vals) / n) if n > 1 else 0.0
        out.append({
            "pid": pid, "window": win, "n": n,
            "min": round(min(vals), 3), "max": round(max(vals), 3),
            "mean": round(mean, 3), "stdev": round(stdev, 4),
            "slope": round(_slope(pts), 5),
            "ts_start": round(pts[0][0], 2), "ts_end": round(pts[-1][0], 2),
        })
    return out


def window_map(windows: list[dict]) -> dict[tuple[str, str], dict]:
    return {(w["pid"], w["window"]): w for w in windows}


def total_trims(wmap: dict, bank: int, window: str) -> float | None:
    ltft = wmap.get((f"LTFT_B{bank}", window))
    stft = wmap.get((f"STFT_B{bank}", window))
    if ltft is None and stft is None:
        return None
    return round((ltft or {}).get("mean", 0.0) + (stft or {}).get("mean", 0.0), 2)


def maf_expected_idle(rpm: float, displacement_l: float, load_pct: float) -> float:
    """Known-good MAF for a 4-stroke petrol engine (doc 02 §2 rationality
    check), LOAD-normalized: expected = LOAD% * rpm/60 * V/2 * ρ_air.

    CALIBRATED ON VED REAL DATA (Oct 22 soak): the healthy ICE fleet centers
    at ratio 1.02 against this expectation (p5=0.94, p95=1.13; n=306 trips).
    The two pre-soak versions were wrong — 100% VE (WOT airflow; would flag
    every healthy car as under-reporting) and a fixed 0.42 VE. Diesel is
    excluded by the caller: J1979 calculated load is not airflow-normalized
    for diesels (OEM-dependent) — diesels get per-VIN baselines (doc 16 L1).
    """
    return load_pct / 100.0 * rpm / 60.0 * (displacement_l / 2.0) * 1.184


# ---------------------------------------------------------------------------
# L1 — deterministic rules (thresholds are arithmetic, not inference; every
# flag carries provenance: the rule that fired + the values that fired it)
# ---------------------------------------------------------------------------

def _win(wmap, pid, window):
    w = wmap.get((pid, window))
    return w if w and w.get("n", 0) > 0 else None


def _flags_from_windows(windows: list[dict], vehicle: dict) -> list[dict]:
    wmap = window_map(windows)
    flags: list[dict] = []

    def flag(rule, severity, message, values=None, pid=None, window=None, refs=None):
        f = {"layer": "L1", "rule": rule, "severity": severity, "message": message,
             "values": values or {}}
        if pid:
            f["pid"] = pid
        if window:
            f["window"] = window
        if refs:
            f["refs"] = refs
        flags.append(f)

    def high_window_for(bank):
        for w in ("rev_hold", "cruise"):
            if _win(wmap, f"LTFT_B{bank}", w) or _win(wmap, f"STFT_B{bank}", w):
                return w
        return None

    for bank in (1, 2):
        idle_t = total_trims(wmap, bank, "idle")
        high_w = high_window_for(bank)
        high_t = total_trims(wmap, bank, high_w) if high_w else None
        if idle_t is None:
            continue
        if idle_t >= 10 and high_t is not None and high_t <= idle_t - 8:
            flag("trim_idle_dominant", "warn",
                 f"Trims pauvres dominants au ralenti (B{bank}): "
                 f"+{idle_t:.0f}% à vide contre +{high_t:.0f}% en régime — "
                 f"air non mesuré probable (fuite d'admission).",
                 {"idle_total": idle_t, "high_total": high_t, "high_window": high_w,
                  "bank": bank},
                 pid=f"LTFT_B{bank}", window="idle")
        if idle_t <= 8 and high_t is not None and high_t >= 12:
            flag("trim_flow_scaled", "warn",
                 f"Trims pauvres qui montent avec le débit (B{bank}): "
                 f"+{idle_t:.0f}% à vide, +{high_t:.0f}% en régime — "
                 f"débitmètre sous-estimant ou alimentation carburant faible.",
                 {"idle_total": idle_t, "high_total": high_t, "high_window": high_w,
                  "bank": bank},
                 pid=f"LTFT_B{bank}", window=high_w)
        load_t = total_trims(wmap, bank, "load")
        cruise_t = total_trims(wmap, bank, "cruise")
        if load_t is not None and load_t >= 12 and (cruise_t is None or cruise_t <= 8):
            flag("trim_load_only", "warn",
                 f"Trims pauvres uniquement sous forte charge (B{bank}): "
                 f"+{load_t:.0f}% — volume carburant insuffisant à la demande.",
                 {"load_total": load_t, "cruise_total": cruise_t, "bank": bank},
                 pid=f"LTFT_B{bank}", window="load")
        if idle_t >= 15:
            flag("trim_lean_overall", "warn",
                 f"Mélange pauvre confirmé (B{bank}): trims totaux +{idle_t:.0f}% au ralenti.",
                 {"idle_total": idle_t, "bank": bank}, pid=f"LTFT_B{bank}", window="idle")

    # bank localization (doc 02 §2: both banks vs one bank)
    idle1, idle2 = total_trims(wmap, 1, "idle"), total_trims(wmap, 2, "idle")
    if idle1 is not None and idle2 is not None:
        elevated = [idle1 >= 10, idle2 >= 10]
        if any(elevated):
            if all(elevated) and abs(idle1 - idle2) <= 3:
                flag("trim_common_cause", "info",
                     "Les deux banks s'appauvissent pareil — cause commune "
                     "(débitmètre, alimentation, fuite après débitmètre).",
                     {"idle_b1": idle1, "idle_b2": idle2})
            elif abs(idle1 - idle2) >= 5:
                bank = 1 if idle1 > idle2 else 2
                flag("trim_bank_local", "warn",
                     f"Un seul bank touché (B{bank}): {idle1:+.0f}% / {idle2:+.0f}% — "
                     f"injecteur, sonde O2 ou fuite localisée sur ce bank.",
                     {"idle_b1": idle1, "idle_b2": idle2, "bank": bank})

    # O2 switching health
    o2 = _win(wmap, "O2_B1S1", "idle")
    if o2 and o2["n"] >= 8:
        if o2["stdev"] < 0.02:
            flag("o2_stuck", "warn",
                 f"Sonde O2 B1S1 figée à {o2['mean']:.2f} V (σ={o2['stdev']:.3f}) — "
                 f"elle doit commuter 0,1–0,9 V.",
                 {"mean": o2["mean"], "stdev": o2["stdev"]}, pid="O2_B1S1", window="idle")
        elif o2["stdev"] < 0.07:
            flag("o2_sluggish", "warn",
                 f"Sonde O2 B1S1 paresseuse (σ={o2['stdev']:.3f} V, oscillation faible "
                 f"autour de {o2['mean']:.2f} V) — réponse lente typique d'une sonde "
                 f"en fin de vie (P0133).",
                 {"mean": o2["mean"], "stdev": o2["stdev"]}, pid="O2_B1S1", window="idle")

    # MAF rationality vs known-good (doc 02 §2) — LOAD-normalized, petrol
    # only. CALIBRATED ON VED (Oct 22 soak): healthy ICE ratio p5=0.94,
    # p95=1.13 — the 0.75/1.35 thresholds sit outside the healthy band.
    # Diesel excluded: its calculated-load is not airflow-normalized.
    maf = _win(wmap, "MAF", "idle")
    rpm = _win(wmap, "RPM", "idle")
    load = _win(wmap, "LOAD", "idle")
    disp = vehicle.get("displacement_l")
    fuel = str(vehicle.get("fuel", "")).lower()
    if maf and rpm and load and disp and fuel != "diesel":
        expected = maf_expected_idle(rpm["mean"], float(disp), load["mean"])
        ratio = maf["mean"] / expected if expected > 0 else None
        if ratio is not None:
            if ratio < 0.75:
                flag("maf_underreport", "warn",
                     f"MAF {maf['mean']:.1f} g/s au ralenti vs ~{expected:.1f} g/s attendus "
                     f"pour {disp} L à {rpm['mean']:.0f} tr/min et {load['mean']:.0f}% de charge — "
                     f"débitmètre sous-estime ({ratio:.0%}).",
                     {"maf": maf["mean"], "expected": round(expected, 1), "ratio": round(ratio, 2)},
                     pid="MAF", window="idle")
            elif ratio > 1.35:
                flag("maf_overreport", "warn",
                     f"MAF {maf['mean']:.1f} g/s au ralenti vs ~{expected:.1f} g/s attendus "
                     f"({ratio:.0%}) — débitmètre sur-estime.",
                     {"maf": maf["mean"], "expected": round(expected, 1), "ratio": round(ratio, 2)},
                     pid="MAF", window="idle")

    # warm-up curve (doc 10 §A: "the cheapest high-confidence test in the book")
    ectw = _win(wmap, "ECT", "warm_up")
    if ectw and ectw["min"] < 40 and ectw["n"] >= 8:
        if ectw["slope"] < 0.035:
            flag("warmup_slow", "warn",
                 f"Courbe de montée en température lente: {ectw['min']:.0f}→{ectw['max']:.0f} °C "
                 f"à {ectw['slope']:.3f} °C/s — thermostat coincé ouvert probable.",
                 {"slope": ectw["slope"], "ect_min": ectw["min"], "ect_max": ectw["max"],
                  "duration_s": round(ectw["ts_end"] - ectw["ts_start"], 0)},
                 pid="ECT", window="warm_up")

    # charging system
    bat_idle = _win(wmap, "BATV", "idle")
    if bat_idle and bat_idle["mean"] < 13.5:
        flag("voltage_low", "warn",
             f"Tension système {bat_idle['mean']:.2f} V au ralenti (<13,5 V) — "
             f"alternateur/régulateur ou batterie à vérifier.",
             {"mean": bat_idle["mean"], "min": bat_idle["min"]}, pid="BATV", window="idle")
    bat_all = [w for w in windows if w["pid"] == "BATV"]
    if bat_all:
        vmin = min(w["min"] for w in bat_all)
        if vmin < 9.6:
            flag("voltage_crank_dip", "warn",
                 f"Chute de tension à {vmin:.2f} V (<9,6 V au démarrage = batterie faible).",
                 {"min": vmin}, pid="BATV")

    # idle instability (soft misfire evidence) — σ>100 CALIBRATED ON VED
    # (Oct 22): real ICE idle σ at 1 Hz runs p50=55, p95=103 — the pre-soak
    # threshold of 60 fired on a third of a healthy fleet. Soft signal only
    # (×1.3); the real per-cylinder evidence arrives at Tier C (counters).
    rpm_idle = _win(wmap, "RPM", "idle")
    if rpm_idle and rpm_idle["n"] >= 8 and rpm_idle["stdev"] > 100:
        flag("idle_unstable", "info",
             f"Ralenti instable: σ={rpm_idle['stdev']:.0f} tr/min autour de {rpm_idle['mean']:.0f}.",
             {"stdev": rpm_idle["stdev"], "mean": rpm_idle["mean"]}, pid="RPM", window="idle")

    return flags


def _flags_from_intake(state) -> list[dict]:
    """L1 rules over intake evidence (freeze frames + codes) — the failure
    "black box" is data too (doc 01 §5): the operating conditions at the
    moment the code set discriminate before any live capture."""
    flags: list[dict] = []
    codes = set(state.dtc_codes())
    for ff in state.freeze_frames:
        v = ff.get("values", {})
        ect, load, speed = v.get("ECT"), v.get("LOAD"), v.get("SPEED")
        if ect is not None and ect <= 10:
            flags.append({
                "layer": "L1", "rule": "cold_soak_freeze", "severity": "info",
                "message": f"Trame de gel: le défaut s'est déclaré à {ect:.0f}°C — "
                           f"le contexte est un démarrage à froid.",
                "values": {"ff_ect": ect}})
        if "P2463" in codes and load is not None and speed is not None \
                and load <= 30 and speed <= 25:
            flags.append({
                "layer": "L1", "rule": "urban_profile_freeze", "severity": "warn",
                "message": f"Trame de gel du P2463: charge {load:.0f}% à {speed:.0f} km/h — "
                           f"trips urbains courts, la régénération DPF n'a jamais pu "
                           f"se terminer à si basse charge.",
                "values": {"ff_load": load, "ff_speed": speed}})
    return flags


def run_funnel(state) -> tuple[list[dict], list[dict], list[dict]]:
    """L0+L1+L2-slot: (windows, flags, digest-ready samples).

    Takes the folded SessionState; returns window features and rule flags.
    L2 (iForest/autoencoder) is deliberately absent in Phase 1 — degradation
    if missing: the agent asks the tech a few more questions (doc 16 §6).
    """
    samples = [{"pid": s["pid"], "value": s["value"], "ts": s["_ts"]}
               for s in state.samples]
    vehicle = state.vehicle or {}
    windows = build_windows(samples) if samples else []
    flags = _flags_from_windows(windows, vehicle)
    flags += _flags_from_intake(state)   # freeze-frame/code-derived rules
    return windows, flags, samples


# ---------------------------------------------------------------------------
# L3 — digest builder (the only thing the LLM reads unprompted)
# ---------------------------------------------------------------------------

def build_digest(state, ranking=None, nba=None) -> dict:
    """One structured session brief — ~1–3K tokens, French-first."""
    windows = state.windows
    wmap = window_map([w["data"] if "data" in w else w for w in windows])
    fenetres: dict[str, dict] = {}
    for w in windows:
        wd = w["data"] if "data" in w else w
        fenetres.setdefault(wd["window"], {})[wd["pid"]] = {
            "moy": wd["mean"], "min": wd["min"], "max": wd["max"], "n": wd["n"],
            "pente": wd.get("slope", 0),
        }
    codes = {"stored": state.dtc_codes("stored"), "pending": state.dtc_codes("pending"),
             "permanent": state.dtc_codes("permanent")}
    ff = state.freeze_frames[0] if state.freeze_frames else None
    digest = {
        "vehicule": {
            "vin": state.vin, **(state.vehicle or {}),
        },
        "plainte": {
            "texte": (state.complaint or {}).get("text", ""),
            "chips": state.chips + state.condition_chips,
            "mecanique": (state.complaint or {}).get("mechanical_chips", []),
        },
        "codes": codes,
        "freeze_frame": (ff or {}).get("values", {}),
        "readiness": (state.readiness or {}).get("monitors", {}),
        "mil": (state.readiness or {}).get("mil"),
        "mode06": [{"nom": m["name"], "valeur": m["value"], "min": m["min"],
                    "max": m["max"], "verdict": m["verdict"]} for m in state.mode06],
        "triage": [{"id": f["id"], "titre": f["title"]} for f in state.triage_findings],
        "fenetres": fenetres,
        "flags": [{"regle": f["rule"], "gravite": f["severity"], "msg": f["message"]}
                  for f in state.flags],
        "travaux_recents": (state.history or {}).get("recent_work", []),
        "mesures": [{"metrique": m["metric"], "valeur": m["value"], "unit": m["unit"],
                     "verdict": (m.get("spec") or {}).get("verdict")}
                    for m in state.measurements],
        "tests_physiques": [{"test": t["test_id"], "resultat": t["result"]}
                            for t in state.physical_tests],
    }
    if ranking:
        digest["hypotheses"] = [
            {"cause": h["cause"], "label": h["label"], "confiance": h["confidence"]}
            for h in ranking.get("ranking", [])[:3]
        ]
    if nba:
        digest["nba"] = {"tache": nba.get("task_id"), "titre": nba.get("title")}
    return digest


def token_estimate(obj) -> int:
    """Rough token count (chars/4) — the doc-17 §4.1 budget metric."""
    return len(json.dumps(obj, ensure_ascii=False)) // 4 + 1


# ---------------------------------------------------------------------------
# Drill-down tools (doc 16 L4): precision preserved by reachability, not by
# sending everything up front.
# ---------------------------------------------------------------------------

def tool_get_samples(state, pid: str, window: str | None = None, limit: int = 40):
    out = []
    for s in state.samples:
        if s["pid"] != pid:
            continue
        if window is not None and s.get("window") not in (window, None):
            continue
        out.append({"ts": s["_ts"], "valeur": s["value"], **({"fenetre": s["window"]} if "window" in s else {})})
    return out[-limit:]


def tool_get_mode06(state, mid: int | None = None):
    return [m for m in state.mode06 if mid is None or m["mid"] == mid]


def tool_replay_segment(state, t1: float, t2: float):
    from .ledger import fold

    evs = [e for e in getattr(state, "_events", []) if t1 <= e["ts"] <= t2]
    if not evs:
        return []
    return [{"seq": e["seq"], "ts": e["ts"], "type": e["type"], "data": e["data"]}
            for e in evs]


DRILL_TOOLS = [
    {"name": "get_samples",
     "description": "Échantillons bruts d'un PID (option: fenêtre idle/cruise/rev_hold/load/warm_up).",
     "params": {"pid": "str", "window": "str?", "limit": "int?"}},
    {"name": "get_mode06",
     "description": "Résultats Mode $06 avec limites usine (option: filtrer par MID).",
     "params": {"mid": "int?"}},
    {"name": "replay_segment",
     "description": "Rejoue les événements du registre entre t1 et t2 (secondes de session).",
     "params": {"t1": "float", "t2": "float"}},
]
