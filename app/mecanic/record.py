"""Stage 9 — Record (doc 00 step 9, doc 12): per-VIN memory, cross-visit
trends (monitor drift, recurring faults), and the outcome store — every
session's evidence + what actually fixed the car. The outcome-verified
patterns ARE the compounding data moat (doc 11 §6 flywheel).

Privacy (doc 15 stage 9, CNDP loi 09-08): local-first storage, VIN hashed
by default; sync policy deliberately open (decided later). The outcome form
is the last screen of every session — 4 taps, never blocking.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path

from .ledger import OUTCOMES_PATH, VIN_DIR

OUTCOME_FIELDS = ("fix_part", "fix_action", "verified", "comeback")


def _vin_key(vin: str | None) -> str:
    if not vin:
        return "unknown"
    return hashlib.sha256(vin.encode()).hexdigest()[:16]


def load_vin_profile(vin: str | None) -> dict:
    path = VIN_DIR / f"{_vin_key(vin)}.json"
    if path.exists():
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    return {"vin_hash": _vin_key(vin), "visits": []}


def save_vin_profile(vin: str | None, profile: dict) -> None:
    VIN_DIR.mkdir(parents=True, exist_ok=True)
    path = VIN_DIR / f"{_vin_key(vin)}.json"
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(profile, fh, ensure_ascii=False, indent=1)


def record_visit(state, verdict_data: dict | None, fix_plan: dict | None) -> dict:
    """Append this session's snapshot to the VIN profile + compute trends."""
    profile = load_vin_profile(state.vin)
    idle_trims = {}
    for w in state.windows:
        wd = w["data"] if "data" in w else w
        if wd["window"] == "idle" and wd["pid"].startswith(("LTFT", "STFT")):
            idle_trims[wd["pid"]] = wd["mean"]
    visit = {
        "date": datetime.now().isoformat(timespec="seconds"),
        "session_id": state.session_id,
        "codes": state.dtc_codes(),
        "trims_idle": idle_trims,
        "mode06": [{"mid": m["mid"], "tid": m["tid"], "name": m["name"],
                    "value": m["value"], "verdict": m["verdict"]} for m in state.mode06],
        "verdict": (verdict_data or {}).get("cause"),
        "verdict_confidence": (verdict_data or {}).get("confidence"),
        "fix_part": (fix_plan or {}).get("parts"),
        "outcome": state.outcome,
    }
    profile["visits"].append(visit)
    profile["trends"] = _trends(profile)
    save_vin_profile(state.vin, profile)
    return profile


def _trends(profile: dict) -> list[str]:
    """Cross-visit drift: monitor values sliding toward their limits,
    recurring codes — the 'your catalyst dropped 92%→78%' story (doc 02 §3)."""
    out: list[str] = []
    visits = profile.get("visits", [])
    by_test: dict[tuple, list] = {}
    recurrences: dict[str, int] = {}
    for v in visits:
        for m in v.get("mode06", []):
            by_test.setdefault((m["mid"], m["tid"], m["name"]), []).append(
                {"date": v["date"], "value": m["value"], "verdict": m["verdict"]})
        for c in v.get("codes", []):
            recurrences[c] = recurrences.get(c, 0) + 1
    for (mid, tid, name), series in by_test.items():
        if len(series) >= 2:
            first, last = series[0], series[-1]
            if first["value"] != last["value"]:
                out.append(f"{name}: {first['value']} -> {last['value']} "
                           f"({len(series)} visites) — dérive à surveiller.")
    for code, n in sorted(recurrences.items(), key=lambda kv: -kv[1]):
        if n >= 2:
            out.append(f"Code {code} récurrent: vu sur {n} visites.")
    return out


def record_outcome(session_id: str, vin: str | None, outcome: dict) -> dict:
    """The outcome form (doc 12 schema v0) — appends to the flywheel store."""
    OUTCOMES_PATH.parent.mkdir(parents=True, exist_ok=True)
    rec = {"session_id": session_id, "vin_hash": _vin_key(vin),
           "date": datetime.now().isoformat(timespec="seconds"),
           **{k: outcome.get(k) for k in OUTCOME_FIELDS}}
    if outcome.get("note"):
        rec["note"] = outcome["note"]
    with open(OUTCOMES_PATH, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return rec
