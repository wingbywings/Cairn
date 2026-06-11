from fastapi import APIRouter, HTTPException
from fastapi.responses import Response
from datetime import datetime
import yaml

from cairn.server.db import get_conn
from cairn.server.services import expire_reason_leases, expire_workers, get_project_or_404

router = APIRouter(tags=["export"])


def format_export_timestamp(value: str | None) -> str | None:
    if not value:
        return value
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return value
    return dt.astimezone().strftime("%Y-%m-%d %H:%M:%S")


def _load_project_data(conn, project_id: str):
    expire_workers(conn, project_id)
    expire_reason_leases(conn, project_id)
    proj = get_project_or_404(conn, project_id)

    facts = conn.execute(
        "SELECT id, description FROM facts WHERE project_id = ?", (project_id,)
    ).fetchall()
    hints = conn.execute(
        "SELECT * FROM hints WHERE project_id = ? ORDER BY pinned DESC, created_at",
        (project_id,),
    ).fetchall()
    intents = conn.execute(
        "SELECT * FROM intents WHERE project_id = ? ORDER BY created_at",
        (project_id,),
    ).fetchall()

    sources_by_intent = {}
    for i in intents:
        rows = conn.execute(
            "SELECT fact_id FROM intent_sources WHERE intent_id = ? AND project_id = ? ORDER BY rowid",
            (i["id"], project_id),
        ).fetchall()
        sources_by_intent[i["id"]] = [r["fact_id"] for r in rows]

    return proj, facts, hints, intents, sources_by_intent


def _export_yaml(conn, project_id: str) -> str:
    proj, facts, hints, intents, sources_by_intent = _load_project_data(conn, project_id)

    origin_desc = ""
    goal_desc = ""
    for f in facts:
        if f["id"] == "origin":
            origin_desc = f["description"]
        elif f["id"] == "goal":
            goal_desc = f["description"]

    data: dict = {
        "project": {
            "title": proj["title"],
            "origin": origin_desc,
            "goal": goal_desc,
            "bootstrap_enabled": bool(proj["bootstrap_enabled"]),
        }
    }

    if hints:
        data["hints"] = [
            {
                "content": h["content"],
                "creator": h["creator"],
                "hint_type": h["hint_type"],
                "priority": h["priority"],
                "target_type": h["target_type"],
                "target_id": h["target_id"],
                "pinned": bool(h["pinned"]),
                "created_at": format_export_timestamp(h["created_at"]),
            }
            for h in hints
        ]

    data["facts"] = [{"id": f["id"], "description": f["description"]} for f in facts]

    intent_list = []
    for i in intents:
        entry: dict = {
            "from": sources_by_intent.get(i["id"], []),
            "to": i["to_fact_id"],
            "description": i["description"],
            "creator": i["creator"],
            "worker": i["worker"],
            "created_at": format_export_timestamp(i["created_at"]),
            "concluded_at": format_export_timestamp(i["concluded_at"]),
        }
        intent_list.append(entry)

    if intent_list:
        data["intents"] = intent_list

    return yaml.dump(data, allow_unicode=True, default_flow_style=False, sort_keys=False)


def _export_timeline(conn, project_id: str) -> str:
    proj, facts, hints, intents, sources_by_intent = _load_project_data(conn, project_id)

    facts_by_id = {f["id"]: f["description"] for f in facts}

    events: list[tuple[str, int, str]] = []  # (timestamp, order, text)
    order = 0

    origin_desc = facts_by_id.get("origin", "")
    goal_desc = facts_by_id.get("goal", "")
    ts = format_export_timestamp(proj["created_at"]) or ""
    block = f"[{ts}] PROJECT CREATED\n  origin: {origin_desc}\n  goal: {goal_desc}"
    events.append((proj["created_at"] or "", order, block))
    order += 1

    for h in hints:
        ts = format_export_timestamp(h["created_at"]) or ""
        meta = f"  type: {h['hint_type']} priority: {h['priority']}"
        if h["pinned"]:
            meta += " pinned: true"
        if h["target_type"] and h["target_id"]:
            meta += f"\n  target: {h['target_type']}:{h['target_id']}"
        block = f"[{ts}] HINT by {h['creator']}\n{meta}\n  {h['content']}"
        events.append((h["created_at"] or "", order, block))
        order += 1

    for i in intents:
        src = sources_by_intent.get(i["id"], [])
        from_str = ", ".join(src)

        ts = format_export_timestamp(i["created_at"]) or ""
        meta = f"  from: {from_str}"
        if i["worker"] and not i["concluded_at"]:
            meta += f"\n  worker: {i['worker']} (in progress)"
        block = f"[{ts}] INTENT DECLARED {i['id']} by {i['creator']}\n{meta}\n  {i['description']}"
        events.append((i["created_at"] or "", order, block))
        order += 1

        if not i["concluded_at"] or not i["to_fact_id"]:
            continue

        ts = format_export_timestamp(i["concluded_at"]) or ""
        actor = i["worker"] or i["creator"]

        if i["to_fact_id"] == "goal":
            block = f"[{ts}] PROJECT COMPLETED by {actor}\n  via: {i['id']} from {from_str}"
        else:
            fact_desc = facts_by_id.get(i["to_fact_id"], "")
            block = f"[{ts}] INTENT CONCLUDED {i['id']} by {actor}\n  from: {from_str}\n  produced: {i['to_fact_id']}\n  {fact_desc}"

        events.append((i["concluded_at"] or "", order, block))
        order += 1

    events.sort(key=lambda e: (e[0], e[1]))

    return "\n\n".join(e[2] for e in events) + "\n"


def _export_report(conn, project_id: str) -> str:
    proj, facts, hints, intents, sources_by_intent = _load_project_data(conn, project_id)

    facts_by_id = {f["id"]: f["description"] for f in facts}
    producing_intent_by_fact = {
        i["to_fact_id"]: i for i in intents if i["to_fact_id"] and i["to_fact_id"] != "goal"
    }
    completion = next((i for i in intents if i["to_fact_id"] == "goal"), None)
    completion_sources = sources_by_intent.get(completion["id"], []) if completion else []

    lineage_fact_ids: list[str] = []
    seen_facts: set[str] = set()

    def visit_fact(fact_id: str) -> None:
        if fact_id in seen_facts:
            return
        seen_facts.add(fact_id)
        producer = producing_intent_by_fact.get(fact_id)
        if producer is not None:
            for source_id in sources_by_intent.get(producer["id"], []):
                visit_fact(source_id)
        lineage_fact_ids.append(fact_id)

    for source_id in completion_sources:
        visit_fact(source_id)

    completed_at = format_export_timestamp(completion["concluded_at"]) if completion else None
    completed_by = completion["worker"] or completion["creator"] if completion else None
    open_intents = [i for i in intents if not i["concluded_at"]]
    concluded_intents = [i for i in intents if i["concluded_at"]]

    lines = [
        f"# Final Report: {proj['title']}",
        "",
        "## Project",
        f"- Project ID: {proj['id']}",
        f"- Status: {proj['status']}",
        f"- Created: {format_export_timestamp(proj['created_at'])}",
        f"- Completed: {completed_at or 'not completed'}",
        f"- Completed by: {completed_by or 'n/a'}",
        f"- Bootstrap enabled: {str(bool(proj['bootstrap_enabled'])).lower()}",
        "",
        "## Objective",
        f"**Origin:** {facts_by_id.get('origin', '')}",
        "",
        f"**Goal:** {facts_by_id.get('goal', '')}",
        "",
        "## Final Outcome",
    ]

    if completion is None:
        lines.extend(
            [
                "No completion intent is present yet. This report is a current-state summary, not a final completed report.",
                "",
            ]
        )
    else:
        lines.extend(
            [
                completion["description"],
                "",
                f"- Completion intent: {completion['id']}",
                f"- Evidence facts: {', '.join(completion_sources) if completion_sources else 'none'}",
                "",
            ]
        )

    lines.extend(["## Evidence Path"])
    if lineage_fact_ids:
        for fact_id in lineage_fact_ids:
            lines.append(f"- {fact_id}: {facts_by_id.get(fact_id, '')}")
    else:
        lines.append("- No evidence path is available.")
    lines.append("")

    lines.extend(
        [
            "## Work Summary",
            f"- Facts: {len(facts)}",
            f"- Intents: {len(intents)}",
            f"- Concluded intents: {len(concluded_intents)}",
            f"- Open intents: {len(open_intents)}",
            f"- Hints: {len(hints)}",
            "",
        ]
    )

    lines.extend(["## Key Facts"])
    for fact in facts:
        lines.append(f"- {fact['id']}: {fact['description']}")
    lines.append("")

    lines.extend(["## Concluded Intents"])
    if concluded_intents:
        for intent in concluded_intents:
            source_ids = ", ".join(sources_by_intent.get(intent["id"], []))
            target = intent["to_fact_id"] or "open"
            when = format_export_timestamp(intent["concluded_at"])
            lines.append(f"- {intent['id']} ({source_ids} -> {target}) by {intent['worker'] or intent['creator']} at {when}: {intent['description']}")
    else:
        lines.append("- None.")
    lines.append("")

    lines.extend(["## Open Intents"])
    if open_intents:
        for intent in open_intents:
            source_ids = ", ".join(sources_by_intent.get(intent["id"], []))
            worker = intent["worker"] or "unclaimed"
            lines.append(f"- {intent['id']} from {source_ids} ({worker}): {intent['description']}")
    else:
        lines.append("- None.")
    lines.append("")

    lines.extend(["## Hints"])
    if hints:
        for hint in hints:
            target = f" target={hint['target_type']}:{hint['target_id']}" if hint["target_type"] and hint["target_id"] else ""
            pinned = " pinned" if hint["pinned"] else ""
            lines.append(
                f"- {hint['id']} [{hint['hint_type']}/{hint['priority']}{pinned}{target}] by {hint['creator']}: {hint['content']}"
            )
    else:
        lines.append("- None.")
    lines.append("")

    lines.extend(["## Timeline", ""])
    lines.append(_export_timeline(conn, project_id).rstrip())
    lines.append("")

    return "\n".join(lines)


@router.get("/projects/{project_id}/export")
def export_project(project_id: str, format: str = "yaml"):
    if format not in ("yaml", "timeline", "report"):
        raise HTTPException(400, "Supported formats: yaml, timeline, report")

    with get_conn() as conn:
        if format == "timeline":
            text = _export_timeline(conn, project_id)
        elif format == "report":
            text = _export_report(conn, project_id)
        else:
            text = _export_yaml(conn, project_id)

        return Response(content=text, media_type="text/plain")
