"""Notion sign-in gate.

The IDE refuses to talk to the model until a real Notion account is attached.
The flow mirrors notion2api's `login.py`, but without third-party packages:

1. A supported browser (Chrome, Edge, Yandex, Brave, Vivaldi, Opera, Chromium)
   is launched into a throwaway profile with `--remote-debugging-port`.
2. The user signs in to Notion in that window.
3. Cookies are polled over CDP (`Network.getCookies` + `document.cookie`) until
   `token_v2` appears.
4. The extraction pipeline from `extract_notion_info.js` runs - `getSpaces` and
   `loadUserContent` are read and folded into (user, space, space_view)
   candidates so the right workspace can be chosen.
5. The chosen account is stored in `.monoide/auth.json`, and, when a notion2api
   checkout is configured, mirrored into its `accounts.json` + `.env`
   (`NOTION_ACCOUNTS=...`) exactly like `login.py` does.

Firefox exposes no CDP endpoint, so for it (and as a general escape hatch) the
same pipeline runs against a pasted `token_v2`: the browser is opened for sign-in
and only the cookie value is supplied by hand.
"""

from __future__ import annotations

import json
import os
import queue
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .ws import create_connection

NOTION_URL = os.environ.get("NOTION_URL", "https://www.notion.so").rstrip("/")
NOTION_DOMAIN = os.environ.get("NOTION_DOMAIN", "www.notion.so").lstrip(".")
AI_URL = NOTION_URL + "/ai"
HOST_HINT = NOTION_DOMAIN[4:] if NOTION_DOMAIN.startswith("www.") else NOTION_DOMAIN
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36"
)
LOGIN_TIMEOUT = 300


class AuthError(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# browser discovery
# ---------------------------------------------------------------------------

BROWSERS: List[Dict[str, Any]] = [
    {
        "key": "chrome",
        "label": "CHROME",
        "cdp": True,
        "which": ["chrome", "google-chrome", "google-chrome-stable", "chrome.exe"],
        "paths": [
            r"%PROGRAMFILES%\Google\Chrome\Application\chrome.exe",
            r"%PROGRAMFILES(X86)%\Google\Chrome\Application\chrome.exe",
            r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe",
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        ],
    },
    {
        "key": "edge",
        "label": "EDGE",
        "cdp": True,
        "which": ["msedge", "microsoft-edge", "microsoft-edge-stable", "msedge.exe"],
        "paths": [
            r"%PROGRAMFILES%\Microsoft\Edge\Application\msedge.exe",
            r"%PROGRAMFILES(X86)%\Microsoft\Edge\Application\msedge.exe",
            "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
        ],
    },
    {
        "key": "yandex",
        "label": "YANDEX",
        "cdp": True,
        "which": ["yandex-browser", "yandex-browser-stable", "browser.exe"],
        "paths": [
            r"%LOCALAPPDATA%\Yandex\YandexBrowser\Application\browser.exe",
            r"%PROGRAMFILES%\Yandex\YandexBrowser\Application\browser.exe",
            r"%PROGRAMFILES(X86)%\Yandex\YandexBrowser\Application\browser.exe",
            "/Applications/Yandex.app/Contents/MacOS/Yandex",
        ],
    },
    {
        "key": "brave",
        "label": "BRAVE",
        "cdp": True,
        "which": ["brave", "brave-browser", "brave.exe"],
        "paths": [
            r"%PROGRAMFILES%\BraveSoftware\Brave-Browser\Application\brave.exe",
            r"%LOCALAPPDATA%\BraveSoftware\Brave-Browser\Application\brave.exe",
            "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
        ],
    },
    {
        "key": "vivaldi",
        "label": "VIVALDI",
        "cdp": True,
        "which": ["vivaldi", "vivaldi-stable", "vivaldi.exe"],
        "paths": [
            r"%LOCALAPPDATA%\Vivaldi\Application\vivaldi.exe",
            "/Applications/Vivaldi.app/Contents/MacOS/Vivaldi",
        ],
    },
    {
        "key": "opera",
        "label": "OPERA",
        "cdp": True,
        "which": ["opera", "opera.exe"],
        "paths": [
            r"%LOCALAPPDATA%\Programs\Opera\opera.exe",
            r"%PROGRAMFILES%\Opera\opera.exe",
            "/Applications/Opera.app/Contents/MacOS/Opera",
        ],
    },
    {
        "key": "chromium",
        "label": "CHROMIUM",
        "cdp": True,
        "which": ["chromium", "chromium-browser", "chrome.exe"],
        "paths": ["/Applications/Chromium.app/Contents/MacOS/Chromium"],
    },
    {
        "key": "firefox",
        "label": "FIREFOX",
        "cdp": False,
        "which": ["firefox", "firefox.exe", "firefox-esr"],
        "paths": [
            r"%PROGRAMFILES%\Mozilla Firefox\firefox.exe",
            r"%PROGRAMFILES(X86)%\Mozilla Firefox\firefox.exe",
            "/Applications/Firefox.app/Contents/MacOS/firefox",
        ],
    },
]


def _expand(path: str) -> Optional[str]:
    expanded = os.path.expandvars(os.path.expanduser(path))
    if "%" in expanded:
        return None
    return expanded if Path(expanded).exists() else None


def find_browser(key: str) -> Optional[str]:
    override = os.environ.get("MONOIDE_BROWSER_PATH")
    if override and Path(override).exists():
        return override
    for spec in BROWSERS:
        if spec["key"] != key:
            continue
        for name in spec["which"]:
            found = shutil.which(name)
            if found:
                return found
        for candidate in spec["paths"]:
            found = _expand(candidate)
            if found:
                return found
    return None


def available_browsers() -> List[Dict[str, Any]]:
    out = []
    for spec in BROWSERS:
        path = find_browser(spec["key"])
        out.append(
            {
                "key": spec["key"],
                "label": spec["label"],
                "cdp": spec["cdp"],
                "installed": bool(path),
                "path": path or "",
            }
        )
    return out


# ---------------------------------------------------------------------------
# chrome devtools protocol
# ---------------------------------------------------------------------------


def _free_port(start: int = 9222, end: int = 9260) -> int:
    for port in range(start, end + 1):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.bind(("127.0.0.1", port))
            except OSError:
                continue
            return port
    raise AuthError("no free debugging port in 9222-9260")


def _get_json(url: str, timeout: float = 2.0) -> Any:
    with urlopen(url, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8", "replace"))


class Cdp:
    def __init__(self, ws_url: str):
        self.ws = create_connection(ws_url, timeout=20.0)
        self._id = 0

    def call(self, method: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        self._id += 1
        message_id = self._id
        self.ws.send(json.dumps({"id": message_id, "method": method, "params": params or {}}))
        while True:
            message = json.loads(self.ws.recv())
            if message.get("id") != message_id:
                continue  # an event, not our answer
            if message.get("error"):
                raise AuthError(str(message["error"]))
            result = message.get("result") or {}
            return result if isinstance(result, dict) else {}

    def evaluate(self, expression: str, await_promise: bool = False) -> Any:
        result = self.call(
            "Runtime.evaluate",
            {
                "expression": expression,
                "returnByValue": True,
                "awaitPromise": await_promise,
            },
        )
        if result.get("exceptionDetails"):
            raise AuthError(str(result["exceptionDetails"].get("text") or "evaluate failed"))
        return (result.get("result") or {}).get("value")

    def close(self) -> None:
        self.ws.close()


def _launch(executable: str, port: int, url: str) -> "tuple[subprocess.Popen, Path]":
    profile = Path(tempfile.mkdtemp(prefix="monoide-login-"))
    argv = [
        executable,
        "--remote-debugging-port=%d" % port,
        "--remote-allow-origins=*",
        "--user-data-dir=%s" % profile,
        "--no-first-run",
        "--no-default-browser-check",
        "--new-window",
        url,
    ]
    process = subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return process, profile


def _pick_target(port: int) -> Optional[Dict[str, Any]]:
    try:
        targets = _get_json("http://127.0.0.1:%d/json/list" % port)
    except Exception:
        return None
    items = targets if isinstance(targets, list) else targets.get("result")
    if not isinstance(items, list):
        return None
    pages = [item for item in items if isinstance(item, dict) and item.get("type") == "page"]
    for item in pages:
        url = str(item.get("url") or "")
        if HOST_HINT in url or "accounts.google.com" in url:
            return item
    return pages[0] if pages else (items[0] if items else None)


def _parse_cookie_header(raw: str) -> Dict[str, str]:
    cookies: Dict[str, str] = {}
    for part in raw.split(";"):
        name, _, value = part.strip().partition("=")
        if name.strip():
            cookies[name.strip()] = value.strip()
    return cookies


def _cookies_via_cdp(port: int) -> Dict[str, str]:
    target = _pick_target(port)
    if not target:
        raise AuthError("devtools target is not up yet")
    ws_url = target.get("webSocketDebuggerUrl")
    if not ws_url:
        raise AuthError("devtools target has no websocket url")
    client = Cdp(str(ws_url))
    try:
        client.call("Page.enable")
        client.call("Network.enable")
        cookies: Dict[str, str] = {}
        try:
            cookies.update(_parse_cookie_header(str(client.evaluate("document.cookie") or "")))
        except Exception:
            pass
        result = client.call("Network.getCookies", {"urls": [NOTION_URL, AI_URL]})
        for item in result.get("cookies") or []:
            if isinstance(item, dict) and item.get("name"):
                cookies[str(item["name"])] = str(item.get("value") or "")
        return cookies
    finally:
        client.close()


# ---------------------------------------------------------------------------
# notion api (urllib, cookie auth) - the server-side twin of
# extract_notion_info.js
# ---------------------------------------------------------------------------


def _notion_post(path: str, cookies: Dict[str, str], timeout: float = 30.0) -> Dict[str, Any]:
    header = "; ".join("%s=%s" % (name, value) for name, value in cookies.items() if value)
    request = Request(
        NOTION_URL + path,
        data=b"{}",
        method="POST",
        headers={
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
            "Origin": NOTION_URL,
            "Referer": AI_URL,
            "Cookie": header,
        },
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8", "replace"))
    except HTTPError as error:
        if error.code in (401, 403):
            raise AuthError("Notion rejected the token (HTTP %d) - sign in again" % error.code)
        raise AuthError("Notion %s failed: HTTP %d" % (path, error.code))
    except URLError as error:
        raise AuthError("cannot reach Notion: %s" % error.reason)
    return payload if isinstance(payload, dict) else {}


def _value_of(raw: Any) -> Dict[str, Any]:
    if isinstance(raw, dict):
        inner = raw.get("value")
        if isinstance(inner, dict):
            deeper = inner.get("value")
            return deeper if isinstance(deeper, dict) else inner
        if inner is not None:
            return {"value": inner}
        return raw
    return {}


def extract_candidates(cookies: Dict[str, str]) -> List[Dict[str, Any]]:
    """Fold getSpaces + loadUserContent into (user, space) candidates."""
    token = str(cookies.get("token_v2") or "").strip()
    if not token:
        raise AuthError("token_v2 is missing")
    active_user = str(cookies.get("notion_user_id") or "").strip()

    users: Dict[str, Dict[str, str]] = {}
    spaces: Dict[str, Dict[str, str]] = {}
    space_views: Dict[str, str] = {}

    def add_users(source: Dict[str, Any], target: Dict[str, Dict[str, str]]) -> None:
        for user_id, raw in (source or {}).items():
            value = _value_of(raw)
            target[user_id] = {
                "name": str(
                    value.get("given_name") or value.get("name") or value.get("family_name") or ""
                ).strip(),
                "email": str(value.get("email") or "").strip(),
            }

    def add_spaces(source: Dict[str, Any], target: Dict[str, Dict[str, str]]) -> None:
        for space_id, raw in (source or {}).items():
            value = _value_of(raw)
            target.setdefault(
                space_id,
                {
                    "name": str(value.get("name") or "").strip(),
                    "plan": str(
                        value.get("plan_type") or value.get("subscription_tier") or ""
                    ).strip(),
                },
            )

    def add_views(source: Dict[str, Any], target: Dict[str, str]) -> None:
        for view_id, raw in (source or {}).items():
            value = _value_of(raw)
            space_id = str(value.get("space_id") or "").strip()
            if space_id:
                target.setdefault(space_id, view_id)

    try:
        get_spaces = _notion_post("/api/v3/getSpaces", cookies)
    except AuthError:
        get_spaces = {}

    candidates: List[Dict[str, Any]] = []

    def build(
        local_users: Dict[str, Dict[str, str]],
        local_spaces: Dict[str, Dict[str, str]],
        local_views: Dict[str, str],
    ) -> List[Dict[str, Any]]:
        rows = []
        for user_id, user in local_users.items():
            for space_id, space in local_spaces.items():
                rows.append(
                    {
                        "token_v2": token,
                        "space_id": space_id,
                        "user_id": user_id,
                        "space_view_id": local_views.get(space_id, ""),
                        "user_name": user["name"] or "user",
                        "user_email": user["email"],
                        "space_name": space.get("name", ""),
                        "space_plan": space.get("plan", ""),
                    }
                )
        return rows

    for group in get_spaces.values():
        if not isinstance(group, dict):
            continue
        group_users: Dict[str, Dict[str, str]] = {}
        group_spaces: Dict[str, Dict[str, str]] = {}
        group_views: Dict[str, str] = {}
        add_users(group.get("notion_user", {}), group_users)
        add_spaces(group.get("space", {}), group_spaces)
        add_views(group.get("space_view", {}), group_views)
        users.update(group_users)
        spaces.update(group_spaces)
        space_views.update(group_views)
        candidates.extend(build(group_users, group_spaces, group_views))

    record_map = _notion_post("/api/v3/loadUserContent", cookies).get("recordMap") or {}
    add_users(record_map.get("notion_user", {}), users)
    add_spaces(record_map.get("space", {}), spaces)
    add_views(record_map.get("space_view", {}), space_views)

    if not users:
        raise AuthError("no Notion users returned - the token is invalid or expired")
    if not spaces:
        raise AuthError("no Notion workspaces returned - the token is invalid or expired")
    if not candidates:
        candidates = build(users, spaces, space_views)

    if active_user:
        active = [row for row in candidates if row["user_id"] == active_user]
        if active:
            candidates = active

    candidates.sort(
        key=lambda row: (
            1 if row["user_id"] == active_user else 0,
            1 if row["user_email"] else 0,
            1 if row["space_view_id"] else 0,
            1 if row["space_name"] else 0,
        ),
        reverse=True,
    )
    # de-duplicate on (user, space)
    seen = set()
    unique = []
    for row in candidates:
        key = (row["user_id"], row["space_id"])
        if key in seen:
            continue
        seen.add(key)
        unique.append(row)
    return unique


def validate(account: Dict[str, Any]) -> "tuple[bool, str]":
    cookies = dict(account.get("cookies") or {})
    cookies["token_v2"] = str(account.get("token_v2") or "")
    if not cookies["token_v2"] or not account.get("space_id") or not account.get("user_id"):
        return False, "incomplete account record"
    try:
        payload = _notion_post("/api/v3/loadUserContent", cookies)
    except AuthError as error:
        return False, str(error)
    return (True, "token valid") if isinstance(payload.get("recordMap"), dict) else (False, "unexpected response")


# ---------------------------------------------------------------------------
# storage
# ---------------------------------------------------------------------------


class AuthStore:
    """Keeps the signed-in account in <workspace>/.monoide/auth.json."""

    def __init__(self, root: Path, notion2api_dir: str = ""):
        self.path = root / ".monoide" / "auth.json"
        self.notion2api_dir = notion2api_dir
        self.account: Optional[Dict[str, Any]] = None
        self.load()

    def load(self) -> Optional[Dict[str, Any]]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            self.account = data if isinstance(data, dict) and data.get("token_v2") else None
        except Exception:
            self.account = None
        return self.account

    @property
    def authenticated(self) -> bool:
        return bool(self.account and self.account.get("token_v2"))

    def public(self) -> Dict[str, Any]:
        """Account view for the UI - never leaks the token."""
        if not self.account:
            return {}
        token = str(self.account.get("token_v2") or "")
        return {
            "profile_name": self.account.get("profile_name", "default"),
            "user_name": self.account.get("user_name", ""),
            "user_email": self.account.get("user_email", ""),
            "space_id": self.account.get("space_id", ""),
            "space_name": self.account.get("space_name", ""),
            "user_id": self.account.get("user_id", ""),
            "token_hint": (token[:6] + ".." + token[-4:]) if len(token) > 12 else "set",
            "saved_at": self.account.get("saved_at", ""),
        }

    def save(self, account: Dict[str, Any]) -> Dict[str, Any]:
        record = dict(account)
        record.setdefault("profile_name", "default")
        record["saved_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
        try:
            os.chmod(self.path, 0o600)  # best effort; a no-op on Windows
        except Exception:
            pass
        self.account = record
        return record

    def clear(self) -> None:
        try:
            self.path.unlink()
        except Exception:
            pass
        self.account = None

    # -- notion2api mirroring ---------------------------------------------
    def sync_notion2api(self, account: Dict[str, Any]) -> str:
        """Write accounts.json + .env of a notion2api checkout, like login.py."""
        if not self.notion2api_dir:
            return ""
        base = Path(os.path.expanduser(self.notion2api_dir))
        if not base.is_dir():
            return "notion2api dir not found: %s" % base
        entry = {
            "profile_name": account.get("profile_name", "default"),
            "token_v2": account.get("token_v2", ""),
            "space_id": account.get("space_id", ""),
            "user_id": account.get("user_id", ""),
            "space_view_id": account.get("space_view_id", ""),
            "user_name": account.get("user_name", ""),
            "user_email": account.get("user_email", ""),
            "cookies": account.get("cookies", {}),
        }
        accounts_path = base / "accounts.json"
        existing: List[Dict[str, Any]] = []
        try:
            loaded = json.loads(accounts_path.read_text(encoding="utf-8"))
            if isinstance(loaded, list):
                existing = [item for item in loaded if isinstance(item, dict)]
        except Exception:
            existing = []
        merged = [
            item
            for item in existing
            if not (
                item.get("user_id") == entry["user_id"]
                and item.get("space_id") == entry["space_id"]
            )
            and str(item.get("profile_name") or "").lower() != str(entry["profile_name"]).lower()
        ]
        merged.insert(0, entry)
        accounts_path.write_text(
            json.dumps(merged, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )

        env_path = base / ".env"
        env_line = "NOTION_ACCOUNTS=" + json.dumps(merged, ensure_ascii=False)
        if env_path.exists():
            lines = env_path.read_text(encoding="utf-8").splitlines()
            replaced = False
            out = []
            for line in lines:
                if line.lstrip().startswith("NOTION_ACCOUNTS="):
                    out.append(env_line)
                    replaced = True
                else:
                    out.append(line)
            if not replaced:
                out.append(env_line)
            env_path.write_text("\n".join(out).rstrip() + "\n", encoding="utf-8")
        else:
            env_path.write_text(env_line + "\n", encoding="utf-8")
        return "synced %s and .env (restart notion2api to pick it up)" % accounts_path.name


# ---------------------------------------------------------------------------
# login flow (background thread + SSE events)
# ---------------------------------------------------------------------------


class LoginFlow:
    """One sign-in attempt. Progress is streamed to the browser."""

    def __init__(self, store: AuthStore, browser: str, timeout: int = LOGIN_TIMEOUT):
        self.id = uuid.uuid4().hex[:8]
        self.store = store
        self.browser = browser
        self.timeout = timeout
        self.events: "queue.Queue[Dict[str, Any]]" = queue.Queue()
        self.status = "running"
        self.candidates: List[Dict[str, Any]] = []
        self.cookies: Dict[str, str] = {}
        self.error = ""
        self.cancelled = False

    # -- events ------------------------------------------------------------
    def emit(self, kind: str, **payload: Any) -> None:
        self.events.put({"type": kind, **payload})

    def log(self, text: str) -> None:
        self.emit("log", text=text)

    def cancel(self) -> None:
        self.cancelled = True
        self.status = "cancelled"
        self.emit("cancelled")

    # -- runners -----------------------------------------------------------
    def start(self) -> None:
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self) -> None:
        try:
            self.cookies = self._collect_cookies()
            if self.cancelled:
                return
            self.log("token_v2 captured, reading workspaces")
            self.candidates = extract_candidates(self.cookies)
            self.status = "choose"
            self.emit("candidates", candidates=self._safe_candidates())
            if len(self.candidates) == 1:
                self.log("single workspace found, selecting it")
                self.select(0)
        except Exception as error:  # surfaced verbatim in the UI
            self.status = "error"
            self.error = str(error)
            self.emit("error", text=self.error)

    def _collect_cookies(self) -> Dict[str, str]:
        executable = find_browser(self.browser)
        if not executable:
            raise AuthError("%s is not installed on this machine" % self.browser)
        spec = next((item for item in BROWSERS if item["key"] == self.browser), None)
        if spec and not spec["cdp"]:
            # Firefox: no DevTools Protocol. Open it and wait for a pasted token.
            subprocess.Popen(
                [executable, "-new-window", AI_URL],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            self.status = "await_token"
            self.emit(
                "await_token",
                text="Firefox has no DevTools Protocol endpoint. Sign in, then open "
                "F12 - Storage - Cookies - www.notion.so, copy token_v2 and paste it below.",
            )
            token = self._wait_for_pasted_token()
            return {"token_v2": token}

        port = _free_port()
        self.log("launching %s with devtools on port %d" % (self.browser, port))
        process, profile = _launch(executable, port, AI_URL)
        try:
            for _ in range(40):
                if self.cancelled:
                    raise AuthError("cancelled")
                try:
                    _get_json("http://127.0.0.1:%d/json/version" % port, timeout=1.0)
                    break
                except Exception:
                    time.sleep(0.5)
            self.log("sign in to Notion in the opened window")
            deadline = time.time() + self.timeout
            notified = ""
            while time.time() < deadline:
                if self.cancelled:
                    raise AuthError("cancelled")
                if process.poll() is not None:
                    raise AuthError("the browser window was closed before sign-in finished")
                try:
                    cookies = _cookies_via_cdp(port)
                except Exception as error:
                    cookies = {}
                    message = str(error)
                    if message != notified:
                        self.log("devtools not ready: %s" % message)
                        notified = message
                token = str(cookies.get("token_v2") or "").strip()
                if token:
                    return cookies
                remaining = int(deadline - time.time())
                self.emit("waiting", seconds_left=max(remaining, 0))
                time.sleep(3)
            raise AuthError("timed out waiting for token_v2")
        finally:
            try:
                process.terminate()
                process.wait(timeout=5)
            except Exception:
                try:
                    process.kill()
                except Exception:
                    pass
            shutil.rmtree(profile, ignore_errors=True)
            self.log("temporary browser profile removed")

    # token pasted from the UI (Firefox / manual path)
    def _wait_for_pasted_token(self) -> str:
        self._pasted: Optional[str] = getattr(self, "_pasted", None)
        deadline = time.time() + self.timeout
        while time.time() < deadline:
            if self.cancelled:
                raise AuthError("cancelled")
            if self._pasted:
                return self._pasted
            time.sleep(0.5)
        raise AuthError("timed out waiting for a pasted token_v2")

    def submit_token(self, token: str) -> None:
        token = token.strip()
        if not token:
            raise AuthError("token_v2 is empty")
        self._pasted = token
        self.log("token received from the form")

    # -- selection ---------------------------------------------------------
    def _safe_candidates(self) -> List[Dict[str, Any]]:
        return [
            {
                "index": index,
                "user_name": row["user_name"],
                "user_email": row["user_email"],
                "space_id": row["space_id"],
                "space_name": row["space_name"],
                "space_plan": row["space_plan"],
                "has_space_view": bool(row["space_view_id"]),
            }
            for index, row in enumerate(self.candidates)
        ]

    def select(self, index: int) -> Dict[str, Any]:
        if index < 0 or index >= len(self.candidates):
            raise AuthError("invalid workspace selection")
        chosen = dict(self.candidates[index])
        chosen["cookies"] = self.cookies
        chosen["profile_name"] = "default"
        record = self.store.save(chosen)
        note = ""
        try:
            note = self.store.sync_notion2api(record)
        except Exception as error:
            note = "notion2api sync failed: %s" % error
        if note:
            self.log(note)
        self.status = "done"
        self.emit("done", account=self.store.public())
        return record
