import os
import sys
from unittest.mock import AsyncMock, MagicMock
import pytest
from telegram import MessageEntity
from telegram.constants import ChatType, MessageEntityType

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")
os.environ.setdefault("AI_API_KEY", "test-key")
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import bot  # noqa: E402


def test_message_contains_link_plain_text():
    msg = MagicMock()
    msg.entities = ()
    msg.caption_entities = ()
    msg.text = "Hello https://example.com world"
    msg.caption = None
    assert bot._message_contains_link(msg) is True


def test_message_contains_link_no_link():
    msg = MagicMock()
    msg.entities = ()
    msg.caption_entities = ()
    msg.text = "Hello world without any link"
    msg.caption = None
    assert bot._message_contains_link(msg) is False


def test_message_contains_link_tg_entity_url():
    msg = MagicMock()
    ent = MessageEntity(type=MessageEntityType.URL, offset=0, length=11)
    msg.entities = (ent,)
    msg.caption_entities = ()
    msg.text = "example.com"
    msg.caption = None
    assert bot._message_contains_link(msg) is True


def test_message_contains_link_tg_entity_text_link():
    msg = MagicMock()
    ent = MessageEntity(
        type=MessageEntityType.TEXT_LINK,
        offset=0,
        length=4,
        url="https://github.com",
    )
    msg.entities = (ent,)
    msg.caption_entities = ()
    msg.text = "点这里"
    msg.caption = None
    assert bot._message_contains_link(msg) is True


def test_message_contains_link_caption_entity():
    msg = MagicMock()
    ent = MessageEntity(
        type=MessageEntityType.TEXT_LINK,
        offset=0,
        length=4,
        url="https://google.com",
    )
    msg.entities = ()
    msg.caption_entities = (ent,)
    msg.text = None
    msg.caption = "看图点击"
    assert bot._message_contains_link(msg) is True


def test_message_contains_link_caption_raw_url():
    msg = MagicMock()
    msg.entities = ()
    msg.caption_entities = ()
    msg.text = None
    msg.caption = "图片说明 http://t.me/some_channel"
    assert bot._message_contains_link(msg) is True


@pytest.mark.asyncio
async def test_enforce_link_rule_replies_with_fixed_warning():
    update = MagicMock()
    context = MagicMock()

    chat = MagicMock()
    chat.type = ChatType.SUPERGROUP
    chat.id = -100123456789
    update.effective_chat = chat

    user = MagicMock()
    user.id = 999999
    user.is_bot = False
    msg = MagicMock()
    msg.message_id = 42
    msg.from_user = user
    msg.text = "这里有链接 https://t.me/test"
    msg.caption = None
    msg.entities = ()
    msg.caption_entities = ()
    msg.reply_text = AsyncMock()
    update.effective_message = msg

    orig_allowed = bot._is_allowed_chat
    orig_protected = bot._is_soft_ban_protected_user
    try:
        bot._is_allowed_chat = lambda c: True
        bot._is_soft_ban_protected_user = lambda uid: False

        await bot.enforce_link_rule(update, context)

        msg.reply_text.assert_awaited_once_with("链接未经验证，谨慎参考")
    finally:
        bot._is_allowed_chat = orig_allowed
        bot._is_soft_ban_protected_user = orig_protected
