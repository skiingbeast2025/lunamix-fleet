"""
hbu_captcha_solver.py

Multi-tier solver for 6-character SVG verification challenges (HBU2 Challenge).
Architecture:
  - Tier 1: Instant SVG DOM / XML text node extraction (fast, zero dependencies).
  - Tier 2: Multimodal Gemini Vision fallback (AI-powered character recognition).
  - Tier 3: Human interactive terminal fallback (opens challenge in browser).
"""

import base64
import json
import os
import re
import string
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
import xml.etree.ElementTree as ET
from typing import Optional

# Valid character set expected in the 6-character challenge
VALID_ALPHABET = string.ascii_uppercase + string.digits


def decode_base64_svg(b64_or_svg: str) -> str:
    """
    Decodes a base64-encoded SVG string (with or without data URI prefix).
    If already raw SVG XML, returns it as-is.
    """
    if "<svg" in b64_or_svg:
        return b64_or_svg

    # Strip data URL prefix if present (e.g. data:image/svg+xml;base64,...)
    if "," in b64_or_svg and "base64" in b64_or_svg:
        b64_or_svg = b64_or_svg.split(",", 1)[1]

    # Clean whitespace and newlines
    b64_clean = re.sub(r"\s+", "", b64_or_svg)

    try:
        decoded_bytes = base64.b64decode(b64_clean)
        return decoded_bytes.decode("utf-8", errors="replace")
    except Exception as e:
        print(f"[!] Warning: Base64 decode failed ({e}), returning raw string.")
        return b64_or_svg


def extract_text_tier1(svg: str) -> Optional[str]:
    """
    Tier 1: Instant DOM text extraction.
    Parses the SVG XML to extract text elements (<text>, <tspan>),
    orders them by horizontal (x) coordinate if available, and verifies
    whether exactly 6 valid uppercase alphanumeric characters are found.
    """
    # Strategy A: XML Parsing with position awareness
    try:
        # Strip potential XML namespaces to simplify element matching
        svg_clean = re.sub(r' xmlns="[^"]+"', "", svg)
        root = ET.fromstring(svg_clean)

        extracted_items = []
        for elem in root.iter():
            tag = elem.tag.split("}")[-1]  # remove any remaining namespace
            if tag in ("text", "tspan") and elem.text:
                text_val = elem.text.strip().upper()
                if not text_val:
                    continue

                # Parse x coordinate if present for ordering
                x_val = elem.attrib.get("x")
                try:
                    x_coord = float(re.findall(r"[-+]?\d*\.?\d+", x_val)[0]) if x_val else None
                except Exception:
                    x_coord = None

                for char in text_val:
                    if char in VALID_ALPHABET:
                        extracted_items.append((x_coord, char))

        if extracted_items:
            # Sort by x coordinate if all or most have coordinates
            if all(item[0] is not None for item in extracted_items):
                extracted_items.sort(key=lambda item: item[0])

            candidate = "".join(item[1] for item in extracted_items)
            if len(candidate) == 6:
                return candidate
    except Exception:
        pass

    # Strategy B: Regex extraction for <text> and <tspan> inner text
    text_matches = re.findall(r"<(?:text|tspan)[^>]*>(.*?)</(?:text|tspan)>", svg, re.DOTALL | re.IGNORECASE)
    if text_matches:
        candidate_chars = []
        for match in text_matches:
            # Remove any nested tags
            clean_text = re.sub(r"<[^>]+>", "", match).strip().upper()
            for char in clean_text:
                if char in VALID_ALPHABET:
                    candidate_chars.append(char)

        candidate = "".join(candidate_chars)
        if len(candidate) == 6:
            return candidate

    return None


def solve_with_gemini_tier2(svg: str) -> Optional[str]:
    """
    Tier 2: Gemini Vision fallback.
    Sends the SVG markup or rendered representation to Gemini API to extract
    the 6-character captcha text. Requires GEMINI_API_KEY environment variable.
    """
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        return None

    prompt = (
        "You are an automated verification solver. Below is the SVG markup for a 6-character "
        "CAPTCHA verification challenge. Identify the 6 alphanumeric characters displayed in the image. "
        "Return ONLY the 6 characters in uppercase with no spaces, punctuation, or explanations."
    )

    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent?key={api_key}"
    payload = {
        "contents": [
            {
                "parts": [
                    {"text": prompt},
                    {"text": f"```xml\n{svg}\n```"}
                ]
            }
        ],
        "generationConfig": {
            "temperature": 0.0,
            "maxOutputTokens": 32
        }
    }

    try:
        req_data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=req_data,
            headers={"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode())
            candidates = data.get("candidates", [])
            if candidates:
                text = candidates[0].get("content", {}).get("parts", [{}])[0].get("text", "")
                # Find any 6-character uppercase alphanumeric string in output
                match = re.search(r"\b([A-Z0-9]{6})\b", text.strip().upper())
                if match:
                    return match.group(1)
    except Exception as e:
        print(f"[!] Tier 2 (Gemini Vision) request error: {e}")

    return None


def solve_with_human_tier3(svg_content: str) -> Optional[str]:
    """
    Tier 3 Interactive Fallback:
    Opens SVG in browser and prompts user in terminal.
    """
    html = f"""<!DOCTYPE html>
<html>
<head><title>HBU2 Verification Challenge</title></head>
<body style="font-family:sans-serif; text-align:center; padding:50px; background:#121212; color:#fff;">
  <h2>HBU2 Verification Challenge</h2>
  <div style="background:#fff; padding:20px; display:inline-block; border-radius:8px;">
    {svg_content}
  </div>
  <p style="color:#aaa; margin-top:20px;">Enter the 6 characters in your terminal prompt.</p>
</body>
</html>"""
    with tempfile.NamedTemporaryFile("w", suffix=".html", delete=False) as f:
        f.write(html)
        tmp_path = f.name
    webbrowser.open(f"file://{tmp_path}")
    print(f"\n[!] Verification challenge opened in your browser ({tmp_path}).")
    try:
        solution = input("Enter the 6 characters from the image: ").strip().upper()
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        if len(solution) == 6 and all(c in VALID_ALPHABET for c in solution):
            return solution
    except (KeyboardInterrupt, EOFError):
        pass
    return None


def solve_challenge(b64_or_svg: str, preferred_mode: str = "auto") -> Optional[str]:
    """
    Master Solver Function:
    Resolves the challenge using Tier 1 -> Tier 2 -> Tier 3.
    """
    svg = decode_base64_svg(b64_or_svg) if ("<svg" not in b64_or_svg) else b64_or_svg

    # Tier 1: Instant DOM text extraction
    t0 = time.perf_counter()
    res1 = extract_text_tier1(svg)
    if res1:
        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        print(f"[*] Tier 1 DOM extraction succeeded in {elapsed_ms:.2f}ms: '{res1}'")
        return res1

    # Tier 2: Gemini Vision fallback
    if preferred_mode != "manual":
        print("[*] Tier 1 extraction failed or incomplete. Attempting Tier 2 (Gemini Vision)...")
        res2 = solve_with_gemini_tier2(svg)
        if res2:
            print(f"[*] Tier 2 (Gemini Vision) succeeded: '{res2}'")
            return res2

    # Tier 3: Human interactive fallback
    print("[*] Falling back to Tier 3 (Interactive Human Prompt)...")
    return solve_with_human_tier3(svg)


if __name__ == "__main__":
    print("[*] Testing hbu_captcha_solver.py against live HBU2 challenge...")
    url = "https://xvkjvuxhawlljakfkllu.supabase.co/functions/v1/send-message?action=image_challenge"
    headers = {
        "apikey": "sb_publishable_uyxd5lS77wdXpD56QF_lgg_zaujLjZZ",
        "Authorization": "Bearer sb_publishable_uyxd5lS77wdXpD56QF_lgg_zaujLjZZ",
        "Origin": "https://rockystudios.github.io"
    }
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            body = json.loads(r.read().decode())
            cid = body.get("id", "unknown")
            b64 = body.get("image", "")
            ans = solve_challenge(b64)
            print(f"✓ Solved Challenge ID [{cid[:30]}...] -> Answer: '{ans}'")
    except Exception as err:
        print(f"[!] Could not connect to live endpoint ({err}). Running synthetic test...")
        # Synthetic test with embedded SVG
        synthetic_svg = (
            '<svg xmlns="http://www.w3.org/2000/svg" width="150" height="50">'
            '<text x="10" y="35">A</text><text x="30" y="35">B</text>'
            '<text x="50" y="35">C</text><text x="70" y="35">1</text>'
            '<text x="90" y="35">2</text><text x="110" y="35">3</text>'
            '</svg>'
        )
        ans = solve_challenge(synthetic_svg)
        print(f"✓ Synthetic Test Result: '{ans}'")