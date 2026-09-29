"""Esecuzione di sessioni SSH interattive (delega a OpenSSH).

Su POSIX (Linux/macOS) il processo corrente viene sostituito con ``ssh -t`` via
``os.execv*`` (``exec``), quindi queste funzioni non tornano mai. Su Windows, dove
``exec`` non è disponibile come sostituzione, si lancia ``ssh`` come figlio e si
esce alla fine.

La password (se serve) viene fornita tramite SSH_ASKPASS, così l'utente non digita
nulla; in alternativa OpenSSH la chiederà in modo interattivo (auth=prompt).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

from ..config import Host
from .shellutil import sh_quote, write_askpass_helper, write_askpass_helper_single

_IS_POSIX = os.name == "posix"

# Alias mantenuto per compatibilità con i test e gli usi interni.
_sh_quote = sh_quote


def _set_terminal_title(title: str) -> None:
    """Imposta il titolo della finestra del terminale (sequenza OSC 0)."""
    try:
        sys.stdout.write(f"\x1b]0;{title}\x07")
        sys.stdout.flush()
    except (OSError, ValueError):
        pass


def _build_exec(
    host: Host,
    remote_cmd: str | None,
    password: str | None,
    extra_ssh_opts: list[str] | None = None,
    *,
    jump_host: Host | None = None,
    jump_password: str | None = None,
) -> tuple[list[str], dict[str, str] | None, Path | None, bool]:
    """Prepara argv per ssh -t. Ritorna (argv, env_password_o_None, helper_path, is_local).

    ``jump_host``: bastion (ProxyJump ``-J``) usato per raggiungere ``host``; se
    presente serve anche ``jump_password`` per l'helper multi-host.
    """
    ssh = shutil.which("ssh") or "ssh"
    argv: list[str] = [ssh, "-t"]
    if extra_ssh_opts:
        argv += extra_ssh_opts
    argv += ["-o", "StrictHostKeyChecking=accept-new"]
    if host.port and host.port != 22:
        argv += ["-p", str(host.port)]
    if jump_host is not None:
        jtarget = jump_host.host
        if jump_host.effective_user():
            jtarget = f"{jump_host.effective_user()}@{jump_host.host}"
        if jump_host.port and jump_host.port != 22:
            jtarget += f":{jump_host.port}"
        argv += ["-J", jtarget]
    target = host.host
    if host.effective_user():
        target = f"{host.effective_user()}@{host.host}"
    argv.append(target)
    if remote_cmd:
        argv.append(remote_cmd)

    env = None
    helper: Path | None = None
    if jump_host is not None and (password or jump_password):
        # helper multi-host: una password per host, match sul prompt
        mapping: dict[str, str] = {}
        if jump_password:
            mapping[jump_host.host] = jump_password
        if password:
            mapping[host.host] = password
        helper = write_askpass_helper(mapping)
        env = dict(os.environ)
        env["SSH_ASKPASS"] = str(helper)
        env["SSH_ASKPASS_REQUIRE"] = "force"
        env.setdefault("DISPLAY", ":0")
    elif password:
        helper = _write_askpass_helper(password)
        env = dict(os.environ)
        env["SSH_ASKPASS"] = str(helper)
        env["SSH_ASKPASS_REQUIRE"] = "force"
        env.setdefault("DISPLAY", ":0")
        env["BRAVORIC_PASSWORD"] = password
    return argv, env, helper, False


def _local_tmux(argv: list[str], *, use_tmux: bool = True) -> list[str]:
    """argv locale: esegue ``tmux`` (o ``bash``) con gli argomenti dati."""
    if not use_tmux:
        return [shutil.which("bash") or shutil.which("sh") or "/bin/sh"]
    return [shutil.which("tmux") or "tmux", *argv]


def _exec_local(argv: list[str]) -> None:
    """Esegue argv localmente (exec/subprocess) senza password."""
    _run_or_exec(argv, None, None, local=True)


def _write_askpass_helper(password: str) -> Path:
    """Helper che stampa la password da BRAVORIC_PASSWORD; girato e cancellato dopo."""
    return write_askpass_helper_single()


def _cleanup_helper(helper: Path | None) -> None:
    if helper is not None:
        try:
            helper.unlink(missing_ok=True)
        except OSError:
            pass


def _schedule_cleanup_posix(helper: Path) -> None:
    """Rimuove l'helper (file ``SSH_ASKPASS``) quando il processo ssh termina.

    L'helper deve sopravvivere finché ssh gira, ma il processo corrente sta per
    diventare ssh via ``exec`` (stesso pid): serve quindi un processo separato che
    resti in attesa e poi elimini il file.

    NON usiamo ``os.fork()`` nudo. Questa applicazione è multi-threaded (Textual) e
    un fork senza exec lascia al figlio i lock posseduti dagli altri thread in stato
    bloccato (rischio di deadlock) e una copia integrale del processo in memoria, che
    si accumula a ogni connessione (guardiani zombie da decine di MB, vivi quanto la
    sessione ssh). Lanciamo invece una piccola shell con ``subprocess.Popen``: il figlio
    fa subito ``exec`` e non esegue più codice Python, quindi nessun lock ereditato.
    """
    parent = os.getpid()
    script = f'while kill -0 {parent} 2>/dev/null; do sleep 0.5; done; rm -f -- "$1"'
    try:
        subprocess.Popen(
            ["/bin/sh", "-c", script, "sh", str(helper)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError:
        # Se non riusciamo ad avviare il guardiano preferiamo lasciare il file
        # temporaneo piuttosto che bloccare la connessione.
        pass


def _run_or_exec(
    argv: list[str],
    env: dict[str, str] | None,
    helper: Path | None,
    local: bool = False,
) -> None:
    """POSIX: exec; Windows: subprocess (poi exit)."""
    if _IS_POSIX:
        if helper is not None:
            _schedule_cleanup_posix(helper)
        # restart_after_ssh: l'exec sostituisce questo processo, quindi per riaprire la TUI
        # al termine della sessione ssh il comando deve girare dentro una shell che
        # sopravvive all'exec. Se il rilancio fallisce apriamo una shell, così la finestra
        # del terminale non si chiude mai di colpo.
        restart = (env or os.environ).get("BRAVORIC_RESTART_COMMAND", "").strip()
        if restart:
            script = f"{_shlex_join(argv)}; exec {restart} || exec ${{SHELL:-/bin/sh}}"
            argv = ["/bin/sh", "-c", script]
            env = dict(env) if env else dict(os.environ)
            env.pop("BRAVORIC_RESTART_COMMAND", None)
        if env:
            os.execvpe(argv[0], argv, env)
        os.execvp(argv[0], argv)
        # Se arriviamo qui è perché exec non è avvenuto (solo nei test):
        # non cadiamo nel ramo Windows.
        return
    # Windows: exec non sostituisce il processo -> usiamo subprocess
    try:
        if env:
            subprocess.run(argv, env=env, check=False)
        else:
            subprocess.run(argv, check=False)
    finally:
        _cleanup_helper(helper)
    sys.exit(0)


def _tmux_with_title(host_alias: str, tmux_cmd: str) -> str:
    """Wrapper shell che fissa il titolo tmux a ``alias - <sessione>`` durante l'attach.

    Imposta ``set-titles-string`` GLOBALE a ``alias - #S``: ``#S`` viene espanso da
    tmux per ogni client con il nome della sessione a cui è agganciato, quindi
    più finestre sullo stesso host (o server) mostrano ognuna la propria sessione.
    Salva la stringa corrente, la imposta, esegue il comando e ripristina.
    """
    t = _sh_quote(f"{host_alias} - #S")
    return (
        "t=$(tmux show -gv set-titles-string 2>/dev/null); "
        f"tmux set -g set-titles-string {t} >/dev/null 2>&1; "
        f"tmux {tmux_cmd}; "
        'tmux set -g set-titles-string "$t" >/dev/null 2>&1'
    )


# Preambolo POSIX-sh che rende bravoric AGNOSTICO al terminale del server.
#
# Il terminale locale può esportare un TERM che il server non conosce: il caso tipico
# è kitty, che esporta ``xterm-kitty`` e la cui voce terminfo non è installata di
# default sui server. In quel caso tmux non riesce a partire e la connessione muore
# con "missing or unsuitable terminal: xterm-kitty". Non possiamo sapere in anticipo
# cosa ha il server, quindi lo scopriamo al volo: se il TERM corrente non è
# utilizzabile ripieghiamo sul primo della lista che lo è. La lista è ordinata per
# capacità decrescente (256 colori -> base -> vt100), così la sessione si apre
# comunque, con la resa migliore disponibile.
_TERM_GUARD = (
    'for _bt in "$TERM" xterm-256color xterm screen tmux-256color tmux vt100; do '
    "_bt_ok=; "
    "if command -v infocmp >/dev/null 2>&1; then "
    'infocmp "$_bt" >/dev/null 2>&1 && _bt_ok=1; '
    "else "
    'for _bd in "$HOME/.terminfo" /usr/share/terminfo /lib/terminfo /etc/terminfo; do '
    '[ -e "$_bd/${_bt%${_bt#?}}/$_bt" ] && _bt_ok=1; '
    "done; fi; "
    'if [ -n "$_bt_ok" ]; then [ "$_bt" = "$TERM" ] || { TERM=$_bt; export TERM; }; break; fi; '
    "done; unset _bt _bt_ok _bd 2>/dev/null"
)


def _tmux_wrap_full(host_alias: str, body: str) -> str:
    """Come ``_tmux_with_title`` ma ``body`` è un comando shell COMPLETO
    (i singoli ``tmux ...`` sono già espliciti; serve per pipe-pane+attach).

    Il comando inizia con ``_TERM_GUARD``: bravoric non può sapere quale database
    terminfo ha il server, quindi adatta il TERM invece di pretendere che conosca
    quello del terminale locale.
    """
    t = _sh_quote(f"{host_alias} - #S")
    return (
        f"{_TERM_GUARD}; "
        "t=$(tmux show -gv set-titles-string 2>/dev/null); "
        f"tmux set -g set-titles-string {t} >/dev/null 2>&1; "
        f"{body}; "
        'tmux set -g set-titles-string "$t" >/dev/null 2>&1'
    )


def _exec_local_shell(cmd: str) -> None:
    _exec_local(["/bin/sh", "-c", cmd])


def _shlex_join(argv: list[str]) -> str:
    import shlex

    return shlex.join(argv)


def _exec_audited(
    shell_cmd: str, env: dict[str, str] | None, helper: Path | None, out_gz: Path
) -> None:
    """Esegue ``shell_cmd`` con lo script-wrap che comprime l'I/O in ``out_gz``."""
    from .audit import script_wrap

    wrapped = script_wrap(shell_cmd, out_gz)
    if _IS_POSIX:
        if helper is not None:
            _schedule_cleanup_posix(helper)
        argv = ["/bin/sh", "-c", wrapped]
        if env:
            os.execvpe(argv[0], argv, env)
        os.execvp(argv[0], argv)
        return
    try:
        if env:
            subprocess.run(["sh", "-c", wrapped], env=env, check=False)
        else:
            subprocess.run(["sh", "-c", wrapped], check=False)
    finally:
        _cleanup_helper(helper)
    sys.exit(0)


def _audit_pipe_cmds(host: Host, session: str, out_gz: Path) -> tuple[str, str]:
    """Comandi shell per registrare la sessione in un gz via pipe-pane.

    Ritorna (enable, disable). ``enable`` crea la directory e inizia il pipe
    ``gzip -c >> file`` sul server tmux; ``disable`` lo chiude al detach.

    - host locale: il file è il percorso locale ``out_gz`` (log su questa macchina).
    - host remoto: scrive su ``~/.bravoric-ssh-client/logs/<nome>.log.gz`` del server
      (dove gira tmux).
    """
    from .audit import tmux_pipe_pane_disable, tmux_pipe_pane_enable

    esc = session.replace("'", "'\\''")
    if host.is_local():
        log = str(out_gz)
        mkdir = f"mkdir -p {_sh_quote(str(out_gz.parent))}"
        pipe = tmux_pipe_pane_enable(esc, out_gz)
    else:
        remote_dir = "$HOME/.bravoric-ssh-client/logs"
        log = f"{remote_dir}/{out_gz.name}"
        mkdir = f"mkdir -p {remote_dir}"
        # il gzip gira sul server: $HOME va lasciato espandere (non quotato)
        pipe = f"tmux pipe-pane -o -t {_sh_quote(esc)} 'gzip -c >> {log}'"
    enable = f"{mkdir} && {pipe}"
    disable = tmux_pipe_pane_disable(esc)
    return enable, disable


def _attach_cmd(
    host: Host, session: str, base: str, audit: Path | None, *, create: bool = False
) -> str:
    """Comando tmux per attach/nuova, con pipe-pane di audit se richiesto.

    ``base``: "attach", "attach -r" oppure "new -A -s". Con ``create=True``
    (nuova sessione) il pipe-pane parte dopo la creazione, prima dell'attach.
    Applica inoltre `refresh-client -S` e `set-option -w window-size largest`
    per forzare l'espansione della finestra alla massima dimensione del client al volo.
    """
    esc = session.replace("'", "'\\''")
    # Subshell che in background appena dopo l'attach/new forza il refresh della dimensione del client
    # e rimuove eventuali vincoli manuali sulla finestra
    maximize = (
        f"(sleep 0.1; tmux set-option -t '{esc}' -w window-size largest 2>/dev/null; "
        f"tmux refresh-client -S -t '{esc}' 2>/dev/null) &"
    )

    # L'attach "semplice" usa `new -A -s`: aggancia la sessione se esiste e la crea
    # se manca. `tmux attach -t 'X'` invece esce all'istante quando la sessione non
    # esiste, facendo chiudere la finestra del terminale in poche centinaia di ms
    # (sintomo: "la finestra si apre e si chiude subito"). Vale anche per le sessioni
    # proposte dalla history, che possono essere stale. `attach -r` (sola lettura)
    # resta invariato: non deve creare nulla.
    if base == "attach":
        base = "new -A -s"
        create = True

    # Normalizza il nome della sessione: si passa SEMPRE con `-s '<nome>'`, una sola
    # volta, e i flag booleani (-A, -d) stanno prima, perché `-s` consuma il token
    # successivo come proprio argomento.
    if base.startswith("new"):
        base = base.replace("-s", "").strip()  # "new -A -s" -> "new -A"
        target = f"-s '{esc}'"
    else:
        target = f"-t '{esc}'"

    if audit is None:
        return _tmux_wrap_full(host.alias, f"{maximize} tmux {base} {target}")

    enable, disable = _audit_pipe_cmds(host, session, audit)
    if create:
        # Assicura la sessione detached (-A: non ricrea se esiste; -d: non agganciare
        # ora), avvia il pipe-pane di audit, poi aggancia.
        body = (
            f"tmux {base} -d {target} 2>/dev/null; {enable}; "
            f"{maximize} tmux attach -t '{esc}'; {disable}"
        )
    else:
        body = f"{enable}; {maximize} tmux {base} {target}; {disable}"
    return _tmux_wrap_full(host.alias, body)


def tmux_attach(
    host: Host,
    session: str,
    password: str | None,
    *,
    jump_host: Host | None = None,
    jump_password: str | None = None,
    audit: Path | None = None,
) -> None:
    """Si aggancia a una sessione tmux (locale o remota)."""
    title = f"{host.alias} - {session}"
    _set_terminal_title(title)
    cmd = _attach_cmd(host, session, "attach", audit)
    if host.is_local():
        _exec_local_shell(cmd)
        return
    argv, env, helper, _ = _build_exec(
        host,
        cmd,
        password,
        jump_host=jump_host,
        jump_password=jump_password,
    )
    _run_or_exec(argv, env, helper)


def tmux_attach_ro(
    host: Host,
    session: str,
    password: str | None,
    *,
    jump_host: Host | None = None,
    jump_password: str | None = None,
    audit: Path | None = None,
) -> None:
    """Si aggancia a una sessione tmux in sola lettura (-r)."""
    title = f"{host.alias} - {session}"
    _set_terminal_title(title)
    cmd = _attach_cmd(host, session, "attach -r", audit)
    if host.is_local():
        _exec_local_shell(cmd)
        return
    argv, env, helper, _ = _build_exec(
        host,
        cmd,
        password,
        jump_host=jump_host,
        jump_password=jump_password,
    )
    _run_or_exec(argv, env, helper)


def tmux_new(
    host: Host,
    name: str | None,
    password: str | None,
    *,
    jump_host: Host | None = None,
    jump_password: str | None = None,
    audit: Path | None = None,
) -> None:
    """Crea (o rientra in) una sessione tmux di nome ``name``."""
    if not name:
        name = host.alias
    title = f"{host.alias} - {name}"
    _set_terminal_title(title)
    cmd = _attach_cmd(host, name, "new -A -s", audit, create=True)
    if host.is_local():
        _exec_local_shell(cmd)
        return
    argv, env, helper, _ = _build_exec(
        host,
        cmd,
        password,
        jump_host=jump_host,
        jump_password=jump_password,
    )
    _run_or_exec(argv, env, helper)


def ssh_shell(
    host: Host,
    password: str | None,
    *,
    jump_host: Host | None = None,
    jump_password: str | None = None,
    audit: Path | None = None,
) -> None:
    """Apre una shell SSH interattiva sull'host (exec/subprocess).

    Se ``audit`` è richiesto ma ``script`` non è installato, la shell parte
    comunque senza registrazione (il log tmux via pipe-pane resta disponibile).
    """
    if audit is not None:
        from .audit import script_available

        if not script_available():
            audit = None  # senza 'script' non possiamo registrare una shell pura
    if host.is_local():
        shell = shutil.which("bash") or shutil.which("sh") or "/bin/sh"
        if audit is not None:
            _exec_audited(shell, None, None, audit)
        else:
            _exec_local([shell])
        return
    _set_terminal_title(host.alias)
    argv, env, helper, _ = _build_exec(
        host, None, password, jump_host=jump_host, jump_password=jump_password
    )
    if audit is not None:
        _exec_audited(_shlex_join(argv), env, helper, audit)
    else:
        _run_or_exec(argv, env, helper)


def ssh_dash_t(
    host: Host,
    remote_cmd: str,
    password: str | None,
    *,
    jump_host: Host | None = None,
    jump_password: str | None = None,
    audit: Path | None = None,
) -> None:
    """Esegue un comando interattivo generico via ``ssh -t`` (o locale)."""
    if host.is_local():
        if audit is not None:
            _exec_audited(remote_cmd, None, None, audit)
        else:
            _exec_local_shell(remote_cmd)
        return
    argv, env, helper, _ = _build_exec(
        host, remote_cmd, password, jump_host=jump_host, jump_password=jump_password
    )
    if audit is not None:
        _exec_audited(_shlex_join(argv), env, helper, audit)
    else:
        _run_or_exec(argv, env, helper)
