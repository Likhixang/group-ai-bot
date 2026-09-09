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
async def test_enforce_link_rule_replies_without_deleting():
    update = MagicMock()
    context = MagicMock()
    context.bot.delete_message = AsyncMock()

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

    status_msg = MagicMock()
    status_msg.edit_text = AsyncMock()
    msg.reply_text = AsyncMock(return_value=status_msg)
    update.effective_message = msg

    orig_allowed = bot._is_allowed_chat
    orig_protected = bot._is_soft_ban_protected_user
    orig_review = bot._review_link_content_with_luna
    try:
        bot._is_allowed_chat = lambda c: True
        bot._is_soft_ban_protected_user = lambda uid: False
        bot._review_link_content_with_luna = AsyncMock(return_value="总结：这是一个电报群链接。\n含金量：80%\n含屎量：20%")

        await bot.enforce_link_rule(update, context)

        context.bot.delete_message.assert_not_called()
        msg.reply_text.assert_awaited_once_with("🔍 Luna 正在审评该链接内容...")
        status_msg.edit_text.assert_awaited_once_with("总结：这是一个电报群链接。\n含金量：80%\n含屎量：20%")
    finally:
        bot._is_allowed_chat = orig_allowed
        bot._is_soft_ban_protected_user = orig_protected
        bot._review_link_content_with_luna = orig_review


@pytest.mark.asyncio
async def test_extract_first_link_from_message():
    # URL entity
    msg = MagicMock()
    msg.text = "check https://example.com/a here"
    msg.caption = None
    ent = MessageEntity(type=MessageEntityType.URL, offset=6, length=21)
    msg.entities = (ent,)
    msg.caption_entities = ()
    assert bot._extract_first_link_from_message(msg) == "https://example.com/a"

    # TEXT_LINK entity
    msg_tl = MagicMock()
    msg_tl.text = "click here"
    msg_tl.caption = None
    ent_tl = MessageEntity(type=MessageEntityType.TEXT_LINK, offset=0, length=10, url="https://example.com/custom")
    msg_tl.entities = (ent_tl,)
    msg_tl.caption_entities = ()
    assert bot._extract_first_link_from_message(msg_tl) == "https://example.com/custom"
