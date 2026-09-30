"""A person's answer is often "I restarted the container".

The sessions close while a job is parked and open again on the answer. If
the container is still coming back, the first open dies at once. That used
to fail the whole job; the runner now keeps trying for a while, and says
why when it gives up.
"""

from odoo_migrator.runner.sessions import open_sides_patiently
from odoo_migrator.sheller.errors import ShellerSessionDead
from odoo_migrator.sheller.fake import FakeShellerClient


class Restarting(FakeShellerClient):
    """A container that is still starting for its first `fails` opens."""

    def __init__(self, fails: int):
        super().__init__()
        self.fails = fails

    def open_session(self, **kwargs):
        if self.fails > 0:
            self.fails -= 1
            raise ShellerSessionDead("session_dead")

        return super().open_session(**kwargs)


class Ctx:
    def __init__(self, sheller):
        self.sheller = sheller
        self.sessions = {}
        self.write_keys = {}
        self.session_targets = {
            side: {"container": name, "database": "acme", "odoo_bin": "/opt/odoo/odoo-bin",
                   "owner": {"kind": "agent"}}
            for side, name in (("old", "odoo-old"), ("new", "odoo-new"))
        }


class Clock:
    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


def test_a_container_that_comes_back_in_time_is_waited_for():
    clock = Clock()
    ctx = Ctx(Restarting(fails=3))

    failure = open_sides_patiently(ctx, wait=120, poll=5, clock=clock)

    assert failure is None
    assert ctx.sessions["old"] and ctx.sessions["new"]
    assert clock.now == 15


def test_one_that_does_not_is_named_not_raised():
    clock = Clock()
    ctx = Ctx(Restarting(fails=10_000))

    failure = open_sides_patiently(ctx, wait=60, poll=5, clock=clock)

    assert "session_dead" in failure
    assert clock.now >= 60


def test_the_module_state_reset_works_on_every_version():
    """button_install_cancel and its siblings are gone from 19.0; the reset
    that replaced them is button_reset_state."""
    from odoo_migrator.runner.steps.upgrade import _RESET

    assert "button_reset_state" in _RESET
    assert "hasattr" in _RESET
