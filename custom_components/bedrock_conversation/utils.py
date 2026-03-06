"""Utility functions for the Bedrock Conversation integration.

Provides color matching utilities for converting RGB tuples to CSS3 color names,
used when reporting light color attributes in the system prompt.
"""
import webcolors

# Pre-compute CSS3 color name to RGB mapping once at module load
_CSS3_COLORS_RGB: dict[str, tuple[int, int, int]] = {}
for _name in webcolors.names('css3'):
    _hex = webcolors.name_to_hex(_name, 'css3')
    _CSS3_COLORS_RGB[_name] = (
        int(_hex[1:3], 16),
        int(_hex[3:5], 16),
        int(_hex[5:7], 16),
    )


def closest_color(rgb_tuple: tuple[int, int, int]) -> str:
    """Find the closest CSS3 color name for a given RGB tuple."""
    r, g, b = rgb_tuple
    best_name = "white"
    best_dist = float("inf")
    for name, (r_c, g_c, b_c) in _CSS3_COLORS_RGB.items():
        dist = (r_c - r) ** 2 + (g_c - g) ** 2 + (b_c - b) ** 2
        if dist < best_dist:
            best_dist = dist
            best_name = name
            if dist == 0:
                break
    return best_name
