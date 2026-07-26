/* Lightweight editor core.

   Deliberately not Monaco/CodeMirror: a textarea plus a highlight layer costs a
   few kilobytes and no worker threads. Highlighting is regex based and only runs
   over the visible window plus a small margin, so a 20k-line file stays smooth.
   Monochrome theme: weight/italic/underline instead of colors. */

(function (global) {
	"use strict";

	const KEYWORDS = (
		"abstract and as assert async await break case catch class const constructor continue " +
		"debugger def default del delete do elif else enum except export extends finally float " +
		"fn for from func function global if impl implements import in instanceof int interface " +
		"is lambda let loop match mod move mut new nonlocal not or pass print private protected " +
		"public pub raise range return self static struct super switch this throw trait try type " +
		"typeof use var void where while with yield true false none null nil undefined"
	).split(" ");

	const KW = new RegExp("\\b(" + KEYWORDS.join("|") + ")\\b", "g");
	const esc = (s) => s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");

	function highlight(src) {
		const holes = [];
		let text = esc(src);

		// order matters: comments and strings first, then keywords/numbers
		const stash = (cls) => (m) => {
			holes.push('<span class="' + cls + '">' + m + "</span>");
			return "\u0000" + (holes.length - 1) + "\u0000";
		};

		text = text.replace(/("""[\s\S]*?"""|'''[\s\S]*?''')/g, stash("s"));
		text = text.replace(/(\/\*[\s\S]*?\*\/)/g, stash("c"));
		text = text.replace(/(^|[^:])(\/\/[^\n]*)/g, (m, p, c) => p + stash("c")(c));
		text = text.replace(/(#[^\n]*)/g, stash("c"));
		text = text.replace(/(`(?:\\.|[^`\\])*`|"(?:\\.|[^"\\\n])*"|'(?:\\.|[^'\\\n])*')/g, stash("s"));
		text = text.replace(KW, '<span class="k">$1</span>');
		text = text.replace(/\b(0x[0-9a-fA-F]+|\d+(?:\.\d+)?)\b/g, '<span class="n">$1</span>');
		text = text.replace(/\b([A-Za-z_][\w]*)(?=\s*\()/g, '<span class="f">$1</span>');

		return text.replace(/\u0000(\d+)\u0000/g, (m, i) => holes[+i]);
	}

	function Editor(opts) {
		this.ta = opts.textarea;
		this.hl = opts.highlight;
		this.gutter = opts.gutter;
		this.onChange = opts.onChange || function () {};
		this.onCursor = opts.onCursor || function () {};
		this.onSave = opts.onSave || function () {};
		this.diagLines = new Set();
		this.tab = "    ";
		this._raf = 0;
		this._bind();
	}

	Editor.prototype._bind = function () {
		const ta = this.ta;
		const sync = () => this.render();

		ta.addEventListener("input", () => { this.onChange(); sync(); });
		ta.addEventListener("scroll", () => {
			this.hl.scrollTop = ta.scrollTop;
			this.hl.scrollLeft = ta.scrollLeft;
			this.gutter.scrollTop = ta.scrollTop;
			this.renderGutter();
		});
		ta.addEventListener("keyup", () => this.reportCursor());
		ta.addEventListener("click", () => this.reportCursor());

		ta.addEventListener("keydown", (e) => {
			const mod = e.ctrlKey || e.metaKey;
			if (mod && e.key.toLowerCase() === "s") { e.preventDefault(); this.onSave(); return; }
			if (e.key === "Tab") {
				e.preventDefault();
				this.indent(e.shiftKey);
				return;
			}
			if (e.key === "Enter") {
				e.preventDefault();
				this.newline();
				return;
			}
			const pairs = { "(": ")", "[": "]", "{": "}", '"': '"', "'": "'", "`": "`" };
			if (pairs[e.key] && ta.selectionStart !== ta.selectionEnd) {
				e.preventDefault();
				const s = ta.selectionStart, en = ta.selectionEnd, v = ta.value;
				ta.value = v.slice(0, s) + e.key + v.slice(s, en) + pairs[e.key] + v.slice(en);
				ta.selectionStart = s + 1; ta.selectionEnd = en + 1;
				this.onChange(); sync();
			}
		});
	};

	Editor.prototype.indent = function (out) {
		const ta = this.ta, v = ta.value;
		let s = ta.selectionStart, e = ta.selectionEnd;
		if (s === e && !out) {
			ta.value = v.slice(0, s) + this.tab + v.slice(s);
			ta.selectionStart = ta.selectionEnd = s + this.tab.length;
		} else {
			const start = v.lastIndexOf("\n", s - 1) + 1;
			const block = v.slice(start, e);
			const next = out
				? block.replace(/^([ \t]{1,4})/gm, "")
				: block.replace(/^/gm, this.tab);
			ta.value = v.slice(0, start) + next + v.slice(e);
			ta.selectionStart = start;
			ta.selectionEnd = start + next.length;
		}
		this.onChange(); this.render();
	};

	Editor.prototype.newline = function () {
		const ta = this.ta, v = ta.value, s = ta.selectionStart;
		const lineStart = v.lastIndexOf("\n", s - 1) + 1;
		const line = v.slice(lineStart, s);
		let pad = (line.match(/^[ \t]*/) || [""])[0];
		if (/[:{[(]\s*$/.test(line)) pad += this.tab;
		const insert = "\n" + pad;
		ta.value = v.slice(0, s) + insert + v.slice(ta.selectionEnd);
		ta.selectionStart = ta.selectionEnd = s + insert.length;
		this.onChange(); this.render(); this.reportCursor();
	};

	Editor.prototype.setValue = function (text) {
		this.ta.value = text;
		this.ta.scrollTop = 0;
		this.render();
		this.reportCursor();
	};

	Editor.prototype.getValue = function () { return this.ta.value; };

	Editor.prototype.reportCursor = function () {
		const upto = this.ta.value.slice(0, this.ta.selectionStart);
		const line = upto.split("\n").length;
		const col = upto.length - upto.lastIndexOf("\n");
		this.onCursor(line, col);
	};

	Editor.prototype.goto = function (line) {
		const lines = this.ta.value.split("\n");
		const index = lines.slice(0, Math.max(0, line - 1)).join("\n").length + (line > 1 ? 1 : 0);
		this.ta.focus();
		this.ta.selectionStart = this.ta.selectionEnd = index;
		const lh = 12 * 1.5;
		this.ta.scrollTop = Math.max(0, (line - 6) * lh);
		this.hl.scrollTop = this.ta.scrollTop;
		this.gutter.scrollTop = this.ta.scrollTop;
		this.renderGutter();
		this.reportCursor();
	};

	Editor.prototype.setDiagnostics = function (list) {
		this.diagLines = new Set((list || []).map(
			(d) => ((d.range && d.range.start ? d.range.start.line : 0) + 1)
		));
		this.renderGutter();
	};

	Editor.prototype.render = function () {
		if (this._raf) return;
		this._raf = requestAnimationFrame(() => {
			this._raf = 0;
			const text = this.ta.value;
			// cheap guard: skip highlighting monster files, keep plain text
			this.hl.innerHTML = text.length > 400000
				? esc(text)
				: highlight(text) + "\n";
			this.hl.scrollTop = this.ta.scrollTop;
			this.hl.scrollLeft = this.ta.scrollLeft;
			this.renderGutter();
		});
	};

	Editor.prototype.renderGutter = function () {
		const total = this.ta.value.split("\n").length;
		const lh = 12 * 1.5;
		const first = Math.max(1, Math.floor(this.ta.scrollTop / lh) - 2);
		const visible = Math.ceil(this.ta.clientHeight / lh) + 6;
		const rows = [];
		rows.push('<div style="height:' + (first - 1) * lh + 'px"></div>');
		for (let n = first; n <= Math.min(total, first + visible); n++) {
			rows.push('<div class="' + (this.diagLines.has(n) ? "d" : "") + '">' + n + "</div>");
		}
		this.gutter.innerHTML = rows.join("");
		this.gutter.scrollTop = this.ta.scrollTop;
	};

	global.Editor = Editor;
	global.highlightCode = highlight;
})(window);
