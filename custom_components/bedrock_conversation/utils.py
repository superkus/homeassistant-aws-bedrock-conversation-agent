"""Small helpers for describing device state in prompts."""

from __future__ import annotations

from homeassistant.util.color import COLORS


def closest_color(rgb: tuple[int, int, int]) -> str:
    """Return the name of the closest CSS colour to an RGB tuple.

    Uses Home Assistant's own colour table, so no extra dependency is needed.
    """
    r, g, b = rgb
    return min(
        COLORS,
        key=lambda name: (COLORS[name].r - r) ** 2
        + (COLORS[name].g - g) ** 2
        + (COLORS[name].b - b) ** 2,
    )
