"""External engine routes: register, probe, list and remove.

These are configuration routes on the launcher app (session token required). They never
start, stop or install anything: an external engine is a server the user already runs,
and the launcher's only claim about it is "I can reach this address".

The probe is the one operation that opens a connection, and its result is written back
to the registry so a failed engine stays visibly failed after a restart.
"""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, HTTPException

router = APIRouter()

# Loopback is the default and the product's premise. Set this to allow a LAN address,
# and say so in the response so the UI can show a warning next to the engine.
ALLOW_REMOTE_ENV = "LUNARSHROUD_ALLOW_REMOTE_ENGINES"


def _allow_remote() -> bool:
    import os

    return os.environ.get(ALLOW_REMOTE_ENV, "").strip() in ("1", "true", "yes")


@router.get("/api/engines")
async def api_list_engines():
    """Registered external engines, with their last probe outcome."""
    from core import engine_registry

    engines = await asyncio.to_thread(engine_registry.list_engines)
    return {
        "engines": [engine.to_dict() for engine in engines],
        "allow_remote": _allow_remote(),
    }


@router.post("/api/engines")
async def api_upsert_engine(body: dict):
    """Create or update one engine record.

    Validation refuses a remote host unless `LUNARSHROUD_ALLOW_REMOTE_ENGINES` is set,
    and refuses an ``api_key_env`` that looks like a key instead of a variable name —
    both mistakes end with a secret or a LAN address in a backed-up JSON file.
    """
    from core import engine_registry

    try:
        engine = await asyncio.to_thread(
            engine_registry.upsert_engine, body, allow_remote=_allow_remote()
        )
    except engine_registry.EngineError as error:
        raise HTTPException(400, str(error)) from error
    return engine.to_dict()


@router.delete("/api/engines/{engine_id}")
async def api_remove_engine(engine_id: str):
    from core import engine_registry

    removed = await asyncio.to_thread(engine_registry.remove_engine, engine_id)
    if not removed:
        raise HTTPException(404, "engine not found")
    return {"deleted": True}


@router.post("/api/engines/{engine_id}/probe")
async def api_probe_engine(engine_id: str):
    """Check reachability and record the outcome.

    Returns a `kind`-specific capability list so the UI can disable controls with a
    stated reason instead of offering something the engine cannot do.
    """
    from core import engine_client, engine_registry

    engine = await asyncio.to_thread(engine_registry.get_engine, engine_id)
    if engine is None:
        raise HTTPException(404, "engine not found")

    result = await engine_client.probe(engine)
    await asyncio.to_thread(
        engine_registry.record_probe,
        engine_id,
        ok=bool(result["ok"]),
        models=result.get("models") or [],
        error=result.get("error"),
    )
    refreshed = await asyncio.to_thread(engine_registry.get_engine, engine_id)
    return {
        "ok": result["ok"],
        "models": result.get("models") or [],
        "error": result.get("error"),
        "engine": refreshed.to_dict() if refreshed else None,
    }


@router.get("/api/engines/{engine_id}/capabilities")
async def api_engine_capabilities(engine_id: str):
    """What this engine can do, expressed as capability names.

    A generic OpenAI server has no ``/tokenize`` and no ``/health``; the UI uses this to
    grey out the token counter and to label it as an estimate, rather than rendering a
    precise-looking number it cannot support.
    """
    from core import engine_registry

    engine = await asyncio.to_thread(engine_registry.get_engine, engine_id)
    if engine is None:
        raise HTTPException(404, "engine not found")
    return {
        "id": engine.id,
        "kind": engine.kind,
        "launch": engine.launch,
        "capabilities": sorted(engine.capabilities),
        "exact_token_count": engine.supports("tokenize"),
    }
