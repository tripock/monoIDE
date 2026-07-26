// Launcher screen: pick a folder, boot the stack, then swap the window over to
// the editor served by the python backend.

const pickButton = document.getElementById("pick")
const recentsBox = document.getElementById("recents-box")
const recentsList = document.getElementById("recents")
const bootBox = document.getElementById("boot")
const logBox = document.getElementById("log")
const errorBox = document.getElementById("error")

const steps = {}
for (const node of document.querySelectorAll(".step")) {
	steps[node.dataset.step] = node
}

function mark(name, state) {
	const node = steps[name]
	if (!node) return
	node.classList.remove("active", "done", "failed")
	if (state) node.classList.add(state)
}

function appendLog(line) {
	logBox.textContent += (logBox.textContent ? "\n" : "") + line
	logBox.scrollTop = logBox.scrollHeight
}

function fail(message) {
	errorBox.hidden = false
	errorBox.textContent = message
	pickButton.disabled = false
	pickButton.textContent = "Choose project folder..."
	renderRecents()
}

async function renderRecents() {
	const rows = await window.monoide.recentFolders()
	recentsList.innerHTML = ""
	recentsBox.hidden = rows.length === 0
	for (const folder of rows) {
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

async function boot(folder) {
	errorBox.hidden = true
	recentsBox.hidden = true
	bootBox.hidden = false
	logBox.textContent = ""
	pickButton.disabled = true
	pickButton.textContent = "starting..."

	mark("folder", "done")
	appendLog("project: " + folder)
	mark("backend", "active")

	const result = await window.monoide.openProject(folder)
	if (!result || !result.ok) {
		mark("backend", "failed")
		for (const line of (result && result.log) || []) appendLog(line)
		return fail((result && result.error) || "could not start the backend")
	}
	mark("backend", "done")

	const upstream = result.upstream || (await window.monoide.upstreamStatus())
	if (upstream && upstream.state === "failed") {
		mark("api", "failed")
		appendLog("notion2api: " + (upstream.error || "failed to start"))
		appendLog("the editor still opens - sign in to Notion, then retry from the chat panel")
	} else if (upstream) {
		mark("api", "done")
		appendLog("notion2api: " + upstream.state + " " + (upstream.base_url || ""))
	} else {
		mark("api", "done")
	}

	mark("ui", "active")
	appendLog("opening editor " + result.url)
	await window.monoide.enterIde()
}

pickButton.addEventListener("click", async () => {
	const folder = await window.monoide.pickFolder()
	if (folder) boot(folder)
})

window.monoide.onLog(appendLog)
renderRecents()
