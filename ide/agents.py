"""The signed-in account's custom Notion agents.

Picking an agent by pasting a link works, but it is a poor deal: the link has to
be hunted down in Notion and a typo silently falls back to the default
assistant. The workspace picker already lists spaces by token_v2, so agents can
be listed the same way.

Everything below was confirmed against real traffic - no guessed endpoints:

* ``POST /api/v3/getCustomAgents`` with ``{"spaceId": ...}`` answers
  ``{"agentIds": [...], "mostRecentTranscripts": [{id, title, parent_id, ...}]}``.
  Ids only, no names.
* ``POST /api/v3/syncRecordValues`` with a ``workflow`` pointer returns the
  agent record: ``data.name``, ``data.icon``, ``data.model.type``,
  ``data.status``, ``data.modules``.
* ``POST /api/v3/getWorkflowCreditUsage`` with ``{spaceId, workflowId}`` answers
  ``{creditUsage, trialEstimatedCreditUsage}`` - a cheap existence check.
* ``POST /api/v3/getInferenceTranscriptsForWorkflow`` returns an agent's chats
  (kept for the chat-history work, not used here).

All of it rides on the cookie of the account the IDE already signed in with, so
nothing agent-specific is ever hardcoded: an agent only exists inside the
workspace that owns it.

Also runnable on its own, which is handy when a user reports "my agent is not in
the list"::

    python -m ide.agents                     # list agents of the signed-in account
    python -m ide.agents <link-or-id>        # check one agent
    python -m ide.agents --workspace C:/proj # when run outside the project root
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .auth import AuthStore, NOTION_URL, USER_AGENT

_UUID = re.compile(
    r"[0-9a-fA-F]{8}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{12}"
)


class AgentError(RuntimeError):
    pass


def agent_id_from(raw: str) -> str:
    """Accept a link, a dashed id or a dashless id; answer a dashed uuid."""
    text = str(raw or "").split("?")[0].split("#")[0]
    found = _UUID.findall(text)
    if not found:
        return ""
    digits = re.sub(r"[^0-9a-fA-F]", "", found[-1]).lower()
    if len(digits) != 32:
        return ""
    return "-".join([digits[:8], digits[8:12], digits[12:16], digits[16:20], digits[20:]])


# ---------------------------------------------------------------------------
# transport
# ---------------------------------------------------------------------------

def _cookie_header(account: Dict[str, Any]) -> str:
    cookies = dict(account.get("cookies") or {})
    cookies["token_v2"] = str(account.get("token_v2") or "")
    if account.get("user_id"):
        cookies.setdefault("notion_user_id", str(account["user_id"]))
    if not cookies["token_v2"]:
        raise AgentError("not signed in to Notion yet")
    return "; ".join("%s=%s" % (name, value) for name, value in cookies.items() if value)


def _post(path: str, body: Dict[str, Any], account: Dict[str, Any],
          timeout: float = 30.0) -> Dict[str, Any]:
    request = Request(
        "%s/api/v3/%s" % (NOTION_URL, path.lstrip("/")),
        data=json.dumps(body).encode("utf-8"),
        method="POST",
        headers={
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
            "Origin": NOTION_URL,
            "Referer": NOTION_URL + "/ai",
            "Cookie": _cookie_header(account),
            "x-notion-space-id": str(account.get("space_id") or ""),
            "x-notion-active-user-header": str(account.get("user_id") or ""),
            "notion-audit-log-platform": "web",
        },
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8", "replace"))
    except HTTPError as error:
        if error.code in (401, 403):
            raise AgentError("Notion rejected the token (HTTP %d) - sign in again" % error.code)
        if error.code == 404:
            raise AgentError("Notion has no %s endpoint (HTTP 404)" % path)
        raise AgentError("Notion %s failed: HTTP %d" % (path, error.code))
    except URLError as error:
        raise AgentError("cannot reach Notion: %s" % error.reason)
    return payload if isinstance(payload, dict) else {}


def load_account(root: Path) -> Dict[str, Any]:
    account = AuthStore(Path(root)).account
    if not account:
        raise AgentError("no Notion account saved yet - sign in through the IDE first")
    return account


# ---------------------------------------------------------------------------
# reading agent records
# ---------------------------------------------------------------------------

def _unwrap(raw: Any) -> Dict[str, Any]:
    """syncRecordValues nests as {value: {value: {...}}}; tolerate both depths."""
    if not isinstance(raw, dict):
        return {}
    inner = raw.get("value")
    if isinstance(inner, dict):
        deeper = inner.get("value")
        if isinstance(deeper, dict):
            return deeper
        return inner
    return raw


def _describe(record: Dict[str, Any], agent_id: str) -> Dict[str, Any]:
    data = record.get("data")
    data = data if isinstance(data, dict) else {}
    model = data.get("model")
    model_name = ""
    if isinstance(model, dict):
        model_name = str(model.get("type") or "")
    elif model:
        model_name = str(model)
    modules = data.get("modules")
    return {
        "id": agent_id,
        "name": str(data.get("name") or "").strip() or "untitled agent",
        "icon": str(data.get("icon") or ""),
        "model": model_name,
        "status": str(data.get("status") or ""),
        "connections": [
            str(item.get("name") or item.get("type") or "")
            for item in (modules if isinstance(modules, list) else [])
            if isinstance(item, dict)
        ],
        "alive": record.get("alive") is not False,
    }


def fetch_records(account: Dict[str, Any], agent_ids: List[str]) -> Dict[str, Dict[str, Any]]:
    """Batch-read workflow records. One request for all ids."""
    if not agent_ids:
        return {}
    space_id = str(account.get("space_id") or "")
    payload = _post(
        "syncRecordValues",
        {
            "requests": [
                {
                    "pointer": {"table": "workflow", "id": agent_id, "spaceId": space_id},
                    "version": -1,
                }
                for agent_id in agent_ids
            ]
        },
        account,
    )
    record_map = payload.get("recordMap")
    workflows = (record_map or {}).get("workflow") if isinstance(record_map, dict) else {}
    out: Dict[str, Dict[str, Any]] = {}
    for agent_id, raw in (workflows or {}).items():
        record = _unwrap(raw)
        if record:
            out[str(agent_id)] = record
    return out


def list_agents(account: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Every custom agent this account can open, newest chat first.

    getCustomAgents hands out ids plus the most recent transcripts; the names
    come from the workflow records themselves.
    """
    space_id = str(account.get("space_id") or "")
    payload = _post("getCustomAgents", {"spaceId": space_id}, account)
    agent_ids = [str(item) for item in (payload.get("agentIds") or []) if item]

    # most recent chat per agent, so the list can be ordered like Notion's own
    last_seen: Dict[str, Dict[str, Any]] = {}
    for transcript in payload.get("mostRecentTranscripts") or []:
        if not isinstance(transcript, dict):
            continue
        owner = str(transcript.get("parent_id") or "")
        if not owner:
            continue
        try:
            updated = int(transcript.get("updated_time") or 0)
        except (TypeError, ValueError):
            updated = 0
        current = last_seen.get(owner)
        if current is None or updated > int(current.get("updated") or 0):
            last_seen[owner] = {
                "updated": updated,
                "title": str(transcript.get("title") or ""),
                "thread_id": str(transcript.get("id") or ""),
            }

    records = fetch_records(account, agent_ids)
    rows: List[Dict[str, Any]] = []
    for agent_id in agent_ids:
        record = records.get(agent_id)
        row = _describe(record or {}, agent_id)
        recent = last_seen.get(agent_id) or {}
        row["last_chat"] = recent.get("title", "")
        row["last_used"] = int(recent.get("updated") or 0)
        # an agent with no readable record is still selectable; only the label suffers
        row["readable"] = bool(record)
        rows.append(row)
    rows.sort(key=lambda row: row["last_used"], reverse=True)
    return rows


def credit_usage(account: Dict[str, Any], agent_id: str) -> Optional[int]:
    try:
        payload = _post(
            "getWorkflowCreditUsage",
            {"spaceId": str(account.get("space_id") or ""), "workflowId": agent_id},
            account,
        )
    except AgentError:
        return None
    try:
        return int(payload.get("creditUsage"))
    except (TypeError, ValueError):
        return None


def verify(account: Dict[str, Any], raw: str) -> Dict[str, Any]:
    """Resolve a pasted link or id into a real agent of this workspace."""
    agent_id = agent_id_from(raw)
    if not agent_id:
        return {"ok": False, "id": "", "detail": "no 32-character id found in that link"}
    records = fetch_records(account, [agent_id])
    record = records.get(agent_id)
    if not record:
        # the record may be unreadable while the agent still exists
        if credit_usage(account, agent_id) is not None:
            return {
                "ok": True,
                "id": agent_id,
                "name": "custom agent",
                "detail": "agent exists, but its record could not be read",
            }
        return {
            "ok": False,
            "id": agent_id,
            "detail": "this workspace has no agent with that id - "
                      "the link may belong to another workspace or account",
        }
    described = _describe(record, agent_id)
    described["ok"] = True
    described["detail"] = ""
    return described


# ---------------------------------------------------------------------------
# cli
# ---------------------------------------------------------------------------

def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="list or check custom Notion agents")
    parser.add_argument("agent", nargs="?", default="", help="agent link or id to check")
    parser.add_argument("--workspace", default=".", help="project folder holding .monoide")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args(argv)

    try:
        account = load_account(Path(args.workspace).expanduser().resolve())
        if args.agent:
            result = verify(account, args.agent)
        else:
            result = {"agents": list_agents(account)}
    except AgentError as error:
        print("error: %s" % error, file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    if args.agent:
        if not result.get("ok"):
            print("not usable: %s" % result.get("detail"))
            return 1
        print("%s  %s" % (result["id"], result.get("name", "")))
        if result.get("model"):
            print("  model: %s" % result["model"])
        if result.get("detail"):
            print("  note: %s" % result["detail"])
        return 0

    rows = result["agents"]
    if not rows:
        print("this account has no custom agents")
        return 0
    for row in rows:
        print("%s  %-28s %s" % (row["id"], row["name"], row.get("model", "")))
        if row.get("last_chat"):
            print("%s  last chat: %s" % (" " * 36, row["last_chat"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
