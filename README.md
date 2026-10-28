# Mecanic

Mecanic is an offline-first vehicle diagnostics project. The Phase 1 Python
implementation reads vehicle data, builds an evidence ledger, ranks likely
causes, and produces diagnostic reports. It can be exercised with the included
emulator and labeled scenarios without a vehicle or adapter.

The implementation, setup instructions, and architecture overview are in
[app/README.md](app/README.md).

## Quick start

```bash
cd app
python -m mecanic run --scenario scenarios/p0171_vacuum_leak.yaml
python -m pytest tests
```

Internal product research and design specifications are intentionally kept
local and are not part of this repository.
