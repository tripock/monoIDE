"""Send requests to a custom Notion agent instead of the default assistant.

What Notion actually wants
--------------------------
A captured browser request to a custom agent shows two things, and one alone is
not enough - with only the first, the default assistant answers:

1. the config step of the transcript::

     {"type": "workflow", "model": "agave-flan",
      "workflowId": "3a14797c-...-9bb113bd",
      "isCustomAgent": true, "useCustomAgentDraft": true,
      "enableCustomAgents": true, ...}

2. the thread the answer is written into is parented by that workflow::

     "threadParentPointer": {"table": "workflow", "id": "3a14797c-...", ...}

   (the resulting thread record carries parent_table "workflow",
   data.workflow_id = the same id and created_source "custom_agent")

So a custom agent is a *workflow* record: its id belongs in ``workflowId``, and
there is no "agentId" field at all. notion2api hardcodes
``isCustomAgent: False`` and parents every thread to the space, which is exactly
why the default assistant answers.

How the choice gets here
------------------------
monoIDE writes it into a small json file (``ide/config.py``,
``write_agent_target``); an agent only exists in the workspace that owns it, so
nothing can be baked into the source. Two hooks are installed from
``app/api/__init__.py``:

* the transcript builders, so every config block carries the workflow id;
* ``cloudscraper.create_scraper``, so the outgoing runInferenceTranscript
  payload gets the workflow thread parent - that payload is a local variable
  inside ``notion_client.stream_response`` and the transport is the only seam.

With the file missing or set to "notion", nothing is touched at all.
"""

from __future__ import annotations

import json
import os
import re
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict

TARGET_FILENAME = "agent-target.json"
# the file is re-read at most this often; the IDE rewrites it between messages
CACHE_SECONDS = 1.0

# the dict that carries this key is the transcript's config block
CONFIG_MARKER = "isCustomAgent"

_UUID = re.compile(
    r"[0-9a-fA-F]{8}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{12}"
)

_lock = threading.Lock()
_cache: Dict[str, Any] = {"read_at": 0.0, "agent": {}}
_installed = False


def state_dir() -> Path:
    """The folder monoIDE keeps its state in. Mirrors ide/config.py."""
    override = os.environ.get("MONOIDE_STATE_DIR")
    if override:
        return Path(override)
    if sys.platform.startswith("win"):
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
        return Path(base) / "monoide"
    if sys.platform == "darwin":
        return Path(os.path.expanduser("~/Library/Application Support/monoide"))
    base = os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state")
    return Path(base) / "monoide"


def target_path() -> Path:
    override = os.environ.get("MONOIDE_AGENT_FILE")
    return Path(override) if override else state_dir() / TARGET_FILENAME


def normalize_id(raw: str) -> str:
    """Accept a link, a dashed id or a dashless id; answer a dashed uuid."""
    text = str(raw or "").split("?")[0].split("#")[0]
    found = _UUID.findall(text)
    if not found:
        return ""
    digits = re.sub(r"[^0-9a-fA-F]", "", found[-1]).lower()
    if len(digits) != 32:
        return ""
    return "-".join(
        [digits[:8], digits[8:12], digits[12:16], digits[16:20], digits[20:]]
    )


def current_agent() -> Dict[str, str]:
    """The selected custom agent, or {} when the default assistant is wanted."""
    now = time.monotonic()
    with _lock:
        if now - float(_cache["read_at"]) < CACHE_SECONDS:
            return dict(_cache["agent"])
        agent: Dict[str, str] = {}
        try:
            raw = json.loads(target_path().read_text("utf-8"))
            if str(raw.get("mode") or "notion").strip().lower() == "custom":
                agent_id = normalize_id(
                    raw.get("agent_id") or raw.get("agent_url") or ""
                )
                if agent_id:
                    agent = {
                        "id": agent_id,
                        "url": str(raw.get("agent_url") or ""),
                        "name": str(raw.get("agent_name") or ""),
                    }
        except Exception:
            # no file yet, unreadable json, whatever: fall back to the default
            agent = {}
        _cache["read_at"] = now
        _cache["agent"] = agent
        return dict(agent)


def apply_to(payload: Any, agent: Dict[str, str]) -> int:
    """Point every config block inside ``payload`` at the custom agent.

    The walk is shape-agnostic on purpose: lite, standard and heavy mode each
    build their transcript differently, but all of them contain the one dict
    that carries ``isCustomAgent``.

    ``type`` is left alone. Heavy mode already uses "workflow", which is what a
    custom agent needs, while forcing it in markdown-chat mode would break the
    Gemini models that depend on that thread type.
    """
    agent_id = agent.get("id") or ""
    if not agent_id:
        return 0
    patched = 0
    seen = set()
    stack = [payload]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            if id(node) in seen:
                continue
            seen.add(id(node))
            if CONFIG_MARKER in node:
                node["isCustomAgent"] = True
                node["enableCustomAgents"] = True
                node["useCustomAgentDraft"] = True
                node["workflowId"] = agent_id
                # never existed; an early guess of ours, kept out of the payload
                node.pop("agentId", None)
                patched += 1
            stack.extend(node.values())
        elif isinstance(node, (list, tuple)):
            stack.extend(node)
    return patched


def retarget_request(payload: Dict[str, Any], agent: Dict[str, str]) -> None:
    """Make a runInferenceTranscript payload belong to the custom agent.

    Without the workflow thread parent Notion accepts the request happily and
    answers with the default assistant, so this half is not optional.
    """
    agent_id = agent.get("id") or ""
    if not agent_id:
        return
    payload["threadParentPointer"] = {
        "table": "workflow",
        "id": agent_id,
        "spaceId": payload.get("spaceId"),
    }
    apply_to(payload.get("transcript"), agent)


def _log(message: str, **fields: Any) -> None:
    try:
        from app.logger import logger

        logger.info(message, extra={"request_info": fields})
    except Exception:
        pass


# ---------------------------------------------------------------------------
# hook 1: transcript builders
# ---------------------------------------------------------------------------

def _wrap_builder(builder):
    def hook(*args: Any, **kwargs: Any):
        transcript = builder(*args, **kwargs)
        agent = current_agent()
        if agent:
            patched = apply_to(transcript, agent)
            _log(
                "Transcript config pointed at a custom agent",
                event="custom_agent_transcript",
                builder=getattr(builder, "__name__", "builder"),
                workflow_id=agent["id"],
                blocks_patched=patched,
            )
        return transcript

    hook._agent_hook = True  # type: ignore[attr-defined]
    hook.__name__ = getattr(builder, "__name__", "builder")
    return hook


def _install_builders() -> None:
    from app import conversation as conv

    manager = getattr(conv, "ConversationManager", None)
    if manager is not None:
        original = manager.get_transcript_payload
        if not getattr(original, "_agent_hook", False):

            def payload_hook(self, *args: Any, **kwargs: Any):
                result = original(self, *args, **kwargs)
                agent = current_agent()
                if agent and isinstance(result, dict):
                    patched = apply_to(result.get("transcript"), agent)
                    _log(
                        "Transcript config pointed at a custom agent",
                        event="custom_agent_transcript",
                        builder="get_transcript_payload",
                        workflow_id=agent["id"],
                        blocks_patched=patched,
                    )
                return result

            payload_hook._agent_hook = True  # type: ignore[attr-defined]
            manager.get_transcript_payload = payload_hook

    for name in ("build_lite_transcript", "build_standard_transcript"):
        builder = getattr(conv, name, None)
        if builder is None or getattr(builder, "_agent_hook", False):
            continue
        setattr(conv, name, _wrap_builder(builder))


# ---------------------------------------------------------------------------
# hook 2: the outgoing request
# ---------------------------------------------------------------------------

class _AgentAwareScraper:
    """Pass-through proxy that retargets runInferenceTranscript payloads.

    Everything except ``post`` is forwarded untouched, and ``post`` only edits
    the payload while a custom agent is selected.
    """

    def __init__(self, inner: Any):
        self._inner = inner

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    def post(self, *args: Any, **kwargs: Any):
        payload = kwargs.get("json")
        if isinstance(payload, dict) and "transcript" in payload:
            agent = current_agent()
            if agent:
                retarget_request(payload, agent)
                _log(
                    "Request retargeted to a custom agent workflow",
                    event="custom_agent_request",
                    workflow_id=agent["id"],
                    thread_id=payload.get("threadId"),
                    thread_type=payload.get("threadType"),
                    create_thread=payload.get("createThread"),
                )
        return self._inner.post(*args, **kwargs)


def _install_transport() -> None:
    import cloudscraper

    original = cloudscraper.create_scraper
    if getattr(original, "_agent_hook", False):
        return

    def create_scraper_hook(*args: Any, **kwargs: Any):
        return _AgentAwareScraper(original(*args, **kwargs))

    create_scraper_hook._agent_hook = True  # type: ignore[attr-defined]
    # notion_client calls cloudscraper.create_scraper() lazily - at init and
    # again when a 403 forces a rebuild - so both paths get the proxy
    cloudscraper.create_scraper = create_scraper_hook


def install() -> None:
    """Install both hooks. Safe to call more than once."""
    global _installed
    if _installed:
        return
    _installed = True
    _install_builders()
    _install_transport()
