"""Configuration. Zero third-party dependencies: stdlib json only.

Config file: <workspace>/.monoide/config.json  (falls back to defaults)

The file also decides which assistant answers in the chat panel: the default
Notion AI, or one of the user's own agents. A custom agent lives in the
workspace that created it, so its id is always something the user pastes - see
``write_agent_target``, which hands the choice to the notion2api child process.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sys
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Dict, List, Union

DEFAULTS: Dict[str, Any] = {
    "upstream": {
        # notion2api base url (OpenAI compatible). Overwritten at boot when the
        # bundled notion2api is used (see ide/supervisor.py).
        "base_url": "http://127.0.0.1:8000/v1",
        # start the notion2api copy that ships in vendor/notion2api
        "embedded": True,
        # preferred port for it; the next free one is used when taken
        "embedded_port": 8000,
        # notion2api APP_MODE. "heavy" is the only mode that binds a Notion
        # thread to a conversation_id; lite and standard call Notion with
        # thread_id=None, which means every single request of the agent loop
        # opens a new chat in Notion (~10 chats per user message).
        "app_mode": "heavy",
        # keep one Notion chat per IDE chat session by replaying the
        # conversation_id notion2api returns in X-Conversation-Id
        "reuse_conversation": True,
        # "notion" = the default Notion AI assistant,
        # "custom"  = the agent identified by agent_url / agent_id below.
        # Never ship a real agent id here: agents are workspace-local.
        "agent_mode": "notion",
        "agent_url": "",
        "agent_id": "",
        "agent_name": "",
        "api_key": "",
        "model": "claude-sonnet4.6",
        "stream": True,
        "timeout": 300,
    },
    "agent": {
        "max_rounds": 24,
        "handshake": True,
        "repair_refusals": True,
        "max_repairs": 2,
        "auto_approve": False,
        "observation_char_limit": 24000,
        "history_char_budget": 90000,
    },
    "chat": {
        # where a new chat is kept: "local" writes .monoide/chats on this
        # machine, "web" keeps it in Notion only. Asked on the first message of
        # a chat and fixed from then on, so this is only a default.
        "storage": "local",
    },
    "sandbox": {
        "deny_outside_root": True,
        "bash_timeout": 120,
        "bash_memory_mb": 2048,
        "blocked_patterns": ["rm -rf /", ":(){", "mkfs", "dd if=/dev/zero of=/dev"],
    },
    "lsp": {
        "enabled": True,
        # lazy: a server starts on first file of that language and dies when idle
        "idle_shutdown_seconds": 180,
        "memory_mb": 700,
        "max_servers": 2,
        "max_file_kb": 512,
        "servers": {
            "python": {"cmd": ["pyright-langserver", "--stdio"], "ext": [".py"]},
            "typescript": {
                "cmd": ["typescript-language-server", "--stdio"],
                "ext": [".ts", ".tsx", ".js", ".jsx"],
            },
            "rust": {"cmd": ["rust-analyzer"], "ext": [".rs"]},
            "go": {"cmd": ["gopls"], "ext": [".go"]},
        },
    },
    "mcp": {
        # name -> {"cmd": [...], "env": {...}, "enabled": true}
        "servers": {}
    },
    "boot": {
        # a failure in any of these stops the app from opening at all
        "required": ["workspace", "runtime", "deps", "notion2api"],
        # these are reported and never block: a missing language server is normal,
        # and "not signed in to Notion yet" is the expected state on a first run
        "advisory": ["account", "lsp", "mcp", "terminal"],
        # set to false to fall back to "open the editor anyway", e.g. offline
        "block_on_failure": True,
        # let the app install the notion2api dependencies by itself
        "deps_install": True,
    },
    "auth": {
        # a Notion account must be attached before the agent will answer
        "require_login": True,
        "login_timeout": 300,
        # path to a notion2api checkout; accounts.json/.env are mirrored there
        "notion2api_dir": "",
    },
    "ui": {"font_size": 13, "tab_width": 4},
    "ignore": [
        ".git", "node_modules", "__pycache__", ".venv", "venv", "dist", "build",
        ".mypy_cache", ".pytest_cache", ".next", "target", ".monoide/cache",
    ],
}


def _deep_merge(base: Dict[str, Any], over: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(base)
    for key, value in (over or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


@dataclass
class Config:
    root: Path
    data: Dict[str, Any] = field(default_factory=dict)

    # -- lifecycle ---------------------------------------------------------
    @classmethod
    def load(cls, root: str | os.PathLike[str]) -> "Config":
        root_path = Path(root).resolve()
        cfg_path = root_path / ".monoide" / "config.json"
        data = dict(DEFAULTS)
        if cfg_path.exists():
            try:
                data = _deep_merge(DEFAULTS, json.loads(cfg_path.read_text("utf-8")))
            except Exception:
                pass
        # Older configs were written with app_mode "standard" or "lite", both of
        # which make Notion open a fresh chat on every request of the agent
        # loop. Nobody picks that on purpose, so migrate it away.
        if str(data.get("upstream", {}).get("app_mode", "")).lower() in ("standard", "lite"):
            data["upstream"]["app_mode"] = "heavy"
        # environment overrides (handy for docker / quick runs)
        env_base = os.environ.get("MONOIDE_BASE_URL")
        env_key = os.environ.get("MONOIDE_API_KEY")
        env_model = os.environ.get("MONOIDE_MODEL")
        env_mode = os.environ.get("MONOIDE_APP_MODE")
        env_agent = os.environ.get("MONOIDE_AGENT_URL")
        if env_base:
            data["upstream"]["base_url"] = env_base
            # an explicit base url means "use my own notion2api"
            data["upstream"]["embedded"] = False
        if env_key:
            data["upstream"]["api_key"] = env_key
        if env_model:
            data["upstream"]["model"] = env_model
        if env_mode:
            data["upstream"]["app_mode"] = env_mode
        if env_agent:
            data["upstream"]["agent_url"] = env_agent
            data["upstream"]["agent_id"] = agent_id_from_url(env_agent)
            data["upstream"]["agent_mode"] = "custom" if data["upstream"]["agent_id"] else "notion"
        return cls(root=root_path, data=data)

    def save(self) -> None:
        cfg_dir = self.root / ".monoide"
        cfg_dir.mkdir(parents=True, exist_ok=True)
        (cfg_dir / "config.json").write_text(
            json.dumps(self.data, indent=2, ensure_ascii=False), "utf-8"
        )

    # -- access ------------------------------------------------------------
    def get(self, *path: str, default: Any = None) -> Any:
        cur: Any = self.data
        for part in path:
            if not isinstance(cur, dict) or part not in cur:
                return default
            cur = cur[part]
        return cur

    def set(self, path: List[str], value: Any) -> None:
        cur = self.data
        for part in path[:-1]:
            cur = cur.setdefault(part, {})
        cur[path[-1]] = value

    @property
    def ignore(self) -> List[str]:
        return list(self.get("ignore", default=[]) or [])

    def as_json(self) -> Dict[str, Any]:
        out = json.loads(json.dumps(self.data))
        # never leak the key to the browser
        if out.get("upstream", {}).get("api_key"):
            out["upstream"]["api_key"] = "***"
        out["_root"] = str(self.root)
        return out


# ---------------------------------------------------------------------------
# which assistant answers: Notion AI or one of the user's own agents
# ---------------------------------------------------------------------------

AGENT_TARGET_FILENAME = "agent-target.json"

_UUID = re.compile(
    r"[0-9a-fA-F]{8}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{12}"
)


def agent_id_from_url(raw: str) -> str:
    """Pull the agent id out of whatever the user pasted.

    Accepts a full notion.so link, a dashed uuid or a dashless one, and returns
    a dashed uuid (or "" when there is no id in the text at all). Query strings
    are cut off first, so a ``?pvs=`` tail cannot be mistaken for the id.
    """
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


def state_dir() -> Path:
    """Where the app keeps state shared with the notion2api child process."""
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


def agent_target_path() -> Path:
    override = os.environ.get("MONOIDE_AGENT_FILE")
    return Path(override) if override else state_dir() / AGENT_TARGET_FILENAME


def read_agent_selection(config: Union["Config", Dict[str, Any]]) -> Dict[str, str]:
    """Normalised view of the agent settings.

    Accepts a :class:`Config` or the plain ``upstream`` settings dict, because
    callers that already hold the browser-facing config (``as_json()``) have no
    Config instance to hand. Getting this wrong would not raise - it would
    quietly report "notion" - so both shapes are read rather than assumed.

    A selection is only "custom" when there is an id to point at; a half-filled
    config silently falls back to the default assistant instead of failing.
    """
    if isinstance(config, dict):
        # either the whole config or just its "upstream" section
        section = config.get("upstream") if isinstance(config.get("upstream"), dict) else config
        read = lambda key, default="": section.get(key, default)  # noqa: E731
    else:
        read = lambda key, default="": config.get("upstream", key, default=default)  # noqa: E731

    mode = str(read("agent_mode", "notion") or "notion")
    url = str(read("agent_url", "") or "").strip()
    raw_id = str(read("agent_id", "") or "").strip()
    name = str(read("agent_name", "") or "").strip()
    agent_id = agent_id_from_url(raw_id) or agent_id_from_url(url)
    if mode.strip().lower() != "custom" or not agent_id:
        return {"mode": "notion", "agent_id": "", "agent_url": "", "agent_name": ""}
    return {
        "mode": "custom",
        "agent_id": agent_id,
        "agent_url": url,
        "agent_name": name or "custom agent",
    }


def write_agent_target(config: "Config") -> str:
    """Publish the choice for notion2api. Returns the agent id ("" = default).

    notion2api runs as a separate process, so the selection travels through a
    tiny json file both sides agree on (vendor/notion2api/app/agent_override.py
    reads it). Writing it before every turn means switching agents takes effect
    on the next message, with no restart.
    """
    selection = read_agent_selection(config)
    payload = dict(selection)
    payload["updated"] = time.time()
    path = agent_target_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(payload, indent=2, ensure_ascii=False), "utf-8")
        os.replace(temporary, path)
    except OSError:
        # not fatal: without the file notion2api keeps using the default agent
        pass
    return selection["agent_id"]


def describe_environment(root: Path) -> Dict[str, str]:
    """Small facts injected into the bridge preamble."""
    import platform
    import subprocess

    git = "not a git repository"
    if (root / ".git").exists() and shutil.which("git"):
        try:
            branch = subprocess.run(
                ["git", "rev-parse", "--abbrev-ref", "HEAD"],
                cwd=root, capture_output=True, text=True, timeout=5,
            ).stdout.strip()
            dirty = subprocess.run(
                ["git", "status", "--porcelain"],
                cwd=root, capture_output=True, text=True, timeout=5,
            ).stdout.strip()
            git = f"branch {branch or '?'}" + (", uncommitted changes" if dirty else ", clean")
        except Exception:
            git = "git repository"
    return {
        "os": f"{platform.system()} {platform.release()} ({platform.machine()})",
        "shell": os.environ.get("SHELL") or ("cmd.exe" if os.name == "nt" else "/bin/sh"),
        "git": git,
    }
