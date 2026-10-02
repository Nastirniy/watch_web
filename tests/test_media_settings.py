"""Media settings accept environment-style string values for list fields."""
from app.backend.config import PosterSettings, ThumbnailSettings


def test_sizes_and_backoff_parse_from_json_strings():
    thumbs = ThumbnailSettings(small_size="[100, 120]", preview_size=[2000, 2000])
    assert thumbs.small_size == (100, 120) and thumbs.preview_size == (2000, 2000)
    assert thumbs.size_for("small") == (100, 120) and thumbs.size_for("unknown") == thumbs.medium_size

    poster = PosterSettings(backoff_seconds="[10, 20]", max_attempts=0, frame_position=2)
    assert poster.backoff_seconds == [10, 20]
    assert poster.max_attempts == 1 and poster.frame_position == 1.0
