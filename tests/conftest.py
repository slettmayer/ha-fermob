"""Shared fixtures for the Home Assistant-backed tests.

`tests/test_protocol.py` needs none of this -- it loads `protocol.py` by file
path precisely so it stays HA-free. This file exists for `test_light.py`.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Let HA load `custom_components/fermob` at all.

    Home Assistant refuses to set up custom integrations in tests unless this
    fixture is requested, and the failure mode is a confusing "integration not
    found" rather than anything about tests.
    """
    return
