"""Phase-1 additions (Oct 2026 research round), all four items:

  1. The used-parts misconfiguration pattern family (doc 02 §3) + its labeled
     scenario — history-gated activation, the coding card confirms the verdict.
  2. The module health map event (doc 12 §2) — the topology view as evidence.
  3. The ignition guard (doc 12 §2) — a key-off car is never "a dead bus".
  4. Post-repair configuration steps carried in every fix plan (doc 15 stage 7).
"""

import pytest

from mecanic.emulator import SCENARIOS_DIR, load_scenario, open_scenario_transport
from mecanic.ledger import Ledger
from mecanic.llm import NemotronClient
from mecanic.session import Session
from mecanic.tasks import CARD_CATALOG

OFFLINE_LLM = NemotronClient(api_key="")


def run(scenario_file, tmp_path, recent_work=True):
    path = SCENARIOS_DIR / scenario_file
    scenario = load_scenario(path)
    transport = open_scenario_transport(path)
    ledger = Ledger("phase1-test", tmp_path / "r.jsonl")
    s = Session(ledger, transport, scenario, llm=OFFLINE_LLM)
    c = scenario.get("complaint", {}) or {}
    recent = c.get("recent_work") if recent_work else None
    s.quick_start(text=c.get("text", ""), chips=c.get("chips", []), recent_work=recent)
    s.connect_and_intake()
    s.live_watch()
    state, ranking, cards, nba, digest = s.analyze()
    if scenario.get("simulate"):
        s.simulate_steps(scenario["simulate"])
        state, ranking, cards, nba, digest = s.analyze()
    ledger.close()
    return s, state, ranking, cards, nba, digest


def test_misconfig_verdict_used_parts_pattern(tmp_path):
    # injector swapped, never coded: the verdict must be the missing
    # configuration step — NOT another part (comeback prevention, doc 02 §3)
    _s, state, ranking, cards, nba, digest = run("misconfig_injector_dci.yaml", tmp_path)
    top = ranking["ranking"][0]
    assert top["cause"] == "post_repair_config_missing"
    assert ranking["status"] == "verdict"
    assert top["confidence"] >= 70
    # the confirming card result is typed PHYSICAL evidence
    phys = [e for e in top["evidence_for"] if e["class"] == "PHYSICAL"]
    assert phys and "not_coded" in phys[0]["text"]
    # the recent-work prior is in the explainability trail
    assert any("configuration" in t["text"].lower() for t in ranking["trail"])


def test_misconfig_pre_card_is_ranking_not_verdict(tmp_path):
    # before the card answers, the agent suspects (ranking) but does not
    # guess — the NBA asks the discriminating question (doc 02 §7)
    _s, state, ranking, cards, nba, digest = run("misconfig_injector_dci.yaml", tmp_path)
    scenario = load_scenario(SCENARIOS_DIR / "misconfig_injector_dci.yaml")
    if not scenario.get("simulate"):
        return
    # simulate=False equivalent: assert on the FIRST analyze via a fresh run
    path = SCENARIOS_DIR / "misconfig_injector_dci.yaml"
    sc = load_scenario(path)
    transport = open_scenario_transport(path)
    ledger = Ledger("phase1-pre", tmp_path / "pre.jsonl")
    s = Session(ledger, transport, sc, llm=OFFLINE_LLM)
    c = sc["complaint"]
    s.quick_start(text=c["text"], chips=c["chips"], recent_work=c["recent_work"])
    s.connect_and_intake()
    s.live_watch()
    _st, pre_ranking, pre_cards, pre_nba, _d = s.analyze()
    ledger.close()
    assert pre_ranking["status"] == "ranking"
    assert pre_ranking["ranking"][0]["cause"] == "post_repair_config_missing"
    assert pre_nba is not None and pre_nba["task_id"] == "verify_injector_coding"


def test_misconfig_needs_history_to_activate(tmp_path):
    # same data, recent work NOT declared: the family stays inactive — a
    # rough idle with nothing recently swapped must not point at misconfig
    _s, state, ranking, _c, _n, _d = run("misconfig_injector_dci.yaml", tmp_path,
                                         recent_work=False)
    assert "misconfig_family" not in ranking["active_playbooks"]
    assert not any(r["cause"] == "post_repair_config_missing"
                   for r in ranking["ranking"])


def test_verify_injector_coding_card_exists():
    card = CARD_CATALOG["verify_injector_coding"]
    assert card["kind"] == "NEEDS_YOU" and card["minutes"] <= 3


def test_module_scan_event_present(tmp_path):
    # the module health map is a canonical event (doc 12 §2) — the topology
    # view as evidence, not decoration
    _s, state, ranking, _c, _n, _d = run("p0171_vacuum_leak.yaml", tmp_path)
    assert state.module_scan and state.module_scan["responding"] >= 1
    assert state.module_scan["modules"][0]["addr"] == "7E8"
    assert state.module_scan["modules"][0]["status"] in ("ok", "codes")


def test_ignition_guard_never_concludes_dead_bus(tmp_path):
    # key-off car: adapter powered, silence — the agent says "check the key",
    # produces the finding, and refuses to guess a verdict
    _s, state, ranking, _c, _n, _d = run("ignition_off.yaml", tmp_path)
    ids = {f["id"] for f in state.triage_findings}
    assert "ignition_off_suspect" in ids
    assert state.module_scan and state.module_scan["responding"] == 0
    assert ranking["status"] == "escalate"


def test_fix_plan_carries_post_repair_steps():
    # doc 15 stage 7: a repair isn't complete until its configuration is
    from mecanic.fixplan import build_fix_plan
    from mecanic.patterns import PatternLibrary
    lib = PatternLibrary.load()
    plan = build_fix_plan("post_repair_config_missing", lib)
    assert plan and plan["post_repair_steps"]
    assert any("codage" in step.lower() for step in plan["post_repair_steps"])
    assert any("C2i" in step for step in plan["procedure"])
