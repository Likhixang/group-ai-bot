"""Tests for the Codex Reset API monitor and /reset formatting."""
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


@pytest.fixture()
def reset_db(tmp_path, monkeypatch):
    db_path = tmp_path / "memory.db"
    monkeypatch.setattr(bot, "MEMORY_DB_PATH", str(db_path))
    bot._init_memory_db()
    return db_path


def api_reset(reset_id="1234567890", announced_at="2026-09-22T04:00:00Z"):
    return bot._api_reset_to_item(
        {
            "id": reset_id,
            "reset_type": "regular",
            "announced_at": announced_at,
            "text": "Reset all propagated.",
            "source": {
                "type": "x_post",
                "url": f"https://x.com/thsottiaux/status/{reset_id}",
            },
        }
    )


def test_api_reset_item_uses_canonical_x_id():
    item = api_reset()
    assert item["source"] == "api"
    assert item["source_event_id"] == "1234567890"
    assert item["event_key"] == "tweet:1234567890"
    assert item["link"].endswith("/1234567890")


def test_first_api_poll_baselines_without_alert(reset_db):
    item = api_reset()
    assert bot._record_codex_observation(item, 100, baseline=True) is False
    assert bot._load_codex_state("api_only_initialized") is None


def test_new_api_reset_is_returned_once(reset_db):
    item = api_reset()
    assert bot._record_codex_observation(item, 100, baseline=False) is True
    bot._mark_codex_event_notified(item["event_key"], 123, now=101)
    assert bot._record_codex_observation(item, 200, baseline=False) is False


def test_alert_record_prevents_repeat_after_observation_write_issue(reset_db):
    item = api_reset()
    bot._save_codex_reset_alert(item["event_key"], 123, 999, None)
    assert bot._record_codex_observation(item, 100, baseline=False) is False


def test_new_api_reset_id_is_not_suppressed_by_previous_event(reset_db):
    first = api_reset("1234567890", "2026-09-22T04:00:00Z")
    second = api_reset("9876543210", "2026-09-23T04:00:00Z")
    assert bot._record_codex_observation(first, 100, baseline=False) is True
    bot._mark_codex_event_notified(first["event_key"], 123, now=101)
    assert bot._record_codex_observation(second, 200, baseline=False) is True


def test_reset_command_uses_api_schedule_and_twitter_link(reset_db):
    status = {
        "data": {
            "latest_reset": {
                "id": "1234567890",
                "reset_type": "regular",
                "announced_at": "2026-09-12T08:09:17Z",
                "text": "Reset all propagated.",
                "source": {
                    "type": "x_post",
                    "url": "https://x.com/thsottiaux/status/1234567890",
                },
            },
            "scheduled_reset": {
                "id": "2222222222",
                "status": "scheduled",
                "reset_type": "regular",
                "announced_at": "2026-09-22T04:31:32Z",
                "scheduled_for": "2026-09-23T07:00:00Z",
                "text": "I promised a reset for Tuesday.",
                "source": {
                    "type": "x_post",
                    "url": "https://x.com/thsottiaux/status/2222222222",
                },
            },
        }
    }
    text = bot._format_reset_command(status)
    assert "上次重置：2026-09-12 16:09" in text
    assert "下次预计重置：2026-09-23 15:00" in text
    assert "https://x.com/thsottiaux/status/2222222222" in text


@pytest.mark.asyncio
async def test_codex_reset_translation_uses_luna(monkeypatch):
    calls = {}

    async def fake_ask(messages, model_name, temperature=0.2):
        calls["model"] = model_name
        calls["temperature"] = temperature
        calls["messages"] = messages
        return "GPT-6 Sol 和 Luna 已发布。"

    monkeypatch.setattr(bot, "_ask_ai_once", fake_ask)
    translated = await bot._translate_codex_reset_text(
        "GPT-6 Sol and Luna are out. https://t.co/example"
    )
    assert translated == "GPT-6 Sol 和 Luna 已发布。"
    assert calls["model"] == "gpt-5.6-luna"
    assert calls["temperature"] == 0.1
    assert calls["messages"][1]["content"].endswith("https://t.co/example")


@pytest.mark.asyncio
async def test_codex_reset_translation_falls_back_to_original(monkeypatch):
    async def failing_ask(*args, **kwargs):
        raise RuntimeError("translation unavailable")

    monkeypatch.setattr(bot, "_ask_ai_once", failing_ask)
    original = "Reset all propagated. https://x.com/example/status/123"
    assert await bot._translate_codex_reset_text(original) == original