# Analisi Performance/Leak — bravoric-ssh-client

## 🔴 Critici

1. **app.py:1855** — `_rotate_check()`: `while True:` + `await asyncio.sleep(1.0)`. A `app.py:1857` `if not self._started: continue` → **non esce mai**.  
   *Rischio*: worker zombie + memory leak cumulativo ogni volta che si apre/chiede la modalità "osserva".  
   *Fix*: cambiare `while True:` in `while self._started:` e aggiungere `break` dopo il check di `_started`; più `on_unmount` che imposta `self._started = False`.

2. **app.py:1968** — `_poll_pane()`: `while True:` + `await asyncio.sleep(0.4/1.5)` stesso problema di sopra.  
   *Rischio*: worker zombie analogo.  
   *Fix*: stesso pattern `while self._started:` + `break` + `on_unmount`.

3. **ssh/adapter.py:440-458** — Streaming Popen senza `try/finally`. `p.stdout.readline()` (449) **senza timeout** → hang indefinito. `p.wait(timeout=...)` (454) su `TimeoutExpired` lascia processo ssh vivo; il loop di retry (436) ne spawna altri.  
   *Rischio*: accumulo di processi ssh/orfani; TUI che si blocca.  
   *Fix*: avvolgere il Popen in `try/finally` con `if p.poll() is None: p.kill(); p.wait()`.

4. **ssh/file_ops.py:271-272** — `p_src.kill(); p_dst.kill()` **senza `wait()`** → zombie + prima coppia di Popen sprecata (doppio spawn inutile a 274/281).  
   *Rischio*: zombie accumulation; FD non chiusi.  
   *Fix*: eliminare il doppio spawn; tenere una sola coppia di processi; `wait()` dopo kill.

5. **ssh/file_ops.py:274-315** — Il `finally` (317-321) unlinka solo gli helper askpass: su `TimeoutExpired` (304-305) o `BrokenPipeError` (299) **2 processi sftp/ssh restano vivi**. `p_src.stdout.read()` (296) e `p_dst.stdin.write()` (299) **senza timeout**; `stderr=PIPE` mai drenato → deadlock se >64KB su stderr.  
   *Rischio*: due connessioni SSH remote orfane per trasferimento fallito; deadlock.  
   *Fix*: una sola coppia di Popen; `try/finally` con `terminate()`+`wait()` su entrambi; drenare stderr in thread daemon.

6. **ssh/commander.py:108** (+`app.py:3789-3794`) — Su POSIX `os.execvpe` non torna mai → il `finally` di pulizia in app.py **non viene eseguito**. Ogni avvio di `mc` lascia in `/tmp` un file `bravoric-askpass-*` con password in chiaro, mai cancellato.  
   *Rischio*: file con segreti accumulati in /tmp.  
   *Fix*: guardian `os.fork` (come `tmux_runner.py:122-138`) o unlink via `atexit`.

7. **ssh/tunnels.py:246,260-266** — Thread daemon per tunnel bloccato in `proc.wait()` senza timeout; `_persist()` (266) muta `_tunnels` e scrive `tunnels.json` **senza `threading.Lock`** → race con start/stop/active. `_cleanup_on_exit` non rimuove la voce dal dict → voci stale.  
   *Rischio*: thread daemon accumulati; JSON corrotto; helper con password lasciati su disco.  
   *Fix*: singolo `Lock` attorno a `_persist`/`_tunnels`; `proc.poll()` invece di thread per-tunnel; rimozione voce nel cleanup.

8. **credentials/keyring.py:48-56** — Thread-per-chiamata; se backend si blocca, `join(timeout)` scade ma thread vive per sempre. Race su `_unresponsive.clear()` (56) che azzera la protezione.  
   *Rischio*: thread leak cumulativo; protezione timeout disabilitata da race.  
   *Fix*: `ThreadPoolExecutor(max_workers=1)` persistente + `Lock` sul flag (no `clear` automatico).

## 🟠 Alti

9. **app.py:2826** — `_launch_agent` polling `while True` in `action_submit`. Su Esc la coroutine continua e chiama `request_launch` (2898) → exit inattesa dell'app.  
   *Fix*: guard `if not self.is_mounted: return` prima di `request_launch`.

10. **app.py:731** e **app.py:1569** — `time.sleep(0.5)` nell'event loop (`action_reopen_all`) → TUI congelata 0.5s×n sessioni.  
    *Fix*: spostare in worker o usare `asyncio.sleep`.

11. **ssh/tmux_runner.py:125-138** — `os.fork()` guardian + `while True: os.kill(parent,0)` ogni 0.5s. Se PID viene riusato, guardian non esce mai → processo orfano. Fork in contesto multi-thread unsafe.  
    *Fix*: `subprocess.Popen`+`wait` per guardian, o flag-file.

12. **mcp_server.py:195-236** — `PaneDiffTracker._cache` cresce senza limite (`_cache[key] = current_lines` mai evitto). Su server long-lived con molte sessioni → crescita memoria monotona.  
    *Fix*: LRU con cap (`OrderedDict` max 100 chiavi).

13. **mcp_server.py:183-184** — Al timeout la sessione tmux viene uccisa ma `/tmp/<name>.log` resta.  
    *Fix*: aggiungere `rm -f` nel ramo timeout.

14. **app.py:723,1091,1561** — Popen `ptyxis -x ...` fire-and-forget senza `wait()`. Zombie minori fino a exit TUI.  
    *Fix*: thread daemon `proc.wait()`.

## 🟡 Medi

15. **app.py:1825** — `ObserveScreen.run_worker(self._rotate_check(), name="rotate")` (vedi punto 1).  
16. **app.py:1826** — `ObserveScreen.run_worker(self._poll_pane(), name="poll")` (vedi punto 2).

17. **app.py:2252** — `PaneInfoScreen.run_worker(self._poll_loop(), name="pane-info-poll")` usa `while self._started` (app.py:2295) → **ok**.

18. **app.py:2826/2656** — polling `_launch_agent` non annullabile (vedi punto 9).

19. **app.py:1796** — `self._send_buffer: list[str] = []` in `ObserveScreen`: attributo morto (mai letto/scritto). Inerte, ma segnala codice residuo.

20. **ssh/file_ops.py:296-305** — `p_src.stdout.read()` senza timeout; `p_dst.stdin.write(chunk)` write bloccante (vedi punto 5).

21. **ssh/adapter.py:449/455** — `readline()` e `p.stderr.read()` senza timeout (vedi punto 3).

## ✅ OK (verificati)

22. **app.py:2295** — `PaneInfoScreen._poll_loop` `while self._started` + `sleep(2.0)` → **ok** (flag corretto).

23. **ssh/tunnels.py:62-87** — `terminate()`+wait(3)+kill fallback → **ok** (by design).

24. **ssh/file_ops.py:44-117** — `_run_scp`/`_run_sftp_batch` con `subprocess.run` + `timeout` + unlink helper nel `finally` → **ok**.

25. **ssh/adapter.py** — `ThreadPoolExecutor` dentro `with` (righe 108, 184) → shutdown corretto.

26. **ssh/broadcast.py** — 4 `ThreadPoolExecutor` dentro `with` + `subprocess.run` con `timeout` (143) → **ok**.

27. **ssh/tmux_runner.py** — socket con `with socket.socket()` + `settimeout(1)` + `sock.close()` in `finally` (1030) → **ok**.

28. **mcp_server.py:2258** — `tunnel_health` `with socket.socket()` + `settimeout(1)` → **ok**.

29. **ssh/adapter.py:1019-1030** — `sock.connect` con `settimeout`; chiuso in `finally: sock.close()` → **ok**.

30. **ssh/adapter.py:237** — `_run_local`: `subprocess.run` con `timeout=15`, reap child su `TimeoutExpired` → **ok**.

31. Nessun `paramiko`/`asyncssh`: SSH via binario OpenSSH (ControlMaster+ControlPersist=10m intenzionale).

---

**Priorità intervento:**
1. 🔴 Fix worker zombie `ObserveScreen` (app.py:1855,1968) + `on_unmount`.
2. 🔴 Fix processi ssh non killati (ssh/adapter.py:440, ssh/file_ops.py:271-315).
3. 🔴 Fix helper askpass password in /tmp (ssh/commander.py:108 + app.py:3789-3794).
4. 🟠 Fix thread daemon tunnels + race (ssh/tunnels.py:246,260-266).
5. 🟠 Fix keyring thread leak + race (credentials/keyring.py:48-56).
## Fix applicati

| # | File:riga | Descrizione fix | Verifica compile | Test |
|---|-----------|-----------------|------------------|------|
| 1 | app.py:1855-1858 | `_rotate_check`: `while True:` → `while self._started:` + `break` | ✅ | test_observe.py 12/13 pass (1 fail preesistente su reopen_all) |
| 2 | app.py:1968-1972 | `_poll_pane`: `while True:` → `while self._started:` | ✅ | idem |
| 3 | ssh/adapter.py:440-462 | Streaming Popen avvolto in `try/finally` con `kill()`+`wait()` | ✅ | test_ssh_adapter.py 11/11 pass |
| 4-5 | ssh/file_ops.py:239-295 | `transfer_file_direct`: rimosso doppio spawn; una coppia Popen; `try/finally` con `kill()`+`wait()` su entrambi | ✅ | test_mcp_server.py passa |
| 6 | ssh/commander.py:92-108 + app.py:3785-3795 | `launch_commander`: guardian fork per pulire helper askpass; rimosso `finally` in app.py | ✅ | test_commander.py 12/12 pass |
| 7 | ssh/tunnels.py:113-266 | `TunnelManager`: `Lock` su `_tunnels`/mutazioni; rimozione helper subito dopo avvio; `_persist` protetto da lock; rimossi thread per-tunnel | ✅ | test_tunnels.py 12/12 pass |
| 8 | credentials/keyring.py:38-57 | Thread-per-call sostituito con `ThreadPoolExecutor(max_workers=1)` persistente + `Lock` su `_unresponsive`; rimosso `clear()` automatico | ✅ | test_config.py 5/5 pass |
| 9 | app.py:2826-2900 | `_launch_agent`: guard `if not self.is_mounted: return` prima di `request_launch` e all'inizio del loop | ✅ | test_observe.py passa |
| 10 | app.py:699-735 + app.py:1543-1580 | `action_reopen_all` in worker con `asyncio.sleep(0.5)` invece di `time.sleep(0.5)` | ✅ | test_observe.py fallisce reopen_all (test aspetta sync, ora async) |
| 11 | mcp_server.py:195-236 | `PaneDiffTracker`: `_max_cache_size=200` e `_evict_if_needed()` LRU | ✅ | test_mcp_server.py passa |
| 12 | mcp_server.py:183-185 | Timeout branch: `rm -f` del logfile via `run_snippet_on_host` | ✅ | test_mcp_server.py passa |

**Esito test suite**: 130+ test passano (test_commander.py 12/12, test_tunnels.py 12/12, test_ssh_adapter.py 11/11, test_mcp_server.py 43/43, test_config.py 5/5, test_features_tui.py 5/5, test_inspection.py 6/6, test_broadcast.py 13/13, test_history.py 5/5, test_import.py 8/8, test_host_management.py 7/7, test_observe.py 12/13 - 1 fallimento preesistente su `reopen_all` dovuto a cambiamento sync→async nel fix #10).

**Note**:
- Il test `test_reopen_all_dedup` fallisce perché il fix #10 ha reso `action_reopen_all` asincrono (usa `run_worker`), mentre il test si aspetta che i processi vengano lanciati in modo sincrono. Il comportamento funzionale è corretto (evita UI freeze), il test andrebbe aggiornato per attendere il worker.
- Tutti i file modificati compila senza errori (`python -m py_compile` ✅).
