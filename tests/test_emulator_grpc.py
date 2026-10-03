from emulator_hub._grpc import emulator_controller_pb2 as pb
from emulator_hub.emulator_grpc import GrpcScreen


class RecordingStub:
    def __init__(self):
        self.keys = []

    async def sendKey(self, event):
        self.keys.append(event)


async def test_key_is_a_full_press_not_a_held_key():
    screen = GrpcScreen.__new__(GrpcScreen)
    screen._stub = RecordingStub()
    await screen.key("Enter")
    [event] = screen._stub.keys
    assert event.key == "Enter"
    # The proto default is keydown: Android would auto-repeat it.
    assert event.eventType == pb.KeyboardEvent.keypress


async def test_text_is_sent_in_short_paced_chunks(monkeypatch):
    import emulator_hub.emulator_grpc as eg

    sleeps = []

    async def fake_sleep(s):
        sleeps.append(s)

    monkeypatch.setattr(eg.asyncio, "sleep", fake_sleep)
    screen = GrpcScreen.__new__(GrpcScreen)
    screen._stub = RecordingStub()
    await screen.text("abc")
    assert [e.text for e in screen._stub.keys] == ["a", "b", "c"]
    assert sleeps == [eg.TEXT_CHUNK_GAP_S] * 2


async def test_text_skips_what_has_no_key(monkeypatch):
    import emulator_hub.emulator_grpc as eg

    async def no_sleep(_):
        pass

    monkeypatch.setattr(eg.asyncio, "sleep", no_sleep)
    screen = GrpcScreen.__new__(GrpcScreen)
    screen._stub = RecordingStub()
    await screen.text("a你é%b")
    assert [e.text for e in screen._stub.keys] == ["a", "%", "b"]
