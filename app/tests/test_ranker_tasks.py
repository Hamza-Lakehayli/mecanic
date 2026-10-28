"""Kickoff steps 4–5: the ranker (deterministic + playbook priors), NBA
selection, capability filtering, verdict thresholds, citations."""

import pytest

from mecanic.emulator import SCENARIOS_DIR
from mecanic.ledger import Ledger, fold
from mecanic.llm import NemotronClient
from mecanic.patterns import PatternLibrary
from mecanic.ranker import Ranker, RANKING_THRESHOLD, VERDICT_THRESHOLD
from mecanic.schema import CapabilityContract
from mecanic.tasks import (CARD_CATALOG, MAX_VISIBLE_CARDS, nba_card,
                           visible_cards)
from mecanic.verdict import compose

# hermetic: offline tests never touch the live API
OFFLINE_LLM = NemotronClient(api_key="")

LIB = PatternLibrary.load()


def run_session(scenario_name, tmp_path, simulate=True):
    from mecanic.session import Session
    from mecanic.emulator import load_scenario, open_scenario_transport

    path = SCENARIOS_DIR / scenario_name
    scenario = load_scenario(path)
    transport = open_scenario_transport(path)
    ledger = Ledger("rank-test", tmp_path / "r.jsonl")
    s = Session(ledger, transport, scenario, llm=OFFLINE_LLM)
    complaint = (scenario.get("complaint", {}) or {})
    s.quick_start(text=complaint.get("text", ""), chips=complaint.get("chips", []))
    s.connect_and_intake()
    s.live_watch()
    state, ranking, cards, nba, digest = s.analyze()
    first = (state, ranking, cards, nba, digest)
    if simulate and scenario.get("simulate"):
        s.simulate_steps(scenario["simulate"])
        state, ranking, cards, nba, digest = s.analyze()
    ledger.close()
    return s, first, (state, ranking, cards, nba, digest)


def test_vacuum_ranks_vacuum_leak_before_test(tmp_path):
    _s, (state, ranking, _c, _n, _d), _post = run_session("p0171_vacuum_leak.yaml", tmp_path,
                                                          simulate=False)
    top = ranking["ranking"][0]
    assert top["cause"] == "vacuum_leak"
    assert top["evidence_for"]  # explainability trail populated (doc 12)
    trail_rules = [t["text"] for t in ranking["trail"] if "trim_idle_dominant" in t["text"]]
    assert trail_rules  # "this flag moved vacuum leak, here's why"


def test_maf_scenario_discriminates_same_code(tmp_path):
    # the two P0171 scenarios must rank DIFFERENT root causes (doc 02 §2)
    _s, (_st1, r1, _c1, _n1, _d1), _p1 = run_session("p0171_vacuum_leak.yaml", tmp_path, simulate=False)
    _s, (_st2, r2, _c2, _n2, _d2), _p2 = run_session("p0171_maf_dirty.yaml", tmp_path, simulate=False)
    assert r1["ranking"][0]["cause"] == "vacuum_leak"
    assert r2["ranking"][0]["cause"] == "maf_underreport"


def test_test_card_confirms_and_reaches_verdict(tmp_path):
    s, _pre, (state, ranking, cards, nba, digest) = run_session("p0171_vacuum_leak.yaml", tmp_path)
    top = ranking["ranking"][0]
    assert ranking["status"] == "verdict"
    assert top["cause"] == "vacuum_leak"
    assert top["confidence"] >= VERDICT_THRESHOLD
    phys = [c for c in top["evidence_for"] if c["class"] == "PHYSICAL"]
    assert phys and "durite PCV" in phys[0]["text"]  # typed citation [PHYSICAL]


def test_nba_is_one_card_and_targets_top_hypothesis(tmp_path):
    _s, (_state, ranking, cards, nba, _d), _post = run_session("p0300_coil_cyl2.yaml", tmp_path,
                                                               simulate=False)
    assert nba is not None and nba["status"] == "queued"
    assert nba["task_id"] == "swap_coil"          # the discriminating 2-minute test
    assert len([c for c in cards]) <= MAX_VISIBLE_CARDS  # anti-fatigue guard


def test_below_threshold_ranking_not_verdict(tmp_path):
    # before the swap test the misfire case must NOT guess (doc 02 §7)
    _s, (_state, ranking, _c, _nba, _d), _post = run_session("p0300_coil_cyl2.yaml", tmp_path,
                                                             simulate=False)
    assert ranking["status"] != "verdict"


def test_capability_filtering_hides_tier_c_cards(tmp_path):
    contract = CapabilityContract(producer="emulator")  # read-only, doc 14 §4
    ranking = {"ranking": [{"cause": "injector_clogged", "label": "x", "share": 0.8, "confidence": 55}]}
    cards = visible_cards(contract, ranking)
    ids = {c["task_id"] for c in cards}
    assert "injector_balance_tierC" not in ids and "egr_sweep_tierC" not in ids
    # Tier C contract unlocks them
    contract_c = CapabilityContract(producer="J2534/CarDAQ-Plus3",
                                    actuation=["INJ_CUTOUT", "EGR_SWEEP"],
                                    enhanced_pids=["dpf_soot_load"])
    ids_c = {c["task_id"] for c in visible_cards(contract_c, ranking)}
    assert "injector_balance_tierC" in ids_c


def test_verdict_composer_status_and_citations(tmp_path):
    s, _pre, (state, ranking, cards, nba, digest) = run_session("p0562_charging.yaml", tmp_path)
    vd = compose(state, ranking, nba)
    assert vd["status"] == "verdict"
    assert vd["cause"] == "alternator_fault"
    classes = {c["class"] for c in vd["citations"]}
    assert "INTAKE" in classes  # the battery-light chip is citable evidence
    assert any("O2" not in c["text"] for c in vd["citations"])


def test_playbooks_load_and_have_fixes():
    ids = {pb.id for pb in LIB.playbooks}
    assert {"lean_family", "misfire_family", "catalyst_family",
            "thermostat_family", "charging_family", "egr_family",
            "misconfig_family"} <= ids
    for pb in LIB.playbooks:
        for cause in pb.causes:
            if cause.get("fix", {}).get("parts"):
                assert cause["fix"]["labor_h"] >= 0
            for card in cause.get("cards", []):
                assert card in CARD_CATALOG, f"{pb.id}/{cause['id']}: unknown card {card}"


def test_ranker_offline_never_blocks():
    # no LLM key, no network: analyze still works (offline-first, doc 12)
    class FakeState:
        dtc_codes = lambda self, kind=None: ["P0171"]
        chips = ["hissing"]
        condition_chips = []
        measurements = []
        physical_tests = []
        flags = []
        history = {}
        vehicle = {}
    r = Ranker(LIB).analyze(FakeState())
    assert r["ranking"][0]["cause"] == "vacuum_leak"
