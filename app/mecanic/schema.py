"""Canonical event schema (doc 14 §2, doc 12 §3).

Every source becomes a producer writing the same typed events into the
session's evidence ledger; the agent reads only the ledger, never raw
adapter bytes. Event sourcing: append events, fold into session state.

Event envelope:
    {"seq": int,          # monotonic per-session sequence
     "ts": float,         # monotonic device clock, seconds since session start
     "type": str,         # one of EVENT_TYPES below
     "source": str,       # producer/consumer id, e.g. "emulator:p0171", "tech", "agent:ranker"
     "quality": {...},    # optional metadata (adapter id, poll rate, retries)
     "data": {...}}       # type-specific payload

Timestamps: monotonic device clock is primary (doc 14 §5) — no wall-clock
dependency, deterministic under replay.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# --- Event types (the canonical vocabulary) ---------------------------------

EVENT_TYPES = {
    # session lifecycle
    "session_start",     # {session_id, mode: AUTO|INTERACTIVE, label?}
    "session_end",       # {}
    # intake (stage 0 — all optional, all non-blocking)
    "complaint",         # {text?, chips: [chip_id], condition_chips: [...], severity?}
    "history",           # {recent_work: [{system, part, when}], tuning_state: {...}, ...}
    "checklist",         # {system, item, status: OK|BAD|REPLACED|NOT_CHECKED, note?}
    "measurement",       # {metric, value, unit, condition?, spec: {min, max, verdict}}
    "physical_test",     # {test_id, performed, result, location?}   (doc 11 schema v0)
    "media",             # {kind: photo|audio|video, tags: [...], note?}
    "note",              # {text}
    # producer / connection (doc 14 §4 capability contract)
    "producer_connected",  # {producer, capabilities: {...}}
    "producer_disconnected",
    # auto-intake scan (stage 2)
    "vin",               # {vin, decode?}
    "ecu_present",       # {addr, name}
    "dtc",               # {code, kind: stored|pending|permanent, ecu?, mil?}
    "freeze_frame",      # {dtc?, values: {pid_name: value}}
    "readiness",         # {monitors: {name: complete|incomplete|unsupported}, mil, dtc_count, drive_cycles_since_clear?}
    "mode06_result",     # {mid, tid, name, value, min, max, unit, verdict: pass|fail|near_limit}
    "module_scan",       # {modules: [{addr, name?, status: ok|codes|missing}], responding: n}
                         #  — the module health map (doc 12 §2): Phase 1 answers
                         #  "which ECUs spoke" at generic OBD-II depth; Tier C
                         #  upgrades it to the full per-make topology — same event.
    # live stream (funnel L0 watches these)
    "pid_sample",        # {pid, name, value, unit}
    # funnel output (L1/L3, doc 16)
    "window",            # {pid, window: idle|cruise|rev_hold|load|warm_up|snap, stats: {...}}
    "flag",              # {layer, rule, severity: info|warn|danger, message, pid?, refs: [...]}
    "digest",            # {digest: {...}, token_estimate}
    # agent reasoning (stages 3–4)
    "triage_finding",    # {id, kind: fault|drift|tamper|readiness, title, detail, refs, paired_with?}
    "hypothesis_ranking",  # {ranking: [{cause, label, playbook, score, confidence,
                         #   evidence_for: [...], evidence_against: [...]}], trail: [...]}
    # task engine (stage 5, docs 10/11/12)
    "task",              # {task_id, kind: AUTO|NEEDS_YOU, class, title, steps: [...],
                         #  watch: [...], spec?, expected_gain, status, requires_capabilities: [...],
                         #  consent_required?}
    # outputs (stages 6–8)
    "verdict",           # {cause, label, playbook, confidence, statement, citations: [...],
                         #  status: verdict|ranking|escalate}
    "fix_plan",          # {parts: [...], labor_h, procedure: [...], notes}
    "verification",      # {drive_cycle: {...}, checklist: [...], consent_note}
    "record",            # {vin_profile_snapshot?} (stage 9)
    # outcome (the flywheel — doc 12 schema v0)
    "outcome",           # {fix_part, fix_action, verified, comeback, note?}
}

# --- Evidence classes for citations (docs 00/11/12) -------------------------
# Every claim in a verdict/report cites typed evidence: [OBD] [INTAKE]
# [PHYSICAL] [MEASUREMENT] [PHOTO] [AUDIO] [AGENT]

EVIDENCE_CLASS = {
    "vin": "OBD",
    "ecu_present": "OBD",
    "dtc": "OBD",
    "freeze_frame": "OBD",
    "readiness": "OBD",
    "mode06_result": "OBD",
    "module_scan": "OBD",
    "pid_sample": "OBD",
    "window": "OBD",
    "flag": "AGENT",
    "digest": "AGENT",
    "triage_finding": "AGENT",
    "hypothesis_ranking": "AGENT",
    "verdict": "AGENT",
    "complaint": "INTAKE",
    "checklist": "INTAKE",
    "note": "INTAKE",
    "history": "INTAKE",
    "measurement": "MEASUREMENT",
    "physical_test": "PHYSICAL",
    "media": "PHOTO",
    "outcome": "INTAKE",
}

# --- Capability contract (doc 14 §4) ------------------------------------------
# Advertised by every producer at connect time; the task engine filters its
# queue by this contract — actuation cards simply don't appear on Tier A/B.

@dataclass
class CapabilityContract:
    producer: str
    protocols: list = field(default_factory=lambda: ["J1979-classic"])
    modes: list = field(default_factory=lambda: ["01", "02", "03", "06", "07", "09", "0A"])
    enhanced_pids: list = field(default_factory=list)
    actuation: list = field(default_factory=list)  # Phase 2 (Tier C)
    max_poll_hz: float = 4.0

    def supports(self, requirement: str) -> bool:
        if requirement.startswith("actuation:"):
            return requirement.split(":", 1)[1] in self.actuation
        if requirement.startswith("mode:"):
            return requirement.split(":", 1)[1] in self.modes
        if requirement.startswith("pid:"):
            return requirement.split(":", 1)[1] in self.enhanced_pids
        return True

    def to_event_data(self) -> dict:
        return {
            "producer": self.producer,
            "protocols": list(self.protocols),
            "modes": list(self.modes),
            "enhanced_pids": list(self.enhanced_pids),
            "actuation": list(self.actuation),
            "max_poll_hz": self.max_poll_hz,
        }


# Session modes (doc 12): AUTO default, INTERACTIVE opt-in. Modes change only
# *when* stages consume the ledger — never whether the agent starts working.

MODES = ("AUTO", "INTERACTIVE")


def event(seq: int, ts: float, type_: str, source: str, data: dict, quality: dict | None = None) -> dict:
    """Build a canonical event dict."""
    if type_ not in EVENT_TYPES:
        raise ValueError(f"unknown event type: {type_!r}")
    ev = {"seq": seq, "ts": round(float(ts), 3), "type": type_, "source": source, "data": data}
    if quality:
        ev["quality"] = quality
    return ev
