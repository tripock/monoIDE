/* Agent panel: SSE client for /api/chat, activity log, approval prompts.
 *
 * The panel also owns the "who answers" switch: the default Notion AI assistant
 * or one of the user's own agents. The agents of the signed-in account are read
 * from /api/agents, so picking one is a click; pasting a link stays as the
 * manual fallback for an agent the list does not show. Nothing agent-specific
 * is ever shipped in the source - an agent only exists inside the workspace
 * that owns it.
 *
 * It owns one more choice: where a chat is kept. That is asked next to SEND and
 * only while the chat is still empty - once the first message is away the
 * transcript is already being written one way or the other, so the switch
 * pins itself and disappears.
 */

(function (global) {
	"use strict";

	const esc = (s) => String(s == null ? "" : s)
		.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");

	/* esc() is for text nodes; attribute values also need the quotes gone */
	const attr = (s) => esc(s).replace(/"/g, "&quot;");

	const UUID = /[0-9a-fA-F]{8}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{12}/g;

	/* Pull the agent id out of a pasted link. Accepts a full notion.so url, a
	 * dashed uuid or a dashless one; answers "" when there is no id at all. */
	function agentIdFromUrl(raw) {
		const text = String(raw || "").split("?")[0].split("#")[0];
		const found = text.match(UUID);
		if (!found) return "";
		const digits = found[found.length - 1].replace(/[^0-9a-fA-F]/g, "").toLowerCase();
		if (digits.length !== 32) return "";
		return [digits.slice(0, 8), digits.slice(8, 12), digits.slice(12, 16),
			digits.slice(16, 20), digits.slice(20)].join("-");
	}

	/* "3H AGO" from a millisecond stamp; "" when there is none */
	function ago(ms) {
		const stamp = Number(ms || 0);
		if (!stamp) return "";
		const seconds = Math.max(1, Math.round((Date.now() - stamp) / 1000));
		if (seconds < 90) return seconds + "S AGO";
		const minutes = Math.round(seconds / 60);
		if (minutes < 90) return minutes + "M AGO";
		const hours = Math.round(minutes / 60);
		if (hours < 36) return hours + "H AGO";
		return Math.round(hours / 24) + "D AGO";
	}

	/* very small markdown: fenced code, inline code, bold, bullets */
	function md(text) {
		const blocks = [];
		let out = esc(text).replace(/```(\w*)\n([\s\S]*?)```/g, (m, lang, code) => {
			blocks.push("<pre>" + code.replace(/\n$/, "") + "</pre>");
			return "\u0000" + (blocks.length - 1) + "\u0000";
		});
		out = out
			.replace(/`([^`\n]+)`/g, "<code>$1</code>")
			.replace(/\*\*([^*\n]+)\*\*/g, "<b>$1</b>")
			.replace(/^\s*[-*]\s+(.*)$/gm, "\u2013 $1");
		return out.replace(/\u0000(\d+)\u0000/g, (m, i) => blocks[+i]);
	}

	function Chat(opts) {
		this.log = opts.log;
		this.status = opts.status;
		this.input = opts.input;
		this.sendBtn = opts.sendBtn;
		this.stopBtn = opts.stopBtn;
		this.getAttachments = opts.getAttachments || (() => []);
		this.onFileChanged = opts.onFileChanged || function () {};
		this.session = null;
		this.busy = false;
		this.controller = null;
		this.current = null;
		this.acts = {};
		this.agent = { mode: "notion", id: "", url: "", name: "" };
		this.agentCard = null;
		/* where this chat is kept. "web" means Notion only: nothing on this pc. */
		this.storage = "local";
		this.chatId = "";
		this.pinned = false;
		this.historyCard = null;
		this.initAgent();
		this.initStorage();
		this.initHistory();
	}

	Chat.prototype.el = function (cls, html) {
		const node = document.createElement("div");
		node.className = cls;
		if (html != null) node.innerHTML = html;
		this.log.appendChild(node);
		this.log.scrollTop = this.log.scrollHeight;
		return node;
	};

	/* -- which assistant answers ------------------------------------------ */

	Chat.prototype.initAgent = function () {
		this.agentBtn = document.getElementById("btn-agent");
		if (!this.agentBtn) return;
		this.agentBtn.onclick = () => this.pickAgent();
		this.loadAgent();
	};

	Chat.prototype.loadAgent = async function () {
		try {
			const state = await (await fetch("/api/state")).json();
			const config = (state || {}).config || {};
			const up = config.upstream || {};
			const custom = String(up.agent_mode || "notion").toLowerCase() === "custom";
			this.agent = {
				mode: custom ? "custom" : "notion",
				id: String(up.agent_id || ""),
				url: String(up.agent_url || ""),
				name: String(up.agent_name || ""),
			};
			// a remembered preference, if the config carries one
			const preferred = String(((config.chat || {}).storage) || "").toLowerCase();
			if (!this.pinned && (preferred === "local" || preferred === "web")) {
				this.storage = preferred;
				this.paintStorage();
			}
		} catch (err) {
			/* keep the default; the button just shows NOTION AI */
		}
		this.paintAgent();
	};

	Chat.prototype.paintAgent = function () {
		if (!this.agentBtn) return;
		const target = this.agent.id || this.agent.url;
		const custom = this.agent.mode === "custom" && !!target;
		// Two words, always the same width: the pane head has six controls in a
		// narrow column, and an agent name of any length used to push the button
		// out of the bar. The name belongs in the tooltip, not in the label.
		this.agentBtn.textContent = custom ? "CUSTOM AGENT" : "NOTION AI";
		this.agentBtn.title = custom
			? (this.agent.name || "custom agent") + " \u2014 " + target
			: "the default Notion AI assistant";
		this.agentBtn.classList.toggle("solid", custom);
	};

	Chat.prototype.pickAgent = function () {
		if (this.agentCard) {
			this.agentCard.remove();
			this.agentCard = null;
		}
		let mode = this.agent.mode;
		let chosen = { id: this.agent.id, name: this.agent.name };
		let listed = false;   // the list is fetched once, and only when needed

		const node = this.el("ask",
			'<div class="q">WHO ANSWERS IN THIS IDE?</div>' +
			'<div class="row">' +
			'<button class="btn" data-m="notion">NOTION AI</button>' +
			'<button class="btn" data-m="custom">MY CUSTOM AGENT</button>' +
			"</div>" +
			'<div id="agent-fields">' +
			'<div class="row" id="agent-list"><span class="muted">READING YOUR AGENTS\u2026</span></div>' +
			'<div class="row"><input class="in" id="agent-link" autocomplete="off"' +
			' placeholder="OR PASTE AN AGENT LINK" value="' + attr(this.agent.url) + '"></div>' +
			"</div>" +
			'<div class="row">' +
			'<button class="btn solid" data-a="save">SAVE</button>' +
			'<button class="btn" data-a="cancel">CANCEL</button>' +
			"</div>" +
			'<div class="muted" id="agent-hint">AGENTS COME FROM THE NOTION ACCOUNT THIS IDE ' +
			"IS SIGNED IN WITH</div>");
		this.agentCard = node;

		const fields = node.querySelector("#agent-fields");
		const list = node.querySelector("#agent-list");
		const hint = node.querySelector("#agent-hint");
		const link = node.querySelector("#agent-link");
		const name = null;
		const modeButtons = node.querySelectorAll("button[data-m]");

		const paintChoice = () => {
			list.querySelectorAll("button[data-id]").forEach((button) =>
				button.classList.toggle("solid", button.dataset.id === chosen.id));
		};

		const renderAgents = (rows) => {
			if (!rows.length) {
				list.innerHTML = '<span class="muted">THIS ACCOUNT HAS NO CUSTOM AGENTS \u2014 ' +
					"PASTE A LINK INSTEAD</span>";
				return;
			}
			list.innerHTML = rows.map((row) =>
				'<button class="btn" data-id="' + attr(row.id) + '"' +
				' data-name="' + attr(row.name) + '"' +
				' title="' + attr((row.model || "") + (row.last_chat ? " \u2014 " + row.last_chat : "")) + '">' +
				esc(String(row.name || "").toUpperCase()) + "</button>").join(" ");
			list.querySelectorAll("button[data-id]").forEach((button) => {
				button.onclick = () => {
					chosen = { id: button.dataset.id, name: button.dataset.name };
					// a click on a row wins over a stale link left in the field
					link.value = "";
					hint.textContent = "SELECTED: " + String(chosen.name).toUpperCase();
					paintChoice();
				};
			});
			paintChoice();
		};

		const loadList = async () => {
			if (listed) return;
			listed = true;
			let payload;
			try {
				payload = await (await fetch("/api/agents")).json();
			} catch (err) {
				listed = false;   // let the next open retry
				list.innerHTML = '<span class="muted">COULD NOT READ THE AGENT LIST: ' +
					esc(String(err.message).toUpperCase()) + "</span>";
				return;
			}
			if (payload && payload.error) {
				listed = false;
				list.innerHTML = '<span class="muted">' +
					esc(String(payload.error).toUpperCase()) + " \u2014 PASTE A LINK INSTEAD</span>";
				return;
			}
			renderAgents((payload && payload.agents) || []);
		};

		const paint = () => {
			fields.style.display = mode === "custom" ? "" : "none";
			modeButtons.forEach((button) =>
				button.classList.toggle("solid", button.dataset.m === mode));
			if (mode === "custom") loadList();
		};
		modeButtons.forEach((button) => {
			button.onclick = () => {
				mode = button.dataset.m;
				paint();
			};
		});
		paint();

		const close = () => {
			node.remove();
			this.agentCard = null;
		};

		const save = async () => {
			const url = link.value.trim();
			let agentId = "";
			let agentUrl = "";
			let agentName = "";
			if (mode === "custom") {
				if (url) {
					// A pasted link may point at an agent the list did not show, so
					// resolve it against Notion rather than trusting the shape.
					hint.textContent = "CHECKING THAT LINK\u2026";
					let verdict;
					try {
						verdict = await (await fetch("/api/agents/verify", {
							method: "POST",
							headers: { "Content-Type": "application/json" },
							body: JSON.stringify({ agent: url }),
						})).json();
					} catch (err) {
						verdict = { ok: false, detail: err.message };
					}
					if (!verdict || !verdict.ok) {
						hint.textContent = "UNUSABLE LINK: " +
							String((verdict && verdict.detail) || "unknown reason").toUpperCase();
						link.focus();
						return;
					}
					agentId = verdict.id;
					agentUrl = url;
					agentName = verdict.name || "custom agent";
				} else if (chosen.id) {
					agentId = chosen.id;
					agentName = chosen.name || "custom agent";
				} else {
					hint.textContent = "PICK AN AGENT ABOVE, OR PASTE ITS LINK";
					return;
				}
			}
			try {
				const response = await fetch("/api/config", {
					method: "POST",
					headers: { "Content-Type": "application/json" },
					body: JSON.stringify({
						set: {
							"upstream.agent_mode": mode,
							"upstream.agent_url": agentUrl,
							"upstream.agent_id": agentId,
							"upstream.agent_name": agentName,
						},
					}),
				});
				if (!response.ok) throw new Error("HTTP " + response.status);
			} catch (err) {
				hint.textContent = "COULD NOT SAVE: " + String(err.message).toUpperCase();
				return;
			}
			this.agent = { mode, id: agentId, url: agentUrl, name: agentName };
			this.paintAgent();
			close();
			// the bound Notion thread belongs to the previous assistant, so the
			// backend starts a fresh chat on the next message by itself
			this.el("notice", mode === "custom"
				? "AGENT SELECTED: " + String(agentName).toUpperCase() +
					" \u2014 A NEW NOTION CHAT STARTS WITH THE NEXT MESSAGE"
				: "BACK TO THE DEFAULT NOTION AI ASSISTANT");
		};

		node.querySelector('button[data-a="save"]').onclick = save;
		node.querySelector('button[data-a="cancel"]').onclick = close;
		link.onkeydown = (event) => {
			if (event.key === "Enter") { event.preventDefault(); save(); }
		};
		void name;
	};

	/* -- where this chat is kept ------------------------------------------ */

	Chat.prototype.initStorage = function () {
		this.storagePick = document.getElementById("storage-pick");
		if (!this.storagePick) return;
		this.storagePick.querySelectorAll("button[data-s]").forEach((button) => {
			button.onclick = () => {
				// After the first message the chat is already being kept one way or
				// the other; the switch is gone by then, but never trust the DOM.
				if (this.pinned) return;
				this.storage = button.dataset.s === "web" ? "web" : "local";
				this.paintStorage();
				// remember it as the default for the next chat, best effort
				fetch("/api/config", {
					method: "POST",
					headers: { "Content-Type": "application/json" },
					body: JSON.stringify({ set: { "chat.storage": this.storage } }),
				}).catch(() => {});
			};
		});
		this.paintStorage();
	};

	Chat.prototype.paintStorage = function () {
		if (!this.storagePick) return;
		this.storagePick.style.display = this.pinned ? "none" : "";
		this.storagePick.querySelectorAll("button[data-s]").forEach((button) =>
			button.classList.toggle("solid", button.dataset.s === this.storage));
	};

	Chat.prototype.pinStorage = function () {
		if (this.pinned) return;
		this.pinned = true;
		this.paintStorage();
		this.el("notice", this.storage === "web"
			? "WEB HISTORY \u2014 THIS CHAT LIVES IN NOTION ONLY, NOTHING IS WRITTEN TO THIS PC"
			: "LOCAL HISTORY \u2014 THIS CHAT IS KEPT IN .MONOIDE/CHATS");
	};

	/* -- past chats and imports ------------------------------------------- */

	Chat.prototype.initHistory = function () {
		const button = document.getElementById("btn-history");
		if (!button) return;
		button.onclick = () => this.openHistory();
	};

	Chat.prototype.openHistory = function () {
		if (this.historyCard) {
			this.historyCard.remove();
			this.historyCard = null;
		}
		const node = this.el("ask",
			'<div class="q">CHAT HISTORY</div>' +
			'<div class="row">' +
			'<button class="btn solid" data-t="local">ON THIS PC</button>' +
			'<button class="btn" data-t="web">IN NOTION</button>' +
			'<button class="btn" data-t="claude">CLAUDE CODE</button>' +
			"</div>" +
			'<div id="hist-rows"><span class="muted">LOADING\u2026</span></div>' +
			'<div class="row"><button class="btn" data-a="close">CLOSE</button></div>' +
			'<div class="muted" id="hist-hint"></div>');
		this.historyCard = node;

		const rows = node.querySelector("#hist-rows");
		const hint = node.querySelector("#hist-hint");
		const tabs = node.querySelectorAll("button[data-t]");
		let tab = "local";

		const say = (text) => { hint.textContent = String(text || "").toUpperCase(); };
		const empty = (text) => { rows.innerHTML = '<span class="muted">' + esc(text) + "</span>"; };

		const renderLocal = (payload) => {
			const chats = (payload && payload.chats) || [];
			if (!chats.length) return empty("NOTHING KEPT ON THIS PC YET");
			rows.innerHTML = chats.map((chat) =>
				'<div class="row">' +
				'<button class="btn" data-open="' + attr(chat.id) + '"' +
				' title="' + attr(chat.title || chat.id) + '">' +
				esc(String(chat.title || "untitled").toUpperCase().slice(0, 44)) + "</button>" +
				'<span class="muted">' + esc([ago(chat.updated),
					(chat.messages || 0) + " MSG",
					chat.source === "claude-code" ? "IMPORTED" : ""]
					.filter(Boolean).join("  ")) + "</span>" +
				'<button class="btn" data-drop="' + attr(chat.id) + '">DEL</button>' +
				"</div>").join("");
			rows.querySelectorAll("button[data-open]").forEach((button) => {
				button.onclick = () => this.openChat(button.dataset.open);
			});
			rows.querySelectorAll("button[data-drop]").forEach((button) => {
				button.onclick = async () => {
					button.disabled = true;
					try {
						await fetch("/api/chat/delete", {
							method: "POST",
							headers: { "Content-Type": "application/json" },
							body: JSON.stringify({ id: button.dataset.drop }),
						});
					} catch (err) {
						button.disabled = false;
						return say("could not delete: " + err.message);
					}
					button.closest(".row").remove();
				};
			});
		};

		const renderWeb = (payload) => {
			const fallback = String((payload && payload.agent_url) || "");
			if (payload && payload.error) return empty(String(payload.error).toUpperCase());
			const chats = (payload && payload.chats) || [];
			if (!chats.length) {
				return empty("NO CHATS IN NOTION FOR THE SELECTED AGENT YET");
			}
			// These transcripts live in Notion, so a row cannot be replayed here -
			// but it is still a link, so it is a button and it opens Notion.
			rows.innerHTML = chats.map((chat) =>
				'<div class="row">' +
				'<button class="btn" data-web="' + attr(chat.url || fallback) + '"' +
				' title="' + attr(chat.title || chat.id) + '">' +
				esc(String(chat.title || "untitled").toUpperCase().slice(0, 44)) + "</button>" +
				'<span class="muted">' + esc(ago(chat.updated)) + "</span></div>").join("");
			rows.querySelectorAll("button[data-web]").forEach((button) => {
				button.onclick = () => {
					const target = button.dataset.web;
					if (!target) return say("no link for this chat");
					window.open(target, "_blank", "noopener");
					say("opened notion - the transcript is on the agent's page");
				};
			});
			say("kept in notion only - a click opens the agent there");
		};

		const renderClaude = (payload) => {
			const sessions = (payload && payload.sessions) || [];
			if (!sessions.length) {
				return empty("NO CLAUDE CODE SESSIONS FOUND UNDER " +
					String((payload && payload.dir) || "~/.claude/projects").toUpperCase());
			}
			// field names come from discover_claude_sessions: bytes, updated, name
			rows.innerHTML = sessions.map((item) =>
				'<div class="row">' +
				'<button class="btn" data-import="' + attr(item.path) + '"' +
				' title="' + attr(item.path) + '">' +
				esc(String(item.project || item.name || "session").toUpperCase().slice(0, 40)) +
				"</button>" +
				'<span class="muted">' + esc([ago(item.updated),
					item.bytes ? Math.max(1, Math.round(item.bytes / 1024)) + " KB" : ""]
					.filter(Boolean).join("  ")) + "</span></div>").join("");
			rows.querySelectorAll("button[data-import]").forEach((button) => {
				button.onclick = async () => {
					button.disabled = true;
					say("importing\u2026");
					let result;
					try {
						result = await (await fetch("/api/chat/import", {
							method: "POST",
							headers: { "Content-Type": "application/json" },
							body: JSON.stringify({ path: button.dataset.import }),
						})).json();
					} catch (err) {
						button.disabled = false;
						return say("import failed: " + err.message);
					}
					if (!result || !result.ok) {
						button.disabled = false;
						return say("import failed: " +
							((result && result.error) || "unknown reason"));
					}
					const chat = result.chat || {};
					button.textContent = "IMPORTED";
					say("imported " + (chat.messages || 0) +
						" messages - see ON THIS PC");
				};
			});
			say("imports are kept on this pc, next to your local chats");
		};

		const load = async () => {
			empty("LOADING\u2026");
			say("");
			const url = tab === "local" ? "/api/chat/sessions"
				: tab === "web" ? "/api/chat/remote" : "/api/chat/imports";
			let payload;
			try {
				payload = await (await fetch(url)).json();
			} catch (err) {
				return empty("COULD NOT READ THE HISTORY: " +
					String(err.message).toUpperCase());
			}
			if (tab === "local") renderLocal(payload);
			else if (tab === "web") renderWeb(payload);
			else renderClaude(payload);
		};

		tabs.forEach((button) => {
			button.onclick = () => {
				tab = button.dataset.t;
				tabs.forEach((other) => other.classList.toggle("solid", other === button));
				load();
			};
		});
		node.querySelector('button[data-a="close"]').onclick = () => {
			node.remove();
			this.historyCard = null;
		};
		load();
	};

	/* Repaint the log from a stored chat. Read-only on purpose: the agent's own
	 * thread is gone, so continuing here would silently start a new one. */
	Chat.prototype.openChat = async function (id) {
		let record;
		try {
			const response = await fetch("/api/chat/session?id=" + encodeURIComponent(id));
			if (!response.ok) {
				// The server explains itself in the body ("no such chat: <path>",
				// "unreadable chat: \u2026"). A bare status code would send the user
				// hunting for something the answer already contains.
				let reason = "HTTP " + response.status;
				try {
					const body = await response.json();
					if (body && body.error) reason = String(body.error);
				} catch (parseError) { /* not json: keep the status */ }
				throw new Error(reason);
			}
			record = await response.json();
		} catch (err) {
			return this.el("notice", "COULD NOT OPEN THAT CHAT: " +
				esc(String(err.message).toUpperCase()));
		}
		this.historyCard = null;
		this.log.innerHTML = "";
		const origin = record.origin || {};
		this.el("notice", "TRANSCRIPT: " +
			esc(String(record.title || "untitled").toUpperCase()) +
			(origin.tool ? " \u2014 FROM " + esc(String(origin.tool).toUpperCase()) : "") +
			" \u2014 READ ONLY, PRESS NEW TO START A CHAT");
		let lastAct = null;
		for (const message of record.messages || []) {
			const text = String(message.content || "");
			if (message.kind === "thinking") {
				const node = this.el("think", "");
				node.textContent = text;
			} else if (message.kind === "action") {
				const act = this.el("act",
					'<div class="hd"><span class="tg">' + esc(message.tool || "tool") + "</span>" +
					'<span class="sm"></span><span class="st">RAN</span></div>' +
					'<pre class="hidden"></pre>');
				act.querySelector(".sm").textContent = text.split("\n")[0].slice(0, 120);
				act.querySelector("pre").textContent = text;
				act.querySelector(".hd").onclick = () =>
					act.querySelector("pre").classList.toggle("hidden");
				lastAct = act;
			} else if (message.kind === "observation") {
				if (lastAct) {
					lastAct.querySelector(".st").textContent = message.failed ? "FAIL" : "OK";
					if (message.failed) lastAct.classList.add("fail");
					lastAct.querySelector("pre").textContent = text;
					lastAct = null;
				} else {
					const act = this.el("act",
						'<div class="hd"><span class="tg">OUTPUT</span>' +
						'<span class="st">' + (message.failed ? "FAIL" : "OK") + "</span></div>" +
						"<pre></pre>");
					act.querySelector("pre").textContent = text;
				}
			} else if (message.role === "user") {
				this.el("msg user",
					'<div class="who">YOU</div><div class="body">' + esc(text) + "</div>");
			} else {
				this.el("msg",
					'<div class="who">AGENT</div><div class="body">' + md(text) + "</div>");
			}
		}
		this.log.scrollTop = 0;
	};

	Chat.prototype.setBusy = function (busy, label) {
		this.busy = busy;
		this.sendBtn.disabled = busy;
		this.stopBtn.disabled = !busy;
		this.status.textContent = label || (busy ? "WORKING" : "IDLE");
	};

	Chat.prototype.reset = function () {
		if (this.session) {
			fetch("/api/chat/reset", {
				method: "POST",
				headers: { "Content-Type": "application/json" },
				body: JSON.stringify({ session: this.session }),
			});
		}
		this.session = null;
		this.agentCard = null;
		this.historyCard = null;
		// a fresh chat gets the choice back
		this.chatId = "";
		this.pinned = false;
		this.paintStorage();
		this.log.innerHTML = "";
		this.el("notice", "NEW SESSION \u2014 BRIDGE PREAMBLE WILL BE RESENT");
	};

	Chat.prototype.send = async function (text) {
		if (this.busy || !text.trim()) return;
		this.el("msg user", '<div class="who">YOU</div><div class="body">' + esc(text) + "</div>");
		this.current = null;
		this.setBusy(true, "CONNECTING");

		this.controller = new AbortController();
		let response;
		try {
			response = await fetch("/api/chat", {
				method: "POST",
				headers: { "Content-Type": "application/json" },
				signal: this.controller.signal,
				body: JSON.stringify({
					session: this.session,
					message: text,
					attachments: this.getAttachments(),
					// only honoured for the first message of a chat
					storage: this.storage,
					chat: this.chatId,
				}),
			});
		} catch (err) {
			this.el("notice", "TRANSPORT ERROR: " + esc(err.message));
			this.setBusy(false);
			return;
		}
		if (!response.ok || !response.body) {
			const detail = await response.text().catch(() => "");
			// The server answers 409/503 with a startup report while notion2api is
			// still coming up; show the reason, not the whole json blob.
			let message = detail.slice(0, 300);
			try {
				const body = JSON.parse(detail);
				if (body.error) message = body.error + (body.hint ? " - " + body.hint : "");
			} catch (err) {
				/* not json: keep the raw text */
			}
			this.el("notice", "HTTP " + response.status + " " + esc(message));
			this.setBusy(false);
			return;
		}
		await this.consume(response.body.getReader());
		this.setBusy(false);
	};

	Chat.prototype.stop = function () {
		if (this.controller) this.controller.abort();
		this.setBusy(false, "STOPPED");
	};

	Chat.prototype.consume = async function (reader) {
		const decoder = new TextDecoder();
		let buffer = "";
		while (true) {
			let chunk;
			try {
				chunk = await reader.read();
			} catch (err) {
				break;
			}
			if (chunk.done) break;
			buffer += decoder.decode(chunk.value, { stream: true });
			const frames = buffer.split("\n\n");
			buffer = frames.pop();
			for (const frame of frames) {
				let event = "message", data = "{}";
				for (const line of frame.split("\n")) {
					if (line.startsWith("event:")) event = line.slice(6).trim();
					else if (line.startsWith("data:")) data = line.slice(5).trim();
				}
				try {
					this.handle(event, JSON.parse(data));
				} catch (err) { /* ignore malformed frame */ }
			}
		}
	};

	Chat.prototype.handle = function (event, data) {
		switch (event) {
			case "session":
				this.session = data.id;
				break;

			case "chat":
				// the backend decides and reports; it also pins the mode
				this.chatId = String(data.id || this.chatId);
				if (data.storage) this.storage = data.storage;
				this.pinStorage();
				break;

			case "status":
				this.setBusy(true, String(data.text || "").toUpperCase());
				break;

			case "token": {
				if (!this.current) {
					const node = this.el("msg", '<div class="who">AGENT</div><div class="body"></div>');
					this.current = { node, body: node.querySelector(".body"), raw: "" };
				}
				this.current.raw += data.text;
				// action blocks are runner instructions, not prose: hide them
				const visible = this.current.raw.replace(/```(?:action|tool)[\s\S]*?```/g, "").trim();
				this.current.body.innerHTML = md(visible);
				this.log.scrollTop = this.log.scrollHeight;
				break;
			}

			case "thinking": {
				if (!this._think) this._think = this.el("think", "");
				this._think.textContent += data.text;
				break;
			}

			case "retry":
				if (this.current) { this.current.node.remove(); this.current = null; }
				break;

			case "notice":
				this.el("notice", esc(String(data.text || "").toUpperCase()));
				break;

			case "action": {
				this.current = null;
				this._think = null;
				const node = this.el("act",
					'<div class="hd"><span class="tg">' + esc(data.tool) + "</span>" +
					'<span class="sm">' + esc(data.summary || "") + "</span>" +
					'<span class="st">RUN</span></div>' +
					'<pre class="hidden"></pre>');
				node.querySelector(".hd").onclick = () =>
					node.querySelector("pre").classList.toggle("hidden");
				this.acts[data.id] = node;
				break;
			}

			case "observation": {
				const node = this.acts[data.id];
				if (!node) break;
				node.querySelector(".st").textContent = data.ok ? "OK" : "FAIL";
				if (!data.ok) node.classList.add("fail");
				const pre = node.querySelector("pre");
				pre.textContent = data.text || "";
				if (!data.ok) pre.classList.remove("hidden");
				break;
			}

			case "file_changed":
				this.onFileChanged(data.path);
				break;

			case "approval":
				this.ask(data);
				break;

			case "error":
				this.el("notice", "ERROR: " + esc(data.message));
				break;

			case "done":
				this.setBusy(false, "DONE " + (data.rounds || "") + " ROUNDS");
				break;

			case "end":
				this.current = null;
				this._think = null;
				if (data && data.chat) this.chatId = String(data.chat);
				break;
		}
	};

	Chat.prototype.ask = function (data) {
		const node = this.el("ask",
			'<div class="q">APPROVAL REQUIRED \u2014 ' + esc(data.tool.toUpperCase()) + "</div>" +
			"<code>" + esc(data.summary) + "</code>" +
			'<div class="row">' +
			'<button class="btn solid" data-d="allow_once">ALLOW ONCE</button>' +
			'<button class="btn" data-d="allow_session">ALLOW SESSION</button>' +
			'<button class="btn" data-d="deny">DENY</button></div>');
		this.setBusy(true, "WAITING FOR APPROVAL");
		node.querySelectorAll("button").forEach((button) => {
			button.onclick = () => {
				fetch("/api/chat/approve", {
					method: "POST",
					headers: { "Content-Type": "application/json" },
					body: JSON.stringify({ session: this.session, decision: button.dataset.d }),
				});
				node.querySelector(".row").innerHTML =
					'<span class="muted">' + button.dataset.d.replace("_", " ").toUpperCase() + "</span>";
				this.setBusy(true, "WORKING");
			};
		});
	};

	global.Chat = Chat;
	global.mdRender = md;
	global.agentIdFromUrl = agentIdFromUrl;
})(window);
