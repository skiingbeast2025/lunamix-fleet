#!/usr/bin/env python3
"""
hbu_auth_client.py - Official HBU2 Challenge Protocol & HTTP Client

Handles challenge acquisition and account registration through the official
Supabase Edge Function endpoint, with isolated proxy routing, SOCKS5h remote
DNS resolution, guaranteed session cleanup, and explicit error classification.
"""

from typing import Any

import requests

BASE_URL = "https://xvkjvuxhawlljakfkllu.supabase.co/functions/v1/send-message"
ANON_KEY = "sb_publishable_uyxd5lS77wdXpD56QF_lgg_zaujLjZZ"

DEFAULT_HEADERS = {
    "apikey": ANON_KEY,
    "Authorization": f"Bearer {ANON_KEY}",
    "Origin": "https://rockystudios.github.io",
    "Referer": "https://rockystudios.github.io/HBU2/",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
}

NETWORK_EXCEPTIONS = (
    requests.exceptions.ProxyError,
    requests.exceptions.ConnectTimeout,
    requests.exceptions.ReadTimeout,
    requests.exceptions.SSLError,
    requests.exceptions.ConnectionError,
    requests.exceptions.Timeout,
    requests.exceptions.ChunkedEncodingError,
    requests.exceptions.InvalidURL,
    requests.exceptions.InvalidSchema,
)


class ProxyDeadException(Exception):
    """Raised when a proxy or network transport failure prevents reaching the endpoint."""

    def __init__(self, message: str, proxy_url: str | None = None) -> None:
        super().__init__(message)
        self.proxy_url = proxy_url


class RateLimitException(Exception):
    """Raised when the upstream endpoint rejects a request with HTTP 429."""

    def __init__(
        self,
        message: str,
        status_code: int = 429,
        retry_after: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.retry_after = retry_after


def _format_proxies(proxy_url: str | None) -> dict[str, str] | None:
    """Normalize a proxy URL into a requests proxies dict using socks5h:// for remote DNS."""
    if not proxy_url or not isinstance(proxy_url, str):
        return None
    url = proxy_url.strip()
    if not url:
        return None
    if "://" not in url:
        url = f"http://{url}"
    # Rewrite socks5:// -> socks5h:// so DNS resolution occurs on the proxy side.
    if url.lower().startswith("socks5://"):
        url = "socks5h://" + url[len("socks5://"):]
    return {"http": url, "https": url}


def _build_session(proxy_url: str | None = None) -> requests.Session:
    """Build an isolated requests.Session with TLS verification, headers, and proxy config."""
    session = requests.Session()
    session.trust_env = False
    session.verify = True
    session.headers.update(DEFAULT_HEADERS)
    proxies = _format_proxies(proxy_url)
    if proxies:
        session.proxies.update(proxies)
    return session


def _perform_request(
    method: str,
    url: str,
    proxy_url: str | None = None,
    timeout: float = 8.0,
    **kwargs: Any,
) -> requests.Response:
    """Execute an HTTP request in an auto-closed session, translating transport errors."""
    proxy_desc = proxy_url or "direct"
    with _build_session(proxy_url) as session:
        try:
            return session.request(method, url, timeout=timeout, **kwargs)
        except NETWORK_EXCEPTIONS as exc:
            raise ProxyDeadException(
                f"Proxy/network failure ({proxy_desc}): {exc}",
                proxy_url=proxy_url,
            ) from exc


def fetch_image_challenge(proxy_url: str | None = None, timeout: float = 8.0) -> dict:
    """
    Fetch a new image challenge { id: string, image: base64_svg }
    through the given proxy (or direct connection).
    """
    url = f"{BASE_URL}?action=image_challenge"
    resp = _perform_request("GET", url, proxy_url=proxy_url, timeout=timeout)

    if resp.status_code == 200:
        try:
            data = resp.json()
        except ValueError as exc:
            raise ValueError("Invalid JSON in challenge response") from exc
        if isinstance(data, dict) and data.get("id") and data.get("image"):
            return data
        raise ValueError("Invalid challenge response payload")

    if resp.status_code == 429:
        raise RateLimitException(
            f"IP Rate Limited (429): {resp.text}",
            retry_after=resp.headers.get("Retry-After"),
        )

    raise requests.exceptions.HTTPError(
        f"HTTP {resp.status_code}: {resp.text}",
        response=resp,
    )


def submit_signup(
    username: str,
    password: str,
    challenge_id: str,
    challenge_answer: str,
    student_id: str | None = None,
    proxy_url: str | None = None,
    timeout: float = 12.0,
) -> dict:
    """
    Submit a registration payload to the send-message Edge Function.
    Returns: { "ok": bool, "status": int, "data": dict | None, "error": str | None }
    """
    payload: dict[str, str] = {
        "action": "signup_image",
        "username": username.strip(),
        "password": password,
        "challenge_id": challenge_id,
        "challenge_answer": challenge_answer.strip().upper(),
    }
    if student_id and student_id.strip():
        payload["student_id"] = student_id.strip()

    resp = _perform_request(
        "POST",
        BASE_URL,
        proxy_url=proxy_url,
        timeout=timeout,
        json=payload,
        headers={"Content-Type": "application/json"},
    )

    result: dict[str, Any] = {
        "ok": resp.status_code == 200,
        "status": resp.status_code,
        "error": None,
        "data": None,
    }

    try:
        body = resp.json()
        result["data"] = body
        if not result["ok"]:
            if isinstance(body, dict):
                result["error"] = body.get("error") or body.get("message") or f"HTTP {resp.status_code}"
            else:
                result["error"] = str(body) or f"HTTP {resp.status_code}"
    except ValueError:
        if not result["ok"]:
            result["error"] = resp.text or f"HTTP {resp.status_code}"

    return result


if __name__ == "__main__":
    print("[*] Testing fetch_image_challenge directly...")
    ch = fetch_image_challenge()
    print(f"✓ Challenge retrieved: ID {ch['id'][:25]}... (Image length: {len(ch['image'])} bytes)")
