"""Regression tests for profile/tag changes replaying old Telegram messages."""
import importlib.util
import os
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from telegram import Update, User
from telegram.ext import Application, CommandHandler, MessageHandler

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "12345:TEST")
os.environ.setdefault("AI_API_KEY", "test")
os.environ.setdefault("MEMORY_DB_PATH", ":memory:")
SPEC = importlib.util.spec_from_file_location(
    "bot_edited_updates_test", Path(__file__).resolve().parents[1] / "bot.py"
)
assert SPEC is not None and SPEC.loader is not None
bot = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(bot)


@pytest.fixture
def application(monkeypatch):
    """Register production handlers without polling or any Telegram API calls."""
    app = Application.builder().token("12345:TEST").build()
    app.bot._bot_user = User(999, "Bot", True, username="Anyincubation_bot")
    app._initialized = True
    builder = Application.builder().token("12345:TEST")
    monkeypatch.setattr(type(builder), "build", lambda self: app)
    monkeypatch.setattr(Application, "run_polling", lambda self, **kwargs: None)
    monkeypatch.setattr(bot, "validate_env", lambda: None)
    monkeypatch.setattr(bot, "_init_memory_db", lambda: None)
    bot.main()
    # Exercise the real filters, group ordering and ApplicationHandlerStop.
    # Only downstream side effects are replaced, not the edit guard itself.
    callbacks = {}
    for handlers in app.handlers.values():
        for handler in handlers:
            if isinstance(handler, (CommandHandler, MessageHandler)):
                name = handler.callback.__name__
                if name != "ignore_edited_update":
                    callbacks.setdefault(name, AsyncMock())
                    handler.callback = callbacks[name]
    return app, callbacks


def make_update(app, kind, body):
    message = {
        "message_id": 77,
        "date": 1700000000,
        "chat": {"id": -100123, "type": "supergroup"},
        "from": {
            "id": 123,
            "is_bot": False,
            "first_name": "Renamed User",
            "username": "changed_tag",
        },
    }
    if "channel_post" in kind:
        message["chat"] = {"id": -100123, "type": "channel"}
    if "business_message" in kind:
        message["business_connection_id"] = "test-business-connection"
        message["chat"] = {"id": 123, "type": "private"}
    if body == "photo":
        message["caption"] = "生成图片 old request"
        message["photo"] = [
            {"file_id": "old-file", "file_unique_id": "old", "width": 10, "height": 10}
        ]
    else:
        message["text"] = body
        if body.startswith("/"):
            message["entities"] = [
                {"type": "bot_command", "offset": 0, "length": len(body.split()[0])}
            ]
        if body == "old reply":
            message["reply_to_message"] = {
                "message_id": 76,
                "date": 1700000000,
                "chat": message["chat"],
                "from": {"id": 999, "is_bot": True, "first_name": "Bot"},
                "text": "previous AI answer",
            }
    return Update.de_json({"update_id": 42, kind: message}, app.bot)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kind", ["edited_message", "edited_channel_post", "edited_business_message"]
)
@pytest.mark.parametrize("body", ["ds old question", "/ds old question", "/force_clear", "photo", "old reply", "#aBc123"])
async def test_edited_updates_never_reach_side_effect_handlers(application, kind, body):
    app, callbacks = application
    await app.process_update(make_update(app, kind, body))
    assert not any(callback.await_count for callback in callbacks.values())


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kind,body,expected",
    [
        ("message", "ds new question", "on_text"),
        ("channel_post", "ds new question", "on_text"),
        ("business_message", "ds new question", "on_text"),
        ("message", "/ds new question", "ai_cmd"),
        ("message", "photo", "on_image_request"),
        ("channel_post", "photo", "on_image_request"),
        ("business_message", "photo", "on_image_request"),
    ],
)
async def test_new_updates_still_reach_existing_handlers(application, kind, body, expected):
    app, callbacks = application
    await app.process_update(make_update(app, kind, body))
    callbacks[expected].assert_awaited_once()


def test_rename_does_not_change_memory_key(application):
    app, _ = application
    original = make_update(app, "message", "ds question")
    data = original.to_dict()
    data["message"]["from"]["first_name"] = "Another Name"
    data["message"]["from"]["username"] = "another_tag"
    renamed = Update.de_json(data, app.bot)
    assert bot._memory_key(original) == bot._memory_key(renamed)


@pytest.mark.asyncio
@pytest.mark.parametrize("reply_kind", [None, "text", "photo"])
@pytest.mark.parametrize("send_fails", [False, True])
async def test_hex_stops_ai_routing_and_quotes_original(application, monkeypatch, reply_kind, send_fails):
    app, callbacks = application
    callbacks["on_hex_color"].side_effect = bot.on_hex_color
    monkeypatch.setattr(bot, "_auto_delete_after", AsyncMock())
    cleanup_tasks = []
    monkeypatch.setattr(Application, "create_task", lambda self, coro: cleanup_tasks.append(coro))
    monkeypatch.setattr(bot, "_is_allowed_chat", lambda chat: True)
    monkeypatch.setattr(bot, "_is_allowed_topic", lambda msg: True)
    send_photo = AsyncMock(side_effect=RuntimeError("Telegram unavailable") if send_fails else None)
    monkeypatch.setattr(type(app.bot), "send_photo", send_photo)
    data = make_update(app, "message", "#aBc123").to_dict()
    data["message"]["is_topic_message"] = True
    data["message"]["message_thread_id"] = 456
    if reply_kind:
        replied = {
            "message_id": 76, "date": 1700000000, "chat": data["message"]["chat"],
            "from": {"id": 999, "is_bot": True, "first_name": "Bot"},
        }
        if reply_kind == "text":
            replied["text"] = "previous AI answer"
        else:
            replied["photo"] = [
                {"file_id": "image", "file_unique_id": "image", "width": 52, "height": 52}
            ]
        data["message"]["reply_to_message"] = replied
    await app.process_update(Update.de_json(data, app.bot))
    assert len(cleanup_tasks) == 1
    await cleanup_tasks[0]
    send_photo.assert_awaited_once()
    kwargs = send_photo.call_args.kwargs
    assert kwargs["caption"] == "#ABC123"
    assert kwargs["message_thread_id"] == 456
    assert kwargs["reply_parameters"].message_id == 77
    callbacks["on_image_request"].assert_not_awaited()
    callbacks["on_text"].assert_not_awaited()
    callbacks["enforce_soft_ban"].assert_awaited_once()
    assert "track_activity" not in callbacks


@pytest.mark.asyncio
async def test_soft_ban_stops_hex_reply(application):
    app, callbacks = application
    callbacks["enforce_soft_ban"].side_effect = bot.ApplicationHandlerStop
    await app.process_update(make_update(app, "message", "#abcdef"))
    callbacks["on_hex_color"].assert_not_awaited()


def test_inactivity_message_tracking_is_not_registered(application):
    _, callbacks = application
    assert "track_activity" not in callbacks


@pytest.mark.asyncio
async def test_startup_has_no_inactivity_job(application, monkeypatch):
    app, _ = application
    tasks = []
    monkeypatch.setattr(type(app.bot), "get_me", AsyncMock(return_value=app.bot._bot_user))
    monkeypatch.setattr(type(app.bot), "set_my_commands", AsyncMock())

    def capture_task(self, coro):
        tasks.append(coro.cr_code.co_name)
        coro.close()

    monkeypatch.setattr(Application, "create_task", capture_task)
    await bot.post_init(app)
    assert tasks == [
        "_managed_pin_scheduler_loop",
        "_codex_reset_scheduler_loop",
        "_ban_release_scheduler_loop",
    ]


def test_inactivity_config_and_kick_implementation_are_removed():
    assert not any(name.startswith("INACTIVITY_") for name in vars(bot))
    assert not hasattr(bot, "_ban_chat_member_kick")
    assert not hasattr(bot, "_check_and_kick_due")
