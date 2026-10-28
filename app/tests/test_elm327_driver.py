"""Kickoff step 2: the ELM327 driver — wire parsing against the behaviors
pinned from the REAL ELM327-emulator (ISO-TP PCI bytes, multi-frame VIN,
echo, headers, NO DATA) plus the v1 scenario contract."""

from mecanic.elm327 import (ELM327Driver, Transport, decode_dtc_word,
                            parse_dtcs, parse_frames)


class FakeTransport(Transport):
    """Replays scripted response lines per command (no hardware)."""

    def __init__(self, script: dict, chip: str = "ELM327 v1.5"):
        self.script = script
        self.chip = chip
        self.last = ""

    def write(self, cmd):
        self.last = cmd.strip().upper()

    def read_response(self):
        return self.script.get(self.last, ["NO DATA"])


def test_pid_scaling_roundtrip():
    # the classic worked example: 41 0C 09 B0 -> (0x09B0)/4 = 620 rpm
    d = ELM327Driver(FakeTransport({"010C": ["7E8 41 0C 09 B0"]}))
    d.connect()
    assert d.pid("RPM")["value"] == 620.0


def test_pid_with_isotp_pci_bytes():
    # real-emulator style: '04' is the ISO-TP single-frame length, not data
    d = ELM327Driver(FakeTransport({"010C": ["7E8 04 41 0C 09 B0"]}))
    d.connect()
    assert d.pid("RPM")["value"] == 620.0


def test_multi_ecu_headers_parse():
    frames = parse_frames(["7E8 43 01 01 71", "7EA 43 01 02 05"], expect=0x43)
    assert frames["7E8"][0] == 0x43
    assert parse_dtcs(frames["7E8"], 0x43) == ["P0171"]
    assert parse_dtcs(frames["7EA"], 0x43) == ["P0205"]


def test_dtc_word_roundtrip():
    for code in ("P0171", "P0302", "P0420", "C0563", "U0100", "B1310"):
        assert decode_dtc_word(_word(code)) == code


def _word(code):
    from mecanic.emulator import _dtc_word
    return _dtc_word(code)


def test_parse_dtcs_tolerant_to_count_byte():
    with_count = [0x43, 0x01, 0x01, 0x71]
    without = [0x43, 0x01, 0x71]
    assert parse_dtcs(with_count, 0x43) == ["P0171"]
    assert parse_dtcs(without, 0x43) == ["P0171"]


def test_vin_multiframe_isotp_reassembly():
    # captured from the real ELM327-emulator (first + consecutive frames)
    lines = ["7E8 10 14 49 02 01 57 50 30",
             "7E8 21 5A 5A 5A 39 39 5A 54",
             "7E8 22 53 33 39 30 30 30 30"]
    frames = parse_frames(lines, expect=0x49)
    d = ELM327Driver(FakeTransport({"0902": lines}))
    d.connect()
    vin = d.vin()
    assert vin and len(vin) == 17
    assert vin.startswith("WP0")


def test_echo_before_ate0_is_stripped():
    d = ELM327Driver(FakeTransport({"ATI": ["ATI", "ELM327 v1.5"]}))
    info = d.connect()
    assert info["chip_id"] == "ELM327 v1.5"


def test_unsupported_pid_returns_none_not_crash():
    # doc 08: graceful fallbacks on unsupported PIDs
    d = ELM327Driver(FakeTransport({"0107": ["NO DATA"]}))
    d.connect()
    assert d.pid("LTFT_B1") is None


def test_readiness_both_wire_shapes():
    # 5-byte data shape (A B C D E; D/E = supported bitmaps — real J1979 CAN):
    # A=0x81 (MIL on, 1 code); B=0x05 -> misfire+CCM complete; C=0x01 -> catalyst complete
    # D=0x07 -> misfire/fuel/CCM supported; E=0x01 -> catalyst supported
    d = ELM327Driver(FakeTransport({"0101": ["7E8 41 01 81 05 01 07 01"]}))
    d.connect()
    r = d.readiness()
    assert r["mil"] is True and r["dtc_count"] == 1
    assert r["monitors"]["misfire"] == "complete"
    assert r["monitors"]["fuel_system"] == "incomplete"
    assert r["monitors"]["catalyst"] == "complete"
    # nothing supported -> empty monitor map
    d2 = ELM327Driver(FakeTransport({"0101": ["7E8 41 01 80 00 00 00 00"]}))
    d2.connect()
    assert d2.readiness()["monitors"] == {}
