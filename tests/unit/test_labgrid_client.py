"""Unit tests for Labgrid gRPC client."""

import pytest

from kernelci_labgrid.runtime.labgrid_client import (
    LabgridClient,
    LabgridClientError,
    LabgridConnectionError,
    Place,
    Resource,
    Reservation,
    ReservationState,
)


class TestPlace:
    """Tests for Place dataclass."""

    def test_basic_place(self):
        """Test creating a basic place."""
        place = Place(
            name="test-board",
            aliases=["board-1"],
            comment="Test board",
            tags={"arch": "arm64"},
        )

        assert place.name == "test-board"
        assert "board-1" in place.aliases
        assert place.tags["arch"] == "arm64"
        assert not place.is_acquired
        assert not place.is_reserved

    def test_acquired_place(self):
        """Test acquired place detection."""
        place = Place(name="test", acquired="user@host")
        assert place.is_acquired

        place = Place(name="test", acquired="")
        assert not place.is_acquired

    def test_reserved_place(self):
        """Test reserved place detection."""
        place = Place(name="test", reservation="token-123")
        assert place.is_reserved

        place = Place(name="test", reservation="")
        assert not place.is_reserved


class TestResource:
    """Tests for Resource dataclass."""

    def test_basic_resource(self):
        """Test creating a basic resource."""
        resource = Resource(
            exporter="exporter-1",
            group="board-group",
            cls="RawSerialPort",
            name="serial0",
            params={"port": "/dev/ttyUSB0"},
        )

        assert resource.exporter == "exporter-1"
        assert resource.cls == "RawSerialPort"
        assert resource.params["port"] == "/dev/ttyUSB0"
        assert resource.avail

    def test_resource_path(self):
        """Test resource path generation."""
        resource = Resource(
            exporter="exp1",
            group="grp1",
            cls="cls",
            name="res1",
        )

        assert resource.path == "exp1/grp1/res1"


class TestReservation:
    """Tests for Reservation dataclass."""

    def test_basic_reservation(self):
        """Test creating a basic reservation."""
        reservation = Reservation(
            owner="user@host",
            token="abc123",
            state=ReservationState.WAITING,
        )

        assert reservation.owner == "user@host"
        assert reservation.token == "abc123"
        assert not reservation.is_allocated
        assert not reservation.is_expired

    def test_allocated_reservation(self):
        """Test allocated reservation detection."""
        reservation = Reservation(
            owner="user",
            token="token",
            state=ReservationState.ALLOCATED,
            allocations={"main": "board-1"},
        )

        assert reservation.is_allocated
        assert "main" in reservation.allocations

    def test_expired_reservation(self):
        """Test expired reservation detection."""
        reservation = Reservation(
            owner="user",
            token="token",
            state=ReservationState.EXPIRED,
        )

        assert reservation.is_expired
        assert not reservation.is_allocated


class TestLabgridClient:
    """Tests for LabgridClient class."""

    def test_initialization(self):
        """Test client initialization."""
        client = LabgridClient(
            "localhost:20408",
            client_name="test-client",
        )

        assert client.coordinator_address == "localhost:20408"
        assert client.client_name == "test-client"
        assert not client.is_connected

    def test_not_connected_error(self):
        """Test error when operations called without connection."""
        client = LabgridClient("localhost:20408")

        with pytest.raises(LabgridClientError, match="not connected"):
            client._ensure_connected()

    @pytest.mark.asyncio
    async def test_context_manager(self):
        """Test async context manager entry/exit."""
        # This will fail to connect since there's no coordinator
        # but tests the context manager structure
        client = LabgridClient("invalid:99999")

        with pytest.raises(LabgridConnectionError):
            async with client:
                pass

    def test_secure_client(self):
        """Test creating secure client."""
        import grpc

        credentials = grpc.ssl_channel_credentials()
        client = LabgridClient(
            "secure.example.com:20408",
            secure=True,
            credentials=credentials,
        )

        assert client.secure
        assert client.credentials is not None


class TestLabgridClientMocked:
    """Tests for LabgridClient with mocked gRPC."""

    @pytest.fixture
    def mock_client(self):
        """Create a client with mocked internals."""
        client = LabgridClient("localhost:20408")
        client._connected = True
        return client

    def test_is_connected(self, mock_client):
        """Test connection status check."""
        assert mock_client.is_connected

    @pytest.mark.asyncio
    async def test_close(self, mock_client):
        """Test closing connection."""
        mock_client._channel = None  # Simulate no channel
        await mock_client.close()
        assert not mock_client.is_connected
