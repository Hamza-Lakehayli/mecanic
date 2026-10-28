"""CAN producer (doc 17 §4.5): the decode path tested against a REAL opendbc
DBC (MIT, vendored in data/dbc/) — encode known signals with cantools, write a
frames CSV, decode through OUR producer, values must round-trip. Plus the bus
survey: counter / checksum / static / signal classification, and the
inferred-quality contract for CAN-decoded evidence (doc 10 §1.2)."""

import csv
from pathlib import Path

import pytest

from mecanic.canproducer import decode_frames, import_can, parse_can_csv, survey
from mecanic.ledger import Ledger

cantools = pytest.importorskip("cantools")  # optional dep — CI stays hermetic

DBC = Path(__file__).resolve().parent.parent / "data" / "dbc" / "toyota_prius_2010_pt.dbc"


def _write_frames_csv(path, frames):
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["timestamp", "id", "data"])
        for ts, fid, data in frames:
            w.writerow([f"{ts:.3f}", hex(fid), data.hex().upper()])


def _prius_frames(n=40):
    db = cantools.database.load_file(str(DBC))
    msg = db.get_message_by_name("GAS_PEDAL")
    base = dict(msg.decode(bytes(msg.length), decode_choices=False))
    # set one mid-range signal with a defined min/max — whatever the DBC says
    sig = next(s for s in msg.signals
               if s.minimum is not None and s.maximum is not None
               and s.maximum > s.minimum)
    target = (sig.minimum + sig.maximum) / 2
    base[sig.name] = target
    frame = msg.encode(base, strict=False)
    frames = [(float(t) / 10.0, msg.frame_id, frame) for t in range(n)]
    return frames, msg, sig.name, target


def test_parse_can_csv_roundtrip_shapes(tmp_path):
    frames, msg, _s, _t = _prius_frames()
    p = tmp_path / "prius.csv"
    _write_frames_csv(p, frames)
    parsed = parse_can_csv(p)
    assert len(parsed) == len(frames)
    assert parsed[0][1] == msg.frame_id
    assert parsed[0][2] == frames[0][2]


def test_opendbc_decode_roundtrip(tmp_path):
    # the whole point of the vendored DBC: OUR pipeline (csv -> frames ->
    # decode) must reproduce values that cantools itself computed
    frames, msg, sig_name, target = _prius_frames()
    p = tmp_path / "prius.csv"
    _write_frames_csv(p, frames)
    events = decode_frames(parse_can_csv(p), DBC)
    vals = [e["value"] for e in events if e["name"] == f"{msg.name}.{sig_name}"]
    assert vals, f"{msg.name}.{sig_name} not decoded"
    assert abs(vals[0] - target) < max(abs(target) * 0.02, 0.05)
    # every decoded event carries a physical value at a monotonic ts
    assert events[0]["ts"] <= events[-1]["ts"]


def test_import_can_quality_is_inferred(tmp_path):
    # doc 10 §1.2: CAN-decoded evidence is honest about being CAN-inferred
    frames, _msg, _s, _t = _prius_frames(10)
    p = tmp_path / "prius.csv"
    _write_frames_csv(p, frames)
    ledger = Ledger("can-test", tmp_path / "c.jsonl")
    summary = import_can(p, ledger, dbc_path=DBC)
    assert summary["frames"] == 10 and summary["decoded"] > 0
    q = next(e["quality"] for e in ledger.events if e["type"] == "pid_sample")
    assert q == {"source": "can", "quality": "inferred",
                 "dbc": "toyota_prius_2010_pt.dbc"}
    ledger.close()


def test_survey_classifies_counter_checksum_static_signal():
    # the decode-first step (doc 08 §2): counter/checksum bytes must never
    # masquerade as signals
    import random
    rng = random.Random(7)
    frames = []
    for i in range(60):
        payload = bytes([
            i % 256,                                # rolling counter
            rng.randrange(256),                     # checksum-like randomness
            0x55,                                   # static byte
            128 if (i // 10) % 2 else 100,          # slow-changing signal
        ])
        frames.append((i * 0.01, 0x245, payload))
    rows = survey(frames)
    assert rows[0]["id"] == "0x245" and rows[0]["frames"] == 60
    kinds = [b["kind"] for b in rows[0]["bytes"]]
    assert kinds[0] == "counter"
    assert kinds[1] == "checksum"
    assert kinds[2] == "static"
    assert kinds[3] == "signal"
    assert rows[0]["mean_period_ms"] == pytest.approx(10.0, abs=0.5)
