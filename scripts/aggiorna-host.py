"""Aggiorna bravoric-ssh-client su Legionario, Legionario-VPN e cubotto-vpn.

- Legionario / Legionario-VPN: NON sono repo git -> copio il pacchetto (swap atomico,
  con backup temporaneo) e purgo __pycache__.
- cubotto-vpn: e' un repo git su main -> git pull (dopo che main e' stato pushato).

Verifica finale su ogni host: i marcatori dei fix devono essere presenti nel codice
che l'interprete importa davvero.
"""

import subprocess

REPO = "/home/riccardo/Progetti/bravoric-ssh-client"
PKG = "bravoric_ssh_client"

# (alias, host ssh, modo)
TARGETS = [
    ("Legionario", "riccardo@192.168.1.81", "copy"),
    ("Legionario - VPN", "riccardo@10.9.0.9", "copy"),
    ("cubotto-vpn", "serverino@10.9.0.2", "git"),
]

SSH = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10"]
VERIFY = "import bravoric_ssh_client as m, pathlib; print(pathlib.Path(m.__file__).parent)"


def run(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, check=False, **kw)


def verify(host: str, name: str) -> None:
    """Controlla che i marcatori dei fix siano nel codice importato."""
    d = run(SSH + [host, f'python3 -c "{VERIFY}"']).stdout.strip()
    if not d:
        print(f"    {name}: import non riuscito")
        return
    checks = {
        "guard TERM": ("grep -c _TERM_GUARD", "ssh/tmux_runner.py"),
        "kitty --hold": ("grep -c '\\-\\-hold'", "app.py"),
        "attach-or-create": ("grep -c 'new -A -s'", "ssh/tmux_runner.py"),
        "audit limitato": ("grep -c 'MAX_LOG_BYTES'", "ssh/audit.py"),
        "writer exec gzip": ("grep -c 'exec gzip'", "ssh/audit.py"),
        "prune remoto": ("grep -c 'PRUNE_REMOTE'", "ssh/audit.py"),
    }
    print(f"    dir import: {d}")
    for label, (g, rel) in checks.items():
        res = run(SSH + [host, f"{g} {d}/{rel}"]).stdout.strip()
        n = res.splitlines()[0] if res else "0"
        try:
            nval = int(n)
        except ValueError:
            nval = 0
        ok = "OK" if nval > 0 else "ASSENTE"
        print(f"    {label:18s} {ok} ({n})")


for name, host, mode in TARGETS:
    print(f"===== {name} ({host}) — modo {mode} =====")
    if run(SSH + [host, "echo ok"]).stdout.strip() != "ok":
        print("    NON raggiungibile, salto\n")
        continue

    if mode == "git":
        r = run(SSH + [host, "cd ~/Progetti/bravoric-ssh-client && git pull --ff-only origin main"])
        print(
            "    git pull:",
            r.stdout.strip().splitlines()[-1] if r.stdout.strip() else r.stderr.strip()[:120],
        )
    else:
        rdir = run(SSH + [host, "ls -d ~/Progetti/bravoric-ssh-client"]).stdout.strip()
        if not rdir:
            print("    (repo assente: salto)\n")
            continue
        # tar del pacchetto -> swap atomico sul remoto (con backup finche' non riesce)
        tar = subprocess.Popen(["tar", "czf", "-", "-C", REPO, PKG], stdout=subprocess.PIPE)
        remote = (
            "set -e; cd ~/Progetti/bravoric-ssh-client; "
            f"rm -rf {PKG}.new; mkdir {PKG}.new; "
            f"tar xzf - -C {PKG}.new --strip-components=1; "
            f"rm -rf {PKG}.old; mv {PKG} {PKG}.old; mv {PKG}.new {PKG}; rm -rf {PKG}.old; "
            f"find {PKG} -name __pycache__ -type d -exec rm -rf {{}} + 2>/dev/null; "
            "echo COPIATO"
        )
        ssh = subprocess.Popen(
            SSH + [host, remote],
            stdin=tar.stdout,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        if tar.stdout is not None:
            tar.stdout.close()
        out, err = ssh.communicate()
        tar.wait()
        print("    copia:", out.strip() or err.strip()[:150])

    if mode != "copy":
        run(
            SSH
            + [
                host,
                "find ~/Progetti/bravoric-ssh-client/bravoric_ssh_client -name __pycache__ -type d -exec rm -rf {} + 2>/dev/null; true",
            ]
        )

    verify(host, name)
    print()
