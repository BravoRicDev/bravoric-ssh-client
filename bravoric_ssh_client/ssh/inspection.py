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


def _run_py(
    host: Host, ssh_cfg: SshConfig | None, script: str, timeout: int = 30
) -> dict[str, Any]:
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
                # Memory-efficient: stream line-by-line instead of reading
                # entire file into RAM. Skip files >100MB to avoid OOM.
                try:
                    st = os.stat(full_path)
                    if st.st_size > 100 * 1024 * 1024:  # 100 MB threshold
                        continue
                    with open(full_path, 'r', encoding='utf-8', errors='ignore') as fp:
                        for line in fp:  # O(1) memory — streams line by line
                            if text in line:
                                results.append(rel_path)
                                count += 1
                                break  # stop reading after first match
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
        import subprocess
        lines = []
        total_lines = 0
        early_exit = False
        with open(filepath, 'r', encoding='utf-8', errors='replace') as fp:
            for idx, line in enumerate(fp, start=1):
                total_lines = idx
                if idx >= offset and len(lines) < limit:
                    lines.append(line)
                # Early exit: stop once we have enough lines past offset
                elif idx >= offset + limit:
                    early_exit = True
                    break
        # If we exited early, get exact total_lines via wc -l (fast, no Python mem)
        if early_exit:
            try:
                wc = subprocess.run(['wc', '-l', filepath], capture_output=True, text=True, timeout=5)
                if wc.returncode == 0:
                    total_lines = int(wc.stdout.split()[0])
            except Exception:
                pass  # keep estimated count
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


def remote_host_top_processes(
    host: Host,
    ssh_cfg: SshConfig | None,
    limit: int = 10,
    sort_by: str = "cpu",
    timeout: int = 30,
) -> dict[str, Any]:
    """Restituisce i processi più pesanti per CPU o RAM sull'host."""
    try:
        sort_by = sort_by.lower()
        if sort_by not in ("cpu", "mem", "rss"):
            sort_by = "cpu"
        limit_val = max(1, min(int(limit), 50))
    except (ValueError, TypeError):
        sort_by = "cpu"
        limit_val = 10

    script = f"""
import subprocess, json

sort_key = {json.dumps(sort_by)}
limit = {limit_val}

procs = []
try:
    p = subprocess.run(['ps', '-eo', 'pid,pcpu,rss,args'], capture_output=True, text=True, timeout=10)
    if p.returncode == 0:
        lines = p.stdout.strip().splitlines()
        for line in lines[1:]:
            parts = line.split(None, 3)
            if len(parts) >= 4:
                try:
                    pid = parts[0]
                    cpu = float(parts[1])
                    rss = int(parts[2])
                    comm = parts[3]
                    name = comm.split()[0].split('/')[-1] if comm else 'unknown'
                    procs.append({{"pid": pid, "name": name, "rss_kb": rss, "cpu_pct": cpu, "command": comm}})
                except ValueError:
                    continue
except Exception:
    pass

if sort_key == "cpu":
    procs.sort(key=lambda x: x.get("cpu_pct", 0.0), reverse=True)
else:
    procs.sort(key=lambda x: x.get("rss_kb", 0), reverse=True)

result = procs[:limit]
print(json.dumps({{"processes": result, "sort_by": sort_key, "limit": limit}}))
"""
    return _run_py(host, ssh_cfg, script, timeout=timeout)


def remote_write_file(
    host: Host,
    ssh_cfg: SshConfig | None,
    path: str,
    content: str,
    mode: str = "overwrite",
    timeout: int = 30,
) -> dict[str, Any]:
    """Scrive o appende contenuto a un file remoto.

    mode: ``overwrite`` (sovrascrive) | ``append`` (aggiunge in coda).
    Il contenuto viene passato via base64 per evitare problemi di quoting.
    """
    import base64

    b64_content = base64.b64encode(content.encode("utf-8")).decode("ascii")
    op = "append" if mode.lower() == "append" else "overwrite"

    script = f"""
import base64, os, json, sys
path = {json.dumps(path)}
op = {json.dumps(op)}
b64 = {json.dumps(b64_content)}
try:
    data = base64.b64decode(b64)
    if op == 'overwrite':
        with open(path, 'wb') as f:
            f.write(data)
    else:
        d = os.path.dirname(path)
        if d:
            os.makedirs(d, exist_ok=True)
        with open(path, 'ab') as f:
            f.write(data)
    size = os.path.getsize(path)
    print(json.dumps({{\"ok\": True, \"path\": path, \"bytes\": size, \"mode\": op}}))
except Exception as e:
    print(json.dumps({{\"ok\": False, \"error\": str(e)}}))
"""
    return _run_py(host, ssh_cfg, script, timeout=timeout)


def remote_edit_file(
    host: Host,
    ssh_cfg: SshConfig | None,
    path: str,
    pattern: str,
    replacement: str = "",
    count: int = 0,
    timeout: int = 30,
) -> dict[str, Any]:
    """Sostituisce pattern nel file remoto con sed/espressione regolare semplice.

    count=0 sostituisce tutte le occorrenze. count>0 limita il numero di sostituzioni.
    """
    import shlex

    try:
        count_val = int(count)
        if count_val < 0:
            count_val = 0
    except (ValueError, TypeError):
        count_val = 0

    safe_pattern = shlex.quote(pattern)
    safe_replacement = shlex.quote(replacement)
    safe_path = shlex.quote(path)

    if count_val > 0:
        sed_cmd = f"sed -i '0,/{safe_pattern}/s/{safe_pattern}/{safe_replacement}/' {safe_path}"
    else:
        sed_cmd = f"sed -i 's/{safe_pattern}/{safe_replacement}/g' {safe_path}"

    script = f"""
import subprocess, json, os
path = {json.dumps(path)}
cmd = {json.dumps(sed_cmd)}
try:
    result = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=30)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or 'sed failed')
    size = os.path.getsize(path) if os.path.exists(path) else 0
    print(json.dumps({{"ok": True, "path": path, "bytes": size}}))
except Exception as e:
    print(json.dumps({{"ok": False, "error": str(e)}}))
"""
    return _run_py(host, ssh_cfg, script, timeout=timeout)


def remote_session_audit_log(
    host: Host,
    ssh_cfg: SshConfig | None,
    session: str,
    max_lines: int = 0,
    timeout: int = 60,
) -> dict[str, Any]:
    """Legge il log di audit di una sessione tmux specifica."""
    import json

    try:
        safe_max = max(0, int(max_lines)) if max_lines else 0
    except (ValueError, TypeError):
        safe_max = 0
    # Build script using string concatenation to avoid f-string complexity
    script_parts = [
        "import subprocess, json, os\n",
        f"session = {json.dumps(session)}\n",
        f"max_lines = {safe_max}\n",
        "\n",
        "home = os.path.expanduser('~')\n",
        "log_base = os.path.join(home, '.bravoric-ssh-client', 'logs')\n",
        "\n",
        "if not os.path.isdir(log_base):\n",
        "    print(json.dumps({'ok': False, 'error': 'directory logs non trovata'}))\n",
        "    exit(0)\n",
        "\n",
        "files = []\n",
        "for f in os.listdir(log_base):\n",
        "    if session in f and f.endswith('.log.gz'):\n",
        "        files.append(os.path.join(log_base, f))\n",
        "\n",
        "if not files:\n",
        "    print(json.dumps({'ok': False, 'error': 'nessun log trovato', 'session': session}))\n",
        "    exit(0)\n",
        "\n",
        "log_file = sorted(files)[-1]\n",
        "\n",
        "try:\n",
        "    cmd = ['zcat', log_file]\n",
        "    if max_lines > 0:\n",
        "        cmd += ['-n', str(max_lines)]\n",
        f"    result = subprocess.run(cmd, capture_output=True, text=True, timeout={timeout})\n",
        "    if result.returncode == 0:\n",
        "        lines = result.stdout.strip().splitlines() if result.stdout else []\n",
        "        print(json.dumps({'ok': True, 'file': log_file, 'lines': len(lines), 'content': result.stdout[:50000]}))\n",
        "    else:\n",
        "        print(json.dumps({'ok': False, 'error': 'lettura fallita', 'stderr': result.stderr}))\n",
        "except Exception as e:\n",
        "    print(json.dumps({'ok': False, 'error': str(e)}))\n",
    ]
    script = "".join(script_parts)
    return _run_py(host, ssh_cfg, script, timeout=timeout)


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
