"""Configuration. Zero third-party dependencies: stdlib json only.

Config file: <workspace>/.monoide/config.json  (falls back to defaults)
"""

from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Dict, List

DEFAULTS: Dict[str, Any] = {
    "upstream": {
        # notion2api base url (OpenAI compatible). Overwritten at boot when the
        # bundled notion2api is used (see ide/supervisor.py).
        "base_url": "http://127.0.0.1:8000/v1",
        # start the notion2api copy that ships in vendor/notion2api
        "embedded": True,
        # preferred port for it; the next free one is used when taken
        "embedded_port": 8000,
        # notion2api APP_MODE: standard keeps full context + thinking
        "app_mode": "standard",
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
        # environment overrides (handy for docker / quick runs)
        env_base = os.environ.get("MONOIDE_BASE_URL")
        env_key = os.environ.get("MONOIDE_API_KEY")
        env_model = os.environ.get("MONOIDE_MODEL")
        if env_base:
            data["upstream"]["base_url"] = env_base
            # an explicit base url means "use my own notion2api"
            data["upstream"]["embedded"] = False
        if env_key:
            data["upstream"]["api_key"] = env_key
        if env_model:
            data["upstream"]["model"] = env_model
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
