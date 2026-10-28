"""Mecanic Study — the diagnostics agent pipeline.

Implements the design canon locked in `17-design-sweep-decisions-test-matrix.md` §5:

    ledger + event schema -> ELM327/emulator producer -> auto-intake scan
    -> triage rules -> P0171 playbook + ranker -> NBA/test-card UI
    -> report generator -> outcome form -> pattern expansion (the flywheel)

Architecture (doc 14): producer -> evidence ledger (JSONL, append-only)
-> consumer. The agent never sees raw adapter bytes; it reads typed events.
Phase 1 = read-only agent, zero trained models (doc 16): the whole funnel
L0–L3 is pure arithmetic, fully offline.
"""

__version__ = "0.1.0"
