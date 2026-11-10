"""Coordinator.release_probes: probe places pytest left locked for a DUT
place (openwrt-tests' PROBE_HOLDER comment) are released with the DUT."""

import asyncio
from types import SimpleNamespace

from kernelci_labgrid.labgrid import PROBE_HOLDER, Coordinator, Lease

ME = "labgrid-aparcar/labgrid-agent"
DUT = "labgrid-aparcar-enterasys_ws-ap3710i"


class FakeStub:
    """The coordinator's gRPC methods (hence N802)."""

    def __init__(self, session):
        self.session = session

    async def ReleasePlace(self, request):  # noqa: N802
        self.session.calls.append(("release", request.placename,
                                   request.fromuser if request.HasField("fromuser") else None))
        place = self.session.places[request.placename]
        if not request.HasField("fromuser") or place.acquired == request.fromuser:
            place.acquired = None

    async def SetPlaceComment(self, request):  # noqa: N802
        self.session.calls.append(("comment", request.placename, request.comment))
        self.session.places[request.placename].comment = request.comment

    async def CancelReservation(self, request):  # noqa: N802
        self.session.calls.append(("cancel", request.token))


class FakeSession:
    def __init__(self, places):
        self.places = places
        self.calls = []
        self.stub = FakeStub(self)
        self.stopping = asyncio.Event()

    async def sync_with_coordinator(self):
        pass

    def gethostname(self):
        return ME.split("/")[0]

    def getuser(self):
        return ME.split("/")[1]


def place(acquired=None, comment=""):
    return SimpleNamespace(acquired=acquired, comment=comment)


def coordinator(places):
    c = Coordinator(None, "labgrid-client", ".")
    c.session = FakeSession(places)
    return c


async def test_releases_probe_left_locked_for_the_dut():
    places = {
        "labgrid-aparcar-wifi-probe": place(ME, PROBE_HOLDER.format(DUT)),
        "labgrid-aparcar-probe": place(),
    }
    c = coordinator(places)
    await c.release_probes(DUT)
    assert places["labgrid-aparcar-wifi-probe"].acquired is None
    assert places["labgrid-aparcar-wifi-probe"].comment == ""
    assert c.session.calls == [
        ("release", "labgrid-aparcar-wifi-probe", ME),
        ("comment", "labgrid-aparcar-wifi-probe", ""),
    ]


async def test_leaves_probe_held_for_another_dut():
    other = PROBE_HOLDER.format("labgrid-aparcar-openwrt_one")
    places = {"labgrid-aparcar-wifi-probe": place(ME, other)}
    c = coordinator(places)
    await c.release_probes(DUT)
    assert places["labgrid-aparcar-wifi-probe"].acquired == ME
    assert c.session.calls == []


async def test_leaves_someone_elses_lock():
    places = {"labgrid-aparcar-wifi-probe": place("laptop/paul", PROBE_HOLDER.format(DUT))}
    c = coordinator(places)
    await c.release_probes(DUT)
    assert places["labgrid-aparcar-wifi-probe"].acquired == "laptop/paul"
    assert places["labgrid-aparcar-wifi-probe"].comment == PROBE_HOLDER.format(DUT)
    assert c.session.calls == []


async def test_clears_stale_comment_on_unlocked_probe():
    places = {"labgrid-aparcar-wifi-probe": place(None, PROBE_HOLDER.format(DUT))}
    c = coordinator(places)
    await c.release_probes(DUT)
    assert c.session.calls == [("comment", "labgrid-aparcar-wifi-probe", "")]


async def test_release_frees_probes_before_the_dut(monkeypatch):
    places = {
        DUT: place(ME),
        "labgrid-aparcar-wifi-probe": place(ME, PROBE_HOLDER.format(DUT)),
    }
    c = coordinator(places)

    async def no_power_off(lease):
        pass

    monkeypatch.setattr(c, "power_off", no_power_off)
    await c.release(Lease(reservation_id="RES1", place=DUT))
    assert [call[:2] for call in c.session.calls] == [
        ("release", "labgrid-aparcar-wifi-probe"),
        ("comment", "labgrid-aparcar-wifi-probe"),
        ("release", DUT),
        ("cancel", "RES1"),
    ]
    assert places[DUT].acquired is None
    assert places["labgrid-aparcar-wifi-probe"].acquired is None


async def test_never_raises():
    class Broken(FakeSession):
        async def sync_with_coordinator(self):
            raise RuntimeError("coordinator gone")

    c = Coordinator(None, "labgrid-client", ".")
    c.session = Broken({})
    await c.release_probes(DUT)
