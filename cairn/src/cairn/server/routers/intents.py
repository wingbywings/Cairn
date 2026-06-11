import json

from fastapi import APIRouter, HTTPException

from cairn.server.db import get_conn
from cairn.server.models import (
    ConcludeRequest,
    ConcludeResponse,
    CreateIntentRequest,
    Fact,
    HeartbeatRequest,
    Intent,
    UndoConcludeRequest,
    UndoConcludeResponse,
)
from cairn.server.services import (
    check_project_active,
    expire_reason_leases,
    get_claimable_open_intent_or_404,
    get_intent_or_404,
    get_project_or_404,
    get_releasable_open_intent_or_404,
    intent_to_model,
    next_fact_id,
    next_intent_id,
    utcnow,
    validate_facts_exist,
    validate_intent_creator_worker,
    validate_goal_not_in_sources,
)

router = APIRouter(tags=["intents"])


@router.post(
    "/projects/{project_id}/intents",
    response_model=Intent,
    status_code=201,
)
def create_intent(project_id: str, body: CreateIntentRequest):
    with get_conn() as conn:
        check_project_active(conn, project_id)
        validate_facts_exist(conn, project_id, body.from_)
        validate_goal_not_in_sources(body.from_)
        validate_intent_creator_worker(body.creator, body.worker)

        now = utcnow()
        iid = next_intent_id(conn, project_id)
        claimed = body.worker is not None
        conn.execute(
            "INSERT INTO intents (id, project_id, to_fact_id, description, creator, worker, last_heartbeat_at, created_at, concluded_at) VALUES (?, ?, NULL, ?, ?, ?, ?, ?, NULL)",
            (
                iid,
                project_id,
                body.description,
                body.creator,
                body.worker,
                now if claimed else None,
                now,
            ),
        )
        for fid in body.from_:
            conn.execute(
                "INSERT INTO intent_sources (intent_id, project_id, fact_id) VALUES (?, ?, ?)",
                (iid, project_id, fid),
            )

        return Intent(
            id=iid,
            **{"from": body.from_},
            to=None,
            description=body.description,
            creator=body.creator,
            worker=body.worker,
            last_heartbeat_at=now if claimed else None,
            created_at=now,
            concluded_at=None,
        )


@router.post(
    "/projects/{project_id}/intents/{intent_id}/heartbeat",
    response_model=Intent,
)
def heartbeat(project_id: str, intent_id: str, body: HeartbeatRequest):
    with get_conn() as conn:
        check_project_active(conn, project_id)
        get_claimable_open_intent_or_404(conn, project_id, intent_id, body.worker)

        now = utcnow()
        conn.execute(
            "UPDATE intents SET worker = ?, last_heartbeat_at = ? WHERE id = ? AND project_id = ?",
            (body.worker, now, intent_id, project_id),
        )

        updated = conn.execute(
            "SELECT * FROM intents WHERE id = ? AND project_id = ?",
            (intent_id, project_id),
        ).fetchone()
        return intent_to_model(conn, updated, project_id)


@router.post(
    "/projects/{project_id}/intents/{intent_id}/release",
    response_model=Intent,
)
def release(project_id: str, intent_id: str, body: HeartbeatRequest):
    with get_conn() as conn:
        check_project_active(conn, project_id)
        row = get_releasable_open_intent_or_404(conn, project_id, intent_id, body.worker)

        if row["worker"] == body.worker:
            conn.execute(
                "UPDATE intents SET worker = NULL WHERE id = ? AND project_id = ?",
                (intent_id, project_id),
            )
            row = conn.execute(
                "SELECT * FROM intents WHERE id = ? AND project_id = ?",
                (intent_id, project_id),
            ).fetchone()

        return intent_to_model(conn, row, project_id)


@router.post(
    "/projects/{project_id}/intents/{intent_id}/conclude",
    response_model=ConcludeResponse,
)
def conclude(project_id: str, intent_id: str, body: ConcludeRequest):
    with get_conn() as conn:
        check_project_active(conn, project_id)
        get_claimable_open_intent_or_404(conn, project_id, intent_id, body.worker)

        now = utcnow()
        fid = next_fact_id(conn, project_id)

        conn.execute(
            "INSERT INTO facts (id, project_id, description) VALUES (?, ?, ?)",
            (fid, project_id, body.description),
        )
        conn.execute(
            "UPDATE intents SET to_fact_id = ?, worker = ?, last_heartbeat_at = ?, concluded_at = ? WHERE id = ? AND project_id = ?",
            (fid, body.worker, now, now, intent_id, project_id),
        )

        updated = conn.execute(
            "SELECT * FROM intents WHERE id = ? AND project_id = ?",
            (intent_id, project_id),
        ).fetchone()

        return ConcludeResponse(
            fact=Fact(id=fid, description=body.description),
            intent=intent_to_model(conn, updated, project_id),
        )


@router.post(
    "/projects/{project_id}/intents/{intent_id}/undo-conclude",
    response_model=UndoConcludeResponse,
)
def undo_conclude(project_id: str, intent_id: str, body: UndoConcludeRequest):
    with get_conn() as conn:
        expire_reason_leases(conn, project_id)
        project = get_project_or_404(conn, project_id)
        if project["status"] == "completed":
            raise HTTPException(409, "Completed projects must be reopened instead of undoing conclude")
        if project["status"] not in ("active", "stopped"):
            raise HTTPException(403, f"Project is {project['status']}")
        if project["reason_worker"] is not None:
            raise HTTPException(409, f"Reason is currently running by {project['reason_worker']}")

        intent = get_intent_or_404(conn, project_id, intent_id)
        produced_fact_id = intent["to_fact_id"]
        if produced_fact_id is None or intent["concluded_at"] is None:
            raise HTTPException(409, "Intent is not concluded")
        if produced_fact_id == "goal":
            raise HTTPException(409, "Completion intent must be reopened instead of undoing conclude")

        produced_fact = conn.execute(
            "SELECT * FROM facts WHERE id = ? AND project_id = ?",
            (produced_fact_id, project_id),
        ).fetchone()
        if produced_fact is None:
            raise HTTPException(409, f"Produced fact {produced_fact_id} is missing")

        dependent_intent = conn.execute(
            """
            SELECT intent_id FROM intent_sources
            WHERE project_id = ? AND fact_id = ?
            LIMIT 1
            """,
            (project_id, produced_fact_id),
        ).fetchone()
        if dependent_intent is not None:
            raise HTTPException(
                409,
                f"Produced fact {produced_fact_id} is used by downstream intent {dependent_intent['intent_id']}",
            )

        dependent_hint = conn.execute(
            """
            SELECT id FROM hints
            WHERE project_id = ? AND target_type = 'fact' AND target_id = ?
            LIMIT 1
            """,
            (project_id, produced_fact_id),
        ).fetchone()
        if dependent_hint is not None:
            raise HTTPException(
                409,
                f"Produced fact {produced_fact_id} is targeted by hint {dependent_hint['id']}",
            )

        removed_fact = Fact(id=produced_fact["id"], description=produced_fact["description"])
        conn.execute(
            """
            UPDATE intents
            SET to_fact_id = NULL,
                worker = NULL,
                last_heartbeat_at = NULL,
                concluded_at = NULL
            WHERE id = ? AND project_id = ?
            """,
            (intent_id, project_id),
        )
        conn.execute(
            "DELETE FROM facts WHERE id = ? AND project_id = ?",
            (produced_fact_id, project_id),
        )

        now = utcnow()
        details = {
            "actor": body.actor,
            "intent_id": intent_id,
            "removed_fact_id": produced_fact_id,
            "reason": body.reason,
        }
        conn.execute(
            """
            INSERT INTO ops_events
                (project_id, event_type, task_type, worker, intent_id, severity, message, details_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                project_id,
                "conclude_undone",
                "manual",
                body.actor,
                intent_id,
                "warning",
                f"Conclude undone for {intent_id}; removed {produced_fact_id}",
                json.dumps(details, ensure_ascii=False, sort_keys=True),
                now,
            ),
        )

        updated = conn.execute(
            "SELECT * FROM intents WHERE id = ? AND project_id = ?",
            (intent_id, project_id),
        ).fetchone()
        assert updated is not None
        return UndoConcludeResponse(
            removed_fact=removed_fact,
            intent=intent_to_model(conn, updated, project_id),
        )
