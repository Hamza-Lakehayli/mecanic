"""The emulator producer (doc 08): a scripted vehicle + adapter behind the
ELM327 wire protocol.

`ScenarioTransport` implements the *adapter+vehicle* side: it answers real
AT commands and real mode $01/$02/$03/$06/$07/$09/$0A requests with real
ELM327 hex responses (v1 wire contract below), so the genuine
`ELM327Driver` code path — AT init, header parsing, PID scaling, DTC
decoding, ISO-TP-less payloads — is what CI actually exercises. A scenario
is data (YAML), so every labeled case is also a replayable regression test.

v1 wire contract (documented; the real-adapter driver maps hardware truth
onto the same canonical events later):
  * mode 01 PID reply:  "<header> 41 <pid> A [B]"          (no ISO-TP PCI)
  * mode 03/07/0A:      "<header> 4X <count> <dtc pairs>"
  * mode 02 freeze:     "42 <dtc 2B> (pid 1B A 1B B 1B)*"
  * mode 09 02 VIN:     "49 02 01 <17 ASCII bytes>"
  * mode 06:            "46 <count> (mid tid v2 min2 max2)*"  values ×10
  * readiness PID $01:  "41 01 A B C S"
      A = MIL<<7 | dtc_count
      B bit0 misfire, bit1 fuel_system, bit2 cc_component
      C bit0 catalyst, bit1 heated_catalyst, bit2 evap, bit3 o2_sensor,
        bit4 o2_heater, bit5 egr, bit6 secondary_air, bit7 ac_system
      S = supported bitmap at the same positions; bit=1 in B/C means
      "complete" when supported.

The scenario `timeline:` field names operating windows (idle / rev_hold /
cruise / load / warm_up / snap) for authoring convenience only — the funnel
derives windows from the data itself (doc 10 §1.1), never from this label.
"""

from __future__ import annotations

import random
from pathlib import Path

from .elm327 import PID_BY_NAME, PID_TABLE, Transport
from .schema import CapabilityContract
from .yamlmini import load_file

SCENARIOS_DIR = Path(__file__).resolve().parent.parent / "scenarios"

READINESS_BITS_B = {"misfire": 0, "fuel_system": 1, "cc_component": 2}
READINESS_BITS_C = {"catalyst": 0, "heated_catalyst": 1, "evap": 2, "o2_sensor": 3,
                    "o2_heater": 4, "egr": 5, "secondary_air": 6, "ac_system": 7}


def load_scenario(path: str | Path) -> dict:
    sc = load_file(str(path))
    if not isinstance(sc, dict) or "timeline" not in sc:
        raise ValueError(f"scenario file missing 'timeline': {path}")
    return sc


def scenario_paths() -> list[Path]:
    return sorted(SCENARIOS_DIR.glob("*.yaml"))


def _dtc_word(code: str) -> int:
    """'P0171' -> 2-byte word (inverse of decode_dtc_word)."""
    letter = "PCBU".index(code[0])
    digit = int(code[1])
    low = int(code[2:], 16)
    return letter << 14 | digit << 12 | low


class ScenarioTransport(Transport):
    """A scripted ELM327 adapter+vehicle, driven by a scenario YAML."""

    def __init__(self, scenario: dict, seed: int = 42):
        self.scenario = scenario
        self.name = f"scenario:{scenario.get('name', 'unnamed')}"
        adapter = scenario.get("adapter", {})
        self.chip = adapter.get("chip", "ELM327 v1.5")
        self.max_poll_hz = float(adapter.get("max_poll_hz", 4))
        self._rng = random.Random(seed)
        self._t = 0.0
        self._last_cmd = ""
        self._echo = False  # echo until ATE0 (realistic clone behavior)
        # timeline phases
        self.phases: list[dict] = []
        t0 = 0.0
        for ph in scenario["timeline"]:
            duration = float(ph.get("duration_s", 30))
            self.phases.append({"start": t0, "end": t0 + duration, "raw": ph})
            t0 += duration
        self.total_duration = t0
        self.baseline: dict = scenario.get("vehicle", {}).get("baseline", {})

    # -- sim clock ------------------------------------------------------------

    def tick(self, dt: float):
        """Advance the simulated vehicle clock (called by the poll loop)."""
        self._t += dt

    def _phase_at(self, t: float) -> dict | None:
        for ph in self.phases:
            if ph["start"] <= t < ph["end"]:
                return ph["raw"]
        # past the end (or between polls): the car keeps running — clamp
        return self.phases[-1]["raw"] if self.phases else None

    def _value(self, pid_name: str, t: float):
        ph = self._phase_at(t)
        if ph is None:
            return self.baseline.get(pid_name)
        values = ph.get("values", {})
        spec = values.get(pid_name)
        base = None
        if isinstance(spec, dict):  # {from, to} linear ramp (e.g. warm-up curve)
            frac = (t - self._t_phase_start(t)) / max(ph.get("duration_s", 30), 1e-6)
            base = spec.get("from", 0) + (spec.get("to", 0) - spec.get("from", 0)) * frac
        elif spec is not None:
            base = spec
        elif pid_name in self.baseline:
            base = self.baseline[pid_name]
        else:
            return None
        sigma = (ph.get("noise") or {}).get(pid_name)
        if sigma:
            base = base + self._rng.gauss(0, sigma)
        return base

    def _t_phase_start(self, t: float) -> float:
        for ph in self.phases:
            if ph["start"] <= t < ph["end"]:
                return ph["start"]
        return 0.0

    # -- Transport interface ---------------------------------------------------

    def write(self, cmd: str) -> None:
        self._last_cmd = cmd

    def read_response(self) -> list[str]:
        cmd = self._last_cmd.strip().upper()
        out = self._respond(cmd)
        if self._echo:
            out = [cmd] + out
        return out

    def _respond(self, cmd: str) -> list[str]:
        v = self.scenario.get("vehicle", {})
        header = min(v.get("ecus", {"7E8": "ECM"}).keys()) if v.get("ecus") else "7E8"
        if cmd == "ATZ":
            return [self.chip]
        if cmd == "ATI":
            return [self.chip]
        if cmd in ("ATE0", "ATL0", "ATH1", "ATS0", "ATS1", "ATSP0", "ATSP6",
                   "ATH0", "ATAT1", "ATST64"):
            if cmd == "ATE0":
                self._echo = False
            return ["OK"]
        # ignition-off guard case (doc 12 §2): the adapter is powered by the
        # battery pin, so AT commands answer — but with the key off no ECU
        # responds. Every mode request is NO DATA.
        if v.get("silent"):
            return ["NO DATA"]
        # readiness PID $01 first (it is a mode-01 PID without a PID_TABLE entry)
        if cmd == "0101":
            a, b, c, d, e = self.readiness_bytes()
            return [f"{header} 41 01 {a:02X} {b:02X} {c:02X} {d:02X} {e:02X}"]
        # mode 01 PID
        if cmd.startswith("01") and len(cmd) >= 4:
            return self._respond_pid(cmd, header)
        if cmd == "03":
            return self._respond_dtcs(v.get("dtcs", {}).get("stored", []), 0x43, header)
        if cmd == "07":
            return self._respond_dtcs(v.get("dtcs", {}).get("pending", []), 0x47, header)
        if cmd == "0A":
            return self._respond_dtcs(v.get("dtcs", {}).get("permanent", []), 0x4A, header)
        if cmd == "02":
            return self._respond_freeze_frame(v.get("freeze_frame"), header)
        if cmd == "0902":
            return self._respond_vin(v.get("vin", "ZFA00000000000000"), header)
        if cmd == "06":
            return self._respond_mode06(v.get("mode06", []), header)
        return ["NO DATA"]

    # -- response builders -------------------------------------------------------

    def _respond_pid(self, cmd: str, header: str) -> list[str]:
        if cmd not in PID_TABLE:
            return ["NO DATA"]
        name, _decode, encode, _unit = PID_TABLE[cmd]
        val = self._value(name, self._t)
        if val is None:
            return ["NO DATA"]
        a, b = encode(float(val))
        return [f"{header} 41 {cmd[2:4]} {a:02X} {b:02X}"]

    def _respond_dtcs(self, codes: list[str], service: int, header: str) -> list[str]:
        if not codes:
            return ["NO DATA"]
        body = " ".join(f"{_dtc_word(c) >> 8:02X} {_dtc_word(c) & 0xFF:02X}" for c in codes)
        return [f"{header} {service:02X} {len(codes):02X} {body}"]

    def _respond_freeze_frame(self, ff: dict | None, header: str) -> list[str]:
        if not ff:
            return ["NO DATA"]
        word = _dtc_word(ff.get("dtc", "P0000"))
        parts = [f"{word >> 8:02X}", f"{word & 0xFF:02X}"]
        for name, val in ff.get("values", {}).items():
            cmd = PID_BY_NAME.get(name)
            if cmd is None:
                continue
            a, b = PID_TABLE[cmd][2](float(val))
            parts += [cmd[2:4], f"{a:02X}", f"{b:02X}"]
        return [f"{header} 42 " + " ".join(parts)]

    def _respond_vin(self, vin: str, header: str) -> list[str]:
        vin = (vin + "0" * 17)[:17]
        ascii_hex = " ".join(f"{ord(c):02X}" for c in vin)
        return [f"{header} 49 02 01 {ascii_hex}"]

    def _respond_mode06(self, results: list[dict], header: str) -> list[str]:
        if not results:
            return ["NO DATA"]
        parts = [f"{len(results):02X}"]
        for r in results:
            def x10(v):
                return max(0, min(0xFFFF, int(round(float(v) * 10))))
            vh, vl = divmod(x10(r["value"]), 256)
            mn, ml = divmod(x10(r["min"]), 256)
            mx, ml2 = divmod(x10(r["max"]), 256)
            parts += [f"{r['mid']:02X}", f"{r['tid']:02X}",
                      f"{vh:02X}", f"{vl:02X}", f"{mn:02X}", f"{ml:02X}", f"{mx:02X}", f"{ml2:02X}"]
        return [f"{header} 46 " + " ".join(parts)]

    # -- readiness (PID $01) ------------------------------------------------------

    def readiness_bytes(self) -> list[int]:
        """Build the readiness payload bytes A B C D E (real J1979 CAN shape:
        A = MIL<<7 | DTC count, B = spark-status bitmap, C = compression-status
        bitmap, D/E = supported bitmaps). Status bit = 1 means 'complete'
        when supported; monitor absent from D/E = unsupported."""
        r = self.scenario.get("vehicle", {}).get("readiness", {})
        a = (0x80 if r.get("mil") else 0) | (int(r.get("dtc_count", 0)) & 0x7F)
        b = c = d = e = 0
        for name, bit in READINESS_BITS_B.items():
            status = r.get("monitors", {}).get(name)
            if status:
                d |= 1 << bit
                if status == "complete":
                    b |= 1 << bit
        for name, bit in READINESS_BITS_C.items():
            status = r.get("monitors", {}).get(name)
            if status:
                e |= 1 << bit
                if status == "complete":
                    c |= 1 << bit
        return [a, b, c, d, e]


def open_scenario_transport(path: str | Path, seed: int = 42) -> ScenarioTransport:
    sc = load_scenario(path)
    return ScenarioTransport(sc, seed=seed)


def capability_from_scenario(scenario: dict) -> CapabilityContract:
    """Phase 1 = Tier A/B read-only contract (doc 14 §4): no actuation —
    the task engine's actuation cards are filtered out *by construction*."""
    return CapabilityContract(
        producer=f"emulator:{scenario.get('name', 'unnamed')}",
        protocols=["J1979-classic"],
        modes=["01", "02", "03", "06", "07", "09", "0A"],
        enhanced_pids=[],
        actuation=[],
        max_poll_hz=float(scenario.get("adapter", {}).get("max_poll_hz", 4)),
    )
