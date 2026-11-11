#!/usr/bin/env python3
"""
hbu_challenge_ui.py - Human-Only Verification Challenge Presenter

Safely displays base64-encoded SVG verification challenges to a human operator
in their local browser via a temporary HTML preview file, collects manual
character input from the terminal, validates the response format, and always
cleans up temporary files.
"""

import base64
import binascii
import os
import tempfile
import webbrowser
from typing import Callable, Optional

CHALLENGE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
CHALLENGE_LENGTH = 6
PROMPT_TEXT = "Enter the 6 characters shown in your browser (or 'skip' to cancel): "
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


def _render_challenge_html(data_uri: str) -> str:
    """Build a minimal self-contained HTML preview page embedding the SVG data URI."""
    # Rendering the SVG inside an <img> tag keeps SVG scripts sandboxed by the browser.
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>HBU2 Human Verification Challenge</title>
  <style>
    body {{
      font-family: system-ui, -apple-system, sans-serif;
      display: flex;
      align-items: center;
      justify-content: center;
      min-height: 100vh;
      margin: 0;
      background: #111827;
      color: #f9fafb;
    }}
    .card {{
      background: #1f2937;
      padding: 2rem;
      border-radius: 12px;
      text-align: center;
      box-shadow: 0 10px 25px rgba(0, 0, 0, 0.45);
      max-width: 480px;
    }}
    img {{
      background: #ffffff;
      padding: 12px;
      border-radius: 8px;
      max-width: 100%;
      height: auto;
      display: block;
      margin: 1rem auto;
    }}
    p {{
      color: #d1d5db;
      font-size: 0.95rem;
      margin-bottom: 0;
    }}
  </style>
</head>
<body>
  <div class="card">
    <h2>HBU2 Verification Challenge</h2>
    <img src="{data_uri}" alt="HBU2 Verification Challenge">
    <p>Type the 6 characters shown above into the terminal prompt.</p>
  </div>
</body>
</html>
"""


def present_challenge_to_human(
    b64_image: str,
    input_fn: Callable[[str], str] = input,
) -> Optional[str]:
    """
    Display a verification challenge to a human in a browser window and prompt
    them to type the 6 characters via input_fn.

    Always deletes the temporary HTML preview file in a finally block.
    Returns the validated 6-character code, or None if skipped or invalid.
    """
    data_uri = build_svg_data_uri(b64_image)
    html_content = _render_challenge_html(data_uri)
    tmp_path: Optional[str] = None

    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            prefix="hbu_challenge_",
            suffix=".html",
            delete=False,
        ) as tmp_file:
            tmp_file.write(html_content)
            tmp_file.flush()
            tmp_path = tmp_file.name

        try:
            webbrowser.open(f"file://{tmp_path}")
        except Exception:
            # Headless or missing browser launchers should not prevent manual input fallback.
            pass

        try:
            raw_input = input_fn(PROMPT_TEXT)
        except (EOFError, KeyboardInterrupt):
            return None

        return validate_challenge_answer(raw_input)
    finally:
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
