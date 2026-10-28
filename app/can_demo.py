"""Dev demo (doc 17 §4.5): synthesize a Toyota Prius CAN "drive" from the
vendored opendbc DBC — pedal sweep + the DBC's own COUNTER/CHECKSUM signals —
then exercise the producer end to end:

    python can_demo.py
    python -m mecanic can var/can_demo.csv --dbc data/dbc/toyota_prius_2010_pt.dbc

The survey must classify the real layout: counter / checksum / signal bytes
exactly as a genuine Toyota bus would present them.
"""

import csv
import math
from pathlib import Path

import cantools

DBC = Path(__file__).resolve().parent / "data" / "dbc" / "toyota_prius_2010_pt.dbc"
OUT = Path(__file__).resolve().parent / "var" / "can_demo.csv"

db = cantools.database.load_file(str(DBC))
msg = db.get_message_by_name("GAS_PEDAL")
base = dict(msg.decode(bytes(msg.length), decode_choices=False))
pedal_sigs = [s for s in msg.signals if "PEDAL" in s.name
              and s.minimum is not None and s.maximum is not None
              and s.maximum > s.minimum]

OUT.parent.mkdir(exist_ok=True)
with open(OUT, "w", newline="", encoding="utf-8") as fh:
    w = csv.writer(fh)
    w.writerow(["timestamp", "id", "data"])
    for i in range(200):
        vals = dict(base)
        for s in pedal_sigs:
            vals[s.name] = s.minimum + (s.maximum - s.minimum) * \
                (0.5 + 0.45 * math.sin(i / 12.0))
        if "COUNTER" in vals:
            vals["COUNTER"] = i % 16
        if "CHECKSUM" in vals:
            vals["CHECKSUM"] = (i * 7 + 3) % 256
        frame = msg.encode(vals, strict=False)
        w.writerow([f"{i * 0.01:.3f}", hex(msg.frame_id), frame.hex().upper()])

print(f"200 trames écrites: {OUT} (0x{msg.frame_id:x} {msg.name}, "
      f"signaux: {[s.name for s in pedal_sigs]})")
