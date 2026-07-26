// Minimal, context-isolated bridge for launcher.html.
const { contextBridge, ipcRenderer } = require("electron")

contextBridge.exposeInMainWorld("monoide", {
	pickFolder: () => ipcRenderer.invoke("pick-folder"),
	recentFolders: () => ipcRenderer.invoke("recent-folders"),
	openProject: (folder) => ipcRenderer.invoke("open-project", folder),
	enterIde: () => ipcRenderer.invoke("enter-ide"),
	upstreamStatus: () => ipcRenderer.invoke("upstream-status"),
	backendLog: () => ipcRenderer.invoke("backend-log"),
	// startup health
	bootState: () => ipcRenderer.invoke("boot-state"),
	bootReport: () => ipcRenderer.invoke("boot-report"),
	bootRetry: () => ipcRenderer.invoke("boot-retry"),
	prewarmState: () => ipcRenderer.invoke("prewarm-state"),
	openLogFile: (path) => ipcRenderer.invoke("open-log-file", path),
	copyText: (text) => ipcRenderer.invoke("copy-text", text),
	onLog: (handler) => {
		ipcRenderer.on("backend-log", (_event, line) => handler(line))
	},
	onBootEvent: (handler) => {
		ipcRenderer.on("boot-event", (_event, event) => handler(event))
	},
	onPrewarm: (handler) => {
		ipcRenderer.on("prewarm-progress", (_event, event) => handler(event))
	},
})
