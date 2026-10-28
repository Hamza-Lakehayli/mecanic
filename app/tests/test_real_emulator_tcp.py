"""Integration (optional): the REAL Ircama ELM327-emulator over TCP — doc 08's
centerpiece. Proves the driver speaks genuine ELM327 wire protocol (ISO-TP
PCI bytes, multi-frame VIN, echo, ATI identity) against a third-party
implementation, through the same driver code path as the scenarios.

Skipped automatically when the package isn't installed or the server can't
start — the scripted scenarios remain the deterministic CI path.
"""

import socket
import subprocess
import sys
import time

import pytest

PORT = 35001


def _emulator_available():
    try:
        import elm  # noqa: F401  (the ELM327-emulator package installs `elm`)
        return True
    except ImportError:
        return False


@pytest.fixture(scope="module")
def emulator_server():
    if not _emulator_available():
        pytest.skip("ELM327-emulator package not installed (pip install ELM327-emulator)")
    proc = subprocess.Popen([sys.executable, "-m", "elm", "-n", str(PORT)],
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    try:
        # wait for the TCP listener (cold start can take a few seconds)
        for _ in range(30):
            time.sleep(1.0)
            try:
                probe = socket.create_connection(("127.0.0.1", PORT), timeout=1.0)
                probe.close()
                break
            except OSError:
                continue
        else:
            pytest.skip("ELM327-emulator did not open the TCP port in 30 s")
        yield proc
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:
            proc.kill()


def test_driver_against_real_emulator(emulator_server):
    from mecanic.elm327 import ELM327Driver, TcpTransport

    transport = TcpTransport("127.0.0.1", PORT)
    driver = ELM327Driver(transport)
    info = driver.connect()
    assert "ELM327" in info["chip_id"].upper() or info["chip_id"] != "unknown-chip"

    rpm = driver.pid("RPM")  # ISO-TP PCI + ISO-TP reassembly exercised here
    assert rpm is not None and rpm["value"] > 0

    vin = driver.vin()  # multi-frame 49 02 reassembly
    assert vin and len(vin) == 17

    # The default emulator scenario doesn't implement 0101/03 — the driver
    # must degrade gracefully (NO DATA -> None), never crash (doc 08).
    rdy = driver.readiness()
    assert rdy is None or isinstance(rdy["monitors"], dict)
    codes = driver.dtcs()
    assert set(codes) == {"stored", "pending", "permanent"}
    transport.close()
