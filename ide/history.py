"""Chat history: what the IDE keeps, and what it reads back from Notion.

Two storage modes, chosen on the first message of a chat and fixed afterwards
(the choice only means anything while the chat is still empty):

``local``
    One json file per chat under ``<workspace>/.monoide/chats``. Survives a
    restart, travels with the project folder, greppable, deletable.

``web``
    Nothing is written to this machine. The conversation still exists - every
    message went through a Notion thread and Notion keeps it - so the list is
    fetched back from the workspace instead of read from disk.

The importer reads Claude Code sessions (``~/.claude/projects/<project>/<uuid>.jsonl``,
on Windows ``%USERPROFILE%\\.claude\\projects\\...``). The format was taken from a
real export rather than guessed:

* one json object per line, ``type`` being ``user``, ``assistant``, ``system``,
  ``attachment``, ``mode``, ``permission-mode``, ``file-history-snapshot`` or
  ``last-prompt``; only the first two carry conversation;
* ``message.content`` is either a plain string or a list of blocks -
  ``text``, ``thinking`` (``{thinking, signature}``), ``tool_use``
  (``{id, name, input}``) and ``tool_result`` (``{tool_use_id, content, is_error}``,
  where ``content`` is always a string);
* a tool result arrives as a ``user`` record, which is a transport detail, not
  something the user said - it is imported as an observation, not as a prompt;
* records are a tree (``uuid`` / ``parentUuid``) but file order is already the
  order things happened, so the import keeps file order and ignores the links.
"""

from __future__ import annotations

import json
import os
import re
import time
import uuid
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

HISTORY_DIRNAME = "chats"
STORAGE_MODES = ("local", "web")
DEFAULT_STORAGE = "local"

# a chat file bigger than this is refused rather than loaded into memory
MAX_CHAT_BYTES = 8 * 1024 * 1024
# an imported session bigger than this is refused; the real ones are ~200 KB
MAX_IMPORT_BYTES = 64 * 1024 * 1024
TITLE_LIMIT = 80
# tool output is kept for context, but a 200 KB directory listing is not worth it
OBSERVATION_LIMIT = 4000

_SAFE_ID = re.compile(r"^[0-9a-zA-Z._-]{1,80}$")


class HistoryError(RuntimeError):
    """A chat could not be read, written or parsed."""


def now_ms() -> int:
    return int(time.time() * 1000)


def new_id() -> str:
    return uuid.uuid4().hex


def normalize_storage(raw: Any) -> str:
    mode = str(raw or "").strip().lower()
    return mode if mode in STORAGE_MODES else DEFAULT_STORAGE


def title_from(text: str) -> str:
    """A one-line title from the first thing the user said."""
    flat = " ".join(str(text or "").split())
    if not flat:
        return "untitled chat"
    if len(flat) <= TITLE_LIMIT:
        return flat
    cut = flat[:TITLE_LIMIT]
    # prefer a word boundary, but only when it is not absurdly early
    space = cut.rfind(" ")
    if space > TITLE_LIMIT // 2:
        cut = cut[:space]
    return cut.rstrip() + "\u2026"


def _clip(text: str, limit: int) -> str:
    body = str(text or "")
    if len(body) <= limit:
        return body
    dropped = len(body) - limit
    return body[:limit] + f"\n\u2026 [{dropped} more characters]"


# ---------------------------------------------------------------------------
# local storage
# ---------------------------------------------------------------------------

class Store:
    """The chats kept on this machine for one workspace."""

    def __init__(self, root: str | os.PathLike[str]):
        self.root = Path(root)
        self.dir = self.root / ".monoide" / HISTORY_DIRNAME

    # -- paths ---------------------------------------------------------------

    def _path(self, chat_id: str) -> Path:
        ident = str(chat_id or "").strip()
        if not _SAFE_ID.match(ident):
            raise HistoryError("bad chat id")
        return self.dir / f"{ident}.json"

    def exists(self, chat_id: str) -> bool:
        try:
            return self._path(chat_id).is_file()
        except HistoryError:
            return False

    # -- reading -------------------------------------------------------------

    def load(self, chat_id: str) -> Dict[str, Any]:
        path = self._path(chat_id)
        try:
            if path.stat().st_size > MAX_CHAT_BYTES:
                raise HistoryError("chat file is too large to open")
            record = json.loads(path.read_text("utf-8"))
        except HistoryError:
            raise
        except FileNotFoundError:
            raise HistoryError("no such chat") from None
        except Exception as exc:
            raise HistoryError(f"unreadable chat: {exc}") from exc
        if not isinstance(record, dict):
            raise HistoryError("unreadable chat: not an object")
        record.setdefault("id", chat_id)
        record.setdefault("messages", [])
        return record

    def list(self) -> List[Dict[str, Any]]:
        """Summaries, newest first. A corrupt file is skipped, not fatal."""
        out: List[Dict[str, Any]] = []
        if not self.dir.is_dir():
            return out
        for path in self.dir.glob("*.json"):
            try:
                record = json.loads(path.read_text("utf-8"))
                if not isinstance(record, dict):
                    continue
            except Exception:
                continue
            out.append(summarize(record, fallback_id=path.stem))
        out.sort(key=lambda row: row.get("updated") or 0, reverse=True)
        return out

    # -- writing -------------------------------------------------------------

    def save(self, record: Dict[str, Any]) -> Dict[str, Any]:
        chat_id = str(record.get("id") or "").strip() or new_id()
        record["id"] = chat_id
        record["storage"] = "local"
        record.setdefault("created", now_ms())
        record["updated"] = now_ms()
        path = self._path(chat_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        try:
            tmp.write_text(
                json.dumps(record, ensure_ascii=False, indent=1), "utf-8"
            )
            os.replace(tmp, path)
        except Exception as exc:
            raise HistoryError(f"could not write the chat: {exc}") from exc
        finally:
            if tmp.exists():
                try:
                    tmp.unlink()
                except OSError:
                    pass
        return record

    def delete(self, chat_id: str) -> bool:
        try:
            self._path(chat_id).unlink()
            return True
        except FileNotFoundError:
            return False
        except HistoryError:
            raise
        except Exception as exc:
            raise HistoryError(f"could not delete the chat: {exc}") from exc


def summarize(record: Dict[str, Any], fallback_id: str = "") -> Dict[str, Any]:
    """The shape the sidebar needs: no message bodies."""
    messages = record.get("messages")
    messages = messages if isinstance(messages, list) else []
    spoken = [
        m
        for m in messages
        if isinstance(m, dict)
        and m.get("role") in ("user", "assistant")
        and m.get("kind") in (None, "", "message")
    ]
    return {
        "id": str(record.get("id") or fallback_id),
        "title": str(record.get("title") or "untitled chat"),
        "created": record.get("created") or 0,
        "updated": record.get("updated") or record.get("created") or 0,
        "storage": normalize_storage(record.get("storage")),
        "source": str(record.get("source") or "ide"),
        "messages": len(spoken),
        "agent": record.get("agent") or {},
        "origin": record.get("origin") or {},
    }


def record_from_session(
    session: Any,
    *,
    chat_id: str = "",
    storage: str = DEFAULT_STORAGE,
    agent: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Snapshot a live ``ide.agent.Session`` into a history record.

    The preamble is deliberately dropped: it is rebuilt for every session from
    the current project state, so storing it would only make old chats reopen
    with a stale picture of the tree.
    """
    messages: List[Dict[str, Any]] = []
    for message in getattr(session, "messages", []) or []:
        if not isinstance(message, dict):
            continue
        role = str(message.get("role") or "")
        if role == "system":
            continue
        messages.append(
            {
                "role": role,
                "content": str(message.get("content") or ""),
                "kind": str(message.get("kind") or "message"),
            }
        )
    first_user = next(
        (m["content"] for m in messages if m["role"] == "user" and m["content"]),
        "",
    )
    return {
        "id": chat_id or new_id(),
        "title": str(getattr(session, "title", "") or "") or title_from(first_user),
        "created": int(getattr(session, "created", 0) or now_ms()),
        "updated": now_ms(),
        "storage": normalize_storage(storage),
        "source": "ide",
        "conversation_id": str(getattr(session, "conversation_id", "") or ""),
        "agent": agent or {},
        "messages": messages,
    }


# ---------------------------------------------------------------------------
# Claude Code import
# ---------------------------------------------------------------------------

def _blocks(content: Any) -> Iterable[Dict[str, Any]]:
    """Claude Code writes content either as a string or as a block list."""
    if isinstance(content, str):
        if content.strip():
            yield {"type": "text", "text": content}
        return
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict):
                yield block


def _tool_call_text(block: Dict[str, Any]) -> str:
    """Render a tool call the way the IDE shows its own action blocks."""
    name = str(block.get("name") or "tool")
    payload = block.get("input")
    try:
        rendered = json.dumps(payload, ensure_ascii=False, indent=1)
    except Exception:
        rendered = str(payload)
    return f"{name}\n{_clip(rendered, OBSERVATION_LIMIT)}"


def parse_claude_session(
    text: str, *, include_thinking: bool = False
) -> Dict[str, Any]:
    """Turn the contents of a session ``.jsonl`` into a history record.

    Malformed lines are counted and skipped: these files are appended to live
    while Claude Code runs, so a half-written last line is normal.
    """
    messages: List[Dict[str, Any]] = []
    skipped = 0
    session_id = ""
    cwd = ""
    version = ""
    started = 0
    ended = 0

    for line in str(text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except Exception:
            skipped += 1
            continue
        if not isinstance(record, dict):
            skipped += 1
            continue

        session_id = session_id or str(record.get("sessionId") or "")
        cwd = cwd or str(record.get("cwd") or "")
        version = version or str(record.get("version") or "")

        stamp = _stamp(record.get("timestamp"))
        if stamp:
            started = min(started or stamp, stamp)
            ended = max(ended, stamp)

        kind = str(record.get("type") or "")
        if kind not in ("user", "assistant"):
            # mode, permission-mode, file-history-snapshot, attachment,
            # last-prompt, system: bookkeeping, no conversation in them
            continue

        message = record.get("message")
        if not isinstance(message, dict):
            continue

        for block in _blocks(message.get("content")):
            btype = str(block.get("type") or "")
            if btype == "text":
                body = str(block.get("text") or "").strip()
                if body:
                    messages.append(
                        {"role": kind, "content": body, "kind": "message", "ts": stamp}
                    )
            elif btype == "thinking":
                if include_thinking:
                    body = str(block.get("thinking") or "").strip()
                    if body:
                        messages.append(
                            {
                                "role": "assistant",
                                "content": body,
                                "kind": "thinking",
                                "ts": stamp,
                            }
                        )
            elif btype == "tool_use":
                messages.append(
                    {
                        "role": "assistant",
                        "content": _tool_call_text(block),
                        "kind": "action",
                        "tool": str(block.get("name") or ""),
                        "ts": stamp,
                    }
                )
            elif btype == "tool_result":
                # arrives as a "user" record, but the user did not type it
                messages.append(
                    {
                        "role": "user",
                        "content": _clip(block.get("content"), OBSERVATION_LIMIT),
                        "kind": "observation",
                        "failed": bool(block.get("is_error")),
                        "ts": stamp,
                    }
                )

    if not messages:
        raise HistoryError("no conversation found in this file")

    first_user = next(
        (
            m["content"]
            for m in messages
            if m["role"] == "user" and m["kind"] == "message" and m["content"]
        ),
        "",
    )
    spoken = sum(1 for m in messages if m["kind"] == "message")
    return {
        "id": new_id(),
        "title": title_from(first_user),
        "created": started or now_ms(),
        "updated": ended or now_ms(),
        "storage": "local",
        "source": "claude-code",
        "conversation_id": "",
        "agent": {},
        "origin": {
            "tool": "claude-code",
            "session_id": session_id,
            "cwd": cwd,
            "version": version,
            "skipped_lines": skipped,
            "spoken_messages": spoken,
        },
        "messages": messages,
    }


def _stamp(raw: Any) -> int:
    """ISO-8601 (what Claude Code writes) or epoch millis, to epoch millis."""
    if isinstance(raw, (int, float)) and raw > 0:
        return int(raw)
    text = str(raw or "").strip()
    if not text:
        return 0
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        from datetime import datetime

        return int(datetime.fromisoformat(text).timestamp() * 1000)
    except Exception:
        return 0


def import_claude_file(
    root: str | os.PathLike[str],
    path: str | os.PathLike[str],
    *,
    include_thinking: bool = False,
) -> Dict[str, Any]:
    """Read one session file and keep it as a local chat."""
    source = Path(os.path.expandvars(os.path.expanduser(str(path))))
    if not source.is_file():
        raise HistoryError(f"no such file: {source}")
    try:
        if source.stat().st_size > MAX_IMPORT_BYTES:
            raise HistoryError("that session file is too large to import")
        text = source.read_text("utf-8", errors="replace")
    except HistoryError:
        raise
    except Exception as exc:
        raise HistoryError(f"could not read the file: {exc}") from exc

    record = parse_claude_session(text, include_thinking=include_thinking)
    record["origin"]["file"] = str(source)
    return Store(root).save(record)


def claude_projects_dir() -> Path:
    return Path(os.path.expanduser("~")) / ".claude" / "projects"


def discover_claude_sessions(limit: int = 60) -> List[Dict[str, Any]]:
    """Session files Claude Code left on this machine, newest first.

    Offered so the user can pick from a list instead of hunting for a uuid
    filename; an explicit path is always accepted too.
    """
    base = claude_projects_dir()
    if not base.is_dir():
        return []
    found: List[Dict[str, Any]] = []
    for path in base.glob("*/*.jsonl"):
        try:
            stat = path.stat()
        except OSError:
            continue
        found.append(
            {
                "path": str(path),
                "project": path.parent.name,
                "name": path.stem,
                "bytes": stat.st_size,
                "updated": int(stat.st_mtime * 1000),
            }
        )
    found.sort(key=lambda row: row["updated"], reverse=True)
    return found[: max(1, limit)]


# ---------------------------------------------------------------------------
# web history
# ---------------------------------------------------------------------------

def remote_chats(root: str | os.PathLike[str]) -> Dict[str, Any]:
    """Chats Notion is holding for the selected custom agent.

    Only the custom-agent listing is verified against the real workspace, so
    that is all this promises. With the default assistant selected the answer
    says so plainly instead of inventing an endpoint.
    """
    from .agents import AgentError, load_account, recent_chats
    from .config import Config, read_agent_selection

    try:
        account = load_account(root)
    except Exception as exc:
        return {"chats": [], "error": str(exc)}
    if not account.get("token_v2"):
        return {"chats": [], "error": "sign in to Notion first"}

    selection = read_agent_selection(Config(root).as_json().get("upstream", {}))
    if selection.get("mode") != "custom" or not selection.get("agent_id"):
        return {
            "chats": [],
            "error": "web history is listed for custom agents; "
            "open notion.ai for default assistant chats",
        }
    try:
        return {"chats": recent_chats(account, selection["agent_id"])}
    except AgentError as exc:
        return {"chats": [], "error": str(exc)}


def main(argv: Optional[List[str]] = None) -> int:
    """``python -m ide.history`` - list, import, inspect. Handy without the UI."""
    import argparse

    parser = argparse.ArgumentParser(description="monoIDE chat history")
    parser.add_argument("--workspace", default=os.getcwd())
    parser.add_argument("--import", dest="import_path", default="")
    parser.add_argument("--discover", action="store_true")
    parser.add_argument("--thinking", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    if args.discover:
        rows = discover_claude_sessions()
        if args.json:
            print(json.dumps(rows, ensure_ascii=False, indent=1))
        elif not rows:
            print(f"no Claude Code sessions under {claude_projects_dir()}")
        else:
            for row in rows:
                print(f"{row['project']:<40} {row['bytes']:>9}  {row['path']}")
        return 0

    if args.import_path:
        try:
            record = import_claude_file(
                args.workspace, args.import_path, include_thinking=args.thinking
            )
        except HistoryError as exc:
            print(f"import failed: {exc}")
            return 1
        origin = record.get("origin", {})
        print(f"imported {record['id']}  {record['title']}")
        print(
            f"  {len(record['messages'])} entries, "
            f"{origin.get('spoken_messages', 0)} spoken, "
            f"{origin.get('skipped_lines', 0)} lines skipped"
        )
        return 0

    rows = Store(args.workspace).list()
    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=1))
        return 0
    if not rows:
        print("no local chats yet")
    for row in rows:
        print(f"{row['id']}  {row['messages']:>3} msg  {row['title']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
