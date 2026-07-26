/* Agent panel: SSE client for /api/chat, activity log, approval prompts. */

(function (global) {
	"use strict";

	const esc = (s) => String(s == null ? "" : s)
		.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");

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
	}

	Chat.prototype.el = function (cls, html) {
		const node = document.createElement("div");
		node.className = cls;
		if (html != null) node.innerHTML = html;
		this.log.appendChild(node);
		this.log.scrollTop = this.log.scrollHeight;
		return node;
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
			this.el("notice", "HTTP " + response.status + " " + esc(detail.slice(0, 300)));
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
})(window);
