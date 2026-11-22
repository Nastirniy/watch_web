"""A small app for tests/test_event_drain.py (and a live nginx check of the events location): the real hub, the real response class and the v1 renderer behind
uvicorn, over a SQLite file (WW_DRAIN_DB). Every stream belongs to user 1; nothing else of the API is here."""
import asyncio
import os
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI
from sqlalchemy.ext.asyncio import create_async_engine
from sqlmodel import SQLModel

import app.backend.models  # noqa: F401  (every table, for create_all)
from app.backend.api.shared.event_stream import EventStreamResponse
from app.backend.api.v1 import V1
from app.backend.api.v1.models.response.events import render_v1
from app.backend.auth.context import AuthContext
from app.backend.config import EventsConfig
from app.backend.enums.routes import EventRoutes
from app.backend.models.user import User
from app.backend.services.events.hub import EventHub


@asynccontextmanager
async def lifespan(app: FastAPI):
    engine = create_async_engine(f"sqlite+aiosqlite:///{os.environ['WW_DRAIN_DB']}.{os.getpid()}")   # one file per worker
    async with engine.begin() as connection:
        await connection.run_sync(SQLModel.metadata.create_all)
    hub = EventHub(engine, render_v1, EventsConfig(enabled=True, drain_retry_max_ms=2000), workers=2)
    await hub.start()
    hub.install_drain(asyncio.get_running_loop())
    app.state.event_hub = hub
    yield
    await hub.stop()


app = FastAPI(lifespan=lifespan)


@app.get(V1.path(EventRoutes.STREAM))     # the published path: nginx passes it on unchanged
async def stream():
    hub = app.state.event_hub
    connection = hub.attach(AuthContext(user=User(id=1, username="u", username_alias="U"), session_id="s"),
                            time.time() + 3600, None)
    return EventStreamResponse(connection.frames())
