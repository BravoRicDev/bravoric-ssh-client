import json
import subprocess
from unittest.mock import patch

from bravoric_ssh_client.config import Config, Host
from bravoric_ssh_client.mcp_server import BravoricMcp
from bravoric_ssh_client.ssh.adapter import (
    ErrorCategory,
    SshError,
    _is_auth_locked,
    _record_auth_failure,
    _reset_auth_failures,
)
from bravoric_ssh_client.ssh.inspection import remote_read_file


def test_error_classification():
    # Test classificazione errori
    proc_auth = subprocess.CompletedProcess(
        args=["ssh"], returncode=1, stdout="", stderr="Permission denied (publickey)."
    )
    err_auth = SshError.from_subprocess(proc_auth, "test_auth")
    assert err_auth.category == ErrorCategory.PERMISSION_DENIED
    assert not err_auth.recoverable

    proc_timeout = subprocess.CompletedProcess(
        args=["ssh"],
        returncode=255,
        stdout="",
        stderr="ssh: connect to host 1.2.3.4 port 22: Connection timed out",
    )
    err_timeout = SshError.from_subprocess(proc_timeout, "test_timeout")
    assert err_timeout.category == ErrorCategory.TIMEOUT
    assert err_timeout.recoverable


def test_auth_rate_limiting():
    # Test rate limiting autenticazione
    host = "test_host_lock"
    _reset_auth_failures(host)

    locked, remaining = _is_auth_locked(host)
    assert not locked

    # Registra 5 fallimenti
    for _ in range(5):
        _record_auth_failure(host)

    locked, remaining = _is_auth_locked(host)
    assert locked
    assert remaining > 0

    _reset_auth_failures(host)
    locked, remaining = _is_auth_locked(host)
    assert not locked


def test_connect_timeout_config():
    # Test connect_timeout per-host e globale
    h = Host(alias="test", host="1.2.3.4", connect_timeout=12)
    assert h.connect_timeout == 12

    c = Config(connect_timeout=8)
    assert c.connect_timeout == 8


@patch("bravoric_ssh_client.ssh.inspection._run_py")
def test_file_size_limits(mock_run_py):
    # Test limiti dimensione file
    h = Host(alias="test", host="1.2.3.4")

    # File troppo grande per lettura
    res = remote_read_file(h, None, "/large_file.txt", limit=100000000)
    # Dovrebbe includere MAX_FILE_SIZE nello script Python inviato
    assert mock_run_py.called
    script = mock_run_py.call_args[0][2]
    assert "MAX_FILE_SIZE = 10485760" in script


def test_mcp_observability():
    # Test logging e correlation ID
    mcp = BravoricMcp()

    # Verifica metriche iniziali
    metrics_raw = mcp.get_metrics()
    metrics = json.loads(metrics_raw)
    assert metrics["tool_calls"] == 0
    assert metrics["errors"] == 0

    # Esegui con correlation context
    with mcp.correlation("test_tool", "test_host") as corr_id:
        assert corr_id is not None
        assert mcp._current_correlation_id == corr_id

    metrics_raw = mcp.get_metrics()
    metrics = json.loads(metrics_raw)
    assert metrics["tool_calls"] == 1
    assert metrics["errors"] == 0
