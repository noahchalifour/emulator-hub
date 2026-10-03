"""A stand-in for the emulator image, for control-plane end-to-end tests.

It honours the same contract the hub relies on, without Android:
  * exits 3 when /dev/kvm is missing or not writable (like entrypoint.sh)
  * the gRPC bridge on :8554 answers getStatus (booted after BOOT_DELAY_S),
    getDisplayConfigurations, getScreenshot, streamScreenshot, sendTouch and
    sendKey, from the same proto the hub uses
  * :5557 (the adb port behind the slot Service) answers every connection
    with "<pod hostname>\n", so tests can see which Pod a Service routes to

Every input event is printed as `EVENT {json}` so tests read it from the Pod log.
FAKE_MODE (baked in at build time) selects a failure:
  ok | never-boots | exit-during-boot
"""

import asyncio
import io
import json
import os
import socket
import sys
import time

import grpc
from google.protobuf.empty_pb2 import Empty
from PIL import Image

from emulator_hub._grpc import emulator_controller_pb2 as pb
from emulator_hub._grpc import emulator_controller_pb2_grpc as rpc

SIZES = {
    "pixel_8": (1080, 2400),
    "medium_phone": (1080, 2400),
    "pixel_tablet": (2560, 1600),
    "medium_tablet": (2560, 1600),
    "tv_1080p": (1920, 1080),
    "tv_720p": (1280, 720),
}
MODE = os.environ.get("FAKE_MODE", "ok")
BOOT_DELAY_S = float(os.environ.get("BOOT_DELAY_S", "4"))
STARTED = time.monotonic()
WIDTH, HEIGHT = SIZES.get(os.environ.get("DEVICE", ""), (1080, 2400))


def event(kind: str, **fields) -> None:
    print("EVENT " + json.dumps({"type": kind, **fields}), flush=True)


class Screen:
    def __init__(self):
        self.version = 0
        self.changed = asyncio.Condition()

    async def bump(self):
        async with self.changed:
            self.version += 1
            self.changed.notify_all()

    def render(self, fmt: pb.ImageFormat) -> pb.Image:
        w = fmt.width or WIDTH
        h = fmt.height or HEIGHT
        shade = (self.version * 37) % 256
        img = Image.new("RGB", (w, h), (shade, 255 - shade, 128))
        if fmt.format == pb.ImageFormat.PNG:
            buf = io.BytesIO()
            img.save(buf, "PNG")
            data = buf.getvalue()
        else:
            data = img.tobytes()
        return pb.Image(format=pb.ImageFormat(format=fmt.format, width=w, height=h), image=data, seq=self.version)


SCREEN = Screen()


class Controller(rpc.EmulatorControllerServicer):
    async def getStatus(self, request, context):
        booted = MODE != "never-boots" and time.monotonic() - STARTED >= BOOT_DELAY_S
        cfg = pb.EntryList(
            entry=[
                pb.Entry(key="hw.ramSize", value=os.environ.get("RAM_MB", "")),
                pb.Entry(key="hw.cpu.ncore", value=os.environ.get("CORES", "")),
                pb.Entry(key="hw.lcd.width", value=str(WIDTH)),
                pb.Entry(key="hw.lcd.height", value=str(HEIGHT)),
                pb.Entry(key="fake.system_image", value=os.environ.get("SYSTEM_IMAGE", "")),
                pb.Entry(key="fake.device", value=os.environ.get("DEVICE", "")),
            ]
        )
        return pb.EmulatorStatus(
            version="fake", uptime=int((time.monotonic() - STARTED) * 1000), booted=booted, hardwareConfig=cfg
        )

    async def getDisplayConfigurations(self, request, context):
        return pb.DisplayConfigurations(displays=[pb.DisplayConfiguration(width=WIDTH, height=HEIGHT, dpi=420)])

    async def getScreenshot(self, request, context):
        return SCREEN.render(request)

    async def streamScreenshot(self, request, context):
        event("stream_open")
        try:
            seen = -1
            while True:
                async with SCREEN.changed:
                    await SCREEN.changed.wait_for(lambda seen=seen: SCREEN.version != seen)
                    seen = SCREEN.version
                yield SCREEN.render(request)
        finally:
            event("stream_closed")

    async def sendTouch(self, request, context):
        for t in request.touches:
            event("touch", x=t.x, y=t.y, pressure=t.pressure, w=WIDTH, h=HEIGHT)
        await SCREEN.bump()
        return Empty()

    async def sendKey(self, request, context):
        event("key", key=request.key, text=request.text)
        await SCREEN.bump()
        return Empty()


async def adb_banner(reader, writer):
    writer.write((socket.gethostname() + "\n").encode())
    await writer.drain()
    writer.close()


async def main():
    if not os.access("/dev/kvm", os.W_OK):
        print("FATAL: /dev/kvm missing or not writable.", file=sys.stderr, flush=True)
        sys.exit(3)
    if MODE == "exit-during-boot":
        await asyncio.sleep(2)
        print("FATAL: fake emulator exiting during boot", file=sys.stderr, flush=True)
        sys.exit(1)
    server = grpc.aio.server()
    rpc.add_EmulatorControllerServicer_to_server(Controller(), server)
    server.add_insecure_port("0.0.0.0:8554")
    await server.start()
    banner = await asyncio.start_server(adb_banner, "0.0.0.0", 5557)
    event("started", mode=MODE, device=os.environ.get("DEVICE"))

    async def tick():
        # A real launcher redraws now and then; keep the stream alive.
        while True:
            await asyncio.sleep(0.2)
            await SCREEN.bump()

    async with banner:
        await asyncio.gather(server.wait_for_termination(), banner.serve_forever(), tick())


if __name__ == "__main__":
    asyncio.run(main())
