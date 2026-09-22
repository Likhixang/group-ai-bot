"""Tests for Codex reset RSS parsing, baseline creation, and deduplication."""
import importlib.util
import os
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

SPEC = importlib.util.spec_from_file_location(
    "bot_codex_reset_test",
    os.path.join(os.path.dirname(__file__), "..", "bot.py"),
)
assert SPEC is not None and SPEC.loader is not None
bot = importlib.util.module_from_spec(SPEC)
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "12345:TEST")
os.environ.setdefault("AI_API_KEY", "test")
os.environ.setdefault("MEMORY_DB_PATH", ":memory:")
SPEC.loader.exec_module(bot)


def feed_xml(items: str) -> bytes:
    return f'''<?xml version="1.0"?>
<rss version="2.0"><channel>{items}</channel></rss>'''.encode()


ANNOUNCEMENT = '''
<item>
  <title>[ANNOUNCEMENT] Codex Reset Tracker</title>
  <link>https://x.com/example/status/1</link>
  <guid>tweet-1</guid>
  <pubDate>Tue, 22 Sep 2026 04:00:00 GMT</pubDate>
  <description>Reset will land today</description>
</item>'''

CONFIRMED = '''
<item>
  <title>[RESET CONFIRMED] Codex Reset Tracker</title>
  <link>https://x.com/example/status/2</link>
  <guid>tweet-2</guid>
  <pubDate>Tue, 22 Sep 2026 04:01:00 GMT</pubDate>
  <description>Reset all propagated.
Classifier Rationale: completed</description>
</item>'''


@pytest.fixture()
def reset_db(tmp_path, monkeypatch):
    db_path = tmp_path / "memory.db"
    monkeypatch.setattr(bot, "MEMORY_DB_PATH", str(db_path))
    bot._init_memory_db()
    return db_path


def test_parse_feed_and_confirmed_filter():
    items = bot._parse_codex_reset_feed(feed_xml(ANNOUNCEMENT + CONFIRMED))
    assert [item["guid"] for item in items] == ["tweet-1", "tweet-2"]
    assert bot._codex_reset_is_confirmed(items[0]) is False
    assert bot._codex_reset_is_confirmed(items[1]) is True


def test_first_poll_baselines_without_alerts(reset_db):
    items = bot._parse_codex_reset_feed(feed_xml(ANNOUNCEMENT + CONFIRMED))
    assert bot._prepare_codex_reset_candidates(items, 100) == []
    assert bot._load_codex_state("feed_initialized") == "1"
    assert bot._prepare_codex_reset_candidates(items, 200) == []


def test_new_confirmed_item_is_returned_once(reset_db):
    initial = bot._parse_codex_reset_feed(feed_xml(ANNOUNCEMENT))
    bot._prepare_codex_reset_candidates(initial, 100)
    all_items = bot._parse_codex_reset_feed(feed_xml(ANNOUNCEMENT + CONFIRMED))
    candidates = bot._prepare_codex_reset_candidates(all_items, 200)
    assert [item["guid"] for item in candidates] == ["tweet-2"]

    bot._mark_codex_reset_notified("tweet-2", 123, now=201)
    assert bot._prepare_codex_reset_candidates(all_items, 300) == []


def test_announcement_to_confirmed_transition_is_detected(reset_db):
    announcement = bot._parse_codex_reset_feed(feed_xml(ANNOUNCEMENT))
    bot._prepare_codex_reset_candidates(announcement, 100)
    transitioned = [
        {
            **announcement[0],
            "title": "[RESET CONFIRMED] Codex Reset Tracker",
            "description": "Reset all propagated.",
        }
    ]
    candidates = bot._prepare_codex_reset_candidates(transitioned, 200)
    assert [item["guid"] for item in candidates] == ["tweet-1"]
