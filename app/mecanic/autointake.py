"""Stage 2 — Auto-intake scan (doc 15): the first AUTO task. ~30 s of zero
button presses: every stored/pending/permanent code, freeze frames, Mode $06
with factory limits (per-make decoding — Dacia/dCi bootstrap, gap #2),
readiness, VIN, live PID snapshot.

Runs through the real ELM327 driver, appends canonical events to the ledger.
"""

from __future__ import annotations

from pathlib import Path

from .elm327 import ELM327Driver
from .ledger import Ledger
from .yamlmini import load_file

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
MODE06_DECODE_PATH = DATA_DIR / "mode06_decode.yaml"

NEAR_LIMIT_FRACTION = 0.10  # within 10% of the binding limit = "drifting"


def load_mode06_decode() -> dict:
    if MODE06_DECODE_PATH.exists():
        return load_file(str(MODE06_DECODE_PATH)) or {}
    return {}


def decode_mode06(raw: list[dict], decode_table: dict | None = None) -> list[dict]:
    """Turn raw MID/TID/value/limits into named, verdicted results.

    The decode library is the self-built asset (doc 02 §5): entries keyed
    '<mid>_<tid>' hold the test name and the binding limit direction.
    Unknown IDs still pass through (named generically) — coverage grows
    with sessions, Dacia/Renault dCi first (doc 15 stage 2 open item).
    """
    table = decode_table if decode_table is not None else load_mode06_decode()
    out = []
    for r in raw:
        entry = table.get(f"{r['mid']}_{r['tid']}", {})
        name = entry.get("name_fr", f"MID {r['mid']} / TID {r['tid']}")
        unit = entry.get("unit", "")
        fail_side = entry.get("fail_side", "high")
        value, vmin, vmax = r["value"], r["min"], r["max"]
        if fail_side == "low":
            binding = vmin
            failed = value < vmin
            near = value <= vmin + (vmax - vmin) * NEAR_LIMIT_FRACTION
        else:
            binding = vmax
            failed = value > vmax
            near = value >= vmax - (vmax - vmin) * NEAR_LIMIT_FRACTION
        verdict = "fail" if failed else ("near_limit" if near else "pass")
        out.append({"mid": r["mid"], "tid": r["tid"], "name": name, "unit": unit,
                    "value": value, "min": vmin, "max": vmax,
                    "fail_side": fail_side, "verdict": verdict})
    return out


class AutoIntake:
    """Scan orchestration: driver -> canonical ledger events."""

    def __init__(self, ledger: Ledger, driver: ELM327Driver,
                 capabilities: dict | None = None, vehicle_decode: dict | None = None,
                 poll_names: list[str] | None = None):
        self.ledger = ledger
        self.driver = driver
        self.capabilities = capabilities or {}
        self.vehicle_decode = vehicle_decode or {}
        self.poll_names = poll_names or ["RPM", "MAF", "ECT", "STFT_B1", "LTFT_B1",
                                         "STFT_B2", "LTFT_B2", "O2_B1S1", "LOAD",
                                         "SPEED", "BATV", "MAP", "IAT", "TPS"]

    def run(self) -> None:
        src = "autointake"
        q = {"adapter": self.driver.chip_id}  # quality metadata (doc 14 §5)

        self.ledger.append("producer_connected", src,
                           {"producer": self.capabilities.get("producer", "unknown"),
                            **{k: v for k, v in self.capabilities.items() if k != "producer"}},
                           quality=q)

        vin = self.driver.vin()
        if vin:
            self.ledger.append("vin", src, {"vin": vin, "decode": self.vehicle_decode}, quality=q)

        rdy = self.driver.readiness()
        if rdy:
            self.ledger.append("readiness", src, rdy, quality=q)

        codes = self.driver.dtcs()
        for kind, code_list in codes.items():
            for code in code_list:
                self.ledger.append("dtc", src, {"code": code, "kind": kind}, quality=q)

        ff = self.driver.freeze_frame()
        if ff:
            self.ledger.append("freeze_frame", src, ff, quality=q)

        for m in decode_mode06(self.driver.mode06()):
            self.ledger.append("mode06_result", src, m, quality=q)

        for addr in self.driver.seen_ecus:
            self.ledger.append("ecu_present", src, {"addr": addr}, quality=q)

        # module health map (doc 12 §2): the responding-module topology — the
        # pro-tablet topology view, but as EVIDENCE (missing/faulted modules
        # feed the U-code analysis, doc 02). Phase 1 answers it honestly at
        # generic OBD-II depth ("which ECUs spoke to us"); Tier C upgrades to
        # the full per-make map — same event, richer content.
        responding = list(self.driver.seen_ecus)
        n_codes = sum(len(v) for v in codes.values())
        modules = [{"addr": a, "status": "codes" if n_codes else "ok"} for a in responding]
        self.ledger.append("module_scan", src,
                           {"modules": modules, "responding": len(modules)}, quality=q)

        # ignition guard (doc 12 §2): a key-off car must never be misread as a
        # dead bus. The adapter is powered (battery pin 16) — total silence
        # means "check the key first". The #1 quick-start failure mode.
        if not responding:
            self.ledger.append("flag", src,
                               {"layer": "L0", "rule": "no_ecu_response",
                                "severity": "warn",
                                "message": "Aucun calculateur ne répond — adaptateur "
                                           "alimenté mais silence total. Cause n°1: "
                                           "contact coupé. Allumer le contact (ignition "
                                           "ON) et relancer le scan.",
                                "values": {}}, quality=q)

        # live PID snapshot (the pre-poll baseline)
        for s in self.driver.poll_cycle(self.poll_names):
            self.ledger.append("pid_sample", src,
                               {"pid": s["name"], "name": s["name"],
                                "value": round(float(s["value"]), 3), "unit": s["unit"]},
                               quality=q)
