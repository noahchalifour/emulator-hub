import json

import pytest
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from emulator_hub.app import create_ui_app
from tests.fakes import FakeScreen


@pytest.fixture
def screens():
    return []


@pytest.fixture
def app(engine, screens):
    def factory(pod_ip):
        s = FakeScreen(pod_ip)
        screens.append(s)
        return s

    return create_ui_app(engine, screen_factory=factory)


async def test_live_streams_frames_and_forwards_input(engine, app, screens):
    grant = await engine.acquire("phone", "a", 30, 1)
    with (
        TestClient(app) as client,
        client.websocket_connect(f"/api/leases/{grant.lease.id}/live", headers={"X-authentik-username": "noah"}) as ws,
    ):
        assert [ws.receive_bytes() for _ in range(3)] == [b"jpeg-0", b"jpeg-1", b"jpeg-2"]
        ws.send_text(json.dumps({"t": "touch", "x": 0.5, "y": 0.25, "down": True}))
        ws.send_text(json.dumps({"t": "key", "key": "GoHome"}))
        ws.send_text(json.dumps({"t": "key", "key": "rm -rf"}))  # not in KEYS: dropped
        ws.send_text(json.dumps({"t": "touch", "x": 7, "y": 0.1, "down": True}))  # out of range: dropped
        ws.send_text(json.dumps({"t": "text", "text": "hello"}))
        ws.close()
    assert screens[0].pod_ip == "10.233.0.9"
    assert screens[0].inputs == [("touch", 0.5, 0.25, True), ("key", "GoHome"), ("text", "hello")]
    assert screens[0].closed


async def test_live_refuses_unknown_lease_and_missing_auth(engine, app):
    with TestClient(app) as client:
        with (
            pytest.raises(WebSocketDisconnect) as err,
            client.websocket_connect("/api/leases/nope/live", headers={"X-authentik-username": "noah"}) as ws,
        ):
            ws.receive_bytes()
        assert err.value.code == 4404


async def test_live_requires_auth(engine, app):
    grant = await engine.acquire("phone", "a", 30, 1)
    with TestClient(app) as client:
        with (
            pytest.raises(WebSocketDisconnect) as err,
            client.websocket_connect(f"/api/leases/{grant.lease.id}/live") as ws,
        ):
            ws.receive_bytes()
        assert err.value.code == 4401


async def test_snapshot_is_a_jpeg_for_a_live_lease(engine, app):
    grant = await engine.acquire("phone", "a", 30, 1)
    with TestClient(app) as client:
        ok = client.get(f"/api/leases/{grant.lease.id}/snapshot", headers={"X-authentik-username": "noah"})
        assert ok.status_code == 200 and ok.headers["content-type"] == "image/jpeg" and ok.content == b"thumb"
        assert client.get("/api/leases/nope/snapshot", headers={"X-authentik-username": "noah"}).status_code == 404


async def test_input_sent_before_disconnect_is_still_delivered(engine):
    """Input is applied in order and nothing a viewer sent is lost when it
    closes right after. (The cancellation race this guards against only shows
    against a real emulator; tests/e2e/test_liveview.py covers that.)"""
    import asyncio
    import time

    class SlowScreen(FakeScreen):
        async def text(self, text):
            await asyncio.sleep(0.5)  # paced typing: longer than the close takes
            self.inputs.append(("text", text))

    screens = []

    def factory(pod_ip):
        s = SlowScreen(pod_ip)
        screens.append(s)
        return s

    app = create_ui_app(engine, screen_factory=factory)
    grant = await engine.acquire("phone", "a", 30, 1)
    with (
        TestClient(app) as client,
        client.websocket_connect(f"/api/leases/{grant.lease.id}/live", headers={"X-authentik-username": "noah"}) as ws,
    ):
        ws.receive_bytes()
        for word in ("one", "two", "three"):
            ws.send_text(json.dumps({"t": "text", "text": word}))
        # Make sure the server has read all three before the close arrives.
        time.sleep(0.2)
        ws.close()
        # The server finishes typing after the close; wait for it to drain.
        deadline = time.monotonic() + 5
        while not screens[0].closed and time.monotonic() < deadline:
            time.sleep(0.05)
    assert screens[0].inputs == [("text", "one"), ("text", "two"), ("text", "three")]
    assert screens[0].closed
