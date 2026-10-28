"""CAN producer (doc 14 §3, doc 17 §4.5) — raw CAN frames become canonical
ledger events; the agent never sees raw bytes on the CAN side either.

    frames CSV (OTIDS / Car-Hacking / webCAN-style: timestamp, id, data)
      -> survey (ID census + per-byte flip rates: rolling-counter &
         checksum candidates — the CSS-skills step 1, doc 08 §2; no DBC
         needed) — counters/checksums must never masquerade as signals
      -> optional DBC decode (cantools; MIT opendbc files vendored under
         data/dbc/) -> pid_sample events with quality
         {"source": "can", "quality": "inferred", "dbc": <name>}
         — CAN-decoded evidence is INFERRED until verified against a
         mandated reference (doc 10 §1.2): passive decode is inside the
         trust chain, and the report says when evidence is CAN-inferred.

LISTEN-ONLY BY CONSTRUCTION: this module reads frames and emits events.
It has no encode/send/replay/write path for frames — the doc 00 charter
("not a tampering tool") is enforced by the absence of a transmit API.
"""

from __future__ import annotations

import csv
from pathlib import Path


# -- frames parsing (tolerant: the wild-CSV reality, doc 08 §2) ----------------

def _norm(h: str) -> str:
    return "".join(ch for ch in h.upper() if ch.isalnum())


def parse_can_csv(path: str | Path, default_dt: float = 0.01) -> list[tuple]:
    """-> [(ts, arb_id_int, data_bytes), ...]

    Accepted shapes: a time column (Timestamp/Time/TS — float seconds, else
    sequence at default_dt), an ID column (CAN ID / ARB ID; 0x-prefixed hex
    or decimal), and data either as one hex-string column ("8A 5D 00") or
    spread over Data0..Data7-style columns.
    """
    text = Path(path).read_text(encoding="utf-8", errors="replace")
    dialect = csv.Sniffer().sniff(text[:2048], delimiters=",;\t")
    rows = list(csv.DictReader(text.splitlines(), dialect=dialect))
    if not rows:
        return []
    raw_headers = [h for h in rows[0].keys() if h is not None]
    normed = {h: _norm(h) for h in raw_headers}

    def find(pred):
        for h in raw_headers:
            if pred(normed[h]):
                return h
        return None

    time_col = find(lambda n: "TIMESTAMP" in n or n == "TIME" or n == "TS")
    id_col = find(lambda n: n.endswith("ID") or n == "CANID" or "ARBID" in n)
    data_cols = [h for h in raw_headers
                 if "DATA" in normed[h] and normed[h] != "DLC"]
    if not id_col or not data_cols:
        raise ValueError(f"unrecognized CAN csv headers: {raw_headers[:12]}")

    single_data = data_cols if len(data_cols) == 1 else \
        sorted(data_cols, key=lambda h: (_norm(h).replace("DATA", "") or "0").isdigit() and
               int(_norm(h).replace("DATA", "") or 0) or -1)

    out: list[tuple] = []
    t = 0.0
    for row in rows:
        raw_id = (row.get(id_col) or "").strip()
        if not raw_id:
            continue
        try:
            fid = int(raw_id, 0) if raw_id.lower().startswith("0x") else int(raw_id)
        except ValueError:
            continue
        if len(single_data) == 1:
            hexstr = (row.get(single_data[0]) or "").replace(" ", "").strip()
            try:
                data = bytes.fromhex(hexstr)
            except ValueError:
                continue
        else:
            data = bytearray()
            for h in single_data:
                v = (row.get(h) or "").strip()
                if not v:
                    continue
                try:
                    data.append(int(v, 16) if v.lower().startswith("0x") else int(v))
                except ValueError:
                    continue
            data = bytes(data)
        if not data:
            continue
        ts_raw = (row.get(time_col) or "").strip() if time_col else ""
        try:
            t = float(ts_raw) if ts_raw else t + default_dt
        except ValueError:
            t = t + default_dt
        out.append((t, fid, data))
    out.sort(key=lambda f: f[0])
    return out


# -- the bus survey (decode step 1: counters/checksums are not signals) --------

def survey(frames: list[tuple], max_ids: int = 80) -> list[dict]:
    """ID census + per-byte classification: 'counter' (clock-like +1), "
    'checksum' (near-random every frame), 'static' (never flips), 'signal'.
    Sorted by frame count; the first step of any decode — flags the bytes
    that must be EXCLUDED from signal search (doc 08 §2)."""
    by_id: dict[int, list] = {}
    for ts, fid, data in frames:
        by_id.setdefault(fid, []).append((ts, data))
    out = []
    for fid, fl in sorted(by_id.items(), key=lambda kv: -len(kv[1]))[:max_ids]:
        fl.sort(key=lambda x: x[0])
        n = len(fl)
        periods = [fl[i + 1][0] - fl[i][0] for i in range(n - 1) if fl[i + 1][0] > fl[i][0]]
        nbytes = max(len(d) for _, d in fl)
        flips = [0] * nbytes
        counterish = [0] * nbytes
        last = None
        for _, d in fl:
            if last is not None:
                for b in range(min(len(d), len(last), nbytes)):
                    if d[b] != last[b]:
                        flips[b] += 1
                    if (d[b] - last[b]) % 256 == 1:
                        counterish[b] += 1
            last = d
        bytes_stats = []
        for b in range(nbytes):
            fr = flips[b] / max(n - 1, 1)
            cl = counterish[b] / max(n - 1, 1)
            kind = ("counter" if cl >= 0.6 else
                    "checksum" if fr >= 0.9 else
                    "static" if fr <= 0.02 else "signal")
            bytes_stats.append({"pos": b, "flip_rate": round(fr, 3), "kind": kind})
        out.append({
            "id": hex(fid), "frames": n,
            "mean_period_ms": round(sum(periods) / len(periods) * 1000, 2) if periods else None,
            "bytes": bytes_stats,
        })
    return out


# -- DBC decode (cantools — optional dependency, MIT opendbc vendored) --------

def decode_frames(frames: list[tuple], dbc_path: str | Path) -> list[dict]:
    """-> [{"ts", "name": "<MESSAGE>.<SIGNAL>", "value": physical}, ...]

    Undecodable ids/lengths skip silently (a log may carry frames from many
    buses/protocols; the DBC covers what it covers)."""
    import cantools

    db = cantools.database.load_file(str(dbc_path))
    events = []
    for ts, fid, data in frames:
        try:
            msg = db.get_message_by_frame_id(fid)
        except KeyError:
            continue
        try:
            decoded = msg.decode(data, decode_choices=False)
        except Exception:  # noqa: BLE001 — wrong length/mux: skip, not fail
            continue
        for name, value in decoded.items():
            events.append({"ts": ts, "name": f"{msg.name}.{name}",
                          "value": float(value)})
    return events


def import_can(path: str | Path, ledger, dbc_path: str | Path | None = None,
               source: str = "can_import") -> dict:
    """Frames CSV -> (optional DBC decode) -> pid_sample ledger events with
    quality: inferred (doc 10 §1.2 — CAN evidence is honest about being
    CAN-inferred)."""
    frames = parse_can_csv(path)
    summary = {"frames": len(frames), "ids": len({f[1] for f in frames}),
               "decoded": 0}
    if dbc_path and frames:
        for ev in decode_frames(frames, dbc_path):
            ledger.append("pid_sample", source,
                          {"pid": ev["name"], "name": ev["name"],
                           "value": round(ev["value"], 4), "unit": ""},
                          ts=ev["ts"],
                          quality={"source": "can", "quality": "inferred",
                                   "dbc": Path(dbc_path).name})
            summary["decoded"] += 1
    return summary
