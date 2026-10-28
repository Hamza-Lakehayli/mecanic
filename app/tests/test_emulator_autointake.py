"""Kickoff steps 2–3: the emulator producer + auto-intake through the REAL
driver code path (doc 14 §3: emulator-first tests the actual pipeline)."""

from pathlib import Path

import pytest

from mecanic.autointake import AutoIntake, decode_mode06
from mecanic.emulator import (SCENARIOS_DIR, capability_from_scenario,
                              load_scenario, open_scenario_transport)
from mecanic.elm327 import ELM327Driver
from mecanic.ledger import fold, Ledger

VACUUM = SCENARIOS_DIR / "p0171_vacuum_leak.yaml"


def _intake(scenario_path, tmp_path):
    scenario = load_scenario(scenario_path)
    transport = open_scenario_transport(scenario_path)
    ledger = Ledger("it-test", tmp_path / "it.jsonl")
    driver = ELM327Driver(transport)
    caps = capability_from_scenario(scenario).to_event_data()
    AutoIntake(ledger, driver, caps, scenario["vehicle"].get("decode", {})).run()
    ledger.close()
    return ledger, transport, driver


def test_scenario_transport_speaks_elm327_wire(tmp_path):
    transport = open_scenario_transport(VACUUM)
    d = ELM327Driver(transport)
    info = d.connect()
    assert "ELM327" in info["chip_id"]
    assert d.readiness()["mil"] is True
    assert d.dtcs() == {"stored": ["P0171"], "pending": [], "permanent": ["P0171"]}
    assert d.vin() == "UU1HSDC3456123456"
    ff = d.freeze_frame()
    assert ff["dtc"] == "P0171"
    # trims quantize to 128/100 steps per byte on the wire — ±0.4% is expected
    assert ff["values"]["LTFT_B1"] == pytest.approx(21.0, abs=0.5)
    m06 = d.mode06()
    assert m06[0]["mid"] == 1 and m06[0]["value"] == 0.8


def test_auto_intake_emits_canonical_events(tmp_path):
    ledger, transport, driver = _intake(VACUUM, tmp_path)
    st = fold(ledger.events)
    types = {e["type"] for e in ledger.events}
    assert {"producer_connected", "vin", "readiness", "dtc", "freeze_frame",
            "mode06_result", "ecu_present", "pid_sample"} <= types
    assert st.vin == "UU1HSDC3456123456"
    assert st.vehicle["make"] == "Dacia"
    assert st.dtc_codes("stored") == ["P0171"]
    assert st.dtc_codes("permanent") == ["P0171"]
    assert st.readiness["monitors"]["evap"] == "incomplete"
    assert any(m["name"].startswith("Temps de réponse") for m in st.mode06)


def test_live_watch_polls_through_the_driver(tmp_path):
    ledger, transport, driver = _intake(VACUUM, tmp_path)
    for _ in range(5):
        transport.tick(1.0)
        for s in driver.poll_cycle(["RPM", "LTFT_B1"]):
            ledger.append("pid_sample", "live_watch",
                           {"pid": s["name"], "name": s["name"], "value": s["value"],
                            "unit": s["unit"]})
    st = fold(ledger.events)
    live = [s for s in st.samples if s["pid"] == "RPM"]
    assert len(live) >= 5
    assert all(900 <= s["value"] <= 1100 for s in live)  # warm-up phase RPM=1000


def test_mode06_decode_verdicts():
    raw = [{"mid": 1, "tid": 5, "value": 63.0, "min": 60.0, "max": 100.0},
           {"mid": 1, "tid": 5, "value": 55.0, "min": 60.0, "max": 100.0},
           {"mid": 1, "tid": 5, "value": 88.0, "min": 60.0, "max": 100.0}]
    out = decode_mode06(raw)
    assert [r["verdict"] for r in out] == ["near_limit", "fail", "pass"]
    assert out[0]["name"] == "Rendement catalyseur B1"


def test_capability_contract_is_read_only(tmp_path):
    sc = load_scenario(VACUUM)
    caps = capability_from_scenario(sc)
    assert caps.to_event_data()["actuation"] == []
    assert not caps.supports("actuation:EGR_SWEEP")
