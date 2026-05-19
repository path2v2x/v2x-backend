"""
Local conftest for dashboard tests.

Extends the shared mocks (in tests/conftest.py) with the bits the
dashboard tests need — without modifying the shared infrastructure,
so these tests stay easy to delete later.

Specifically:
- MockWorld.spawn_actor / try_spawn_actor accept the `attachment_type`
  kwarg used by real CARLA when attaching child actors.
- MockActor gains stop() and listen() no-ops (sensor lifecycle methods).
- The shared fake `carla` module gets AttachmentType.Rigid /
  .SpringArmGhost — `_attach_camera` reads them when picking the
  attachment for the active view.

All patching is reverted after each test.
"""

import sys

import pytest

from tests.conftest import MockWorld, MockActor


class _FakeAttachmentType:
    """Stand-in for `carla.AttachmentType.{Rigid, SpringArmGhost}`."""

    Rigid = "Rigid"
    SpringArmGhost = "SpringArmGhost"


@pytest.fixture(autouse=True)
def _extend_carla_mocks_for_camera_sensor():
    original_spawn = MockWorld.spawn_actor
    original_try_spawn = MockWorld.try_spawn_actor
    had_stop = hasattr(MockActor, "stop")
    had_listen = hasattr(MockActor, "listen")

    fake_carla = sys.modules.get("carla")
    had_attachment = fake_carla is not None and hasattr(fake_carla, "AttachmentType")

    def patched_spawn(self, blueprint, transform, attach_to=None, attachment_type=None):
        return original_spawn(self, blueprint, transform, attach_to=attach_to)

    def patched_try_spawn(self, blueprint, transform, attach_to=None, attachment_type=None):
        return original_try_spawn(self, blueprint, transform, attach_to=attach_to)

    MockWorld.spawn_actor = patched_spawn
    MockWorld.try_spawn_actor = patched_try_spawn

    if not had_stop:
        MockActor.stop = lambda self: None
    if not had_listen:
        MockActor.listen = lambda self, callback: None

    if fake_carla is not None and not had_attachment:
        fake_carla.AttachmentType = _FakeAttachmentType

    yield

    MockWorld.spawn_actor = original_spawn
    MockWorld.try_spawn_actor = original_try_spawn
    if not had_stop:
        delattr(MockActor, "stop")
    if not had_listen:
        delattr(MockActor, "listen")
    if fake_carla is not None and not had_attachment:
        delattr(fake_carla, "AttachmentType")
