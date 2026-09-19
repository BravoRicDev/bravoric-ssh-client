"""
Tool di ispezione remota e gestione file ad alta efficienza per bravoric-ssh-client.

Fornisce operazioni strutturate eseguite tramite python3 one-liner remoto (o locale)
per garantire robustezza, formati JSON deterministici e minimo consumo di token:
- search_files: ricerca file per nome/glob con 3 modalità (compact, metadata, grep).
- read_file: lettura file mirata con offset e limit (righe o bytes).
- git_status: stato sintetico di un repository git in JSON compatto.
- host_health: carico CPU, RAM libera, spazio disco e container Docker attivi.
"""

from __future__ import annotations

import json
import shlex
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ..config import Host
    from .adapter import SshConfig


def _run_py(host: Host, ssh_cfg: SshConfig | None, script: str, timeout: int = 30) -> dict[str, Any]:
    """Esegue uno script python3 su un host (locale o remoto via SSH) e ne restituisce il JSON."""
    from .broadcast import run_snippet_on_host

    cmd = f"python3 -c {shlex.quote(script)}"
    res = run_snippet_on_host(host, cmd, ssh_cfg, timeout=timeout)
    if res.error:
        return {"ok": False, "error": res.error}
    if not res.ok:
        err = (res.stderr or "").strip() or f"Exit code {res.exit_code}"
        return {"ok": False, "error": err, "stdout": res.stdout}
    out = (res.stdout or "").strip()
    try:
        data = json.loads(out)
        return {"ok": True, "data": data}
    except Exception as e:
        return {"ok": True, "raw": out, "parse_error": str(e)}


# ---------------------------------------------------------------------------
# 1. search_files
# ---------------------------------------------------------------------------

def remote_search_files(
    host: Host,
    ssh_cfg: SshConfig | None,
    path: str = ".",
    pattern: str = "*",
    mode: str = "compact",
    text: str = "",
    max_results: int = 50,
    include_hidden: bool = False,
    timeout: int = 30,
) -> dict[str, Any]:
    """Cerca file sul filesystem dell'host.

    mode:
      - 'compact': lista semplice di percorsi (1 per riga / array di stringhe).
      - 'metadata': array di oggetti {path, size, mtime, is_dir}.
      - 'grep': cerca file corrispondenti a pattern che contengono la stringa `text`.
    """
    try:
        max_res_val = int(max_results)
    except (ValueError, TypeError):
        max_res_val = 50

    script = f"""
import os, sys, fnmatch, json

root = os.path.abspath(os.path.expanduser({json.dumps(path)}))
pattern = {json.dumps(pattern)}
mode = {json.dumps(mode)}
text = {json.dumps(text)}
max_res = {max_res_val}
inc_hidden = {bool(include_hidden)}

EXCLUDE_DIRS = {{'.git', 'node_modules', '__pycache__', '.venv', 'venv', '.cache'}}

results = []
count = 0

if not os.path.exists(root):
    print(json.dumps({{"error": "Path non trovato: " + root}}))
    sys.exit(0)

for dirpath, dirnames, filenames in os.walk(root):
    # Prune excluded directories
    dirnames[:] = [d for d in dirnames if d not in EXCLUDE_DIRS and (inc_hidden or not d.startswith('.'))]

    for f in filenames:
        if not inc_hidden and f.startswith('.'):
            continue
        if fnmatch.fnmatch(f, pattern):
            full_path = os.path.join(dirpath, f)
            rel_path = os.path.relpath(full_path, root)

            if mode == 'grep':
                if not text:
                    continue
                try:
                    with open(full_path, 'r', encoding='utf-8', errors='ignore') as fp:
                        content = fp.read()
                        if text in content:
                            results.append(rel_path)
                            count += 1
                except Exception:
                    pass
            elif mode == 'metadata':
                try:
                    st = os.stat(full_path)
                    results.append({{
                        "path": rel_path,
                        "size": st.st_size,
                        "mtime": int(st.st_mtime),
                    }})
                    count += 1
                except Exception:
                    results.append({{"path": rel_path, "error": "stat_failed"}})
                    count += 1
            else: # compact
                results.append(rel_path)
                count += 1

            if count >= max_res:
                break
    if count >= max_res:
        break

print(json.dumps({{
    "root": root,
    "mode": mode,
    "count": len(results),
    "truncated": count >= max_res,
    "results": results
}}))
"""
    return _run_py(host, ssh_cfg, script, timeout=timeout)


# ---------------------------------------------------------------------------
# 2. read_file
# ---------------------------------------------------------------------------

def remote_read_file(
    host: Host,
    ssh_cfg: SshConfig | None,
    path: str,
    offset: int = 1,
    limit: int = 100,
    unit: str = "lines",
    timeout: int = 30,
) -> dict[str, Any]:
    """Legge una porzione mirata di file (lines o bytes) per preservare il contesto."""
    try:
        offset_val = int(offset)
    except (ValueError, TypeError):
        offset_val = 1

    try:
        limit_val = int(limit)
    except (ValueError, TypeError):
        limit_val = 100

    script = f"""
import os, sys, json

filepath = os.path.abspath(os.path.expanduser({json.dumps(path)}))
offset = {offset_val}
limit = {limit_val}
unit = {json.dumps(unit)}

if not os.path.isfile(filepath):
    print(json.dumps({{"error": "File non trovato o non regolare: " + filepath}}))
    sys.exit(0)

try:
    file_size = os.path.getsize(filepath)
    if unit == 'bytes':
        with open(filepath, 'rb') as fp:
            fp.seek(max(0, offset - 1))
            chunk = fp.read(limit)
            content = chunk.decode('utf-8', errors='replace')
        print(json.dumps({{
            "path": filepath,
            "unit": "bytes",
            "file_size": file_size,
            "offset": offset,
            "length": len(chunk),
            "content": content
        }}))
    else: # lines (1-indexed)
        lines = []
        total_lines = 0
        with open(filepath, 'r', encoding='utf-8', errors='replace') as fp:
            for idx, line in enumerate(fp, start=1):
                total_lines = idx
                if idx >= offset and len(lines) < limit:
                    lines.append(line)
        print(json.dumps({{
            "path": filepath,
            "unit": "lines",
            "file_size": file_size,
            "total_lines": total_lines,
            "offset": offset,
            "limit": limit,
            "returned_lines": len(lines),
            "content": "".join(lines)
        }}))
except Exception as e:
    print(json.dumps({{"error": str(e)}}))
"""
    return _run_py(host, ssh_cfg, script, timeout=timeout)


# ---------------------------------------------------------------------------
# 3. git_status
# ---------------------------------------------------------------------------

def remote_git_status(
    host: Host,
    ssh_cfg: SshConfig | None,
    path: str = ".",
    timeout: int = 30,
) -> dict[str, Any]:
    """Restituisce lo stato sintetico di un repository git in JSON compatto."""
    script = f"""
import os, subprocess, json, sys

repo = os.path.abspath(os.path.expanduser({json.dumps(path)}))

def sh(cmd):
    try:
        p = subprocess.run(cmd, cwd=repo, shell=True, capture_output=True, text=True, timeout=10)
        return p.stdout.strip()
    except Exception:
        return ""

if not os.path.exists(os.path.join(repo, ".git")) and not sh("git rev-parse --is-inside-work-tree"):
    print(json.dumps({{"error": "Non è un repository git: " + repo}}))
    sys.exit(0)

branch = sh("git rev-parse --abbrev-ref HEAD")
commit = sh("git log -1 --format='%h - %s (%cr)'")
raw_status = sh("git status --porcelain")

modified = 0
untracked = 0
staged = 0

files_sample = []

for line in raw_status.splitlines():
    if not line:
        continue
    x = line[0]
    y = line[1] if len(line) > 1 else ' '
    if x in ('M', 'A', 'D', 'R', 'C'):
        staged += 1
    if y in ('M', 'D'):
        modified += 1
    if x == '?' and y == '?':
        untracked += 1
    if len(files_sample) < 15:
        files_sample.append(line.strip())

clean = (staged == 0 and modified == 0 and untracked == 0)

print(json.dumps({{
    "repo": repo,
    "branch": branch,
    "latest_commit": commit,
    "clean": clean,
    "staged_count": staged,
    "modified_count": modified,
    "untracked_count": untracked,
    "total_changes": staged + modified + untracked,
    "sample_changes": files_sample
}}))
"""
    return _run_py(host, ssh_cfg, script, timeout=timeout)


# ---------------------------------------------------------------------------
# 4. host_health
# ---------------------------------------------------------------------------

def remote_host_health(
    host: Host,
    ssh_cfg: SshConfig | None,
    timeout: int = 30,
) -> dict[str, Any]:
    """Restituisce un quadro sintetico delle risorse dell'host (CPU, RAM, disco, Docker)."""
    script = """
import os, shutil, subprocess, json

# 1. Carico / CPU
load = [round(x, 2) for x in os.getloadavg()] if hasattr(os, 'getloadavg') else []

# 2. RAM (da /proc/meminfo se Linux)
ram = {}
if os.path.exists('/proc/meminfo'):
    try:
        mem = {}
        with open('/proc/meminfo') as f:
            for line in f:
                parts = line.split(':')
                if len(parts) == 2:
                    k = parts[0].strip()
                    v = parts[1].strip().split()[0]
                    mem[k] = int(v)
        total_kb = mem.get('MemTotal', 0)
        avail_kb = mem.get('MemAvailable', 0)
        ram = {
            "total_mb": round(total_kb / 1024),
            "avail_mb": round(avail_kb / 1024),
            "used_pct": round((total_kb - avail_kb) / total_kb * 100, 1) if total_kb else 0
        }
    except Exception:
        pass

# 3. Spazio Disco (root e home)
disks = {}
for p in ['/', os.path.expanduser('~')]:
    try:
        u = shutil.disk_usage(p)
        disks[p] = {
            "total_gb": round(u.total / (1024**3), 1),
            "free_gb": round(u.free / (1024**3), 1),
            "used_pct": round(u.used / u.total * 100, 1)
        }
    except Exception:
        pass

# 4. Docker (se installato ed eseguibile)
docker = {"present": False, "running_containers": []}
if shutil.which('docker'):
    docker["present"] = True
    try:
        p = subprocess.run(
            ['docker', 'ps', '--format', '{{.Names}}|{{.Status}}|{{.Image}}'],
            capture_output=True, text=True, timeout=5
        )
        if p.returncode == 0:
            containers = []
            for line in p.stdout.strip().splitlines():
                if not line:
                    continue
                pts = line.split('|')
                containers.append({
                    "name": pts[0],
                    "status": pts[1] if len(pts) > 1 else "",
                    "image": pts[2] if len(pts) > 2 else ""
                })
            docker["running_containers"] = containers
            docker["count"] = len(containers)
    except Exception as e:
        docker["error"] = str(e)

print(json.dumps({
    "load_avg": load,
    "ram": ram,
    "disk": disks,
    "docker": docker
}))
"""
    return _run_py(host, ssh_cfg, script, timeout=timeout)
