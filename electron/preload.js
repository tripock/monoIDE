// Minimal, context-isolated bridge for launcher.html.
const { contextBridge, ipcRenderer } = require("electron")

contextBridge.exposeInMainWorld("monoide", {
	pickFolder: () => ipcRenderer.invoke("pick-folder"),
	recentFolders: () => ipcRenderer.invoke("recent-folders"),
	openProject: (folder) => ipcRenderer.invoke("open-project", folder),
	enterIde: () => ipcRenderer.invoke("enter-ide"),
	upstreamStatus: () => ipcRenderer.invoke("upstream-status"),
	backendLog: () => ipcRenderer.invoke("backend-log"),
	onLog: (handler) => {
		ipcRenderer.on("backend-log", (_event, line) => handler(line))
	},
})
