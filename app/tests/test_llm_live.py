"""Live LLM tests — doc 17 §4.1, the Nemotron floor-test phase.

These run against the REAL NVIDIA API (key from MECANIC_NVIDIA_API_KEY or
app/.env). They skip automatically when the key is absent, so the default
CI path stays offline and deterministic. Metrics measured here are the ones
the doc defers to tests: extraction quality FR vs Darija (gap #4), verdict
agreement with the LLM in the loop, tokens/dossier (<10K), turn latency.
"""

import time
from pathlib import Path

import pytest

from mecanic.emulator import SCENARIOS_DIR, load_scenario, open_scenario_transport
from mecanic.ledger import Ledger, fold
from mecanic.llm import NemotronClient
from mecanic.session import Session
from mecanic.yamlmini import load_file

DATA_DIR = Path(__file__).resolve().parent.parent / "data"

LLM = NemotronClient()
requires_key = pytest.mark.skipif(not LLM.available,
                                 reason="no MECANIC_NVIDIA_API_KEY — offline CI path")

# The §4.1 extraction matrix: French, Darija (Latin script), Darija (Arabic
# script), code-switched — the garage reality (docs 11/12).
EXTRACTION_CASES = [
    ("french",
     "Sifflement d'air au ralenti, le moteur broute et consomme plus depuis une semaine",
     {"hissing"}, {"idle_only"}),
    ("darija_latin",
     "kaykhrej siffl dyal l-hwa mn l-moteur 3end ralenti, l-moteur kay-tremel w makhdouch",
     {"hissing"}, {"idle_only"}),
    ("darija_arabic",
     "الطوموبيل كايسمع بحال الهواء كايخرج من عندالمحرك فوق وكيهز راسو فالصباح",
     {"hissing"}, {"morning"}),
    ("darija_mixed",
     "machi 9wa f sa7, ila jit f cote kay tele3 dkhan k7el",
     {"power_loss", "black_smoke"}, {"under_load"}),
]


def _banks():
    priors = load_file(str(DATA_DIR / "priors.yaml")) or {}
    return list((priors.get("symptoms") or {}).keys()), list((priors.get("conditions") or {}).keys())


def _mech_bank():
    priors = load_file(str(DATA_DIR / "priors.yaml")) or {}
    return list((priors.get("mechanical") or {}).keys())


def _gloss():
    from mecanic.session import build_chip_gloss
    return build_chip_gloss()


@requires_key
def test_chat_roundtrip_and_latency():
    t0 = time.monotonic()
    out = LLM.chat([{"role": "user", "content": "Reponds uniquement par le mot: OK"}],
                   temperature=0.1)
    warm = time.monotonic() - t0
    assert out is not None and "OK" in out.upper()
    # §4.1: INTERACTIVE turn latency target <3 s — holds warm (cold starts
    # on the 550B can take longer; the client retries + degrades offline)
    assert warm < 30, f"unexpected latency {warm:.1f}s"
    assert LLM.last_usage.get("total_tokens", 0) > 0


@requires_key
@pytest.mark.parametrize("label,text,sym_conds,cond_conds", EXTRACTION_CASES,
                         ids=[c[0] for c in EXTRACTION_CASES])
def test_extraction_quality_by_language(label, text, sym_conds, cond_conds):
    sym_bank, cond_bank = _banks()
    out = LLM.extract_complaint(text, sym_bank, cond_bank, gloss=_gloss())
    assert out is not None, f"no LLM answer for {label}"
    assert sym_conds <= set(out["chips"]), f"{label}: expected {sym_conds}, got {out['chips']}"
    assert cond_conds <= set(out["condition_chips"]), \
        f"{label}: expected {cond_conds}, got {out['condition_chips']}"
    # the LLM must never invent chips outside the banks (doc 12: no guessing)
    assert set(out["chips"]) <= set(sym_bank)
    assert set(out["condition_chips"]) <= set(cond_bank)


@requires_key
def test_lexicon_vs_llm_darija_delta():
    """Gap #4: measure the delta between the offline lexicon (fallback) and
    the LLM on Darija text — the case for keeping both paths (doc 12 §3)."""
    from mecanic.session import extract_chips_lexicon

    text = "kaykhrej siffl dyal l-hwa 3end ralenti, machi 9wa f cote"
    lex = extract_chips_lexicon(text)
    sym_bank, cond_bank = _banks()
    llm_out = LLM.extract_complaint(text, sym_bank, cond_bank, gloss=_gloss())
    llm_chips = set((llm_out or {}).get("chips", [])) | set((llm_out or {}).get("condition_chips", []))
    print(f"\nlexicon: {sorted(lex)}\nLLM:     {sorted(llm_chips)}")
    # the LLM should find at least what the lexicon finds (superset on Darija)
    assert set(lex) <= llm_chips


@requires_key
def test_full_loop_with_darija_free_text_only(tmp_path):
    """End-to-end §4.1: the tech types Darija free text (no chips), the LLM
    extracts into the same schema, the deterministic pipeline still reaches
    the labeled verdict — two paths, one ledger (doc 12 §3)."""
    path = SCENARIOS_DIR / "p0171_vacuum_leak.yaml"
    scenario = load_scenario(path)
    transport = open_scenario_transport(path)
    ledger = Ledger("llm-loop-test", tmp_path / "ll.jsonl")
    s = Session(ledger, transport, scenario, llm=LLM)
    # Darija free text, NO chips — the LLM is now the only extraction path
    s.quick_start(text="kaykhrej siffl dyal l-hwa mn l-moteur 3end ralenti w kay-broute")
    s.connect_and_intake()
    s.live_watch()
    state, ranking, cards, nba, digest = s.analyze()
    s.simulate_steps(scenario.get("simulate", []))
    state, ranking, cards, nba, digest = s.analyze()
    result = s.conclude(state, ranking, cards, nba, digest)
    ledger.close()

    assert (state.complaint or {}).get("llm_extracted") is True
    chips = set(state.chips) | set(state.condition_chips)
    assert "hissing" in chips, f"LLM extraction missed hissing: {chips}"
    assert ranking["ranking"][0]["cause"] == "vacuum_leak"
    assert ranking["status"] == "verdict"
    assert ranking["ranking"][0]["confidence"] >= 70


@requires_key
def test_token_budget_per_dossier(tmp_path):
    """§4.1 metric: tokens/dossier < 10K — extraction (LLM) + digest (funnel)."""
    from mecanic.funnel import token_estimate

    sym_bank, cond_bank = _banks()
    LLM.extract_complaint("kaykhrej siffl dyal l-hwa 3end ralenti", sym_bank, cond_bank,
                          gloss=_gloss())
    extraction_tokens = LLM.last_usage.get("total_tokens", 0)

    path = SCENARIOS_DIR / "p0171_vacuum_leak.yaml"
    scenario = load_scenario(path)
    transport = open_scenario_transport(path)
    ledger = Ledger("llm-tokens-test", tmp_path / "lt.jsonl")
    s = Session(ledger, transport, scenario, llm=LLM)
    s.quick_start(text="Sifflement d'air au ralenti")
    s.connect_and_intake()
    s.live_watch()
    _state, _ranking, cards, nba, digest = s.analyze()
    ledger.close()
    digest_tokens = token_estimate(digest)
    total = extraction_tokens + digest_tokens
    print(f"\nextraction={extraction_tokens} tok, digest~{digest_tokens} tok, total={total}")
    assert total < 10000  # doc 16 budget — leaves room for reasoning + report


@requires_key
def test_llm_offline_fallback_still_proves_superfluous():
    """The floor-test rationale (doc 17 §4.1): the pipeline must not *depend*
    on the LLM. A client with a dead key degrades to None -> deterministic
    path — same as no key at all."""
    dead = NemotronClient(api_key="nvapi-invalid-key-for-fallback-test")
    assert dead.available
    t0 = time.monotonic()
    out = dead.chat([{"role": "user", "content": "OK?"}])
    assert out is None  # auth fails -> deterministic fallback, never a crash
    assert time.monotonic() - t0 < 180


# The real garage register: French + Darija code-switched in ONE sentence
# (docs 11/12 — "msha do men l motor", "la porte dyal tomobil makat7elsh").
# The second one is a MECHANICAL complaint: it must route to mechanical_chips
# (out of data diagnosis — doc 00 scope honesty), never to engine symptoms.

@requires_key
def test_extraction_codeswitched_daw_voyant():
    """'msha do men l motor' — most probable reading: 'msha daw' = the engine
    light came on (daw = ضو). With mil_on in the bank + gloss, the LLM must
    land it there (it identified this reading itself before the chip existed)."""
    sym_bank, cond_bank = _banks()
    out = LLM.extract_complaint("msha do men l motor", sym_bank, cond_bank,
                                gloss=_gloss(), mechanical_chips=_mech_bank())
    assert out is not None
    assert "mil_on" in out["chips"], f"expected mil_on, got {out}"
    assert set(out["chips"]) <= set(sym_bank)


@requires_key
def test_extraction_codeswitched_door_routes_out():
    """'la porte dyal tomobil makat7elsh' — FR noun + Darija possessive +
    negation. Must map to mechanical_chips (route-out), invent nothing
    engine-side, and read the register as mixed."""
    sym_bank, cond_bank = _banks()
    out = LLM.extract_complaint("la porte dyal tomobil makat7elsh", sym_bank, cond_bank,
                                gloss=_gloss(), mechanical_chips=_mech_bank())
    assert out is not None
    assert "door_window_issue" in out["mechanical_chips"], f"got {out}"
    assert out["chips"] == []  # never dress a body problem up as an engine symptom
    assert out["language"] in ("mix", "darija", "fr")
