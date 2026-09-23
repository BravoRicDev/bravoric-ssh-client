# HANDOFF — bravoric-ssh-client

## Stato attuale (2026-09-22)

### Problema iniziale
La TUI non si adattava alla dimensione del terminale — elementi tagliati, schermata LaunchAgentScreen tutta a sinistra non centrata.

### Root cause identificate e risolte
1. `#output-box` in LaunchAgentScreen senza `width` → default `1fr` annullava `align: center top`
2. `#form-box`, `#confirm-box`, `#pw-box` con `height: auto` → contenuto tagliato su terminali piccoli
3. `.box-title` senza `width` → titolo non centrato
4. `#bcast-mode` senza `width` → disallineato con ListView
5. `#send-text` TextArea senza altezza esplicita
6. `content-align: center` non è proprietà CSS valida in Textual
7. **Fix "default home"**: Se directory vuota → non si imposta home locale, si lascia la directory remota di default
8. **Widget types**: `Slider` non esiste in Textual 8.2.8 → sostituito con `Input(type="number")`

### Fix applicati (commit `ec7b99e` → `3f05727`)
- CSS globale: `Screen { width: 100%; height: 1fr; }`, `.box-title { width: 80%; }`, `#form-box/#confirm-box/#pw-box { height: 1fr; overflow-y: auto; }`, `.suggest-list { width: 80%; }`, `#bcast-mode { width: 80%; }`, `.box-textarea { width: 100%; height: 1fr; }`, `#send-text { width: 100%; height: 1fr; }`
- CSS LaunchAgentScreen: `#status-box { height: 1; width: 80%; }`, `#output-box { display: none; }`, `Input[type="number"] { width: 10ch; }`
- LaunchAgentScreen: `Slider` → `Input(type="number")` per compatibilità Textual 8.2.8
- LaunchAgentScreen: `force` → `Checkbox` invece di `Input`
- LaunchAgentScreen: se `path` vuoto → non si imposta home locale, si lascia la directory remota di default
- Nome sessione: se `path` vuoto → `{agent}-sessione` invece di errore su `Path(path).name`

### Deploy
GitHub main + pull su cubotto-di-legno-ssh (192.168.1.38). App funziona senza errori.

### File chiave
- `/home/riccardo/Progetti/bravoric-ssh-client/bravoric_ssh_client/app.py`
  - CSS in `BravoricApp.CSS` righe 71-116
  - CSS `LaunchAgentScreen` righe 2310-2315
  - `LaunchAgentScreen.compose()` righe 2338-2360
  - `LaunchAgentScreen.action_submit()` righe 2370-2385
  - `LaunchAgentScreen._launch_agent()` righe 2390-2620

### Widget attuali in LaunchAgentScreen
| Campo | Widget | Note |
|-------|--------|------|
| Agente | `Select` | opencode, claude, pi |
| Directory di lavoro | `Input` | Vuoto = home remota |
| Titolo | `Input` | Opzionale |
| Argomenti extra | `Input` | Opzionale |
| Prompt iniziale | `Input` | Opzionale |
| Timeout (s) | `Input(type="number")` | Range 5-120, default 25 |
| Force | `Checkbox` | Ricrea se esiste |

### Prossimi possibili miglioramenti
- Aggiungere `ProgressBar` per il polling di caricamento TUI
- Aggiungere `Log` widget per l'output invece di `Static`
- Considerare `TextArea` per il prompt iniziale (multiriga nativa)
- Validazione lato client del campo numero (min/max)
