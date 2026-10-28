"""Doc 17 §4.1 model-comparison probe (dev tool, run manually):

    python llm_compare.py            # quality + warm latency + tokens/call

The benchmark --llm run can't differentiate models (its French complaints are
handled by even the offline lexicon) — THIS probe measures the real §4.1
deltas: Darija extraction quality (gap #4), the codeswitched register, the
mechanical route-out, warm latency, and tokens per extraction call.
"""

from __future__ import annotations

import time
from pathlib import Path

from mecanic.llm import NemotronClient
from mecanic.session import build_chip_gloss
from mecanic.yamlmini import load_file

DATA = Path(__file__).resolve().parent / "data"

# (label, complaint text, expected symptom chips, expected condition chips)
CASES = [
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
    ("codeswitch_daw",
     "msha do men l motor",
     {"mil_on"}, set()),
    ("door_routeout",
     "la porte dyal tomobil makat7elsh",
     {"__mechanical__"}, set()),
]

MODELS = [
    "nvidia/nemotron-3-ultra-550b-a55b",    # floor-test baseline (locked)
    "nvidia/nemotron-3.5-lightning-30b-a3b", # routine-tier candidate
    "nvidia/nemotron-3-super-120b-a12b",     # mid tier (flaky upstream Oct 22)
    "deepseek-ai/deepseek-v4.1-flash",       # frontier candidate (slow/timeout-prone)
]


def main() -> None:
    import sys
    priors = load_file(str(DATA / "priors.yaml"))
    sym = list((priors.get("symptoms") or {}).keys())
    cond = list((priors.get("conditions") or {}).keys())
    mech = list((priors.get("mechanical") or {}).keys())
    gloss = build_chip_gloss()

    models = sys.argv[1:] or MODELS
    for model in models:
        c = NemotronClient(model=model)
        if not c.available:
            print(f"\n=== {model} — no API key, skipped ===")
            continue
        print(f"\n=== {model} ===")
        passed = 0
        for label, text, exp_sym, exp_cond in CASES:
            out = c.extract_complaint(text, sym, cond, gloss=gloss,
                                      mechanical_chips=mech)
            if label == "door_routeout":
                ok = bool(out) and "door_window_issue" in out["mechanical_chips"] \
                     and out["chips"] == []
            else:
                ok = bool(out) and exp_sym <= set(out["chips"]) \
                     and exp_cond <= set(out["condition_chips"]) \
                     and set(out["chips"]) <= set(sym)
            passed += int(ok)
            print(f"  {label:16} " + ("PASS" if ok else "FAIL " + str(out)[:110]))
        # warm latency + tokens: 3 repeats of a Darija extraction
        lat, toks = [], 0
        for _ in range(3):
            t0 = time.monotonic()
            c.extract_complaint("kaykhrej siffl dyal l-hwa 3end ralenti w kay-broute",
                                sym, cond, gloss=gloss, mechanical_chips=mech)
            lat.append(time.monotonic() - t0)
            toks = c.last_usage.get("total_tokens", 0)
        print(f"  -> {passed}/{len(CASES)} quality | warm: "
              + ", ".join(f"{x:.2f}s" for x in lat)
              + f" | tokens/call: {toks}")


if __name__ == "__main__":
    main()
