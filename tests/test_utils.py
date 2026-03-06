"""Test the utils module."""
from custom_components.bedrock_conversation.utils import closest_color


def test_closest_color_red():
    assert closest_color((255, 0, 0)) == "red"


def test_closest_color_blue():
    assert closest_color((0, 0, 255)) == "blue"


def test_closest_color_green():
    assert closest_color((0, 128, 0)) == "green"


def test_closest_color_white():
    assert closest_color((255, 255, 255)) == "white"


def test_closest_color_black():
    assert closest_color((0, 0, 0)) == "black"


def test_closest_color_near_match():
    """A slightly off-red should still match red."""
    result = closest_color((250, 5, 5))
    assert result == "red"


def test_closest_color_exact_match_returns_immediately():
    """Exact CSS3 color match should return quickly."""
    result = closest_color((255, 255, 0))
    assert result == "yellow"
