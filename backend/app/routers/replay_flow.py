"""页面样本回放与模板切换的富语义接口。

通用 /api/{module}/{id}/actions 仍保留；这里提供更适合断线重试、幂等和回执查询的路径。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Header, Request

from app.services.replay_flow import replay_flow

router = APIRouter(prefix="/api", tags=["页面回放流转"])


async def _body(request: Request) -> dict[str, Any]:
    try:
        data = await request.json()
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


@router.get("/replay-ledger")
def replay_ledger() -> dict[str, Any]:
    return replay_flow.ledger()


@router.get("/replay-queue")
def replay_queue(site: str | None = None) -> dict[str, Any]:
    return replay_flow.queue(site)


@router.get("/page_parser/replay-queue")
def parser_replay_queue(site: str | None = None) -> dict[str, Any]:
    return replay_flow.queue(site)


@router.get("/template-switches")
def list_template_switches() -> dict[str, Any]:
    return replay_flow.list_switches()


@router.post("/template-switches/{switch_id}/confirm")
async def confirm_switch(switch_id: str, request: Request) -> dict[str, Any]:
    await _body(request)
    return replay_flow.confirm_switch(switch_id)


@router.post("/page_parser/switch")
async def switch_template_collection(
    request: Request,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> dict[str, Any]:
    payload = await _body(request)
    return replay_flow.switch_template(payload.get("new_template_id") or payload.get("template_id"),
                                       payload, header_idempotency_key=idempotency_key)


@router.post("/page_parser/{template_id}/switch")
async def switch_template(
    template_id: int,
    request: Request,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> dict[str, Any]:
    payload = await _body(request)
    return replay_flow.switch_template(template_id, payload, header_idempotency_key=idempotency_key)


@router.post("/page_parser/{template_id}/claim-next")
@router.post("/page_parser/{template_id}/claim")
async def claim_next(template_id: int, request: Request) -> dict[str, Any]:
    payload = await _body(request)
    return replay_flow.claim_next(template_id, payload)


@router.get("/sample_store/replays/{replay_id}")
@router.get("/sample_store/replay/{replay_id}")
def get_sample_replay_alias(replay_id: str) -> dict[str, Any]:
    return replay_flow.get_replay(replay_id)


@router.post("/sample_store/replays/{replay_id}/continue")
@router.post("/sample_store/replay/{replay_id}/continue")
async def continue_sample_replay_alias(
    replay_id: str,
    request: Request,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> dict[str, Any]:
    payload = await _body(request)
    return replay_flow.continue_replay(replay_id, payload, header_idempotency_key=idempotency_key)


@router.post("/sample_store/replays/{replay_id}/retry")
@router.post("/sample_store/replay/{replay_id}/retry")
async def retry_sample_replay_alias(
    replay_id: str,
    request: Request,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> dict[str, Any]:
    payload = await _body(request)
    return replay_flow.retry_replay(replay_id, payload, header_idempotency_key=idempotency_key)


@router.post("/sample_store/replays/{replay_id}/confirm")
@router.post("/sample_store/replay/{replay_id}/confirm")
async def confirm_sample_replay_alias(replay_id: str, request: Request) -> dict[str, Any]:
    payload = await _body(request)
    return replay_flow.confirm_replay(replay_id, payload)


@router.get("/sample_store/{sample_id}/replay-state")
def sample_replay_state(sample_id: int) -> dict[str, Any]:
    return replay_flow.sample_state(sample_id)


@router.get("/sample_store/{sample_id}/receipts")
def sample_receipts(sample_id: int) -> dict[str, Any]:
    return replay_flow.sample_state(sample_id)


@router.post("/sample_store/{sample_id}/replay")
@router.post("/sample_store/{sample_id}/replays")
async def start_sample_replay(
    sample_id: int,
    request: Request,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> dict[str, Any]:
    payload = await _body(request)
    return replay_flow.start_replay(sample_id, payload, header_idempotency_key=idempotency_key)


@router.post("/sample_store/{sample_id}/continue")
async def continue_sample_replay(
    sample_id: int,
    request: Request,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> dict[str, Any]:
    payload = await _body(request)
    replay_id = payload.get("replay_id") or payload.get("replayId")
    if not replay_id:
        state = replay_flow.sample_state(sample_id)
        replay_id = (state.get("active_replay") or {}).get("id") if state.get("ok") else None
    if not replay_id:
        started = replay_flow.start_replay(sample_id, payload, header_idempotency_key=idempotency_key)
        replay_id = (started.get("replay") or {}).get("id")
        if not replay_id:
            return started
    return replay_flow.continue_replay(str(replay_id), payload, header_idempotency_key=idempotency_key)


@router.get("/replays/{replay_id}")
def get_replay(replay_id: str) -> dict[str, Any]:
    return replay_flow.get_replay(replay_id)


@router.post("/replays/{replay_id}/continue")
async def continue_replay(
    replay_id: str,
    request: Request,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> dict[str, Any]:
    payload = await _body(request)
    return replay_flow.continue_replay(replay_id, payload, header_idempotency_key=idempotency_key)


@router.post("/replays/{replay_id}/retry")
async def retry_replay(
    replay_id: str,
    request: Request,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> dict[str, Any]:
    payload = await _body(request)
    return replay_flow.retry_replay(replay_id, payload, header_idempotency_key=idempotency_key)


@router.post("/replays/{replay_id}/confirm")
async def confirm_replay(replay_id: str, request: Request) -> dict[str, Any]:
    payload = await _body(request)
    return replay_flow.confirm_replay(replay_id, payload)
