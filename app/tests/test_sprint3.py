"""Sprint 3 (Oct 22, 2026): fuel-aware cards (a dCi has no ignition coils),
post-verdict NBA relabel (confirmation tests, not next-action), the Rapport
Confiance tiers (doc 07 wedge #1), and a spot-check of the marquee DPF
discrimination (regen succeeds → driving pattern, not a dead FAP)."""

import pytest

from mecanic.emulator import SCENARIOS_DIR, load_scenario, open_scenario_transport
from mecanic.ledger import Ledger
from mecanic.llm import NemotronClient
from mecanic.report import confiance_tier, render_confiance_report, render_report
from mecanic.schema import CapabilityContract
from mecanic.session import Session
from mecanic.tasks import visible_cards
from mecanic.verdict import compose

OFFLINE_LLM = NemotronClient(api_key="")


def run(scenario_file, tmp_path, simulate=True, recent_work=True):
    path = SCENARIOS_DIR / scenario_file
    scenario = load_scenario(path)
    transport = open_scenario_transport(path)
    ledger = Ledger("sprint3-test", tmp_path / "r.jsonl")
    s = Session(ledger, transport, scenario, llm=OFFLINE_LLM)
    c = scenario.get("complaint", {}) or {}
    recent = c.get("recent_work") if recent_work else None
    s.quick_start(text=c.get("text", ""), chips=c.get("chips", []), recent_work=recent)
    s.connect_and_intake()
    s.live_watch()
    state, ranking, cards, nba, digest = s.analyze()
    if simulate and scenario.get("simulate"):
        s.simulate_steps(scenario["simulate"])
        state, ranking, cards, nba, digest = s.analyze()
    ledger.close()
    return s, state, ranking, cards, nba, digest


def _row(cause, share):
    return {"cause": cause, "label": cause, "share": share, "confidence": 50}


# -- fuel-aware cards ---------------------------------------------------------

def test_petrol_cards_hidden_on_diesel():
    ranking = {"ranking": [_row("lean_misfire", 0.6), _row("ignition_coil", 0.4)]}
    contract = CapabilityContract(producer="emulator")
    petrol = {c["task_id"] for c in visible_cards(contract, ranking, vehicle={"fuel": "essence"})}
    diesel = {c["task_id"] for c in visible_cards(contract, ranking, vehicle={"fuel": "diesel"})}
    assert {"swap_coil", "photo_spark_plugs"} <= petrol
    assert "swap_coil" not in diesel and "photo_spark_plugs" not in diesel


def test_diesel_cards_hidden_on_petrol():
    ranking = {"ranking": [_row("glow_plug_worn", 0.6), _row("dpf_soot_overload", 0.4)]}
    contract = CapabilityContract(producer="emulator")
    diesel = {c["task_id"] for c in visible_cards(contract, ranking, vehicle={"fuel": "diesel"})}
    petrol = {c["task_id"] for c in visible_cards(contract, ranking, vehicle={"fuel": "essence"})}
    assert {"gauge_glow_plugs", "dpf_highway_regen"} <= diesel
    assert "gauge_glow_plugs" not in petrol and "dpf_highway_regen" not in petrol


def test_unknown_fuel_means_no_filtering():
    # live adapter with no VIN decode yet → cards pass (safe default)
    ranking = {"ranking": [_row("ignition_coil", 0.9)]}
    contract = CapabilityContract(producer="emulator")
    ids = {c["task_id"] for c in visible_cards(contract, ranking, vehicle={})}
    assert "swap_coil" in ids


# -- post-verdict relabel -------------------------------------------------------

def test_post_verdict_report_says_confirmation(tmp_path):
    _s, state, ranking, cards, nba, digest = run("p0171_vacuum_leak.yaml", tmp_path)
    assert ranking["status"] == "verdict"
    md = render_report(state, compose(state, ranking, nba), None, None, cards, nba, digest)
    assert "Confirmation recommandée" in md
    assert "sépare les finalistes" not in md


def test_pre_verdict_report_keeps_nba(tmp_path):
    _s, state, ranking, cards, nba, digest = run("misconfig_injector_dci.yaml", tmp_path,
                                                 simulate=False)
    assert ranking["status"] != "verdict"
    md = render_report(state, compose(state, ranking, nba), None, None, cards, nba, digest)
    assert "Prochaine meilleure action (NBA)" in md
    assert nba is not None and nba["task_id"] == "verify_injector_coding"


# -- Rapport Confiance tiers (doc 07 wedge #1) ----------------------------------

def test_confiance_red_on_tamper(tmp_path):
    _s, state, *_ = run("tamper_cleared_codes.yaml", tmp_path)
    tier, headline = confiance_tier(state)
    assert tier == "red" and "effac" in headline.lower()
    md = render_confiance_report(state)
    assert "Manipulation probable" in md
    assert "NE peut PAS vérifier" in md   # the honesty box sells the report


def test_confiance_green_on_healthy(tmp_path):
    _s, state, *_ = run("healthy_dci.yaml", tmp_path)
    tier, _headline = confiance_tier(state)
    assert tier == "green"
    md = render_confiance_report(state)
    assert "Aucun défaut détecté" in md


def test_confiance_incomplete_on_ignition_off(tmp_path):
    _s, state, *_ = run("ignition_off.yaml", tmp_path)
    tier, headline = confiance_tier(state)
    assert tier == "incomplete" and "NON évalué" in headline


# -- the marquee DPF verdict: the highway regen test saves the FAP --------------

def test_dpf_regen_success_is_driving_pattern_not_dead_fap(tmp_path):
    _s, state, ranking, cards, nba, digest = run("p2463_dpf_urban.yaml", tmp_path)
    assert ranking["status"] == "verdict"
    top = ranking["ranking"][0]
    assert top["cause"] == "dpf_soot_overload"
    assert top["confidence"] >= 70
    # the freeze-frame rule fired and is cited evidence
    rules = {f["rule"] for f in state.flags}
    assert "urban_profile_freeze" in rules
    assert any("urban_profile_freeze" in (ev.get("rule") or "") for ev in top["evidence_for"])
