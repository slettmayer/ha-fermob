"""How the connection-mode option turns into the two timings it controls.

The point of `resolve_connection_profile` is that the idle timeout and the
check-in interval are chosen together. They interact -- a check-in re-arms the
idle timer, so an interval shorter than the timeout holds the link open no
matter what the timeout says -- and deriving both from one user-facing choice is
what makes that combination impossible to configure by accident. These tests pin
that coupling, not the particular numbers.
"""

from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace

import pytest

from custom_components.fermob import resolve_connection_profile
from custom_components.fermob.config_flow import (
    CONF_CONNECTION_MODE,
    CONNECTION_MODE_ALWAYS,
    CONNECTION_MODE_ON_DEMAND,
)


def _entry(**options) -> SimpleNamespace:
    """A stand-in for ConfigEntry: only `.options` is read."""
    return SimpleNamespace(options=options)


def test_the_default_holds_the_link_open():
    """A lamp added before this option existed must get the new behaviour.

    Holding the link is the only way a physical button press reaches Home
    Assistant; the cost it carries -- a connection slot -- is exactly what the
    option exists to hand back, so it has to be opt-in rather than opt-out.
    """
    profile = resolve_connection_profile(_entry())
    assert profile.idle_disconnect_delay is None


def test_always_connected_is_explicitly_the_same_as_the_default():
    assert resolve_connection_profile(
        _entry(**{CONF_CONNECTION_MODE: CONNECTION_MODE_ALWAYS})
    ) == resolve_connection_profile(_entry())


def test_on_demand_drops_the_link_and_slows_the_check_in():
    """Both halves move together: with nothing listening between commands, the
    check-in cannot be a reconnect heartbeat, only a battery poll."""
    profile = resolve_connection_profile(
        _entry(**{CONF_CONNECTION_MODE: CONNECTION_MODE_ON_DEMAND})
    )
    assert profile.idle_disconnect_delay == 30.0
    assert profile.check_in_interval == timedelta(hours=6)


@pytest.mark.parametrize(
    "mode", [CONNECTION_MODE_ALWAYS, CONNECTION_MODE_ON_DEMAND, "nonsense", None]
)
def test_a_check_in_never_outlives_its_idle_timeout(mode):
    """The invariant the mode exists to protect.

    A check-in that fires more often than the idle timeout re-arms the timer
    every time, so the link never drops and the timeout silently does nothing.
    Every profile must keep the interval strictly longer than the timeout --
    or have no timeout at all, where the question does not arise.
    """
    profile = resolve_connection_profile(_entry(**{CONF_CONNECTION_MODE: mode}))
    if profile.idle_disconnect_delay is None:
        return
    assert profile.check_in_interval.total_seconds() > profile.idle_disconnect_delay


def test_an_unrecognised_stored_mode_falls_back_to_always_connected():
    """A downgrade, or a hand-edited entry, must not leave the lamp unmanaged."""
    profile = resolve_connection_profile(_entry(**{CONF_CONNECTION_MODE: "sometimes"}))
    assert profile == resolve_connection_profile(_entry())
