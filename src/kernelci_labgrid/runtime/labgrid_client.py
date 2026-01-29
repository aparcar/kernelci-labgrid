"""Labgrid Coordinator gRPC Client.

This module provides a high-level async client for interacting with
Labgrid coordinators using gRPC protocol (v25.0+).
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any, AsyncIterator

import grpc

logger = logging.getLogger(__name__)


class ReservationState(IntEnum):
    """Reservation state enumeration."""

    WAITING = 0
    ALLOCATED = 1
    ACQUIRED = 2
    EXPIRED = 3
    INVALID = 4


@dataclass
class Place:
    """Represents a Labgrid place."""

    name: str
    aliases: list[str] = field(default_factory=list)
    comment: str = ""
    tags: dict[str, str] = field(default_factory=dict)
    acquired: str = ""  # Client name if acquired, empty otherwise
    allowed: list[str] = field(default_factory=list)
    reservation: str = ""  # Reservation token if reserved

    @property
    def is_acquired(self) -> bool:
        """Check if place is currently acquired."""
        return bool(self.acquired)

    @property
    def is_reserved(self) -> bool:
        """Check if place has an active reservation."""
        return bool(self.reservation)


@dataclass
class Resource:
    """Represents a Labgrid resource."""

    exporter: str
    group: str
    cls: str  # Resource class name
    name: str
    params: dict[str, str] = field(default_factory=dict)
    extra: dict[str, str] = field(default_factory=dict)
    acquired: bool = False
    avail: bool = True

    @property
    def path(self) -> str:
        """Return the full resource path."""
        return f"{self.exporter}/{self.group}/{self.name}"


@dataclass
class Reservation:
    """Represents a Labgrid reservation."""

    owner: str
    token: str
    state: ReservationState
    prio: str = "0"
    filters: dict[str, str] = field(default_factory=dict)
    allocations: dict[str, str] = field(default_factory=dict)
    created: int = 0
    timeout: int = 0

    @property
    def is_allocated(self) -> bool:
        """Check if reservation has been allocated."""
        return self.state == ReservationState.ALLOCATED

    @property
    def is_expired(self) -> bool:
        """Check if reservation has expired."""
        return self.state == ReservationState.EXPIRED


class LabgridClientError(Exception):
    """Base exception for Labgrid client errors."""

    pass


class LabgridConnectionError(LabgridClientError):
    """Raised when connection to coordinator fails."""

    pass


class LabgridPlaceError(LabgridClientError):
    """Raised when place operations fail."""

    pass


class LabgridReservationError(LabgridClientError):
    """Raised when reservation operations fail."""

    pass


class LabgridClient:
    """Async gRPC client for Labgrid coordinator.

    This client provides methods for interacting with the Labgrid coordinator
    including place management, reservations, and resource queries.

    Example:
        async with LabgridClient("coordinator.example.com:20408") as client:
            places = await client.get_places()
            for place in places:
                print(f"Place: {place.name}, Acquired: {place.is_acquired}")

            # Reserve and acquire a place
            reservation = await client.create_reservation({"board": "main"})
            await client.poll_reservation_until_allocated(reservation.token)
            await client.acquire_place("my-board")
            try:
                # Do work with the board
                pass
            finally:
                await client.release_place("my-board")
    """

    def __init__(
        self,
        coordinator_address: str,
        *,
        secure: bool = False,
        credentials: grpc.ChannelCredentials | None = None,
        keepalive_time_ms: int = 7500,
        keepalive_timeout_ms: int = 10000,
        client_name: str = "kernelci-labgrid",
    ):
        """Initialize the Labgrid client.

        Args:
            coordinator_address: Address of the coordinator (host:port)
            secure: Whether to use TLS (requires credentials)
            credentials: gRPC channel credentials for TLS
            keepalive_time_ms: gRPC keepalive interval
            keepalive_timeout_ms: gRPC keepalive timeout
            client_name: Client identifier for logging and tracking
        """
        self.coordinator_address = coordinator_address
        self.secure = secure
        self.credentials = credentials
        self.client_name = client_name
        self._channel: grpc.aio.Channel | None = None
        self._stub: Any = None  # CoordinatorStub from generated code
        self._connected = False
        self._options = [
            ("grpc.keepalive_time_ms", keepalive_time_ms),
            ("grpc.keepalive_timeout_ms", keepalive_timeout_ms),
            ("grpc.keepalive_permit_without_calls", True),
            ("grpc.http2.min_time_between_pings_ms", 5000),
        ]

    async def __aenter__(self) -> "LabgridClient":
        """Async context manager entry."""
        await self.connect()
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        """Async context manager exit."""
        await self.close()

    async def connect(self) -> None:
        """Establish connection to the coordinator.

        Raises:
            LabgridConnectionError: If connection fails
        """
        try:
            if self.secure and self.credentials:
                self._channel = grpc.aio.secure_channel(
                    self.coordinator_address,
                    self.credentials,
                    options=self._options,
                )
            else:
                self._channel = grpc.aio.insecure_channel(
                    self.coordinator_address,
                    options=self._options,
                )

            # Import generated stubs dynamically to handle missing generated code
            try:
                from kernelci_labgrid.generated import (
                    labgrid_coordinator_pb2_grpc as coordinator_grpc,
                )

                self._stub = coordinator_grpc.CoordinatorStub(self._channel)
            except ImportError:
                # Fall back to basic channel if stubs not generated
                logger.warning(
                    "Generated gRPC stubs not found. "
                    "Run protoc to generate labgrid_coordinator_pb2*.py"
                )
                self._stub = None

            # Verify connection by getting version
            if self._stub:
                await self._verify_connection()

            self._connected = True
            logger.info(f"Connected to Labgrid coordinator at {self.coordinator_address}")

        except grpc.aio.AioRpcError as e:
            raise LabgridConnectionError(
                f"Failed to connect to coordinator at {self.coordinator_address}: {e}"
            ) from e

    async def _verify_connection(self) -> None:
        """Verify connection by querying coordinator version."""
        try:
            from kernelci_labgrid.generated import labgrid_coordinator_pb2 as coordinator_pb2

            response = await self._stub.GetVersion(
                coordinator_pb2.GetVersionRequest(), timeout=5.0
            )
            logger.info(f"Labgrid coordinator version: {response.version}")
        except Exception as e:
            logger.warning(f"Could not verify coordinator version: {e}")

    async def close(self) -> None:
        """Close the connection to the coordinator."""
        if self._channel:
            await self._channel.close()
            self._channel = None
            self._stub = None
            self._connected = False
            logger.info("Disconnected from Labgrid coordinator")

    @property
    def is_connected(self) -> bool:
        """Check if client is connected."""
        return self._connected and self._channel is not None

    def _ensure_connected(self) -> None:
        """Ensure client is connected before operations."""
        if not self.is_connected:
            raise LabgridClientError("Client not connected. Call connect() first.")

    # ============ Place Operations ============

    async def get_places(
        self, filters: dict[str, str] | None = None
    ) -> list[Place]:
        """Get list of all places from coordinator.

        Args:
            filters: Optional filters to apply (tag-based)

        Returns:
            List of Place objects
        """
        self._ensure_connected()

        try:
            from kernelci_labgrid.generated import labgrid_coordinator_pb2 as coordinator_pb2

            request = coordinator_pb2.GetPlacesRequest(filter=filters or {})
            response = await self._stub.GetPlaces(request)

            return [
                Place(
                    name=p.name,
                    aliases=list(p.aliases),
                    comment=p.comment,
                    tags=dict(p.tags),
                    acquired=p.acquired,
                    allowed=list(p.allowed),
                    reservation=p.reservation,
                )
                for p in response.places
            ]
        except grpc.aio.AioRpcError as e:
            raise LabgridPlaceError(f"Failed to get places: {e}") from e

    async def get_place(self, name: str) -> Place:
        """Get a specific place by name.

        Args:
            name: Place name

        Returns:
            Place object

        Raises:
            LabgridPlaceError: If place not found or query fails
        """
        self._ensure_connected()

        try:
            from kernelci_labgrid.generated import labgrid_coordinator_pb2 as coordinator_pb2

            request = coordinator_pb2.GetPlaceRequest(name=name)
            response = await self._stub.GetPlace(request)

            p = response.place
            return Place(
                name=p.name,
                aliases=list(p.aliases),
                comment=p.comment,
                tags=dict(p.tags),
                acquired=p.acquired,
                allowed=list(p.allowed),
                reservation=p.reservation,
            )
        except grpc.aio.AioRpcError as e:
            raise LabgridPlaceError(f"Failed to get place '{name}': {e}") from e

    async def acquire_place(self, name: str) -> bool:
        """Acquire exclusive access to a place.

        Args:
            name: Place name to acquire

        Returns:
            True if acquisition successful

        Raises:
            LabgridPlaceError: If acquisition fails
        """
        self._ensure_connected()

        try:
            from kernelci_labgrid.generated import labgrid_coordinator_pb2 as coordinator_pb2

            request = coordinator_pb2.AcquirePlaceRequest(name=name)
            response = await self._stub.AcquirePlace(request)

            if not response.success:
                raise LabgridPlaceError(
                    f"Failed to acquire place '{name}': {response.message}"
                )

            logger.info(f"Acquired place: {name}")
            return True
        except grpc.aio.AioRpcError as e:
            raise LabgridPlaceError(f"Failed to acquire place '{name}': {e}") from e

    async def release_place(self, name: str) -> bool:
        """Release a previously acquired place.

        Args:
            name: Place name to release

        Returns:
            True if release successful

        Raises:
            LabgridPlaceError: If release fails
        """
        self._ensure_connected()

        try:
            from kernelci_labgrid.generated import labgrid_coordinator_pb2 as coordinator_pb2

            request = coordinator_pb2.ReleasePlaceRequest(name=name)
            response = await self._stub.ReleasePlace(request)

            if not response.success:
                raise LabgridPlaceError(
                    f"Failed to release place '{name}': {response.message}"
                )

            logger.info(f"Released place: {name}")
            return True
        except grpc.aio.AioRpcError as e:
            raise LabgridPlaceError(f"Failed to release place '{name}': {e}") from e

    async def set_place_tags(self, name: str, tags: dict[str, str]) -> bool:
        """Set tags on a place.

        Args:
            name: Place name
            tags: Tags to set

        Returns:
            True if successful
        """
        self._ensure_connected()

        try:
            from kernelci_labgrid.generated import labgrid_coordinator_pb2 as coordinator_pb2

            request = coordinator_pb2.SetPlaceTagsRequest(name=name, tags=tags)
            response = await self._stub.SetPlaceTags(request)
            return response.success
        except grpc.aio.AioRpcError as e:
            raise LabgridPlaceError(f"Failed to set tags on place '{name}': {e}") from e

    # ============ Reservation Operations ============

    async def create_reservation(
        self, filters: dict[str, str], priority: str = "0"
    ) -> Reservation:
        """Create a reservation for places matching filters.

        Args:
            filters: Filter criteria for place selection
            priority: Reservation priority (higher = more urgent)

        Returns:
            Reservation object with token

        Raises:
            LabgridReservationError: If reservation creation fails
        """
        self._ensure_connected()

        try:
            from kernelci_labgrid.generated import labgrid_coordinator_pb2 as coordinator_pb2

            request = coordinator_pb2.CreateReservationRequest(
                filters=filters, prio=priority
            )
            response = await self._stub.CreateReservation(request)

            r = response.reservation
            reservation = Reservation(
                owner=r.owner,
                token=r.token,
                state=ReservationState(r.state),
                prio=r.prio,
                filters=dict(r.filters),
                allocations=dict(r.allocations),
                created=r.created,
                timeout=r.timeout,
            )

            logger.info(f"Created reservation: {reservation.token}")
            return reservation
        except grpc.aio.AioRpcError as e:
            raise LabgridReservationError(f"Failed to create reservation: {e}") from e

    async def cancel_reservation(self, token: str) -> bool:
        """Cancel an existing reservation.

        Args:
            token: Reservation token

        Returns:
            True if cancellation successful
        """
        self._ensure_connected()

        try:
            from kernelci_labgrid.generated import labgrid_coordinator_pb2 as coordinator_pb2

            request = coordinator_pb2.CancelReservationRequest(token=token)
            response = await self._stub.CancelReservation(request)

            if response.success:
                logger.info(f"Cancelled reservation: {token}")
            return response.success
        except grpc.aio.AioRpcError as e:
            raise LabgridReservationError(
                f"Failed to cancel reservation '{token}': {e}"
            ) from e

    async def poll_reservation(self, token: str) -> Reservation:
        """Poll the status of a reservation.

        Args:
            token: Reservation token

        Returns:
            Updated Reservation object
        """
        self._ensure_connected()

        try:
            from kernelci_labgrid.generated import labgrid_coordinator_pb2 as coordinator_pb2

            request = coordinator_pb2.PollReservationRequest(token=token)
            response = await self._stub.PollReservation(request)

            r = response.reservation
            return Reservation(
                owner=r.owner,
                token=r.token,
                state=ReservationState(r.state),
                prio=r.prio,
                filters=dict(r.filters),
                allocations=dict(r.allocations),
                created=r.created,
                timeout=r.timeout,
            )
        except grpc.aio.AioRpcError as e:
            raise LabgridReservationError(
                f"Failed to poll reservation '{token}': {e}"
            ) from e

    async def poll_reservation_until_allocated(
        self,
        token: str,
        timeout: float = 300.0,
        poll_interval: float = 1.0,
    ) -> Reservation:
        """Poll reservation until it becomes allocated or times out.

        Args:
            token: Reservation token
            timeout: Maximum time to wait in seconds
            poll_interval: Time between polls in seconds

        Returns:
            Allocated Reservation object

        Raises:
            LabgridReservationError: If reservation expires or times out
        """
        import time

        start = time.monotonic()

        while time.monotonic() - start < timeout:
            reservation = await self.poll_reservation(token)

            if reservation.is_allocated:
                logger.info(
                    f"Reservation {token} allocated: {reservation.allocations}"
                )
                return reservation

            if reservation.is_expired:
                raise LabgridReservationError(
                    f"Reservation {token} expired before allocation"
                )

            if reservation.state == ReservationState.INVALID:
                raise LabgridReservationError(f"Reservation {token} is invalid")

            await asyncio.sleep(poll_interval)

        raise LabgridReservationError(
            f"Reservation {token} allocation timed out after {timeout}s"
        )

    async def get_reservations(self) -> list[Reservation]:
        """Get all active reservations.

        Returns:
            List of Reservation objects
        """
        self._ensure_connected()

        try:
            from kernelci_labgrid.generated import labgrid_coordinator_pb2 as coordinator_pb2

            request = coordinator_pb2.GetReservationsRequest()
            response = await self._stub.GetReservations(request)

            return [
                Reservation(
                    owner=r.owner,
                    token=r.token,
                    state=ReservationState(r.state),
                    prio=r.prio,
                    filters=dict(r.filters),
                    allocations=dict(r.allocations),
                    created=r.created,
                    timeout=r.timeout,
                )
                for r in response.reservations
            ]
        except grpc.aio.AioRpcError as e:
            raise LabgridReservationError(f"Failed to get reservations: {e}") from e

    # ============ Resource Operations ============

    async def get_resources(
        self, filters: dict[str, str] | None = None
    ) -> list[Resource]:
        """Get list of all resources from coordinator.

        Args:
            filters: Optional filters to apply

        Returns:
            List of Resource objects
        """
        self._ensure_connected()

        try:
            from kernelci_labgrid.generated import labgrid_coordinator_pb2 as coordinator_pb2

            request = coordinator_pb2.GetResourcesRequest(filter=filters or {})
            response = await self._stub.GetResources(request)

            return [
                Resource(
                    exporter=r.exporter,
                    group=r.group,
                    cls=r.cls,
                    name=r.name,
                    params=dict(r.params),
                    extra=dict(r.extra),
                    acquired=r.acquired,
                    avail=r.avail,
                )
                for r in response.resources
            ]
        except grpc.aio.AioRpcError as e:
            raise LabgridClientError(f"Failed to get resources: {e}") from e

    # ============ Utility Methods ============

    async def find_places_by_compatible(
        self, compatible: list[str]
    ) -> list[Place]:
        """Find places that match device tree compatible strings.

        This searches for places with tags matching the given compatible strings.

        Args:
            compatible: List of device tree compatible strings

        Returns:
            List of matching Place objects
        """
        places = await self.get_places()
        matching = []

        for place in places:
            place_compatible = place.tags.get("compatible", "").split(",")
            if any(c in place_compatible for c in compatible):
                matching.append(place)

        return matching

    async def find_available_place(
        self,
        platform: str | None = None,
        compatible: list[str] | None = None,
        tags: dict[str, str] | None = None,
    ) -> Place | None:
        """Find an available (not acquired) place matching criteria.

        Args:
            platform: Platform name to match (via 'platform' tag)
            compatible: Device tree compatible strings to match
            tags: Additional tags to match

        Returns:
            First available matching Place, or None if none found
        """
        places = await self.get_places()

        for place in places:
            if place.is_acquired:
                continue

            if platform and place.tags.get("platform") != platform:
                continue

            if compatible:
                place_compat = place.tags.get("compatible", "").split(",")
                if not any(c in place_compat for c in compatible):
                    continue

            if tags:
                if not all(place.tags.get(k) == v for k, v in tags.items()):
                    continue

            return place

        return None
