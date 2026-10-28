"""CSV importer producer (doc 14 §3): tolerant parser for Car Scanner /
Torque / forum exports and VED-style dataset files -> the same canonical
events. This unlocks "bring your own log": a garage WhatsApps a CSV, the
agent diagnoses from it (doc 14 §6.5).

Tolerances (the wild-CSV reality, doc 08 §1): comma/semicolon/tab
delimiters, heterogeneous column aliases (FR/EN), missing timestamps
(synthesized at 1 Hz), renamed/uppercased headers.
"""

from __future__ import annotations

import csv
from pathlib import Path

from .ledger import Ledger

# alias -> canonical pid name (VED, Car Scanner, Torque, KIA, FR names)
ALIASES: dict[str, str] = {
    "ENGINERPM": "RPM", "RPM": "RPM", "ENGINESPEED": "RPM", "REGIME": "RPM",
    "VEHICLESPEED": "SPEED", "SPEED": "SPEED", "VITESSE": "SPEED", "SPEEDKMH": "SPEED",
    "MAFGPS": "MAF", "MAF": "MAF", "MAFG/S": "MAF", "AIRFLOWRATE": "MAF", "DEBITMASSIQUE": "MAF",
    "ENGINECOOLANTTEMP": "ECT", "COOLANTTEMP": "ECT", "ECT": "ECT", "TEMPDEAU": "ECT",
    "INTAKEAIRTEMP": "IAT", "IAT": "IAT",
    "SHORTTERMFUELTRIMBANK1": "STFT_B1", "STFTB1": "STFT_B1", "STFT1": "STFT_B1",
    "LONGTERMFUELTRIMBANK1": "LTFT_B1", "LTFTB1": "LTFT_B1", "LTFT1": "LTFT_B1",
    "SHORTTERMFUELTRIMBANK2": "STFT_B2", "STFTB2": "STFT_B2", "STFT2": "STFT_B2",
    "LONGTERMFUELTRIMBANK2": "LTFT_B2", "LTFTB2": "LTFT_B2", "LTFT2": "LTFT_B2",
    "ENGINERUNTIME": "RUNTIME", "TIMESTAMP": "TS",
    "BATTERYVOLTAGE": "BATV", "BATV": "BATV", "CONTROLMODULEVOLTAGE": "BATV",
    "TENSION": "BATV", "ENGINELOAD": "LOAD", "LOAD": "LOAD", "CHARGE": "LOAD",
    "THROTTLEPOSITION": "TPS", "TPS": "TPS", "PAPILLON": "TPS",
    "O2SENSORVOLTAGEB1S1": "O2_B1S1", "O2B1S1": "O2_B1S1",
}


def _norm_header(h: str) -> str:
    up = "".join(ch for ch in h.upper() if ch.isalnum() or ch in " /")
    return up.replace(" ", "")


def import_csv(path: str | Path, ledger: Ledger, source: str = "csv_import",
               default_dt: float = 1.0) -> dict:
    """Stream a CSV into the ledger as pid_sample events. Returns a summary."""
    text = Path(path).read_text(encoding="utf-8", errors="replace")
    dialect = csv.Sniffer().sniff(text[:2048], delimiters=",;\t")
    rows = list(csv.DictReader(text.splitlines(), dialect=dialect))
    if not rows:
        return {"rows": 0}

    mapping: dict[str, str] = {}
    ts_col = None
    for raw, val in rows[0].items():
        if raw is None:
            continue
        norm = _norm_header(raw)
        canon = ALIASES.get(norm)
        if canon == "TS":
            ts_col = raw
        elif canon:
            mapping[raw] = canon

    n = 0
    t = 0.0
    for row in rows:
        if ts_col and row.get(ts_col):
            try:
                t = float(row[ts_col])
            except ValueError:
                t += default_dt
        else:
            t += default_dt
        for col, pid in mapping.items():
            raw_val = row.get(col)
            if raw_val in (None, "", "-", "nan"):
                continue
            try:
                value = float(str(raw_val).replace(",", "."))
            except ValueError:
                continue
            ledger.append("pid_sample", source,
                          {"pid": pid, "name": pid, "value": value, "unit": ""},
                          ts=t)
            n += 1
    return {"rows": len(rows), "samples": n, "pids": sorted(set(mapping.values()))}
