/**
 * Bravoric SSH Remote Bridge Extension for Pi Agent
 *
 * Integrates bravoric-ssh-client with Pi Agent:
 * - Selezione Host → Sessione senza apertura forzata del terminale (/ssh-select o menu Ctrl+H)
 * - Apertura manuale/esplicita della finestra terminale dedicata (F6 o /ssh-window)
 * - Chiusura deterministica del processo attivo (/ssh-close)
 * - Toggle Pianificazione (LOCALE) vs Esecuzione Remota (REMOTE)
 * - Toggle contesto LLM (LINKED/UNLINKED)
 * - Conferme di sicurezza per host di produzione
 */

import { spawn } from "node:child_process";
import { execFile } from "node:child_process";
import { readFile } from "node:fs/promises";
import * as fs from "node:fs";
import * as path from "node:path";
import * as os from "node:os";
import { createHash } from "node:crypto";
import { promisify } from "node:util";
import type {
	ExtensionAPI,
	ExtensionContext,
} from "@earendil-works/pi-coding-agent";

const execFileAsync = promisify(execFile);

const HOME = os.homedir();

/** Primo percorso che esiste, o `null` se nessuno. Un candidato illeggibile non ferma la ricerca. */
function firstExisting(paths: string[]): string | null {
	for (const p of paths) {
		try {
			if (fs.existsSync(p)) return p;
		} catch {
			/* ignora e prova il prossimo */
		}
	}
	return null;
}

// NESSUN PATH DI UNA MACCHINA SOLA: ogni percorso e' sovrascrivibile da variabile
// d'ambiente e i default sono le posizioni note su macOS e Linux. Vince il primo
// candidato che esiste, quindi su una macchina gia' configurata il comportamento e'
// quello di sempre; le variabili servono a chi installa altrove.
//   BRAVORIC_SSH_CLIENT_DIR  cartella del client (quella con la venv)
//   BRAVORIC_PYTHON          interprete Python che ha il pacchetto installato
//   BRAVORIC_BRIDGE          percorso di bravoric-bridge.py
//   BRAVORIC_BIN             eseguibile della CLI bravoric-ssh
const CLIENT_DIR =
	process.env.BRAVORIC_SSH_CLIENT_DIR ??
	firstExisting([
		path.join(HOME, "Progetti", "bravoric-ssh-client"),
		path.join(HOME, "progetti", "bravoric-ssh-client"),
	]) ??
	path.join(HOME, "Progetti", "bravoric-ssh-client");

const PYTHON_BIN =
	process.env.BRAVORIC_PYTHON ??
	firstExisting([
		path.join(CLIENT_DIR, ".venv", "bin", "python"),
		path.join(CLIENT_DIR, ".venv", "bin", "python3"),
		"/usr/bin/python3",
	]) ??
	path.join(CLIENT_DIR, ".venv", "bin", "python");

const BRIDGE_SCRIPT =
	process.env.BRAVORIC_BRIDGE ??
	firstExisting([
		path.join(HOME, ".pi", "agent", "extensions", "bravoric-bridge.py"),
		path.join(CLIENT_DIR, "integrations", "pi", "bravoric-bridge.py"),
	]) ??
	path.join(HOME, ".pi", "agent", "extensions", "bravoric-bridge.py");

const BRAVORIC_BIN =
	process.env.BRAVORIC_BIN ??
	firstExisting([
		path.join(HOME, ".local", "bin", "bravoric-ssh"),
		"/usr/local/bin/bravoric-ssh",
		"/opt/homebrew/bin/bravoric-ssh",
	]) ??
	path.join(HOME, ".local", "bin", "bravoric-ssh");

const NEW_SESSION = "➕ Nuova sessione…";
const SESSION_PREFIX = "🪟 ";

// Shell che indicano "nessun processo in primo piano da chiudere".
// NB: 'node' NON è incluso: molte TUI (es. opencode) girano come 'node'.
const SHELL_COMMANDS = new Set([
	"bash",
	"zsh",
	"sh",
	"dash",
	"fish",
	"ksh",
	"tcsh",
	"csh",
	"ash",
	"login",
	"tmux",
	"nu",
	"xonsh",
	"elvish",
	"oil",
	"osh",
	"pwsh",
]);

interface HostInfo {
	alias: string;
	host: string;
	user: string;
	tags: string[];
	is_prod: boolean;
}

interface PaneInfoSnapshot {
	command: string;
	cwd: string;
	pid: number;
	size: string;
	is_shell: boolean;
}

interface RemoteState {
	selectedHost: string;
	mode: "OFF" | "ON";
	contextLinked: boolean;
	selectedSession: string;
	execMode: "tmux" | "direct";
	isProd: boolean;
	pingStatus: { ok: boolean; detail: string; timestamp: number } | null;
	lastOutputPreview: string;
	paneInfo: PaneInfoSnapshot | null;
}

const state: RemoteState = {
	selectedHost: "localhost",
	mode: "OFF",
	contextLinked: true,
	selectedSession: "Pi",
	execMode: "tmux",
	isProd: false,
	pingStatus: null,
	lastOutputPreview: "",
	paneInfo: null,
};

let cachedHosts: HostInfo[] = [];

async function runBridge(cmd: string, args: string[] = []): Promise<any> {
	try {
		const { stdout } = await execFileAsync(PYTHON_BIN, [BRIDGE_SCRIPT, cmd, ...args], {
			timeout: 20000,
		});
		return JSON.parse(stdout.trim());
	} catch (err: any) {
		return { ok: false, error: err.message || String(err) };
	}
}

async function refreshHosts(): Promise<HostInfo[]> {
	const res = await runBridge("hosts");
	if (res.ok && Array.isArray(res.hosts)) {
		cachedHosts = res.hosts;
		const cur = cachedHosts.find((h) => h.alias === state.selectedHost);
		if (cur) {
			state.isProd = cur.is_prod;
		}
	}
	return cachedHosts;
}

async function pingCurrentHost(): Promise<{ ok: boolean; detail: string }> {
	const res = await runBridge("ping", [state.selectedHost]);
	if (res.ok !== undefined) {
		state.pingStatus = {
			ok: res.ok,
			detail: res.detail || (res.ok ? "Online" : "Offline"),
			timestamp: Date.now(),
		};
		return { ok: res.ok, detail: state.pingStatus.detail };
	}
	state.pingStatus = { ok: false, detail: "Errore esecuzione ping", timestamp: Date.now() };
	return { ok: false, detail: "Errore" };
}

function updateWidget(ctx: ExtensionContext): void {
	if (!ctx.hasUI) return;

	const theme = ctx.ui.theme;
	const isOnline = state.pingStatus?.ok;
	const statusDot =
		state.pingStatus === null
			? theme.fg("warning", "◌")
			: isOnline
				? theme.fg("success", "●")
				: theme.fg("error", "○");

	const hostLabel =
		theme.fg("accent", state.selectedHost) +
		(state.isProd ? theme.fg("error", " [PROD ⚠️]") : "");

	const modeLabel =
		state.mode === "ON"
			? theme.fg("success", "● REMOTE")
			: theme.fg("muted", "○ LOCALE");

	const sessionLabel =
		state.execMode === "tmux"
			? theme.fg("accent", state.selectedSession || "—") +
				theme.fg("muted", " (tmux)")
			: theme.fg("warning", "Direct");

	const line1 = `⚡ Bravoric [ ${hostLabel} ${statusDot} ] | Sessione: [ ${sessionLabel} ] | Modo: [ ${modeLabel} ] | Finestra: [ ${theme.fg("accent", "F6")} ] | Chiudi: ${theme.fg("accent", "/ssh-close")} | Menu: ${theme.fg("accent", "Ctrl+H")}`;

	const lines = [line1];
	if (state.paneInfo) {
		const p = state.paneInfo;
		const procLabel = p.is_shell
			? theme.fg("muted", `${p.command || "shell"} (idle)`)
			: theme.fg("success", p.command || "?");
		lines.push(
			`   ↳ Pane: ${procLabel}${theme.fg("muted", " @ ")}${theme.fg("accent", shortPath(p.cwd) || "—")}${theme.fg("muted", ` · pid ${p.pid} · ${p.size}`)} | Incolla: ${theme.fg("accent", "/ssh-paste")} | Finestre: ${theme.fg("accent", "/ssh-windows")} | Riavvia: ${theme.fg("accent", "/ssh-restart")}`
		);
	}

	ctx.ui.setWidget("bravoric-remote", lines, { placement: "belowEditor" });
}

/** Ultimi due componenti del path, per un widget compatto. */
function shortPath(p: string): string {
	if (!p) return "";
	const parts = p.replace(/\/+$/, "").split("/");
	return parts.slice(-2).join("/") || p;
}

function openAttachedWindow(ctx: ExtensionContext, host?: string, session?: string): void {
	const h = host || state.selectedHost || "localhost";
	const s = session || state.selectedSession;

	if (!s) {
		ctx.ui.notify("Nessuna sessione selezionata. Selezionala prima con /ssh-select o dal menu (Ctrl+H).", "warning");
		return;
	}

	ctx.ui.notify(`Apertura finestra per '${h}:${s}'...`, "info");

	try {
		const child = spawn(
			"ptyxis",
			[
				"--new-window",
				"-T",
				`Bravoric: ${h} - ${s}`,
				"--",
				BRAVORIC_BIN,
				"--attach",
				h,
				s,
			],
			{
				detached: true,
				stdio: "ignore",
			}
		);
		child.unref();
		ctx.ui.notify(`Finestra avviata per '${h}:${s}'!`, "info");
	} catch (err: any) {
		ctx.ui.notify(`Errore avvio finestra: ${err.message}`, "error");
	}
}

/**
 * Flusso di selezione Host → Sessione tmux.
 * Imposta l'host e la sessione attiva SENZA aprire il terminale.
 */
async function selectSessionFlow(ctx: ExtensionContext): Promise<void> {
	if (cachedHosts.length === 0) {
		await refreshHosts();
	}
	if (cachedHosts.length === 0) {
		ctx.ui.notify("Nessun host configurato in bravoric-ssh-client.", "error");
		return;
	}

	// ── Passo 1/2 · Host ────────────────────────────────────────────────
	const hostChoices = cachedHosts.map((h) => {
		const tag = h.is_prod ? "  ⚠️ [PROD]" : "";
		const userPart = h.user ? `${h.user}@` : "";
		return `${h.alias}  —  ${userPart}${h.host}${tag}`;
	});

	const hostChoice = await ctx.ui.select("1/2 · Seleziona il server (Host):", hostChoices);
	if (!hostChoice) return;

	const alias = hostChoice.split("  —  ")[0].trim();
	const host = cachedHosts.find((h) => h.alias === alias);
	if (!host) {
		ctx.ui.notify(`Host '${alias}' non riconosciuto.`, "error");
		return;
	}

	if (host.is_prod) {
		const ok = await ctx.ui.confirm(
			"⚠️ HOST DI PRODUZIONE",
			`Hai selezionato '${host.alias}'.\nSi tratta di un server critico/prod. Confermi la selezione?`
		);
		if (!ok) {
			ctx.ui.notify("Selezione annullata.", "info");
			return;
		}
	}

	state.selectedHost = alias;
	state.isProd = host.is_prod;
	state.pingStatus = null;
	updateWidget(ctx);

	// ── Passo 2/2 · Sessione tmux ───────────────────────────────────────
	ctx.ui.notify(`Recupero le sessioni tmux su '${alias}'...`, "info");
	const sessRes = await runBridge("sessions", [alias]);
	const activeSessions: string[] =
		sessRes.ok && Array.isArray(sessRes.sessions) ? sessRes.sessions : [];

	const sessionChoices = [
		...activeSessions.map((s) => `${SESSION_PREFIX}${s}`),
		NEW_SESSION,
	];

	let title: string;
	if (activeSessions.length > 0) {
		title = `2/2 · Sessioni tmux su '${alias}' (${activeSessions.length} attive):`;
	} else if (sessRes.ok) {
		title = `2/2 · Nessuna sessione attiva su '${alias}'. Creane una nuova:`;
	} else {
		title = `2/2 · Impossibile elencare le sessioni su '${alias}' (${sessRes.error || "errore"}). Puoi crearne una:`;
	}

	const sessChoice = await ctx.ui.select(title, sessionChoices);
	if (!sessChoice) return;

	let sessionName: string;

	if (sessChoice === NEW_SESSION) {
		const nameInput = await ctx.ui.input(
			`Nome per la nuova sessione tmux su '${alias}':`,
			alias
		);
		if (!nameInput || !nameInput.trim()) {
			ctx.ui.notify("Creazione annullata.", "info");
			return;
		}
		sessionName = nameInput.trim();

		// Crea solo se non esiste già tra quelle attive.
		if (!activeSessions.includes(sessionName)) {
			ctx.ui.notify(`Creazione sessione '${sessionName}' su '${alias}'...`, "info");
			const createRes = await runBridge("create_session", [alias, sessionName]);
			const errText = String(createRes.error || "");
			const alreadyExists = /duplicate|already exists|già esistente/i.test(errText);
			if (!createRes.ok && !alreadyExists) {
				ctx.ui.notify(`Errore creazione sessione: ${errText || "sconosciuto"}`, "error");
				return;
			}
			ctx.ui.notify(`Sessione '${sessionName}' creata con successo!`, "info");
		}
	} else {
		sessionName = sessChoice.slice(SESSION_PREFIX.length).trim();
	}

	state.selectedSession = sessionName;
	state.execMode = "tmux";
	updateWidget(ctx);

	ctx.ui.notify(`Sessione attiva impostata su '${alias}:${sessionName}' (premi F6 per aprire la finestra).`, "info");
	await refreshPaneInfo(ctx);
}

/**
 * Chiude il processo in primo piano nella sessione tmux corrente.
 * Preconfezionato: legge #{pane_current_command}, distingue TUI/processo da shell,
 * poi invia una sequenza di chiusura (default: escalation C-c → C-c → C-d).
 */
async function closeActiveProcess(ctx: ExtensionContext, method = "auto"): Promise<void> {
	const host = state.selectedHost || "localhost";
	const session = state.selectedSession;
	if (!session) {
		ctx.ui.notify("Nessuna sessione selezionata. Usa il menu (Ctrl+H) o /ssh-select per selezionarne una.", "warning");
		return;
	}

	const info = await runBridge("pane_command", [host, session]);
	const cmd = String(info.command || "").trim();
	if (!info.ok || !cmd) {
		ctx.ui.notify(
			`Impossibile leggere il processo in '${session}': ${info.error || cmd || "sconosciuto"}`,
			"error"
		);
		return;
	}

	const base = (cmd.split("/").pop() || cmd).replace(/^-/, "").toLowerCase();
	if (SHELL_COMMANDS.has(base)) {
		ctx.ui.notify(
			`Nella sessione '${session}' è attiva solo la shell ('${cmd}'): niente da chiudere.`,
			"info"
		);
		return;
	}

	const ok = await ctx.ui.confirm(
		state.isProd ? "⚠️ CHIUDI PROCESSO (PRODUZIONE)" : "Chiudi processo attivo",
		`Chiudere '${cmd}' nella sessione '${session}' (${host})?`
	);
	if (!ok) return;

	ctx.ui.notify(`Chiusura di '${cmd}' in corso (metodo: ${method})...`, "info");
	const res = await runBridge("close", [host, session, method]);
	if (res.ok) {
		ctx.ui.notify(res.detail || `Processo '${cmd}' chiuso in '${session}'.`, "info");
	} else {
		ctx.ui.notify(res.detail || res.error || "Chiusura non riuscita.", "error");
	}
	updateWidget(ctx);
}

/**
 * FEATURE 1 · Legge e mostra processo, CWD, PID, titolo e geometria della pane attiva.
 */
async function showPaneInfo(ctx: ExtensionContext): Promise<void> {
	const host = state.selectedHost || "localhost";
	const session = state.selectedSession;
	if (!session) {
		ctx.ui.notify("Nessuna sessione selezionata. Usa F6 per connetterti.", "warning");
		return;
	}
	const info = await runBridge("pane_info", [host, session]);
	if (!info.ok) {
		ctx.ui.notify(`Errore info pane: ${info.error || "sconosciuto"}`, "error");
		return;
	}
	state.paneInfo = {
		command: info.command || "",
		cwd: info.cwd || "",
		pid: info.pid || 0,
		size: info.size || "",
		is_shell: !!info.is_shell,
	};
	updateWidget(ctx);
	const stateLabel = info.is_shell ? "shell (idle)" : "processo attivo";
	await ctx.ui.confirm(
		`Pane Info: ${host} / ${session}`,
		[
			`Processo : ${info.command || "—"}  [${stateLabel}]`,
			`CWD      : ${info.cwd || "—"}`,
			`PID      : ${info.pid ?? "—"}`,
			`Titolo   : ${info.title || "—"}`,
			`Geometria: ${info.size || "—"}`,
		].join("\n") + "\n\n(Invio per chiudere)"
	);
}

/** Aggiorna in background l'anteprima della pane nel widget. */
async function refreshPaneInfo(ctx: ExtensionContext): Promise<void> {
	const host = state.selectedHost || "localhost";
	const session = state.selectedSession;
	if (!session) {
		state.paneInfo = null;
		updateWidget(ctx);
		return;
	}
	const info = await runBridge("pane_info", [host, session]);
	state.paneInfo = info.ok
		? {
				command: info.command || "",
				cwd: info.cwd || "",
				pid: info.pid || 0,
				size: info.size || "",
				is_shell: !!info.is_shell,
			}
		: null;
	updateWidget(ctx);
}

/**
 * FEATURE 2 · Incolla testo, un file (@file:percorso) o un testo scritto nell'editor
 * nella sessione tmux con bracketed paste (indentazione e caratteri speciali intatti).
 */
async function pasteIntoSession(ctx: ExtensionContext, rawArgs = ""): Promise<void> {
	const host = state.selectedHost || "localhost";
	const session = state.selectedSession;
	if (!session) {
		ctx.ui.notify("Nessuna sessione selezionata. Usa F6 per connetterti.", "warning");
		return;
	}

	const arg = (rawArgs || "").trim();
	let text = "";
	let source = "testo";

	if (arg.startsWith("@file:")) {
		const filepath = arg.slice("@file:".length).trim();
		try {
			text = await readFile(filepath, "utf8");
			source = `file ${filepath}`;
		} catch (err: any) {
			ctx.ui.notify(`Impossibile leggere '${filepath}': ${err.message}`, "error");
			return;
		}
	} else if (arg) {
		text = rawArgs;
	} else {
		const edited = await ctx.ui.editor(`Testo da incollare in ${host}:${session}`, "");
		if (edited === undefined || edited === "") {
			ctx.ui.notify("Incolla annullato (nessun testo).", "info");
			return;
		}
		text = edited;
	}

	const ok = await ctx.ui.confirm(
		state.isProd ? "⚠️ INCOLLA (PRODUZIONE)" : "Invia a Tmux (bracketed paste)",
		`Inviare ${text.length} caratteri (${text.split("\n").length} righe) da ${source} a ${host}:${session}?`
	);
	if (!ok) return;

	ctx.ui.notify("Invio con bracketed paste...", "info");
	const res = await runBridge("paste", [host, session, "--bracketed", text]);
	if (res.ok) {
		ctx.ui.notify(`Incollati ${res.chars ?? text.length} caratteri in '${session}'.`, "info");
	} else {
		ctx.ui.notify(`Errore incolla: ${res.error || "sconosciuto"}`, "error");
	}
}

/**
 * FEATURE 4 · Sotto-menu per elencare, attivare e creare finestre nella stessa sessione.
 */
async function showWindowsMenu(ctx: ExtensionContext): Promise<void> {
	const host = state.selectedHost || "localhost";
	const session = state.selectedSession;
	if (!session) {
		ctx.ui.notify("Nessuna sessione selezionata. Usa F6 per connetterti.", "warning");
		return;
	}

	const res = await runBridge("windows", [host, session]);
	if (!res.ok || !Array.isArray(res.windows)) {
		ctx.ui.notify(`Errore elenco finestre: ${res.error || "sconosciuto"}`, "error");
		return;
	}

	const windows: Array<{ index: number; name: string; active: boolean; pane_count: number }> =
		res.windows;
	const NEW_WINDOW = "➕ Nuova finestra nella sessione";
	const choices = [
		...windows.map(
			(w) =>
				`${w.active ? "●" : "○"} ${w.index}: ${w.name}${w.active ? " (attiva)" : ""} — ${w.pane_count} pane`
		),
		NEW_WINDOW,
	];

	const choice = await ctx.ui.select(
		`🪟 Finestre di ${host}:${session} (${windows.length})`,
		choices
	);
	if (!choice) return;

	if (choice === NEW_WINDOW) {
		const name = await ctx.ui.input(`Nome per la nuova finestra in '${session}' (opzionale):`);
		if (name === undefined) return;
		const res2 = await runBridge("new_window", [host, session, (name || "").trim()]);
		if (res2.ok) {
			ctx.ui.notify(res2.detail || "Finestra creata.", "info");
		} else {
			ctx.ui.notify(`Errore creazione finestra: ${res2.error || res2.detail}`, "error");
		}
		return;
	}

	const match = choice.match(/(\d+):/);
	if (!match) return;
	const idx = Number(match[1]);
	const win = windows.find((w) => w.index === idx);
	if (win?.active) {
		ctx.ui.notify(`La finestra ${idx} ('${win.name}') è già attiva.`, "info");
		return;
	}
	const sel = await runBridge("select_window", [host, session, String(idx)]);
	if (sel.ok) {
		ctx.ui.notify(sel.detail || `Finestra ${idx} attivata.`, "info");
		await refreshPaneInfo(ctx);
	} else {
		ctx.ui.notify(`Errore selezione finestra: ${sel.error || sel.detail}`, "error");
	}
}

/**
 * FEATURE 5 · Chiude e rilancia il processo in primo piano (riavvio deterministico).
 * Se la pane è già una shell serve un comando esplicito: /ssh-restart <comando>.
 */
async function restartForeground(ctx: ExtensionContext, rawArgs = ""): Promise<void> {
	const host = state.selectedHost || "localhost";
	const session = state.selectedSession;
	if (!session) {
		ctx.ui.notify("Nessuna sessione selezionata. Usa F6 per connetterti.", "warning");
		return;
	}

	const fallback = (rawArgs || "").trim();
	const info = await runBridge("pane_info", [host, session]);
	if (!info.ok) {
		ctx.ui.notify(`Impossibile analizzare la pane: ${info.error || "sconosciuto"}`, "error");
		return;
	}

	const current = String(info.command || "").trim();
	const isShell = !!info.is_shell;
	if (isShell && !fallback) {
		ctx.ui.notify(
			`Nessun processo attivo in '${session}' (solo shell). Specifica il comando: /ssh-restart <comando>`,
			"warning"
		);
		return;
	}

	const target = fallback || current;
	const ok = await ctx.ui.confirm(
		state.isProd ? "⚠️ RIAVVIA PROCESSO (PRODUZIONE)" : "Riavvia processo in primo piano",
		`Chiudere '${current || "shell"}' e rilanciare '${target}' in '${session}' (${host})?`
	);
	if (!ok) return;

	ctx.ui.notify(`Riavvio di '${target}' in '${session}'...`, "info");
	const args = fallback ? [host, session, fallback] : [host, session];
	const res = await runBridge("restart", args);
	if (res.ok) {
		const method = res.method ? ` [${res.method}]` : "";
		ctx.ui.notify(`Riavviato '${res.restarted || target}' in '${session}'${method}.`, "info");
	} else {
		ctx.ui.notify(`Errore riavvio: ${res.error || "sconosciuto"}`, "error");
	}
	await refreshPaneInfo(ctx);
}

/**
 * FEATURE 3 · Snapshot & diff incrementale dell'output (token saver): mostra solo
 * le righe nuove rispetto al campionamento precedente. reset=true azzera la baseline.
 */
async function showPaneDiff(ctx: ExtensionContext, reset = false): Promise<void> {
	const host = state.selectedHost || "localhost";
	const session = state.selectedSession;
	if (!session) {
		ctx.ui.notify("Nessuna sessione selezionata. Usa F6 per connetterti.", "warning");
		return;
	}

	const args = reset ? [host, session, "200", "--reset"] : [host, session, "200"];
	const res = await runBridge("pane_diff", args);
	if (!res.ok) {
		ctx.ui.notify(`Errore diff: ${res.error || "sconosciuto"}`, "error");
		return;
	}

	if (res.is_first_sample) {
		const preview = (res.new_lines || []).join("\n") || "(vuoto)";
		await ctx.ui.confirm(
			`📊 Baseline pane: ${host}/${session}`,
			`Primo campionamento (${res.total_lines} righe): baseline salvata.\n\nAnteprima ultime righe:\n${preview}\n\n(Invio per chiudere)`
		);
		return;
	}

	if (!res.has_changes) {
		ctx.ui.notify(`Nessuna nuova riga in '${session}' dal campionamento precedente.`, "info");
		return;
	}

	const content = String(res.new_content || "");
	const shown = content.length > 4000 ? `…(troncato)…\n${content.slice(-4000)}` : content;
	await ctx.ui.confirm(
		`📊 Nuove righe (${res.diff_count}) in ${host}/${session}`,
		shown + "\n\n(Invio per chiudere)"
	);
}

export default function bravoricRemoteExtension(pi: ExtensionAPI): void {
	pi.on("session_start", async (_event, ctx) => {
		if (!ctx.hasUI) return;
		await refreshHosts();
		await pingCurrentHost();
		updateWidget(ctx);
		await refreshPaneInfo(ctx);
	});

	// Context injection for LLM when in REMOTE mode
	pi.on("before_agent_start", async (_event, ctx) => {
		// Redraw once per turn. The only other automatic draw is the one in
		// `session_start` above, and pi-emote's interceptor is installed from ITS
		// `session_start`, which runs AFTER this extension's: the loader registers
		// the extensions in ~/.pi/agent/extensions before the packages, and no
		// event precedes `session_start`. A widget drawn before the interceptor
		// exists is never seen by it, so it would stay outside the panel for the
		// whole session. Redrawing here makes the line pass through the
		// interceptor and move inside.
		updateWidget(ctx);
		if (state.mode === "ON" && state.contextLinked) {
			const prodWarning = state.isProd
				? "\n⚠️ ATTENZIONE: Questo è un server di PRODUZIONE. Procedi con massima cautela ed evita comandi distruttivi.\n"
				: "";

			return {
				message: {
					customType: "bravoric-remote-bridge",
					content: `[BRAVORIC REMOTE BRIDGE: ATTIVO]
Host remoto di destinazione: "${state.selectedHost}"
Sessione tmux: "${state.selectedSession || "Pi"}"
Modalità esecuzione: "${state.execMode}"${prodWarning}

ISTRUZIONI OPERATIVE PER L'AGENTE:
Sei collegato come operatore remoto all'host '${state.selectedHost}' sulla sessione tmux '${state.selectedSession || "Pi"}'.
Quando l'utente richiede verifiche, modifiche o esecuzione comandi su questo server:
1. Utilizza gli strumenti MCP di bravoric-ssh:
   - Per interagire con la sessione tmux dell'host: 'bravoric-ssh_send_keys' (con testo) e 'bravoric-ssh_send_enter', oppure 'bravoric-ssh_run_and_wait'
   - Per verificare lo stato della finestra/pane tmux: 'bravoric-ssh_capture_pane' (host='${state.selectedHost}', session='${state.selectedSession || "Pi"}')
   - Per sapere in che repo/cartella si trova la sessione (processo, CWD, PID, geometria): 'bravoric-ssh_pane_info'
   - Per incollare file o testo multiriga senza corrompere l'indentazione: 'bravoric-ssh_paste' (usa bracketed paste)
   - Per monitorare output lunghi/TUI risparmiando token (solo le righe nuove): 'bravoric-ssh_pane_diff' (usa reset=true per ripartire dalla baseline)
   - Per gestire più finestre della stessa sessione: 'bravoric-ssh_list_windows_parsed' e 'bravoric-ssh_select_window'
   - Per ripristinare un processo/agente bloccato: 'bravoric-ssh_close_foreground' e 'bravoric-ssh_restart_foreground' (MAI 'tmux kill-server')
   - Per comandi batch diretti: 'bravoric-ssh_run_command'
2. Tutte le azioni richieste devono essere eseguite su '${state.selectedHost}' (sessione '${state.selectedSession}') e non sulla macchina locale.
3. Riferisci chiaramente l'esito dei comandi all'utente.`,
					display: true,
				},
			};
		}
	});

	// Main Interactive Menu Handler
	async function openMainMenu(ctx: ExtensionContext): Promise<void> {
		if (cachedHosts.length === 0) {
			await refreshHosts();
		}

		const statusText = state.pingStatus?.ok
			? "Online"
			: state.pingStatus
				? "Offline"
				: "Check...";
		const prodTag = state.isProd ? " ⚠️[PROD]" : "";
		const currentTarget = `${state.selectedHost}:${state.selectedSession || "nessuna"}`;

		const menuOptions = [
			`🖥️ Apri Finestra Terminale Dedicata [${currentTarget}]   [F6]`,
			`🌐 Seleziona Host / Sessione tmux (Host → Sessione)`,
			`⚡ Toggle Modalità [Attuale: ${state.mode === "ON" ? "🔴 REMOTE (Agente Dentro)" : "⚪ LOCALE (Pianifica Fuori)"}]`,
			`🔗 Toggle Contesto LLM [Attuale: ${state.contextLinked ? "LINKED (Attivo nel prompt)" : "UNLINKED (Fuori contesto)"}]`,
			`👁️ Cattura Output Rapido (Dialog)`,
			`⌨️ Invia Comando Rapido`,
			`⏹️ Chiudi processo attivo nella sessione [${state.selectedSession || "—"}]`,
			`🔁 Riavvia processo in primo piano (chiudi + rilancio)`,
			`📋 Info Pane (processo, CWD, PID, geometria)`,
			`📎 Incolla testo/file nella sessione (bracketed paste)`,
			`🪟 Finestre della sessione (attiva / nuova)`,
			`📊 Monitor output (snapshot & diff — solo righe nuove)`,
			`🔄 Riconnetti / Test Connessione Ping [${state.selectedHost}${prodTag} — ${statusText}]`,
			`❌ Chiudi`,
		];

		const choice = await ctx.ui.select("Menu Bravoric-SSH Remote Bridge:", menuOptions);
		if (!choice || choice.startsWith("❌")) return;

		if (choice.startsWith("🖥️")) {
			openAttachedWindow(ctx);
		} else if (choice.startsWith("🌐")) {
			await selectSessionFlow(ctx);
		} else if (choice.startsWith("⚡")) {
			// Toggle Remote Mode
			if (state.mode === "OFF") {
				if (state.isProd) {
					const confirm = await ctx.ui.confirm(
						"⚠️ ATTIVAZIONE MODALITÀ REMOTA SU PRODUZIONE",
						`Stai per attivare il controllo diretto di Pi Agent su '${state.selectedHost}' (PRODUZIONE). Procedere?`
					);
					if (!confirm) return;
				}
				state.mode = "ON";
				ctx.ui.notify(
					`Modalità REMOTA attivata su '${state.selectedHost}' (sessione: ${state.selectedSession}).`,
					"info"
				);
			} else {
				state.mode = "OFF";
				ctx.ui.notify("Modalità LOCALE attivata. Pi Agent torna in pianificazione locale.", "info");
			}
			updateWidget(ctx);
		} else if (choice.startsWith("🔗")) {
			// Toggle Context Linked
			state.contextLinked = !state.contextLinked;
			ctx.ui.notify(
				state.contextLinked
					? "Contesto LLM COLLEGATO: l'agente riceverà istruzioni dell'host remoto."
					: "Contesto LLM SCOLLEGATO: le operazioni non consumeranno token del prompt.",
				"info"
			);
			updateWidget(ctx);
		} else if (choice.startsWith("👁️")) {
			// Quick capture dialog
			if (state.execMode !== "tmux" || !state.selectedSession) {
				ctx.ui.notify("Nessuna sessione tmux selezionata.", "warning");
				return;
			}
			const cap = await runBridge("capture", [state.selectedHost, state.selectedSession, "35"]);
			if (cap.ok) {
				state.lastOutputPreview = (cap.output || "").trim().split("\n").pop() || "";
				updateWidget(ctx);
				const lines = (cap.output || "(Nessun output registrato)").split("\n");
				await ctx.ui.confirm(
					`Tmux Pane: ${state.selectedHost} / ${state.selectedSession}`,
					lines.slice(-20).join("\n") + "\n\n(Invio per chiudere)"
				);
			} else {
				ctx.ui.notify(`Errore cattura: ${cap.error || "fallita"}`, "error");
			}
		} else if (choice.startsWith("⌨️")) {
			// Send quick command
			const cmd = await ctx.ui.input(`Comando rapido per ${state.selectedHost}:`);
			if (cmd && cmd.trim()) {
				if (state.isProd) {
					const ok = await ctx.ui.confirm(
						"⚠️ CONFERMA PRODUZIONE",
						`Eseguire '${cmd}' su ${state.selectedHost}?`
					);
					if (!ok) return;
				}

				if (state.execMode === "tmux") {
					ctx.ui.notify(`Invio comando alla sessione '${state.selectedSession}'...`, "info");
					const res = await runBridge("send", [state.selectedHost, state.selectedSession, cmd]);
					if (res.ok) {
						ctx.ui.notify("Comando inviato alla sessione tmux!", "info");
						state.lastOutputPreview = `Comando inviato: ${cmd}`;
						updateWidget(ctx);
					} else {
						ctx.ui.notify(`Errore invio comando: ${res.error}`, "error");
					}
				} else {
					ctx.ui.notify(`Esecuzione diretta di '${cmd}' su ${state.selectedHost}...`, "info");
					const res = await runBridge("run", [state.selectedHost, cmd]);
					if (res.ok) {
						state.lastOutputPreview = JSON.stringify(res.result).substring(0, 100);
						updateWidget(ctx);
						ctx.ui.notify("Comando eseguito con successo!", "info");
					} else {
						ctx.ui.notify(`Errore esecuzione: ${res.error}`, "error");
					}
				}
			}
		} else if (choice.startsWith("⏹️")) {
			await closeActiveProcess(ctx);
		} else if (choice.startsWith("🔁")) {
			await restartForeground(ctx);
		} else if (choice.startsWith("📋")) {
			await showPaneInfo(ctx);
		} else if (choice.startsWith("📎")) {
			await pasteIntoSession(ctx);
		} else if (choice.startsWith("🪟")) {
			await showWindowsMenu(ctx);
		} else if (choice.startsWith("📊")) {
			await showPaneDiff(ctx);
		} else if (choice.startsWith("🔄")) {
			// Ping / Reconnect
			ctx.ui.notify(`Verifica connessione per '${state.selectedHost}'...`, "info");
			const p = await pingCurrentHost();
			updateWidget(ctx);
			ctx.ui.notify(`Esito ping: ${p.detail}`, p.ok ? "info" : "error");
		}
	}

	// Register Commands
	pi.registerCommand("ssh", {
		description: "Apri il menu di controllo Bravoric-SSH Remote Bridge",
		handler: async (_args, ctx) => {
			await openMainMenu(ctx);
		},
	});

	pi.registerCommand("ssh-window", {
		description: "Apri la finestra terminale per la sessione selezionata (F6)",
		handler: async (_args, ctx) => {
			openAttachedWindow(ctx);
		},
	});

	pi.registerCommand("ssh-select", {
		description: "Seleziona Host e Sessione tmux attiva (Host → Sessione)",
		handler: async (_args, ctx) => {
			await selectSessionFlow(ctx);
		},
	});

	pi.registerCommand("ssh-toggle", {
		description: "Attiva / disattiva rapidamente la modalità remota",
		handler: async (_args, ctx) => {
			if (state.mode === "OFF") {
				if (state.isProd) {
					const ok = await ctx.ui.confirm(
						"⚠️ PRODUZIONE",
						`Attivare modalità remota su ${state.selectedHost}?`
					);
					if (!ok) return;
				}
				state.mode = "ON";
				ctx.ui.notify(
					`Modalità REMOTA attivata su '${state.selectedHost}' (sessione: ${state.selectedSession}).`,
					"info"
				);
			} else {
				state.mode = "OFF";
				ctx.ui.notify("Modalità LOCALE ripristinata.", "info");
			}
			updateWidget(ctx);
		},
	});

	pi.registerCommand("ssh-close", {
		description:
			"Chiudi il processo in primo piano nella sessione tmux corrente (metodo opzionale: auto|sigint|sigint2|eof|exit)",
		handler: async (args, ctx) => {
			const method = (args || "").trim() || "auto";
			await closeActiveProcess(ctx, method);
		},
	});

	pi.registerCommand("ssh-paste", {
		description:
			"Incolla testo/file nella sessione tmux con bracketed paste: /ssh-paste <testo> | @file:percorso (senza argomenti apre l'editor)",
		handler: async (args, ctx) => {
			await pasteIntoSession(ctx, args || "");
		},
	});

	pi.registerCommand("ssh-info", {
		description: "Mostra processo, CWD, PID, titolo e geometria della pane attiva",
		handler: async (_args, ctx) => {
			await showPaneInfo(ctx);
		},
	});

	pi.registerCommand("ssh-windows", {
		description: "Elenca/attiva/crea le finestre della sessione tmux corrente",
		handler: async (_args, ctx) => {
			await showWindowsMenu(ctx);
		},
	});

	pi.registerCommand("ssh-restart", {
		description:
			"Chiude e rilancia il processo in primo piano: /ssh-restart [comando] (il comando è obbligatorio se la pane è già una shell)",
		handler: async (args, ctx) => {
			await restartForeground(ctx, args || "");
		},
	});

	pi.registerCommand("ssh-diff", {
		description:
			"Snapshot & diff incrementale dell'output pane (solo righe nuove). Aggiungi 'reset' per azzerare la baseline",
		handler: async (args, ctx) => {
			const reset = (args || "").trim().toLowerCase() === "reset";
			await showPaneDiff(ctx, reset);
		},
	});

	pi.registerCommand("ssh-host", {
		description: "Cambia host attivo: /ssh-host <alias>",
		handler: async (args, ctx) => {
			if (!args || !args.trim()) {
				ctx.ui.notify("Specifica un alias: /ssh-host <alias>", "warning");
				return;
			}
			const target = args.trim();
			if (cachedHosts.length === 0) await refreshHosts();
			const h = cachedHosts.find((x) => x.alias === target);
			if (!h) {
				ctx.ui.notify(`Host '${target}' non trovato nella configurazione.`, "error");
				return;
			}
			if (h.is_prod) {
				const ok = await ctx.ui.confirm("⚠️ PRODUZIONE", `Confermi il passaggio all'host ${target}?`);
				if (!ok) return;
			}
			state.selectedHost = target;
			state.isProd = h.is_prod;
			ctx.ui.notify(`Host impostato su '${target}'.`, "info");
			updateWidget(ctx);
			await pingCurrentHost();
			updateWidget(ctx);
		},
	});

	// Register Keyboard Shortcut: Ctrl+H for Main Menu
	pi.registerShortcut("ctrl+h", {
		description: "Apri menu Bravoric-SSH",
		handler: async (ctx) => {
			await openMainMenu(ctx);
		},
	});

	// Register Keyboard Shortcut: F6 for Dedicated Terminal Window
	pi.registerShortcut("f6", {
		description: "Apri finestra terminale per la sessione selezionata",
		handler: async (ctx) => {
			openAttachedWindow(ctx);
		},
	});
}
