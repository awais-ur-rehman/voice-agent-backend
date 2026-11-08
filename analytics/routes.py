"""Staff analytics allowlist; operator metrics live on a separate private router."""

from datetime import date
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request, Response
from pydantic import ValidationError
from sqlalchemy.orm import Session

from analytics import queries, views
from analytics.aggregation import configure
from analytics.contracts import Outcome, QualityFlag, SettingsUpdate
from analytics.transcripts import (
    delete_transcripts,
    read_transcript_page,
    settings_row,
    update_settings,
)
from scheduler.db import Principal, manager, principal, session
from scheduler.errors import AppError

router = APIRouter(prefix="/dashboard", tags=["Analytics"])
Limit = Annotated[int, Query(ge=1, le=100)]
Cursor = Annotated[str | None, Query(max_length=512)]
Ordinal = Annotated[int, Query(ge=0)]


def filters(
    start: date,
    end: date,
    channel: views.ChannelFilter = "all",
    agent_id: UUID | None = None,
    timezone: Annotated[str | None, Query(max_length=100)] = None,
) -> views.Filters:
    try:
        return views.Filters(
            start=start, end=end, channel=channel, agent_id=agent_id, timezone=timezone
        )
    except ValidationError:
        raise AppError("analytics_date_range", 422) from None


def snapshot(request: Request, user: Principal = Depends(principal)):
    # principal() already read the users table in its own authentication transaction.
    # A fresh transaction establishes isolation BEFORE any projection data query.
    with request.app.state.session_factory() as db, db.begin():
        configure(db, user.tenant_id, snapshot=True)
        yield db


def validated(
    request: Request, f: views.Filters = Depends(filters), db: Session = Depends(snapshot)
):
    queries.validate_scope(db, f, request.app.state.clock())
    return f


def call_filters(
    f: views.Filters = Depends(validated),
    outcome: Outcome | None = None,
    review_flag: QualityFlag | None = None,
) -> views.CallFilters:
    return views.CallFilters(**f.model_dump(), outcome=outcome, review_flag=review_flag)


@router.get("/analytics/overview", response_model=views.Overview)
def overview(
    request: Request, f: views.Filters = Depends(validated), db: Session = Depends(snapshot)
):
    return queries.overview(db, f, request.app.state.clock())


@router.get("/analytics/quality", response_model=views.Quality)
def quality(
    request: Request, f: views.Filters = Depends(validated), db: Session = Depends(snapshot)
):
    return queries.quality(db, f, request.app.state.clock())


@router.get("/analytics/latency", response_model=views.Latency)
def latency(
    request: Request, f: views.Filters = Depends(validated), db: Session = Depends(snapshot)
):
    return queries.latency(db, f, request.app.state.clock())


@router.get("/analytics/intents", response_model=views.Intents)
def intents(
    request: Request, f: views.Filters = Depends(validated), db: Session = Depends(snapshot)
):
    return queries.intents(db, f, request.app.state.clock())


@router.get("/analytics/knowledge-gaps", response_model=views.KnowledgeGaps)
def knowledge_gaps(
    request: Request,
    f: views.Filters = Depends(validated),
    db: Session = Depends(snapshot),
    limit: Limit = 100,
    after: Cursor = None,
):
    return queries.knowledge_gaps(db, f, request.app.state.clock(), limit, after)


@router.get("/analytics/alerts", response_model=views.AlertPage)
def alerts(
    request: Request,
    f: views.Filters = Depends(validated),
    db: Session = Depends(snapshot),
    limit: Limit = 100,
    after: Cursor = None,
):
    return queries.alerts(db, f, request.app.state.clock(), limit, after)


@router.get("/calls", response_model=views.CallPage)
def calls(
    request: Request,
    f: views.CallFilters = Depends(call_filters),
    db: Session = Depends(snapshot),
    limit: Limit = 100,
    after: Cursor = None,
):
    return queries.calls(db, f, request.app.state.clock(), limit, after)


@router.get("/calls/{session_id}", response_model=views.CallDetail)
def detail(
    session_id: UUID,
    request: Request,
    db: Session = Depends(snapshot),
    limit: Limit = 100,
    after: Ordinal = 0,
    measurement_after: Cursor = None,
):
    return queries.detail(
        db, session_id, request.app.state.clock(), limit, after, measurement_after
    )


@router.get("/calls/{session_id}/transcript", response_model=views.TranscriptPage)
def transcript(
    session_id: UUID,
    request: Request,
    response: Response,
    user: Principal = Depends(manager),
    limit: Limit = 100,
    after: Ordinal = 0,
):
    response.headers["Cache-Control"] = "no-store"
    with request.app.state.session_factory() as db, db.begin():
        configure(db, user.tenant_id, snapshot=True)
        return read_transcript_page(
            db, user, session_id, request.app.state.clock(), limit=limit, after=after
        )


@router.delete("/calls/{session_id}/transcript", status_code=204)
def erase(
    session_id: UUID,
    request: Request,
    user: Principal = Depends(manager),
    db: Session = Depends(session),
):
    configure(db)
    delete_transcripts(db, user, session_id, request.app.state.clock())
    return Response(status_code=204, headers={"Cache-Control": "no-store"})


@router.get("/analytics/settings", response_model=views.SettingsView)
def settings(user: Principal = Depends(manager), db: Session = Depends(session)):
    configure(db)
    return settings_row(db, user.tenant_id)


@router.put("/analytics/settings", response_model=views.SettingsView)
def settings_put(
    body: SettingsUpdate,
    request: Request,
    user: Principal = Depends(manager),
    db: Session = Depends(session),
):
    configure(db)
    return update_settings(db, user, body, request.app.state.clock())
