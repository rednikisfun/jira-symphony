"""FastAPI web dashboard for the orchestrator."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse

if TYPE_CHECKING:
    from .orchestrator import Orchestrator

log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"

app = FastAPI(title="Jira Symphony Dashboard")

# Will be set by cli.py before startup
_orchestrator: Orchestrator | None = None


def set_orchestrator(orch: Orchestrator) -> None:
    global _orchestrator
    _orchestrator = orch


def _orch() -> Orchestrator:
    assert _orchestrator is not None, "Orchestrator not initialized"
    return _orchestrator


# ── Pages ─────────────────────────────────────────────────


@app.get("/", response_class=HTMLResponse)
async def index():
    from .config import config_exists
    if not config_exists():
        return RedirectResponse("/setup")
    return (STATIC_DIR / "index.html").read_text()


@app.get("/setup", response_class=HTMLResponse)
async def setup_page():
    return (STATIC_DIR / "setup.html").read_text()


# ── REST API ──────────────────────────────────────────────


@app.get("/api/status")
async def api_status():
    return _orch().get_status()


@app.get("/api/history")
async def api_history():
    return await _orch().get_history()


@app.get("/api/workers/{issue_key}/log")
async def api_worker_log(issue_key: str):
    orch = _orch()
    cw = orch._workers.get(issue_key)
    if not cw:
        return {"log": [], "error": "Worker not found or not active"}
    return {"log": list(cw.progress.log_lines)}


@app.post("/api/orchestrator/pause")
async def api_pause():
    _orch().pause()
    return {"ok": True, "paused": True}


@app.post("/api/orchestrator/resume")
async def api_resume():
    _orch().resume()
    return {"ok": True, "paused": False}


@app.post("/api/workers/{issue_key}/kill")
async def api_kill_worker(issue_key: str):
    ok = await _orch().kill_worker(issue_key)
    return {"ok": ok}


@app.post("/api/workers/{issue_key}/retry")
async def api_retry_worker(issue_key: str):
    ok = await _orch().retry_worker(issue_key)
    return {"ok": ok}


@app.post("/api/dispatch")
async def api_dispatch(request: Request):
    """Manually dispatch a Jira issue."""
    body = await request.json()
    issue_key = body.get("issue_key", "").strip()
    project = body.get("project", "").strip() or None

    if not issue_key:
        return JSONResponse(
            {"ok": False, "message": "issue_key is required"}, status_code=400
        )

    msg = await _orch().manual_dispatch(issue_key, project)
    return {"ok": True, "message": msg}


# ── Setup API ─────────────────────────────────────────────


@app.post("/api/setup/validate-jira")
async def api_validate_jira(request: Request):
    """Test Jira credentials."""
    import httpx

    body = await request.json()
    cloud_id = body.get("cloud_id", "")
    email = body.get("email", "")
    api_token = body.get("api_token", "")

    if not all([cloud_id, email, api_token]):
        return JSONResponse(
            {"ok": False, "error": "All fields required"}, status_code=400
        )

    base_url = f"https://api.atlassian.com/ex/jira/{cloud_id}/rest/api/3"
    creds = base64.b64encode(f"{email}:{api_token}".encode()).decode()
    headers = {"Authorization": f"Basic {creds}", "Accept": "application/json"}

    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(f"{base_url}/myself", headers=headers, timeout=10)
            resp.raise_for_status()
            user = resp.json()
            return {"ok": True, "user": user.get("displayName", "")}
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)


@app.post("/api/setup/save")
async def api_setup_save(request: Request):
    """Save configuration from web wizard."""
    from .config import SymphonyConfig, save_config

    body = await request.json()
    try:
        config = SymphonyConfig.model_validate(body)
        path = save_config(config)
        return {"ok": True, "path": str(path)}
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)


# ── SSE (Server-Sent Events) ─────────────────────────────


@app.get("/api/events")
async def api_events():
    """Stream orchestrator status as SSE."""
    async def event_stream():
        while True:
            data = json.dumps(_orch().get_status())
            yield f"data: {data}\n\n"
            await asyncio.sleep(2)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
    )
