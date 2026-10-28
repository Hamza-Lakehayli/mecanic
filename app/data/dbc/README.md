# Vendored DBCs (doc 17 §4.5 — the CAN decode track)

From [commaai/opendbc](https://github.com/commaai/opendbc) — MIT license,
the community DBC library behind openpilot.

- `toyota_prius_2010_pt.dbc` — the round-trip CI target for the CAN producer
  (`tests/test_canproducer.py`): encode known signals with cantools, write a
  frames CSV, decode through OUR producer, values must survive the round trip.
- `mercedes_benz_e350_2010.dbc` — the validation oracle for the CSS Electronics
  data pack's Mercedes E350 CAN+OBD2 fixture: run our decode pipeline on their
  sample CSV, cross-check against this community DBC, then trust the same
  pipeline on the Dacia parc where no oracle exists (docs 08 §2, 17 §4.5).

The decode path is listen-only by construction (`mecanic/canproducer.py` has
no frame-transmit API — doc 00 charter).
