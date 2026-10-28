"""Kickoff step 1: ledger + event schema (doc 14 §2, doc 12 §3)."""

import json

from mecanic.ledger import Ledger, SessionState, fold, evidence_class_of
from mecanic.schema import CapabilityContract, EVENT_TYPES, event


def _ledger(tmp_path) -> Ledger:
    return Ledger("test-session", tmp_path / "s.jsonl")


def test_event_types_are_closed():
    ev = event(0, 0.0, "pid_sample", "test", {"pid": "RPM", "value": 800, "unit": "rpm"})
    assert ev["type"] in EVENT_TYPES
    try:
        event(0, 0.0, "bogus_type", "test", {})
        raised = False
    except ValueError:
        raised = True
    assert raised


def test_ledger_appends_jsonl_and_folds(tmp_path):
    led = _ledger(tmp_path)
    led.append("session_start", "app", {"session_id": "x", "mode": "AUTO"})
    led.append("vin", "autointake", {"vin": "VF1AAA"})
    led.append("dtc", "autointake", {"code": "P0171", "kind": "stored"})
    led.append("pid_sample", "live", {"pid": "RPM", "value": 810, "unit": "rpm"})
    led.append("dtc", "autointake", {"code": "P0171", "kind": "permanent"})
    led.close()

    raw = [json.loads(l) for l in (tmp_path / "s.jsonl").read_text().splitlines()]
    assert [r["type"] for r in raw] == ["session_start", "vin", "dtc", "pid_sample", "dtc"]

    st = fold(raw)
    assert isinstance(st, SessionState)
    assert st.vin == "VF1AAA"
    assert st.dtc_codes("stored") == ["P0171"]
    assert st.dtc_codes("permanent") == ["P0171"]
    assert st.samples[0]["value"] == 810


def test_seq_is_monotonic_and_ts_increases(tmp_path):
    led = _ledger(tmp_path)
    t0 = led.now()
    for i in range(5):
        led.append("pid_sample", "live", {"pid": "RPM", "value": 800 + i, "unit": "rpm"})
    assert [e["seq"] for e in led.events] == [0, 1, 2, 3, 4]
    assert led.events[-1]["ts"] >= t0


def test_evidence_classes_cover_citation_vocabulary():
    # doc 00 step 6: [OBD] [INTAKE] [PHYSICAL] [MEASUREMENT] [PHOTO]
    classes = {evidence_class_of(t) for t in EVENT_TYPES}
    assert {"OBD", "INTAKE", "PHYSICAL", "MEASUREMENT", "PHOTO", "AGENT"} <= classes


def test_capability_contract_filters_tier_c(tmp_path):
    # doc 14 §4: actuation cards don't appear on Tier A/B — by construction
    tier_ab = CapabilityContract(producer="emulator:x")
    assert not tier_ab.supports("actuation:EGR_SWEEP")
    assert not tier_ab.supports("pid:dpf_soot_load")
    tier_c = CapabilityContract(producer="J2534", actuation=["EGR_SWEEP"],
                                enhanced_pids=["dpf_soot_load"])
    assert tier_c.supports("actuation:EGR_SWEEP")
    assert tier_c.supports("pid:dpf_soot_load")
