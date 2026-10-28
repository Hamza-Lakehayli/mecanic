"""Stage 4 — Hypothesis ranking (docs 02/12/15).

Hybrid brain, by design (doc 12 §1):
  * chips + measurements + physical-test results -> DETERMINISTIC priors
    (exact multipliers, offline, zero marginal cost);
  * funnel flags (L1 rules) -> per-playbook discriminators;
  * the pattern library supplies causes, base priors, and test cards;
  * the LLM (Nemotron floor-test baseline, doc 17 §4.1) only ever sees the
    DIGEST and free text/photos — never the stream — and its output merges
    into the same schema (optional layer, never a dependency).

Every re-rank is logged: "this measurement moved vacuum leak 40% -> 75%,
here's why" (doc 12). Confidence v0 is a documented heuristic — calibration
on labeled cases is the demo-weeks task (doc 17 gap #6).
"""

from __future__ import annotations

import math
from pathlib import Path

from .funnel import window_map, total_trims
from .patterns import PatternLibrary, Playbook
from .triage import load_dtc_titles
from .yamlmini import load_file

DATA_DIR = Path(__file__).resolve().parent.parent / "data"

# confidence v0 (gap #6): every confirming multiplier adds
# min(40, 12×mult) evidence points to that cause's strength; strength is
# capped and multiplied by sqrt(share). Numbers get calibrated on labeled
# cases during the LLM test phase (doc 17 §4.1).
BASE_STRENGTH = 40
STRENGTH_CAP = 95
EVIDENCE_POINTS_PER_MULT = 12.0
EVIDENCE_POINTS_CAP = 40.0
VERDICT_THRESHOLD = 70     # >= this -> verdict (doc 15 stage 6)
RANKING_THRESHOLD = 40     # >= this -> show ranking + NBA; below -> escalate


def load_priors() -> dict:
    p = DATA_DIR / "priors.yaml"
    return load_file(str(p)) if p.exists() else {}


class Ranker:
    """Ranks root causes for the current folded state. Pure function of the
    ledger — re-runs on every event (doc 12: re-ranking on every ledger event)."""

    def __init__(self, library: PatternLibrary, priors: dict | None = None,
                 dtc_titles: dict | None = None):
        self.library = library
        self.priors = priors if priors is not None else load_priors()
        self.dtc_titles = dtc_titles if dtc_titles is not None else load_dtc_titles()

    # -- public ---------------------------------------------------------------

    def analyze(self, state) -> dict:
        codes = state.dtc_codes()
        # folded flag events are flattened ({rule, severity, ...} at top level)
        flags = {f["rule"]: f for f in state.flags}
        flag_rules = set(flags)
        recent = (state.history or {}).get("recent_work", [])
        playbooks = self.library.active(codes, flag_rules, recent=recent)

        trail: list[dict] = []
        rows: list[dict] = []
        for pb in playbooks:
            for cause in pb.causes:
                row = self._score_cause(pb, cause, state, flags, trail)
                if row:
                    rows.append(row)

        total = sum(r["score"] for r in rows) or 1.0
        for r in rows:
            share = r["score"] / total
            r["share"] = round(share, 3)
            # v0 calibration (gap #6): strength = how much discrimination
            # happened (flags + confirming tests); sqrt(share) concentrates
            # confidence on the leader without burying runners-up. Numbers
            # get calibrated on labeled cases during the LLM test phase.
            r["confidence"] = round(r["strength"] * math.sqrt(share))
        rows.sort(key=lambda r: (-r["confidence"], -r["score"]))
        for i, r in enumerate(rows, 1):
            r["rank"] = i

        status = "escalate"
        if rows:
            top = rows[0]
            if top["confidence"] >= VERDICT_THRESHOLD:
                status = "verdict"
            elif top["confidence"] >= RANKING_THRESHOLD:
                status = "ranking"
        return {"ranking": rows, "trail": trail, "active_playbooks": [pb.id for pb in playbooks],
                "status": status}

    # -- scoring ----------------------------------------------------------------

    def _score_cause(self, pb: Playbook, cause: dict, state, flags: dict, trail: list) -> dict:
        score = float(cause.get("base_prior", 0.1))
        evidence_for: list[dict] = []
        evidence_against: list[dict] = []
        points = 0.0

        def note(text, cause_id=cause["id"], kind="flag"):
            trail.append({"cause": cause_id, "kind": kind, "text": text})

        # 1 — funnel flags (deterministic discriminators)
        for flag_rule in (cause.get("boosted_by") or []):
            f = flags.get(flag_rule)
            if not f:
                continue
            mult = self._flag_multiplier(flag_rule)
            score *= mult
            points += min(EVIDENCE_POINTS_PER_MULT * mult, EVIDENCE_POINTS_CAP)
            note(f"Flag '{flag_rule}' (×{mult}): {f.get('message', '')}")
            evidence_for.append({"class": "AGENT", "text": f.get("message", flag_rule),
                                 "rule": flag_rule, "values": f.get("values", {})})
        for flag_rule in (cause.get("suppressed_by") or []):
            f = flags.get(flag_rule)
            if not f:
                continue
            score *= 0.45
            note(f"Flag contradictoire '{flag_rule}' (×0.45)")
            evidence_against.append({"class": "AGENT", "text": f.get("message", flag_rule),
                                     "rule": flag_rule})

        # 2 — intake chips (deterministic priors, doc 12 §1)
        chips = list(dict.fromkeys(list(state.chips) + list(state.condition_chips)))
        for chip in chips:
            effect = self._chip_effect(chip)
            if effect and cause["id"] in effect:
                mult = float(effect[cause["id"]])
                score *= mult
                note(f"Chip '{chip}' (×{mult})")
                evidence_for.append({"class": "INTAKE", "text": f"Indice déclaré: {chip}",
                                     "chip": chip, "multiplier": mult})

        # 3 — measurements (widgets with specs — exact priors)
        for m in state.measurements:
            effect = self._measurement_effect(m)
            if not effect:
                continue
            for cause_id, mult in effect.items():
                if cause_id != cause["id"]:
                    continue
                score *= mult
                conf = mult >= 1
                points += min(EVIDENCE_POINTS_PER_MULT * mult, EVIDENCE_POINTS_CAP) if conf else 0
                verdict = (m.get("spec") or {}).get("verdict", "?")
                note(f"Mesure {m['metric']}={m['value']} {m.get('unit', '')} "
                     f"[{verdict}] (×{mult})")
                (evidence_for if conf else evidence_against).append(
                    {"class": "MEASUREMENT", "text": f"{m['metric']} = {m['value']} {m.get('unit', '')} ({verdict})",
                     "metric": m["metric"]})

        # 4 — physical test results (you act, it watches — doc 11)
        for t in state.physical_tests:
            effect = self._physical_test_effect(t)
            if not effect:
                continue
            for cause_id, mult in effect.items():
                if cause_id != cause["id"]:
                    continue
                score *= mult
                conf = mult >= 1
                points += min(EVIDENCE_POINTS_PER_MULT * mult, EVIDENCE_POINTS_CAP) if conf else 0
                note(f"Test physique {t['test_id']} -> {t['result']} (×{mult})")
                (evidence_for if conf else evidence_against).append(
                    {"class": "PHYSICAL", "text": f"{t['test_id']} : {t['result']}"
                     + (f" ({t['location']})" if t.get("location") else ""),
                     "test": t["test_id"]})

        # 5 — comeback prior: recent work on a related system (doc 11 §2)
        recent = (state.history or {}).get("recent_work", [])
        for rw in recent:
            if self._recent_work_related(rw, cause):
                score *= 1.5
                note(f"Travaux récents sur {rw.get('system', '?')} — priorité au retour atelier (×1.5)")
                evidence_for.append({"class": "INTAKE",
                                     "text": f"Intervention récente: {rw.get('system')} / {rw.get('part')}"})

        # 5b — targeted recent-work boost: the used-parts pattern (doc 02 §3).
        # A swapped part missing its configuration step (codage/adaptation/
        # calibrage) is a fault family of its own — and recent work is the
        # highest-value question in the bank (doc 11 §2), so a keyword match
        # between the swapped part and the cause earns a strong multiplier.
        rwb = cause.get("recent_work_boost") or {}
        if rwb:
            terms = [str(t).lower() for t in (rwb.get("keywords") or [])]
            for rw in recent:
                hay = f"{rw.get('system', '')} {rw.get('part', '')}".lower()
                if any(t in hay for t in terms):
                    mult = float(rwb.get("mult", 1.0))
                    score *= mult
                    points += min(EVIDENCE_POINTS_PER_MULT * mult, EVIDENCE_POINTS_CAP)
                    note(f"Pièce remplacée récemment ({rw.get('part', '?')}) — "
                         f"étape de configuration (codage/adaptation) à vérifier en premier (×{mult})")
                    evidence_for.append({
                        "class": "INTAKE",
                        "text": f"Intervention récente: {rw.get('system')} / {rw.get('part')} — "
                                "étape de configuration post-réparation probablement manquante"})

        strength = min(STRENGTH_CAP, BASE_STRENGTH + points)
        return {
            "cause": cause["id"],
            "label": cause.get("label_fr", cause["id"]),
            "playbook": pb.id,
            "score": round(score, 4),
            "strength": round(strength, 1),
            "evidence_for": evidence_for,
            "evidence_against": evidence_against,
            "cards": list(cause.get("cards", [])),
        }

    # -- prior tables ----------------------------------------------------------

    def _flag_multiplier(self, rule: str) -> float:
        # strong discriminators move more (doc 02 §2 tables)
        return {"trim_idle_dominant": 2.4, "trim_flow_scaled": 2.4, "trim_load_only": 2.4,
                "trim_lean_overall": 1.5, "trim_bank_local": 1.8, "trim_common_cause": 1.3,
                "o2_stuck": 2.2, "o2_sluggish": 2.2, "maf_underreport": 2.4, "maf_overreport": 2.2,
                "warmup_slow": 2.6, "voltage_low": 2.0, "voltage_crank_dip": 2.0,
                "idle_unstable": 1.3, "cold_soak_freeze": 1.5,
                "urban_profile_freeze": 1.8}.get(rule, 1.6)

    def _chip_effect(self, chip: str) -> dict | None:
        for section in ("symptoms", "conditions"):
            e = (self.priors.get(section) or {}).get(chip) or {}
            if isinstance(e, dict) and e.get("prior_effects"):
                return e["prior_effects"]
        return None

    def _measurement_effect(self, m: dict) -> dict | None:
        verdict = (m.get("spec") or {}).get("verdict")
        if not verdict:
            return None
        table = self.priors.get("measurements") or {}
        # the YAML keys are written "<metric>:BAD" (mixed case) while the
        # widget verdict is "OK"/"BAD" — match as written first, then
        # case-insensitive fallbacks (a latent no-op bug caught by the
        # sprint-3 measurement scenarios, Oct 22 2026)
        raw = f"{m.get('metric', '')}:{verdict}"
        entry = table.get(raw) or table.get(raw.lower()) or table.get(raw.upper())
        return entry.get("prior_effects") if isinstance(entry, dict) else None

    def _physical_test_effect(self, t: dict) -> dict | None:
        table = (self.priors.get("physical_tests") or {}).get(t.get("test_id", "")) or {}
        if not isinstance(table, dict):
            return None
        outcomes = table.get("outcomes") or {}
        eff = outcomes.get(str(t.get("result", "")).lower())
        return eff if isinstance(eff, dict) else None

    def _recent_work_related(self, rw: dict, cause: dict) -> bool:
        system = (rw.get("system", "") + " " + rw.get("part", "")).lower()
        label = (cause.get("id", "") + " " + cause.get("label_fr", "")).lower()
        keywords = {
            "admission": ["admission", "air", "durite", "filtre", "maf", "debitmetre", "collecteur"],
            "allumage": ["allumage", "bobine", "bougie", "coil"],
            "carburant": ["carburant", "injecteur", "pompe", "filtre", "pression"],
            "refroidissement": ["refroidissement", "thermostat", "radiateur", "ventilateur"],
            "echappement": ["echappement", "catalyseur", "dpf", "egr"],
            "electrique": ["batterie", "alternateur", "masse", "electrique"],
        }
        for _cat, terms in keywords.items():
            if any(t in label for t in terms) and any(t in system for t in terms):
                return True
        return False


def summarize_ranking_for_dashboard(ranking: dict, top_n: int = 5) -> list[dict]:
    return [{k: r[k] for k in ("rank", "cause", "label", "confidence", "share")}
            for r in ranking["ranking"][:top_n]]
