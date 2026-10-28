"""ELM327-class driver (doc 14 §3): AT init, tolerant response parsing,
PID scaling (SAE J1979 subset), mode 01/02/03/06/07/09/0A queries, and a
priority poll scheduler.

The driver is transport-agnostic — the same driver code path runs against:
  * ScenarioTransport   (in-process scripted vehicle — CI, doc 08)
  * TcpTransport        (the real `ELM327-emulator` package over TCP — its
                         default scenario was used to pin the wire behavior:
                         ISO-TP PCI bytes, multi-frame VIN, echo, ATI)
  * SerialTransport     (a physical BLE/USB adapter, via pyserial)

This is the *only* hardware-specific code; everything above it consumes
canonical ledger events (doc 14 §2).

Response tolerance (clone reality, doc 14 §3):
  * echoes before ATE0 takes effect,
  * multi-ECU headers (7E8/7EA...) when ATH1,
  * ISO-TP single/first/consecutive frames with PCI bytes,
  * headerless payloads (scenario transport / clones that ignore ATH1),
  * NO DATA / UNABLE TO CONNECT / CAN ERROR as "unsupported", not failure.
"""

from __future__ import annotations

import re
import socket
import time

# ---------------------------------------------------------------------------
# PID table (SAE J1979 mode $01 subset) — decode(A,B) -> engineering value,
# encode(value) -> (A,B) for the emulator side. Single-byte PIDs ignore B.
# ---------------------------------------------------------------------------

PID_TABLE: dict[str, tuple] = {
    # cmd     name          decode                              encode                                     unit
    "0103": ("FUEL_STATUS", lambda a, b: (a >> 4) & 0x3, lambda v: (int(v) << 4, 0), ""),
    "0104": ("LOAD", lambda a, b: a * 100 / 255, lambda v: (round(v * 255 / 100), 0), "%"),
    "0105": ("ECT", lambda a, b: a - 40, lambda v: (round(v + 40), 0), "°C"),
    "0106": ("STFT_B1", lambda a, b: (a - 128) * 100 / 128, lambda v: (round(v * 128 / 100 + 128), 0), "%"),
    "0107": ("LTFT_B1", lambda a, b: (a - 128) * 100 / 128, lambda v: (round(v * 128 / 100 + 128), 0), "%"),
    "0108": ("STFT_B2", lambda a, b: (a - 128) * 100 / 128, lambda v: (round(v * 128 / 100 + 128), 0), "%"),
    "0109": ("LTFT_B2", lambda a, b: (a - 128) * 100 / 128, lambda v: (round(v * 128 / 100 + 128), 0), "%"),
    "010A": ("EGR_ERR", lambda a, b: (a - 128) * 100 / 128, lambda v: (round(v * 128 / 100 + 128), 0), "%"),
    "010B": ("MAP", lambda a, b: a, lambda v: (round(v), 0), "kPa"),
    "010C": ("RPM", lambda a, b: (a * 256 + b) / 4, lambda v: (int(v * 4) >> 8, int(v * 4) & 0xFF), "rpm"),
    "010D": ("SPEED", lambda a, b: a, lambda v: (round(v), 0), "km/h"),
    "010F": ("IAT", lambda a, b: a - 40, lambda v: (round(v + 40), 0), "°C"),
    "0110": ("MAF", lambda a, b: (a * 256 + b) / 100, lambda v: (int(v * 100) >> 8, int(v * 100) & 0xFF), "g/s"),
    "0111": ("TPS", lambda a, b: a * 100 / 255, lambda v: (round(v * 255 / 100), 0), "%"),
    "0114": ("O2_B1S1", lambda a, b: a / 200, lambda v: (round(v * 200), 0), "V"),
    "0133": ("EQ_B1S1", lambda a, b: (a * 256 + b) / 32768, lambda v: (int(v * 32768) >> 8, int(v * 32768) & 0xFF), ""),
    "0142": ("BATV", lambda a, b: (a * 256 + b) / 1000, lambda v: (int(v * 1000) >> 8, int(v * 1000) & 0xFF), "V"),
    "0146": ("AMBIENT", lambda a, b: a - 40, lambda v: (round(v + 40), 0), "°C"),
    "015C": ("OIL_TEMP", lambda a, b: a - 40, lambda v: (round(v + 40), 0), "°C"),
}

PID_BY_NAME = {v[0]: cmd for cmd, v in PID_TABLE.items()}

# Doc 14 §5: polling priorities — the highest-value signals stay fresh even
# on slow clone chips (trims > rpm/maf > temps > the rest).
POLL_PRIORITY = [
    "STFT_B1", "LTFT_B1", "STFT_B2", "LTFT_B2",
    "RPM", "MAF", "ECT", "O2_B1S1", "BATV",
    "LOAD", "SPEED", "MAP", "TPS", "IAT", "OIL_TEMP", "EGR_ERR", "EQ_B1S1", "AMBIENT",
]

HEADER_RE = re.compile(r"^(7[0-9A-F]{2}|18[0-9A-F]{6})$", re.IGNORECASE)
LINE_PREFIX_RE = re.compile(r"^\d:")

# ---------------------------------------------------------------------------
# Transports
# ---------------------------------------------------------------------------

class TransportError(RuntimeError):
    pass


class Transport:
    """A byte-pipe to something that behaves like an ELM327 chip."""

    name = "base"

    def write(self, cmd: str) -> None:
        raise NotImplementedError

    def read_response(self) -> list[str]:
        """Return the response lines for the last command (prompt stripped)."""
        raise NotImplementedError

    def close(self) -> None:
        pass


class TcpTransport(Transport):
    """TCP transport — the real `ELM327-emulator` package (start it with
    `python -m elm -n 35000`) or a WiFi ELM327 adapter."""

    def __init__(self, host: str = "127.0.0.1", port: int = 35000, timeout: float = 4.0):
        self.name = f"tcp://{host}:{port}"
        self._sock = socket.create_connection((host, port), timeout=timeout)
        self._buf = b""
        self._sock.settimeout(timeout)

    def write(self, cmd: str) -> None:
        self._sock.sendall((cmd + "\r").encode("ascii", "ignore"))

    def read_response(self) -> list[str]:
        lines: list[str] = []
        deadline = time.monotonic() + 8.0
        while time.monotonic() < deadline:
            if b">" in self._buf:
                chunk, _, self._buf = self._buf.partition(b">")
                for raw in chunk.split(b"\r"):
                    line = raw.decode("ascii", "ignore").strip()
                    if line:
                        lines.append(line)
                return lines
            try:
                data = self._sock.recv(4096)
                if not data:
                    break
                self._buf += data
            except socket.timeout:
                break
        raise TransportError(f"no prompt from {self.name}")

    def close(self) -> None:
        try:
            self._sock.close()
        except OSError:
            pass


class SerialTransport(Transport):
    """A physical adapter over a serial/COM port (pyserial when present)."""

    def __init__(self, port: str, baud: int = 38400, timeout: float = 4.0):
        try:
            import serial  # type: ignore
        except ImportError as e:
            raise TransportError("pyserial not installed — pip install pyserial") from e
        self.name = f"serial:{port}"
        self._ser = serial.Serial(port, baud, timeout=timeout)

    def write(self, cmd: str) -> None:
        self._ser.write((cmd + "\r").encode("ascii", "ignore"))

    def read_response(self) -> list[str]:
        raw = self._ser.read_until(b">", size=8192)
        lines = []
        for chunk in raw.split(b"\r"):
            line = chunk.decode("ascii", "ignore").replace("\n", "").strip()
            if line and line != ">":
                lines.append(line)
        return lines

    def close(self) -> None:
        try:
            self._ser.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Response parsing (clone-tolerant — doc 14 §3 "response repair")
# ---------------------------------------------------------------------------

def _hex_tokens(line: str) -> list[str]:
    """Hex tokens from one response line, tolerating '0:'-style continuation
    prefixes. Headers (7E8, 18DAF1E1) stay whole tokens. Non-hex words
    (ELM327, SEARCHING...) are dropped by the caller via is_no_data."""
    line = LINE_PREFIX_RE.sub("", line.strip())
    return re.findall(r"[0-9A-Fa-f]+", line)


def _norm(line: str) -> str:
    return line.strip().upper().replace(" ", "")


def is_no_data(lines: list[str]) -> bool:
    joined = " ".join(_norm(l) for l in lines)
    if "NO DATA" in joined or "NODATA" in joined or "UNABLETOCONNECT" in joined:
        return True
    if "CAN ERROR" in joined or "BUS ERROR" in joined or "FBERROR" in joined:
        return True
    return any(_norm(l) in ("?",) for l in lines)


def parse_frames(lines: list[str], expect: int | None = None) -> dict[str, list[int]]:
    """Group response bytes by ECU header, reassembling ISO-TP frames.

    `expect` is the positive-response service byte (0x40 | mode, e.g. 0x41
    for mode $01). It disambiguates PCI presence: if the first byte after the
    header is *not* `expect` but the second one *is*, the transport carries
    ISO-TP PCI bytes (real CAN adapters) — strip/reassemble them. Otherwise
    treat the bytes as a plain payload (scenario transport, headerless
    clones).
    """
    per_header: dict[str, list[list[int]]] = {}
    for line in lines:
        toks = _hex_tokens(line)
        if not toks:
            continue
        if HEADER_RE.match(toks[0]):
            header, body = toks[0].upper(), toks[1:]
        else:
            header, body = "7E8", toks
        try:
            per_header.setdefault(header, []).append([int(t, 16) for t in body])
        except ValueError:
            continue
    return {h: _assemble(frame_list, expect) for h, frame_list in per_header.items()}


def _assemble(frame_list: list[list[int]], expect: int | None) -> list[int]:
    if frame_list and frame_list[0]:
        first = frame_list[0]
        kind = first[0] & 0xF0
        # Mode-response payloads always start with 0x4X (service byte), so a
        # 0x0L/0x1L/0x2L first byte unambiguously means ISO-TP PCI bytes are
        # present (single / first / consecutive frames — pinned against the
        # real ELM327-emulator: '7E8 10 14 49 02 ...', '7E8 21 ...').
        if kind in (0x00, 0x10, 0x20):
            data: list[int] = []
            for frame in frame_list:
                if not frame:
                    continue
                pci = frame[0]
                fkind = pci & 0xF0
                if fkind == 0x00:
                    data.extend(frame[1:1 + (pci & 0x0F)])       # single frame
                elif fkind == 0x10 and (pci & 0x0F) == 0:
                    data.extend(frame[2:])                       # first frame, 12-bit length byte follows
                elif fkind == 0x10:
                    data.extend(frame[1:])                       # first frame, 4-bit length
                elif fkind == 0x20:
                    data.extend(frame[1:])                       # consecutive frame
                else:
                    data.extend(frame)  # weird clone line — keep raw
            return data
    out: list[int] = []
    for frame in frame_list:
        out.extend(frame)
    return out


def decode_dtc_word(word: int) -> str:
    """Two DTC bytes -> 'P0171' (P/C/B/U + digit + 3 hex nibbles)."""
    letter = "PCBU"[(word >> 14) & 0x3]
    digit = (word >> 12) & 0x3
    return f"{letter}{digit}{word & 0xFFF:03X}"


def parse_dtcs(payload: list[int], expect_first_byte: int) -> list[str]:
    """Parse a mode 03/07/0A payload. Tolerant to both '4x [count] pairs'
    and bare '4x pairs' (clone variance)."""
    if not payload or payload[0] != expect_first_byte:
        return []
    body = payload[1:]
    if len(body) % 2 == 1 and body and body[0] == len(body) // 2:
        body = body[1:]  # count byte present
    codes = []
    for i in range(0, len(body) - 1, 2):
        code = decode_dtc_word(body[i] * 256 + body[i + 1])
        if code not in ("P0000", "U0000", "C0000", "B0000", "P000"):
            codes.append(code)
    return codes


# ---------------------------------------------------------------------------
# The driver
# ---------------------------------------------------------------------------

class ELM327Driver:
    """Speaks the ELM327 text protocol over any Transport, emits canonical
    data structures (the caller turns them into ledger events)."""

    def __init__(self, transport: Transport, retries: int = 2):
        self.transport = transport
        self.retries = retries
        self.chip_id: str = "unknown"
        self.headers_on = False
        self.connected = False
        self._seen_ecus: set[str] = set()
        self._last_cmd = ""

    # -- low level ----------------------------------------------------------

    def _request(self, cmd: str) -> list[str]:
        """Send a command, read the response; tolerate echo (before ATE0)
        and transport hiccups (retry)."""
        last_err: Exception | None = None
        for attempt in range(self.retries + 1):
            self._last_cmd = cmd
            self.transport.write(cmd)
            try:
                lines = self.transport.read_response()
            except TransportError as e:
                last_err = e
                continue
            # strip a full-line echo of the command (ATE0 not yet active)
            if lines and _norm(lines[0]) == _norm(cmd):
                lines = lines[1:]
            if not lines and attempt < self.retries:
                continue
            return lines
        if last_err is not None and not self.connected:
            raise last_err
        return []

    def _frames(self, cmd: str, expect: int) -> dict[str, list[int]]:
        lines = self._request(cmd)
        if not lines or is_no_data(lines):
            return {}
        return parse_frames(lines, expect)

    def _payload(self, cmd: str, expect: int) -> list[int]:
        frames = self._frames(cmd, expect)
        for header in sorted(frames):
            if frames[header]:
                return frames[header]
        return []

    # -- init (doc 14 §3) ----------------------------------------------------

    def connect(self) -> dict:
        """AT init sequence. Returns fingerprint info (clone detection stub —
        the certified-adapter program, doc 07).

        Uses ATI (not ATZ) for the chip fingerprint: the Ircama ELM327
        emulator — our TCP test vehicle — drops the connection on ATZ, and
        ATI returns the same identity string on real chips. Serial transports
        pass send_atz=True to hard-reset flaky clone firmware first.
        """
        banner = self._request("ATI")
        self.chip_id = " ".join(banner).strip() or "unknown-chip"
        self._request("ATE0")           # echo off
        hdr = self._request("ATH1")     # headers on (multi-ECU)
        self.headers_on = "OK" in " ".join(hdr).upper() or not hdr
        self._request("ATSP6")          # CAN 11-bit 500k
        self.connected = True
        family = "clone-elm327"
        up = self.chip_id.upper()
        if "STN" in up or "OBDLINK" in up:
            family = "stn-obdlink"
        elif "VLINKER" in up or "V-LINK" in up:
            family = "vlinker"
        return {"chip_id": self.chip_id, "chip_family": family,
                "certified_hint": family != "clone-elm327"}

    # -- mode $01 -----------------------------------------------------------

    def pid(self, name: str) -> dict | None:
        """Read one live PID by name -> {name, value, unit, cmd} or None when
        unsupported (NO DATA — a normal, non-fatal outcome; doc 08)."""
        cmd = PID_BY_NAME.get(name)
        if cmd is None:
            return None
        expect = 0x40 | int(cmd[:2], 16)
        payload = self._payload(cmd, expect)
        if len(payload) < 3 or payload[0] != expect:
            return None
        a = payload[2] if len(payload) > 2 else 0
        b = payload[3] if len(payload) > 3 else 0
        pname, decode, _enc, unit = PID_TABLE[cmd]
        return {"name": pname, "value": decode(a, b), "unit": unit, "cmd": cmd}

    def readiness(self) -> dict | None:
        """PID $01 -> MIL, DTC count, monitor readiness.

        Two wire shapes are accepted:
        * v1 scenario contract: '41 01 A B C S' (B/C = status bitmaps,
          S = supported bitmap; positions documented in emulator.py),
        * real J1979 CAN: 5 data bytes (A, B, C, D, E) with D/E = supported.
        The real-vehicle bit-position mapping per J1979 table is driver-layer
        work for the certified-adapter phase (gap register #2/#7 adjacent).
        """
        frames = self._frames("0101", 0x41)
        for header in frames:
            # an ECU answering mode $01 at all = a responding module
            # (the module health map + ignition guard, doc 12 §2) — this also
            # keeps the map honest on healthy cars with zero codes, where
            # modes 03/07/0A return nothing.
            self._seen_ecus.add(header)
        payload = []
        for header in sorted(frames):
            if frames[header]:
                payload = frames[header]
                break
        if len(payload) < 3 or payload[0] != 0x41:
            return None
        data = payload[2:]
        if len(data) < 3:
            return None
        a = data[0]
        mil = bool(a & 0x80)
        dtc_count = a & 0x7F
        b, c = data[1], data[2]
        if len(data) >= 5:
            # real J1979 CAN: D = spark-supported bitmap (high byte of s),
            # E = compression-supported bitmap (low byte)
            s = data[3] << 8 | data[4]
        elif len(data) >= 4:
            s = 0xFF00 | data[3]  # legacy v1 4-byte shape: assume spark supported
        else:
            s = 0xFFFF
        bits_b = {0: "misfire", 1: "fuel_system", 2: "cc_component"}
        bits_c = {0: "catalyst", 1: "heated_catalyst", 2: "evap", 3: "o2_sensor",
                  4: "o2_heater", 5: "egr", 6: "secondary_air", 7: "ac_system"}
        monitors: dict[str, str] = {}
        for bit, name in bits_b.items():
            if s >> (bit + 8) & 1:
                monitors[name] = "complete" if b >> bit & 1 else "incomplete"
        for bit, name in bits_c.items():
            if s >> bit & 1:
                monitors[name] = "complete" if c >> bit & 1 else "incomplete"
        return {"mil": mil, "dtc_count": dtc_count, "monitors": monitors}

    # -- codes (modes 03 / 07 / 0A) ------------------------------------------

    def dtcs(self) -> dict[str, list[str]]:
        out = {"stored": [], "pending": [], "permanent": []}
        for kind, cmd, expect in (("stored", "03", 0x43), ("pending", "07", 0x47),
                                  ("permanent", "0A", 0x4A)):
            for header, payload in self._frames(cmd, expect).items():
                self._seen_ecus.add(header)
                for code in parse_dtcs(payload, expect):
                    if code not in out[kind]:
                        out[kind].append(code)
        return out

    # -- freeze frame (mode 02; v1 contract: full frame in one response) -----

    def freeze_frame(self) -> dict | None:
        payload = self._payload("02", 0x42)
        if len(payload) < 5 or payload[0] != 0x42:
            return None
        dtc = decode_dtc_word(payload[1] * 256 + payload[2])
        values: dict[str, float] = {}
        i = 3
        while i + 3 <= len(payload):
            pid_cmd = f"01{payload[i]:02X}"
            if pid_cmd not in PID_TABLE:
                i += 3
                continue
            a, b = payload[i + 1], payload[i + 2]
            name, decode, _enc, _unit = PID_TABLE[pid_cmd]
            values[name] = round(float(decode(a, b)), 2)
            i += 3
        return {"dtc": dtc, "values": values}

    # -- VIN (mode 09 02) ----------------------------------------------------

    def vin(self) -> str | None:
        payload = self._payload("0902", 0x49)
        if len(payload) < 4 or payload[0] != 0x49 or payload[1] != 0x02:
            return None
        try:
            raw = bytes(payload[3:20])
            vin = raw.decode("ascii", "ignore").strip()
            if len(vin) >= 11:
                return vin
        except (ValueError, IndexError):
            return None
        return None

    # -- Mode $06 (v1 contract: 46 [count] (mid tid v2 min2 max2)*, values ×10)

    def mode06(self) -> list[dict]:
        payload = self._payload("06", 0x46)
        if len(payload) < 2 or payload[0] != 0x46:
            return []
        count = payload[1]
        body = payload[2:]
        results = []
        for i in range(min(count, len(body) // 8)):
            mid, tid = body[i * 8], body[i * 8 + 1]
            value = (body[i * 8 + 2] * 256 + body[i * 8 + 3]) / 10.0
            vmin = (body[i * 8 + 4] * 256 + body[i * 8 + 5]) / 10.0
            vmax = (body[i * 8 + 6] * 256 + body[i * 8 + 7]) / 10.0
            results.append({"mid": mid, "tid": tid, "value": value,
                            "min": vmin, "max": vmax})
        return results

    # -- live polling (doc 14 §5 priority scheduling) -------------------------

    def poll_cycle(self, names: list[str]) -> list[dict]:
        """One pass over the requested PID names in priority order.
        Unsupported PIDs (NO DATA) are skipped gracefully (doc 08 test plan)."""
        ordered = sorted(names, key=lambda n: POLL_PRIORITY.index(n) if n in POLL_PRIORITY else 99)
        samples = []
        for name in ordered:
            s = self.pid(name)
            if s is not None:
                samples.append(s)
        return samples

    @property
    def seen_ecus(self) -> list[str]:
        return sorted(self._seen_ecus)


def open_transport(spec: str) -> Transport:
    """'scenario:<path>' handled by emulator.py; 'tcp://host:port' and
    'serial:port' here."""
    if spec.startswith("tcp://"):
        rest = spec[6:]
        host, _, port = rest.partition(":")
        return TcpTransport(host or "127.0.0.1", int(port) if port else 35000)
    if spec.startswith("serial:"):
        return SerialTransport(spec[7:])
    raise ValueError(f"unknown transport spec: {spec!r} (use scenario:<path>, tcp://host:port, serial:port)")
