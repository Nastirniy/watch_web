"""Media settings accept environment-style string values for list fields."""
from pathlib import Path

import yaml

from app.backend.config import ImgproxySettings, PosterSettings, StoryboardSettings, ThumbnailSettings, settings

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def test_the_api_knows_the_resolution_limit_of_imgproxy():
    """Above IMGPROXY_MAX_SRC_RESOLUTION imgproxy refuses every render: the API must mark such images unsupported
    with the same limit, so both containers take it from one variable"""
    default = ImgproxySettings().max_src_resolution_mp
    assert default == 250
    services = yaml.safe_load((PROJECT_ROOT / "docker-compose.yml").read_text(encoding="utf-8"))["services"]
    shared = "${IMGPROXY_MAX_SRC_RESOLUTION:-250}"
    assert services["imgproxy"]["environment"]["IMGPROXY_MAX_SRC_RESOLUTION"] == shared
    assert services["api"]["environment"]["MYSETTINGS_STORAGE__IMGPROXY__MAX_SRC_RESOLUTION_MP"] == shared

    e2e = yaml.safe_load((PROJECT_ROOT / "tests" / "docker" / "compose.test.yml").read_text(encoding="utf-8"))["services"]
    assert float(e2e["imgproxy"]["environment"]["IMGPROXY_MAX_SRC_RESOLUTION"]) == default, "the e2e API runs with the default"
    assert float(ImgproxySettings(max_src_resolution_mp="0.5").max_src_resolution_mp) == 0.5


def test_sizes_and_backoff_parse_from_json_strings():
    thumbs = ThumbnailSettings(small_size="[100, 120]", preview_size=[2000, 2000])
    assert thumbs.small_size == (100, 120) and thumbs.preview_size == (2000, 2000)
    assert thumbs.size_for("small") == (100, 120) and thumbs.size_for("unknown") == thumbs.medium_size

    poster = PosterSettings(backoff_seconds="[10, 20]", max_attempts=0, frame_position=2)
    assert poster.backoff_seconds == [10, 20]
    assert poster.max_attempts == 1 and poster.frame_position == 1.0


def test_storyboard_defaults_and_string_values():
    sb = StoryboardSettings()
    assert (sb.enabled, sb.slots, sb.min_duration_seconds, sb.max_frames, sb.min_step_seconds) == (True, 1, 60, 300, 2)
    assert (sb.tile_width, sb.columns, sb.rows, sb.jpeg_qscale, sb.threads, sb.nice) == (160, 10, 10, 5, 1, 10)
    assert (sb.timeout_seconds, sb.lease_seconds, sb.max_attempts, sb.retry_after_seconds) == (1800, 300, 3, 30)
    assert sb.backoff_seconds == [60, 300, 1800]
    assert StoryboardSettings(backoff_seconds="[1, 2]").backoff_seconds == [1, 2]
    assert isinstance(settings.storage.storyboard, StoryboardSettings)
