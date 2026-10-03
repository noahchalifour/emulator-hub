"""Everything that speaks the emulator's gRPC bridge (`emulator -grpc 8554`).

The bridge runs without auth inside the emulator Pod; the emulator Pod's
NetworkPolicy admits :8554 only from the hub Pod, which is the protection.
"""

import asyncio
import io

import grpc
from google.protobuf.empty_pb2 import Empty
from PIL import Image

from emulator_hub._grpc import emulator_controller_pb2 as pb
from emulator_hub._grpc import emulator_controller_pb2_grpc as rpc
from emulator_hub.pods import GRPC_PORT

TEXT_CHUNK = 1
TEXT_CHUNK_GAP_S = 0.08
MAX_WIDTH = 480
JPEG_QUALITY = 70
KEYS = frozenset(
    {
        "GoHome",
        "GoBack",
        "AppSwitch",
        "Power",
        "AudioVolumeUp",
        "AudioVolumeDown",
        "Enter",
        "Backspace",
        "ArrowUp",
        "ArrowDown",
        "ArrowLeft",
        "ArrowRight",
    }
)


def _channel(pod_ip: str) -> grpc.aio.Channel:
    # A full-resolution RGB888 frame is ~11 MB; the 4 MB default would kill the
    # stream if the emulator ever ignores the requested size.
    return grpc.aio.insecure_channel(
        f"{pod_ip}:{GRPC_PORT}", options=[("grpc.max_receive_message_length", 32 * 1024 * 1024)]
    )


class GrpcBootProbe:
    async def booted(self, pod_ip: str) -> bool:
        async with _channel(pod_ip) as ch:
            try:
                status = await rpc.EmulatorControllerStub(ch).getStatus(Empty(), timeout=5)
            except grpc.aio.AioRpcError:
                return False  # bridge not up yet
            return bool(status.booted)


class GrpcScreen:
    """One live-view session: a frame stream plus input injection."""

    def __init__(self, pod_ip: str):
        self._channel = _channel(pod_ip)
        self._stub = rpc.EmulatorControllerStub(self._channel)
        self._size: tuple[int, int] | None = None

    async def _device_size(self) -> tuple[int, int]:
        if self._size is None:
            cfg = await self._stub.getDisplayConfigurations(Empty())
            primary = next(d for d in cfg.displays if d.display == 0)
            self._size = (primary.width, primary.height)
        return self._size

    async def frames(self):
        """Yields JPEG bytes. The emulator only sends a frame when the screen
        changes, so a static screen yields nothing; viewers keep the last one."""
        w, h = await self._device_size()
        width = min(MAX_WIDTH, w)
        # The emulator scales only when BOTH width and height are set.
        fmt = pb.ImageFormat(format=pb.ImageFormat.RGB888, width=width, height=round(h * width / w))
        async for img in self._stub.streamScreenshot(fmt):
            buf = io.BytesIO()
            Image.frombytes("RGB", (img.format.width, img.format.height), img.image).save(
                buf, "JPEG", quality=JPEG_QUALITY
            )
            yield buf.getvalue()

    async def snapshot(self, width: int = 240) -> bytes:
        """One small JPEG of the current screen, for dashboard thumbnails."""
        w, h = await self._device_size()
        fmt = pb.ImageFormat(format=pb.ImageFormat.RGB888, width=width, height=round(h * width / w))
        img = await self._stub.getScreenshot(fmt, timeout=5)
        buf = io.BytesIO()
        Image.frombytes("RGB", (img.format.width, img.format.height), img.image).save(buf, "JPEG", quality=60)
        return buf.getvalue()

    async def touch(self, x: float, y: float, down: bool) -> None:
        w, h = await self._device_size()
        touch = pb.Touch(x=int(x * w), y=int(y * h), identifier=0, pressure=1 if down else 0)
        await self._stub.sendTouch(pb.TouchEvent(touches=[touch]))

    async def key(self, key: str) -> None:
        # keypress = down + up. The proto default (keydown) never releases the
        # key, so Android auto-repeats it until the next event arrives.
        await self._stub.sendKey(pb.KeyboardEvent(key=key, eventType=pb.KeyboardEvent.keypress))

    async def text(self, text: str) -> None:
        # The emulator drops a whole text event longer than ~15 characters,
        # loses characters when events arrive faster than it types them, and
        # under load can leave Shift latched across events. One character per
        # event at fast-human typing pace (~12 chars/s) is the reliable rate.
        # Only printable ASCII has a key to press (emulator_controller.proto,
        # KeyboardEvent.text); anything else is skipped rather than sent.
        text = "".join(c for c in text if 32 <= ord(c) < 127)
        for i in range(0, len(text), TEXT_CHUNK):
            if i:
                await asyncio.sleep(TEXT_CHUNK_GAP_S)
            await self._stub.sendKey(pb.KeyboardEvent(text=text[i : i + TEXT_CHUNK]))

    async def close(self) -> None:
        await self._channel.close()
