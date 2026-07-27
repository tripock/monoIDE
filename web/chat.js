/* Agent panel: SSE client for /api/chat, activity log, approval prompts.
 *
 * The panel also owns the "who answers" switch: the default Notion AI assistant
 * or one of the user's own agents. Agents are workspace-local, so the only way
 * to know one is to have the user paste its link - nothing about it can be
 * shipped in the source.
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
		this.agent = { mode: "notion", url: "", name: "" };
		this.agentCard = null;
		this.initAgent();
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
			const up = ((state || {}).config || {}).upstream || {};
			const custom = String(up.agent_mode || "notion").toLowerCase() === "custom";
			this.agent = {
				mode: custom ? "custom" : "notion",
				url: String(up.agent_url || ""),
				name: String(up.agent_name || ""),
			};
		} catch (err) {
			/* keep the default; the button just shows NOTION AI */
		}
		this.paintAgent();
	};

	Chat.prototype.paintAgent = function () {
		if (!this.agentBtn) return;
		const custom = this.agent.mode === "custom" && this.agent.url;
		const label = custom ? (this.agent.name || "CUSTOM AGENT") : "NOTION AI";
		this.agentBtn.textContent = "AGENT: " + String(label).toUpperCase().slice(0, 22);
		this.agentBtn.title = custom
			? "custom agent: " + this.agent.url
			: "the default Notion AI assistant";
		this.agentBtn.classList.toggle("solid", !!custom);
	};

	Chat.prototype.pickAgent = function () {
		if (this.agentCard) {
			this.agentCard.remove();
			this.agentCard = null;
		}
		let mode = this.agent.mode;
		const node = this.el("ask",
			'<div class="q">WHO ANSWERS IN THIS IDE?</div>' +
			'<div class="row">' +
			'<button class="btn" data-m="notion">NOTION AI</button>' +
			'<button class="btn" data-m="custom">MY CUSTOM AGENT</button>' +
			"</div>" +
			'<div id="agent-fields">' +
			'<div class="row"><input class="in" id="agent-link" autocomplete="off"' +
			' placeholder="PASTE THE AGENT LINK (NOTION.SO/…)" value="' + attr(this.agent.url) + '"></div>' +
			'<div class="row"><input class="in" id="agent-name" autocomplete="off"' +
			' placeholder="LABEL FOR THE BUTTON (OPTIONAL)" value="' + attr(this.agent.name) + '"></div>' +
			"</div>" +
			'<div class="row">' +
			'<button class="btn solid" data-a="save">SAVE</button>' +
			'<button class="btn" data-a="cancel">CANCEL</button>' +
			"</div>" +
			'<div class="muted" id="agent-hint">OPEN THE AGENT IN NOTION AND COPY ITS LINK — ' +
			"AN AGENT ONLY EXISTS IN THE WORKSPACE THAT OWNS IT</div>");
		this.agentCard = node;

		const fields = node.querySelector("#agent-fields");
		const hint = node.querySelector("#agent-hint");
		const link = node.querySelector("#agent-link");
		const name = node.querySelector("#agent-name");
		const modeButtons = node.querySelectorAll("button[data-m]");

		const paint = () => {
			fields.style.display = mode === "custom" ? "" : "none";
			modeButtons.forEach((button) =>
				button.classList.toggle("solid", button.dataset.m === mode));
		};
		modeButtons.forEach((button) => {
			button.onclick = () => {
				mode = button.dataset.m;
				paint();
				if (mode === "custom") link.focus();
			};
		});
		paint();

		const close = () => {
			node.remove();
			this.agentCard = null;
		};

		const save = async () => {
			const url = link.value.trim();
			const label = name.value.trim();
			let agentId = "";
			if (mode === "custom") {
				agentId = agentIdFromUrl(url);
				if (!agentId) {
					hint.textContent = url
						? "NO AGENT ID IN THAT LINK — IT SHOULD CONTAIN A 32-CHARACTER ID"
						: "PASTE THE LINK TO YOUR AGENT FIRST";
					link.focus();
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
							"upstream.agent_url": mode === "custom" ? url : "",
							"upstream.agent_id": agentId,
							"upstream.agent_name": mode === "custom" ? label : "",
						},
					}),
				});
				if (!response.ok) throw new Error("HTTP " + response.status);
			} catch (err) {
				hint.textContent = "COULD NOT SAVE: " + String(err.message).toUpperCase();
				return;
			}
			this.agent = {
				mode,
				url: mode === "custom" ? url : "",
				name: mode === "custom" ? label : "",
			};
			this.paintAgent();
			close();
			// the bound Notion thread belongs to the previous assistant, so the
			// backend starts a fresh chat on the next message by itself
			this.el("notice", mode === "custom"
				? "CUSTOM AGENT SELECTED — A NEW NOTION CHAT STARTS WITH THE NEXT MESSAGE"
				: "BACK TO THE DEFAULT NOTION AI ASSISTANT");
		};

		node.querySelector('button[data-a="save"]').onclick = save;
		node.querySelector('button[data-a="cancel"]').onclick = close;
		link.onkeydown = (event) => {
			if (event.key === "Enter") { event.preventDefault(); save(); }
		};
		name.onkeydown = link.onkeydown;
		if (mode === "custom") link.focus();
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
		this.log.innerHTML = "";
		this.el("notice", "NEW SESSION — BRIDGE PREAMBLE WILL BE RESENT");
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
				break;
		}
	};

	Chat.prototype.ask = function (data) {
		const node = this.el("ask",
			'<div class="q">APPROVAL REQUIRED — ' + esc(data.tool.toUpperCase()) + "</div>" +
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
