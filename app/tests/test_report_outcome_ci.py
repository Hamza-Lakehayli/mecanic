"""Kickoff steps 6–9: report generator, verification, VIN memory, outcome
form (the flywheel), the §4.1 CI benchmark, and the tolerant CSV producer."""

import json

import pytest

from mecanic.cli import main
from mecanic.emulator import SCENARIOS_DIR
from mecanic.funnel import token_estimate
from mecanic.ledger import Ledger, fold, OUTCOMES_PATH, VIN_DIR
from mecanic.llm import NemotronClient
from mecanic.record import load_vin_profile, record_outcome, record_visit

# hermetic: offline tests never touch the live API
OFFLINE_LLM = NemotronClient(api_key="")


def _full_run(scenario_name, tmp_path):
    from mecanic.session import Session
    from mecanic.emulator import load_scenario, open_scenario_transport

    path = SCENARIOS_DIR / scenario_name
    scenario = load_scenario(path)
    transport = open_scenario_transport(path)
    ledger = Ledger("rep-test", tmp_path / "rep.jsonl")
    s = Session(ledger, transport, scenario, llm=OFFLINE_LLM)
    complaint = (scenario.get("complaint", {}) or {})
    s.quick_start(text=complaint.get("text", ""), chips=complaint.get("chips", []))
    s.connect_and_intake()
    s.live_watch()
    state, ranking, cards, nba, digest = s.analyze()
    s.simulate_steps(scenario.get("simulate", []))
    if scenario.get("simulate"):
        state, ranking, cards, nba, digest = s.analyze()
    result = s.conclude(state, ranking, cards, nba, digest)
    ledger.close()
    return s, state, result


def test_report_renders_evidence_and_mad(tmp_path):
    _s, state, result = _full_run("p0171_vacuum_leak.yaml", tmp_path)
    md = open(result["report_path"], encoding="utf-8").read()
    assert "Dossier Mécanic" in md
    assert "[PHYSICAL]" in md          # typed evidence citations (doc 00 step 6)
    assert "spray" in md.lower() or "NBA" in md or "pulvérisation" in md
    assert "MAD" in md                 # parts priced in MAD (doc 15 stage 7)
    assert "consentement" in md.lower()  # consent gate for code clear (doc 02 §7)
    assert "drive cycle" in md.lower() or "conduite" in md
    assert "P0171" in md


def test_partial_report_when_no_verdict(tmp_path):
    from mecanic.session import Session
    from mecanic.emulator import load_scenario, open_scenario_transport
    from mecanic.report import render_report
    from mecanic.verdict import compose

    path = SCENARIOS_DIR / "tamper_cleared_codes.yaml"
    scenario = load_scenario(path)
    transport = open_scenario_transport(path)
    ledger = Ledger("partial-test", tmp_path / "p.jsonl")
    s = Session(ledger, transport, scenario, llm=OFFLINE_LLM)
    s.quick_start(text="Controle avant achat")
    s.connect_and_intake()
    s.live_watch()
    state, ranking, cards, nba, digest = s.analyze()
    vd = compose(state, ranking, nba)
    md = render_report(state, vd, None, None, cards, nba, digest)
    assert "Dossier partiel" in md       # partial dossier is sellable (doc 12 §2)
    assert "effacement" in md.lower()    # the Rapport Confiance finding
    ledger.close()


def test_verification_uses_playbook_drive_cycle(tmp_path):
    from mecanic.session import Session
    from mecanic.emulator import load_scenario, open_scenario_transport
    from mecanic.verification import build_verification

    path = SCENARIOS_DIR / "p0128_thermostat_dci.yaml"
    scenario = load_scenario(path)
    s = Session(Ledger("v-test", tmp_path / "v.jsonl"),
                open_scenario_transport(path), scenario, llm=OFFLINE_LLM)
    s.quick_start(text="x")
    s.connect_and_intake()
    s.live_watch()
    state, ranking, cards, nba, digest = s.analyze()
    s.simulate_steps(scenario["simulate"])
    state, ranking, cards, nba, digest = s.analyze()
    verif = build_verification(state, s.library, {"cause": "thermostat_stuck_open"})
    assert verif["drive_cycle"]["key"] == "renault_dci_drive"  # dCi first (doc 15)
    assert "s'effacera de LUI-MÊME" in " ".join(verif["checklist"])


def test_vin_memory_and_outcome_flywheel(tmp_path):
    _s, state, result = _full_run("tamper_cleared_codes.yaml", tmp_path)

    # stage 9: the visit lands in per-VIN memory (hashed, CNDP-friendly)
    profile = record_visit(state, {"cause": None, "confidence": 0}, None)
    assert len(profile["visits"]) >= 1
    assert profile["visits"][0]["codes"] == ["P0171"]

    # second visit with drifted mode06 -> the trend line appears (doc 02 §3)
    state.mode06[0]["value"] = 55.0
    state.mode06[0]["verdict"] = "fail"
    profile2 = record_visit(state, {"cause": None, "confidence": 0}, None)
    assert any("dérive" in t for t in profile2["trends"])

    # the outcome form closes the loop (doc 12 schema v0)
    rec = record_outcome("rep-test", state.vin,
                         {"fix_part": "catalyseur", "fix_action": "replace",
                          "verified": "yes", "comeback": "no"})
    assert rec["fix_part"] == "catalyseur"
    assert OUTCOMES_PATH.exists()
    last = OUTCOMES_PATH.read_text(encoding="utf-8").strip().splitlines()[-1]
    assert json.loads(last)["verified"] == "yes"
    # VIN stored hashed, never raw (loi 09-08 posture)
    assert state.vin not in json.loads(last)["vin_hash"]


def test_benchmark_ci_reaches_target():
    # doc 17 §4.1: replay all labeled scenarios; agreement >= 80%.
    # Exit code 0 == every expectation passed AND agreement target met.
    assert main(["benchmark", "--dir", str(SCENARIOS_DIR)]) == 0


def test_digest_token_budget(tmp_path):
    _s, state, result = _full_run("p0171_vacuum_leak.yaml", tmp_path)
    # the attention funnel: digest is small; raw ledger is intact underneath
    digest = state.digest["digest"]
    assert token_estimate(digest) < 3000        # doc 16: 1–3K digest budget
    assert len(state.samples) > 1000           # raw stream never deleted


def test_csv_importer_tolerates_wild_formats(tmp_path):
    from mecanic.csvproducer import import_csv

    wild = tmp_path / "wild.csv"
    wild.write_text(
        "Timestamp;ENGINE RPM;MAF[g/s];Long Term Fuel Trim Bank 1;Speed\n"
        "0.0;810;12.1;19.5;0\n0.5;812;12.2;19.4;0\n1.0;2500;26.0;6.0;0\n",
        encoding="utf-8")
    ledger = Ledger("csv-test", tmp_path / "c.jsonl")
    summary = import_csv(wild, ledger)
    assert summary["samples"] == 12  # 4 data columns x 3 rows
    st = fold(ledger.events)
    rpm = [s for s in st.samples if s["pid"] == "RPM"]
    assert [s["value"] for s in rpm] == [810, 812, 2500]
    assert ledger.events[0]["type"] == "pid_sample"  # canonical events only
