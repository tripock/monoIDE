"""Agent loop: notion2api client + text-protocol tool calling + refusal repair.

The loop emits events (dicts) that the HTTP layer forwards to the browser as
Server-Sent Events, so the UI can stream tokens and show the activity log.

Event kinds:
  token       {text}
  thinking    {text}
  action      {id, tool, args, summary}
  observation {id, tool, ok, text}
  approval    {id, tool, args, summary}   -> loop pauses, UI must answer
  notice      {text}                      -> e.g. "refusal repaired"
  status      {text}
  done        {rounds}
  error       {message}

One IDE session = one Notion chat
---------------------------------
Every round of the loop is a separate HTTP request to notion2api. notion2api
only keeps a Notion thread alive across requests in "heavy" app mode, and only
when the caller replays the conversation_id it handed out in the
X-Conversation-Id response header. Without that, one user message turns into a
dozen chats in Notion - one per tool round. Upstream therefore remembers the
conversation id and Session feeds it back on every round.

Heavy mode caveat: notion2api rebuilds the Notion transcript from its own
sqlite sliding window and forwards only the LAST message of the request
(conversation.get_transcript_payload(new_prompt=...)). Anything we put earlier
in the list - including the runner preamble - is dropped. _payload_messages()
therefore folds the preamble into the outgoing last user message.
"""

from __future__ import annotations

import json
import re
import threading
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional

from . import prompts, tools as tools_mod
from .config import describe_environment
from .tools import ApprovalRequired, ToolContext, ToolError

ACTION_BLOCK = re.compile(r"```(?:action|tool|json:action)\s*\n(.*?)```", re.DOTALL)

# notion2api appends its web-search footer as markdown quote lines when it
# thinks the client cannot render metadata ("> 🔍 已搜索" / "> 🌐 来源:").
# We ask for the rich protocol instead, but old replies and other upstreams can
# still carry it, so strip it defensively.
SOURCE_FOOTER = re.compile(r"(?m)^[ \t]*>[ \t]*(?:🌐|🔍)[^\n]*\n?")
SOURCE_FOOTER_ITEM = re.compile(r"(?m)^[ \t]*>[ \t]*\d+\.[ \t]*\[[^\]]*\]\([^)]*\)[ \t]*\n?")


def strip_source_footer(text: str) -> str:
    cleaned = SOURCE_FOOTER.sub("", text or "")
    cleaned = SOURCE_FOOTER_ITEM.sub("", cleaned)
    return cleaned


# ---------------------------------------------------------------------------
# upstream (notion2api, OpenAI-compatible)
# ---------------------------------------------------------------------------

class Upstream:
    def __init__(self, config):
        self.config = config
        # filled from the X-Conversation-Id response header; replayed on the
        # next request so Notion keeps writing into the same chat
        self.conversation_id = ""

    def _url(self, suffix: str) -> str:
        base = str(self.config.get("upstream", "base_url", default="")).rstrip("/")
        return f"{base}/{suffix.lstrip('/')}"

    def _headers(self) -> Dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
            # we understand notion2api's rich events, so it must not inline the
            # "🌐 来源" markdown footer into the assistant's answer
            "X-Client-Type": "web",
        }
        key = self.config.get("upstream", "api_key", default="")
        if key:
            headers["Authorization"] = f"Bearer {key}"
        return headers

    def models(self) -> List[str]:
        request = urllib.request.Request(self._url("models"), headers=self._headers())
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except Exception:
            return []
        return [item.get("id") for item in payload.get("data", []) if item.get("id")]

    def stream(self, messages: List[Dict[str, str]], model: str,
               conversation_id: str = "") -> Iterator[Dict[str, str]]:
        """Yield {'type': 'content'|'thinking'|'replace', 'text': ...} chunks."""
        payload: Dict[str, Any] = {
            "model": model,
            "messages": messages,
            "stream": bool(self.config.get("upstream", "stream", default=True)),
        }
        reuse = bool(self.config.get("upstream", "reuse_conversation", default=True))
        if reuse and conversation_id:
            payload["conversation_id"] = conversation_id
        body = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            self._url("chat/completions"), data=body, headers=self._headers(), method="POST"
        )
        timeout = float(self.config.get("upstream", "timeout", default=300))
        try:
            response = urllib.request.urlopen(request, timeout=timeout)
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:800]
            raise RuntimeError(f"upstream HTTP {exc.code}: {detail}")
        except Exception as exc:
            raise RuntimeError(f"cannot reach notion2api at {self._url('chat/completions')}: {exc}")

        # heavy mode answers with the conversation it used; remember it even
        # when it differs from what we asked for (expired / unknown ids)
        handed_out = response.headers.get("X-Conversation-Id") or ""
        if handed_out:
            self.conversation_id = handed_out

        content_type = response.headers.get("Content-Type", "")
        if "text/event-stream" not in content_type:
            payload_json = json.loads(response.read().decode("utf-8", "replace"))
            if payload_json.get("error"):
                raise RuntimeError(str(payload_json["error"]))
            text = payload_json["choices"][0]["message"].get("content") or ""
            yield {"type": "content", "text": text}
            return

        for raw in response:
            line = raw.decode("utf-8", "replace").strip()
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                break
            try:
                chunk = json.loads(data)
            except json.JSONDecodeError:
                continue
            if chunk.get("error"):
                raise RuntimeError(str(chunk["error"]))

            # notion2api's own event types (sent because of X-Client-Type: web)
            event = str(chunk.get("type") or "")
            if event == "thinking_chunk":
                text = chunk.get("text") or ""
                if text:
                    yield {"type": "thinking", "text": text}
                continue
            if event == "content_replace":
                yield {"type": "replace", "text": chunk.get("content") or ""}
                continue
            if event in ("search_metadata", "thinking_replace"):
                # search sources and rewritten reasoning are UI sugar we do not
                # want inside the transcript the agent reasons over
                continue

            for choice in chunk.get("choices") or []:
                delta = choice.get("delta") or {}
                if delta.get("reasoning_content") or delta.get("thinking"):
                    yield {"type": "thinking",
                           "text": delta.get("reasoning_content") or delta.get("thinking")}
                if delta.get("content"):
                    yield {"type": "content", "text": delta["content"]}


# ---------------------------------------------------------------------------
# parsing
# ---------------------------------------------------------------------------

def parse_actions(text: str) -> List[Dict[str, Any]]:
    actions: List[Dict[str, Any]] = []
    for match in ACTION_BLOCK.finditer(text or ""):
        raw = match.group(1).strip()
        if not raw:
            continue
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            payload = _salvage_json(raw)
            if payload is None:
                actions.append({"__parse_error__": raw[:400]})
                continue
        if isinstance(payload, list):
            actions.extend(item for item in payload if isinstance(item, dict))
        elif isinstance(payload, dict):
            actions.append(payload)
    return actions


def _salvage_json(raw: str) -> Optional[Any]:
    # tolerate trailing commas and stray prose around the object
    cleaned = re.sub(r",\s*([}\]])", r"\1", raw)
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start >= 0 and end > start:
        try:
            return json.loads(cleaned[start:end + 1])
        except json.JSONDecodeError:
            return None
    return None


def strip_actions(text: str) -> str:
    return ACTION_BLOCK.sub("", text or "").strip()


def summarize(action: Dict[str, Any]) -> str:
    tool = action.get("tool", "?")
    if tool == "bash":
        return f"$ {str(action.get('command', ''))[:160]}"
    if tool == "mcp_call":
        return f"{action.get('server', '?')}.{action.get('tool_name') or action.get('name') or ''}"
    if tool == "grep":
        return f"grep {action.get('pattern', '')}"
    if tool == "glob":
        return f"glob {action.get('pattern', '')}"
    return str(action.get("path", "")) or tool


# ---------------------------------------------------------------------------
# session
# ---------------------------------------------------------------------------

class Session:
    """One chat session = one message history + pending approval state."""

    def __init__(self, session_id: str, config, root: Path, mcp=None, lsp=None):
        self.id = session_id
        self.config = config
        self.root = root
        self.mcp = mcp
        self.lsp = lsp
        self.messages: List[Dict[str, str]] = []
        self.approvals: set = set()
        self.title = ""
        self.created = time.time()
        self.updated = time.time()
        # notion2api conversation id: the whole session maps to one Notion chat
        self.conversation_id = ""
        self.lock = threading.Lock()
        self._decision: Optional[str] = None
        self._decision_event = threading.Event()
        self.busy = False

    # -- approvals ---------------------------------------------------------
    def answer_approval(self, decision: str) -> None:
        self._decision = decision
        self._decision_event.set()

    def _wait_for_approval(self) -> str:
        self._decision_event.clear()
        self._decision = None
        # block until the UI answers (or the browser gives up)
        self._decision_event.wait(timeout=900)
        return self._decision or "deny"

    # -- prompt ------------------------------------------------------------
    def _ensure_preamble(self) -> None:
        if self.messages:
            return
        env = describe_environment(self.root)
        tools_doc = tools_mod.TOOLS_DOC
        if self.mcp is not None:
            status = self.mcp.status()
            if status:
                names = ", ".join(row["name"] for row in status)
                tools_doc += f"\n\nMCP servers configured: {names} (use mcp_list to see their tools)"
        self.messages.append({
            "role": "user",
            "content": prompts.build_preamble(
                root=str(self.root),
                os_name=env["os"],
                shell=env["shell"],
                git=env["git"],
                tools_doc=tools_doc,
                tree=self._root_snapshot(),
            ),
        })

    def _root_snapshot(self, limit: int = 60) -> str:
        """A short, real listing of the project root.

        Starting the conversation with actual data (instead of assertions about
        a "bridge") is what keeps the model from treating the setup message as a
        prompt injection.
        """
        skip = {".git", "node_modules", "__pycache__", ".venv", "venv", ".build", "dist", "build"}
        rows: List[str] = []
        try:
            entries = sorted(
                self.root.iterdir(), key=lambda p: (p.is_file(), p.name.lower())
            )
        except Exception:
            return ""
        for entry in entries:
            if entry.name.startswith(".") and entry.name not in {".env.example"}:
                continue
            if entry.name in skip:
                continue
            rows.append("- %s%s" % (entry.name, "/" if entry.is_dir() else ""))
            if len(rows) >= limit:
                rows.append("- ... (more)")
                break
        return "\n".join(rows)

    def _trim(self) -> None:
        """Keep the context inside a character budget without losing the preamble."""
        budget = int(self.config.get("agent", "history_char_budget", default=90000))
        total = sum(len(m["content"]) for m in self.messages)
        index = 1  # never drop message 0 (the bridge preamble)
        while total > budget and index < len(self.messages) - 6:
            total -= len(self.messages[index]["content"])
            self.messages.pop(index)

    # -- request payload ---------------------------------------------------
    def _heavy_mode(self) -> bool:
        mode = str(self.config.get("upstream", "app_mode", default="heavy") or "heavy")
        return mode.strip().lower() == "heavy"

    def _payload_messages(self) -> List[Dict[str, str]]:
        """Messages to actually send upstream.

        In heavy mode notion2api ignores everything except the last message: it
        rebuilds the Notion transcript from its own sqlite window and passes our
        final message as `new_prompt`. The preamble (message 0) would silently
        disappear, leaving the model with just the turn reminder - it then has no
        idea it is driving a local editor and no tool protocol to follow.
        So the preamble rides along with the last user message instead.
        """
        messages = self.messages
        if not self._heavy_mode() or len(messages) < 2:
            return messages
        preamble = messages[0]["content"]
        last = messages[-1]
        if last["role"] != "user" or not preamble:
            return messages
        if preamble in last["content"]:
            return messages
        merged = f"{preamble}\n\n---\n\n{last['content']}"
        return messages[:-1] + [{"role": "user", "content": merged}]

    # -- main loop ---------------------------------------------------------
    def run(self, user_text: str, *, attachments: Optional[List[str]] = None,
            emit: Callable[[str, Dict[str, Any]], None]) -> None:
        self.busy = True
        try:
            self._run(user_text, attachments or [], emit)
        except Exception as exc:  # keep the UI informed instead of dying silently
            emit("error", {"message": str(exc)})
        finally:
            self.busy = False
            self.updated = time.time()

    def _run(self, user_text: str, attachments: List[str],
             emit: Callable[[str, Dict[str, Any]], None]) -> None:
        self._ensure_preamble()
        if not self.title:
            self.title = user_text.strip().splitlines()[0][:60] if user_text.strip() else "session"

        context_blocks = []
        for rel in attachments:
            try:
                path = (self.root / rel).resolve()
                text = path.read_text("utf-8", errors="replace")
                context_blocks.append(f"[open editor buffer: {rel}]\n{text}")
            except Exception:
                continue

        turn = user_text
        if context_blocks:
            turn = "\n\n".join(context_blocks) + "\n\n[user request]\n" + user_text
        self.messages.append({"role": "user", "content": f"{prompts.TURN_REMINDER}\n\n{turn}"})

        model = self.config.get("upstream", "model", default="claude-sonnet4.6")
        upstream = Upstream(self.config)
        upstream.conversation_id = self.conversation_id
        max_rounds = int(self.config.get("agent", "max_rounds", default=24))
        repairs_left = int(self.config.get("agent", "max_repairs", default=2)) \
            if self.config.get("agent", "repair_refusals", default=True) else 0

        ctx = ToolContext(
            root=self.root,
            config=self.config,
            mcp=self.mcp,
            lsp=self.lsp,
            auto_approve=bool(self.config.get("agent", "auto_approve", default=False)),
            approvals=self.approvals,
            on_event=lambda kind, data: emit(kind, data),
        )

        for round_index in range(1, max_rounds + 1):
            self._trim()
            emit("status", {"text": f"round {round_index}"})
            assistant_text = ""
            for chunk in upstream.stream(self._payload_messages(), model, self.conversation_id):
                if chunk["type"] == "thinking":
                    emit("thinking", {"text": chunk["text"]})
                    continue
                if chunk["type"] == "replace":
                    # notion2api decided its final answer diverged from what it
                    # streamed; only useful when nothing was shown yet
                    if not assistant_text.strip() and chunk["text"]:
                        assistant_text = chunk["text"]
                        emit("token", {"text": chunk["text"]})
                    continue
                assistant_text += chunk["text"]
                emit("token", {"text": chunk["text"]})

            # one Notion chat per session: keep the id it answered with
            if upstream.conversation_id and upstream.conversation_id != self.conversation_id:
                self.conversation_id = upstream.conversation_id

            assistant_text = strip_source_footer(assistant_text)
            actions = parse_actions(assistant_text)

            # Refusal repair: the model answered about itself instead of working.
            if not actions and repairs_left > 0 and prompts.looks_like_refusal(assistant_text):
                repairs_left -= 1
                emit("notice", {"text": "clarifying the setup and asking again"})
                emit("retry", {})
                self.messages.append({"role": "assistant", "content": assistant_text})
                self.messages.append({"role": "user", "content": prompts.REPAIR_MESSAGE})
                continue

            self.messages.append({"role": "assistant", "content": assistant_text})

            if not actions:
                emit("done", {"rounds": round_index})
                return

            observations: List[str] = []
            for action in actions:
                if "__parse_error__" in action:
                    observations.append(
                        "ERROR: an ```action block was not valid JSON. Resend it as a "
                        "single JSON object.\noffending block:\n" + action["__parse_error__"]
                    )
                    continue
                name = str(action.get("tool") or "")
                args = {k: v for k, v in action.items() if k != "tool"}
                if name == "mcp_call":
                    args["tool"] = action.get("tool_name") or action.get("name")
                handler = tools_mod.REGISTRY.get(name)
                if handler is None:
                    observations.append(
                        f"ERROR: unknown tool {name!r}. Available: "
                        + ", ".join(sorted(tools_mod.REGISTRY))
                    )
                    continue

                action_id = uuid.uuid4().hex[:8]
                emit("action", {"id": action_id, "tool": name, "args": args,
                               "summary": summarize({**args, "tool": name})})
                text, ok = self._execute(ctx, handler, name, args, action_id, emit)
                emit("observation", {"id": action_id, "tool": name, "ok": ok,
                                    "text": text[:4000]})
                observations.append(f"[{name}] {text}")

            limit = int(self.config.get("agent", "observation_char_limit", default=24000))
            joined = "\n\n".join(observations)
            if len(joined) > limit:
                joined = joined[:limit] + f"\n[... truncated, {len(joined) - limit} chars omitted]"
            self.messages.append({
                "role": "user",
                "content": f"{prompts.OBSERVATION_HEADER}\n\n{joined}",
            })

        emit("error", {"message": f"stopped after {max_rounds} rounds (agent.max_rounds)"})

    def _execute(self, ctx: ToolContext, handler, name: str, args: Dict[str, Any],
                 action_id: str, emit) -> tuple[str, bool]:
        while True:
            try:
                return handler(ctx, args), True
            except ApprovalRequired as need:
                emit("approval", {"id": action_id, "tool": name, "args": args,
                                  "summary": need.summary})
                decision = self._wait_for_approval()
                if decision == "allow_once":
                    ctx.auto_approve = True
                    try:
                        result = handler(ctx, args)
                        return result, True
                    finally:
                        ctx.auto_approve = bool(
                            self.config.get("agent", "auto_approve", default=False)
                        )
                if decision == "allow_session":
                    kind = "bash" if name == "bash" else ("mcp" if name == "mcp_call" else "write")
                    self.approvals.add(kind)
                    continue
                return (
                    "BLOCKED: the user declined this action. Do not retry it. "
                    "Explain what you need, or propose a different approach.",
                    False,
                )
            except ToolError as exc:
                return f"ERROR: {exc}", False
            except Exception as exc:  # noqa: BLE001 - surface real failures
                return f"ERROR: {type(exc).__name__}: {exc}", False
