/* Shell: explorer, tabs, saving, search, terminal, config modal, status bar. */

(function () {
	"use strict";

	const $ = (id) => document.getElementById(id);
	const esc = (s) => String(s == null ? "" : s)
		.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");

	const api = {
		get: (url) => fetch(url).then((r) => r.json()),
		post: (url, body) => fetch(url, {
			method: "POST",
			headers: { "Content-Type": "application/json" },
			body: JSON.stringify(body || {}),
		}).then((r) => r.json()),
	};

	const state = {
		root: "",
		config: {},
		open: {},        // path -> {content, dirty, saved}
		active: null,
		expanded: { ".": true },
		children: {},    // dir path -> entries
		terminal: null,
		sendCtx: true,
		auth: { locked: true, account: {} },
		loginFlow: null,
		loginStream: null,
	};

	/* ------------------------------------------------------------------ editor */
	const editor = new Editor({
		textarea: $("code"),
		highlight: $("hl"),
		gutter: $("gutter"),
		onChange: () => {
			if (!state.active) return;
			const file = state.open[state.active];
			file.content = editor.getValue();
			file.dirty = file.content !== file.saved;
			renderTabs();
		},
		onCursor: (line, col) => { $("st-pos").textContent = line + ":" + col; },
		onSave: () => saveActive(),
	});

	/* ------------------------------------------------------------------ tree */
	async function loadDir(path) {
		const data = await api.get("/api/tree?path=" + encodeURIComponent(path));
		state.children[path] = data.entries || [];
	}

	function renderTree() {
		const rows = [];
		const walk = (dir, depth) => {
			for (const entry of state.children[dir] || []) {
				const open = !!state.expanded[entry.path];
				rows.push(
					'<div class="node ' + (entry.dir ? "dir" : "file") +
					(state.active === entry.path ? " sel" : "") +
					'" data-path="' + esc(entry.path) + '" data-dir="' + (entry.dir ? 1 : 0) +
					'" style="padding-left:' + (8 + depth * 12) + 'px">' +
					'<span class="box"></span><span class="nm">' + esc(entry.name) + "</span>" +
					(entry.dir ? '<span class="dot">' + (open ? "−" : "+") + "</span>" : "") +
					"</div>"
				);
				if (entry.dir && open) walk(entry.path, depth + 1);
			}
		};
		walk(".", 0);
		$("tree").innerHTML = rows.join("");
	}

	$("tree").addEventListener("click", async (event) => {
		const node = event.target.closest(".node");
		if (!node) return;
		const path = node.dataset.path;
		if (node.dataset.dir === "1") {
			state.expanded[path] = !state.expanded[path];
			if (state.expanded[path] && !state.children[path]) await loadDir(path);
			renderTree();
		} else {
			openFile(path);
		}
	});

	/* ------------------------------------------------------------------ tabs */
	function renderTabs() {
		const paths = Object.keys(state.open);
		$("tabs").innerHTML = paths.map((path) =>
			'<div class="tab' + (path === state.active ? " act" : "") +
			(state.open[path].dirty ? " dirty" : "") + '" data-path="' + esc(path) + '">' +
			'<span class="nm">' + esc(path.split("/").pop()) + "</span>" +
			'<span class="x" data-x="' + esc(path) + '">X</span></div>'
		).join("");
		$("st-file").textContent = state.active
			? state.active.toUpperCase() + (state.open[state.active].dirty ? " *" : "")
			: "NO FILE";
	}

	$("tabs").addEventListener("click", (event) => {
		const close = event.target.dataset.x;
		if (close) {
			delete state.open[close];
			if (state.active === close) {
				const next = Object.keys(state.open)[0] || null;
				state.active = next;
				editor.setValue(next ? state.open[next].content : "");
			}
			renderTabs();
			return;
		}
		const tab = event.target.closest(".tab");
		if (tab) activate(tab.dataset.path);
	});

	function activate(path) {
		state.active = path;
		editor.setValue(state.open[path].content);
		editor.setDiagnostics([]);
		$("diags").innerHTML = "";
		renderTabs();
		renderTree();
	}

	async function openFile(path, line) {
		if (!state.open[path]) {
			const data = await api.get("/api/file?path=" + encodeURIComponent(path));
			if (data.error) { flash(data.error); return; }
			if (data.binary) { flash("BINARY FILE NOT SHOWN"); return; }
			state.open[path] = { content: data.content, saved: data.content, dirty: false };
		}
		activate(path);
		if (line) editor.goto(line);
		lint();
	}

	async function saveActive() {
		if (!state.active) return;
		const path = state.active;
		const content = editor.getValue();
		const result = await api.post("/api/file/save", { path, content });
		if (result.error) { flash(result.error); return; }
		state.open[path].saved = content;
		state.open[path].dirty = false;
		renderTabs();
		showDiagnostics(result.diagnostics || []);
	}

	/* ------------------------------------------------------------------ lsp */
	let lintTimer = 0;
	function lint() {
		clearTimeout(lintTimer);
		lintTimer = setTimeout(async () => {
			if (!state.active) return;
			const result = await api.post("/api/lsp/diagnostics", {
				path: state.active,
				content: editor.getValue(),
			});
			$("st-lsp").textContent = result.available ? "LSP ON" : "LSP IDLE";
			showDiagnostics(result.diagnostics || []);
		}, 700);
	}
	$("code").addEventListener("input", lint);

	function showDiagnostics(list) {
		editor.setDiagnostics(list);
		$("diags").innerHTML = list.slice(0, 60).map((item) => {
			const line = ((item.range && item.range.start ? item.range.start.line : 0) + 1);
			const severity = ["", "ERROR", "WARN", "INFO", "HINT"][item.severity || 1] || "INFO";
			return '<div class="diag" data-line="' + line + '"><b>' + severity + "</b>" +
				'<span class="ln">' + line + "</span><span>" + esc(item.message) + "</span></div>";
		}).join("");
	}
	$("diags").addEventListener("click", (event) => {
		const row = event.target.closest(".diag");
		if (row) editor.goto(+row.dataset.line);
	});

	/* ------------------------------------------------------------------ search */
	$("btn-search").onclick = async () => {
		const query = $("q").value;
		if (!query) return;
		$("hits").innerHTML = '<div class="hit"><span>SEARCHING…</span></div>';
		const result = await api.post("/api/search", {
			query, regex: $("q-regex").checked, case: $("q-case").checked,
		});
		const hits = result.hits || [];
		$("hits").innerHTML = hits.length
			? hits.map((hit) =>
				'<div class="hit" data-path="' + esc(hit.path) + '" data-line="' + hit.line + '">' +
				"<b>" + esc(hit.path) + ":" + hit.line + "</b><span>" + esc(hit.text) + "</span></div>"
			).join("")
			: '<div class="hit"><span>NO MATCHES</span></div>';
	};
	$("q").addEventListener("keydown", (e) => { if (e.key === "Enter") $("btn-search").click(); });
	$("hits").addEventListener("click", (event) => {
		const hit = event.target.closest(".hit");
		if (hit && hit.dataset.path) openFile(hit.dataset.path, +hit.dataset.line);
	});

	/* ------------------------------------------------------------------ panels */
	document.querySelectorAll(".nav .btn").forEach((button) => {
		button.onclick = () => {
			document.querySelectorAll(".nav .btn").forEach((b) => b.classList.remove("on"));
			button.classList.add("on");
			document.querySelectorAll(".pane").forEach((pane) => {
				pane.classList.toggle("hidden", pane.dataset.pane !== button.dataset.panel);
			});
			if (button.dataset.panel === "tools") renderRuntime();
		};
	});
	document.querySelector('.nav .btn[data-panel="files"]').classList.add("on");

	async function renderRuntime() {
		const data = await api.get("/api/state");
		const rows = (list, head) =>
			'<table class="mono-table"><tr><th>' + head + "</th><th>STATE</th></tr>" +
			(list.length ? list : [{ a: "—", b: "NONE" }])
				.map((row) => "<tr><td>" + esc(row.a) + "</td><td>" + esc(row.b) + "</td></tr>").join("") +
			"</table>";
		$("runtime").innerHTML =
			rows((data.lsp || []).map((r) => ({
				a: r.language,
				b: (r.running ? "RUNNING " + (r.rss_mb || 0) + "MB" : "STOPPED"),
			})), "LANGUAGE SERVER") +
			rows((data.mcp || []).map((r) => ({
				a: r.name,
				b: (r.running ? r.tools + " TOOLS" : "IDLE"),
			})), "MCP SERVER") +
			'<button class="btn wide" id="btn-mcp">EDIT MCP SERVERS</button>';
		$("btn-mcp").onclick = mcpModal;
	}

	/* ------------------------------------------------------------------ files ops */
	$("btn-refresh").onclick = async () => {
		for (const dir of Object.keys(state.children)) await loadDir(dir);
		renderTree();
	};
	$("btn-new-file").onclick = async () => {
		const name = prompt("NEW FILE PATH");
		if (!name) return;
		await api.post("/api/file/create", { path: name });
		await loadDir(".");
		renderTree();
		openFile(name);
	};
	$("btn-new-dir").onclick = async () => {
		const name = prompt("NEW FOLDER PATH");
		if (!name) return;
		await api.post("/api/file/create", { path: name, dir: true });
		await loadDir(".");
		renderTree();
	};
	$("btn-save").onclick = saveActive;

	/* ------------------------------------------------------------------ terminal */
	const term = $("term");
	$("btn-term").onclick = async () => {
		if (state.terminal) return;
		const data = await api.post("/api/term/new", { cols: 100, rows: 24 });
		if (data.error) { term.textContent = data.error; return; }
		state.terminal = data.id;
		term.textContent = "";
		const stream = new EventSource("/api/term/stream?id=" + data.id);
		stream.addEventListener("out", (event) => {
			const payload = JSON.parse(event.data).data || "";
			// strip the escape sequences a plain <pre> cannot render
			term.textContent += payload
				.replace(/\u001b\][^\u0007]*\u0007/g, "")
				.replace(/\u001b\[[0-9;?]*[A-Za-z]/g, "")
				.replace(/\r/g, "");
			if (term.textContent.length > 120000) {
				term.textContent = term.textContent.slice(-90000);
			}
			term.scrollTop = term.scrollHeight;
		});
		stream.addEventListener("exit", () => { stream.close(); state.terminal = null; });
		term.focus();
	};
	$("btn-term-kill").onclick = async () => {
		if (!state.terminal) return;
		await api.post("/api/term/close", { id: state.terminal });
		state.terminal = null;
		term.textContent += "\n[terminal closed]\n";
	};
	$("btn-term-toggle").onclick = () => {
		const wrap = $("term-wrap");
		wrap.classList.toggle("collapsed");
		$("btn-term-toggle").textContent = wrap.classList.contains("collapsed") ? "SHOW" : "HIDE";
	};
	term.addEventListener("keydown", (event) => {
		if (!state.terminal) return;
		event.preventDefault();
		let data = event.key;
		if (data === "Enter") data = "\n";
		else if (data === "Backspace") data = "\x7f";
		else if (data === "Tab") data = "\t";
		else if (data === "ArrowUp") data = "\x1b[A";
		else if (data === "ArrowDown") data = "\x1b[B";
		else if (data === "ArrowRight") data = "\x1b[C";
		else if (data === "ArrowLeft") data = "\x1b[D";
		else if (event.ctrlKey && event.key.length === 1) {
			data = String.fromCharCode(event.key.toUpperCase().charCodeAt(0) - 64);
		} else if (data.length !== 1) return;
		api.post("/api/term/input", { id: state.terminal, data });
	});

	/* ------------------------------------------------------------------ chat */
	const chat = new Chat({
		log: $("log"),
		status: $("chat-status"),
		input: $("msg"),
		sendBtn: $("btn-send"),
		stopBtn: $("btn-stop"),
		getAttachments: () => (state.sendCtx && state.active ? [state.active] : []),
		onFileChanged: async (path) => {
			if (state.open[path]) {
				const data = await api.get("/api/file?path=" + encodeURIComponent(path));
				if (!data.error && !data.binary) {
					state.open[path] = { content: data.content, saved: data.content, dirty: false };
					if (state.active === path) editor.setValue(data.content);
				}
			}
			await loadDir(".");
			renderTree();
		},
	});

	function send() {
		const text = $("msg").value;
		if (!text.trim()) return;
		if (state.auth && state.auth.locked) { showGate(true); return; }
		$("msg").value = "";
		chat.send(text);
	}
	$("btn-send").onclick = send;
	$("btn-stop").onclick = () => chat.stop();
	$("btn-new-chat").onclick = () => chat.reset();
	$("msg").addEventListener("keydown", (event) => {
		if (event.key === "Enter" && !event.shiftKey) { event.preventDefault(); send(); }
	});
	$("btn-ctx").onclick = () => {
		state.sendCtx = !state.sendCtx;
		$("btn-ctx").textContent = state.sendCtx ? "CTX ON" : "CTX OFF";
		$("btn-ctx").classList.toggle("on", state.sendCtx);
	};
	$("btn-auto").onclick = async () => {
		const next = !(state.config.agent && state.config.agent.auto_approve);
		await api.post("/api/config", { set: { "agent.auto_approve": next } });
		state.config.agent = Object.assign({}, state.config.agent, { auto_approve: next });
		$("btn-auto").textContent = next ? "AUTO ON" : "AUTO OFF";
		$("btn-auto").classList.toggle("on", next);
	};

	/* ------------------------------------------------------------------ modals */
	function modal(title, bodyHtml, footHtml) {
		$("modal-title").textContent = title;
		$("modal-body").innerHTML = bodyHtml;
		$("modal-foot").innerHTML = footHtml || '<button class="btn" id="modal-close">CLOSE</button>';
		$("veil").classList.remove("hidden");
		const close = $("modal-close");
		if (close) close.onclick = hideModal;
	}
	function hideModal() { $("veil").classList.add("hidden"); }
	$("modal-x").onclick = hideModal;

	$("btn-settings").onclick = () => {
		const upstream = state.config.upstream || {};
		const agent = state.config.agent || {};
		const lsp = state.config.lsp || {};
		modal("CONFIG",
			field("UPSTREAM BASE URL", "upstream.base_url", upstream.base_url,
				"notion2api endpoint, e.g. http://127.0.0.1:8000/v1") +
			field("API KEY", "upstream.api_key", upstream.api_key, "matches API_KEY in notion2api") +
			field("MODEL", "upstream.model", upstream.model) +
			field("MAX ROUNDS", "agent.max_rounds", agent.max_rounds,
				"tool iterations per request") +
			field("LSP MEMORY MB", "lsp.memory_mb", lsp.memory_mb,
				"hard cap per language server") +
			field("LSP IDLE SHUTDOWN S", "lsp.idle_shutdown_seconds", lsp.idle_shutdown_seconds,
				"servers are killed after this idle time") +
			field("MAX LANGUAGE SERVERS", "lsp.max_servers", lsp.max_servers),
			'<button class="btn" id="modal-close">CANCEL</button>' +
			'<button class="btn solid" id="modal-apply">APPLY</button>');
		$("modal-apply").onclick = async () => {
			const set = {};
			$("modal-body").querySelectorAll("input[data-key]").forEach((input) => {
				let value = input.value;
				if (/^-?\d+$/.test(value)) value = parseInt(value, 10);
				set[input.dataset.key] = value;
			});
			const result = await api.post("/api/config", { set });
			state.config = result.config || state.config;
			hideModal();
			refreshStatus();
			loadModels();
		};
	};

	function field(label, key, value, hint) {
		return '<div class="field"><label>' + label + "</label>" +
			'<input class="in" data-key="' + key + '" value="' + esc(value == null ? "" : value) + '">' +
			(hint ? '<span class="hint">' + esc(hint) + "</span>" : "") + "</div>";
	}

	async function mcpModal() {
		const current = JSON.stringify((state.config.mcp || {}).servers || {}, null, 2);
		modal("MCP SERVERS",
			'<div class="field"><label>SERVERS JSON</label>' +
			'<textarea id="mcp-json" rows="14" class="in" style="height:auto;font-size:12px">' +
			esc(current) + "</textarea>" +
			'<span class="hint">{"NAME": {"command": "npx", "args": ["-y", "pkg"], ' +
			'"env": {}, "enabled": true}}</span></div>',
			'<button class="btn" id="modal-close">CANCEL</button>' +
			'<button class="btn solid" id="mcp-save">SAVE</button>');
		$("mcp-save").onclick = async () => {
			let parsed;
			try { parsed = JSON.parse($("mcp-json").value); }
			catch (err) { alert("INVALID JSON: " + err.message); return; }
			const result = await api.post("/api/config", { set: { "mcp.servers": parsed } });
			state.config = result.config || state.config;
			hideModal();
			renderRuntime();
		};
	}

	/* ------------------------------------------------------------------ status */
	async function loadModels() {
		const data = await api.get("/api/models");
		const models = data.models && data.models.length
			? data.models
			: [(state.config.upstream || {}).model || "claude-sonnet4.6"];
		const active = (state.config.upstream || {}).model;
		$("model").innerHTML = models.map((id) =>
			'<option value="' + esc(id) + '"' + (id === active ? " selected" : "") + ">" +
			esc(id) + "</option>").join("");
		$("st-upstream").textContent =
			(data.models && data.models.length ? "UPSTREAM OK " : "UPSTREAM OFFLINE ") + models.length;
	}
	$("model").onchange = async () => {
		await api.post("/api/config", { set: { "upstream.model": $("model").value } });
	};

	async function refreshStatus() {
		const metrics = await api.get("/api/metrics").catch(() => null);
		if (!metrics) return;
		$("m-ide").textContent = metrics.ide_mb;
		$("m-lsp").textContent = metrics.lsp_mb;
		$("m-mcp").textContent = (metrics.mcp_running || []).length;
		$("m-up").textContent = metrics.uptime_s;
		$("st-mem").textContent =
			"MEM " + metrics.ide_mb + "+" + metrics.lsp_mb + "MB";
		$("st-lsp").textContent = (metrics.lsp_running || []).length
			? "LSP " + metrics.lsp_running.join(",").toUpperCase()
			: "LSP IDLE";
	}

	function flash(message) {
		$("st-file").textContent = String(message).toUpperCase().slice(0, 80);
	}

	/* ------------------------------------------------------------------ keys */
	document.addEventListener("keydown", (event) => {
		const mod = event.ctrlKey || event.metaKey;
		if (mod && event.key.toLowerCase() === "s") { event.preventDefault(); saveActive(); }
		if (mod && event.key.toLowerCase() === "p") {
			event.preventDefault();
			document.querySelector('.nav .btn[data-panel="search"]').click();
			$("q").focus();
		}
		if (mod && event.key.toLowerCase() === "j") {
			event.preventDefault();
			$("btn-term-toggle").click();
		}
		if (mod && event.key === "Enter") { event.preventDefault(); $("msg").focus(); }
		if (event.key === "Escape") hideModal();
	});

	/* ------------------------------------------------------------------ login gate
	   Mirrors notion2api's login.py: a browser is launched with a debugging port,
	   the user signs in, token_v2 is read from that session, then the workspace
	   list is pulled and one (user, space) pair is stored. */
	function gateLog(line) {
		const box = $("gate-log");
		const text = String(line).toUpperCase();
		box.textContent = (box.textContent === "WAITING FOR A BROWSER CHOICE" ? "" : box.textContent + "\n") + text;
		box.scrollTop = box.scrollHeight;
	}
	function gateStatus(text) { $("gate-status").textContent = String(text).toUpperCase(); }

	function showGate(show) {
		$("gate").classList.toggle("hidden", !show);
		if (show) $("gate-log").scrollTop = $("gate-log").scrollHeight;
	}

	async function loadAuth() {
		const data = await api.get("/api/auth/state");
		state.auth = data;
		renderBrowsers(data.browsers || []);
		const account = data.account || {};
		$("btn-account").textContent = data.authenticated
			? (account.user_name || "ACCOUNT").toUpperCase().slice(0, 12)
			: "SIGN IN";
		$("btn-account").classList.toggle("on", !!data.authenticated);
		showGate(!!data.locked);
		if (data.authenticated) {
			gateStatus("attached: " + (account.user_email || account.user_name || "account"));
		}
		return data;
	}

	function renderBrowsers(browsers) {
		$("gate-browsers").innerHTML = browsers.map((item) => {
			const note = !item.installed ? "NOT FOUND" : (item.cdp ? "AUTO" : "MANUAL TOKEN");
			return '<button class="btn" data-browser="' + esc(item.key) + '"' +
				(item.installed ? "" : " disabled") + ">" + esc(item.label) +
				'<span class="tagline">' + note + "</span></button>";
		}).join("");
		$("gate-browsers").querySelectorAll("button[data-browser]").forEach((button) => {
			button.onclick = () => startLogin(button.dataset.browser);
		});
	}

	async function startLogin(browser) {
		stopLoginStream();
		$("gate-pick").classList.add("hidden");
		$("gate-pick-label").classList.add("hidden");
		gateLog("opening " + browser);
		gateStatus("waiting for sign-in");
		$("gate-cancel").classList.remove("hidden");
		const result = await api.post("/api/auth/login", { browser });
		if (result.error) { gateLog("error: " + result.error); gateStatus("failed"); return; }
		state.loginFlow = result.flow;
		const stream = new EventSource("/api/auth/stream?flow=" + encodeURIComponent(result.flow));
		state.loginStream = stream;
		stream.addEventListener("log", (event) => gateLog(JSON.parse(event.data).text));
		stream.addEventListener("waiting", (event) => {
			gateStatus("waiting for token_v2 - " + JSON.parse(event.data).seconds_left + "s left");
		});
		stream.addEventListener("await_token", (event) => {
			gateLog(JSON.parse(event.data).text);
			$("gate-manual").open = true;
			$("gate-token").focus();
		});
		stream.addEventListener("candidates", (event) => {
			renderCandidates(JSON.parse(event.data).candidates || []);
		});
		stream.addEventListener("error", (event) => {
			let text = "login failed";
			try { text = JSON.parse(event.data).text || text; } catch (err) { /* transport error */ }
			gateLog("error: " + text);
			gateStatus("failed - pick a browser to retry");
			stopLoginStream();
		});
		stream.addEventListener("done", async (event) => {
			const account = JSON.parse(event.data).account || {};
			gateLog("attached " + (account.user_email || account.user_name || "account"));
			stopLoginStream();
			await loadAuth();
			loadModels();
		});
		stream.addEventListener("end", () => stopLoginStream());
	}

	function renderCandidates(candidates) {
		if (!candidates.length) return;
		$("gate-pick-label").classList.remove("hidden");
		$("gate-pick").classList.remove("hidden");
		$("gate-pick").innerHTML = candidates.map((row) => {
			const who = (row.user_name || "USER") + (row.user_email ? " <" + row.user_email + ">" : "");
			const where = (row.space_name || row.space_id.slice(0, 13) + "…") +
				(row.space_plan ? " / " + row.space_plan : "") +
				(row.has_space_view ? "" : " / NO SPACE VIEW");
			return '<button class="btn" data-index="' + row.index + '">' +
				'<span class="who">' + esc(who) + "</span>" +
				'<span class="where">' + esc(where.toUpperCase()) + "</span></button>";
		}).join("");
		$("gate-pick").querySelectorAll("button[data-index]").forEach((button) => {
			button.onclick = async () => {
				const result = await api.post("/api/auth/select", {
					flow: state.loginFlow, index: parseInt(button.dataset.index, 10),
				});
				if (result.error) { gateLog("error: " + result.error); return; }
				await loadAuth();
				loadModels();
			};
		});
		gateStatus("pick a workspace");
	}

	function stopLoginStream() {
		if (state.loginStream) { state.loginStream.close(); state.loginStream = null; }
		$("gate-cancel").classList.add("hidden");
	}

	$("gate-cancel").onclick = async () => {
		if (state.loginFlow) await api.post("/api/auth/cancel", { flow: state.loginFlow });
		stopLoginStream();
		gateLog("cancelled");
		gateStatus("locked");
	};

	$("gate-token-go").onclick = async () => {
		const token = $("gate-token").value.trim();
		if (!token) return;
		gateLog("validating pasted token");
		const result = await api.post("/api/auth/token", { token, flow: state.loginFlow || "" });
		if (result.error) { gateLog("error: " + result.error); gateStatus("failed"); return; }
		$("gate-token").value = "";
		if (result.flow) state.loginFlow = result.flow;
		if (result.candidates && result.candidates.length > 1) renderCandidates(result.candidates);
		await loadAuth();
		loadModels();
	};
	$("gate-token").addEventListener("keydown", (event) => {
		if (event.key === "Enter") { event.preventDefault(); $("gate-token-go").click(); }
	});

	$("btn-account").onclick = async () => {
		const data = await api.get("/api/auth/state");
		if (!data.authenticated) { showGate(true); return; }
		const account = data.account || {};
		modal("NOTION ACCOUNT",
			'<div class="kv">' +
			row("USER", account.user_name) + row("EMAIL", account.user_email) +
			row("SPACE", account.space_name || account.space_id) +
			row("SPACE ID", account.space_id) + row("USER ID", account.user_id) +
			row("TOKEN", account.token_hint) + row("SAVED", account.saved_at) +
			row("NOTION2API DIR", data.notion2api_dir || "not linked") +
			"</div>",
			'<button class="btn" id="modal-close">CLOSE</button>' +
			'<button class="btn" id="acc-check">CHECK TOKEN</button>' +
			'<button class="btn solid" id="acc-out">SIGN OUT</button>');
		$("acc-check").onclick = async () => {
			$("acc-check").textContent = "CHECKING…";
			const check = await api.get("/api/auth/check");
			$("acc-check").textContent = (check.ok ? "OK: " : "BAD: ") + String(check.status).toUpperCase();
		};
		$("acc-out").onclick = async () => {
			await api.post("/api/auth/logout", {});
			hideModal();
			chat.reset();
			await loadAuth();
			gateLog("signed out");
			gateStatus("locked");
		};
	};

	function row(label, value) {
		return "<div><b>" + label + "</b><span>" + esc(value || "–") + "</span></div>";
	}

	/* ------------------------------------------------------------------ boot */
	(async function boot() {
		await loadAuth();
		const data = await api.get("/api/state");
		state.root = data.root;
		state.config = data.config || {};
		$("ws-name").textContent = (data.name || "WORKSPACE").toUpperCase();
		document.title = (data.name || "EDITOR").toUpperCase();
		if (state.config.agent && state.config.agent.auto_approve) {
			$("btn-auto").textContent = "AUTO ON";
			$("btn-auto").classList.add("on");
		}
		$("btn-ctx").classList.add("on");
		await loadDir(".");
		renderTree();
		renderTabs();
		loadModels();
		refreshStatus();
		setInterval(refreshStatus, 5000);
	})();
})();
