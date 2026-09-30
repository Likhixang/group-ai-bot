"""HEX parsing, pixel accuracy and Telegram reply regression tests."""
import asyncio
import importlib.util
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, call

import pytest
from PIL import Image

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "12345:TEST")
os.environ.setdefault("AI_API_KEY", "test")
SPEC = importlib.util.spec_from_file_location(
    "bot_hex_test", Path(__file__).resolve().parents[1] / "bot.py"
)
assert SPEC is not None and SPEC.loader is not None
bot = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(bot)


@pytest.mark.parametrize("text,expected", [
    ("#f00", "#FF0000"), ("#AbC", "#AABBCC"),
    ("#12aBcD", "#12ABCD"), ("12aBcD", "#12ABCD"),
    ("  #0f8\n", "#00FF88"), ("000000", "#000000"),
    ("FFFFFF", "#FFFFFF"),
])
def test_png_pixels_and_dimensions(text, expected):
    color, photo = bot._hex_color_png(text)
    with photo, Image.open(photo) as image:
        assert color == expected
        assert photo.name == expected[1:] + ".png"
        assert image.format == "PNG"
        assert image.mode == "RGB"
        assert image.size == (52, 52)
        rgb = tuple(int(expected[i:i + 2], 16) for i in (1, 3, 5))
        assert image.getextrema() == tuple((value, value) for value in rgb)


@pytest.mark.parametrize("text", [
    "", "abc", "123", "#12", "#1234", "#12345", "#1234567",
    "#12345678", "#gg0000", "0xFF0000", "##ff0000", "#ＦＦ００００",
    "color #ff0000", "#ff0000 please", "#ff0000\n#00ff00", "/img #ff0000",
])
def test_invalid_or_embedded_codes_do_not_match(text):
    assert not bot.HEX_COLOR_PATTERN.search(text)
    with pytest.raises(ValueError):
        bot._hex_color_png(text)


@pytest.mark.parametrize("chat_type,uid,allow_chat,allow_topic,should_send", [
    ("supergroup", 1, True, True, True),
    ("group", 1, True, True, True),
    ("channel", None, True, True, True),
    ("supergroup", 1, False, True, False),
    ("supergroup", 1, True, False, False),
    ("private", 1, True, True, False),
    ("private", 999, False, False, True),
])
def test_reply_and_permissions(monkeypatch, chat_type, uid, allow_chat, allow_topic, should_send):
    monkeypatch.setattr(bot, "SUPER_ADMIN_ID", 999)
    monkeypatch.setattr(bot, "_is_allowed_chat", lambda chat: allow_chat)
    monkeypatch.setattr(bot, "_is_allowed_topic", lambda msg: allow_topic)
    photos = []

    async def reply_photo(**kwargs):
        photo = kwargs["photo"]
        assert photo.tell() == 0
        with Image.open(photo) as image:
            assert image.size == (52, 52)
            assert image.getpixel((0, 0)) == (255, 0, 0)
        assert kwargs["caption"] == "#FF0000"
        assert kwargs["do_quote"] is True
        photos.append(photo)

    reply = AsyncMock(side_effect=reply_photo)
    msg = SimpleNamespace(text="#f00", from_user=SimpleNamespace(id=uid), reply_photo=reply)
    update = SimpleNamespace(effective_message=msg, effective_chat=SimpleNamespace(type=chat_type))
    cleanup = AsyncMock()
    monkeypatch.setattr(bot, "_auto_delete_after", cleanup)
    schedule = Mock(side_effect=lambda coro: coro.close())
    context = SimpleNamespace(application=SimpleNamespace(create_task=schedule))
    with pytest.raises(bot.ApplicationHandlerStop):
        asyncio.run(bot.on_hex_color(update, context))
    assert schedule.call_count == int(should_send)
    assert cleanup.call_count == int(should_send)
    assert reply.await_count == int(should_send)
    assert all(photo.closed for photo in photos)


@pytest.mark.asyncio
@pytest.mark.parametrize("ttl", [30, 7])
@pytest.mark.parametrize("send_fails", [False, True])
@pytest.mark.parametrize("source_already_deleted", [False, True])
async def test_cleanup_uses_global_ttl_and_deletes_both_messages(
    monkeypatch, ttl, send_fails, source_already_deleted
):
    monkeypatch.setattr(bot, "NOTICE_DELETE_TTL", ttl)
    monkeypatch.setattr(bot, "_is_allowed_chat", lambda chat: True)
    monkeypatch.setattr(bot, "_is_allowed_topic", lambda msg: True)
    sleep = AsyncMock()
    monkeypatch.setattr(bot.asyncio, "sleep", sleep)
    sent = SimpleNamespace(chat_id=-100123, message_id=102)
    reply = AsyncMock(return_value=sent)
    if send_fails:
        reply.side_effect = RuntimeError("Telegram unavailable")
    msg = SimpleNamespace(
        text="#f00", from_user=SimpleNamespace(id=1), reply_photo=reply,
        chat_id=-100123, message_id=101,
        reply_to_message=SimpleNamespace(chat_id=-100123, message_id=100),
    )
    delete = AsyncMock(side_effect=[RuntimeError("already deleted"), True]
                       if source_already_deleted else None)
    pending = []
    context = SimpleNamespace(
        bot=SimpleNamespace(delete_message=delete),
        application=SimpleNamespace(create_task=pending.append),
    )
    update = SimpleNamespace(
        effective_message=msg, effective_chat=SimpleNamespace(type="supergroup")
    )
    with pytest.raises(bot.ApplicationHandlerStop):
        await bot.on_hex_color(update, context)
    assert len(pending) == 1
    # Scheduling must not block the handler or delete before the delay.
    delete.assert_not_awaited()
    await pending[0]
    sleep.assert_awaited_once_with(ttl)
    expected = [call(chat_id=-100123, message_id=101)]
    if not send_fails:
        expected.append(call(chat_id=-100123, message_id=102))
    assert delete.await_args_list == expected
