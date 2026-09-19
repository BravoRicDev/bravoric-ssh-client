from __future__ import annotations

from pathlib import Path

from bravoric_ssh_client.config import Config, Host
from bravoric_ssh_client.mcp_server import BravoricMcp
from bravoric_ssh_client.ssh.inspection import (
    remote_git_status,
    remote_host_health,
    remote_read_file,
    remote_search_files,
)


def test_remote_search_files_local(tmp_path: Path):
    host = Host(alias="loc", host="localhost", local=True)

    # Crea file di prova
    d = tmp_path / "subdir"
    d.mkdir()
    f1 = d / "test_alpha.txt"
    f1.write_text("hello world")
    f2 = tmp_path / "beta.md"
    f2.write_text("sample markdown")

    # 1. compact mode
    res = remote_search_files(host, None, path=str(tmp_path), pattern="*.txt", mode="compact")
    assert res["ok"] is True
    assert "data" in res
    assert res["data"]["count"] == 1
    assert "subdir/test_alpha.txt" in res["data"]["results"]

    # 2. grep mode
    res_grep = remote_search_files(
        host, None, path=str(tmp_path), pattern="*.*", mode="grep", text="markdown"
    )
    assert res_grep["ok"] is True
    assert res_grep["data"]["count"] == 1
    assert "beta.md" in res_grep["data"]["results"]

    # 3. metadata mode
    res_meta = remote_search_files(host, None, path=str(tmp_path), pattern="*.txt", mode="metadata")
    assert res_meta["ok"] is True
    assert res_meta["data"]["count"] == 1
    assert res_meta["data"]["results"][0]["size"] == len("hello world")


def test_remote_read_file_local(tmp_path: Path):
    host = Host(alias="loc", host="localhost", local=True)

    f = tmp_path / "lines.txt"
    f.write_text("line 1\nline 2\nline 3\nline 4\nline 5\n")

    # Lettura linee con offset e limit
    res = remote_read_file(host, None, path=str(f), offset=2, limit=2, unit="lines")
    assert res["ok"] is True
    data = res["data"]
    assert data["unit"] == "lines"
    assert data["offset"] == 2
    assert data["limit"] == 2
    assert data["returned_lines"] == 2
    assert data["content"] == "line 2\nline 3\n"

    # Lettura bytes
    res_b = remote_read_file(host, None, path=str(f), offset=1, limit=6, unit="bytes")
    assert res_b["ok"] is True
    assert res_b["data"]["unit"] == "bytes"
    assert res_b["data"]["content"] == "line 1"


def test_remote_git_status_and_health_local():
    host = Host(alias="loc", host="localhost", local=True)

    # host_health
    res = remote_host_health(host, None)
    assert res["ok"] is True
    data = res["data"]
    assert "disk" in data
    assert "/" in data["disk"]
    assert "ram" in data

    # git_status su repo corrente
    res_git = remote_git_status(host, None, path=str(Path(__file__).resolve().parent.parent))
    assert res_git["ok"] is True
    assert "branch" in res_git["data"]
    assert "clean" in res_git["data"]


def test_mcp_server_registers_new_tools(tmp_path: Path):
    cfg = Config(path=tmp_path / "config.toml")
    mcp = BravoricMcp(config=cfg)
    for name in ["search_files", "read_file", "git_status", "host_health"]:
        assert hasattr(mcp, name)
