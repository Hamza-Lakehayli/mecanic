"""Kickoff steps 3–4: the funnel (L0/L1) and triage — windows derived from
data, the doc-02 §2 discrimination table as rules, tamper forensics."""

import pytest

from mecanic.emulator import SCENARIOS_DIR
from mecanic.funnel import (build_windows, delta_compress, maf_expected_idle,
                            run_funnel, total_trims, window_map)
from mecanic.ledger import Ledger, fold
from mecanic.llm import NemotronClient
from mecanic.triage import Triage

# hermetic: offline pipeline tests never touch the live API — the LLM path
# has its own dedicated live tests (test_llm_live.py)
OFFLINE_LLM = NemotronClient(api_key="")


def run_pipeline(scenario_name, tmp_path, quick_start=None):
    from mecanic.autointake import AutoIntake
    from mecanic.emulator import capability_from_scenario, load_scenario, open_scenario_transport
    from mecanic.elm327 import ELM327Driver
    from mecanic.session import Session

    path = SCENARIOS_DIR / scenario_name
    scenario = load_scenario(path)
    transport = open_scenario_transport(path)
    ledger = Ledger("funnel-test", tmp_path / "f.jsonl")
    session = Session(ledger, transport, scenario, llm=OFFLINE_LLM)
    session.quick_start(**(quick_start or {}))
    session.connect_and_intake()
    session.live_watch()
    ledger.close()
    return session, fold(ledger.events)


def test_delta_compress_keeps_changes_only():
    samples = [{"pid": "ECT", "value": v, "ts": float(i)} for i, v in enumerate([90] * 50 + [91])]
    out = delta_compress(samples)
    assert len(out) == 2


def test_windows_derived_from_data_not_labels(tmp_path):
    _session, st = run_pipeline("p0171_vacuum_leak.yaml", tmp_path)
    windows, flags, _ = run_funnel(st)
    wmap = window_map(windows)
    # idle and rev_hold exist with distinct trims — classified from RPM/speed
    assert wmap[("LTFT_B1", "idle")]["mean"] == pytest.approx(19, abs=1.5)
    assert wmap[("LTFT_B1", "rev_hold")]["mean"] == pytest.approx(6, abs=1.5)
    assert wmap[("RPM", "rev_hold")]["mean"] == pytest.approx(2500, abs=60)


def test_trim_idle_dominant_flag_fires(tmp_path):
    _session, st = run_pipeline("p0171_vacuum_leak.yaml", tmp_path)
    windows, flags, _ = run_funnel(st)
    rules = {f["rule"] for f in flags}
    assert "trim_idle_dominant" in rules
    assert "trim_lean_overall" in rules
    flag = next(f for f in flags if f["rule"] == "trim_idle_dominant")
    assert flag["severity"] == "warn"
    assert flag["values"]["idle_total"] > flag["values"]["high_total"]


def test_maf_discriminator_separates_same_code(tmp_path):
    _s1, st1 = run_pipeline("p0171_vacuum_leak.yaml", tmp_path)
    _s2, st2 = run_pipeline("p0171_maf_dirty.yaml", tmp_path)
    rules1 = {f["rule"] for f in run_funnel(st1)[1]}
    rules2 = {f["rule"] for f in run_funnel(st2)[1]}
    # same DTC family, opposite signatures (doc 02 §2 table rows 1–2)
    assert "trim_idle_dominant" in rules1 and "trim_idle_dominant" not in rules2
    assert "trim_flow_scaled" in rules2 and "maf_underreport" in rules2


def test_warmup_and_voltage_rules(tmp_path):
    _s, st_therm = run_pipeline("p0128_thermostat_dci.yaml", tmp_path)
    rules = {f["rule"] for f in run_funnel(st_therm)[1]}
    assert "warmup_slow" in rules

    _s2, st_charging = run_pipeline("p0562_charging.yaml", tmp_path)
    rules2 = {f["rule"] for f in run_funnel(st_charging)[1]}
    assert "voltage_low" in rules2


def test_healthy_scenario_stays_silent(tmp_path):
    # doc 16 §3: zero flags + zero codes is itself a finding — no false alarms
    _s, st = run_pipeline("healthy_dci.yaml", tmp_path)
    windows, flags, _ = run_funnel(st)
    assert flags == []
    findings = Triage().analyze(st)
    assert findings == []


def test_tamper_forensics(tmp_path):
    _s, st = run_pipeline("tamper_cleared_codes.yaml", tmp_path,
                           {"text": "Controle avant achat", "chips": []})
    findings = {f["id"]: f for f in Triage().analyze(st)}
    assert "tamper_cleared_codes" in findings
    assert "P0171" in findings["tamper_cleared_codes"]["detail"]
    assert findings["tamper_cleared_codes"]["severity"] == "danger"
    assert "drift_1_5" in findings  # monitor passing but drifting toward limit
    assert "readiness_incomplete" not in findings  # tamper finding takes precedence


def test_paired_statement_hissing_matches_trims(tmp_path):
    _s, st = run_pipeline("p0171_vacuum_leak.yaml", tmp_path,
                          {"text": "Sifflement d'air au ralenti", "chips": ["hissing", "idle_only"]})
    # fold flags into state for triage's paired statements
    windows, flags, _ = run_funnel(st)
    for w in windows:
        st.windows.append({**w, "_ev": 0, "_ts": 0})
    for f in flags:
        st.flags.append({**f, "_ev": 0, "_ts": 0})
    findings = {f["id"] for f in Triage().analyze(st)}
    assert "paired_hissing_trims" in findings


def test_maf_expected_idle_formula():
    # 1.6 L petrol at 800 rpm, 20% calculated load ≈ 2.5 g/s known-good
    # (LOAD-normalized; calibrated on VED: healthy fleet ratio ≈ 1.0)
    assert maf_expected_idle(800, 1.6, 20) == pytest.approx(2.5, abs=0.2)


def test_mixed_fr_darija_lexicon_extracts():
    # the real garage register: FR + Darija code-switched in ONE sentence
    from mecanic.session import extract_chips_lexicon

    got = extract_chips_lexicon("msha do men l motor")
    assert "mil_on" in got  # "msha daw/do" = le voyant moteur s'est allumé
    got = extract_chips_lexicon("la porte dyal tomobil makat7elsh")
    assert "door_window_issue" in got  # mechanical bank (route-out)


def test_mechanical_complaint_routes_out(tmp_path):
    # doc 00 scope honesty: a door that won't open is NOT data-detectable —
    # triage must route it to physical inspection, never invent an engine
    # hypothesis (and that honesty is what sells the other verdicts).
    _s, st = run_pipeline("healthy_dci.yaml", tmp_path,
                          {"text": "la porte dyal tomobil makat7elsh"})
    findings = {f["id"]: f for f in Triage().analyze(st)}
    assert "mechanical_refer_out" in findings
    assert "porte" in findings["mechanical_refer_out"]["detail"].lower()
    assert st.chips == [] and st.condition_chips == []  # no engine chips invented
    mech = (st.complaint or {}).get("mechanical_chips", [])
    assert "door_window_issue" in mech


def test_comeback_prior_from_recent_work(tmp_path):
    _s, st = run_pipeline("p0171_vacuum_leak.yaml", tmp_path,
                          {"text": "", "recent_work": [{"system": "admission", "part": "durite", "when": "la semaine derniere"}]})
    findings = {f["id"] for f in Triage().analyze(st)}
    assert any(fid.startswith("comeback_") for fid in findings)
