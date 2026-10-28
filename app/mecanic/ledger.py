"""The evidence ledger (docs 12/14): append-only, typed, JSONL, offline-first.

"Live" is just consuming events as they arrive; replay re-consumes a stored
session for debugging, regression tests, and pattern-library building.
One consumer, three timings — the same fold drives all of them.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from .schema import EVIDENCE_CLASS, event

# Where runtime artifacts live: app/var/ (sessions, VIN profiles, outcomes)
VAR_DIR = Path(__file__).resolve().parent.parent / "var"
SESSIONS_DIR = VAR_DIR / "sessions"
VIN_DIR = VAR_DIR / "vin_profiles"
OUTCOMES_PATH = VAR_DIR / "outcomes.jsonl"


def _ensure_dirs():
    for d in (VAR_DIR, SESSIONS_DIR, VIN_DIR):
        d.mkdir(parents=True, exist_ok=True)


class Ledger:
    """Append-only event log for one diagnostic session (the dossier).

    Writes JSONL to disk as it goes (offline-first, append is native to
    JSONL); keeps the in-memory list for folding and replay.
    """

    def __init__(self, session_id: str, path: Path | None = None):
        self.session_id = session_id
        self.events: list[dict] = []
        self._seq = 0
        self._t0 = time.monotonic()
        self.path = path
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)
            self._fh = open(path, "a", encoding="utf-8")
        else:
            self._fh = None

    # -- append ------------------------------------------------------------

    def now(self) -> float:
        """Monotonic session clock (seconds since ledger creation)."""
        return time.monotonic() - self._t0

    def append(self, type_: str, source: str, data: dict, ts: float | None = None,
               quality: dict | None = None) -> dict:
        ev = event(self._seq, self.now() if ts is None else ts, type_, source, data, quality)
        self._seq += 1
        self.events.append(ev)
        if self._fh is not None:
            self._fh.write(json.dumps(ev, ensure_ascii=False) + "\n")
            self._fh.flush()
        return ev

    def close(self):
        if self._fh is not None:
            self._fh.close()
            self._fh = None

    # -- fold: events -> session state ---------------------------------------

    def fold(self) -> "SessionState":
        return fold(self.events)


class SessionState:
    """The folded view of a session — what consumers read.

    Nothing in the system reads the dossier any other way: every consumer
    (triage, ranker, task engine, report) consumes SessionState.
    """

    def __init__(self):
        self.session_id: str | None = None
        self.mode: str = "AUTO"
        self.producer: dict | None = None
        self.capabilities: dict = {}
        self.vin: str | None = None
        self.vehicle: dict = {}          # decode of the VIN/emulator vehicle record
        self.dtcs: list[dict] = []
        self.freeze_frames: list[dict] = []
        self.readiness: dict | None = None
        self.mode06: list[dict] = []
        self.module_scan: dict | None = None   # the module health map (doc 12 §2)
        self.ecus: list[dict] = []
        self.samples: list[dict] = []    # pid_sample events (raw, kept forever)
        self.windows: list[dict] = []
        self.flags: list[dict] = []
        self.complaint: dict | None = None
        self.chips: list[str] = []
        self.condition_chips: list[str] = []
        self.history: dict | None = None
        self.checklist: list[dict] = []
        self.measurements: list[dict] = []
        self.physical_tests: list[dict] = []
        self.media: list[dict] = []
        self.notes: list[str] = []
        self.triage_findings: list[dict] = []
        self.rankings: list[dict] = []
        self.tasks: list[dict] = []
        self.verdict: dict | None = None
        self.fix_plan: dict | None = None
        self.verification: dict | None = None
        self.outcome: dict | None = None
        self.digest: dict | None = None

    # convenience -------------------------------------------------------------
    def latest_windows(self, name: str) -> list[dict]:
        """All folded windows for one operating window name."""
        return [w for w in self.windows if w.get("window") == name]

    def pid_windows(self, pid: str, window: str) -> list[dict]:
        return [w for w in self.windows
                if w.get("window") == window and w.get("pid") == pid]

    def dtc_codes(self, kind: str | None = None) -> list[str]:
        out = []
        for d in self.dtcs:
            if kind is None or d.get("kind") == kind:
                code = d.get("code")
                if code and code not in out:
                    out.append(code)
        return out


def fold(events: list[dict]) -> SessionState:
    """Fold an event list into session state (the event-sourcing reducer)."""
    st = SessionState()
    st._events = events  # kept for drill-down tools (doc 16 L4 replay_segment)
    for ev in events:
        d = ev["data"]
        t = ev["type"]
        if t == "session_start":
            st.session_id = d.get("session_id")
            st.mode = d.get("mode", "AUTO")
        elif t == "producer_connected":
            st.producer = d
            st.capabilities = {k: v for k, v in d.items() if k != "producer"}
        elif t == "vin":
            st.vin = d.get("vin")
            st.vehicle = d.get("decode", {})
        elif t == "ecu_present":
            st.ecus.append({**d, "_ev": ev["seq"], "_ts": ev["ts"]})
        elif t == "dtc":
            st.dtcs.append({**d, "_ev": ev["seq"], "_ts": ev["ts"]})
        elif t == "freeze_frame":
            st.freeze_frames.append({**d, "_ev": ev["seq"], "_ts": ev["ts"]})
        elif t == "readiness":
            st.readiness = {**d, "_ev": ev["seq"], "_ts": ev["ts"]}
        elif t == "mode06_result":
            st.mode06.append({**d, "_ev": ev["seq"], "_ts": ev["ts"]})
        elif t == "module_scan":
            st.module_scan = {**d, "_ev": ev["seq"], "_ts": ev["ts"]}
        elif t == "pid_sample":
            st.samples.append({**d, "_ev": ev["seq"], "_ts": ev["ts"]})
        elif t == "window":
            st.windows.append({**d, "_ev": ev["seq"], "_ts": ev["ts"]})
        elif t == "flag":
            st.flags.append({**d, "_ev": ev["seq"], "_ts": ev["ts"]})
        elif t == "complaint":
            st.complaint = {**d, "_ev": ev["seq"], "_ts": ev["ts"]}
            st.chips = list(d.get("chips", []))
            st.condition_chips = list(d.get("condition_chips", []))
        elif t == "history":
            st.history = {**d, "_ev": ev["seq"], "_ts": ev["ts"]}
        elif t == "checklist":
            st.checklist.append({**d, "_ev": ev["seq"], "_ts": ev["ts"]})
        elif t == "measurement":
            st.measurements.append({**d, "_ev": ev["seq"], "_ts": ev["ts"]})
        elif t == "physical_test":
            st.physical_tests.append({**d, "_ev": ev["seq"], "_ts": ev["ts"]})
        elif t == "media":
            st.media.append({**d, "_ev": ev["seq"], "_ts": ev["ts"]})
        elif t == "note":
            st.notes.append(d.get("text", ""))
        elif t == "triage_finding":
            st.triage_findings.append({**d, "_ev": ev["seq"], "_ts": ev["ts"]})
        elif t == "hypothesis_ranking":
            st.rankings.append({**d, "_ev": ev["seq"], "_ts": ev["ts"]})
        elif t == "task":
            st.tasks.append({**d, "_ev": ev["seq"], "_ts": ev["ts"]})
        elif t == "verdict":
            st.verdict = {**d, "_ev": ev["seq"], "_ts": ev["ts"]}
        elif t == "fix_plan":
            st.fix_plan = {**d, "_ev": ev["seq"], "_ts": ev["ts"]}
        elif t == "verification":
            st.verification = {**d, "_ev": ev["seq"], "_ts": ev["ts"]}
        elif t == "outcome":
            st.outcome = {**d, "_ev": ev["seq"], "_ts": ev["ts"]}
        elif t == "digest":
            st.digest = {**d, "_ev": ev["seq"], "_ts": ev["ts"]}
    return st


def evidence_class_of(event_type: str) -> str:
    return EVIDENCE_CLASS.get(event_type, "AGENT")


# -- replay -------------------------------------------------------------------

def replay_file(path: str | Path) -> SessionState:
    """Replay a stored JSONL session — free regression test (doc 14 §6.2)."""
    events = []
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                events.append(json.loads(line))
    return fold(events)


def new_session(session_id: str | None = None, persist: bool = True) -> Ledger:
    """Create a Ledger with the standard on-disk location under app/var/sessions."""
    _ensure_dirs()
    import datetime as _dt

    if session_id is None:
        session_id = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    path = SESSIONS_DIR / f"{session_id}.jsonl" if persist else None
    return Ledger(session_id, path)
