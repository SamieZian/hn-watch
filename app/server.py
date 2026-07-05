"""FastAPI app: REST + SSE + static frontend."""
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import config, db, swarm
from .events import bus
from .monitors import scheduler

log = logging.getLogger("hnwatch.server")

STATIC_DIR = config.PROJECT_ROOT / "app" / "static"


class MonitorIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    prompt: str = Field(min_length=1, max_length=2000)
    interval_minutes: int = Field(default=config.DEFAULT_INTERVAL_MINUTES, ge=1, le=24 * 60)


class MonitorPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=80)
    prompt: str | None = Field(default=None, min_length=1, max_length=2000)
    interval_minutes: int | None = Field(default=None, ge=1, le=24 * 60)
    enabled: bool | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.connect()
    scheduler.start_all()
    log.info("hn-watch up on http://%s:%s", config.HOST, config.PORT)
    yield
    scheduler.stop_all()
    db.close()


def create_app() -> FastAPI:
    app = FastAPI(title="HN Watch", lifespan=lifespan)

    @app.get("/api/monitors")
    async def monitors_list():
        return db.list_monitors()

    @app.post("/api/monitors", status_code=201)
    async def monitors_create(body: MonitorIn):
        m = db.create_monitor(body.name, body.prompt, body.interval_minutes)
        scheduler.start(m["id"])  # loop jitters, then runs the first tick
        return m

    @app.patch("/api/monitors/{monitor_id}")
    async def monitors_patch(monitor_id: int, body: MonitorPatch):
        if db.get_monitor(monitor_id) is None:
            raise HTTPException(404)
        fields = {k: v for k, v in body.model_dump().items() if v is not None}
        m = db.update_monitor(monitor_id, **fields)
        if "enabled" in fields or "interval_minutes" in fields:
            if m["enabled"]:
                scheduler.start(monitor_id)
            else:
                scheduler.stop(monitor_id)
        return m

    @app.delete("/api/monitors/{monitor_id}", status_code=204)
    async def monitors_delete(monitor_id: int):
        scheduler.stop(monitor_id)
        db.delete_monitor(monitor_id)

    @app.post("/api/monitors/{monitor_id}/run")
    async def monitors_run_now(monitor_id: int):
        if db.get_monitor(monitor_id) is None:
            raise HTTPException(404)
        await scheduler.run_now(monitor_id)
        return db.get_monitor(monitor_id)

    @app.get("/api/feed")
    async def feed(limit: int = 50, before_id: int | None = None):
        return db.list_feed(limit=min(limit, 200), before_id=before_id)

    @app.post("/api/feed/{item_id}/dig", status_code=202)
    async def dig(item_id: int):
        run_id = swarm.start_run(item_id)
        if run_id is None:
            raise HTTPException(404)
        return {"run_id": run_id}

    @app.get("/api/runs/{run_id}")
    async def get_run(run_id: int):
        run = db.get_swarm_run(run_id)
        if run is None:
            raise HTTPException(404)
        return run

    @app.get("/api/events")
    async def events():
        return StreamingResponse(
            bus.sse_stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.get("/")
    async def index():
        return FileResponse(STATIC_DIR / "index.html")

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    return app
