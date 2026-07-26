// Electron shell for Mono IDE.
//
// Boot order (exactly what the project asks for):
//   1. launcher window  -> user picks a project folder with a native dialog
//   2. python backend   -> spawned as a child process on a free port
//   3. bundled notion2api -> started by that backend (ide/supervisor.py)
//   4. editor UI        -> loaded into this window once /api/state answers
//
// No terminal, no "open this url in a browser" step, no cli argument.

const { app, BrowserWindow, dialog, ipcMain, shell, Menu } = require("electron")
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

function waitForBackend(port, timeoutMs = 120000) {
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

function stopBackend() {
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
		const upstream = await apiGet("/api/upstream/status")
		if (upstream && upstream.state === "failed") {
			log("notion2api: " + (upstream.error || "failed to start"))
		}
		return {
			ok: true,
			url: `http://127.0.0.1:${backendPort}/`,
			upstream: upstream || null,
		}
	} catch (error) {
		stopBackend()
		return { ok: false, error: String(error.message || error), log: backendLog.slice(-12) }
	}
})

ipcMain.handle("enter-ide", async () => {
	if (!backendPort) return false
	await win.loadURL(`http://127.0.0.1:${backendPort}/`)
	return true
})

ipcMain.handle("upstream-status", () => apiGet("/api/upstream/status"))
ipcMain.handle("backend-log", () => backendLog.slice(-80))

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
	app.whenReady().then(createWindow)
	app.on("activate", () => {
		if (BrowserWindow.getAllWindows().length === 0) createWindow()
	})
	app.on("window-all-closed", () => {
		stopBackend()
		if (process.platform !== "darwin") app.quit()
	})
	app.on("before-quit", stopBackend)
	process.on("exit", stopBackend)
}
