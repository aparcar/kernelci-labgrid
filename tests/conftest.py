"""Pytest configuration and shared fixtures."""

import os
import tempfile
from pathlib import Path

import pytest


@pytest.fixture
def temp_dir():
    """Create a temporary directory for tests."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)


@pytest.fixture
def mock_env_vars():
    """Set up mock environment variables for tests."""
    original = os.environ.copy()

    os.environ["KCI_API_URL"] = "https://test-api.kernelci.org"
    os.environ["KCI_API_TOKEN"] = "test-token-12345"

    yield

    os.environ.clear()
    os.environ.update(original)


def pytest_configure(config):
    """Configure custom markers."""
    config.addinivalue_line(
        "markers", "integration: mark test as integration test"
    )
