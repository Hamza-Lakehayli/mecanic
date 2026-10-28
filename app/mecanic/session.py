"""The 9-step loop as a runnable session (doc 00, doc 15).

  0 QUICK START -> 1 INGESTION -> 2 AUTO-INTAKE -> 3 TRIAGE -> 4 HYPOTHESES
  -> 5 TASK ENGINE & TESTS -> 6 VERDICT -> 7 FIX PLAN -> 8 VERIFICATION
  -> 9 RECORD

Session modes (doc 12): AUTO default / INTERACTIVE opt-in — they change only
*when* stages consume the ledger, never whether the agent starts working.
Every stage reads/writes the evidence ledger; every human input is optional
and incremental (zero input still produces a full dossier).
"""

from __future__ import annotations

from pathlib import Path

from .autointake import AutoIntake
from .elm327 import ELM327Driver
from .emulator import capability_from_scenario
from .fixplan import build_fix_plan
from .funnel import build_digest, run_funnel, token_estimate
from .ledger import Ledger, fold
from .llm import NemotronClient
from .patterns import PatternLibrary
from .ranker import Ranker
from .record import record_outcome, record_visit
from .report import render_report, save_report
from .tasks import AUTO_TASKS, CARD_CATALOG, MAX_VISIBLE_CARDS, nba_card, visible_cards
from .triage import Triage
from .verification import build_verification
from .yamlmini import load_file

DATA_DIR = Path(__file__).resolve().parent.parent / "data"


def load_lexicon() -> dict:
    p = DATA_DIR / "lexicon_fr_darija.yaml"
    return load_file(str(p)) if p.exists() else {}


def extract_chips_lexicon(text: str, lexicon: dict | None = None) -> list[str]:
    """Deterministic FR/Darija keyword -> chip extraction (the offline path;
    the LLM path is the optional upgrade, doc 12 §3). Darija-extraction
    quality is an explicit test-week item (doc 17 gap #4)."""
    lex = lexicon if lexicon is not None else load_lexicon()
    low = (text or "").lower()
    hits = []
    for chip, terms in lex.items():
        for term in terms or []:
            if not term:
                continue
            if term.lower() in low:
                hits.append(chip)
                break
    return hits


def build_chip_gloss() -> dict[str, str]:
    """chip id -> FR label + FR/Darija keywords — shared by the LLM prompt so
    the two extraction paths speak the same bank (doc 12 §3, gap #4)."""
    priors = load_file(str(DATA_DIR / "priors.yaml")) or {}
    lexicon = load_lexicon()
    gloss: dict[str, str] = {}
    for section in ("symptoms", "conditions", "mechanical"):
        for chip, spec in (priors.get(section) or {}).items():
            if not isinstance(spec, dict):
                continue
            desc = spec.get("label_fr", chip)
            terms = lexicon.get(chip) or []
            if terms:
                desc += " — mots-clés: " + ", ".join(str(t) for t in terms[:6])
            gloss[chip] = desc
    return gloss


CHIP_BANKS = ("symptoms", "conditions", "mechanical")


def chip_bank(chip: str) -> str | None:
    """Which bank a chip id belongs to (symptoms / conditions / mechanical)."""
    priors = load_file(str(DATA_DIR / "priors.yaml")) or {}
    for section in CHIP_BANKS:
        if chip in (priors.get(section) or {}):
            return section
    return None


class Session:
    """One dossier = one job (doc 12 §1)."""

    def __init__(self, ledger: Ledger, transport, scenario: dict | None = None,
                 mode: str = "AUTO", llm: NemotronClient | None = None):
        self.ledger = ledger
        self.transport = transport
        self.scenario = scenario or {}
        self.mode = mode if mode in ("AUTO", "INTERACTIVE") else "AUTO"
        self.llm = llm or NemotronClient()
        self.library = PatternLibrary.load()
        self.ranker = Ranker(self.library)
        self.triage = Triage()
        self.capabilities = capability_from_scenario(self.scenario).to_event_data() \
            if self.scenario else {}
        self.vehicle_decode = self.scenario.get("vehicle", {}).get("decode", {})
        self.driver: ELM327Driver | None = None
        self._poll_names = ["STFT_B1", "LTFT_B1", "STFT_B2", "LTFT_B2", "RPM", "MAF",
                            "ECT", "O2_B1S1", "BATV", "LOAD", "SPEED", "MAP", "IAT", "TPS"]

    # -- stage 0: quick start (≤30 s, everything optional) ----------------------

    def quick_start(self, text: str | None = None, chips: list[str] | None = None,
                    condition_chips: list[str] | None = None,
                    recent_work: list[dict] | None = None,
                    tuning_state: dict | None = None) -> None:
        self.ledger.append("session_start", "app",
                           {"session_id": self.ledger.session_id, "mode": self.mode})
        priors = load_file(str(DATA_DIR / "priors.yaml")) or {}
        sym_bank = (priors.get("symptoms") or {})
        cond_bank = (priors.get("conditions") or {})
        mech_bank = (priors.get("mechanical") or {})
        extracted: dict = {}
        if text:
            # LLM path first (FR/Darija extraction, mixed-in-one-sentence
            # register included), deterministic lexicon as the fallback —
            # two paths, one ledger (doc 12 §3). The LLM gets the chip
            # gloss (FR labels + Darija keywords) so both paths speak the
            # same bank (gap #4).
            extracted = (self.llm.extract_complaint(text, list(sym_bank), list(cond_bank),
                                                    gloss=build_chip_gloss(),
                                                    mechanical_chips=list(mech_bank))
                         or {})
            lex_chips = extract_chips_lexicon(text)
            for c in lex_chips:
                bank = chip_bank(c)
                key = {"symptoms": "chips", "conditions": "condition_chips",
                       "mechanical": "mechanical_chips"}.get(bank or "", "chips")
                if c not in extracted.setdefault(key, []):
                    extracted[key] = list(extracted.get(key, [])) + [c]
        chips = list(dict.fromkeys((chips or []) + extracted.get("chips", [])))
        condition_chips = list(dict.fromkeys((condition_chips or []) + extracted.get("condition_chips", [])))
        mechanical_chips = list(dict.fromkeys(extracted.get("mechanical_chips", [])))
        self.ledger.append("complaint", "tech",
                           {"text": text or "", "chips": chips,
                            "condition_chips": condition_chips,
                            "mechanical_chips": mechanical_chips,
                            "severity": extracted.get("severity"),
                            "language": extracted.get("language"),
                            "llm_extracted": bool(extracted) and bool(self.llm.available),
                            "notes": extracted.get("notes", "")})
        history = {}
        if recent_work:
            history["recent_work"] = recent_work
        if tuning_state:
            history["tuning_state"] = tuning_state
        if history:
            self.ledger.append("history", "tech", history)

    # -- stages 1–2: connect + auto-intake (zero button presses) ----------------

    def connect_and_intake(self) -> None:
        self.driver = ELM327Driver(self.transport)
        fingerprint = self.driver.connect()
        caps = {**self.capabilities, "adapter_fingerprint": fingerprint,
                "producer": self.capabilities.get("producer", fingerprint["chip_id"])}
        AutoIntake(self.ledger, self.driver, caps, self.vehicle_decode,
                   self._poll_names).run()

    # -- live watch: the drive-recorder / bay watch (AUTO task) ------------------

    def live_watch(self, poll_dt: float = 1.0, total_s: float | None = None,
                   poll_names: list[str] | None = None) -> int:
        """Poll the priority PID set over the session's duration. On the
        scenario transport this advances simulated time; on a real transport
        it runs in real time. Either way the funnel later bins samples into
        idle / 2000–3000 tr/min windows (doc 10 §1.1)."""
        assert self.driver is not None, "connect_and_intake() first"
        names = poll_names or self._poll_names
        if hasattr(self.transport, "total_duration"):
            total = total_s if total_s is not None else self.transport.total_duration
        else:
            total = total_s if total_s is not None else 60.0
        import time as _time

        n = 0
        t = 0.0
        sim = hasattr(self.transport, "tick")
        while t < total:
            if sim:
                self.transport.tick(poll_dt)
            else:
                _time.sleep(poll_dt)
            t += poll_dt
            # stamp events with the VEHICLE's clock on simulated transports —
            # the wall clock would pack 780 cycles into 300 ms of "session time"
            ts = self.transport._t if sim else None
            for s in self.driver.poll_cycle(names):
                self.ledger.append("pid_sample", "live_watch",
                                   {"pid": s["name"], "name": s["name"],
                                    "value": round(float(s["value"]), 3),
                                    "unit": s["unit"]},
                                   quality={"adapter": self.driver.chip_id}, ts=ts)
            n += 1
        return n

    # -- stages 3–5: triage + hypotheses + task engine --------------------------

    def analyze(self) -> tuple:
        # funnel L0-L1 over the folded stream — append only new windows/flags
        # (analyze() re-runs after every card answer; the ledger stays clean)
        state = self.ledger.fold()
        windows, flags, _samples = run_funnel(state)
        seen_windows = {(w["pid"], w["window"]) for w in state.windows}
        for w in windows:
            if (w["pid"], w["window"]) not in seen_windows:
                self.ledger.append("window", "agent:funnel", w)
        seen_flags = {f["rule"] for f in state.flags}
        for f in flags:
            if f["rule"] not in seen_flags:
                self.ledger.append("flag", "agent:funnel", f)

        # triage (reads intake together with the data) — findings are
        # re-derived on every analyze(); only *new* finding ids append
        # (event-sourced history stays clean of duplicates on re-ranks)
        state = self.ledger.fold()
        known_ids = {f["id"] for f in state.triage_findings}
        for f in self.triage.analyze(state):
            if f["id"] not in known_ids:
                self.ledger.append("triage_finding", "agent:triage", f)

        # hypothesis ranking (re-runs on every fold — doc 12)
        state = self.ledger.fold()
        ranking = self.ranker.analyze(state)
        self.ledger.append("hypothesis_ranking", "agent:ranker", ranking)

        # task engine: AUTO done, NEEDS-YOU queued, NBA chosen
        state = self.ledger.fold()
        performed = {t["test_id"] for t in state.physical_tests}
        performed |= {m.get("condition") for m in state.measurements if m.get("condition")}
        cards = visible_cards(self.capabilities or {"producer": "unknown"}, ranking,
                               performed, vehicle=self.vehicle_decode or {})
        nba = nba_card(cards)
        queued_ids = {t["task_id"] for t in state.tasks if t.get("kind") == "NEEDS_YOU"}
        for t in AUTO_TASKS:
            if not any(ex["task_id"] == t["task_id"] and ex.get("kind") == "AUTO"
                       for ex in state.tasks):
                self.ledger.append("task", "agent:tasks", {**t, "kind": "AUTO", "status": "done"})
        for c in cards:
            if c["task_id"] not in queued_ids:
                self.ledger.append("task", "agent:tasks", c)
                queued_ids.add(c["task_id"])

        # L3 digest — the only thing the LLM reads unprompted
        state = self.ledger.fold()
        digest = build_digest(state, ranking, nba)
        self.ledger.append("digest", "agent:funnel",
                           {"digest": digest, "token_estimate": token_estimate(digest)})
        state = self.ledger.fold()
        return state, ranking, cards, nba, digest

    # -- stage 5 (human side): card answers merge into the ledger ----------------

    def apply_card_result(self, card_id: str, result: str, location: str | None = None) -> None:
        card = CARD_CATALOG.get(card_id, {})
        data = {"test_id": card_id, "performed": True, "result": result,
                "title": card.get("title_fr", card_id)}
        if location:
            data["location"] = location
        self.ledger.append("physical_test", "tech", data)

    def apply_measurement_card(self, card_id: str, value: float) -> None:
        widget = (CARD_CATALOG.get(card_id) or {}).get("widget") or {}
        spec = {"min": widget.get("min"), "max": widget.get("max")}
        spec["verdict"] = "OK" if spec["min"] <= value <= spec["max"] else "BAD"
        self.ledger.append("measurement", "tech",
                           {"metric": widget.get("metric", card_id), "value": value,
                            "unit": widget.get("unit", ""), "condition": card_id,
                            "spec": spec})

    def simulate_steps(self, steps: list[dict]) -> None:
        """Benchmark/interactive helper: perform the queued cards (the tech
        answers when they return to the car — doc 12)."""
        for step in steps or []:
            card = step.get("card", "")
            if "value" in step:
                self.apply_measurement_card(card, float(step["value"]))
            else:
                self.apply_card_result(card, step.get("result", "done"),
                                       step.get("location"))

    # -- stages 6–9: verdict, fix plan, verification, record --------------------

    def conclude(self, state=None, ranking=None, cards=None, nba=None, digest=None):
        from .verdict import compose

        if state is None:
            state, ranking, cards, nba, digest = self.analyze()
        verdict_data = compose(state, ranking, nba)
        self.ledger.append("verdict", "agent:verdict", verdict_data)

        fix_plan = None
        verification = None
        # the fix plan follows the VERDICT only — a ranking leader below
        # threshold is not a guess we sell (doc 02 §7, doc 15 stage 6)
        if verdict_data.get("cause") and verdict_data.get("status") == "verdict":
            fix_plan = build_fix_plan(verdict_data["cause"], self.library)
            if fix_plan:
                self.ledger.append("fix_plan", "agent:fixplan", fix_plan)
            verification = build_verification(state, self.library, verdict_data)
            self.ledger.append("verification", "agent:verification", verification)

        state = self.ledger.fold()
        markdown = render_report(state, verdict_data, fix_plan, verification,
                                  cards or [], nba, digest)
        report_path = save_report(self.ledger.session_id, markdown,
                                  self.ledger.path.parent if self.ledger.path
                                  else Path("var/sessions"))
        profile = record_visit(state, verdict_data, fix_plan)
        self.ledger.append("record", "agent:record",
                           {"vin_profile_visits": len(profile.get("visits", [])),
                            "trends": profile.get("trends", [])})
        return {"verdict": verdict_data, "fix_plan": fix_plan,
                "verification": verification, "report_path": str(report_path),
                "trends": profile.get("trends", [])}

    # -- outcome form (the flywheel's closing tap) -------------------------------

    def submit_outcome(self, fix_part: str, fix_action: str, verified: str,
                       comeback: str, note: str = "") -> dict:
        outcome = {"fix_part": fix_part, "fix_action": fix_action,
                   "verified": verified, "comeback": comeback}
        if note:
            outcome["note"] = note
        self.ledger.append("outcome", "tech", outcome)
        rec = record_outcome(self.ledger.session_id, self.ledger.fold().vin, outcome)
        return rec
