"""The lab's labgrid-coordinator, via labgrid's own client library.

Each lab runs its own coordinator (LG_COORDINATOR, default 127.0.0.1:20408);
every place on it belongs to the lab. Places carry a `device=<platform>` tag
(see openwrt-tests' ansible places.yaml.j2), which is what jobs reserve by.

One long-lived labgrid ClientSession keeps `session.places` up to date over
the coordinator's stream; reservations and place locks are the same gRPC
calls labgrid-client makes. Only power control, which needs the target's
drivers, still runs `labgrid-client power off`.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shlex
import signal
import time
from dataclasses import dataclass, field
from pathlib import Path

# We start subprocesses (labgrid-client, pytest) while a gRPC channel is
# open; gRPC's fork handlers aren't needed for that and only print warnings.
os.environ.setdefault("GRPC_ENABLE_FORK_SUPPORT", "0")

import grpc  # noqa: E402
from labgrid.remote.client import ClientSession  # noqa: E402
from labgrid.remote.common import Reservation, ReservationState  # noqa: E402
from labgrid.remote.generated import labgrid_coordinator_pb2 as pb2  # noqa: E402
from labgrid.util.proxy import proxymanager  # noqa: E402

logger = logging.getLogger(__name__)

DEFAULT_COORDINATOR = "127.0.0.1:20408"
# Coordinators drop reservations 60s after the last poll; refresh well before
REFRESH_INTERVAL = 20


@dataclass
class Place:
    name: str
    tags: dict[str, str] = field(default_factory=dict)
    acquired: str | None = None
    reservation: str | None = None

    @property
    def device(self) -> str | None:
        return self.tags.get("device")

    @property
    def free(self) -> bool:
        return self.acquired is None and self.reservation is None


@dataclass
class Lease:
    """A reserved and locked place, held for one job or health check."""

    token: str
    place: str
    refresher: asyncio.Task | None = None


async def spawn(*cmd: str, **kwargs) -> asyncio.subprocess.Process:
    """Start a command in its own process group, so kill_group() also stops
    what `uv run` starts underneath it (otherwise the child keeps running and
    holds our pipes open, and asyncio waits forever)."""
    return await asyncio.create_subprocess_exec(*cmd, start_new_session=True, **kwargs)


async def kill_group(proc: asyncio.subprocess.Process) -> None:
    if proc.returncode is None:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        await proc.wait()


def child_env() -> dict[str, str]:
    """os.environ without our own virtualenv (uv run in the tests repo warns)."""
    env = os.environ.copy()
    env.pop("VIRTUAL_ENV", None)
    return env


class Coordinator:
    """Session with the lab's labgrid-coordinator."""

    def __init__(self, address: str | None, labgrid_command: str, cwd: Path):
        self.address = address or DEFAULT_COORDINATOR
        self.labgrid_command = shlex.split(labgrid_command)
        self.cwd = cwd
        self.session: ClientSession | None = None

    # --- connection --------------------------------------------------------

    async def connect(self) -> None:
        """(Re)connect if there is no live session."""
        if self.session and not self.session.stopping.is_set():
            return
        await self.close()
        address = proxymanager.get_grpc_address(self.address, default_port=20408)
        session = ClientSession(address=address, loop=asyncio.get_running_loop())
        await session.start()
        self.session = session
        logger.info(f"Connected to labgrid coordinator {self.address}")

    async def close(self) -> None:
        if not self.session:
            return
        session, self.session = self.session, None
        try:
            await session.stop()
            await session.close()
        except Exception as e:
            logger.debug(f"Closing labgrid session: {e}")

    # --- places ----------------------------------------------------------------

    async def places(self) -> list[Place]:
        await self.connect()
        return [
            Place(name=name, tags=dict(p.tags), acquired=p.acquired or None,
                  reservation=p.reservation or None)
            for name, p in sorted(self.session.places.items())
        ]

    # --- reservations and locks --------------------------------------------

    async def reserve(self, platform: str, timeout: float) -> Lease | None:
        """Reserve and lock a place with tag device=<platform>.

        Returns None (and cancels the reservation) if no place is allocated
        within `timeout` seconds or the lock fails.
        """
        await self.connect()
        request = pb2.CreateReservationRequest(
            filters={"main": pb2.Reservation.Filter(filter={"device": platform})}, prio=0.0)
        response = await self.session.stub.CreateReservation(request)
        token = Reservation.from_pb2(response.reservation).token

        place = None
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            res = await self._poll(token)
            if res.state is ReservationState.allocated:
                place = (res.allocations.get("main") or [None])[0]
                break
            if res.state is not ReservationState.waiting:
                logger.warning(f"Reservation {token} for {platform} ended as {res.state.name}")
                break
            await asyncio.sleep(1)

        if not place:
            await self.cancel(token)
            logger.info(f"No place for {platform} within {timeout:.0f}s")
            return None

        try:
            await self.session.stub.AcquirePlace(pb2.AcquirePlaceRequest(placename=place))
            await self.session.sync_with_coordinator()
        except grpc.aio.AioRpcError as e:
            logger.warning(f"Cannot lock place {place}: {e.details()}")
            await self.cancel(token)
            return None

        lease = Lease(token=token, place=place)
        lease.refresher = asyncio.create_task(self._refresh(token))
        logger.info(f"Reserved and locked place {place} for {platform} (reservation {token})")
        return lease

    async def _poll(self, token: str) -> Reservation:
        response = await self.session.stub.PollReservation(pb2.PollReservationRequest(token=token))
        return Reservation.from_pb2(response.reservation)

    async def _refresh(self, token: str) -> None:
        """Keep a reservation alive while its job runs."""
        while True:
            await asyncio.sleep(REFRESH_INTERVAL)
            try:
                await self._poll(token)
            except Exception as e:
                logger.warning(f"Refreshing reservation {token} failed: {e}")

    async def cancel(self, token: str) -> None:
        try:
            await self.session.stub.CancelReservation(pb2.CancelReservationRequest(token=token))
        except Exception as e:
            logger.warning(f"Cancelling reservation {token} failed: {e}")

    def env(self, lease: Lease) -> dict[str, str]:
        """Environment for pytest (and labgrid-client) on the leased place."""
        return {"LG_COORDINATOR": self.address, "LG_PLACE": lease.place}

    async def release(self, lease: Lease) -> None:
        """Power off, unlock and cancel the reservation; never raises."""
        if lease.refresher:
            lease.refresher.cancel()
        await self.power_off(lease)
        try:
            await self.connect()
            await self.session.stub.ReleasePlace(pb2.ReleasePlaceRequest(placename=lease.place))
            await self.session.sync_with_coordinator()
        except Exception as e:
            logger.warning(f"Unlocking place {lease.place} failed: {e}")
        await self.cancel(lease.token)
        logger.info(f"Released place {lease.place}")

    async def power_off(self, lease: Lease, timeout: float = 60) -> None:
        """Power control needs the target's drivers: use labgrid-client."""
        env = child_env()
        env.pop("LG_ENV", None)
        env.update(self.env(lease))
        proc = await spawn(*self.labgrid_command, "power", "off", cwd=self.cwd, env=env,
                           stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
        try:
            _, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            if proc.returncode:
                logger.warning(f"Power off {lease.place} failed: {err.decode(errors='replace').strip()}")
        except asyncio.TimeoutError:
            await kill_group(proc)
            logger.warning(f"Power off {lease.place} timed out")
