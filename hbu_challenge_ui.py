#!/usr/bin/env python3
"""
hbu_challenge_ui.py - Terminal-Only Human Verification Challenge Presenter

Rasterizes base64-encoded SVG verification challenges into a pixel bitmap
using cairosvg + Pillow and renders them directly in the terminal as crisp
Unicode half-block art (█ ▀ ▄) so a human operator can read and enter the
code without any browser, while never parsing or extracting SVG <text> tags.
"""

import base64
import binascii
import io
from typing import Callable, List, Optional

import cairosvg
from PIL import Image

CHALLENGE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
CHALLENGE_LENGTH = 6
PROMPT_TEXT = "Enter the 6 characters shown above (or 'skip' to cancel): "
_SVG_DATA_URI_PREFIX = "data:image/svg+xml;base64,"


def build_svg_data_uri(b64_image: str) -> str:
    """
    Validate a base64 SVG payload and wrap it as a data:image/svg+xml;base64 URI.

    Never parses or extracts SVG markup; only validates base64 encoding.
    """
    if not isinstance(b64_image, str):
        raise ValueError("Challenge image must be a base64 string")

    raw = b64_image.strip()
    if raw.lower().startswith(_SVG_DATA_URI_PREFIX):
        raw = raw[len(_SVG_DATA_URI_PREFIX):]

    compact = "".join(raw.split())
    if not compact:
        raise ValueError("Challenge image payload is empty")

    try:
        base64.b64decode(compact, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("Challenge image is not valid base64") from exc

    return f"{_SVG_DATA_URI_PREFIX}{compact}"


def validate_challenge_answer(raw_answer: Optional[str]) -> Optional[str]:
    """
    Normalize and validate human-entered challenge characters.

    Returns the uppercase 6-character string when valid, or None if skipped/invalid.
    """
    if not isinstance(raw_answer, str):
        return None

    cleaned = raw_answer.strip().upper()
    if not cleaned or cleaned == "SKIP":
        return None

    if len(cleaned) != CHALLENGE_LENGTH:
        return None

    if any(ch not in CHALLENGE_ALPHABET for ch in cleaned):
        return None

    return cleaned


def render_svg_to_terminal_lines(
    b64_image: str,
    width: int = 68,
    height_px: int = 24,
) -> List[str]:
    """
    Rasterize a base64 SVG image to pixels and convert to Unicode half-block lines.

    Uses only pixel luminance values from the rendered PNG bitmap. Never parses
    SVG XML or extracts character data.
    """
    data_uri = build_svg_data_uri(b64_image)
    b64_payload = data_uri[len(_SVG_DATA_URI_PREFIX):]
    svg_bytes = base64.b64decode(b64_payload)

    png_bytes = cairosvg.svg2png(bytestring=svg_bytes, output_width=320, output_height=100)
    img = Image.open(io.BytesIO(png_bytes)).convert("L")

    target_w = max(24, int(width))
    target_h = max(8, int(height_px))
    if target_h % 2 != 0:
        target_h += 1

    resized = img.resize((target_w, target_h), Image.Resampling.LANCZOS)
    px = resized.load()

    # Compute adaptive dark-ink threshold from pixel histogram
    samples = [px[x, y] for y in range(target_h) for x in range(target_w)]
    mean_lum = sum(samples) / max(1, len(samples))
    threshold = min(170, max(85, int(mean_lum * 0.72)))

    lines: List[str] = []
    for y in range(0, target_h, 2):
        row_chars: List[str] = []
        for x in range(target_w):
            top_dark = px[x, y] < threshold
            bot_dark = (px[x, y + 1] < threshold) if (y + 1) < target_h else False
            if top_dark and bot_dark:
                row_chars.append("█")
            elif top_dark:
                row_chars.append("▀")
            elif bot_dark:
                row_chars.append("▄")
            else:
                row_chars.append(" ")
        lines.append("".join(row_chars))

    # Trim completely blank leading/trailing rows while keeping at least 4 rows
    while len(lines) > 4 and not lines[0].strip():
        lines.pop(0)
    while len(lines) > 4 and not lines[-1].strip():
        lines.pop()

    return lines


def present_challenge_to_human(
    b64_image: str,
    input_fn: Callable[[str], str] = input,
    output_fn: Optional[Callable[[str], None]] = print,
    width: int = 68,
) -> Optional[str]:
    """
    Render a verification challenge directly in the terminal and prompt the human
    operator to type the 6 characters via input_fn.

    Returns the validated 6-character code, or None if skipped or invalid.
    """
    lines = render_svg_to_terminal_lines(b64_image, width=width)

    if output_fn is not None:
        border = "-" * len(lines[0]) if lines else "-" * width
        output_fn(f"\n+{border}+")
        output_fn("| HBU2 VERIFICATION IMAGE (Terminal Render)".ljust(len(border) + 1) + "|")
        output_fn(f"+{border}+")
        for line in lines:
            output_fn(f"|{line}|")
        output_fn(f"+{border}+")

    try:
        raw_input = input_fn(PROMPT_TEXT)
    except (EOFError, KeyboardInterrupt):
        return None

    return validate_challenge_answer(raw_input)
