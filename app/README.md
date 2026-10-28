# Mecanic — the app (Phase 1: read-only diagnostic agent)

Implementation of the **doc 17 §5 kickoff** in the dependency order locked in
doc 15's build sequence. Python, **zero hard dependencies**, offline-first,
CI-tested entirely on the emulator + labeled scenarios — no car, no adapter
needed (docs 08/10).

```
ledger + event schema → ELM327/emulator producer → auto-intake scan → triage rules
→ P0171 playbook + ranker → NBA/test-card UI → report generator → outcome form
→ pattern expansion (the flywheel)
```

## Quickstart

```bash
cd app
python -m mecanic run --scenario scenarios/p0171_vacuum_leak.yaml   # full 9-step loop
python -m mecanic benchmark                                          # doc 17 §4.1 CI matrix
python -m pytest tests                                               # 104 tests, no hardware
python -m mecanic soak var/datasets/VED       # §4.2: the funnel vs 14,904 real trips (or --diagnose 249)
python -m mecanic can <frames.csv> --dbc data/dbc/toyota_prius_2010_pt.dbc   # CAN survey + decode (listen-only)
python -m mecanic confiance var/sessions/<id>.jsonl                 # buyer report (Rapport Confiance)
python -m mecanic outcome --session <id> --part "durite PCV" --verified yes
python -m mecanic replay var/sessions/<id>.jsonl                     # free regression replay
```

**One-page visualization of the whole pipeline:** [docs/architecture.svg](docs/architecture.svg) (fr) ·
[docs/architecture-en.svg](docs/architecture-en.svg) (en) — sources → ledger → funnel, the 9-step
loop with both task lanes, the verdict thresholds, the outputs, and the flywheel.
**Layered architecture:** [docs/layers.svg](docs/layers.svg) (fr) — présentation / application / données,
with the external systems (the car, the NVIDIA API, public datasets) and the boundary guarantees.

Live hardware path (the same driver code, real `ELM327-emulator` over TCP —
wire behaviors pinned from it: ISO-TP PCI bytes, multi-frame VIN, echo):

```bash
pip install ELM327-emulator && python -m elm -n 35000   # a car on localhost
python -m mecanic connect --spec tcp://127.0.0.1:35000
# serial adapter later: python -m mecanic connect --spec serial:COM3
```

## Architecture (doc 14 — the agent never sees raw bytes)

```
ELM327 driver ──┐                                  ┌─ triage rules (stage 3)
Emulator        ─┼─► EVIDENCE LEDGER (JSONL) ──────┼─ ranker: rules+playbooks (4)
CSV importer    ─┤    append-only, typed events    ├─ task engine + NBA (5)
ELM327-emulator─┘    (the doc-12 ledger IS the     ├─ verdict composer (6)
(tcp / serial)       ingestion interface)          ├─ fix plan (7) · verification (8)
                                                   └─ record + outcomes (9)
        attention funnel (doc 16) between ledger and LLM:
        L0 delta-sampling → L1 windows+rules/SPC (zero-model Phase 1)
        → L2 [slot: iForest/autoencoder, Phase 2] → L3 digest (~1–1.5K tokens)
        → L4 Nemotron (optional, offline-queued) + drill-down tools
```

## Module map (kickoff step → code)

| Kickoff step (doc 17 §5) | Module | What it implements |
|---|---|---|
| 1. ledger + event schema | `mecanic/schema.py`, `mecanic/ledger.py` | Canonical typed events (doc 14 §2), JSONL append-only, fold/replay, capability contract, evidence classes `[OBD][INTAKE][PHYSICAL][MEASUREMENT][PHOTO]` |
| 2. ELM327/emulator producer | `mecanic/elm327.py`, `emulator.py`, `csvproducer.py` | Clone-tolerant driver (echo, ISO-TP reassembly, multi-ECU headers, NO DATA), AT init + chip fingerprint; scripted scenario vehicle speaking real ELM327 hex; tolerant CSV importer ("bring your own log") |
| 3. auto-intake scan | `mecanic/autointake.py` | Modes 01/02/03/06/07/09/0A → events; Mode $06 decode library (`data/mode06_decode.yaml`, gap #2 bootstrap) |
| 4. triage rules | `mecanic/triage.py`, `funnel.py` | Windows derived from data (idle/rev_hold/cruise/load/warm_up — never from labels); the doc 02 §2 discrimination table as L1 flags; tamper forensics (Rapport Confiance); paired statements ("votre sifflement colle avec les trims +22%") |
| 5. P0171 playbook + ranker | `patterns/*.yaml`, `mecanic/ranker.py` | YAML playbooks (lean/misfire/catalyst/thermostat/charging/EGR — AI-authored v1, doc 02 seed, gap #1); deterministic priors (chips/measurements/tests → exact multipliers, `data/priors.yaml`); confidence v0 (gap #6) + explainability trail; **Nemotron floor-test client** (`llm.py`, optional by construction — never reads the stream, offline → deterministic fallback) |
| 6. NBA/test-card UI | `mecanic/tasks.py`, `session.py`, `cli.py` | AUTO tasks + NEEDS-YOU cards (doc 11 catalog), capability filtering (Tier C cards hidden by construction), info-gain NBA (gap #10 heuristic), ≤5 visible cards, AUTO default / INTERACTIVE opt-in |
| 7. report generator | `mecanic/report.py` | French dossier, evidence-cited, partial reports at any moment ("what would sharpen this"), MAD parts estimates (gap #5 bootstrap) |
| 8. outcome form | `mecanic/record.py`, `cli.py` (`outcome`) | Outcome labels → `var/outcomes.jsonl`; per-VIN memory (hashed, CNDP posture); cross-visit monitor-drift trends — the flywheel data |
| 9. pattern expansion | `scenarios/*.yaml`, `mecanic/cli.py` (`benchmark`) | The §4.1 labeled test set as the CI gate; every replay is a regression test |

## CI = replay metrics (docs 14/16/17 §4.1)

`python -m mecanic benchmark` — the honest gate: verdict agreement vs
labeled cases (**15/15 = 100%**, target ≥80%), digest tokens (<10K/dossier;
measured ~0.9–1.5K), status/flags/triage expectations per scenario:

```
healthy_dci            None                   None                          0 escalate   PASS
ignition_off           None                   None                          0 escalate   PASS
misconfig_injector_dci post_repair_config_missing post_repair_config_missing 81 verdict PASS
p0087_fuel_filter      fuel_filter_clogged    fuel_filter_clogged          78 verdict PASS
p0128_thermostat_dci   thermostat_stuck_open  thermostat_stuck_open        95 verdict PASS
p0133_o2_lazy          o2_sensor_aging       o2_sensor_aging               87 verdict PASS
p0171_maf_dirty        maf_underreport        maf_underreport              89 verdict PASS
p0171_vacuum_leak      vacuum_leak            vacuum_leak                   91 verdict PASS
p0300_coil_cyl2        ignition_coil          ignition_coil                 71 verdict PASS
p0455_evap_cap         gas_cap_seal           gas_cap_seal                  74 verdict PASS
p0562_charging         alternator_fault       alternator_fault              87 verdict PASS
p0670_glow_plugs       glow_plug_worn         glow_plug_worn                87 verdict PASS
p2463_dpf_urban        dpf_soot_overload      dpf_soot_overload             92 verdict PASS
tamper_cleared_codes   None                   (no verdict)                  22 escalate PASS
u0100_bus_wire         bus_wire_intermittent  bus_wire_intermittent         77 verdict PASS
```

Same code, different data: the two P0171 scenarios rank **different** root
causes — that is the doc 02 flagship discriminator working, not a code lookup.
The tamper case correctly refuses to guess (escalate + NBA).

## Oct 2026 research-round additions (docs 02 §3, 12 §2, 15 stage 7)

1. **`patterns/misconfig_family.yaml`** — the used-parts pattern: history-gated
   activation (`triggers.recent_part`), a targeted `recent_work_boost` prior in
   the ranker, and the `verify_injector_coding` card. The scenario reaches its
   81% verdict **only after the card answers** ("not_coded"); before that it is
   a ranking + NBA — the agent suspects, then asks the 2-minute question, never
   guesses (doc 02 §7). Without declared recent work the family stays invisible
   (a rough idle with nothing recently swapped never points at misconfig).
2. **`module_scan` event** — the module health map as evidence (which ECUs
   spoke), rendered in the report; Tier C upgrades it to the full per-make
   topology, same event shape (doc 12 §2).
3. **Ignition guard** — `vehicle.silent: true` scenarios: the adapter answers
   AT commands (battery pin 16) but no ECU responds → the `ignition_off_suspect`
   finding says "check the key first", never "dead bus", and no verdict is
   produced. The cheapest dossier in the suite: 158 tokens of honest silence.
4. **`fix.post_repair_steps`** — fix plans carry post-repair configuration
   steps ("code the injector / reset the adaptation — a repair isn't complete
   until its configuration is"), rendered as their own report section
   (doc 02 §3, doc 15 stage 7).

## Sprint 3 (Oct 22, 2026) — demo polish + the revenue track

1. **Fuel-aware cards** — petrol-only tests (coil swap, spark-plug photos) never
   appear on a diesel, and vice-versa (glow plugs, DPF regen, leak-off). A dCi
   has no bobines — now the app knows it.
2. **Post-verdict relabel** — once the verdict lands, the queue becomes
   "Confirmation recommandée (optionnel)" instead of pretending there is still
   a next best action.
3. **Rapport Confiance** — `mecanic confiance <session.jsonl>` renders the
   buyer-facing pre-purchase report from the evidence ledger: red/amber/green/
   orange tiers (tamper > faults > drift > readiness), plain-language findings,
   CT readiness, the "diagnostic déjà réalisé" verdict section when present, and
   the honesty box ("ce que ce rapport NE peut pas vérifier") — the scope
   honesty that makes the other 80% of verdicts sellable.
4. **Six new diesel-first families** (13 playbooks total): EVAP (gas cap first
   after a fill-up), O2 sensor health, DPF (P2463: the highway-regen test that
   separates a driving pattern from a dead FAP), fuel pressure (filter first),
   glow plugs (the winter queue), U-codes (the module is almost always
   innocent). With them: freeze-frame funnel rules (urban-profile, cold-soak —
   the failure "black box" is data too), 4 fuel-gated cards, a glow-plug
   resistance widget, and 12 new prior entries.
5. **A latent ranker bug fixed** — measurement priors never applied (case
   mismatch on the `metric:VERDICT` keys); caught by the two measurement
   scenarios landing at 46-47 instead of 78-87. Thermostat rose 88→95 once its
   IR-gun evidence started counting.
6. **Field kit** — doc 18 in the study repo: what to buy, the real-car
   checklist, pass criteria, the capture protocol.

## Dataset layer (Oct 22, 2026 — docs 08 §1/§2, 17 §4.2)

The public-data soak before the adapter purchase. **VED Part 1 (22 weeks,
1.3 GB, 14,904 trips, 368 real vehicles)** runs through the funnel via
`mecanic soak`. What it caught before the adapter could: the MAF expectation
was WOT airflow (100% VE) — every healthy car would have flagged; now
LOAD-normalized, **zero MAF flags across 6,151 evaluable trips** (p50=1.02).
`idle_unstable` was emulator-calibrated (σ>60 fired on a third of the real
fleet; now 100 ≈ p93). Hybrid stop-start poisoned idle windows — windows now
require the engine running. Trim thresholds validated: healthy p95=11.5%
against our 10/15. And the flagged tail is real: **vehicle 249 carries a
chronic vacuum leak visible from week 4 to +72% trims** —
`mecanic soak --diagnose 249` runs the full stack on its trips (vacuum_leak
#1 throughout, honest escalation without complaint/code evidence).

The CAN decode path shipped with it: `mecanic/canproducer.py` (frames parser,
bus survey with counter/checksum classification, DBC decode via cantools)
with **real MIT opendbc DBCs vendored in `data/dbc/`** (Toyota Prius
round-trip CI green; Mercedes E350 reserved as the CSS-pack oracle).
Listen-only by construction — there is no frame-transmit API anywhere.

What remains gated: Edge Impulse (account), Kaggle (auth), the CSS data pack
(newsletter), commaCarSegments (size). The CAN security sets (OTIDS,
Car-Hacking) are usable today through the survey path. EV/battery sets wait
for the EV-track content phase.

## What's deliberately next (per the canon)

- **Real-car tests:** buy the certified BLE adapter (vLinker/OBDLink class),
  `mecanic connect --spec serial:COM3` — driver layer only, agent untouched.
- **Pattern expansion:** author more `patterns/*.yaml` + `scenarios/*.yaml`;
  every closed session with an outcome label sharpens the library (doc 00 §9).
- **Phase 2:** J2534/UDS actuation producer — the consent-gated Tier C cards
  (`egr_sweep_tierC`, `injector_balance_tierC`, `dpf_check_tierC`) and their
  capability filtering already exist as the architecture seam.

## LLM test phase — measured (doc 17 §4.1, Oct 21 2026)

The key lives in **`app/.env`** (gitignored, loaded by `mecanic/llm.py` — never
in code, never in the repo). Model: `nvidia/nemotron-3-ultra-550b-a55b`, the
locked floor-test baseline, confirmed present in the NVIDIA catalog.
`tests/test_llm_live.py` runs the matrix (skips without a key — CI stays
hermetic); `mecanic benchmark --llm` runs the labeled scenarios with the LLM
in the loop. Measured:

| §4.1 metric | Target | Measured |
|---|---|---|
| Verdict agreement vs labeled cases | ≥80% | **7/7 = 100%** offline AND `--llm` |
| Tokens/dossier | <10K | **~2.8K** (extraction 1348 + digest ~1437) |
| INTERACTIVE turn latency | <3 s | **~0.7–1 s warm** (cold start can exceed 30 s on the 550B — client retries, then degrades to the deterministic path) |
| French vs Darija delta | gap #4 | All four registers (FR, Darija-Latin, Darija-Arabic, mixed) extract correct chips; on Darija the LLM returns a **strict superset** of the lexicon — e.g. it catches "siffl dyal l-hwa" the lexicon misses |
| Hallucinated chips | none | Every extraction validated against the chip banks |
| Pipeline independence | never blocks | Full Darija free-text run → verdict ✓ with LLM; dead-key run → deterministic fallback ✓ (the floor-test thesis: the pipeline doesn't depend on the model) |

Gap-#4 finding written back into the product: bare chip ids in the prompt
weren't enough — the LLM missed "machi 9wa" → `power_loss` until the prompt
carried each chip's FR label + Darija keywords (`build_chip_gloss()`, shared
with the lexicon: two paths, one bank).

**Model comparison (Oct 22, 2026 — `python llm_compare.py`):** the benchmark
--llm can't differentiate models (its French complaints are covered by the
lexicon fallback — a dead model still "passes"). The probe measures what the
benchmark can't: Darija quality, warm latency, tokens/call. **Result: the
550B stays the single model** — 6/6 quality at ~7 s/call and ~1.8K tokens;
the smaller Lightning 30B-A3B is *slower* (25–45 s) and costlier (~3.6K
tokens) at 4/6 (Darija Latin/Arabic gaps); Super 120B upstream-502,
DeepSeek timeouts, Kimi/nano not enabled on this account. The routine-tier
routing split is deferred until the catalog stabilizes. Two fixes shipped:
the 1500-token cap that truncated reasoning models mid-thinking (empty
content — only the fallback had been running), and the honest extraction
latency figure (~7 s warm, not the <1 s roundtrip).

## The garage register — FR + Darija code-switched in ONE sentence

The real complaint is rarely "French OR Darija" — it's **"la porte dyal
tomobil makat7elsh"** (FR noun + Darija possessive + negation) or
**"msha do men l motor"** ("msha daw" = le voyant moteur s'est allumé — the
single most common garage complaint). Handled at three layers:

1. **`mil_on` chip** — the check-engine-light complaint now exists in the
   bank (both scripts), so "msha do/daw men l motor" maps correctly instead
   of falling to notes.
2. **Mechanical route-out bank** (`mechanical:` in `data/priors.yaml`) —
   porte/vitre/coffre, freins, embrayage, suspension, direction: body
   complaints map to `mechanical_chips`, *never* to engine symptoms. Triage
   emits a **refer-out finding** — doc 00's scope honesty, productized:
   *"non détectable par les données — orienter vers contrôle physique"*
   (routing ~20% of jobs away is what sells the other 80%, doc 10 §3C).
3. **Extraction prompt** — tells the model the register is mixed-in-one-
   sentence, gives the Darija glosses (`dyal` = possessive, `makat7elsh` =
   ne s'ouvre pas…), and forbids dressing a body problem up as engine data.

Measured live (both canonical sentences): the mixed complaint
"…makat7elsh + sifflement 3end ralenti…" extracts
`chips=[hissing]`, `conditions=[idle_only, morning]`,
`mechanical=[door_window_issue]`, `language=mix`, and the dossier routes
the door to physical inspection without inventing an engine diagnosis.

**Hermeticity contract:** offline tests and `mecanic benchmark` force
`NemotronClient(api_key="")` — truly offline (an early bug made `""` fall
through to the env key; fixed: only `None` consults the environment). Live
behavior lives in `tests/test_llm_live.py` + `mecanic benchmark --llm`,
both skipping cleanly without a key.

## Layout

```
mecanic/     the pipeline (schema → ledger → producers → funnel → triage →
             ranker → tasks → verdict → fix/verify → record → report → cli)
patterns/    YAML playbooks (the diagnostic brain's content — grows with outcomes)
scenarios/   labeled CI vehicles (data, not code — replayable regression tests)
data/        deterministic priors, FR/Darija lexicon, drive cycles, MAD parts,
             DTC titles, Mode $06 decode (the self-built assets)
tests/       104 tests incl. live integration vs the real ELM327-emulator over TCP
var/         runtime only (gitignored): session JSONL, reports, VIN profiles, outcomes
```
