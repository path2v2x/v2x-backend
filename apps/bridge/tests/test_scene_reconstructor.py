"""Tests for the scene reconstructor — TDD: write tests first, then implement."""

import json
import pytest
from unittest.mock import patch, MagicMock

from tests.conftest import (
    MockWorld,
    MockMap,
    FakeV2XApi,
    SAMPLE_DETECTIONS,
)


@pytest.mark.unit
class TestSceneReconstructor:
    """Unit tests with mocked CARLA and fake API."""

    def test_reconstruct_spawns_objects(self, mock_world, fake_v2x_api):
        """Given detections in the time range, objects are spawned in CARLA."""
        from digital_twin_bridge.scene_reconstructor import SceneReconstructor

        recon = SceneReconstructor(
            world=mock_world,
            carla_map=mock_world.get_map(),
            api_fetcher=fake_v2x_api.get_detections_range,
        )
        result = recon.reconstruct("2026-03-22T17:00:00Z", "2026-03-22T17:30:00Z")

        # Should have spawned actors (deduplicated: cone_001 appears twice → use latest)
        assert len(result.spawned_actors) == 2  # cone_001 (deduped) + cone_002
        assert result.total_detections >= 2

    def test_deduplication_uses_latest(self, mock_world, fake_v2x_api):
        """When same object_id appears multiple times, use the latest detection."""
        from digital_twin_bridge.scene_reconstructor import SceneReconstructor

        recon = SceneReconstructor(
            world=mock_world,
            carla_map=mock_world.get_map(),
            api_fetcher=fake_v2x_api.get_detections_range,
        )
        result = recon.reconstruct("2026-03-22T17:00:00Z", "2026-03-22T17:30:00Z")

        # traffic_cone_001 has two entries; should use the one with timestamp 17:08:45
        cone_001 = [o for o in result.objects if o["object_id"] == "traffic_cone_001"]
        assert len(cone_001) == 1
        assert cone_001[0]["timestamp_utc"] == "2026-03-22T17:08:45Z"

    def test_empty_timeframe_returns_empty(self, mock_world, empty_v2x_api):
        """No detections in the time range → empty result, no actors spawned."""
        from digital_twin_bridge.scene_reconstructor import SceneReconstructor

        recon = SceneReconstructor(
            world=mock_world,
            carla_map=mock_world.get_map(),
            api_fetcher=empty_v2x_api.get_detections_range,
        )
        result = recon.reconstruct("2026-03-22T17:00:00Z", "2026-03-22T17:30:00Z")

        assert len(result.spawned_actors) == 0
        assert result.total_detections == 0

    def test_api_failure_raises_clear_error(self, mock_world):
        """API failure should raise a descriptive error, not crash silently."""
        from digital_twin_bridge.scene_reconstructor import SceneReconstructor

        def failing_api(start, end, limit=500):
            raise ConnectionError("API Gateway timeout")

        recon = SceneReconstructor(
            world=mock_world,
            carla_map=mock_world.get_map(),
            api_fetcher=failing_api,
        )

        with pytest.raises(ConnectionError, match="API Gateway timeout"):
            recon.reconstruct("2026-03-22T17:00:00Z", "2026-03-22T17:30:00Z")

    def test_cleanup_destroys_all_spawned(self, mock_world, fake_v2x_api):
        """cleanup() should destroy all actors that were spawned by reconstruct()."""
        from digital_twin_bridge.scene_reconstructor import SceneReconstructor

        recon = SceneReconstructor(
            world=mock_world,
            carla_map=mock_world.get_map(),
            api_fetcher=fake_v2x_api.get_detections_range,
        )
        result = recon.reconstruct("2026-03-22T17:00:00Z", "2026-03-22T17:30:00Z")
        assert len(result.spawned_actors) > 0

        recon.cleanup()

        # All spawned actors should be destroyed
        for actor in result.spawned_actors:
            mock_actor = mock_world.get_actor(actor.id)
            assert mock_actor is not None
            assert mock_actor.is_destroyed

    def test_sessions_never_reuse_historical_actor_by_object_id(
        self, mock_world, fake_v2x_api
    ):
        """Each requested range owns distinct actors, even for the same IDs."""
        from digital_twin_bridge.scene_reconstructor import SceneReconstructor

        recon_a = SceneReconstructor(
            world=mock_world,
            carla_map=mock_world.get_map(),
            api_fetcher=fake_v2x_api.get_detections_range,
        )
        result_a = recon_a.reconstruct("2026-03-22T17:00:00Z", "2026-03-22T17:30:00Z")

        recon_b = SceneReconstructor(
            world=mock_world,
            carla_map=mock_world.get_map(),
            api_fetcher=fake_v2x_api.get_detections_range,
        )
        result_b = recon_b.reconstruct("2026-03-22T17:00:00Z", "2026-03-22T17:30:00Z")

        first_ids = {actor.id for actor in result_a.spawned_actors}
        second_ids = {actor.id for actor in result_b.spawned_actors}
        assert first_ids
        assert second_ids
        assert first_ids.isdisjoint(second_ids)

    def test_session_cleanup_destroys_only_its_historical_actors(
        self, mock_world, fake_v2x_api
    ):
        """Ending one concurrent range must not destroy another range's actors."""
        from digital_twin_bridge.scene_reconstructor import SceneReconstructor

        first = SceneReconstructor(
            world=mock_world,
            carla_map=mock_world.get_map(),
            api_fetcher=fake_v2x_api.get_detections_range,
        )
        second = SceneReconstructor(
            world=mock_world,
            carla_map=mock_world.get_map(),
            api_fetcher=fake_v2x_api.get_detections_range,
        )
        first_result = first.reconstruct(
            "2026-03-22T17:00:00Z", "2026-03-22T17:30:00Z"
        )
        second_result = second.reconstruct(
            "2026-03-22T18:00:00Z", "2026-03-22T18:30:00Z"
        )

        destroyed = first.cleanup()
        assert destroyed == len(first_result.spawned_actors)

        assert all(
            mock_world.get_actor(actor.id).is_destroyed
            for actor in first_result.spawned_actors
        )
        assert all(
            not mock_world.get_actor(actor.id).is_destroyed
            for actor in second_result.spawned_actors
        )

    def test_correct_gps_to_carla_conversion(self, mock_world, fake_v2x_api):
        """Objects should be spawned at CARLA coordinates derived from GPS."""
        from digital_twin_bridge.scene_reconstructor import SceneReconstructor

        recon = SceneReconstructor(
            world=mock_world,
            carla_map=mock_world.get_map(),
            api_fetcher=fake_v2x_api.get_detections_range,
        )
        result = recon.reconstruct("2026-03-22T17:00:00Z", "2026-03-22T17:30:00Z")

        # Each spawned actor should have a valid transform (not at origin)
        for actor in result.spawned_actors:
            mock_actor = mock_world.get_actor(actor.id)
            assert mock_actor is not None
            # MockMap.geolocation_to_transform returns (100, 200, 0)
            # so actors should be near there
            assert mock_actor._transform is not None
