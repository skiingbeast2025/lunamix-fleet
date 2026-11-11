#!/usr/bin/env python3
"""
download_messages.py

Fetches, sanitizes, deduplicates, and aggregates conversational/chat-appropriate
text datasets from multiple public GitHub repositories into chat_messages.json.

Also exposes load_message_pool() as a shared loader for daemon_hbu_sim.py and
simulate_hbu_chat.py, and supports --verify to validate the generated corpus.
"""

from __future__ import annotations

import argparse
from collections.abc import Callable, Sequence
import concurrent.futures
import csv
from dataclasses import dataclass
from datetime import datetime, timezone
import html
import http.client
import io
import json
import os
import re
import ssl
import sys
import tempfile
import unicodedata
import urllib.error
import urllib.request

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_OUTPUT_FILE = os.path.join(BASE_DIR, "chat_messages.json")

# HBU2 exact message length bounds
MIN_MESSAGE_LEN = 1
MAX_MESSAGE_LEN = 500

# Strict corpus filter bounds (safely inside HBU2's 1..500 limit)
CORPUS_MIN_LEN = 5
CORPUS_MAX_LEN = 450
IDEAL_MIN_LEN = CORPUS_MIN_LEN
IDEAL_MAX_LEN = CORPUS_MAX_LEN

MIN_DISTINCT_REPOS = 5
DEFAULT_TIMEOUT_SEC = 20
MAX_FETCH_WORKERS = 12
USER_AGENT = "Lunamix-ChatHarvester/1.0"

# ==============================================================================
# Sanitization & Filtering Patterns
# ==============================================================================

URL_RE = re.compile(r"(?:https?://|www\.)\S+", re.IGNORECASE)
EMAIL_RE = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")
HTML_TAG_RE = re.compile(r"<[^>]+>")
MD_IMAGE_RE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
MD_LINK_RE = re.compile(r"\[([^\]]+)\]\([^)]+\)")
MD_INLINE_RE = re.compile(r"[`*_~]+")
MD_PREFIX_RE = re.compile(r"^\s*(?:[#>\-*•]+|\d+\.)\s+")
WHITESPACE_RE = re.compile(r"\s+")
REPEAT_CHAR_RE = re.compile(r"(.)\1{5,}")
MOJIBAKE_HINT_RE = re.compile(r"(?:Ã.|â\x80|â\x84|Â[\xa0-\xbf])")

# Strip Unicode emoji / pictograph blocks & variation selectors / ZWJ
EMOJI_RE = re.compile(
    "["
    "\U0001F1E0-\U0001F1FF"
    "\U0001F300-\U0001F5FF"
    "\U0001F600-\U0001F64F"
    "\U0001F680-\U0001F6FF"
    "\U0001F700-\U0001F77F"
    "\U0001F780-\U0001F7FF"
    "\U0001F800-\U0001F8FF"
    "\U0001F900-\U0001F9FF"
    "\U0001FA00-\U0001FAFF"
    "\U00002600-\U000027BF"
    "\U0000FE00-\U0000FE0F"
    "\U0000200D"
    "]+",
    flags=re.UNICODE,
)

# Offensive / spam / bot-meta / non-chat noise blocklist
BLOCKLIST_RE = re.compile(
    r"\b(?:"
    r"fuck\w*|shit\w*|bitch\w*|asshole\w*|bastard\w*|cunt\w*|dick\w*|cock\w*|pussy\w*|"
    r"whore\w*|slut\w*|fag\w*|nigger\w*|nigga\w*|retard\w*|rape\w*|porn\w*|nsfw|"
    r"suicide|kill\s+yourself|kys|"
    r"viagra|cialis|casino\s+bonus|buy\s+now|click\s+here|promo\s+code|"
    r"free\s+bitcoin|crypto\s+giveaway|subscribe\s+to\s+my|"
    r"chatterbot|aiml|richard\s+wallace|as\s+an\s+ai|language\s+model"
    r")\b",
    re.IGNORECASE,
)

NOISE_SUBSTRINGS = (
    "___",
    "[deleted]",
    "[removed]",
    "credit:",
    "http:",
    "https:",
)


def _fix_mojibake(text: str) -> str:
    """Attempt one-pass latin-1 -> utf-8 repair if classic UTF-8 mojibake appears."""
    if not MOJIBAKE_HINT_RE.search(text):
        return text
    try:
        repaired = text.encode("latin-1").decode("utf-8")
        return repaired
    except (UnicodeEncodeError, UnicodeDecodeError):
        return text


def sanitize_message(
    raw: str,
    *,
    min_len: int = IDEAL_MIN_LEN,
    max_len: int = IDEAL_MAX_LEN,
) -> str | None:
    """
    Strictly sanitize a candidate chat message.
    Returns cleaned text or None if rejected.
    """
    if not isinstance(raw, str):
        return None

    text = _fix_mojibake(raw)
    text = unicodedata.normalize("NFKC", text)
    text = html.unescape(text)

    # Strip images, links (preserving anchor label), HTML, URLs, emails
    text = MD_IMAGE_RE.sub(" ", text)
    text = MD_LINK_RE.sub(r"\1", text)
    text = HTML_TAG_RE.sub(" ", text)
    text = URL_RE.sub(" ", text)
    text = EMAIL_RE.sub(" ", text)

    # Strip emojis and control characters
    text = EMOJI_RE.sub("", text)
    cleaned_chars: list[str] = []
    for ch in text:
        if ch in ("\r", "\n", "\t"):
            cleaned_chars.append(" ")
            continue
        cat = unicodedata.category(ch)
        if cat.startswith("C"):
            continue
        cleaned_chars.append(ch)
    text = "".join(cleaned_chars)

    # Strip markdown formatting & list/heading prefixes
    text = MD_PREFIX_RE.sub("", text)
    text = MD_INLINE_RE.sub("", text)
    text = WHITESPACE_RE.sub(" ", text).strip()

    # Trim wrapping quotes only if unbalanced or redundant outer pair without attribution
    if len(text) >= 2 and text[0] == text[-1] and text[0] in ('"', "'"):
        inner = text[1:-1].strip()
        if text[0] not in inner:
            text = inner

    # Enforce hard HBU2 bounds as well as configured natural bounds
    effective_min = max(MIN_MESSAGE_LEN, min_len)
    effective_max = min(MAX_MESSAGE_LEN, max_len)
    if not (effective_min <= len(text) <= effective_max):
        return None

    lower = text.lower()
    if any(tok in lower for tok in NOISE_SUBSTRINGS):
        return None
    if BLOCKLIST_RE.search(text):
        return None
    if REPEAT_CHAR_RE.search(text):
        return None

    # Require natural readable text (avoid binary/non-English-heavy/symbol-only strings)
    letters = sum(1 for ch in text if ch.isalpha())
    ascii_chars = sum(1 for ch in text if ord(ch) < 128)
    if letters < 3 or (ascii_chars / len(text)) < 0.85:
        return None

    # Reject excessive ALL-CAPS shouting
    upper_letters = sum(1 for ch in text if ch.isupper())
    if letters > 12 and (upper_letters / letters) > 0.70:
        return None

    return text


# ==============================================================================
# Format-Specific Parsers
# ==============================================================================


def parse_chatterbot_yaml(text: str) -> list[str]:
    """Parse gunthercox/chatterbot-corpus English YAML conversation files."""
    candidates: list[str] = []
    in_conversations = False
    current: list[str] = []

    def flush_current() -> None:
        if current:
            utterance = " ".join(current).strip()
            if len(utterance) >= 2 and utterance[0] in ("'", '"') and utterance[-1] == utterance[0]:
                q = utterance[0]
                utterance = utterance[1:-1]
                if q == "'":
                    utterance = utterance.replace("''", "'")
            candidates.append(utterance)
            current.clear()

    for line in text.splitlines():
        if not in_conversations:
            if line.strip() == "conversations:":
                in_conversations = True
            continue
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if line.startswith("- - "):
            flush_current()
            current.append(line[4:].strip())
        elif line.startswith("  - "):
            flush_current()
            current.append(line[4:].strip())
        elif line.startswith("    ") and current:
            current.append(line.strip())

    flush_current()
    return candidates


def parse_topical_chat(text: str) -> list[str]:
    """Parse alexa/Topical-Chat valid_freq.json conversation turns."""
    data = json.loads(text)
    candidates: list[str] = []
    if not isinstance(data, dict):
        return candidates
    for conv in data.values():
        if not isinstance(conv, dict):
            continue
        for turn in conv.get("content", []):
            if isinstance(turn, dict):
                msg = turn.get("message")
                if isinstance(msg, str):
                    candidates.append(msg)
    return candidates


def parse_icebreakers_md(text: str) -> list[str]:
    """Parse rendall/icebreakers QUESTIONS.md indented question bullets."""
    candidates: list[str] = []
    for line in text.splitlines():
        if line.startswith("  * "):
            candidates.append(line[4:].strip())
    return candidates


def parse_official_jokes_json(text: str) -> list[str]:
    """Parse 15Dkatz/official_joke_api jokes/index.json setup + punchline."""
    data = json.loads(text)
    candidates: list[str] = []
    if not isinstance(data, list):
        return candidates
    for item in data:
        if not isinstance(item, dict):
            continue
        setup = str(item.get("setup", "")).strip()
        punchline = str(item.get("punchline", "")).strip()
        if setup and punchline:
            candidates.append(f"{setup} {punchline}")
    return candidates


def parse_dadjokes_txt(text: str) -> list[str]:
    """Parse yesinteractive/dadjokes controllers/jokes.txt ('<>'-delimited)."""
    candidates: list[str] = []
    for line in text.splitlines():
        line = line.strip()
        if line:
            candidates.append(line.replace("<>", " "))
    return candidates


def parse_quotes_json(text: str) -> list[str]:
    """Parse quote JSON arrays cleanly without author attribution clutter."""
    data = json.loads(text)
    candidates: list[str] = []
    if not isinstance(data, list):
        return candidates
    for item in data:
        if not isinstance(item, dict):
            continue
        quote = (
            item.get("text")
            or item.get("quoteText")
            or item.get("en")
            or item.get("quote")
            or ""
        )
        quote = str(quote).strip()
        if quote:
            candidates.append(quote)
    return candidates


def parse_csv_lines(text: str, column: int | str = 0) -> list[str]:
    """Parse CSV text cleanly, extracting the target column and skipping header rows."""
    candidates: list[str] = []
    reader = csv.reader(io.StringIO(text))
    rows = list(reader)
    if not rows:
        return candidates
    col_idx = column if isinstance(column, int) else 0
    start_row = 0
    if isinstance(column, str) and column in rows[0]:
        col_idx = rows[0].index(column)
        start_row = 1
    elif rows[0] and rows[0][col_idx].strip().lower() in ("text", "message", "quote", "question", "fact"):
        start_row = 1
    for row in rows[start_row:]:
        if 0 <= col_idx < len(row):
            cell = row[col_idx].strip()
            if cell:
                candidates.append(cell)
    return candidates


def parse_trivia_json(text: str) -> list[str]:
    """Parse itmmckernan/triviaJSON category files into conversational trivia lines."""
    data = json.loads(text)
    candidates: list[str] = []
    if not isinstance(data, list):
        return candidates
    skip_phrases = ("which of these", "which of the following", "following is", "not one of")
    for item in data:
        if not isinstance(item, dict):
            continue
        question = str(item.get("question", "")).strip()
        answer = str(item.get("answer", "")).strip()
        if not question or not answer:
            continue
        q_lower = question.lower()
        if "___" in question or any(phrase in q_lower for phrase in skip_phrases):
            continue
        if question.endswith("?"):
            candidates.append(f"Trivia: {question} Answer: {answer}")
        else:
            candidates.append(f"Trivia: {question} ({answer})")
    return candidates


def parse_proverbs_json(text: str) -> list[str]:
    """Parse dariusk/corpora proverbs.json categorized proverbs."""
    data = json.loads(text)
    candidates: list[str] = []
    if not isinstance(data, dict):
        return candidates
    for group in data.get("proverbs", []):
        if isinstance(group, dict):
            for items in group.values():
                if isinstance(items, list):
                    candidates.extend(str(p) for p in items if isinstance(p, str))
    return candidates


# ==============================================================================
# Verified Live GitHub Corpus Sources (10 Distinct Repositories)
# ==============================================================================


@dataclass(frozen=True)
class CorpusSource:
    repo: str
    category: str
    urls: tuple[str, ...]
    parser: Callable[[str], list[str]]
    max_accept: int | None = None


CORPUS_SOURCES: tuple[CorpusSource, ...] = (
    CorpusSource(
        repo="gunthercox/chatterbot-corpus",
        category="greetings_and_conversations",
        urls=(
            "https://raw.githubusercontent.com/gunthercox/chatterbot-corpus/master/chatterbot_corpus/data/english/greetings.yml",
            "https://raw.githubusercontent.com/gunthercox/chatterbot-corpus/master/chatterbot_corpus/data/english/conversations.yml",
            "https://raw.githubusercontent.com/gunthercox/chatterbot-corpus/master/chatterbot_corpus/data/english/humor.yml",
            "https://raw.githubusercontent.com/gunthercox/chatterbot-corpus/master/chatterbot_corpus/data/english/trivia.yml",
            "https://raw.githubusercontent.com/gunthercox/chatterbot-corpus/master/chatterbot_corpus/data/english/computers.yml",
            "https://raw.githubusercontent.com/gunthercox/chatterbot-corpus/master/chatterbot_corpus/data/english/movies.yml",
            "https://raw.githubusercontent.com/gunthercox/chatterbot-corpus/master/chatterbot_corpus/data/english/food.yml",
            "https://raw.githubusercontent.com/gunthercox/chatterbot-corpus/master/chatterbot_corpus/data/english/science.yml",
            "https://raw.githubusercontent.com/gunthercox/chatterbot-corpus/master/chatterbot_corpus/data/english/sports.yml",
        ),
        parser=parse_chatterbot_yaml,
    ),
    CorpusSource(
        repo="alexa/Topical-Chat",
        category="casual_chat_and_fun_facts",
        urls=(
            "https://raw.githubusercontent.com/alexa/Topical-Chat/master/conversations/valid_freq.json",
        ),
        parser=parse_topical_chat,
        max_accept=3500,
    ),
    CorpusSource(
        repo="rendall/icebreakers",
        category="icebreakers_and_banter",
        urls=(
            "https://raw.githubusercontent.com/rendall/icebreakers/master/QUESTIONS.md",
        ),
        parser=parse_icebreakers_md,
    ),
    CorpusSource(
        repo="15Dkatz/official_joke_api",
        category="jokes",
        urls=(
            "https://raw.githubusercontent.com/15Dkatz/official_joke_api/master/jokes/index.json",
        ),
        parser=parse_official_jokes_json,
    ),
    CorpusSource(
        repo="yesinteractive/dadjokes",
        category="dad_jokes",
        urls=(
            "https://raw.githubusercontent.com/yesinteractive/dadjokes/master/controllers/jokes.txt",
        ),
        parser=parse_dadjokes_txt,
    ),
    CorpusSource(
        repo="skolakoda/programming-quotes-api",
        category="tech_quotes",
        urls=(
            "https://raw.githubusercontent.com/skolakoda/programming-quotes-api/master/data/quotes.json",
        ),
        parser=parse_quotes_json,
    ),
    CorpusSource(
        repo="dwyl/quotes",
        category="friendly_quotes",
        urls=(
            "https://raw.githubusercontent.com/dwyl/quotes/main/quotes.json",
        ),
        parser=parse_quotes_json,
    ),
    CorpusSource(
        repo="JamesFT/Database-Quotes-JSON",
        category="quotes",
        urls=(
            "https://raw.githubusercontent.com/JamesFT/Database-Quotes-JSON/master/quotes.json",
        ),
        parser=parse_quotes_json,
        max_accept=2500,
    ),
    CorpusSource(
        repo="itmmckernan/triviaJSON",
        category="trivia",
        urls=(
            "https://raw.githubusercontent.com/itmmckernan/triviaJSON/master/video-games.json",
            "https://raw.githubusercontent.com/itmmckernan/triviaJSON/master/general.json",
            "https://raw.githubusercontent.com/itmmckernan/triviaJSON/master/science-technology.json",
            "https://raw.githubusercontent.com/itmmckernan/triviaJSON/master/hobbies.json",
            "https://raw.githubusercontent.com/itmmckernan/triviaJSON/master/for-kids.json",
        ),
        parser=parse_trivia_json,
        max_accept=2500,
    ),
    CorpusSource(
        repo="dariusk/corpora",
        category="proverbs",
        urls=(
            "https://raw.githubusercontent.com/dariusk/corpora/master/data/words/proverbs.json",
        ),
        parser=parse_proverbs_json,
    ),
)


# ==============================================================================
# Networking, Atomic Persistence & Shared Loader
# ==============================================================================


def _fetch_url(url: str, timeout: int = DEFAULT_TIMEOUT_SEC) -> str:
    """Fetch UTF-8 text from a live HTTP(S) URL, raising on non-200 or network error."""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    ctx = ssl.create_default_context()
    with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
        status = getattr(resp, "status", resp.getcode())
        if status != 200:
            raise RuntimeError(f"Unexpected HTTP {status} for {url}")
        raw_bytes = resp.read()
    return raw_bytes.decode("utf-8", errors="replace")


def atomic_write_json(path: str, payload: dict) -> None:
    """Write JSON payload atomically via a same-directory temp file + os.replace."""
    target_dir = os.path.dirname(os.path.abspath(path))
    os.makedirs(target_dir, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(prefix=".chat_messages_", suffix=".tmp", dir=target_dir)
    file_obj = None
    try:
        file_obj = os.fdopen(fd, "w", encoding="utf-8")
        with file_obj as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_path, path)
    except Exception:
        if file_obj is None:
            try:
                os.close(fd)
            except OSError:
                pass
        if os.path.exists(tmp_path):
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
        raise


def load_message_pool(
    fallback_messages: Sequence[str] | None = None,
    path: str = DEFAULT_OUTPUT_FILE,
) -> list[str]:
    """
    Shared helper for daemon_hbu_sim.py and simulate_hbu_chat.py.
    Loads sanitized messages from chat_messages.json enforcing HBU2's 1..500 bounds,
    falling back safely to `fallback_messages` if the corpus file is absent or unreadable.
    """
    fallback_list = list(fallback_messages) if fallback_messages is not None else []
    if not os.path.exists(path):
        return fallback_list

    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError, UnicodeDecodeError, ValueError):
        return fallback_list

    raw_list = data.get("messages") if isinstance(data, dict) else data
    if not isinstance(raw_list, list):
        return fallback_list

    valid = [
        msg
        for msg in raw_list
        if isinstance(msg, str) and msg.strip() and MIN_MESSAGE_LEN <= len(msg) <= MAX_MESSAGE_LEN
    ]
    return valid if valid else fallback_list


def load_chat_messages(
    fallback: Sequence[str] | None = None,
    path: str = DEFAULT_OUTPUT_FILE,
) -> list[str]:
    """Backward-compatible alias for load_message_pool()."""
    return load_message_pool(fallback_messages=fallback, path=path)


def _fetch_and_parse_url(source: CorpusSource, url: str) -> tuple[list[str], str | None]:
    """Fetch a single URL and parse raw candidate messages, returning (candidates, error)."""
    try:
        content = _fetch_url(url)
        return source.parser(content), None
    except (
        urllib.error.URLError,
        urllib.error.HTTPError,
        http.client.HTTPException,
        json.JSONDecodeError,
        UnicodeError,
        ValueError,
        OSError,
        RuntimeError,
    ) as exc:
        return [], f"{url}: {exc}"


def harvest_corpus(output_path: str = DEFAULT_OUTPUT_FILE) -> dict:
    """Fetch all configured GitHub repositories concurrently, sanitize, deduplicate, and save atomically."""
    tasks: list[tuple[int, CorpusSource, str]] = [
        (src_idx, source, url)
        for src_idx, source in enumerate(CORPUS_SOURCES)
        for url in source.urls
    ]

    # Fetch and parse all URLs concurrently while preserving deterministic source order for dedup.
    results_by_url: dict[tuple[int, str], tuple[list[str], str | None]] = {}
    worker_count = max(1, min(MAX_FETCH_WORKERS, len(tasks)))
    with concurrent.futures.ThreadPoolExecutor(max_workers=worker_count) as executor:
        future_map = {
            executor.submit(_fetch_and_parse_url, source, url): (src_idx, url)
            for src_idx, source, url in tasks
        }
        for future in concurrent.futures.as_completed(future_map):
            key = future_map[future]
            try:
                results_by_url[key] = future.result()
            except Exception as exc:
                results_by_url[key] = ([], f"{key[1]}: {exc}")

    seen_lower: set[str] = set()
    all_messages: list[str] = []
    source_metadata: list[dict] = []

    for src_idx, source in enumerate(CORPUS_SOURCES):
        raw_candidates: list[str] = []
        ok_urls: list[str] = []
        errors: list[str] = []

        for url in source.urls:
            parsed, err = results_by_url[(src_idx, url)]
            if err is not None:
                errors.append(err)
            else:
                raw_candidates.extend(parsed)
                ok_urls.append(url)

        accepted_for_repo = 0
        for candidate in raw_candidates:
            if source.max_accept is not None and accepted_for_repo >= source.max_accept:
                break
            cleaned = sanitize_message(candidate)
            if cleaned is None:
                continue
            key = cleaned.lower()
            if key in seen_lower:
                continue
            seen_lower.add(key)
            all_messages.append(cleaned)
            accepted_for_repo += 1

        entry: dict = {
            "repo": source.repo,
            "category": source.category,
            "urls": ok_urls,
            "raw_candidates": len(raw_candidates),
            "accepted_count": accepted_for_repo,
        }
        if errors:
            entry["errors"] = errors
        source_metadata.append(entry)

        status_note = f"{accepted_for_repo} accepted / {len(raw_candidates)} raw"
        if errors:
            status_note += f" ({len(errors)} URL errors)"
        print(f"  [{source.repo}] ({source.category}): {status_note}")

    active_repos = {s["repo"] for s in source_metadata if s["accepted_count"] > 0}
    if len(active_repos) < MIN_DISTINCT_REPOS:
        raise RuntimeError(
            f"Only {len(active_repos)} distinct GitHub repositories yielded messages; "
            f"need at least {MIN_DISTINCT_REPOS}."
        )
    if not all_messages:
        raise RuntimeError("No valid messages harvested from any source.")

    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "total_count": len(all_messages),
        "distinct_repo_count": len(active_repos),
        "hbu2_bounds": {"min": MIN_MESSAGE_LEN, "max": MAX_MESSAGE_LEN},
        "harvest_bounds": {"min": CORPUS_MIN_LEN, "max": CORPUS_MAX_LEN},
        "source_repos": source_metadata,
        "messages": all_messages,
    }
    atomic_write_json(output_path, payload)
    return payload


def verify_corpus(path: str = DEFAULT_OUTPUT_FILE) -> bool:
    """
    Verify chat_messages.json:
      - Clean JSON load & expected structure
      - >= 5 distinct GitHub source repositories with accepted messages
      - Every message strictly within 5..450 corpus bounds (inside HBU2 1..500)
      - Case-insensitive uniqueness and non-empty corpus
    """
    if not os.path.exists(path):
        print(f"[FAIL] Corpus file not found: {path}", file=sys.stderr)
        return False

    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError, UnicodeDecodeError, ValueError) as exc:
        print(f"[FAIL] Could not load JSON from {path}: {exc}", file=sys.stderr)
        return False

    if not isinstance(data, dict):
        print("[FAIL] Top-level JSON must be an object.", file=sys.stderr)
        return False

    messages = data.get("messages")
    source_repos = data.get("source_repos")
    generated_at = data.get("generated_at")
    total_count = data.get("total_count")

    if not isinstance(messages, list) or not messages:
        print("[FAIL] 'messages' must be a non-empty list.", file=sys.stderr)
        return False
    if not isinstance(source_repos, list):
        print("[FAIL] 'source_repos' metadata must be a list.", file=sys.stderr)
        return False
    if not isinstance(generated_at, str) or not generated_at.strip():
        print("[FAIL] Missing 'generated_at' timestamp metadata.", file=sys.stderr)
        return False
    if total_count != len(messages):
        print(f"[FAIL] total_count ({total_count}) != len(messages) ({len(messages)}).", file=sys.stderr)
        return False

    active_repos: set[str] = set()
    for s in source_repos:
        if not isinstance(s, dict) or not s.get("repo"):
            continue
        try:
            if int(s.get("accepted_count") or 0) > 0:
                active_repos.add(s["repo"])
        except (TypeError, ValueError):
            continue
    if len(active_repos) < MIN_DISTINCT_REPOS:
        print(
            f"[FAIL] Only {len(active_repos)} distinct active source repos; need >= {MIN_DISTINCT_REPOS}.",
            file=sys.stderr,
        )
        return False

    seen: set[str] = set()
    observed_min = MAX_MESSAGE_LEN
    observed_max = 0
    for idx, msg in enumerate(messages):
        if not isinstance(msg, str):
            print(f"[FAIL] Message at index {idx} is not a string.", file=sys.stderr)
            return False
        msg_len = len(msg)
        if not (CORPUS_MIN_LEN <= msg_len <= CORPUS_MAX_LEN):
            print(
                f"[FAIL] Message at index {idx} violates {CORPUS_MIN_LEN}..{CORPUS_MAX_LEN} bounds (len={msg_len}).",
                file=sys.stderr,
            )
            return False
        if not (MIN_MESSAGE_LEN <= msg_len <= MAX_MESSAGE_LEN):
            print(
                f"[FAIL] Message at index {idx} violates HBU2 {MIN_MESSAGE_LEN}..{MAX_MESSAGE_LEN} bounds (len={msg_len}).",
                file=sys.stderr,
            )
            return False
        observed_min = min(observed_min, msg_len)
        observed_max = max(observed_max, msg_len)
        key = msg.lower()
        if key in seen:
            print(f"[FAIL] Duplicate message (case-insensitive) at index {idx}.", file=sys.stderr)
            return False
        seen.add(key)

    loaded = load_message_pool(path=path)
    if len(loaded) != len(messages):
        print(f"[FAIL] load_message_pool() returned {len(loaded)} != {len(messages)}.", file=sys.stderr)
        return False

    print(
        f"[OK] Verified {len(messages)} unique messages from {len(active_repos)} distinct GitHub repos "
        f"(generated_at={generated_at}, bounds={CORPUS_MIN_LEN}..{CORPUS_MAX_LEN}, "
        f"observed_len={observed_min}..{observed_max})."
    )
    for src in source_repos:
        print(f"  - {src['repo']} [{src['category']}]: {src['accepted_count']} messages")
    return True


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Harvest or verify HBU2 chat message corpus.")
    parser.add_argument(
        "--output",
        default=DEFAULT_OUTPUT_FILE,
        help=f"Output JSON path (default: {DEFAULT_OUTPUT_FILE})",
    )
    parser.add_argument(
        "--verify",
        action="store_true",
        help="Generate corpus if missing, then verify repository count, breakdown, uniqueness, and bounds.",
    )
    args = parser.parse_args(argv)

    if args.verify:
        if not os.path.exists(args.output):
            print(f"Harvesting chat corpus from {len(CORPUS_SOURCES)} GitHub repositories...")
            harvest_corpus(args.output)
        return 0 if verify_corpus(args.output) else 1

    print(f"Harvesting chat corpus from {len(CORPUS_SOURCES)} GitHub repositories...")
    payload = harvest_corpus(args.output)
    print(
        f"Saved {payload['total_count']} unique sanitized messages "
        f"({payload['distinct_repo_count']} distinct repos) to {args.output}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
