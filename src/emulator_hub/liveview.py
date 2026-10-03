"""Live view: one WebSocket per viewer at /api/leases/{id}/live on the UI port.

  server -> browser: binary JPEG frames
  browser -> server: JSON text
      {"t": "touch", "x": 0..1, "y": 0..1, "down": true|false}
      {"t": "key",   "key": "GoHome" | "GoBack" | "AppSwitch" | ...}   (see KEYS)
      {"t": "text",  "text": "hello"}
Coordinates are normalised, so the browser never needs the device resolution.
"""

import asyncio
import contextlib
import json
import logging
from collections.abc import Callable

from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import Response

from emulator_hub.auth import ui_user
from emulator_hub.emulator_grpc import KEYS, GrpcScreen
from emulator_hub.leases import LeaseEngine

log = logging.getLogger(__name__)
# How long a closed viewer's queued input (a long paste) may keep typing.
INPUT_DRAIN_S = 60


async def handle_input(screen, raw: str) -> None:
    msg = json.loads(raw)
    kind = msg.get("t")
    if kind == "touch":
        x, y = float(msg["x"]), float(msg["y"])
        if 0 <= x <= 1 and 0 <= y <= 1:
            await screen.touch(x, y, bool(msg["down"]))
    elif kind == "key" and msg.get("key") in KEYS:
        await screen.key(msg["key"])
    elif kind == "text" and isinstance(msg.get("text"), str):
        await screen.text(msg["text"][:500])


def build_liveview(engine: LeaseEngine, screen_factory: Callable = GrpcScreen) -> APIRouter:
    r = APIRouter()

    @r.get("/api/leases/{lease_id}/snapshot")
    async def snapshot(lease_id: str):
        pod_ip = engine.devices.get(lease_id)
        if pod_ip is None:
            raise HTTPException(404, "that lease has no running emulator")
        screen = screen_factory(pod_ip)
        try:
            jpeg = await screen.snapshot()
        except Exception as exc:
            raise HTTPException(502, f"emulator did not return a screenshot: {exc}") from exc
        finally:
            await screen.close()
        return Response(jpeg, media_type="image/jpeg", headers={"Cache-Control": "no-store"})

    @r.websocket("/api/leases/{lease_id}/live")
    async def live(websocket: WebSocket, lease_id: str):
        if ui_user(websocket.headers) is None:
            await websocket.close(code=4401)
            return
        pod_ip = engine.devices.get(lease_id)
        if pod_ip is None:
            await websocket.close(code=4404)
            return
        await websocket.accept()
        screen = screen_factory(pod_ip)

        async def pump_frames():
            async for jpeg in screen.frames():
                await websocket.send_bytes(jpeg)

        # Input is applied in order by one worker, so typing (paced, seconds
        # for a paste) never blocks frames or later messages, and what a viewer
        # sent is still delivered if it disconnects right after.
        queue: asyncio.Queue[str | None] = asyncio.Queue()

        async def apply_input():
            while (raw := await queue.get()) is not None:
                await handle_input(screen, raw)

        async def pump_input():
            while True:
                queue.put_nowait(await websocket.receive_text())

        worker = asyncio.create_task(apply_input())
        tasks = [asyncio.create_task(pump_frames()), asyncio.create_task(pump_input()), worker]
        try:
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for t in done:
                exc = t.exception()
                if exc and not isinstance(exc, WebSocketDisconnect):
                    log.warning("live view for lease %s ended: %r", lease_id, exc)
        finally:
            tasks[0].cancel()
            tasks[1].cancel()
            if not worker.done():
                # Finish what the viewer already sent (bounded), then stop.
                queue.put_nowait(None)
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(worker, INPUT_DRAIN_S)
            await screen.close()
            with contextlib.suppress(Exception):
                await websocket.close()

    return r
