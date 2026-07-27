"""Send requests to a custom Notion agent instead of the default assistant.

Why this file exists
--------------------
notion2api always talks to the default Notion assistant: the config block it
puts at the top of every transcript carries ``isCustomAgent: False``. monoIDE
lets the user paste a link to one of their own agents, and a custom agent only
exists inside the workspace that owns it, so no id can ever be baked into the
source.

monoIDE writes the picked agent into a small json file (``ide/config.py``,
``write_agent_target``). Every transcript built in this process is walked and,
when a custom agent is selected, the config block is rewritten to point at it.
When the file is missing or says ``notion``, nothing is touched at all and the
behaviour is exactly what it was before.

The hook is installed from ``app/api/__init__.py`` - that runs before
``app.api.chat`` copies the transcript builders into its own namespace, which is
the only moment where patching them still has an effect.
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
# the file is read at most this often; the IDE may rewrite it between messages
CACHE_SECONDS = 1.0

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
    """Rewrite every config block inside ``payload`` to target the agent.

    The walk is deliberately shape-agnostic: lite, standard and heavy mode all
    build their transcripts differently, but each of them contains exactly the
    dict that carries ``isCustomAgent``.
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
            if "isCustomAgent" in node:
                node["isCustomAgent"] = True
                node["enableCustomAgents"] = True
                node["agentId"] = agent_id
                patched += 1
            stack.extend(node.values())
        elif isinstance(node, (list, tuple)):
            stack.extend(node)
    return patched


def _log(message: str, **fields: Any) -> None:
    try:
        from app.logger import logger

        logger.info(message, extra={"request_info": fields})
    except Exception:
        pass


def _wrap_builder(builder):
    def hook(*args: Any, **kwargs: Any):
        transcript = builder(*args, **kwargs)
        agent = current_agent()
        if agent:
            patched = apply_to(transcript, agent)
            _log(
                "Transcript routed to a custom agent",
                event="custom_agent_applied",
                builder=getattr(builder, "__name__", "builder"),
                agent_id=agent["id"],
                blocks_patched=patched,
            )
        return transcript

    hook._agent_hook = True  # type: ignore[attr-defined]
    hook.__name__ = getattr(builder, "__name__", "builder")
    return hook


def install() -> None:
    """Wrap the transcript builders. Safe to call more than once."""
    global _installed
    if _installed:
        return
    _installed = True

    from app import conversation as conv

    manager = getattr(conv, "ConversationManager", None)
    if manager is not None:
        original = manager.get_transcript_payload
        if not getattr(original, "_agent_hook", False):

            def payload_hook(self, *args: Any, **kwargs: Any):
                payload = original(self, *args, **kwargs)
                agent = current_agent()
                if agent and isinstance(payload, dict):
                    patched = apply_to(payload.get("transcript"), agent)
                    _log(
                        "Transcript routed to a custom agent",
                        event="custom_agent_applied",
                        builder="get_transcript_payload",
                        agent_id=agent["id"],
                        blocks_patched=patched,
                    )
                return payload

            payload_hook._agent_hook = True  # type: ignore[attr-defined]
            manager.get_transcript_payload = payload_hook

    for name in ("build_lite_transcript", "build_standard_transcript"):
        builder = getattr(conv, name, None)
        if builder is None or getattr(builder, "_agent_hook", False):
            continue
        setattr(conv, name, _wrap_builder(builder))
