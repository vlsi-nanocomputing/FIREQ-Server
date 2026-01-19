# file: fireq-utils/test/test_tcp_server.py
"""TCP Server Integration & Robustness Test Suite (Pytest Version)."""

import json
import socket
import threading
import time
from unittest.mock import MagicMock

import numpy as np
import pytest

from server.message_handler import MessageHandler, SweepPointResult
from server.ol_adapter import OverlayAdapter
from server.tcp_server import FIREQServer

try:
    from test.mock_hardware import MockOverlay
except ImportError:
    from mock_hardware import MockOverlay

# =============================================================================
# HELPER CLASSES
# =============================================================================


class TCPClientHelper:
    """Simple TCP client wrapper for testing purposes.

    Renamed from TestClient to avoid Pytest collection warnings.
    """

    def __init__(self, host, port):
        self.host = host
        self.port = port
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.socket.settimeout(2.0)

    def connect(self):
        self.socket.connect((self.host, self.port))

    def close(self):
        try:
            self.socket.shutdown(socket.SHUT_RDWR)
            self.socket.close()
        except Exception:
            pass

    def send(self, data: dict):
        payload = json.dumps(data).encode("utf-8")
        length = len(payload).to_bytes(4, "big")
        self.socket.sendall(length + payload)

    def receive(self) -> dict:
        header = self._recv_exact(4)
        length = int.from_bytes(header, "big")
        payload = self._recv_exact(length)
        return json.loads(payload.decode("utf-8"))

    def _recv_exact(self, n):
        data = b""
        while len(data) < n:
            chunk = self.socket.recv(n - len(data))
            if not chunk:
                raise ConnectionError("Socket closed by peer")
            data += chunk
        return data

    def handshake(self, token="test_token"):
        _ = self.receive()  # Read Server Hello
        self.send({"type": "handshake_ack", "token": token, "client_name": "pytest_client"})
        return self.receive()  # Read OK response


# =============================================================================
# FIXTURES
# =============================================================================


class ServerContext:
    def __init__(self, server, adapter, port, auth_token):
        self.server = server
        self.adapter = adapter
        self.port = port
        self.auth_token = auth_token


@pytest.fixture(scope="module")
def server_ctx():
    port = 5556
    token = "test_token"

    # 1. Setup Stack
    mock_hw = MockOverlay()
    # FIX: Ensure num_generators is present for status/logout logic
    if "num_generators" not in mock_hw.hw_specs["summary"]:
        mock_hw.hw_specs["summary"]["num_generators"] = 1

    adapter = OverlayAdapter(mock_hw)
    handler = MessageHandler(adapter)

    # 2. Start Server
    server = FIREQServer(handler=handler, port=port, auth_token=token)
    server_thread = threading.Thread(target=server.start, daemon=True)
    server_thread.start()
    time.sleep(0.5)

    ctx = ServerContext(server, adapter, port, token)
    yield ctx

    server.stop()
    server_thread.join(timeout=1.0)


@pytest.fixture
def client(server_ctx):
    c = TCPClientHelper("127.0.0.1", server_ctx.port)
    yield c
    c.close()


# =============================================================================
# FUNCTIONAL TESTS
# =============================================================================


def test_handshake_success(client, server_ctx):
    client.connect()
    res = client.handshake(server_ctx.auth_token)
    assert res.get("type") == "handshake_ok"


def test_handshake_wrong_token(client):
    client.connect()
    client.receive()  # Hello
    client.send({"type": "handshake_ack", "token": "WRONG", "client_name": "bad"})
    res = client.receive()
    assert res.get("type") == "handshake_error"


def test_ping_pong(client, server_ctx):
    client.connect()
    client.handshake(server_ctx.auth_token)

    client.send({"cmd": "ping", "session_id": "123"})
    res = client.receive()
    assert res.get("cmd") == "pong"
    assert res.get("session_id") == "123"


def test_logout(client, server_ctx):
    client.connect()
    client.handshake(server_ctx.auth_token)

    # Pre-condition: Dirty the cache
    server_ctx.adapter.get_wave_cache(0)["dummy"] = "exists"

    client.send({"cmd": "logout"})
    res = client.receive()

    assert res.get("ok") is True
    assert len(server_ctx.adapter.get_wave_cache(0)) == 0


# =============================================================================
# ROBUSTNESS & EDGE CASES
# =============================================================================


def test_malformed_json_handling(server_ctx):
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.connect(("127.0.0.1", server_ctx.port))
    _ = sock.recv(1024)
    msg = json.dumps({"type": "handshake_ack", "token": server_ctx.auth_token}).encode()
    sock.sendall(len(msg).to_bytes(4, "big") + msg)
    _ = sock.recv(1024)

    bad_json = b'{"cmd": "ping", "session_id": '
    sock.sendall(len(bad_json).to_bytes(4, "big") + bad_json)

    time.sleep(0.1)
    sock.close()

    check_client = TCPClientHelper("127.0.0.1", server_ctx.port)
    try:
        check_client.connect()
        res = check_client.handshake(server_ctx.auth_token)
        assert res.get("type") == "handshake_ok"
    finally:
        check_client.close()


def test_large_payload_protection(server_ctx):
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.connect(("127.0.0.1", server_ctx.port))
    huge_size = 11 * 1024 * 1024
    header = huge_size.to_bytes(4, "big")
    sock.sendall(header)

    try:
        sock.settimeout(1.0)
        _ = sock.recv(1024)
    except (TimeoutError, ConnectionResetError, ConnectionAbortedError):
        pass  # Expected behavior
    finally:
        sock.close()

    check_client = TCPClientHelper("127.0.0.1", server_ctx.port)
    try:
        check_client.connect()
        res = check_client.handshake(server_ctx.auth_token)
        assert res.get("type") == "handshake_ok"
    finally:
        check_client.close()


def test_broken_pipe_during_sweep(client, server_ctx):
    """
    Scenario: Client disconnects abruptly while server is streaming sweep data.
    """
    # 1. Preload dummy data
    gen_idx = 0
    server_ctx.adapter.ol.generators[gen_idx].envelope_memory_dict["rect"] = {"type": "std"}
    server_ctx.adapter.compile_waves(
        gen_index=gen_idx, waves=[{"wave_id": "w1", "envelope": "rect", "duration": 100, "gain": 1.0}], replace=True
    )

    # 2. Mock acquisition to be slow
    original_acq = server_ctx.adapter.run_multi_acquisition

    def slow_mock_acquisition(*args, **kwargs):
        time.sleep(0.2)
        shots = kwargs.get("shots", 1)
        sps = kwargs.get("samp_per_shot", 100)
        adc = kwargs.get("adc_indices", [0])[0]
        return {adc: np.zeros((shots, sps), dtype=np.complex64)}

    server_ctx.adapter.run_multi_acquisition = slow_mock_acquisition

    try:
        client.connect()
        client.handshake(server_ctx.auth_token)

        sweep_cmd = {
            "cmd": "run_sweep",
            "sweep_id": "crash_test",
            "base": {
                "generators": [{"gen_index": 0, "drive": {"fifo": ["w1"], "frequency_mhz": "$freq"}}],
                "acquisitions": [{"acq_index": 0, "duration": 100}],
                "trigger": {"shots": 10},
            },
            "variables": [{"name": "freq", "values": [10.0, 20.0, 30.0]}],
        }

        client.send(sweep_cmd)

        msg = client.receive()
        assert msg.get("type") == "sweep_header"

        client.close()
        time.sleep(0.5)

        check_client = TCPClientHelper("127.0.0.1", server_ctx.port)
        try:
            check_client.connect()
            res = check_client.handshake(server_ctx.auth_token)
            assert res.get("type") == "handshake_ok"
        finally:
            check_client.close()

    finally:
        server_ctx.adapter.run_multi_acquisition = original_acq


def test_abort_command_execution(client, server_ctx):
    """Verifies that the 'abort' command effectively interrupts a long-running
    operation."""
    original_run_sweep = server_ctx.server.handler.run_sweep

    # Improved Mock: Checks stop_event immediately after waking up
    def blocking_sweep(msg, on_point, stop_event, on_plan=None):
        points = [{"dummy": v} for v in msg.get("variables", [{}])[0].get("values", [])]
        if on_plan is not None:
            on_plan(points)

        for i in range(50):
            if stop_event.is_set():
                return server_ctx.server.handler.status_h.get_sweep_status("aborted")

            time.sleep(0.1)

            # DOUBLE CHECK: Re-check stop event after sleep before sending data
            # This prevents the "last gasp" packet that was breaking the next test
            if stop_event.is_set():
                return server_ctx.server.handler.status_h.get_sweep_status("aborted")

            on_point(SweepPointResult(i, len(points), points[i % len(points)], {}))

        return server_ctx.server.handler.status_h.get_sweep_status("completed")

    server_ctx.server.handler.run_sweep = blocking_sweep

    try:
        client.connect()
        client.handshake(server_ctx.auth_token)

        client.send(
            {
                "cmd": "run_sweep",
                "sweep_id": "test_abort",
                "batch_size": 1,
                "base": {"generators": [], "acquisitions": [], "trigger": {"shots": 1}},
                "variables": [{"name": "dummy", "values": [1, 2]}],
            }
        )

        _ = client.receive()
        client.send({"cmd": "abort", "session_id": "999"})

        abort_ack_received = False
        start_time = time.time()

        while time.time() - start_time < 2.0:
            msg = client.receive()
            if msg.get("cmd") == "abort" and msg.get("ok") is True:
                abort_ack_received = True
                break

        assert abort_ack_received, "Did not receive abort acknowledgement"

    finally:
        server_ctx.server.handler.run_sweep = original_run_sweep


def test_internal_exception_handling(client, server_ctx):
    """Ensures that exceptions raised within the handler are caught and returned as JSON
    errors without killing the server.

    Uses a drain loop to ignore stale packets from previous tests.
    """
    # Clean queue just in case
    with server_ctx.server.queue_out.mutex:
        server_ctx.server.queue_out.queue.clear()

    original_compile = server_ctx.server.handler.wave_h.compile
    server_ctx.server.handler.wave_h.compile = MagicMock(side_effect=ValueError("Simulated Compile Error"))

    try:
        client.connect()
        client.handshake(server_ctx.auth_token)

        target_sid = "err_test_unique"
        client.send({"cmd": "compile_waves", "session_id": target_sid, "data": {}})

        # DRAIN LOOP: Keep receiving until we get the message for OUR session_id.
        # This ignores any "ghost" packets left over from the abort test.
        start_time = time.time()
        found_response = None

        while time.time() - start_time < 2.0:
            res = client.receive()
            if res.get("session_id") == target_sid:
                found_response = res
                break
            # If session_id doesn't match, loop again (drain the stale packet)

        if not found_response:
            pytest.fail("Timed out waiting for specific error response")

        # Assertions on the correct message
        assert found_response.get("ok") is False
        assert "Simulated Compile Error" in found_response.get("error", "")
        assert found_response.get("cmd") == "compile_waves"

        # Verify server is still alive
        client.send({"cmd": "ping"})
        pong = client.receive()
        assert pong.get("cmd") == "pong"

    finally:
        server_ctx.server.handler.wave_h.compile = original_compile
