"""Stage 4 — the pattern library (docs 02/15/17 gap #1).

AI-authored v1 playbooks, YAML, machine-readable, diesel-first where it
matters (doc 02 is the seed; validated on dataset tests now, real cars once
the certified adapter arrives — gap #1 closure plan). The *format* is the
locked part; the content grows with every outcome-verified session (the
flywheel, doc 00 step 9).

Playbook structure:
    id, name_fr, triggers {dtcs[], flags[]}      — what activates it
    causes[]: id, label_fr, base_prior, boosted_by[]/suppressed_by[] (flag
              rule ids — deterministic multipliers), chips_boost[] (intake
              chips — deterministic priors, doc 12), cards[] (task ids),
              fix {parts[{ref,qty}], labor_h, procedure[]}
    verification {drive_cycle, recheck_mode06[], note_fr}
"""

from __future__ import annotations

from pathlib import Path

from .yamlmini import load_file

PATTERNS_DIR = Path(__file__).resolve().parent.parent / "patterns"
DATA_DIR = Path(__file__).resolve().parent.parent / "data"


class Playbook:
    def __init__(self, raw: dict):
        self.raw = raw
        self.id = raw.get("id", "?")
        self.name_fr = raw.get("name_fr", self.id)
        triggers = raw.get("triggers", {}) or {}
        self.trigger_dtcs = list(triggers.get("dtcs", []) or [])
        self.trigger_flags = list(triggers.get("flags", []) or [])
        # history-triggered families (the used-parts pattern, doc 02 §3):
        # lowercase keywords matched against recent_work {system, part}.
        self.trigger_recent = [str(t).lower() for t in (triggers.get("recent_part", []) or [])]
        self.causes = list(raw.get("causes", []) or [])
        self.verification = raw.get("verification", {}) or {}
        self.notes_fr = raw.get("notes_fr", "")

    def cause(self, cause_id: str) -> dict | None:
        for c in self.causes:
            if c.get("id") == cause_id:
                return c
        return None

    def matches(self, codes: list[str], flag_rules: set[str], recent: list | None = None) -> bool:
        if any(d in self.trigger_dtcs for d in codes):
            return True
        if any(f in flag_rules for f in self.trigger_flags):
            return True
        # history-triggered: activate only when relevant recent work exists —
        # a swapped part without its configuration step often sets NO code.
        if self.trigger_recent and recent:
            for rw in recent:
                hay = f"{rw.get('system', '')} {rw.get('part', '')}".lower()
                if any(t in hay for t in self.trigger_recent):
                    return True
        return False


class PatternLibrary:
    def __init__(self, playbooks: list[Playbook]):
        self.playbooks = playbooks
        self._by_cause: dict[str, tuple[Playbook, dict]] = {}
        for pb in self.playbooks:
            for c in pb.causes:
                self._by_cause[c["id"]] = (pb, c)

    @classmethod
    def load(cls, directory: str | Path | None = None) -> "PatternLibrary":
        d = Path(directory) if directory else PATTERNS_DIR
        pbs = []
        for p in sorted(d.glob("*.yaml")):
            raw = load_file(str(p))
            if isinstance(raw, dict) and raw.get("id"):
                pbs.append(Playbook(raw))
        return cls(pbs)

    def active(self, codes: list[str], flag_rules: set[str],
               recent: list | None = None) -> list[Playbook]:
        return [pb for pb in self.playbooks if pb.matches(codes, flag_rules, recent)]

    def find_cause(self, cause_id: str) -> tuple[Playbook, dict] | tuple[None, None]:
        return self._by_cause.get(cause_id, (None, None))

    def cards_for_cause(self, cause_id: str) -> list[str]:
        _pb, cause = self.find_cause(cause_id)
        return list(cause.get("cards", [])) if cause else []


def load_parts_pricing() -> dict:
    p = DATA_DIR / "parts_mad.yaml"
    return load_file(str(p)) if p.exists() else {}


def load_drive_cycles() -> dict:
    p = DATA_DIR / "drive_cycles.yaml"
    return load_file(str(p)) if p.exists() else {}