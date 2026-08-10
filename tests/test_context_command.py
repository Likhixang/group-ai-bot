import asyncio
import importlib
import os
import sys
from types import SimpleNamespace

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")
os.environ.setdefault("AI_API_KEY", "test-key")
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
bot = importlib.import_module("bot")


def _private_update(text="/context gpt-5.6-luna"):
    user = SimpleNamespace(id=bot.SUPER_ADMIN_ID)
    msg = SimpleNamespace(
        text=text,
        message_id=100,
        chat_id=200,
        from_user=user,
    )
    chat = SimpleNamespace(type=bot.ChatType.PRIVATE, id=200)
    return SimpleNamespace(effective_message=msg, effective_chat=chat)


def test_find_model_context_accepts_unqualified_model_id():
    models = {
        "openai/gpt-5.6-luna": {
            "id": "openai/gpt-5.6-luna",
            "limit": {"context": 1050000, "output": 128000},
        }
    }
    assert bot._find_model_context(models, "gpt-5.6-luna") == (
        "openai/gpt-5.6-luna",
        1050000,
    )


def test_find_openrouter_model_context_accepts_unqualified_model_id():
    models = [
        {
            "id": "openai/gpt-5.6-luna",
            "canonical_slug": "openai/gpt-5.6-luna-20260709",
            "context_length": 1050000,
            "top_provider": {"context_length": 1050000},
        }
    ]
    assert bot._find_openrouter_model_context(models, "gpt-5.6-luna") == (
        "openai/gpt-5.6-luna",
        1050000,
    )


def test_context_cmd_prefers_openrouter(monkeypatch):
    calls = []

    async def fake_fetch_openrouter_models(model_name):
        assert model_name == "gpt-5.6-luna"
        return [
            {
                "id": "openai/gpt-5.6-luna",
                "context_length": 1050000,
            }
        ]

    async def fake_fetch_models_dev_models():
        raise AssertionError("models.dev should not be called when OpenRouter matches")

    async def fake_reply_and_cleanup(msg, context, text, delay_seconds, **kwargs):
        calls.append((text, delay_seconds, kwargs))

    monkeypatch.setattr(bot, "_fetch_openrouter_models", fake_fetch_openrouter_models)
    monkeypatch.setattr(bot, "_fetch_models_dev_models", fake_fetch_models_dev_models)
    monkeypatch.setattr(bot, "_reply_and_cleanup", fake_reply_and_cleanup)

    asyncio.run(
        bot.context_cmd(
            _private_update(),
            SimpleNamespace(args=["gpt-5.6-luna"]),
        )
    )

    assert len(calls) == 1
    assert "gpt-5.6-luna" in calls[0][0]
    assert "1050000" in calls[0][0]
    assert "OpenRouter" in calls[0][0]
    assert "🧠" not in calls[0][0]
    assert calls[0][1] == bot.NOTICE_DELETE_TTL


def test_context_cmd_falls_back_to_models_dev(monkeypatch):
    calls = []

    async def fake_fetch_openrouter_models(model_name):
        assert model_name == "gpt-5.6-luna"
        raise RuntimeError("OpenRouter unavailable")

    async def fake_fetch_models_dev_models():
        return {
            "openai/gpt-5.6-luna": {
                "id": "openai/gpt-5.6-luna",
                "limit": {"context": 1050000},
            }
        }

    async def fake_reply_and_cleanup(msg, context, text, delay_seconds, **kwargs):
        calls.append((text, delay_seconds, kwargs))

    monkeypatch.setattr(bot, "_fetch_openrouter_models", fake_fetch_openrouter_models)
    monkeypatch.setattr(bot, "_fetch_models_dev_models", fake_fetch_models_dev_models)
    monkeypatch.setattr(bot, "_reply_and_cleanup", fake_reply_and_cleanup)

    asyncio.run(
        bot.context_cmd(
            _private_update(),
            SimpleNamespace(args=["gpt-5.6-luna"]),
        )
    )

    assert len(calls) == 1
    assert "1050000" in calls[0][0]
    assert "models.dev" in calls[0][0]


def test_context_cmd_reports_usage_without_model(monkeypatch):
    calls = []

    async def fake_reply_and_cleanup(msg, context, text, delay_seconds, **kwargs):
        calls.append((text, delay_seconds, kwargs))

    monkeypatch.setattr(bot, "_reply_and_cleanup", fake_reply_and_cleanup)

    asyncio.run(
        bot.context_cmd(
            _private_update("/context"),
            SimpleNamespace(args=[]),
        )
    )

    assert len(calls) == 1
    assert "/context <模型名>" in calls[0][0]


def test_context_cmd_reports_missing_model(monkeypatch):
    calls = []

    async def fake_fetch_openrouter_models(model_name):
        assert model_name == "unknown-model"
        return []

    async def fake_fetch_models_dev_models():
        return {"openai/gpt-5.6-luna": {"limit": {"context": 1050000}}}

    async def fake_reply_and_cleanup(msg, context, text, delay_seconds, **kwargs):
        calls.append((text, delay_seconds, kwargs))

    monkeypatch.setattr(bot, "_fetch_openrouter_models", fake_fetch_openrouter_models)
    monkeypatch.setattr(bot, "_fetch_models_dev_models", fake_fetch_models_dev_models)
    monkeypatch.setattr(bot, "_reply_and_cleanup", fake_reply_and_cleanup)

    asyncio.run(
        bot.context_cmd(
            _private_update("/context unknown-model"),
            SimpleNamespace(args=["unknown-model"]),
        )
    )

    assert len(calls) == 1
    assert "没找到这个模型" in calls[0][0]
