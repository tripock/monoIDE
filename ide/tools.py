"""Tool implementations for the bridge runner. Pure stdlib.

Every tool takes (ctx, args) and returns a string observation. Exceptions are
turned into readable ERROR observations by the caller, so failures stay visible
to the model instead of silently becoming "success".
"""

from __future__ import annotations

import fnmatch
import os
import re
import shlex
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

BINARY_SNIFF = 4096


class ToolError(Exception):
    pass


class ApprovalRequired(Exception):
    def __init__(self, summary: str):
        super().__init__(summary)
        self.summary = summary


@dataclass
class ToolContext:
    root: Path
    config: Any
    mcp: Any = None
    lsp: Any = None
    auto_approve: bool = False
    approvals: Optional[set] = None  # per-session granted action ids
    on_event: Optional[Callable[[str, Dict[str, Any]], None]] = None

    # ---- path safety -----------------------------------------------------
    def resolve(self, raw: str, *, must_exist: bool = False) -> Path:
        if raw is None:
            raise ToolError("missing 'path'")
        candidate = Path(str(raw)).expanduser()
        path = candidate if candidate.is_absolute() else (self.root / candidate)
        path = Path(os.path.normpath(str(path)))
        if self.config.get("sandbox", "deny_outside_root", default=True):
            try:
                path.resolve().relative_to(self.root.resolve())
            except Exception:
                raise ToolError(
                    f"path escapes the workspace root ({self.root}): {raw}"
                )
        if must_exist and not path.exists():
            raise ToolError(f"no such path: {self.rel(path)}")
        return path

    def rel(self, path: Path) -> str:
        try:
            return str(path.resolve().relative_to(self.root.resolve())) or "."
        except Exception:
            return str(path)

    def ignored(self, name: str) -> bool:
        return name in set(self.config.ignore)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _is_binary(path: Path) -> bool:
    try:
        with path.open("rb") as handle:
            return b"\0" in handle.read(BINARY_SNIFF)
    except OSError:
        return False


def _read_text(path: Path) -> str:
    return path.read_text("utf-8", errors="replace")


def _numbered(text: str, start: int = 1) -> str:
    lines = text.splitlines()
    width = len(str(start + len(lines) - 1))
    return "\n".join(f"{str(i + start).rjust(width)}│{line}" for i, line in enumerate(lines))


def _walk(ctx: ToolContext, base: Path, max_entries: int = 20000):
    ignore = set(ctx.config.ignore)
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = sorted(d for d in dirnames if d not in ignore and not d.startswith(".git"))
        for name in sorted(filenames):
            yield Path(dirpath) / name
            max_entries -= 1
            if max_entries <= 0:
                return


# ---------------------------------------------------------------------------
# tools
# ---------------------------------------------------------------------------

def t_list_dir(ctx: ToolContext, args: Dict[str, Any]) -> str:
    path = ctx.resolve(args.get("path", "."), must_exist=True)
    if not path.is_dir():
        raise ToolError(f"not a directory: {ctx.rel(path)}")
    rows: List[str] = []
    for entry in sorted(path.iterdir(), key=lambda p: (p.is_file(), p.name.lower())):
        if ctx.ignored(entry.name):
            continue
        if entry.is_dir():
            rows.append(f"dir   {entry.name}/")
        else:
            try:
                size = entry.stat().st_size
            except OSError:
                size = 0
            rows.append(f"file  {entry.name}  {size}b")
    header = f"{ctx.rel(path)} ({len(rows)} entries)"
    return header + "\n" + ("\n".join(rows) if rows else "(empty)")


def t_read_file(ctx: ToolContext, args: Dict[str, Any]) -> str:
    path = ctx.resolve(args.get("path"), must_exist=True)
    if path.is_dir():
        raise ToolError(f"{ctx.rel(path)} is a directory; use list_dir")
    if _is_binary(path):
        return f"{ctx.rel(path)}: binary file, {path.stat().st_size} bytes (not shown)"
    text = _read_text(path)
    offset = int(args.get("offset") or 1)
    limit = args.get("limit")
    lines = text.splitlines()
    total = len(lines)
    start = max(1, offset)
    end = total if limit in (None, 0) else min(total, start + int(limit) - 1)
    chunk = "\n".join(lines[start - 1:end])
    head = f"{ctx.rel(path)} lines {start}-{end} of {total}"
    return head + "\n" + _numbered(chunk, start)


def t_write_file(ctx: ToolContext, args: Dict[str, Any]) -> str:
    path = ctx.resolve(args.get("path"))
    content = args.get("content")
    if content is None:
        raise ToolError("missing 'content'")
    _require_approval(ctx, "write", f"write {ctx.rel(path)} ({len(str(content))} chars)")
    existed = path.exists()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(str(content), "utf-8")
    _notify(ctx, path)
    verb = "overwrote" if existed else "created"
    return f"{verb} {ctx.rel(path)} ({len(str(content).splitlines())} lines)"


def t_edit_file(ctx: ToolContext, args: Dict[str, Any]) -> str:
    path = ctx.resolve(args.get("path"), must_exist=True)
    edits = args.get("edits")
    if not edits:
        one = {
            "old_string": args.get("old_string"),
            "new_string": args.get("new_string", ""),
            "replace_all": bool(args.get("replace_all")),
        }
        if one["old_string"] is None:
            raise ToolError("missing 'old_string' (or 'edits')")
        edits = [one]
    _require_approval(ctx, "write", f"edit {ctx.rel(path)} ({len(edits)} edit(s))")
    text = _read_text(path)
    report: List[str] = []
    for index, edit in enumerate(edits, 1):
        old = str(edit.get("old_string", ""))
        new = str(edit.get("new_string", ""))
        if not old:
            raise ToolError(f"edit #{index}: empty 'old_string'")
        count = text.count(old)
        if count == 0:
            raise ToolError(
                f"edit #{index}: 'old_string' not found in {ctx.rel(path)}. "
                "Re-read the file and copy the exact text."
            )
        if count > 1 and not edit.get("replace_all"):
            raise ToolError(
                f"edit #{index}: 'old_string' occurs {count} times in "
                f"{ctx.rel(path)}. Add more context or set replace_all."
            )
        text = text.replace(old, new) if edit.get("replace_all") else text.replace(old, new, 1)
        report.append(f"edit #{index}: {count if edit.get('replace_all') else 1} replacement(s)")
    path.write_text(text, "utf-8")
    _notify(ctx, path)
    return f"patched {ctx.rel(path)}\n" + "\n".join(report)


def t_delete_path(ctx: ToolContext, args: Dict[str, Any]) -> str:
    import shutil

    path = ctx.resolve(args.get("path"), must_exist=True)
    _require_approval(ctx, "write", f"delete {ctx.rel(path)}")
    if path.is_dir():
        shutil.rmtree(path)
    else:
        path.unlink()
    return f"deleted {ctx.rel(path)}"


def t_glob(ctx: ToolContext, args: Dict[str, Any]) -> str:
    pattern = str(args.get("pattern") or "*")
    base = ctx.resolve(args.get("path", "."), must_exist=True)
    limit = int(args.get("limit") or 200)
    hits: List[str] = []
    for file_path in _walk(ctx, base):
        rel = ctx.rel(file_path)
        if fnmatch.fnmatch(rel, pattern) or fnmatch.fnmatch(file_path.name, pattern):
            hits.append(rel)
            if len(hits) >= limit:
                break
    return f"glob {pattern}: {len(hits)} match(es)\n" + ("\n".join(hits) or "(none)")


def t_grep(ctx: ToolContext, args: Dict[str, Any]) -> str:
    pattern = args.get("pattern")
    if not pattern:
        raise ToolError("missing 'pattern'")
    base = ctx.resolve(args.get("path", "."), must_exist=True)
    glob_filter = args.get("glob")
    limit = int(args.get("limit") or 120)
    flags = 0 if args.get("case_sensitive") else re.IGNORECASE
    try:
        regex = re.compile(str(pattern), flags)
    except re.error as exc:
        raise ToolError(f"bad regex: {exc}")
    out: List[str] = []
    files = [base] if base.is_file() else _walk(ctx, base)
    for file_path in files:
        rel = ctx.rel(file_path)
        if glob_filter and not (
            fnmatch.fnmatch(rel, str(glob_filter)) or fnmatch.fnmatch(file_path.name, str(glob_filter))
        ):
            continue
        if _is_binary(file_path):
            continue
        try:
            for number, line in enumerate(_read_text(file_path).splitlines(), 1):
                if regex.search(line):
                    out.append(f"{rel}:{number}: {line.strip()[:300]}")
                    if len(out) >= limit:
                        raise StopIteration
        except StopIteration:
            break
        except OSError:
            continue
    return f"grep {pattern}: {len(out)} hit(s)\n" + ("\n".join(out) or "(none)")


def t_bash(ctx: ToolContext, args: Dict[str, Any]) -> str:
    command = args.get("command")
    if not command:
        raise ToolError("missing 'command'")
    command = str(command)
    for blocked in ctx.config.get("sandbox", "blocked_patterns", default=[]) or []:
        if blocked in command:
            raise ToolError(f"command blocked by policy (matched {blocked!r})")
    cwd = ctx.resolve(args.get("cwd", "."), must_exist=True)
    timeout = int(args.get("timeout") or ctx.config.get("sandbox", "bash_timeout", default=120))
    _require_approval(ctx, "bash", f"$ {command}")

    preexec = None
    mem_mb = int(ctx.config.get("sandbox", "bash_memory_mb", default=0) or 0)
    if mem_mb and os.name != "nt":
        import resource

        def preexec() -> None:  # noqa: E306
            limit = mem_mb * 1024 * 1024
            resource.setrlimit(resource.RLIMIT_AS, (limit, limit))
            os.setsid()

    started = time.time()
    try:
        proc = subprocess.run(
            command,
            shell=True,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=timeout,
            preexec_fn=preexec,
        )
    except subprocess.TimeoutExpired:
        return f"$ {command}\n[timeout after {timeout}s - process killed]"
    elapsed = time.time() - started
    body = (proc.stdout or "") + (("\n[stderr]\n" + proc.stderr) if proc.stderr else "")
    body = body.strip() or "(no output)"
    return f"$ {command}\n[exit {proc.returncode} in {elapsed:.1f}s]\n{body}"


def t_lsp_diagnostics(ctx: ToolContext, args: Dict[str, Any]) -> str:
    if ctx.lsp is None:
        raise ToolError("language server support is disabled")
    path = ctx.resolve(args.get("path"), must_exist=True)
    items = ctx.lsp.diagnostics(path)
    if items is None:
        return f"no language server available for {ctx.rel(path)}"
    if not items:
        return f"{ctx.rel(path)}: no diagnostics"
    rows = []
    for item in items[:100]:
        line = item.get("range", {}).get("start", {}).get("line", 0) + 1
        sev = {1: "error", 2: "warn", 3: "info", 4: "hint"}.get(item.get("severity", 1), "info")
        rows.append(f"{ctx.rel(path)}:{line}: {sev}: {item.get('message', '')}")
    return "\n".join(rows)


def t_mcp_list(ctx: ToolContext, args: Dict[str, Any]) -> str:
    if ctx.mcp is None:
        raise ToolError("MCP support is disabled")
    catalog = ctx.mcp.catalog()
    if not catalog:
        return "no MCP servers configured"
    rows = []
    for server, tools in catalog.items():
        rows.append(f"[{server}]")
        for tool in tools:
            rows.append(f"  {tool['name']}: {(tool.get('description') or '').strip()[:200]}")
            if tool.get("inputSchema"):
                keys = list((tool["inputSchema"].get("properties") or {}).keys())
                if keys:
                    rows.append(f"    args: {', '.join(keys)}")
    return "\n".join(rows)


def t_mcp_call(ctx: ToolContext, args: Dict[str, Any]) -> str:
    if ctx.mcp is None:
        raise ToolError("MCP support is disabled")
    server = args.get("server")
    name = args.get("tool") or args.get("name")
    if not server or not name:
        raise ToolError("mcp_call needs 'server' and 'tool'")
    _require_approval(ctx, "mcp", f"mcp {server}.{name}")
    return ctx.mcp.call(str(server), str(name), args.get("arguments") or {})


# ---------------------------------------------------------------------------
# approvals / events
# ---------------------------------------------------------------------------

def _require_approval(ctx: ToolContext, kind: str, summary: str) -> None:
    if ctx.auto_approve:
        return
    if ctx.approvals is not None and kind in ctx.approvals:
        return
    raise ApprovalRequired(summary)


def _notify(ctx: ToolContext, path: Path) -> None:
    if ctx.on_event:
        ctx.on_event("file_changed", {"path": ctx.rel(path)})


REGISTRY: Dict[str, Callable[[ToolContext, Dict[str, Any]], str]] = {
    "list_dir": t_list_dir,
    "read_file": t_read_file,
    "write_file": t_write_file,
    "edit_file": t_edit_file,
    "delete_path": t_delete_path,
    "glob": t_glob,
    "grep": t_grep,
    "bash": t_bash,
    "lsp_diagnostics": t_lsp_diagnostics,
    "mcp_list": t_mcp_list,
    "mcp_call": t_mcp_call,
}

WRITE_TOOLS = {"write_file", "edit_file", "delete_path"}

TOOLS_DOC = """\
- list_dir {"path": "."} -> directory listing
- read_file {"path": "a/b.py", "offset": 1, "limit": 400} -> numbered lines
- write_file {"path": "a/b.py", "content": "..."} -> create/overwrite a whole file
- edit_file {"path": "a/b.py", "old_string": "...", "new_string": "...", "replace_all": false}
    also accepts {"path": ..., "edits": [{"old_string": ..., "new_string": ...}, ...]}
- delete_path {"path": "tmp/x"} -> delete file or directory
- glob {"pattern": "**/*.ts", "path": "."} -> matching files
- grep {"pattern": "def main", "glob": "*.py", "case_sensitive": false} -> file:line hits
- bash {"command": "pytest -q", "cwd": ".", "timeout": 120} -> stdout/stderr + exit code
- lsp_diagnostics {"path": "a/b.py"} -> real compiler/linter diagnostics
- mcp_list {} -> connected MCP servers and their tools
- mcp_call {"server": "name", "tool": "tool_name", "arguments": {...}} -> MCP tool result

write_file / edit_file / delete_path / bash / mcp_call may require the user to
press an approval button; the OBSERVATION will tell you if that happened."""
