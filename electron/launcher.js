// Launcher screen: pick a folder, watch the stack come up, and swap the window
// over to the editor served by the python backend - but only when every required
// component actually started. A failed boot shows the diagnosis instead.

const pickButton = document.getElementById("pick")
const recentsBox = document.getElementById("recents-box")
const recentsList = document.getElementById("recents")
const bootBox = document.getElementById("boot")
const stepsBox = document.getElementById("steps")
const logBox = document.getElementById("log")
const errorBox = document.getElementById("error")
const failureBox = document.getElementById("failure")
const failureSummary = document.getElementById("failure-summary")
const failureList = document.getElementById("failure-list")
const failurePath = document.getElementById("failure-path")
const reportBox = document.getElementById("report")

// Local pseudo-steps: the backend cannot report on the two stages that happen
// before it exists. They share the shape of a real component so one renderer
// handles the whole list.
const local = {
	folder: { key: "folder", label: "project folder", state: "pending", required: true, detail: "" },
	backend: { key: "backend", label: "python backend", state: "pending", required: true, detail: "" },
	deps: { key: "deps", label: "python dependencies", state: "pending", required: true, detail: "" },
}
let remote = []
let currentFolder = ""
// The boot state arrives over several channels (snapshot, done, end, the
// open-project reply); the verdict must only be acted on once.
let settled = false

// ---------------------------------------------------------------------------
// rendering
// ---------------------------------------------------------------------------

const GLYPH_CLASS = {
	pending: "",
	active: "active",
	ok: "done",
	failed: "failed",
	skipped: "skipped",
}

function rows() {
	// The backend reports "deps" too; once it does, its version wins over the
	// prewarm placeholder.
	const seen = new Set(remote.map((component) => component.key))
	const head = [local.folder, local.backend]
	if (!seen.has("deps")) head.push(local.deps)
	return [...head, ...remote]
}

function renderSteps() {
	const wanted = rows()
	const existing = new Map()
	for (const node of stepsBox.querySelectorAll(".step")) existing.set(node.dataset.step, node)

	for (const component of wanted) {
		let node = existing.get(component.key)
		if (!node) {
			node = document.createElement("div")
			node.className = "step"
			node.dataset.step = component.key
			node.append(document.createElement("span"), document.createElement("em"))
			stepsBox.append(node)
		}
		existing.delete(component.key)
		const state = component.state === "failed" && !component.required ? "warn" : component.state
		node.className = "step " + (state === "warn" ? "warn" : GLYPH_CLASS[state] || "")
		if (!component.required) node.classList.add("advisory")
		node.firstChild.textContent = component.label
		node.lastChild.textContent = component.error || component.detail || ""
	}
	for (const stale of existing.values()) stale.remove()
}

function mark(key, state, detail) {
	if (!local[key]) return
	local[key].state = state
	if (detail !== undefined) local[key].detail = detail
	renderSteps()
}

// The dependency install can print a couple of thousand lines. Rebuilding the
// whole string per line would freeze the window, so keep a bounded buffer and
// flush it once per frame.
const LOG_KEEP = 400
let logLines = []
let logPending = false

function flushLog() {
	logPending = false
	logBox.textContent = logLines.join("\n")
	logBox.scrollTop = logBox.scrollHeight
}

function appendLog(line) {
	if (!line) return
	logLines.push(String(line))
	if (logLines.length > LOG_KEEP) logLines = logLines.slice(-LOG_KEEP)
	if (!logPending) {
		logPending = true
		requestAnimationFrame(flushLog)
	}
}

function fail(message) {
	errorBox.hidden = false
	errorBox.textContent = message
	pickButton.disabled = false
	pickButton.hidden = false
	pickButton.textContent = "Choose project folder..."
	renderRecents()
}

async function renderRecents() {
	const folders = await window.monoide.recentFolders()
	recentsList.innerHTML = ""
	recentsBox.hidden = folders.length === 0
	for (const folder of folders) {
		const parts = folder.replace(/[\\/]+$/, "").split(/[\\/]/)
		const name = parts.pop()
		const item = document.createElement("li")
		const button = document.createElement("button")
		const strong = document.createElement("strong")
		strong.textContent = name
		button.append(strong, document.createTextNode("  " + folder))
		button.addEventListener("click", () => boot(folder))
		item.append(button)
		recentsList.append(item)
	}
}

// ---------------------------------------------------------------------------
// the gate
// ---------------------------------------------------------------------------

async function showFailure(state) {
	bootBox.hidden = true
	failureBox.hidden = false
	pickButton.hidden = true

	const failed = (state.components || []).filter(
		(component) => component.required && component.state === "failed",
	)
	failureSummary.textContent =
		failed.length === 1
			? `${failed[0].label} did not start, so the editor was not opened.`
			: `${failed.length} required components did not start, so the editor was not opened.`

	failureList.innerHTML = ""
	for (const component of failed) {
		const item = document.createElement("li")
		const title = document.createElement("strong")
		title.textContent = component.label
		const error = document.createElement("div")
		error.textContent = component.error || "failed"
		item.append(title, error)
		if (component.hint) {
			const hint = document.createElement("div")
			hint.className = "hint"
			hint.textContent = component.hint
			item.append(hint)
		}
		failureList.append(item)
	}

	const report = await window.monoide.bootReport()
	reportBox.textContent = (report && report.text) || "the diagnostic report is not available"
	const path = (report && report.path) || state.report_path || ""
	failurePath.textContent = path ? "full log: " + path : ""
	document.getElementById("open-log").dataset.path = path
}

function applyState(state) {
	if (!state) {
		// A backend older than the health gate has no /api/boot. Rather than wait
		// for a verdict that will never arrive, behave the way it used to.
		if (settled) return
		settled = true
		appendLog("this backend does not report startup health - opening the editor")
		window.monoide.enterIde()
		return
	}
	remote = state.components || []
	renderSteps()
	if (!state.finished || settled) return
	settled = true

	if (state.blocked) {
		showFailure(state)
		return
	}
	mark("backend", "ok")
	appendLog("opening the editor")
	window.monoide.enterIde().then((result) => {
		if (result && result.ok === false && result.boot) {
			settled = true
			showFailure(result.boot)
		}
	})
}

// ---------------------------------------------------------------------------
// boot
// ---------------------------------------------------------------------------

async function boot(folder) {
	currentFolder = folder
	settled = false
	errorBox.hidden = true
	failureBox.hidden = true
	recentsBox.hidden = true
	bootBox.hidden = false
	logLines = []
	flushLog()
	pickButton.disabled = true
	pickButton.textContent = "starting..."

	mark("folder", "ok", folder)
	appendLog("project: " + folder)
	mark("backend", "active")

	const result = await window.monoide.openProject(folder)
	if (!result || !result.ok) {
		mark("backend", "failed", (result && result.error) || "")
		for (const line of (result && result.log) || []) appendLog(line)
		return fail((result && result.error) || "could not start the backend")
	}
	mark("backend", "ok")
	applyState(result.boot)
}

// ---------------------------------------------------------------------------
// wiring
// ---------------------------------------------------------------------------

pickButton.addEventListener("click", async () => {
	const folder = await window.monoide.pickFolder()
	if (folder) boot(folder)
})

document.getElementById("retry").addEventListener("click", async () => {
	failureBox.hidden = true
	bootBox.hidden = false
	logLines = []
	flushLog()
	settled = false
	const state = await window.monoide.bootRetry()
	if (state) applyState(state)
})

document.getElementById("back").addEventListener("click", () => {
	failureBox.hidden = true
	bootBox.hidden = true
	pickButton.hidden = false
	pickButton.disabled = false
	pickButton.textContent = "Choose project folder..."
	renderRecents()
})

document.getElementById("open-log").addEventListener("click", (event) => {
	window.monoide.openLogFile(event.currentTarget.dataset.path || "")
})

document.getElementById("copy-report").addEventListener("click", (event) => {
	window.monoide.copyText(reportBox.textContent)
	event.currentTarget.textContent = "Copied"
	setTimeout(() => {
		event.currentTarget.textContent = "Copy report"
	}, 1500)
})

window.monoide.onLog(appendLog)

window.monoide.onBootEvent((event) => {
	if (!event) return
	if (event.type === "log") {
		appendLog(event.data.line)
	} else if (event.type === "component") {
		remote = remote.map((row) => (row.key === event.data.key ? event.data : row))
		if (!remote.some((row) => row.key === event.data.key)) remote.push(event.data)
		renderSteps()
	} else if (event.type === "snapshot" || event.type === "end") {
		applyState(event.data)
	} else if (event.type === "done") {
		window.monoide.bootState().then(applyState)
	}
})

// The dependency install starts with the app, before a folder is chosen, so its
// progress is visible on the very first screen.
window.monoide.onPrewarm((event) => {
	if (!event) return
	const state = { running: "active", ready: "ok", failed: "failed", skipped: "skipped" }[
		event.state
	]
	if (state) local.deps.state = state
	if (event.phase) local.deps.detail = event.phase
	if (event.log) appendLog(event.log)
	if (bootBox.hidden && local.deps.state !== "pending") bootBox.hidden = false
	renderSteps()
})

window.monoide.prewarmState().then((state) => {
	if (!state) return
	local.deps.state = { running: "active", ready: "ok", failed: "failed" }[state.state] || "pending"
	local.deps.detail = state.phase || ""
	if (local.deps.state !== "pending") bootBox.hidden = false
	renderSteps()
})

renderSteps()
renderRecents()
