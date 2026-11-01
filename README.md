# Mecanic

**An offline-first vehicle diagnostic agent that turns OBD-II/CAN data into evidence-cited hypotheses, guided tests, and workshop reports.**

Mecanic is a Python prototype for helping workshop technicians investigate vehicle faults. It ingests data from an ELM327 adapter, emulator, CSV log, or CAN frames decoded with DBC files. It then records evidence, applies deterministic diagnostic rules and playbooks, ranks possible causes, and recommends what to check next.

The goal is to reduce a workshop’s reliance on a separate diagnostic specialist—not to replace the technician or automate physical repairs. Phase 1 is read-only.

## Current status

The Phase 1 prototype runs locally, offline, and without a car or adapter using the included emulator and labeled scenarios.

- **13 diagnostic playbooks**
- **15 labeled benchmark scenarios**
- **104 automated tests passing**
- **15/15 benchmark scenarios passing**
- Funnel calibration tested against **14,904 trips from 368 vehicles** in the public Vehicle Energy Dataset (VED)

These results validate the software against scripted scenarios and public data. **Real-car validation is still a next step.**

## What it does

- Ingests vehicle data through an ELM327-compatible driver, emulator, CSV importer, or CAN/DBC decoder.
- Stores session evidence in an append-only JSONL ledger.
- Reads diagnostic trouble codes, freeze frames, readiness, Mode $06 results, VIN, and supported live PIDs.
- Derives operating windows from readings and applies deterministic rules, including fuel-trim, O2, MAF, warm-up, voltage, and freeze-frame checks.
- Ranks possible causes using diagnostic playbooks and evidence from the vehicle and technician.
- Recommends a next-best action and queues optional technician test cards.
- Produces evidence-cited diagnostic reports and a prototype pre-purchase **Rapport Confiance**.
- Records outcomes and per-vehicle history for future review.

The LLM is optional. It can extract structured chips from French or Darija text; deterministic rules perform the diagnostic ranking, and the app can fall back to its offline lexicon when the LLM is unavailable.

## Architecture

```
ELM327 / emulator / CSV / CAN+DBC
                 ↓
       Append-only evidence ledger
                 ↓
    Funnel → triage → hypothesis ranking
                 ↓
      Test cards → verdict → fix plan
                 ↓
          Reports → outcome record 
```
The agent consumes typed ledger events rather than raw adapter bytes. CAN decoding is listen-only; the project does not transmit or inject raw CAN frames.
Quick start
From the project directory:
python -m mecanic run --scenario scenarios/p0171_vacuum_leak.yaml
python -m mecanic benchmark
python -m pytest tests
Other useful commands:
# Render a buyer-facing Rapport Confiance from a stored session
python -m mecanic confiance var/sessions/<session-id>.jsonl

# Replay a session ledger
python -m mecanic replay var/sessions/<session-id>.jsonl

# Connect to an ELM327 emulator over TCP
python -m elm -n 35000
python -m mecanic connect --spec tcp://127.0.0.1:35000

# Soak the diagnostic funnel on VED data
python -m mecanic soak var/datasets/VED

# Survey and decode a CAN frames CSV with a DBC
python -m mecanic can <frames.csv> --dbc data/dbc/toyota_prius_2010_pt.dbc
The CAN command requires cantools. The VED soak requires downloading and extracting the VED dataset separately.
Tests and datasets
The labeled emulator scenarios are used for regression tests and the benchmark. They cover healthy data, ignition-off handling, tamper indicators, lean conditions, misfires, charging, thermostat behavior, EVAP, O2 sensors, DPF, fuel pressure, glow plugs, and network faults.
The VED dataset provides real-world driving trips used to examine flag rates and calibrate selected funnel rules. Public datasets are useful for testing ingestion and signal processing, but they are not a substitute for real-car validation or a fully labeled diagnostic dataset.
Milestones reached
- Implemented the evidence ledger, ELM327 driver, emulator, CSV importer, funnel, triage, ranker, test-card catalog, reports, and outcome recording.
- Added 13 YAML diagnostic playbooks and 15 labeled scenarios.
- Evaluated the funnel on 14,904 VED trips from 368 vehicles; used the results to revise the MAF rationality formula, idle-instability threshold, and hybrid idle-window handling.
- Added CAN survey and DBC decoding, with a round-trip test using a Toyota Prius DBC from commaai/opendbc (https://github.com/commaai/opendbc).
- Added the prototype buyer-facing Rapport Confiance report.
Next steps
1. Validate intake, live readings, and reports on real cars using a certified BLE adapter.
2. Improve vehicle-specific decoding and assess behavior across more makes and models.
3. Expand playbooks using real sessions and verified repair outcomes.
4. Build the mobile workshop interface and background, asynchronous workflow.
5. Evaluate sanctioned J2534/UDS test routines in a later phase.
Safety and scope
- Phase 1 is read-only.
- CAN decoding is listen-only; there is no raw-frame transmit or injection feature.
- Later diagnostic actuation is limited to OEM-exposed routines, with capability checks and consent gates.
- The app should report uncertainty rather than invent a diagnosis.
- Mechanical issues that cannot be inferred from vehicle data should be referred for physical inspection.
Project documentation
The product research, design decisions, and architecture notes are maintained separately from the application code. See the project documentation for the full design and roadmap.
