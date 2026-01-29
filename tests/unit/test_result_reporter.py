"""Unit tests for result reporter."""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from kernelci_labgrid.result_reporter import (
    LabgridResultReporter,
    APIError,
)


class TestLabgridResultReporter:
    """Tests for LabgridResultReporter class."""

    def test_initialization(self):
        """Test reporter initialization."""
        reporter = LabgridResultReporter(
            "https://api.example.com",
            "test-token",
            timeout=60,
            retry_count=5,
        )

        assert reporter.api_url == "https://api.example.com"
        assert reporter.api_token == "test-token"
        assert reporter.retry_count == 5

    def test_url_trailing_slash_removed(self):
        """Test that trailing slash is removed from URL."""
        reporter = LabgridResultReporter(
            "https://api.example.com/",
            "token",
        )

        assert reporter.api_url == "https://api.example.com"

    @pytest.mark.asyncio
    async def test_context_manager(self):
        """Test async context manager."""
        reporter = LabgridResultReporter("https://api.example.com", "token")

        async with reporter:
            assert reporter._session is not None

        assert reporter._session is None

    def test_session_not_connected(self):
        """Test error when session not created."""
        reporter = LabgridResultReporter("https://api.example.com", "token")

        with pytest.raises(RuntimeError, match="not connected"):
            _ = reporter.session


class TestResultReporterParsing:
    """Tests for result parsing methods."""

    @pytest.fixture
    def reporter(self):
        """Create a reporter for testing."""
        return LabgridResultReporter("https://api.example.com", "token")

    def test_parse_pytest_output(self, reporter):
        """Test parsing pytest output."""
        output = """
============================= test session starts ==============================
platform linux -- Python 3.11.0
collected 5 items

test_basic.py::test_login PASSED
test_basic.py::test_boot PASSED
test_basic.py::test_network FAILED
test_basic.py::test_memory PASSED
test_basic.py::test_gpu PASSED

========================= 4 passed, 1 failed in 5.23s =========================
"""

        results = reporter.parse_pytest_output(output)

        assert len(results) >= 4
        pass_count = sum(1 for r in results if r["result"] == "pass")
        fail_count = sum(1 for r in results if r["result"] == "fail")
        assert pass_count >= 3
        assert fail_count >= 1

    def test_parse_tap_output(self, reporter):
        """Test parsing TAP output."""
        output = """
TAP version 13
1..4
ok 1 - test_a
ok 2 - test_b
not ok 3 - test_c
ok 4 - test_d
"""

        results = reporter.parse_tap_output(output)

        assert len(results) == 4
        assert results[0]["result"] == "pass"
        assert results[2]["result"] == "fail"

    def test_parse_tap_empty(self, reporter):
        """Test parsing empty TAP output."""
        results = reporter.parse_tap_output("")
        assert results == []

    def test_parse_tap_with_descriptions(self, reporter):
        """Test parsing TAP with test descriptions."""
        output = """
ok 1 - Login as root user
not ok 2 - Network connectivity test
"""

        results = reporter.parse_tap_output(output)

        assert len(results) == 2
        # Check that descriptions are captured (minus the number)
        assert "root" in results[0]["name"].lower() or "login" in results[0]["name"].lower()


class TestResultReporterMocked:
    """Tests for reporter with mocked HTTP."""

    @pytest.fixture
    def mock_session(self):
        """Create a mock aiohttp session."""
        session = AsyncMock()
        return session

    @pytest.fixture
    def reporter_with_session(self, mock_session):
        """Create a reporter with mocked session."""
        reporter = LabgridResultReporter("https://api.example.com", "token")
        reporter._session = mock_session
        return reporter

    @pytest.mark.asyncio
    async def test_create_node(self, reporter_with_session, mock_session):
        """Test creating a node."""
        # Mock the response
        mock_response = AsyncMock()
        mock_response.status = 200
        mock_response.json = AsyncMock(return_value={"id": "new-node-123"})
        mock_response.__aenter__ = AsyncMock(return_value=mock_response)
        mock_response.__aexit__ = AsyncMock(return_value=None)

        mock_session.request = MagicMock(return_value=mock_response)

        result = await reporter_with_session.create_node({"name": "test"})

        assert result["id"] == "new-node-123"

    @pytest.mark.asyncio
    async def test_api_error_handling(self, reporter_with_session, mock_session):
        """Test API error handling."""
        # Mock error response
        mock_response = AsyncMock()
        mock_response.status = 500
        mock_response.text = AsyncMock(return_value="Internal Server Error")
        mock_response.__aenter__ = AsyncMock(return_value=mock_response)
        mock_response.__aexit__ = AsyncMock(return_value=None)

        mock_session.request = MagicMock(return_value=mock_response)

        with pytest.raises(APIError) as exc_info:
            await reporter_with_session._request("GET", "/test")

        assert exc_info.value.status_code == 500


class TestJobNodeCreation:
    """Tests for job node creation helpers."""

    @pytest.fixture
    def reporter(self):
        """Create a reporter for testing."""
        return LabgridResultReporter("https://api.example.com", "token")

    @pytest.mark.asyncio
    async def test_create_job_node_structure(self, reporter):
        """Test job node data structure."""
        # We can't test actual API calls without mocking
        # But we can verify the data structure that would be sent

        # Mock create_node to capture the data
        captured_data = {}

        async def capture_create(data):
            captured_data.update(data)
            return {"id": "test-id"}

        reporter.create_node = capture_create

        await reporter.create_job_node(
            parent_id="parent-123",
            job_name="baseline-arm64",
            platform="bcm2711-rpi-4-b",
            artifacts={"kernel": "url"},
            runtime="labgrid",
            lab_name="test-lab",
        )

        assert captured_data["name"] == "baseline-arm64"
        assert captured_data["parent"] == "parent-123"
        assert captured_data["kind"] == "job"
        assert captured_data["state"] == "pending"
        assert captured_data["data"]["platform"] == "bcm2711-rpi-4-b"
        assert captured_data["data"]["runtime"] == "labgrid"
        assert captured_data["data"]["lab"] == "test-lab"
