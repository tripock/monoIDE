// Electron shell for Mono IDE.
//
// Boot order (exactly what the project asks for):
//   1. launcher window  -> user picks a project folder with a native dialog
//   2. python backend   -> spawned as a child process on a free port
//   3. bundled notion2api -> started by that backend (ide/supervisor.py)
//   4. editor UI        -> loaded into this window once /api/state answers
//
// No terminal, no "open this url in a browser" step, no cli argument.

const { app, BrowserWindow, clipboard, dialog, ipcMain, shell, Menu } = require("electron")
const { spawn, spawnSync } = require("child_process")
const fs = require("fs")
const http = require("http")
const net = require("net")
const os = require("os")
const path = require("path")

const PROJECT_ROOT = path.resolve(__dirname, "..")
const RECENTS_FILE = path.join(app.getPath("userData"), "recent-folders.json")

let win = null
let backend = null
let backendPort = 0
let backendLog = []
let prewarm = null
let prewarmState = { state: "pending", phase: "" }
let bootRequest = null

// ---------------------------------------------------------------------------
// helpers
// ---------------------------------------------------------------------------

function log(line) {
	const text = String(line).replace(/\s+$/, "")
	if (!text) return
	backendLog.push(text)
	if (backendLog.length > 400) backendLog.shift()
	if (win && !win.isDestroyed()) win.webContents.send("backend-log", text)
	console.log("[backend]", text)
}

function freePort() {
	return new Promise((resolve, reject) => {
		const probe = net.createServer()
		probe.unref()
		probe.on("error", reject)
		probe.listen(0, "127.0.0.1", () => {
			const { port } = probe.address()
			probe.close(() => resolve(port))
		})
	})
}

function readRecents() {
	try {
		const rows = JSON.parse(fs.readFileSync(RECENTS_FILE, "utf8"))
		return Array.isArray(rows) ? rows.filter((p) => fs.existsSync(p)) : []
	} catch {
		return []
	}
}

function rememberRecent(folder) {
	const rows = [folder, ...readRecents().filter((p) => p !== folder)].slice(0, 8)
	try {
		fs.mkdirSync(path.dirname(RECENTS_FILE), { recursive: true })
		fs.writeFileSync(RECENTS_FILE, JSON.stringify(rows, null, 2))
	} catch {
		/* non fatal */
	}
	return rows
}

// The backend is either the frozen exe (packaged app) or `python main.py`.
function resolveBackend() {
	const exeName = process.platform === "win32" ? "monoide.exe" : "monoide"
	const frozen = [
		// electron-builder layout (extraResources)
		path.join(process.resourcesPath || "", "backend", exeName),
		// @electron/packager layout (--extra-resource)
		path.join(process.resourcesPath || "", exeName),
		path.join(PROJECT_ROOT, "dist", exeName),
	]
	for (const candidate of frozen) {
		if (candidate && fs.existsSync(candidate)) {
			return { command: candidate, args: [] }
		}
	}

	const mainPy = path.join(PROJECT_ROOT, "main.py")
	if (!fs.existsSync(mainPy)) {
		throw new Error("neither dist/" + exeName + " nor main.py was found next to the app")
	}
	const names =
		process.platform === "win32" ? ["python", "python3", "py"] : ["python3", "python"]
	for (const name of names) {
		const probe = spawnSync(name, name === "py" ? ["-3", "--version"] : ["--version"], {
			encoding: "utf8",
		})
		if (!probe.error && probe.status === 0) {
			const pre = name === "py" ? ["-3"] : []
			return { command: name, args: [...pre, mainPy] }
		}
	}
	throw new Error("Python 3.9+ was not found in PATH - install it from python.org")
}

// Install the python dependencies while the user is still choosing a folder.
// Failures here are not fatal: the backend runs the same check again and is the
// one that produces the diagnostic report.
function startPrewarm() {
	let command
	let args
	try {
		;({ command, args } = resolveBackend())
	} catch (error) {
		prewarmState = { state: "failed", phase: String(error.message || error) }
		return
	}
	prewarmState = { state: "running", phase: "checking python dependencies" }
	try {
		prewarm = spawn(command, [...args, "--prewarm"], {
			cwd: PROJECT_ROOT,
			env: { ...process.env, PYTHONUNBUFFERED: "1", PYTHONIOENCODING: "utf-8" },
			windowsHide: true,
		})
	} catch (error) {
		prewarmState = { state: "failed", phase: String(error.message || error) }
		return
	}
	let pending = ""
	let spoke = false // did it ever answer in our JSON protocol?
	prewarm.stdout.on("data", (chunk) => {
		pending += String(chunk)
		const rows = pending.split(/\r?\n/)
		pending = rows.pop() || ""
		for (const row of rows) {
			if (!row.trim()) continue
			let event
			try {
				event = JSON.parse(row)
			} catch {
				continue // a stray print from the interpreter, not our protocol
			}
			spoke = true
			if (event.phase) prewarmState.phase = event.phase
			if (event.state) prewarmState.state = event.state
			send("prewarm-progress", { ...event, ...prewarmState })
		}
	})
	prewarm.stderr.on("data", (chunk) => console.log("[prewarm]", String(chunk).trim()))
	prewarm.on("exit", (code) => {
		if (prewarmState.state === "running") {
			if (code === 0) {
				prewarmState = { state: "ready", phase: "dependencies ready" }
			} else if (!spoke) {
				// An older dist/monoide.exe does not know --prewarm. Not a problem:
				// the backend installs the dependencies itself and reports properly.
				prewarmState = { state: "skipped", phase: "installed with the backend" }
			} else {
				prewarmState = { state: "failed", phase: `prewarm exited with code ${code}` }
			}
		}
		prewarm = null
		send("prewarm-progress", { t: "deps", ...prewarmState })
	})
}

function send(channel, payload) {
	if (win && !win.isDestroyed()) win.webContents.send(channel, payload)
}

// Relay GET /api/boot/stream to the renderer. Done here rather than with an
// EventSource in the page so launcher.html keeps its `default-src 'none'` CSP.
function subscribeBoot(port) {
	if (bootRequest) {
		bootRequest.destroy()
		bootRequest = null
	}
	let buffer = ""
	const request = http.get(
		{ host: "127.0.0.1", port, path: "/api/boot/stream" },
		(response) => {
			response.setEncoding("utf8")
			response.on("data", (chunk) => {
				buffer += chunk
				let split
				while ((split = buffer.indexOf("\n\n")) !== -1) {
					const frame = buffer.slice(0, split)
					buffer = buffer.slice(split + 2)
					let type = "message"
					const data = []
					for (const line of frame.split("\n")) {
						if (line.startsWith("event: ")) type = line.slice(7).trim()
						else if (line.startsWith("data: ")) data.push(line.slice(6))
					}
					if (!data.length) continue
					try {
						send("boot-event", { type, data: JSON.parse(data.join("\n")) })
					} catch {
						/* ignore a malformed frame */
					}
				}
			})
			response.on("end", () => {
				bootRequest = null
			})
		},
	)
	request.on("error", () => {
		bootRequest = null
		// Fall back to a single poll so the launcher is never left with nothing.
		apiGet("/api/boot").then((state) => {
			if (state) send("boot-event", { type: "snapshot", data: state })
		})
	})
	bootRequest = request
}

function waitForBackend(port, timeoutMs = 45000) {
	const deadline = Date.now() + timeoutMs
	return new Promise((resolve, reject) => {
		const attempt = () => {
			if (backend && backend.exitCode !== null) {
				return reject(new Error("backend exited: " + backendLog.slice(-6).join(" | ")))
			}
			const request = http.get(
				{ host: "127.0.0.1", port, path: "/api/state", timeout: 2000 },
				(response) => {
					response.resume()
					if (response.statusCode && response.statusCode < 500) return resolve(true)
					retry()
				},
			)
			request.on("timeout", () => request.destroy())
			request.on("error", retry)
		}
		const retry = () => {
			if (Date.now() > deadline) return reject(new Error("backend did not answer in time"))
			setTimeout(attempt, 400)
		}
		attempt()
	})
}

function apiGet(pathname) {
	return new Promise((resolve) => {
		if (!backendPort) return resolve(null)
		const request = http.get(
			{ host: "127.0.0.1", port: backendPort, path: pathname, timeout: 4000 },
			(response) => {
				let raw = ""
				response.on("data", (chunk) => (raw += chunk))
				response.on("end", () => {
					try {
						resolve(JSON.parse(raw))
					} catch {
						resolve(null)
					}
				})
			},
		)
		request.on("timeout", () => request.destroy())
		request.on("error", () => resolve(null))
	})
}

function apiPost(pathname, body = {}) {
	return new Promise((resolve) => {
		if (!backendPort) return resolve(null)
		const payload = Buffer.from(JSON.stringify(body), "utf8")
		const request = http.request(
			{
				host: "127.0.0.1",
				port: backendPort,
				path: pathname,
				method: "POST",
				timeout: 10000,
				headers: { "Content-Type": "application/json", "Content-Length": payload.length },
			},
			(response) => {
				let raw = ""
				response.on("data", (chunk) => (raw += chunk))
				response.on("end", () => {
					try {
						resolve(JSON.parse(raw))
					} catch {
						resolve(null)
					}
				})
			},
		)
		request.on("timeout", () => request.destroy())
		request.on("error", () => resolve(null))
		request.end(payload)
	})
}

function stopBackend() {
	if (bootRequest) {
		bootRequest.destroy()
		bootRequest = null
	}
	if (!backend || backend.exitCode !== null) return
	try {
		if (process.platform === "win32") {
			spawnSync("taskkill", ["/pid", String(backend.pid), "/t", "/f"])
		} else {
			backend.kill("SIGTERM")
		}
	} catch {
		/* ignore */
	}
	backend = null
}

function stopAll() {
	stopBackend()
	if (prewarm && prewarm.exitCode === null) {
		try {
			if (process.platform === "win32") {
				spawnSync("taskkill", ["/pid", String(prewarm.pid), "/t", "/f"])
			} else {
				prewarm.kill("SIGTERM")
			}
		} catch {
			/* ignore */
		}
		prewarm = null
	}
}

// ---------------------------------------------------------------------------
// window
// ---------------------------------------------------------------------------

function createWindow() {
	win = new BrowserWindow({
		width: 1440,
		height: 900,
		minWidth: 900,
		minHeight: 620,
		backgroundColor: "#0f0f0f",
		autoHideMenuBar: true,
		title: "Mono IDE",
		webPreferences: {
			preload: path.join(__dirname, "preload.js"),
			contextIsolation: true,
			nodeIntegration: false,
			spellcheck: false,
		},
	})
	Menu.setApplicationMenu(
		Menu.buildFromTemplate([
			{
				label: "File",
				submenu: [
					{
						label: "Open folder…",
						accelerator: "CmdOrCtrl+O",
						click: () => {
							stopBackend()
							backendPort = 0
							win.loadFile(path.join(__dirname, "launcher.html"))
						},
					},
					{ role: "quit" },
				],
			},
			{ role: "editMenu" },
			{
				label: "View",
				submenu: [
					{ role: "reload" },
					{ role: "toggleDevTools" },
					{ role: "resetZoom" },
					{ role: "zoomIn" },
					{ role: "zoomOut" },
					{ role: "togglefullscreen" },
				],
			},
		]),
	)
	win.webContents.setWindowOpenHandler(({ url }) => {
		shell.openExternal(url)
		return { action: "deny" }
	})
	win.loadFile(path.join(__dirname, "launcher.html"))
}

// ---------------------------------------------------------------------------
// ipc
// ---------------------------------------------------------------------------

ipcMain.handle("pick-folder", async () => {
	const result = await dialog.showOpenDialog(win, {
		title: "Choose a project folder",
		properties: ["openDirectory", "createDirectory"],
		defaultPath: readRecents()[0] || os.homedir(),
	})
	if (result.canceled || !result.filePaths.length) return null
	return result.filePaths[0]
})

ipcMain.handle("recent-folders", () => readRecents())

ipcMain.handle("open-project", async (_event, folder) => {
	if (!folder || !fs.existsSync(folder)) {
		return { ok: false, error: "that folder does not exist any more" }
	}
	stopBackend()
	backendLog = []
	try {
		const { command, args } = resolveBackend()
		backendPort = await freePort()
		log(`launching ${path.basename(command)} on port ${backendPort}`)
		backend = spawn(
			command,
			[...args, folder, "--host", "127.0.0.1", "--port", String(backendPort)],
			{
				cwd: folder,
				env: { ...process.env, PYTHONUNBUFFERED: "1", PYTHONIOENCODING: "utf-8", MONOIDE_SHELL: "electron" },
				windowsHide: true,
			},
		)
		backend.stdout.on("data", (chunk) => String(chunk).split(/\r?\n/).forEach(log))
		backend.stderr.on("data", (chunk) => String(chunk).split(/\r?\n/).forEach(log))
		backend.on("exit", (code) => log(`backend exited with code ${code}`))

		await waitForBackend(backendPort)
		rememberRecent(folder)
		// The rest of the boot (dependencies, notion2api) is watched over the
		// event stream; the launcher decides whether the editor may open.
		subscribeBoot(backendPort)
		return {
			ok: true,
			url: `http://127.0.0.1:${backendPort}/`,
			boot: await apiGet("/api/boot"),
		}
	} catch (error) {
		stopBackend()
		return { ok: false, error: String(error.message || error), log: backendLog.slice(-12) }
	}
})

ipcMain.handle("enter-ide", async () => {
	if (!backendPort) return { ok: false, error: "the backend is not running" }
	// Second line of defence: the renderer already refuses to call this while the
	// boot is blocked, but File -> Open folder can drive the window too.
	const state = await apiGet("/api/boot")
	if (state && state.blocked) {
		return { ok: false, blocked: state.failed || [], boot: state }
	}
	await win.loadURL(`http://127.0.0.1:${backendPort}/`)
	return { ok: true }
})

ipcMain.handle("upstream-status", () => apiGet("/api/upstream/status"))
ipcMain.handle("backend-log", () => backendLog.slice(-80))
ipcMain.handle("boot-state", () => apiGet("/api/boot"))
ipcMain.handle("boot-report", () => apiGet("/api/boot/report"))
ipcMain.handle("boot-retry", async () => {
	const state = await apiPost("/api/boot/retry", {})
	if (state) subscribeBoot(backendPort)
	return state
})
ipcMain.handle("prewarm-state", () => prewarmState)
ipcMain.handle("copy-text", (_event, text) => {
	clipboard.writeText(String(text || ""))
	return true
})
ipcMain.handle("open-log-file", async (_event, target) => {
	const stateDir =
		process.platform === "win32"
			? path.join(process.env.LOCALAPPDATA || app.getPath("userData"), "monoide")
			: path.join(process.env.XDG_STATE_HOME || path.join(os.homedir(), ".local", "state"), "monoide")
	const candidate = target || path.join(stateDir, "logs", "boot-last.log")
	if (!fs.existsSync(candidate)) return false
	shell.showItemInFolder(candidate)
	return true
})

// ---------------------------------------------------------------------------
// lifecycle
// ---------------------------------------------------------------------------

if (!app.requestSingleInstanceLock()) {
	app.quit()
} else {
	app.on("second-instance", () => {
		if (win) {
			if (win.isMinimized()) win.restore()
			win.focus()
		}
	})
	app.whenReady().then(() => {
		createWindow()
		startPrewarm()
	})
	app.on("activate", () => {
		if (BrowserWindow.getAllWindows().length === 0) createWindow()
	})
	app.on("window-all-closed", () => {
		stopAll()
		if (process.platform !== "darwin") app.quit()
	})
	app.on("before-quit", stopAll)
	process.on("exit", stopAll)
}
