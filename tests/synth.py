"""Synthetic fixture builders. Every conversation here is invented for the tests; nothing comes from a real one."""

from __future__ import annotations

import json
from pathlib import Path

FAKE_SK = "sk-" + "A1b2C3d4" * 4          # obviously fake, key-shaped
FAKE_GH = "ghp_" + "Zz9Yy8Xx" * 5
FAKE_DB = "postgresql://appuser:s3cretpassw0rd@db.example.test:5432/app"


def jl(path: Path, lines: list[dict | str]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join((x if isinstance(x, str) else json.dumps(x)) + "\n" for x in lines), encoding="utf-8")
    return path


def claude_code_session(root: Path, project: str = "-work-demo", sid: str = "sess-0001") -> Path:
    base = {"sessionId": sid, "entrypoint": "cli"}
    lines = [
        {**base, "type": "custom-title", "customTitle": "Fixing the widget"},
        {**base, "type": "user", "uuid": "u1", "timestamp": "2026-03-01T10:00:00.000Z",
         "message": {"role": "user", "content": "Please look at /srv/app/widget.py and commit abc1234 for me"}},
        {**base, "type": "assistant", "uuid": "a1", "timestamp": "2026-03-01T10:00:05.000Z",
         "message": {"role": "assistant", "model": "model-x", "content": [
             {"type": "thinking", "thinking": "Considering the widget."},
             {"type": "text", "text": "Opening the file now."},
             {"type": "tool_use", "id": "tool1", "name": "Read", "input": {"file_path": "/srv/app/widget.py"}}]}},
        {**base, "type": "user", "uuid": "u2", "timestamp": "2026-03-01T10:00:06.000Z",
         "message": {"role": "user", "content": [
             {"type": "tool_result", "tool_use_id": "tool1", "content": "def widget(): pass"}]}},
        {**base, "type": "user", "uuid": "u3", "timestamp": "2026-03-01T10:00:07.000Z",
         "message": {"role": "user", "content": "<system-reminder>be kind</system-reminder>"}},
        {**base, "type": "user", "uuid": "u4", "timestamp": "2026-03-01T10:00:08.000Z",
         "origin": {"kind": "peer", "name": "Robo Helper"},
         "message": {"role": "user", "content": "from another agent: the build is green"}},
        {**base, "type": "assistant", "uuid": "a2", "isSidechain": True, "timestamp": "2026-03-01T10:00:09.000Z",
         "message": {"role": "assistant", "content": [{"type": "text", "text": "subagent chatter"}]}},
        {**base, "type": "assistant", "uuid": "a3", "timestamp": "2026-03-01T10:01:00.000Z",
         "message": {"role": "assistant", "model": "model-x", "content": [
             {"type": "text", "text": f"Done. The key {FAKE_SK} was in the file, so I removed it."}]}},
        "{this line is broken json",
        {**base, "type": "ai-title", "aiTitle": "Ignored because a custom title exists"},
    ]
    return jl(root / project / f"{sid}.jsonl", lines)


def mirror_tree(root: Path) -> Path:
    sid = "mirror-sess-1"
    d1, d2 = root / "2026-04-01", root / "2026-04-02"
    t0 = 1775037600000  # 2026-04-01 10:00 UTC, in ms
    jl(d1 / f"{sid}.jsonl", [
        {"t": t0, "event": "session_fresh", "sid": sid, "how": "start"},
        {"t": t0 + 1000, "sid": sid, "uuid": "m1", "door": "prompt", "origin": "composer", "role": "user",
         "text": "What is the plan for the garden?"},
        {"t": t0 + 2000, "sid": sid, "uuid": "m2", "door": "tool-result", "role": "assistant",
         "text": "Let me look.", "tools": ["Read({\"file_path\":\"/home/user/garden.md\"})"]},
        {"t": t0 + 3000, "sid": sid, "uuid": "m3", "door": "tool-result", "role": "user", "results": ["tulips and beans"]},
        {"t": t0 + 4000, "sid": sid, "uuid": "m4", "door": "prompt", "origin": "composer", "role": "user",
         "queued": "prompt", "text": "also add herbs"},
        {"t": t0 + 5000, "sid": sid, "uuid": "m5", "door": "command", "role": "user", "meta": True,
         "text": "<command-name>/clear</command-name>"},
        {"t": t0 + 1000, "sid": sid, "uuid": "m1", "door": "prompt", "origin": "composer", "role": "user",
         "text": "What is the plan for the garden?"},          # a retried flush wrote this row twice
        "{cut short by a crash",
        {"t": t0 + 6000, "event": "compaction", "trigger": "manual", "session": sid,
         "summary": "We planned the garden: tulips, beans, herbs."},
        {"t": t0 + 7000, "event": "memory_formed", "sid": sid},
    ])
    jl(d2 / f"{sid}.jsonl", [
        {"t": t0 + 86400000, "sid": sid, "uuid": "m6", "door": "tool-result", "role": "assistant",
         "text": "Herbs added to the plan."},
    ])
    jl(d1 / f"{sid}.memories.jsonl", [
        {"t": t0 + 8000, "event": "memory", "level": 1, "sid": sid, "ordinal": 0, "text": "We sketched the garden plan.",
         "from_uuid": "m1", "to_uuid": "m5", "t_from": t0 + 1000, "t_to": t0 + 5000, "est_tokens": 900, "rows": 5,
         "source_hash": "abcd1234abcd1234", "mac": "deadbeef"},
    ])
    return root


def nexus_gpt(root: Path, name: str = "Trip planning.md", conv: str = "00000000-0000-0000-0000-000000000001",
              update: str = "03/05/2026 at 9:30 AM") -> Path:
    body = f"""---
conversation_id: {conv}
aliases: Trip planning
create_time: 03/05/2026 at 9:00 AM
update_time: {update}
provider: chatgpt
---

### User, on 03/05/2026 at 9:00 AM;
> Where should we stay in Lisbon?
> Near https://example.test/maps?q=lisbon please.
<!-- UID: u-1 -->

---
#### ChatGPT, on 03/05/2026 at 9:01 AM;
>> Try the old town.
>>
>> It is walkable.
<!-- UID: a-1 -->

---
#### ChatGPT, on 03/05/2026 at 9:02 AM;
>> User prefers quiet hotels.
<!-- UID: a-2 -->

---
#### ChatGPT, on 03/05/2026 at 9:02 PM;
>> Model set context updated.
<!-- UID: a-3 -->
"""
    p = root / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body, encoding="utf-8")
    return p


def nexus_claude(root: Path, name: str = "Recipe help.md", conv: str = "00000000-0000-0000-0000-000000000002") -> Path:
    body = f"""---
conversation_id: {conv}
aliases: Recipe help
create_time: 2026-03-10T12:00:00.000Z
update_time: 2026-03-10T12:05:00.000Z
provider: claude
---

>[!nexus_user] **User** - 10/03/2026 at 12:00:00
> How long do I boil an egg?
> Soft please.
<!-- UID: cu-1 -->

---
>[!nexus_agent] **Assistant** - 10/03/2026 at 12:00:30
> Six minutes.
>>[!nexus_attachment] **timer.png**
>>[!nexus_artifact] **egg-notes** v1
<!-- UID: ca-1 -->
"""
    p = root / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body, encoding="utf-8")
    return p
