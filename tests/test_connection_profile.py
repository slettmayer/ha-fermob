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

from custom_components.fermob import (
    CHECK_IN_STARTUP_DELAY,
    resolve_connection_profile,
)
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


def test_the_startup_check_in_does_not_make_the_user_wait():
    """What this delay costs is blindness, not the ability to command the lamp.

    `_async_send_led` calls `ensure_connected` itself and never waits on this
    timer, so it has never gated a command. What it does gate, in
    always-connected mode, is the link being held open -- and until it is, a
    button press goes unseen and both battery entities read unavailable.

    Both directions cost something, which is why the value is a middle one.
    Firing late is a window of exactly that blindness; firing *early* is worse
    than it looks, because the check-in swallows its failures, so a Bluetooth
    stack that is not up yet consumes the single attempt in silence and the next
    is a whole interval away. There is deliberately no retry -- see the constant.

    The bound, rather than the number, is what matters: it must stay well inside
    the shortest check-in interval, or the startup tick stops being a distinct
    thing at all.
    """
    shortest = min(
        resolve_connection_profile(
            _entry(**{CONF_CONNECTION_MODE: mode})
        ).check_in_interval
        for mode in (CONNECTION_MODE_ALWAYS, CONNECTION_MODE_ON_DEMAND)
    )

    # Two-sided on purpose. An upper bound alone is satisfied by *any* shorter
    # value, including the 30 s that was tried and reverted -- so the assertion
    # meant to stop the delay creeping up would have waved the regression
    # through.
    assert timedelta(seconds=45) <= CHECK_IN_STARTUP_DELAY <= timedelta(minutes=1)
    assert shortest / 10 > CHECK_IN_STARTUP_DELAY
