import logging
import os
import time
import json
import asyncio
import ipaddress
import sqlite3
import re
import urllib.parse
import base64
from io import BytesIO
from collections import defaultdict
from html import escape, unescape
from typing import Optional
from zoneinfo import ZoneInfo
from datetime import datetime

import httpx
from PIL import Image, ImageDraw, ImageFont
from telegram import (
    BotCommand,
    BotCommandScopeAllGroupChats,
    BotCommandScopeAllPrivateChats,
    MessageEntity,
    Update,
)
from telegram.constants import ChatMemberStatus, ChatType, ParseMode
from telegram.error import BadRequest, TimedOut
from telegram.ext import (
    Application,
    ChatMemberHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from ipcheck import check_ip, format_report, is_valid_ip

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("group-ai-bot")
# HTTPX logs complete request URLs at INFO. Suppress them so `/av <番号>`
# inputs are not copied into persistent container logs.
logging.getLogger("httpx").setLevel(logging.WARNING)


TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
AI_BASE_URL = os.getenv("AI_BASE_URL", "https://api.openai.com/v1").strip()
AI_API_KEY = os.getenv("AI_API_KEY", "").strip()
# Prefer DS_MODEL; fall back to legacy AI_MODEL for existing .env files.
DS_MODEL = (
    os.getenv("DS_MODEL")
    or os.getenv("AI_MODEL")
    or "deepseek-v4-flash"
).strip()
AI_THINKING_MODEL = os.getenv("AI_THINKING_MODEL", "ds-4.1-thinking").strip()
OAI_MODEL = os.getenv("OAI_MODEL", "gpt-5.5").strip()
GROK_MODEL = os.getenv("GROK_MODEL", "grok-4.6").strip()
IMAGE_MODEL = os.getenv("IMAGE_MODEL", "default").strip().lower()
IMAGE_EDIT_MODEL = os.getenv("IMAGE_EDIT_MODEL", IMAGE_MODEL).strip().lower()
# --- 图片生成 (imagefree API, https://imagefree.tingfengai.art) ---
# 文生图：POST /v1/generate（同步等待出图，典型 20~45 秒）
# 图生图：POST /v1/edit（异步提交，轮询 /v1/edit/tasks/{job_id}，上游排队约 1~5 分钟）
IMAGEFREE_BASE_URL = os.getenv("IMAGEFREE_BASE_URL", "https://imagefree.tingfengai.art").strip().rstrip("/")
# imagefree 风格预设（model 参数取值），非白名单值自动回退 default（兼容旧 .env 里的模型名）
IMAGEFREE_TXT2IMG_MODELS = {"default", "anime", "realistic", "watercolor", "ink", "cyberpunk"}
IMAGEFREE_IMG2IMG_MODELS = {"default", "watercolor", "cyberpunk"}
if IMAGE_MODEL not in IMAGEFREE_TXT2IMG_MODELS:
    IMAGE_MODEL = "default"
if IMAGE_EDIT_MODEL not in IMAGEFREE_IMG2IMG_MODELS:
    IMAGE_EDIT_MODEL = "default"
# 画幅：1:1 / 3:4 / 4:3 / 9:16 / 16:9
IMAGE_ASPECT_RATIO = os.getenv("IMAGE_ASPECT_RATIO", "1:1").strip()
# 图生图异步任务轮询参数
IMAGE_EDIT_POLL_INTERVAL = int(os.getenv("IMAGE_EDIT_POLL_INTERVAL", "10"))
IMAGE_EDIT_POLL_TIMEOUT = int(os.getenv("IMAGE_EDIT_POLL_TIMEOUT", "600"))
MEMORY_MAX_TURNS = int(os.getenv("MEMORY_MAX_TURNS", "8"))
SUPER_ADMIN_ID = int(os.getenv("SUPER_ADMIN_ID", "697735771"))
MAX_CONCURRENT_UPDATES = int(os.getenv("MAX_CONCURRENT_UPDATES", "8"))
MEMORY_DB_PATH = os.getenv("MEMORY_DB_PATH", "/data/memory.db").strip()
# 空字符串 = 不注入任何 system prompt
SYSTEM_PROMPT = os.getenv("SYSTEM_PROMPT", "").strip()
ALLOWED_GROUP_IDS = {
    x.strip() for x in os.getenv("ALLOWED_GROUP_IDS", "").split(",") if x.strip()
}
ALLOWED_CHAT_USERNAMES = {
    x.strip().lower()
    for x in os.getenv("ALLOWED_CHAT_USERNAMES", "").split(",")
    if x.strip()
}
ALLOWED_TOPIC_IDS = {
    x.strip() for x in os.getenv("ALLOWED_TOPIC_IDS", "").split(",") if x.strip()
}
PIN_TARGET_CHAT_ID = int(os.getenv("PIN_TARGET_CHAT_ID", "-1003757376633"))
STATUS_API_URL = os.getenv("STATUS_API_URL", "http://status-monitor:8000/api/status").strip()
PIN_TARGET_TOPIC_ID = os.getenv("PIN_TARGET_TOPIC_ID", "1").strip()
PIN_TZ = os.getenv("PIN_TZ", "Asia/Shanghai").strip()
PIN_HOUR = int(os.getenv("PIN_HOUR", "0"))
PIN_MINUTE = int(os.getenv("PIN_MINUTE", "1"))
DEFAULT_DAILY_PIN_TEXT = os.getenv(
    "DEFAULT_DAILY_PIN_TEXT",
    "这里不许开盒 涉政 灰产 要饭 可以搞黄色",
).strip()

BOT_USERNAME: Optional[str] = None
BOT_ID: Optional[int] = None
MEMORY_LOCKS = defaultdict(asyncio.Lock)
PIN_LOCK = asyncio.Lock()
URL_PATTERN = re.compile(r"https?://[^\s\"'<>]+", re.IGNORECASE)
TG_USER_URL_RE = re.compile(r"tg://user\?id=(\d+)", re.IGNORECASE)
MARKDOWN_IMAGE_RE = re.compile(r"!\[([^\]]*)\]\((https?://[^)\s]+)\)", re.IGNORECASE)
HTML_IMG_TAG_RE = re.compile(r"<img\b[^>]*>", re.IGNORECASE)
IMAGE_URL_FILE_RE = re.compile(
    r"^https?://\S+\.(?:png|jpe?g|gif|webp|bmp|svg)(?:\?\S*)?$",
    re.IGNORECASE,
)
AVSCAN_COMMAND_RE = re.compile(
    r"(?:^|\s)/av(?:@(?P<target>\w+))?(?=\s|$)", re.IGNORECASE
)
AV_TEXT_COMMAND_RE = re.compile(
    r"^/av(?:@(?P<target>\w+))?(?:\s+(?P<argument>.+?))?\s*$",
    re.IGNORECASE | re.DOTALL,
)
AV_COVER_CODE_RE = re.compile(
    r"^(?=.{3,48}$)(?=.*[A-Za-z])(?=.*\d)[A-Za-z0-9]+(?:[-_][A-Za-z0-9]+){0,3}$"
)
PIN_LAST_RUN_DATE: Optional[str] = None
# 并发限制：最多 3 个请求同时处理
_ACTIVE_DS = 0
_ACTIVE_DS_LOCK = asyncio.Lock()
MAX_CONCURRENT_DS = 3
# 活跃 AI 任务追踪（用于 /force_stop 和 /stop）
_ACTIVE_AI_TASKS: set[asyncio.Task] = set()
_ACTIVE_USER_TASKS: dict[int, set[asyncio.Task]] = defaultdict(set)
MODEL_LINE_RE = re.compile(r"(?:模型|model)\s*[:：]\s*([^\n\r]+)", re.IGNORECASE)
CHATTER_LINE_RE = re.compile(
    r"(如果你喜欢|随时告诉我|我再帮你|今天有什么开心|我很乐意继续陪你|希望能让你|收到|继续给你分享)",
    re.IGNORECASE,
)
EMOJI_RE = re.compile(
    "[\U0001F300-\U0001FAFF\U00002700-\U000027BF\U0001F1E6-\U0001F1FF]+"
)
AI_STREAM_TIMEOUT_RETRIES = 2
IMAGE_GEN_TIMEOUT = 300
IMAGE_GEN_RETRIES = 1

# --- AVScan 图片检索 ---
# AVScan 当前没有公开 API key / 文档；前端使用同源 POST /search 上传 `file`。
# 这里默认走其 HTTPS 入口，并把 Telegram 原图统一转为与其前端一致的缩小 JPEG。
AVSCAN_API_URL = (
    os.getenv("AVSCAN_API_URL", "https://avscan.cc/search").strip()
    or "https://avscan.cc/search"
)
AVSCAN_TIMEOUT = max(5, int(os.getenv("AVSCAN_TIMEOUT", "90")))
AVSCAN_MAX_RESULTS = max(1, min(10, int(os.getenv("AVSCAN_MAX_RESULTS", "10"))))
AVSCAN_MAX_SOURCE_BYTES = 20 * 1024 * 1024
AVSCAN_MAX_UPLOAD_BYTES = 8 * 1024 * 1024
AVSCAN_MAX_IMAGE_PIXELS = 40_000_000
AVSCAN_MAX_SIDE = 1024

# --- 番号封面查询（R18.dev JSON → DMM jacket）---
# R18.dev 的 DVD ID JSON 返回 DMM jacket URL；每次命令只请求一次。
R18DEV_LOOKUP_URL = "https://r18.dev/videos/vod/movies/detail/-/dvd_id={dvd_id}/json"
R18DEV_TIMEOUT = max(5, int(os.getenv("R18DEV_TIMEOUT", "30")))
R18DEV_MAX_JSON_BYTES = 1 * 1024 * 1024
R18DEV_MAX_COVER_BYTES = 8 * 1024 * 1024
R18DEV_MAX_COVER_PIXELS = 40_000_000
R18DEV_COVER_HOST = "pics.dmm.co.jp"
R18DEV_USER_AGENT = (
    "group-ai-bot/1.0 "
    "(single cover lookup; https://github.com/Likhixang/group-ai-bot)"
)

# --- 视频生成 (agnes-video-v2.0 异步任务 API) ---
# 注意：创建任务可走 AxonHub，但取结果必须直连上游 —— AxonHub 会剥掉响应里的
# 视频 url 字段，只回 status/progress/seconds/size，拿不到成片。
VIDEO_MODEL = os.getenv("VIDEO_MODEL", "agnes-video-v2.0").strip()
VIDEO_BASE_URL = os.getenv("VIDEO_BASE_URL", "https://apihub.agnes-ai.com").strip().rstrip("/")
VIDEO_API_KEY = os.getenv("VIDEO_API_KEY", "").strip()
VIDEO_NUM_FRAMES = int(os.getenv("VIDEO_NUM_FRAMES", "81"))   # 8n+1，<=441；81/24fps ≈ 3.4s
VIDEO_FRAME_RATE = int(os.getenv("VIDEO_FRAME_RATE", "24"))
VIDEO_WIDTH = int(os.getenv("VIDEO_WIDTH", "832"))
VIDEO_HEIGHT = int(os.getenv("VIDEO_HEIGHT", "448"))
VIDEO_POLL_INTERVAL = int(os.getenv("VIDEO_POLL_INTERVAL", "6"))
VIDEO_POLL_TIMEOUT = int(os.getenv("VIDEO_POLL_TIMEOUT", "600"))
WEB_SEARCH_MAX_RESULTS = int(os.getenv("WEB_SEARCH_MAX_RESULTS", "5"))
WEB_FETCH_MAX_RESULTS = int(os.getenv("WEB_FETCH_MAX_RESULTS", "3"))
WEB_FETCH_MAX_CHARS = int(os.getenv("WEB_FETCH_MAX_CHARS", "1800"))
OPENROUTER_MODELS_URL = "https://openrouter.ai/api/v1/models"
OPENROUTER_MODEL_URL = "https://openrouter.ai/api/v1/model"
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "").strip()
OPENROUTER_CACHE_TTL = max(0, int(os.getenv("OPENROUTER_CACHE_TTL", "3600")))
MODELS_DEV_MODELS_URL = "https://models.dev/models.json"
MODELS_DEV_CACHE_TTL = max(0, int(os.getenv("MODELS_DEV_CACHE_TTL", "3600")))
_OPENROUTER_CACHE: dict[str, tuple[float, list[dict]]] = {}
_MODELS_DEV_CACHE: Optional[tuple[float, dict]] = None
WEB_SEARCH_TRIGGERS = (
    "搜索", "搜一下", "查一下", "查查", "联网", "上网", "最新", "新闻",
    "今天", "现在", "recent", "latest", "news", "search", "web",
)

# 所有「定时删除」的命令/提示统一留存时间（秒）。立刻删除仍用 0。
NOTICE_DELETE_TTL = 30
BAN_CHECK_INTERVAL = 20  # seconds
# soft ban 用 until_date=0 表示永久（仅超管 /allow 解除）
SOFT_BAN_PERMANENT_UNTIL = 0
# ban/allow 公告与 soft ban 再发言提醒：与全局 NOTICE_DELETE_TTL 一致
SOFT_BAN_NOTICE_TTL = NOTICE_DELETE_TTL
# 同一用户被 soft ban 后重复发言的提醒编辑冷却（秒），避免 tp 被限
SOFT_BAN_REPEAT_COOLDOWN = 2
# chat_id:user_id -> last_notice_ts
_SOFT_BAN_LAST_NOTICE: dict[str, int] = {}
# chat_id:user_id -> asyncio.Task（提醒的定时删除任务，编辑时取消重排）
_SOFT_BAN_NOTICE_DELETE_TASKS: dict[str, "asyncio.Task"] = {}

# --- 群活跃度：30 天提醒 / 45 天再提醒 / 60 天最后警告后移出 ---
# 仅追踪 bot 实际见过的成员（发言 / 入群）。Bot API 无法枚举全员。
# 若 bot 开启了隐私模式且非管理员，只能看到部分消息，活跃度会不完整。
# 全程不修改成员权限；超管 /ban 也是 soft ban（删消息）。
INACTIVITY_WARN_ENABLED = os.getenv("INACTIVITY_WARN_ENABLED", "1").strip().lower() not in {
    "0", "false", "no", "off", ""
}
INACTIVITY_DAYS = max(1, int(os.getenv("INACTIVITY_DAYS", "30")))
# 第二次提醒天数（兼容旧环境变量 INACTIVITY_MUTE_DAYS）
INACTIVITY_REMIND2_DAYS = max(
    INACTIVITY_DAYS,
    int(os.getenv("INACTIVITY_REMIND2_DAYS", os.getenv("INACTIVITY_MUTE_DAYS", "45"))),
)
# 最后警告 / 踢人预告天数（需 >= 第二次提醒）
INACTIVITY_KICK_DAYS = max(
    INACTIVITY_REMIND2_DAYS,
    int(os.getenv("INACTIVITY_KICK_DAYS", "60")),
)
# 踢人预告后的宽限期（天），到期仍无发言记录则移出
INACTIVITY_KICK_GRACE_DAYS = max(1, int(os.getenv("INACTIVITY_KICK_GRACE_DAYS", "3")))
INACTIVITY_CHECK_INTERVAL = max(60, int(os.getenv("INACTIVITY_CHECK_INTERVAL", "3600")))
INACTIVITY_MAX_WARNS_PER_CYCLE = max(1, int(os.getenv("INACTIVITY_MAX_WARNS_PER_CYCLE", "10")))
INACTIVITY_MAX_REMIND2_PER_CYCLE = max(
    1,
    int(os.getenv("INACTIVITY_MAX_REMIND2_PER_CYCLE", os.getenv("INACTIVITY_MAX_MUTES_PER_CYCLE", "5"))),
)
INACTIVITY_MAX_KICK_WARNS_PER_CYCLE = max(
    1, int(os.getenv("INACTIVITY_MAX_KICK_WARNS_PER_CYCLE", "5"))
)
INACTIVITY_MAX_KICKS_PER_CYCLE = max(1, int(os.getenv("INACTIVITY_MAX_KICKS_PER_CYCLE", "5")))
INACTIVITY_WARN_TEXT = os.getenv(
    "INACTIVITY_WARN_TEXT",
    "你已一个月未发言，请增加活跃度，否则严重会被移出群组。",
).strip()
INACTIVITY_REMIND2_TEXT = os.getenv(
    "INACTIVITY_REMIND2_TEXT",
    os.getenv(
        "INACTIVITY_MUTE_TEXT",
        "你已一个半月未发言，请尽快增加活跃度，否则将被移出群组。",
    ),
).strip()
INACTIVITY_KICK_WARN_TEXT = os.getenv(
    "INACTIVITY_KICK_WARN_TEXT",
    "你已经两个月未发言，将在三天后移出群。请尽快发言以保留群籍。",
).strip()
INACTIVITY_KICK_DONE_TEXT = os.getenv(
    "INACTIVITY_KICK_DONE_TEXT",
    "因长期未发言，已移出群组。",
).strip()
# 兼容旧名（仅供日志/测试引用，语义已改为第二次提醒天数）
INACTIVITY_MUTE_DAYS = INACTIVITY_REMIND2_DAYS
INACTIVITY_MUTE_TEXT = INACTIVITY_REMIND2_TEXT
INACTIVITY_MAX_MUTES_PER_CYCLE = INACTIVITY_MAX_REMIND2_PER_CYCLE


def _extract_first_url(text: str) -> Optional[str]:
    m = URL_PATTERN.search(text or "")
    if not m:
        return None
    return m.group(0).rstrip(".,;:!?)]}>\"'")


def _extract_all_urls(text: str) -> list[str]:
    urls = []
    for m in URL_PATTERN.finditer(text or ""):
        u = m.group(0).rstrip(".,;:!?)]}>\"'")
        if u:
            urls.append(u)
    return urls


def _is_timeout_error(exc: Exception) -> bool:
    if isinstance(exc, (httpx.TimeoutException, asyncio.TimeoutError, TimedOut)):
        return True

    seen = set()
    cur = exc
    for _ in range(6):
        if not cur or id(cur) in seen:
            break
        seen.add(id(cur))
        if isinstance(cur, (httpx.TimeoutException, asyncio.TimeoutError, TimedOut)):
            return True
        cur = cur.__cause__ or cur.__context__
    return False


async def _edit_message_text_retry(
    context: ContextTypes.DEFAULT_TYPE,
    *,
    chat_id: int,
    message_id: int,
    text: str,
    parse_mode=ParseMode.HTML,
    disable_web_page_preview: bool = True,
    attempts: int = 2,
    retry_delay: float = 0.6,
) -> None:
    for i in range(attempts):
        try:
            await context.bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=text,
                parse_mode=parse_mode,
                disable_web_page_preview=disable_web_page_preview,
            )
            return
        except BadRequest as e:
            if "Message is not modified" in str(e):
                return
            raise
        except Exception as e:
            if i < attempts - 1 and _is_timeout_error(e):
                logger.warning(
                    "edit_message_text timed out, retrying (%s/%s)",
                    i + 1,
                    attempts,
                )
                await asyncio.sleep(retry_delay)
                continue
            raise


def _is_allowed_chat(chat) -> bool:
    has_group_rules = bool(ALLOWED_GROUP_IDS)
    has_username_rules = bool(ALLOWED_CHAT_USERNAMES)
    if not has_group_rules and not has_username_rules:
        return True
    chat_id = str(chat.id)
    username = (chat.username or "").lower()
    return (chat_id in ALLOWED_GROUP_IDS) or (
        username in ALLOWED_CHAT_USERNAMES
    )


def _topic_id_of_message(msg, chat=None) -> Optional[str]:
    if not msg:
        return None
    tid = getattr(msg, "message_thread_id", None)
    if tid is None:
        # In forum groups, the "General" topic may come without message_thread_id.
        # Treat it as topic 1 so topic-link `/1` can be configured consistently.
        c = chat or getattr(msg, "chat", None)
        if c is not None and getattr(c, "is_forum", False):
            return "1"
        return None
    return str(tid)


def _is_allowed_topic(msg) -> bool:
    if not ALLOWED_TOPIC_IDS:
        return True
    tid = _topic_id_of_message(msg, getattr(msg, "chat", None))
    return bool(tid and tid in ALLOWED_TOPIC_IDS)


def _is_private_super_admin(chat, uid) -> bool:
    """Check if this is the super admin in a private chat (bypass all group checks)."""
    return chat.type == ChatType.PRIVATE and uid == SUPER_ADMIN_ID


def _allowed_topics_text() -> str:
    if not ALLOWED_TOPIC_IDS:
        return "全部话题"
    return ", ".join(sorted(ALLOWED_TOPIC_IDS))


def _not_allowed_usage_text() -> str:
    return (
        "🚫 该机器人仅允许在指定话题使用。"
    )


def _now_pin_tz() -> datetime:
    return datetime.now(ZoneInfo(PIN_TZ))


async def _reply_text_and_track(msg, text: str, **kwargs):
    return await msg.reply_text(text, **kwargs)


def _user_display_name(user, *, fallback_id: Optional[int] = None) -> str:
    """Best-effort plain display name for a Telegram user object or partial data."""
    if user is not None:
        name = (getattr(user, "full_name", None) or "").strip()
        if name:
            return name
        username = (getattr(user, "username", None) or "").strip()
        if username:
            return username
        uid = getattr(user, "id", None)
        if uid is not None:
            return f"用户{uid}"
    if fallback_id is not None:
        return f"用户{fallback_id}"
    return "未知用户"


def _html_user_mention(user_id: int, display_name: Optional[str] = None) -> str:
    """Clickable Telegram user mention via HTML <a href=\"tg://user?id=...\">.

    Renders as a tappable name that opens the user's profile.
    """
    uid = int(user_id)
    name = (display_name or "").strip() or f"用户{uid}"
    return f'<a href="tg://user?id={uid}">{escape(name)}</a>'


async def _auto_delete_after(cmd_msg, reply_msg, context, *, delay: int = NOTICE_DELETE_TTL) -> None:
    """延时后自动删除命令消息和机器人回复。"""
    await asyncio.sleep(delay)
    for m in (cmd_msg, reply_msg):
        if m is None:
            continue
        try:
            await context.bot.delete_message(chat_id=m.chat_id, message_id=m.message_id)
        except Exception:
            pass


async def _publish_managed_pin(
    bot,
    *,
    chat_id: int,
    topic_id: str,
    text: str,
    entities: Optional[list] = None,
) -> int:
    cleaned = (text or "").strip()
    if not cleaned:
        raise ValueError("pin text is empty")

    async with PIN_LOCK:
        current = _load_managed_pin(chat_id, topic_id)
        old_message_id = current["message_id"] if current else None

        send_kwargs: dict = {"chat_id": chat_id, "text": cleaned}
        # Preserve native Telegram formatting (bold/italic/link/code...) by
        # carrying the MessageEntity objects through to send_message.
        if entities:
            send_kwargs["entities"] = entities
        if int(topic_id) > 0:
            send_kwargs["message_thread_id"] = int(topic_id)
        sent = await bot.send_message(**send_kwargs)
        await bot.pin_chat_message(
            chat_id=chat_id,
            message_id=sent.message_id,
            disable_notification=True,
        )

        if old_message_id and old_message_id != sent.message_id:
            try:
                await bot.unpin_chat_message(
                    chat_id=chat_id,
                    message_id=old_message_id,
                )
            except Exception:
                logger.warning(
                    "Failed to unpin previous managed pin: chat=%s topic=%s message_id=%s",
                    chat_id,
                    topic_id,
                    old_message_id,
                )
            try:
                await bot.delete_message(
                    chat_id=chat_id,
                    message_id=old_message_id,
                )
            except Exception:
                logger.warning(
                    "Failed to delete previous managed pin: chat=%s topic=%s message_id=%s",
                    chat_id,
                    topic_id,
                    old_message_id,
                )

        _save_managed_pin(chat_id, topic_id, cleaned, sent.message_id, entities=entities)
        logger.info(
            "Managed pin updated: chat=%s topic=%s message_id=%s entities=%s",
            chat_id,
            topic_id,
            sent.message_id,
            bool(entities),
        )
        return sent.message_id


async def _managed_pin_scheduler_loop(application: Application) -> None:
    global PIN_LAST_RUN_DATE
    if not PIN_TARGET_CHAT_ID or not PIN_TARGET_TOPIC_ID:
        return

    while True:
        try:
            now = _now_pin_tz()
            run_date = now.strftime("%Y-%m-%d")
            should_run = now.hour == PIN_HOUR and now.minute == PIN_MINUTE
            if should_run and PIN_LAST_RUN_DATE != run_date:
                current = _load_managed_pin(PIN_TARGET_CHAT_ID, PIN_TARGET_TOPIC_ID)
                if current and current.get("disabled"):
                    logger.info("Managed pin is disabled, skipping daily auto-pin")
                    PIN_LAST_RUN_DATE = run_date
                else:
                    text = _load_managed_pin_text(PIN_TARGET_CHAT_ID, PIN_TARGET_TOPIC_ID)
                    entities = _deserialize_entities(
                        (current or {}).get("entities")
                    ) if current else None
                    await _publish_managed_pin(
                        application.bot,
                        chat_id=PIN_TARGET_CHAT_ID,
                        topic_id=PIN_TARGET_TOPIC_ID,
                        text=text,
                        entities=entities,
                    )
                    PIN_LAST_RUN_DATE = run_date
            elif not should_run and PIN_LAST_RUN_DATE == run_date:
                # Keep daily execution idempotent while allowing next day to run.
                pass
        except Exception:
            logger.exception("Managed pin scheduler failed")
        await asyncio.sleep(20)


def _touch_user_activity(
    chat_id: int,
    user_id: int,
    full_name: str = "",
    *,
    ts: Optional[int] = None,
) -> None:
    """Record member activity and clear warn/remind2/kick markers for a new active stretch."""
    now = int(ts if ts is not None else time.time())
    name = (full_name or "").strip()
    conn = sqlite3.connect(MEMORY_DB_PATH)
    try:
        conn.execute(
            """INSERT INTO user_activity(
                   chat_id, user_id, full_name, last_active_at,
                   last_warned_at, muted_at, kick_warned_at, kick_deadline_at, updated_at
               )
               VALUES (?, ?, ?, ?, 0, 0, 0, 0, ?)
               ON CONFLICT(chat_id, user_id) DO UPDATE SET
                   full_name=CASE
                       WHEN excluded.full_name != '' THEN excluded.full_name
                       ELSE user_activity.full_name
                   END,
                   last_active_at=excluded.last_active_at,
                   last_warned_at=0,
                   muted_at=0,
                   kick_warned_at=0,
                   kick_deadline_at=0,
                   updated_at=excluded.updated_at""",
            (int(chat_id), int(user_id), name, now, now),
        )
        conn.commit()
    finally:
        conn.close()


def _load_inactive_users(threshold_ts: int, limit: int) -> list[tuple[int, int, str, int]]:
    """Users inactive since threshold and not yet warned for this inactive stretch.

    Returns: [(chat_id, user_id, full_name, last_active_at), ...]
    """
    conn = sqlite3.connect(MEMORY_DB_PATH)
    try:
        rows = conn.execute(
            """SELECT chat_id, user_id, full_name, last_active_at
               FROM user_activity
               WHERE last_active_at <= ?
                 AND last_warned_at <= last_active_at
                 AND muted_at <= last_active_at
                 AND kick_warned_at <= last_active_at
               ORDER BY last_active_at ASC
               LIMIT ?""",
            (int(threshold_ts), int(limit)),
        ).fetchall()
    finally:
        conn.close()
    return [
        (int(r[0]), int(r[1]), str(r[2] or ""), int(r[3]))
        for r in rows
    ]


def _load_inactivity_remind2_candidates(
    threshold_ts: int, limit: int
) -> list[tuple[int, int, str, int, int]]:
    """First-warned users still silent past 45d threshold, not yet second-reminded.

    DB column muted_at is reused as second-remind timestamp (no permission mute).
    Returns: [(chat_id, user_id, full_name, last_active_at, last_warned_at), ...]
    """
    conn = sqlite3.connect(MEMORY_DB_PATH)
    try:
        rows = conn.execute(
            """SELECT chat_id, user_id, full_name, last_active_at, last_warned_at
               FROM user_activity
               WHERE last_active_at <= ?
                 AND last_warned_at > last_active_at
                 AND muted_at <= last_active_at
                 AND kick_warned_at <= last_active_at
               ORDER BY last_active_at ASC
               LIMIT ?""",
            (int(threshold_ts), int(limit)),
        ).fetchall()
    finally:
        conn.close()
    return [
        (int(r[0]), int(r[1]), str(r[2] or ""), int(r[3]), int(r[4] or 0))
        for r in rows
    ]


# Backward-compatible alias
_load_inactivity_mute_candidates = _load_inactivity_remind2_candidates


def _load_inactivity_kick_warn_candidates(
    threshold_ts: int, limit: int
) -> list[tuple[int, int, str, int, int]]:
    """Second-reminded users still silent past kick threshold, not yet kick-warned.

    muted_at column holds second-remind timestamp.
    Returns: [(chat_id, user_id, full_name, last_active_at, muted_at), ...]
    """
    conn = sqlite3.connect(MEMORY_DB_PATH)
    try:
        rows = conn.execute(
            """SELECT chat_id, user_id, full_name, last_active_at, muted_at
               FROM user_activity
               WHERE last_active_at <= ?
                 AND muted_at > last_active_at
                 AND kick_warned_at <= last_active_at
               ORDER BY last_active_at ASC
               LIMIT ?""",
            (int(threshold_ts), int(limit)),
        ).fetchall()
    finally:
        conn.close()
    return [
        (int(r[0]), int(r[1]), str(r[2] or ""), int(r[3]), int(r[4] or 0))
        for r in rows
    ]


def _load_inactivity_kick_due(now_ts: int, limit: int) -> list[tuple[int, int, str, int, int]]:
    """Kick-warned users whose grace deadline has passed and still no activity.

    Returns: [(chat_id, user_id, full_name, last_active_at, kick_deadline_at), ...]
    """
    conn = sqlite3.connect(MEMORY_DB_PATH)
    try:
        rows = conn.execute(
            """SELECT chat_id, user_id, full_name, last_active_at, kick_deadline_at
               FROM user_activity
               WHERE kick_deadline_at > 0
                 AND kick_deadline_at <= ?
                 AND kick_warned_at > last_active_at
                 AND muted_at > last_active_at
               ORDER BY kick_deadline_at ASC
               LIMIT ?""",
            (int(now_ts), int(limit)),
        ).fetchall()
    finally:
        conn.close()
    return [
        (int(r[0]), int(r[1]), str(r[2] or ""), int(r[3]), int(r[4] or 0))
        for r in rows
    ]


def _mark_inactivity_warned(chat_id: int, user_id: int, *, ts: Optional[int] = None) -> None:
    now = int(ts if ts is not None else time.time())
    conn = sqlite3.connect(MEMORY_DB_PATH)
    try:
        conn.execute(
            """UPDATE user_activity
               SET last_warned_at=?, updated_at=?
               WHERE chat_id=? AND user_id=?""",
            (now, now, int(chat_id), int(user_id)),
        )
        conn.commit()
    finally:
        conn.close()


def _mark_inactivity_remind2(chat_id: int, user_id: int, *, ts: Optional[int] = None) -> None:
    """Record second inactivity reminder timestamp (reuses muted_at column)."""
    now = int(ts if ts is not None else time.time())
    conn = sqlite3.connect(MEMORY_DB_PATH)
    try:
        conn.execute(
            """UPDATE user_activity
               SET muted_at=?, updated_at=?
               WHERE chat_id=? AND user_id=?""",
            (now, now, int(chat_id), int(user_id)),
        )
        conn.commit()
    finally:
        conn.close()


# Backward-compatible alias
_mark_inactivity_muted = _mark_inactivity_remind2


def _mark_inactivity_kick_warned(
    chat_id: int,
    user_id: int,
    *,
    warned_at: Optional[int] = None,
    deadline_at: Optional[int] = None,
) -> None:
    now = int(warned_at if warned_at is not None else time.time())
    deadline = int(
        deadline_at
        if deadline_at is not None
        else now + INACTIVITY_KICK_GRACE_DAYS * 24 * 3600
    )
    conn = sqlite3.connect(MEMORY_DB_PATH)
    try:
        conn.execute(
            """UPDATE user_activity
               SET kick_warned_at=?, kick_deadline_at=?, updated_at=?
               WHERE chat_id=? AND user_id=?""",
            (now, deadline, now, int(chat_id), int(user_id)),
        )
        conn.commit()
    finally:
        conn.close()


def _load_inactivity_remind2(chat_id: int, user_id: int) -> Optional[tuple[int, int, str, int]]:
    """Active second-reminder marker (muted_at > last_active), if any.

    Returns (chat_id, user_id, full_name, muted_at) or None.
    """
    conn = sqlite3.connect(MEMORY_DB_PATH)
    try:
        row = conn.execute(
            """SELECT chat_id, user_id, full_name, muted_at
               FROM user_activity
               WHERE chat_id=? AND user_id=?
                 AND muted_at > last_active_at
                 AND muted_at > 0""",
            (int(chat_id), int(user_id)),
        ).fetchone()
    finally:
        conn.close()
    if not row:
        return None
    return (int(row[0]), int(row[1]), str(row[2] or ""), int(row[3]))


# Backward-compatible alias (no longer means permission mute)
_load_inactivity_mute = _load_inactivity_remind2


def _clear_inactivity_markers(
    chat_id: int,
    user_id: int,
    full_name: str = "",
    *,
    ts: Optional[int] = None,
) -> None:
    """Clear warn/remind2/kick markers and restart activity clock."""
    _touch_user_activity(chat_id, user_id, full_name, ts=ts)


# Backward-compatible alias
_clear_inactivity_mute = _clear_inactivity_markers


def _remove_user_activity(chat_id: int, user_id: int) -> None:
    conn = sqlite3.connect(MEMORY_DB_PATH)
    try:
        conn.execute(
            "DELETE FROM user_activity WHERE chat_id=? AND user_id=?",
            (int(chat_id), int(user_id)),
        )
        conn.commit()
    finally:
        conn.close()


async def _get_admin_user_ids(bot, chat_id: int) -> set[int]:
    try:
        admins = await bot.get_chat_administrators(chat_id)
    except Exception:
        logger.warning("inactivity: failed to load admins chat=%s", chat_id)
        return set()
    out: set[int] = set()
    for a in admins or []:
        user = getattr(a, "user", None)
        if user is not None and getattr(user, "id", None) is not None:
            out.add(int(user.id))
    return out


def _left_member_statuses() -> set:
    return {
        ChatMemberStatus.LEFT,
        ChatMemberStatus.BANNED,
        getattr(ChatMemberStatus, "KICKED", "kicked"),
    }


async def _resolve_inactivity_member(
    bot,
    chat_id: int,
    user_id: int,
    full_name: str,
) -> Optional[tuple[str, object]]:
    """Return (display_name, user_obj) or None if member should be skipped/removed."""
    try:
        member = await bot.get_chat_member(chat_id, user_id)
        status = getattr(member, "status", None)
        user_obj = getattr(member, "user", None)
        if status in _left_member_statuses():
            _remove_user_activity(chat_id, user_id)
            return None
        if user_obj is not None and getattr(user_obj, "is_bot", False):
            _remove_user_activity(chat_id, user_id)
            return None
        if full_name:
            display = full_name
        else:
            display = _user_display_name(user_obj, fallback_id=user_id)
        return display, user_obj
    except Exception:
        return (full_name or f"用户{user_id}"), None


async def _ban_chat_member_kick(bot, chat_id: int, user_id: int) -> None:
    """Kick member (ban then unban so they can rejoin later if invited)."""
    await bot.ban_chat_member(chat_id=chat_id, user_id=user_id)
    try:
        await bot.unban_chat_member(chat_id=chat_id, user_id=user_id, only_if_banned=True)
    except TypeError:
        # older PTB without only_if_banned
        await bot.unban_chat_member(chat_id=chat_id, user_id=user_id)
    except Exception:
        # ban succeeded; unban best-effort (permanent ban is acceptable fallback)
        logger.warning(
            "inactivity_kick: unban after ban failed chat=%s user=%s",
            chat_id,
            user_id,
            exc_info=True,
        )


async def _check_and_kick_due(application: Application) -> None:
    """宽限期到期且仍无发言 → 移出群。"""
    if not INACTIVITY_WARN_ENABLED:
        return

    now = int(time.time())
    candidates = _load_inactivity_kick_due(
        now,
        limit=max(INACTIVITY_MAX_KICKS_PER_CYCLE * 3, INACTIVITY_MAX_KICKS_PER_CYCLE),
    )
    if not candidates:
        return

    kicked = 0
    admin_cache: dict[int, set[int]] = {}
    done_body = INACTIVITY_KICK_DONE_TEXT or "因长期未发言，已移出群组。"

    for chat_id, user_id, full_name, last_active_at, kick_deadline_at in candidates:
        if kicked >= INACTIVITY_MAX_KICKS_PER_CYCLE:
            break
        if BOT_ID and user_id == BOT_ID:
            _remove_user_activity(chat_id, user_id)
            continue

        if chat_id not in admin_cache:
            admin_cache[chat_id] = await _get_admin_user_ids(application.bot, chat_id)
        if user_id in admin_cache[chat_id]:
            _touch_user_activity(chat_id, user_id, full_name)
            continue

        resolved = await _resolve_inactivity_member(
            application.bot, chat_id, user_id, full_name
        )
        if resolved is None:
            continue
        display, _user_obj = resolved
        mention = _html_user_mention(user_id, display)

        try:
            await _ban_chat_member_kick(application.bot, chat_id, user_id)
        except Exception:
            logger.warning(
                "inactivity_kick: ban/kick failed chat=%s user=%s",
                chat_id,
                user_id,
                exc_info=True,
            )
            continue

        text = f"👋 {mention} {done_body}"
        try:
            await application.bot.send_message(
                chat_id=chat_id,
                text=text,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
            )
        except Exception:
            logger.warning(
                "inactivity_kick: notify failed chat=%s user=%s",
                chat_id,
                user_id,
                exc_info=True,
            )

        _remove_user_activity(chat_id, user_id)
        kicked += 1
        logger.info(
            "inactivity_kick: chat=%s user=%s last_active=%s deadline=%s",
            chat_id,
            user_id,
            last_active_at,
            kick_deadline_at,
        )


async def _check_and_kick_warn_inactive(application: Application) -> None:
    """60 天仍未发言（已二次提醒）→ 预告三天后移出。"""
    if not INACTIVITY_WARN_ENABLED:
        return

    threshold = int(time.time()) - INACTIVITY_KICK_DAYS * 24 * 3600
    candidates = _load_inactivity_kick_warn_candidates(
        threshold,
        limit=max(
            INACTIVITY_MAX_KICK_WARNS_PER_CYCLE * 3,
            INACTIVITY_MAX_KICK_WARNS_PER_CYCLE,
        ),
    )
    if not candidates:
        return

    warned = 0
    admin_cache: dict[int, set[int]] = {}
    warn_body = INACTIVITY_KICK_WARN_TEXT or (
        "你已经两个月未发言，将在三天后移出群。请尽快发言以保留群籍。"
    )
    now = int(time.time())
    deadline = now + INACTIVITY_KICK_GRACE_DAYS * 24 * 3600

    for chat_id, user_id, full_name, last_active_at, muted_at in candidates:
        if warned >= INACTIVITY_MAX_KICK_WARNS_PER_CYCLE:
            break
        if BOT_ID and user_id == BOT_ID:
            _remove_user_activity(chat_id, user_id)
            continue

        if chat_id not in admin_cache:
            admin_cache[chat_id] = await _get_admin_user_ids(application.bot, chat_id)
        if user_id in admin_cache[chat_id]:
            _mark_inactivity_kick_warned(
                chat_id, user_id, warned_at=now, deadline_at=now
            )
            continue

        resolved = await _resolve_inactivity_member(
            application.bot, chat_id, user_id, full_name
        )
        if resolved is None:
            continue
        display, _user_obj = resolved
        mention = _html_user_mention(user_id, display)
        text = f"⚠️ {mention} {warn_body}"

        try:
            await application.bot.send_message(
                chat_id=chat_id,
                text=text,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
            )
        except Exception:
            logger.warning(
                "inactivity_kick_warn: send failed chat=%s user=%s",
                chat_id,
                user_id,
                exc_info=True,
            )
            continue

        _mark_inactivity_kick_warned(
            chat_id, user_id, warned_at=now, deadline_at=deadline
        )
        warned += 1
        logger.info(
            "inactivity_kick_warn: chat=%s user=%s last_active=%s muted=%s "
            "deadline=%s grace_days=%s",
            chat_id,
            user_id,
            last_active_at,
            muted_at,
            deadline,
            INACTIVITY_KICK_GRACE_DAYS,
        )


async def _check_and_remind2_inactive(application: Application) -> None:
    """45 天仍未发言（且已 30 天提醒过）→ 再次提醒，不改权限。"""
    if not INACTIVITY_WARN_ENABLED:
        return

    threshold = int(time.time()) - INACTIVITY_REMIND2_DAYS * 24 * 3600
    candidates = _load_inactivity_remind2_candidates(
        threshold,
        limit=max(INACTIVITY_MAX_REMIND2_PER_CYCLE * 3, INACTIVITY_MAX_REMIND2_PER_CYCLE),
    )
    if not candidates:
        return

    reminded = 0
    admin_cache: dict[int, set[int]] = {}
    body = INACTIVITY_REMIND2_TEXT or "你已一个半月未发言，请尽快增加活跃度，否则将被移出群组。"

    for chat_id, user_id, full_name, last_active_at, last_warned_at in candidates:
        if reminded >= INACTIVITY_MAX_REMIND2_PER_CYCLE:
            break
        if BOT_ID and user_id == BOT_ID:
            _remove_user_activity(chat_id, user_id)
            continue

        if chat_id not in admin_cache:
            admin_cache[chat_id] = await _get_admin_user_ids(application.bot, chat_id)
        if user_id in admin_cache[chat_id]:
            # 管理员豁免：标记已处理，避免反复命中
            _mark_inactivity_remind2(chat_id, user_id)
            _mark_inactivity_warned(chat_id, user_id)
            continue

        resolved = await _resolve_inactivity_member(
            application.bot, chat_id, user_id, full_name
        )
        if resolved is None:
            continue
        display, _user_obj = resolved
        mention = _html_user_mention(user_id, display)
        text = f"📣 {mention} {body}"

        try:
            await application.bot.send_message(
                chat_id=chat_id,
                text=text,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
            )
        except Exception:
            logger.warning(
                "inactivity_remind2: send failed chat=%s user=%s",
                chat_id,
                user_id,
                exc_info=True,
            )
            continue

        _mark_inactivity_remind2(chat_id, user_id)
        reminded += 1
        logger.info(
            "inactivity_remind2: chat=%s user=%s last_active=%s warned=%s days=%s",
            chat_id,
            user_id,
            last_active_at,
            last_warned_at,
            INACTIVITY_REMIND2_DAYS,
        )


# Backward-compatible alias
_check_and_mute_inactive = _check_and_remind2_inactive


async def _check_and_warn_inactive(application: Application) -> None:
    if not INACTIVITY_WARN_ENABLED:
        return

    threshold = int(time.time()) - INACTIVITY_DAYS * 24 * 3600
    # Pull extra candidates so admin/bot skips still leave room to warn others.
    candidates = _load_inactive_users(
        threshold,
        limit=max(INACTIVITY_MAX_WARNS_PER_CYCLE * 3, INACTIVITY_MAX_WARNS_PER_CYCLE),
    )
    if not candidates:
        return

    warned = 0
    admin_cache: dict[int, set[int]] = {}
    warn_body = INACTIVITY_WARN_TEXT or "你已一个月未发言，请增加活跃度，否则严重会被移出群组。"

    for chat_id, user_id, full_name, last_active_at in candidates:
        if warned >= INACTIVITY_MAX_WARNS_PER_CYCLE:
            break
        if BOT_ID and user_id == BOT_ID:
            _remove_user_activity(chat_id, user_id)
            continue

        if chat_id not in admin_cache:
            admin_cache[chat_id] = await _get_admin_user_ids(application.bot, chat_id)
        if user_id in admin_cache[chat_id]:
            # Admins are exempt; keep activity row but mark warned so we don't loop.
            _mark_inactivity_warned(chat_id, user_id)
            continue

        resolved = await _resolve_inactivity_member(
            application.bot, chat_id, user_id, full_name
        )
        if resolved is None:
            continue
        display, _user_obj = resolved

        mention = _html_user_mention(user_id, display)
        text = f"📣 {mention} {warn_body}"
        try:
            await application.bot.send_message(
                chat_id=chat_id,
                text=text,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
            )
            _mark_inactivity_warned(chat_id, user_id)
            warned += 1
            logger.info(
                "inactivity_warn: chat=%s user=%s last_active=%s days=%s",
                chat_id,
                user_id,
                last_active_at,
                INACTIVITY_DAYS,
            )
        except Exception:
            logger.warning(
                "inactivity_warn: send failed chat=%s user=%s",
                chat_id,
                user_id,
                exc_info=True,
            )


async def _inactivity_scheduler_loop(application: Application) -> None:
    """后台循环：30 天提醒 → 45 天再提醒 → 60 天最后警告 → 宽限期到期移出。"""
    # 启动后先等一轮，避免刚重启立刻刷屏。
    await asyncio.sleep(min(120, INACTIVITY_CHECK_INTERVAL))
    while True:
        try:
            # 严重程度从高到低
            await _check_and_kick_due(application)
            await _check_and_kick_warn_inactive(application)
            await _check_and_remind2_inactive(application)
            await _check_and_warn_inactive(application)
        except Exception:
            logger.exception("Inactivity scheduler failed")
        await asyncio.sleep(INACTIVITY_CHECK_INTERVAL)


async def _ban_release_scheduler_loop(application: Application) -> None:
    """后台循环：限时 soft ban 到期 → 主动通知已恢复发言，清理旧 ban 提示。

    until_date=0 表示永久 soft ban，只可由超管 /allow 解除，不走本调度器。
    恢复公告约 SOFT_BAN_NOTICE_TTL 秒后自动删除。
    """
    while True:
        try:
            expired = _load_expired_bans()
            for chat_id, user_id, full_name, cmd_msg_id, reply_msg_id in expired:
                display_name = full_name or f"用户{user_id}"
                mention = _html_user_mention(user_id, display_name)
                restore_mid = 0
                try:
                    sent = await application.bot.send_message(
                        chat_id=chat_id,
                        text=f"🔊 {mention} 禁言时间已到，已恢复发言，请谨言慎行！",
                        parse_mode=ParseMode.HTML,
                        disable_web_page_preview=True,
                    )
                    restore_mid = int(getattr(sent, "message_id", 0) or 0)
                except Exception:
                    logger.warning(
                        "ban_release: failed to send message chat=%s user=%s",
                        chat_id,
                        user_id,
                    )
                for mid in (cmd_msg_id, reply_msg_id):
                    if mid > 0:
                        try:
                            await application.bot.delete_message(
                                chat_id=chat_id, message_id=mid
                            )
                        except Exception:
                            pass
                _remove_ban_record(chat_id, user_id)
                _remove_soft_ban_notice(chat_id, user_id)
                _SOFT_BAN_LAST_NOTICE.pop(f"{chat_id}:{user_id}", None)
                _SOFT_BAN_NOTICE_DELETE_TASKS.pop(f"{chat_id}:{user_id}", None)
                if restore_mid:
                    async def _del_restore(cid=chat_id, mid=restore_mid):
                        await asyncio.sleep(SOFT_BAN_NOTICE_TTL)
                        try:
                            await application.bot.delete_message(
                                chat_id=cid, message_id=mid
                            )
                        except Exception:
                            pass
                    application.create_task(_del_restore())
                logger.info(
                    "ban_release: chat=%s user=%s name=%s cleaned=%s,%s restore=%s",
                    chat_id,
                    user_id,
                    display_name,
                    cmd_msg_id,
                    reply_msg_id,
                    restore_mid,
                )
        except Exception:
            logger.exception("Ban release scheduler failed")
        await asyncio.sleep(BAN_CHECK_INTERVAL)


def _is_image_generation_request(raw_text: str) -> bool:
    text = (raw_text or "").strip().lower()
    return text.startswith("img ") or text.startswith("/img ")


def _is_image_edit_request(raw_text: str) -> bool:
    text = (raw_text or "").strip().lower()
    return text.startswith("edit ") or text.startswith("/edit ")


def _clean_image_prompt(raw_text: str) -> str:
    text = (raw_text or "").strip()
    low = text.lower()
    for prefix in ("/edit ", "/img ", "edit ", "img "):
        if low.startswith(prefix):
            return text[len(prefix):].strip()
    return text.strip()


def _message_image_target(msg) -> Optional[str]:
    if not msg:
        return None
    photos = getattr(msg, "photo", None) or []
    if photos:
        return photos[-1].file_id
    doc = getattr(msg, "document", None)
    if doc and (getattr(doc, "mime_type", "") or "").startswith("image/"):
        return doc.file_id
    return None


def _reply_image_target(msg) -> Optional[str]:
    if not _is_reply_to_this_bot(msg):
        return None
    return _message_image_target(getattr(msg, "reply_to_message", None))


def _avscan_reply_image_target(msg) -> Optional[str]:
    """Return the replied image's file ID for /av.

    Unlike image editing, AVScan intentionally accepts an image sent by any
    chat member; the command itself must still be an explicit reply.
    """
    return _message_image_target(getattr(msg, "reply_to_message", None))


def _is_avscan_request(raw_text: str) -> bool:
    """Whether text/caption contains `/av` for this bot (or has no target)."""
    match = AVSCAN_COMMAND_RE.search(raw_text or "")
    if not match:
        return False
    target = match.group("target")
    if not target:
        return True
    return bool(BOT_USERNAME and target.lower() == BOT_USERNAME.lower())


def _av_cover_argument(msg) -> tuple[bool, Optional[str]]:
    """Return whether a text `/av` has an argument and its raw value.

    This accepts an exact text command or an exact image caption command. A
    plain `/av` image caption stays on the existing AVScan path; a command with
    an argument selects the R18.dev cover lookup before any image is read.
    """
    text = _message_prompt_text(msg)
    match = AV_TEXT_COMMAND_RE.fullmatch(text)
    if not match:
        return False, None
    target = match.group("target")
    if target and (not BOT_USERNAME or target.lower() != BOT_USERNAME.lower()):
        return False, None
    argument = match.group("argument")
    return (argument is not None), (argument.strip() if argument else None)


def _avscan_image_source(msg):
    """Return the image file ID and message that /av must delete afterward.

    A photo/document carrying `/av` in its caption searches itself. A plain
    `/av` command searches the image it replies to.
    """
    own_file_id = _message_image_target(msg)
    if own_file_id:
        return own_file_id, msg
    replied = getattr(msg, "reply_to_message", None)
    return _avscan_reply_image_target(msg), replied


def _is_text_ai_prefix(raw_text: str) -> bool:
    text = (raw_text or "").strip().lower()
    return (
        text.startswith("ds ")
        or text.startswith("/ds ")
        or text.startswith("gk ")
        or text.startswith("/gk ")
        or text in {"ds", "/ds", "gk", "/gk"}
    )


def _need_reply(update: Update) -> bool:
    msg = update.effective_message
    if not msg or not msg.text:
        return False

    text = msg.text.strip().lower()
    if _is_text_ai_prefix(text) or _is_bot_mentioned(msg):
        return True

    if msg.reply_to_message:
        replied = msg.reply_to_message
        if replied.from_user and BOT_ID and replied.from_user.id == BOT_ID:
            return _message_image_target(replied) is None
        if (
            replied.from_user
            and BOT_USERNAME
            and (replied.from_user.username or "").lower() == BOT_USERNAME.lower()
        ):
            return _message_image_target(replied) is None

    return False


def _is_bot_mentioned(msg) -> bool:
    if not msg:
        return False
    text = (getattr(msg, "text", None) or "").strip()
    if not text or not BOT_USERNAME:
        return False
    return f"@{BOT_USERNAME.lower()}" in text.lower()


def _is_reply_to_this_bot(msg) -> bool:
    if not msg or not getattr(msg, "reply_to_message", None):
        return False
    replied = msg.reply_to_message
    sender = getattr(replied, "from_user", None)
    if not sender:
        return False
    if BOT_ID and sender.id == BOT_ID:
        return True
    if BOT_USERNAME and (sender.username or "").lower() == BOT_USERNAME.lower():
        return True
    return False


def _clean_prompt(raw_text: str) -> str:
    text = raw_text.strip()
    low = text.lower()
    for prefix in ("/gk ", "gk ", "/ds ", "ds "):
        if low.startswith(prefix):
            text = text[len(prefix):].strip()
            break
    else:
        if low in {"/gk", "gk", "/ds", "ds"}:
            text = ""
    # Strip leading @bot_username mention
    if BOT_USERNAME:
        mention = f"@{BOT_USERNAME.lower()}"
        low_stripped = text.lower()
        if low_stripped.startswith(mention):
            text = text[len(mention):].strip()
    return text.strip()


def _extract_model_name_from_message_text(text: str) -> Optional[str]:
    if not text:
        return None
    candidates = MODEL_LINE_RE.findall(text or "")
    for raw in reversed(candidates):
        name = re.sub(r"\s+", " ", (raw or "").strip())
        name = name.strip(" \t\r\n<>[]()\"'")
        if name:
            return name
    return None


def _inherit_text_model_from_reply(msg) -> Optional[str]:
    if not _is_reply_to_this_bot(msg):
        return None
    replied = getattr(msg, "reply_to_message", None)
    if not replied:
        return None
    replied_text = (getattr(replied, "text", None) or "").strip()
    replied_caption = (getattr(replied, "caption", None) or "").strip()
    reply_source = replied_text or replied_caption
    model_name = _extract_model_name_from_message_text(reply_source)
    if not model_name:
        logger.info("model_inherit: no model line found in replied text")
        return None
    if model_name in {DS_MODEL, AI_THINKING_MODEL, OAI_MODEL, GROK_MODEL}:
        logger.info("model_inherit: inherited model=%s", model_name)
        return model_name
    logger.info("model_inherit: model line found but not allowed model=%s", model_name)
    return None


def _select_text_model(raw_text: str, msg=None) -> str:
    low = (raw_text or "").strip().lower()
    if low.startswith("/gk ") or low.startswith("gk ") or low in {"/gk", "gk"}:
        return GROK_MODEL
    if low.startswith("/ds ") or low.startswith("ds ") or low in {"/ds", "ds"}:
        return DS_MODEL
    inherited = _inherit_text_model_from_reply(msg) if msg is not None else None
    if inherited:
        return inherited
    return DS_MODEL


def _render_quote_text(
    raw_text: str,
    model_name: Optional[str] = None,
    reasoning: Optional[str] = None,
) -> str:
    safe = escape((raw_text or "").strip())
    if not safe:
        safe = "..."
    model_line = f"模型: {escape(model_name or DS_MODEL)}"
    parts = []
    if reasoning:
        r = escape(reasoning.strip())
        parts.append(f"<blockquote expandable>🤔 {r}</blockquote>")
    parts.append(safe)
    parts.append(f"<blockquote>{model_line}</blockquote>")
    return "\n".join(parts)


def _strip_think_blocks(text: str) -> str:
    # Remove hidden reasoning blocks like <think>...</think>.
    # If closing tag hasn't arrived yet, drop content from <think> to the end.
    out = []
    i = 0
    open_tag = "<think>"
    close_tag = "</think>"

    while i < len(text):
        start = text.find(open_tag, i)
        if start == -1:
            out.append(text[i:])
            break
        out.append(text[i:start])
        end = text.find(close_tag, start + len(open_tag))
        if end == -1:
            break
        i = end + len(close_tag)

    return "".join(out)


def _split_visible_and_reasoning(text: str) -> tuple[str, str]:
    """Split inline <think>...</think> content into visible text and reasoning."""
    raw = text or ""
    visible = []
    reasoning = []
    i = 0
    open_tag = "<think>"
    close_tag = "</think>"

    while i < len(raw):
        start = raw.find(open_tag, i)
        if start == -1:
            visible.append(raw[i:])
            break
        visible.append(raw[i:start])
        body_start = start + len(open_tag)
        end = raw.find(close_tag, body_start)
        if end == -1:
            reasoning.append(raw[body_start:])
            break
        reasoning.append(raw[body_start:end])
        i = end + len(close_tag)

    return "".join(visible), "".join(reasoning).strip()


def _format_visible_text(text: str) -> str:
    cleaned, _ = _split_visible_and_reasoning(text or "")

    found = MARKDOWN_IMAGE_RE.findall(cleaned)
    image_refs = []
    for idx, (title, url) in enumerate(found, start=1):
        norm_title = re.sub(r"\s+", " ", (title or "").strip())[:80] or f"图片{idx}"
        image_refs.append((norm_title, url))

    cleaned = MARKDOWN_IMAGE_RE.sub("", cleaned)
    cleaned = HTML_IMG_TAG_RE.sub("", cleaned)

    kept_lines = []
    for line in cleaned.splitlines():
        t = line.strip()
        if not t:
            kept_lines.append("")
            continue
        if IMAGE_URL_FILE_RE.match(t):
            image_refs.append((f"图片{len(image_refs) + 1}", t))
            continue
        kept_lines.append(line)
    cleaned = "\n".join(kept_lines)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()

    if image_refs:
        lines = ["图片参考："]
        for i, (title, url) in enumerate(image_refs, start=1):
            lines.append(f"{i}. {title} - {url}")
        image_block = "\n".join(lines)
        tail_lines = []
        for line in cleaned.splitlines():
            t = EMOJI_RE.sub("", line).strip()
            if not t:
                continue
            if CHATTER_LINE_RE.search(t):
                continue
            tail_lines.append(t)
        cleaned_tail = "\n".join(tail_lines).strip()
        if cleaned_tail:
            brief = cleaned_tail.splitlines()[0][:120]
            return f"{image_block}\n\n{brief}".strip()
        return image_block
    return cleaned


def _memory_key(update: Update):
    msg = update.effective_message
    chat = update.effective_chat
    if not msg or not chat:
        return None
    topic_id = _topic_id_of_message(msg, chat) or "0"
    if msg.from_user:
        actor = f"topic:{topic_id}|user:{msg.from_user.id}"
    elif msg.sender_chat:
        actor = f"topic:{topic_id}|sender_chat:{msg.sender_chat.id}"
    else:
        return None
    return (chat.id, actor)



def _trim_history(history):
    keep = max(1, MEMORY_MAX_TURNS) * 2
    return history[-keep:]


def _init_memory_db() -> None:
    folder = os.path.dirname(MEMORY_DB_PATH)
    if folder:
        os.makedirs(folder, exist_ok=True)

    conn = sqlite3.connect(MEMORY_DB_PATH)
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS chat_memory (
                chat_id INTEGER NOT NULL,
                actor TEXT NOT NULL,
                history_json TEXT NOT NULL,
                updated_at INTEGER NOT NULL,
                PRIMARY KEY (chat_id, actor)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS managed_pins (
                chat_id INTEGER NOT NULL,
                topic_id TEXT NOT NULL,
                pin_text TEXT NOT NULL,
                message_id INTEGER,
                updated_at INTEGER NOT NULL,
                PRIMARY KEY (chat_id, topic_id)
            )
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_managed_pins_updated_at
            ON managed_pins(updated_at)
            """
        )
        # Add disabled column for existing DBs (safe to re-run)
        try:
            conn.execute(
                "ALTER TABLE managed_pins ADD COLUMN disabled INTEGER NOT NULL DEFAULT 0"
            )
        except Exception:
            pass
        # Add pin_entities column for existing DBs (safe to re-run).
        # Stores a JSON array of Telegram MessageEntity dicts so native
        # formatting (bold/italic/link/code...) survives daily re-pins.
        try:
            conn.execute(
                "ALTER TABLE managed_pins ADD COLUMN pin_entities TEXT"
            )
        except Exception:
            pass

        conn.execute(
            """CREATE TABLE IF NOT EXISTS bans (
                chat_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                full_name TEXT NOT NULL DEFAULT '',
                until_date INTEGER NOT NULL,
                cmd_msg_id INTEGER NOT NULL DEFAULT 0,
                reply_msg_id INTEGER NOT NULL DEFAULT 0,
                updated_at INTEGER NOT NULL,
                PRIMARY KEY (chat_id, user_id)
            )"""
        )
        # Add cmd_msg_id / reply_msg_id columns for existing DBs (safe to re-run)
        try:
            conn.execute("ALTER TABLE bans ADD COLUMN cmd_msg_id INTEGER NOT NULL DEFAULT 0")
        except Exception:
            pass
        try:
            conn.execute("ALTER TABLE bans ADD COLUMN reply_msg_id INTEGER NOT NULL DEFAULT 0")
        except Exception:
            pass

        conn.execute(
            """CREATE TABLE IF NOT EXISTS soft_ban_notices (
                chat_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                message_id INTEGER NOT NULL,
                updated_at INTEGER NOT NULL,
                PRIMARY KEY (chat_id, user_id)
            )"""
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_soft_ban_notices_updated"
            " ON soft_ban_notices(updated_at)"
        )

        conn.execute(
            "CREATE TABLE IF NOT EXISTS message_links ("
            "    chat_id INTEGER NOT NULL,"
            "    user_id INTEGER NOT NULL,"
            "    user_msg_id INTEGER NOT NULL,"
            "    bot_msg_id INTEGER NOT NULL,"
            "    updated_at INTEGER NOT NULL,"
            "    PRIMARY KEY (chat_id, user_id, user_msg_id)"
            ")"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_message_links_chat"
            " ON message_links(chat_id, user_id)"
        )

        # 群成员最近发言时间；发言后重置 warn/mute/kick 标记。
        conn.execute(
            """CREATE TABLE IF NOT EXISTS user_activity (
                chat_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                full_name TEXT NOT NULL DEFAULT '',
                last_active_at INTEGER NOT NULL,
                last_warned_at INTEGER NOT NULL DEFAULT 0,
                muted_at INTEGER NOT NULL DEFAULT 0,
                kick_warned_at INTEGER NOT NULL DEFAULT 0,
                kick_deadline_at INTEGER NOT NULL DEFAULT 0,
                updated_at INTEGER NOT NULL,
                PRIMARY KEY (chat_id, user_id)
            )"""
        )
        # 已有库迁移
        for col_sql in (
            "ALTER TABLE user_activity ADD COLUMN muted_at INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE user_activity ADD COLUMN kick_warned_at INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE user_activity ADD COLUMN kick_deadline_at INTEGER NOT NULL DEFAULT 0",
        ):
            try:
                conn.execute(col_sql)
            except Exception:
                pass
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_user_activity_last_active"
            " ON user_activity(last_active_at)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_user_activity_kick_deadline"
            " ON user_activity(kick_deadline_at)"
        )

        conn.commit()
    finally:
        conn.close()


def _load_memory(key):
    chat_id, actor = key
    conn = sqlite3.connect(MEMORY_DB_PATH)
    try:
        row = conn.execute(
            "SELECT history_json FROM chat_memory WHERE chat_id=? AND actor=?",
            (chat_id, actor),
        ).fetchone()
    finally:
        conn.close()

    if not row:
        return []

    try:
        history = json.loads(row[0])
        return history if isinstance(history, list) else []
    except Exception:
        return []


def _save_memory(key, history) -> None:
    chat_id, actor = key
    payload = json.dumps(_trim_history(history), ensure_ascii=False)
    now = int(time.time())
    conn = sqlite3.connect(MEMORY_DB_PATH)
    try:
        conn.execute(
            """
            INSERT INTO chat_memory(chat_id, actor, history_json, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(chat_id, actor)
            DO UPDATE SET
                history_json=excluded.history_json,
                updated_at=excluded.updated_at
            """,
            (chat_id, actor, payload, now),
        )
        conn.commit()
    finally:
        conn.close()


def _clear_memory(key) -> int:
    chat_id, actor = key
    conn = sqlite3.connect(MEMORY_DB_PATH)
    try:
        cur = conn.execute(
            "DELETE FROM chat_memory WHERE chat_id=? AND actor=?",
            (chat_id, actor),
        )
        conn.commit()
        return cur.rowcount or 0
    finally:
        conn.close()


def _save_message_link(chat_id: int, user_id: int, user_msg_id: int, bot_msg_id: int) -> None:
    now = int(time.time())
    conn = sqlite3.connect(MEMORY_DB_PATH)
    try:
        conn.execute(
            "INSERT INTO message_links(chat_id, user_id, user_msg_id, bot_msg_id, updated_at)"
            " VALUES (?, ?, ?, ?, ?)"
            " ON CONFLICT(chat_id, user_id, user_msg_id)"
            " DO UPDATE SET bot_msg_id=excluded.bot_msg_id, updated_at=excluded.updated_at",
            (chat_id, user_id, user_msg_id, bot_msg_id, now),
        )
        conn.commit()
    finally:
        conn.close()


def _load_user_message_links(chat_id: int, user_id: int) -> list[tuple[int, int]]:
    conn = sqlite3.connect(MEMORY_DB_PATH)
    try:
        rows = conn.execute(
            "SELECT user_msg_id, bot_msg_id FROM message_links WHERE chat_id=? AND user_id=? ORDER BY user_msg_id",
            (chat_id, user_id),
        ).fetchall()
    finally:
        conn.close()
    return [(int(r[0]), int(r[1])) for r in rows]


def _load_chat_message_links(chat_id: int) -> list[tuple[int, int, int]]:
    conn = sqlite3.connect(MEMORY_DB_PATH)
    try:
        rows = conn.execute(
            "SELECT user_id, user_msg_id, bot_msg_id FROM message_links WHERE chat_id=? ORDER BY user_msg_id",
            (chat_id,),
        ).fetchall()
    finally:
        conn.close()
    return [(int(r[0]), int(r[1]), int(r[2])) for r in rows]


def _delete_user_message_links(chat_id: int, user_id: int) -> int:
    conn = sqlite3.connect(MEMORY_DB_PATH)
    try:
        cur = conn.execute(
            "DELETE FROM message_links WHERE chat_id=? AND user_id=?", (chat_id, user_id)
        )
        conn.commit()
        return cur.rowcount or 0
    finally:
        conn.close()


def _delete_chat_message_links(chat_id: int) -> int:
    conn = sqlite3.connect(MEMORY_DB_PATH)
    try:
        cur = conn.execute(
            "DELETE FROM message_links WHERE chat_id=?", (chat_id,)
        )
        conn.commit()
        return cur.rowcount or 0
    finally:
        conn.close()


def _load_managed_pin(chat_id: int, topic_id: str) -> Optional[dict]:
    conn = sqlite3.connect(MEMORY_DB_PATH)
    try:
        row = conn.execute(
            """
            SELECT pin_text, message_id, updated_at, disabled, pin_entities
            FROM managed_pins
            WHERE chat_id=? AND topic_id=?
            """,
            (int(chat_id), str(topic_id)),
        ).fetchone()
    finally:
        conn.close()
    if not row:
        return None
    # pin_entities is a JSON array of entity dicts (or NULL for legacy rows)
    entities: Optional[list] = None
    if row[4]:
        try:
            entities = json.loads(row[4])
        except Exception:
            entities = None
    return {
        "pin_text": row[0] or "",
        "message_id": int(row[1]) if row[1] is not None else None,
        "updated_at": int(row[2]) if row[2] is not None else 0,
        "disabled": bool(row[3]) if row[3] is not None else False,
        "entities": entities,
    }


def _serialize_entities(entities) -> Optional[str]:
    """Serialize a list of MessageEntity (or dicts) to a JSON string.

    Returns None when there are no entities so legacy/plain rows stay clean.
    """
    if not entities:
        return None
    out = []
    for ent in entities:
        if isinstance(ent, MessageEntity):
            out.append(ent.to_dict())
        elif isinstance(ent, dict):
            out.append(ent)
        else:
            # Fallback: try to extract a dict
            try:
                out.append(dict(ent))
            except Exception:
                continue
    if not out:
        return None
    return json.dumps(out, ensure_ascii=False)


def _save_managed_pin(
    chat_id: int, topic_id: str, pin_text: str, message_id: Optional[int],
    disabled: bool = False, entities=None,
) -> None:
    now = int(time.time())
    pin_entities_json = _serialize_entities(entities)
    conn = sqlite3.connect(MEMORY_DB_PATH)
    try:
        conn.execute(
            """
            INSERT INTO managed_pins(chat_id, topic_id, pin_text, message_id, updated_at, disabled, pin_entities)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(chat_id, topic_id)
            DO UPDATE SET
                pin_text=excluded.pin_text,
                message_id=excluded.message_id,
                updated_at=excluded.updated_at,
                disabled=excluded.disabled,
                pin_entities=excluded.pin_entities
            """,
            (
                int(chat_id), str(topic_id), (pin_text or "").strip(), message_id,
                now, int(disabled), pin_entities_json,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def _load_managed_pin_text(chat_id: int, topic_id: str) -> str:
    row = _load_managed_pin(chat_id, topic_id)
    if row and row["pin_text"].strip():
        return row["pin_text"].strip()
    return DEFAULT_DAILY_PIN_TEXT


def _deserialize_entities(db_entities) -> Optional[list]:
    """Build a list of MessageEntity objects from stored JSON for re-sending."""
    if not db_entities:
        return None
    try:
        raw = json.loads(db_entities) if isinstance(db_entities, str) else db_entities
    except Exception:
        return None
    if not raw:
        return None
    out = []
    for item in raw:
        try:
            out.append(MessageEntity(**item))
        except Exception:
            continue
    return out or None


def _save_ban_record(chat_id: int, user_id: int, full_name: str, until_date: int, cmd_msg_id: int = 0, reply_msg_id: int = 0) -> None:
    now = int(time.time())
    conn = sqlite3.connect(MEMORY_DB_PATH)
    try:
        conn.execute(
            """INSERT INTO bans(chat_id, user_id, full_name, until_date, cmd_msg_id, reply_msg_id, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(chat_id, user_id)
               DO UPDATE SET
                   full_name=excluded.full_name,
                   until_date=excluded.until_date,
                   cmd_msg_id=excluded.cmd_msg_id,
                   reply_msg_id=excluded.reply_msg_id,
                   updated_at=excluded.updated_at""",
            (chat_id, user_id, full_name, until_date, cmd_msg_id, reply_msg_id, now),
        )
        conn.commit()
    finally:
        conn.close()


def _remove_ban_record(chat_id: int, user_id: int) -> None:
    conn = sqlite3.connect(MEMORY_DB_PATH)
    try:
        conn.execute(
            "DELETE FROM bans WHERE chat_id=? AND user_id=?",
            (chat_id, user_id),
        )
        conn.commit()
    finally:
        conn.close()


def _save_soft_ban_notice(chat_id: int, user_id: int, message_id: int, updated_at: Optional[int] = None) -> None:
    """记录某个 chat/user 当前唯一的 soft ban 提醒消息。"""
    now = int(updated_at if updated_at is not None else time.time())
    conn = sqlite3.connect(MEMORY_DB_PATH)
    try:
        conn.execute(
            """INSERT INTO soft_ban_notices(chat_id, user_id, message_id, updated_at)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(chat_id, user_id)
               DO UPDATE SET message_id=excluded.message_id, updated_at=excluded.updated_at""",
            (int(chat_id), int(user_id), int(message_id), now),
        )
        conn.commit()
    finally:
        conn.close()


def _load_soft_ban_notice(chat_id: int, user_id: int) -> Optional[tuple[int, int]]:
    """返回 (message_id, updated_at)，没有记录时返回 None。"""
    conn = sqlite3.connect(MEMORY_DB_PATH)
    try:
        row = conn.execute(
            "SELECT message_id, updated_at FROM soft_ban_notices WHERE chat_id=? AND user_id=?",
            (int(chat_id), int(user_id)),
        ).fetchone()
    finally:
        conn.close()
    if not row:
        return None
    return int(row[0]), int(row[1])


def _remove_soft_ban_notice(chat_id: int, user_id: int) -> None:
    conn = sqlite3.connect(MEMORY_DB_PATH)
    try:
        conn.execute(
            "DELETE FROM soft_ban_notices WHERE chat_id=? AND user_id=?",
            (int(chat_id), int(user_id)),
        )
        conn.commit()
    finally:
        conn.close()


def _clear_soft_ban_notice_memory(chat_id: int, user_id: int) -> None:
    """清理 DB 与进程内冷却状态。"""
    _remove_soft_ban_notice(chat_id, user_id)
    _SOFT_BAN_LAST_NOTICE.pop(f"{chat_id}:{user_id}", None)
    _SOFT_BAN_NOTICE_DELETE_TASKS.pop(f"{chat_id}:{user_id}", None)


def _load_expired_bans() -> list[tuple[int, int, str, int, int]]:
    """返回已到期的 soft ban 记录（不含永久 until_date=0）。

    [(chat_id, user_id, full_name, cmd_msg_id, reply_msg_id), ...]
    """
    now = int(time.time())
    conn = sqlite3.connect(MEMORY_DB_PATH)
    try:
        rows = conn.execute(
            """SELECT chat_id, user_id, full_name, cmd_msg_id, reply_msg_id
               FROM bans
               WHERE until_date > 0 AND until_date <= ?""",
            (now,),
        ).fetchall()
    finally:
        conn.close()
    return [(int(r[0]), int(r[1]), str(r[2] or ""), int(r[3] or 0), int(r[4] or 0)) for r in rows]


def _is_soft_ban_active(until_date: int, *, now: Optional[int] = None) -> bool:
    """until_date=0 → permanent; otherwise active while until_date > now."""
    if int(until_date) == SOFT_BAN_PERMANENT_UNTIL:
        return True
    ts = int(now if now is not None else time.time())
    return int(until_date) > ts


def _load_active_ban(chat_id: int, user_id: int) -> Optional[tuple[int, int, str, int, int, int]]:
    """查询用户当前是否有生效中的 soft ban:
    (chat_id, user_id, full_name, until_date, cmd_msg_id, reply_msg_id) or None

    until_date=0 表示永久 soft ban。
    """
    now = int(time.time())
    conn = sqlite3.connect(MEMORY_DB_PATH)
    try:
        row = conn.execute(
            "SELECT chat_id, user_id, full_name, until_date, cmd_msg_id, reply_msg_id "
            "FROM bans WHERE chat_id=? AND user_id=?",
            (chat_id, user_id),
        ).fetchone()
    finally:
        conn.close()
    if not row:
        return None
    until_date = int(row[3])
    if not _is_soft_ban_active(until_date, now=now):
        return None
    return (int(row[0]), int(row[1]), str(row[2] or ""), until_date, int(row[4] or 0), int(row[5] or 0))


def _load_active_bans_in_chat(chat_id: int) -> list[tuple[int, int, str, int, int, int]]:
    """当前群所有生效 soft ban 记录。"""
    now = int(time.time())
    conn = sqlite3.connect(MEMORY_DB_PATH)
    try:
        rows = conn.execute(
            "SELECT chat_id, user_id, full_name, until_date, cmd_msg_id, reply_msg_id "
            "FROM bans WHERE chat_id=?",
            (int(chat_id),),
        ).fetchall()
    finally:
        conn.close()
    out = []
    for r in rows:
        until_date = int(r[3])
        if _is_soft_ban_active(until_date, now=now):
            out.append(
                (int(r[0]), int(r[1]), str(r[2] or ""), until_date, int(r[4] or 0), int(r[5] or 0))
            )
    return out


def _clear_all_bans_in_chat(chat_id: int) -> list[tuple[int, int, str, int, int, int]]:
    """删除本群全部 soft ban 记录，返回被清除的生效记录。"""
    active = _load_active_bans_in_chat(chat_id)
    conn = sqlite3.connect(MEMORY_DB_PATH)
    try:
        conn.execute("DELETE FROM bans WHERE chat_id=?", (int(chat_id),))
        conn.commit()
    finally:
        conn.close()
    return active




def _extract_explicit_result_count(prompt: str) -> Optional[int]:
    t = (prompt or "").strip()
    if not t:
        return None
    m = re.search(r"(?:前|最近)?\s*(\d{1,2})\s*条", t)
    if m:
        n = int(m.group(1))
        return max(1, min(50, n))
    m = re.search(r"(?:前|最近)\s*(\d{1,2})(?!\d)", t)
    if m:
        n = int(m.group(1))
        return max(1, min(50, n))
    return None



def _should_web_search(prompt: str) -> bool:
    text = (prompt or "").strip().lower()
    if not text:
        return False
    return any(trigger in text for trigger in WEB_SEARCH_TRIGGERS)


IMAGE_WEB_SEARCH_TRIGGERS = (
    "最新", "新闻", "今天", "现在", "最近", "当前", "现任", "刚发布",
    "发布会", "新款", "新版", "2025", "2026", "2027", "2028", "2029",
    "latest", "recent", "current", "news", "new", "released", "launch",
)


def _should_web_search_image_prompt(prompt: str) -> bool:
    text = _clean_image_prompt(prompt or "")
    low = text.lower()
    if not low:
        return False
    if any(trigger in low for trigger in IMAGE_WEB_SEARCH_TRIGGERS):
        return True
    # Product/model names often require current visual references, e.g. iPhone 18.
    if re.search(r"\b[a-z][a-z0-9-]{1,}\s*(?:\d{2,}|[ivx]{2,})\b", low, re.IGNORECASE):
        return True
    return False


def _enrich_image_prompt_with_web_context(prompt: str, web_context: Optional[str]) -> str:
    cleaned = (prompt or "").strip()
    context_text = (web_context or "").strip()
    if not context_text:
        return cleaned
    return (
        f"{cleaned}\n\n"
        "真实参考资料如下。生成图片时请只把这些资料作为视觉参考，"
        "不要在画面中加入资料来源、URL、水印或解释文字。\n"
        f"{context_text[:3500]}"
    ).strip()


async def _fetch_models_dev_models() -> dict:
    """Fetch provider-agnostic model metadata from models.dev with a short cache."""
    global _MODELS_DEV_CACHE

    now = time.monotonic()
    if (
        MODELS_DEV_CACHE_TTL > 0
        and _MODELS_DEV_CACHE is not None
        and now - _MODELS_DEV_CACHE[0] < MODELS_DEV_CACHE_TTL
    ):
        return _MODELS_DEV_CACHE[1]

    headers = {
        "Accept": "application/json",
        "User-Agent": "anyincubation_bot/1.0",
    }
    async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
        resp = await client.get(MODELS_DEV_MODELS_URL, headers=headers)
        resp.raise_for_status()
        data = resp.json()

    if not isinstance(data, dict):
        raise RuntimeError("models.dev returned an invalid model catalog")

    _MODELS_DEV_CACHE = (time.monotonic(), data)
    return data


async def _fetch_openrouter_models(model_name: Optional[str] = None) -> list[dict]:
    """Fetch OpenRouter model metadata, optionally narrowed to one model."""
    query = re.sub(r"\s+", " ", (model_name or "").strip())
    cache_key = query.lower()
    now = time.monotonic()
    cached = _OPENROUTER_CACHE.get(cache_key)
    if (
        OPENROUTER_CACHE_TTL > 0
        and cached is not None
        and now - cached[0] < OPENROUTER_CACHE_TTL
    ):
        return cached[1]

    headers = {"Accept": "application/json", "User-Agent": "anyincubation_bot/1.0"}
    if OPENROUTER_API_KEY:
        headers["Authorization"] = f"Bearer {OPENROUTER_API_KEY}"

    async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
        if "/" in query:
            author, slug = query.split("/", 1)
            url = (
                f"{OPENROUTER_MODEL_URL}/"
                f"{urllib.parse.quote(author, safe='')}/"
                f"{urllib.parse.quote(slug, safe=':')}"
            )
            resp = await client.get(url, headers=headers)
            if resp.status_code in {401, 403, 404}:
                # Allow OpenRouter aliases to be resolved by the list endpoint.
                resp = await client.get(
                    OPENROUTER_MODELS_URL,
                    headers=headers,
                    params={"q": query, "output_modalities": "text"},
                )
                resp.raise_for_status()
                payload = resp.json()
                models = payload.get("data") if isinstance(payload, dict) else None
            else:
                resp.raise_for_status()
                payload = resp.json()
                model = payload.get("data") if isinstance(payload, dict) else None
                models = [model] if isinstance(model, dict) else []
        else:
            resp = await client.get(
                OPENROUTER_MODELS_URL,
                headers=headers,
                params={"q": query, "output_modalities": "text"} if query else None,
            )
            resp.raise_for_status()
            payload = resp.json()
            models = payload.get("data") if isinstance(payload, dict) else None

    if not isinstance(models, list) or not all(isinstance(item, dict) for item in models):
        raise RuntimeError("OpenRouter returned an invalid model catalog")

    _OPENROUTER_CACHE[cache_key] = (time.monotonic(), models)
    return models


def _find_openrouter_model_context(
    models: list[dict], model_name: str
) -> Optional[tuple[str, int]]:
    """Find a model context limit in OpenRouter metadata."""
    query = re.sub(r"\s+", " ", (model_name or "").strip()).lower()
    if not query or not isinstance(models, list):
        return None

    candidates: list[dict] = []
    for entry in models:
        if not isinstance(entry, dict):
            continue
        identifiers = {
            str(entry.get("id") or "").lower(),
            str(entry.get("canonical_slug") or "").lower(),
        }
        identifiers.discard("")
        matches = query in identifiers
        if "/" not in query:
            matches = matches or any(
                identifier.rsplit("/", 1)[-1] == query
                for identifier in identifiers
            )
        if matches:
            candidates.append(entry)

    for entry in candidates:
        top_provider = entry.get("top_provider")
        provider_context = (
            top_provider.get("context_length")
            if isinstance(top_provider, dict)
            else None
        )
        context_length = entry.get("context_length")
        for value in (provider_context, context_length):
            if (
                isinstance(value, (int, float))
                and not isinstance(value, bool)
                and value > 0
            ):
                return str(entry.get("id") or entry.get("canonical_slug")), int(value)
    return None


def _find_model_context(models: dict, model_name: str) -> Optional[tuple[str, int]]:
    """Find a model's context limit by full ID or unqualified model ID."""
    query = re.sub(r"\s+", " ", (model_name or "").strip()).lower()
    if not query or not isinstance(models, dict):
        return None

    candidates: list[tuple[str, dict]] = []
    for model_id, entry in models.items():
        if not isinstance(model_id, str) or not isinstance(entry, dict):
            continue
        entry_id = str(entry.get("id") or model_id)
        model_id_lower = model_id.lower()
        entry_id_lower = entry_id.lower()
        matches = model_id_lower == query or entry_id_lower == query
        if "/" not in query:
            matches = matches or model_id_lower.rsplit("/", 1)[-1] == query
            matches = matches or entry_id_lower.rsplit("/", 1)[-1] == query
        if matches:
            candidates.append((entry_id, entry))

    if not candidates:
        return None

    # Prefer an entry with a valid context value if duplicate aliases exist.
    for model_id, entry in candidates:
        limit = entry.get("limit")
        context_limit = limit.get("context") if isinstance(limit, dict) else None
        if (
            isinstance(context_limit, (int, float))
            and not isinstance(context_limit, bool)
            and context_limit > 0
        ):
            return model_id, int(context_limit)
    return None


def _extract_duckduckgo_url(href: str) -> str:
    if not href:
        return ""
    href = unescape(href)
    if href.startswith("//"):
        href = "https:" + href
    parsed = urllib.parse.urlparse(href)
    qs = urllib.parse.parse_qs(parsed.query)
    uddg = qs.get("uddg")
    if uddg and uddg[0]:
        return urllib.parse.unquote(uddg[0])
    return href


def _parse_duckduckgo_lite_results(html: str, max_results: int = WEB_SEARCH_MAX_RESULTS) -> list[dict]:
    results = []
    link_re = re.compile(
        r"<a(?=[^>]*class=['\"][^'\"]*result-link[^'\"]*['\"])(?=[^>]*href=['\"]([^'\"]+)['\"])[^>]*>(.*?)</a>",
        re.IGNORECASE | re.DOTALL,
    )
    matches = list(link_re.finditer(html or ""))
    for idx, match in enumerate(matches):
        title = re.sub(r"<[^>]+>", "", match.group(2))
        title = re.sub(r"\s+", " ", unescape(title)).strip()
        url = _extract_duckduckgo_url(match.group(1))
        snippet = ""
        tail_start = match.end()
        tail_end = matches[idx + 1].start() if idx + 1 < len(matches) else len(html or "")
        tail = (html or "")[tail_start:tail_end]
        sm = re.search(r"<td[^>]+class=['\"]result-snippet['\"][^>]*>(.*?)</td>", tail, re.IGNORECASE | re.DOTALL)
        if sm:
            snippet = re.sub(r"<[^>]+>", "", sm.group(1))
            snippet = re.sub(r"\s+", " ", unescape(snippet)).strip()
        if title and url:
            results.append({"title": title, "url": url, "snippet": snippet})
        if len(results) >= max_results:
            break
    return results


async def _web_search(query: str, max_results: int = WEB_SEARCH_MAX_RESULTS) -> list[dict]:
    url = "https://lite.duckduckgo.com/lite/?q=" + urllib.parse.quote(query or "")
    headers = {"User-Agent": "Mozilla/5.0"}
    async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
        resp = await client.get(url, headers=headers)
        resp.raise_for_status()
    return _parse_duckduckgo_lite_results(resp.text, max_results=max_results)


def _format_web_search_context(query: str, results: list[dict]) -> str:
    lines = [
        "WEB_SEARCH_CONTEXT",
        f"Query: {query}",
        "Use the following web search results as current external context. Cite URLs when using them. If results are insufficient, say so.",
    ]
    if not results:
        lines.append("No search results found.")
    for i, item in enumerate(results, start=1):
        content = (item.get("content") or "").strip()
        content_block = f"\n   Content: {content[:WEB_FETCH_MAX_CHARS]}" if content else ""
        lines.append(
            f"{i}. {item.get('title', '').strip()}\n"
            f"   URL: {item.get('url', '').strip()}\n"
            f"   Snippet: {item.get('snippet', '').strip()}"
            f"{content_block}"
        )
    return "\n".join(lines)


def _jina_reader_url(url: str) -> str:
    return "https://r.jina.ai/http://" + (url or "").strip()


def _is_thin_or_blocked_content(content: str) -> bool:
    text = (content or "").strip()
    low = text.lower()
    if len(text) < 300:
        return True
    blocked_markers = (
        "just a moment",
        "enable javascript",
        "security verification",
        "captcha",
        "access denied",
        "403: forbidden",
    )
    return any(marker in low for marker in blocked_markers)


async def _fetch_url_readable(url: str) -> str:
    reader_url = _jina_reader_url(url)
    headers = {"User-Agent": "Mozilla/5.0"}
    async with httpx.AsyncClient(timeout=25, follow_redirects=True) as client:
        resp = await client.get(reader_url, headers=headers)
        resp.raise_for_status()
    content = resp.text.strip()
    if _is_thin_or_blocked_content(content):
        return ""
    return content[:WEB_FETCH_MAX_CHARS]


async def _fetch_web_result_contents(results: list[dict], max_results: int = WEB_FETCH_MAX_RESULTS) -> list[dict]:
    enriched = [dict(item) for item in results]
    for item in enriched[:max_results]:
        url = item.get("url") or ""
        if not url.startswith("http"):
            continue
        try:
            item["content"] = await _fetch_url_readable(url)
            item["fetch_method"] = "jina" if item.get("content") else "snippet_only"
        except Exception:
            logger.exception("web_fetch failed: url=%s", url[:200])
            item["content"] = ""
            item["fetch_method"] = "failed"
    return enriched


def _build_messages(
    prompt: str,
    history,
    system_prompt: str,
    web_context: Optional[str] = None,
    image_data_uri: Optional[str] = None,
):
    messages = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    if web_context:
        messages.append({"role": "system", "content": web_context})
    if image_data_uri:
        user_content = [
            {"type": "image_url", "image_url": {"url": image_data_uri}},
            {"type": "text", "text": prompt},
        ]
        messages.extend([*history, {"role": "user", "content": user_content}])
    else:
        messages.extend([*history, {"role": "user", "content": prompt}])
    if "发言有哪些" in (prompt or "") or "发言记录" in (prompt or ""):
        explicit_count = _extract_explicit_result_count(prompt)
        messages.append(
            {
                "role": "user",
                "content": (
                    (
                        f"如果在问某人在群里的发言，用户已明确要求{explicit_count}条，"
                        f"就取最近{explicit_count}条并按时间从旧到新输出。"
                        if explicit_count
                        else "如果在问某人在群里的发言，若未明确要求条数（如N条/前N条/最近N条），"
                        "就输出你拿到的全部有效记录（最多50条），按时间从旧到新逐条列出。"
                        "孤立数字（例如句尾的“3”）不算条数要求。"
                    )
                ),
            }
        )
    return messages


STREAM_CHUNK_TIMEOUT = 45  # seconds with no data before retrying


async def _ask_ai_stream(messages, model_name: str):
    base = AI_BASE_URL.rstrip("/")
    url = f"{base}/chat/completions"
    headers = {
        "Authorization": f"Bearer {AI_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": model_name,
        "messages": messages,
        "temperature": 0.4,
        "stream": True,
        "tool_choice": "none",
    }

    timeout = httpx.Timeout(connect=10, read=300, write=10, pool=10)
    async with httpx.AsyncClient(timeout=timeout) as client:
        async with client.stream("POST", url, headers=headers, json=payload) as resp:
            if resp.status_code >= 400:
                body = (await resp.aread()).decode("utf-8", errors="ignore")[:500]
                raise RuntimeError(f"AI HTTP {resp.status_code}: {body}")

            last_chunk = time.time()
            async for raw_line in resp.aiter_lines():
                line = (raw_line or "").strip()
                if not line.startswith("data:"):
                    if time.time() - last_chunk > STREAM_CHUNK_TIMEOUT:
                        raise asyncio.TimeoutError("No data chunk received")
                    continue
                data = line[5:].strip()
                if not data or data == "[DONE]":
                    if time.time() - last_chunk > STREAM_CHUNK_TIMEOUT:
                        raise asyncio.TimeoutError("No data chunk received")
                    continue
                last_chunk = time.time()
                try:
                    obj = json.loads(data)
                except json.JSONDecodeError:
                    continue
                choices = obj.get("choices") or []
                if not choices:
                    continue
                delta = choices[0].get("delta") or {}
                reasoning = delta.get("reasoning_content")
                if reasoning:
                    yield ("reasoning", reasoning)
                piece = delta.get("content")
                if piece:
                    yield ("content", piece)


async def _ask_ai_once(messages, model_name: str, temperature: float = 0.2) -> str:
    base = AI_BASE_URL.rstrip("/")
    url = f"{base}/chat/completions"
    headers = {
        "Authorization": f"Bearer {AI_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": model_name,
        "messages": messages,
        "temperature": temperature,
        "stream": False,
    }
    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(url, headers=headers, json=payload)
        if resp.status_code >= 400:
            raise RuntimeError(f"AI HTTP {resp.status_code}: {resp.text[:500]}")
        data = resp.json()
    choices = data.get("choices") or []
    if not choices:
        return ""
    content = (choices[0].get("message") or {}).get("content") or ""
    if isinstance(content, list):
        return " ".join(str(x) for x in content)
    return str(content)


async def _download_imagefree_image(image_url: str) -> bytes:
    """下载 imagefree 返回的 R2 图片直链为 bytes。"""
    if not image_url:
        raise RuntimeError("imagefree 返回了空的 image_url")
    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.get(image_url)
        if resp.status_code >= 400:
            raise RuntimeError(
                f"imagefree 下载图片 HTTP {resp.status_code} from {image_url[:120]}"
            )
        return resp.content


async def _poll_imagefree_task(
    task_url: str,
    timeout: int,
    interval: int,
    on_progress=None,
) -> str:
    """轮询 imagefree 异步任务直到 completed，返回图片 URL。"""
    deadline = time.time() + timeout
    last_status = None
    async with httpx.AsyncClient(timeout=60) as client:
        while time.time() < deadline:
            resp = await client.get(task_url)
            if resp.status_code == 404:
                raise RuntimeError(f"imagefree 任务不存在: {task_url}")
            if resp.status_code >= 400:
                raise RuntimeError(f"imagefree 任务轮询 HTTP {resp.status_code}: {resp.text[:300]}")
            data = resp.json()
            status = (data.get("status") or "").lower()
            if status == "completed":
                return data.get("image_url") or ""
            if status in {"error", "failed"}:
                raise RuntimeError(f"imagefree 任务失败: {str(data.get('error') or data)[:300]}")
            if status != last_status:
                last_status = status
                if on_progress:
                    try:
                        await on_progress(status)
                    except Exception:
                        pass
            await asyncio.sleep(interval)
    raise RuntimeError(f"imagefree 任务超时（{timeout}s）")


async def _generate_image(prompt: str) -> bytes:
    """文生图：imagefree /v1/generate（同步等待出图，典型 20~45 秒）。

    高并发排队超过等待窗口时返回 202 + Location 头，按该地址轮询任务。
    """
    url = f"{IMAGEFREE_BASE_URL}/v1/generate"
    payload = {"prompt": prompt, "aspect_ratio": IMAGE_ASPECT_RATIO}
    if IMAGE_MODEL != "default":
        payload["model"] = IMAGE_MODEL
    last_exc = None
    for attempt in range(IMAGE_GEN_RETRIES + 1):
        try:
            async with httpx.AsyncClient(timeout=IMAGE_GEN_TIMEOUT) as client:
                resp = await client.post(url, json=payload)
                if resp.status_code == 202:
                    location = resp.headers.get("Location")
                    if not location:
                        raise RuntimeError("imagefree 202 响应缺少 Location 头")
                    image_url = await _poll_imagefree_task(
                        urllib.parse.urljoin(f"{IMAGEFREE_BASE_URL}/v1/generate", location),
                        timeout=IMAGE_GEN_TIMEOUT,
                        interval=5,
                    )
                    return await _download_imagefree_image(image_url)
                if resp.status_code >= 400:
                    raise RuntimeError(f"imagefree 文生图 HTTP {resp.status_code}: {resp.text[:500]}")
                data = resp.json()
                if (data.get("status") or "") != "completed":
                    raise RuntimeError(f"imagefree 文生图失败: {str(data)[:300]}")
                return await _download_imagefree_image(data.get("image_url"))
        except Exception as e:
            if _is_timeout_error(e) and attempt < IMAGE_GEN_RETRIES:
                last_exc = e
                logger.warning("image gen timeout attempt %s/%s, retrying", attempt + 1, IMAGE_GEN_RETRIES)
                continue
            raise


async def _edit_image(prompt: str, image_bytes: bytes) -> bytes:
    """图生图：imagefree /v1/edit（异步提交 + 轮询，上游排队约 1~5 分钟）。

    输入图 ≤4MB（data URI 提交）；超限先用 AVScan 的 JPEG 预处理缩小。
    """
    if len(image_bytes) > 4 * 1024 * 1024:
        image_bytes = _prepare_avscan_image(image_bytes)
    mime = "image/png" if image_bytes[:8] == b"\x89PNG\r\n\x1a\n" else "image/jpeg"
    payload = {
        "image": f"data:{mime};base64," + base64.b64encode(image_bytes).decode(),
        "prompt": prompt,
    }
    if IMAGE_EDIT_MODEL != "default":
        payload["model"] = IMAGE_EDIT_MODEL
    last_exc = None
    for attempt in range(IMAGE_GEN_RETRIES + 1):
        try:
            async with httpx.AsyncClient(timeout=60) as client:
                resp = await client.post(f"{IMAGEFREE_BASE_URL}/v1/edit", json=payload)
                if resp.status_code >= 400:
                    raise RuntimeError(f"imagefree 图生图 HTTP {resp.status_code}: {resp.text[:500]}")
                data = resp.json()
                task_id = data.get("id")
                if not task_id:
                    raise RuntimeError(f"imagefree 图生图无任务 id: {str(data)[:300]}")
                image_url = await _poll_imagefree_task(
                    f"{IMAGEFREE_BASE_URL}/v1/edit/tasks/{task_id}",
                    timeout=IMAGE_EDIT_POLL_TIMEOUT,
                    interval=IMAGE_EDIT_POLL_INTERVAL,
                )
                if not image_url:
                    raise RuntimeError("imagefree 图生图完成但无 image_url")
                return await _download_imagefree_image(image_url)
        except Exception as e:
            if _is_timeout_error(e) and attempt < IMAGE_GEN_RETRIES:
                last_exc = e
                logger.warning("image edit timeout attempt %s/%s, retrying", attempt + 1, IMAGE_GEN_RETRIES)
                continue
            raise


def _split_video_prefix(raw_text: str) -> Optional[str]:
    """若文本以 vid 前缀开头，返回去掉前缀后的提示词，否则返回 None。

    兼容群里 /vid@BotName 的写法 —— Telegram 在群中会把命令补成
    /vid@BotName，只匹配 "/vid " 会漏掉。
    """
    text = (raw_text or "").strip()
    if not text:
        return None
    head, _, rest = text.partition(" ")
    head = head.lower().split("@")[0]
    if head in {"/vid", "vid"}:
        return rest.strip()
    return None


def _is_video_request(raw_text: str) -> bool:
    return _split_video_prefix(raw_text) is not None


def _clean_video_prompt(raw_text: str) -> str:
    stripped = _split_video_prefix(raw_text)
    if stripped is not None:
        return stripped
    return (raw_text or "").strip()


def _video_source_image(msg) -> Optional[str]:
    """取图生视频的输入图 file_id。

    与 _reply_image_target 不同：不要求被回复的消息属于 bot 自己 ——
    图生视频允许拿群里任何人发的图当首帧。
    """
    if not msg:
        return None
    own = _message_image_target(msg)
    if own:
        return own
    return _message_image_target(getattr(msg, "reply_to_message", None))


async def _create_video_task(
    prompt: str,
    image_url: Optional[str] = None,
    image_bytes: Optional[bytes] = None,
) -> str:
    """提交视频生成任务，返回 video_id。直连上游 Agnes。

    image_bytes 走 data URL 提交（实测上游接受 data:image/...;base64,...），
    所以不需要公网图床就能做图生视频。上游会自行转存并把 mode 设为 ti2vid。
    """
    headers = {
        "Authorization": f"Bearer {VIDEO_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": VIDEO_MODEL,
        "prompt": prompt,
        "width": VIDEO_WIDTH,
        "height": VIDEO_HEIGHT,
        "num_frames": VIDEO_NUM_FRAMES,
        "frame_rate": VIDEO_FRAME_RATE,
    }
    if image_bytes:
        mime = "image/png" if image_bytes[:8] == b"\x89PNG\r\n\x1a\n" else "image/jpeg"
        payload["image"] = f"data:{mime};base64," + base64.b64encode(image_bytes).decode()
    elif image_url:
        payload["image"] = image_url
    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(f"{VIDEO_BASE_URL}/v1/videos", headers=headers, json=payload)
        if resp.status_code >= 400:
            raise RuntimeError(f"Video create HTTP {resp.status_code}: {resp.text[:500]}")
        data = resp.json()
    vid = data.get("video_id") or data.get("task_id") or data.get("id")
    if not vid:
        raise RuntimeError(f"Video create: no id in response {str(data)[:300]}")
    return vid


async def _poll_video_result(video_id: str, on_progress=None) -> str:
    """轮询任务直到完成，返回视频 URL。

    URL 在响应顶层 `url` 字段 —— 官方文档写的 `metadata.url` 实际是空的。
    """
    headers = {"Authorization": f"Bearer {VIDEO_API_KEY}"}
    url = f"{VIDEO_BASE_URL}/agnesapi?video_id={urllib.parse.quote(video_id)}"
    deadline = time.time() + VIDEO_POLL_TIMEOUT
    last_progress = -1
    async with httpx.AsyncClient(timeout=60) as client:
        while time.time() < deadline:
            resp = await client.get(url, headers=headers)
            if resp.status_code >= 400:
                raise RuntimeError(f"Video poll HTTP {resp.status_code}: {resp.text[:300]}")
            data = resp.json()
            status = (data.get("status") or "").lower()
            progress = data.get("progress") or 0
            if status in {"completed", "succeeded", "success"}:
                video_url = data.get("url") or (data.get("metadata") or {}).get("url")
                if not video_url:
                    raise RuntimeError(f"Video done but no url: {str(data)[:300]}")
                return video_url
            if status in {"failed", "error", "cancelled"}:
                err = data.get("error") or status
                raise RuntimeError(f"Video generation failed: {str(err)[:300]}")
            if on_progress and progress != last_progress:
                last_progress = progress
                try:
                    await on_progress(progress)
                except Exception:
                    pass
            await asyncio.sleep(VIDEO_POLL_INTERVAL)
    raise RuntimeError(f"Video generation timed out after {VIDEO_POLL_TIMEOUT}s")


async def _download_video(url: str) -> bytes:
    async with httpx.AsyncClient(timeout=180, follow_redirects=True) as client:
        resp = await client.get(url)
        if resp.status_code >= 400:
            raise RuntimeError(f"Video download HTTP {resp.status_code}")
        return resp.content


async def _download_telegram_file(
    context: ContextTypes.DEFAULT_TYPE,
    file_id: str,
    *,
    max_bytes: Optional[int] = None,
) -> bytes:
    tg_file = await context.bot.get_file(file_id)
    declared_size = getattr(tg_file, "file_size", None)
    if max_bytes is not None and isinstance(declared_size, int) and declared_size > max_bytes:
        raise ValueError(f"图片文件过大，请发送不超过 {max_bytes // (1024 * 1024)}MB 的图片。")
    data = await tg_file.download_as_bytearray()
    output = bytes(data)
    if max_bytes is not None and len(output) > max_bytes:
        raise ValueError(f"图片文件过大，请发送不超过 {max_bytes // (1024 * 1024)}MB 的图片。")
    return output


class AVScanError(RuntimeError):
    """A safe, user-facing failure from the external AVScan service."""


class AVScanRateLimitedError(AVScanError):
    """AVScan temporarily refused the request due to rate limiting."""


def _prepare_avscan_image(image_bytes: bytes) -> bytes:
    """Validate and reduce a Telegram image before sending it to AVScan.

    The AVScan browser client itself submits a JPEG capped at 1024 pixels on
    its longest side. Matching that behavior keeps payloads under its 8 MB
    limit and avoids sending a full-resolution source image unnecessarily.
    """
    if not image_bytes:
        raise ValueError("图片为空，无法检索。")
    if len(image_bytes) > AVSCAN_MAX_SOURCE_BYTES:
        raise ValueError("图片文件过大，请发送不超过 20MB 的图片。")

    try:
        with Image.open(BytesIO(image_bytes)) as source:
            width, height = source.size
            if width < 1 or height < 1:
                raise ValueError("图片尺寸无效。")
            if width * height > AVSCAN_MAX_IMAGE_PIXELS:
                raise ValueError("图片分辨率过大，请发送较小的截图。")
            source.thumbnail(
                (AVSCAN_MAX_SIDE, AVSCAN_MAX_SIDE), Image.Resampling.LANCZOS
            )
            # Convert after shrinking, not before: a large original screenshot
            # should never make the bot retain a full-resolution RGB copy.
            if source.mode in {"RGBA", "LA"} or "transparency" in source.info:
                rgba = source.convert("RGBA")
                prepared = Image.new("RGB", rgba.size, "white")
                prepared.paste(rgba, mask=rgba.getchannel("A"))
            else:
                prepared = source.convert("RGB")
            output = BytesIO()
            prepared.save(output, format="JPEG", quality=85, optimize=True)
    except ValueError:
        raise
    except (OSError, Image.DecompressionBombError) as exc:
        raise ValueError("无法读取这张图片，请换一张 JPG、PNG 或 WEBP 截图。") from exc

    prepared_bytes = output.getvalue()
    if len(prepared_bytes) > AVSCAN_MAX_UPLOAD_BYTES:
        raise ValueError("处理后的图片仍超过 8MB，请先裁剪或压缩后重试。")
    return prepared_bytes


async def _search_avscan(image_bytes: bytes) -> dict:
    """Upload a prepared JPEG to AVScan's observed browser endpoint."""
    timeout = httpx.Timeout(connect=15, read=AVSCAN_TIMEOUT, write=30, pool=15)
    files = {"file": ("frame.jpg", image_bytes, "image/jpeg")}
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
        response = await client.post(AVSCAN_API_URL, files=files)

    if response.status_code == 429:
        raise AVScanRateLimitedError("AVScan 请求过于频繁")
    if response.status_code >= 400:
        raise AVScanError(f"AVScan HTTP {response.status_code}")
    try:
        payload = response.json()
    except ValueError as exc:
        raise AVScanError("AVScan 返回了无效响应") from exc
    if not isinstance(payload, dict):
        raise AVScanError("AVScan 返回了无效响应")
    return payload


class R18DevError(RuntimeError):
    """A safe, user-facing failure from the R18.dev cover lookup."""


class R18DevNotFoundError(R18DevError):
    """R18.dev has no usable jacket for the requested DVD ID."""


class R18DevRateLimitedError(R18DevError):
    """R18.dev temporarily refused the lookup due to rate limiting."""


def _normalize_av_cover_code(raw_code: str) -> Optional[str]:
    """Return a safe R18.dev DVD ID, or None when `/av` has no valid code."""
    code = (raw_code or "").strip().upper()
    if not AV_COVER_CODE_RE.fullmatch(code):
        return None
    compact = code.replace("-", "").replace("_", "")
    return compact if 3 <= len(compact) <= 48 else None


async def _read_limited_http_body(response: httpx.Response, max_bytes: int) -> bytes:
    """Read a streamed response without allowing an unexpected large body."""
    content_length = response.headers.get("content-length")
    if content_length:
        try:
            if int(content_length) > max_bytes:
                raise R18DevError("response body is too large")
        except ValueError:
            pass

    body = bytearray()
    async for chunk in response.aiter_bytes():
        body.extend(chunk)
        if len(body) > max_bytes:
            raise R18DevError("response body is too large")
    return bytes(body)


def _validate_r18dev_cover_url(cover_url: str) -> str:
    """Accept only the expected direct DMM jacket URL shape."""
    if not isinstance(cover_url, str) or not cover_url:
        raise R18DevError("R18.dev returned an invalid jacket URL")
    try:
        parsed = urllib.parse.urlsplit(cover_url)
        port = parsed.port
    except ValueError as exc:
        raise R18DevError("R18.dev returned an invalid jacket URL") from exc
    if (
        parsed.scheme != "https"
        or (parsed.hostname or "").lower() != R18DEV_COVER_HOST
        or parsed.username
        or parsed.password
        or port not in (None, 443)
        or parsed.query
        or parsed.fragment
        or not parsed.path.startswith("/digital/video/")
        or not parsed.path.lower().endswith(".jpg")
    ):
        raise R18DevError("R18.dev returned an unexpected jacket URL")
    return cover_url


def _r18dev_cover_from_payload(payload: dict) -> tuple[str, str]:
    """Extract a strictly allow-listed DMM jacket URL from R18.dev JSON."""
    images = payload.get("images") if isinstance(payload, dict) else None
    jacket = images.get("jacket_image") if isinstance(images, dict) else None
    cover_url = jacket.get("large2") if isinstance(jacket, dict) else None
    if not isinstance(cover_url, str) or not cover_url:
        raise R18DevNotFoundError("R18.dev returned no jacket")
    cover_url = _validate_r18dev_cover_url(cover_url)

    title = payload.get("title")
    return ((title.strip()[:800] if isinstance(title, str) else ""), cover_url)


async def _lookup_r18dev_cover(dvd_id: str) -> tuple[str, str]:
    """Look up one DVD ID and return its title plus trusted DMM cover URL."""
    lookup_url = R18DEV_LOOKUP_URL.format(
        dvd_id=urllib.parse.quote(dvd_id, safe="")
    )
    timeout = httpx.Timeout(connect=10, read=R18DEV_TIMEOUT, write=15, pool=10)
    headers = {"Accept": "application/json", "User-Agent": R18DEV_USER_AGENT}
    # Direct HTTPS only: do not inherit any environment proxy settings.
    async with httpx.AsyncClient(
        timeout=timeout, follow_redirects=False, trust_env=False
    ) as client:
        async with client.stream("GET", lookup_url, headers=headers) as response:
            if 300 <= response.status_code < 400:
                raise R18DevError(f"R18.dev unexpected redirect {response.status_code}")
            if response.status_code == 404:
                raise R18DevNotFoundError("R18.dev returned 404")
            if response.status_code == 429:
                raise R18DevRateLimitedError("R18.dev rate limited the lookup")
            if response.status_code >= 400:
                raise R18DevError(f"R18.dev HTTP {response.status_code}")
            if "json" not in response.headers.get("content-type", "").lower():
                raise R18DevError("R18.dev did not return JSON")
            body = await _read_limited_http_body(response, R18DEV_MAX_JSON_BYTES)

    try:
        payload = json.loads(body)
    except (TypeError, ValueError) as exc:
        raise R18DevError("R18.dev returned invalid JSON") from exc
    if not isinstance(payload, dict):
        raise R18DevError("R18.dev returned invalid JSON")
    return _r18dev_cover_from_payload(payload)


async def _download_r18dev_cover(cover_url: str) -> bytes:
    """Download the allow-listed DMM jacket without following redirects."""
    cover_url = _validate_r18dev_cover_url(cover_url)
    timeout = httpx.Timeout(connect=10, read=R18DEV_TIMEOUT, write=15, pool=10)
    headers = {"Accept": "image/jpeg", "User-Agent": R18DEV_USER_AGENT}
    # The cover URL was allow-listed above; keep this direct as well.
    async with httpx.AsyncClient(
        timeout=timeout, follow_redirects=False, trust_env=False
    ) as client:
        async with client.stream("GET", cover_url, headers=headers) as response:
            if 300 <= response.status_code < 400:
                raise R18DevError(f"DMM cover unexpected redirect {response.status_code}")
            if response.status_code == 429:
                raise R18DevRateLimitedError("DMM rate limited the cover download")
            if response.status_code >= 400:
                raise R18DevError(f"DMM cover HTTP {response.status_code}")
            if not response.headers.get("content-type", "").lower().startswith("image/"):
                raise R18DevError("DMM did not return an image")
            body = await _read_limited_http_body(response, R18DEV_MAX_COVER_BYTES)

    try:
        with Image.open(BytesIO(body)) as cover:
            width, height = cover.size
            if width < 1 or height < 1 or width * height > R18DEV_MAX_COVER_PIXELS:
                raise R18DevError("DMM returned an invalid cover size")
            if cover.format != "JPEG":
                raise R18DevError("DMM did not return a JPEG cover")
            cover.verify()
    except R18DevError:
        raise
    except (OSError, Image.DecompressionBombError) as exc:
        raise R18DevError("DMM returned an invalid cover image") from exc
    return body


def _r18dev_cover_file(image_bytes: bytes) -> BytesIO:
    bio = BytesIO(image_bytes)
    bio.name = "cover.jpg"
    bio.seek(0)
    return bio


def _avscan_number(value) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    if number != number:  # NaN
        return 0.0
    return max(0.0, min(100.0, number))


def _avscan_timestamp(image_name) -> str:
    """Turn AVScan's `CODE_01-02-03.jpg` frame name into `01:02:03`."""
    base = str(image_name or "").rsplit("/", 1)[-1].rsplit(".", 1)[0]
    _prefix, sep, timestamp = base.rpartition("_")
    if not sep or not re.fullmatch(r"\d{2}-\d{2}-\d{2}", timestamp):
        return "--:--:--"
    return timestamp.replace("-", ":")


def _format_avscan_results(payload: dict, max_results: int = AVSCAN_MAX_RESULTS) -> str:
    """Render the compact, untrusted AVScan JSON response as Telegram HTML."""
    raw_results = payload.get("results") if isinstance(payload, dict) else None
    results = [item for item in raw_results if isinstance(item, dict)] if isinstance(raw_results, list) else []
    if not results:
        return "🔎 <b>AVScan 检索结果</b>\n没有找到可用匹配。换一张更清晰、少水印的截图再试试。"

    limit = max(1, min(10, int(max_results)))
    lines = [
        "🔎 <b>AVScan 检索结果</b>",
        "相似度低于 85% 时仅供参考。",
    ]
    for index, item in enumerate(results[:limit], start=1):
        code = escape(str(item.get("video_code") or "未知番号")[:48])
        similarity = _avscan_number(item.get("best_similarity"))
        raw_frames = item.get("frames")
        frames = [frame for frame in raw_frames if isinstance(frame, dict)] if isinstance(raw_frames, list) else []
        best_frame = max(
            frames,
            key=lambda frame: _avscan_number(frame.get("similarity")),
            default=None,
        )
        timestamp = escape(_avscan_timestamp(
            best_frame.get("image_name") if best_frame else None
        ))
        lines.append(
            f"{index}. <code>{code}</code> — <b>{similarity:.1f}%</b> · {timestamp}"
        )
    return "\n".join(lines)


def _photo_file(image_bytes: bytes) -> BytesIO:
    bio = BytesIO(image_bytes)
    bio.name = "image.png"
    bio.seek(0)
    return bio


async def _delete_messages_later(
    context: ContextTypes.DEFAULT_TYPE, chat_id: int, message_ids, delay_seconds: int
) -> None:
    await asyncio.sleep(delay_seconds)
    for mid in message_ids:
        try:
            await context.bot.delete_message(chat_id=chat_id, message_id=mid)
        except Exception:
            # Ignore already deleted / too old / insufficient rights.
            pass


def _schedule_av_cleanup(context: ContextTypes.DEFAULT_TYPE, chat_id: int, *messages) -> None:
    """Delete AV lookup command, associated media, and response on the global TTL."""
    message_ids = []
    for message in messages:
        message_id = getattr(message, "message_id", None)
        if isinstance(message_id, int) and message_id not in message_ids:
            message_ids.append(message_id)
    if message_ids:
        context.application.create_task(
            _delete_messages_later(
                context,
                chat_id,
                message_ids,
                NOTICE_DELETE_TTL,
            )
        )


async def _reply_not_allowed_and_cleanup(
    msg, context: ContextTypes.DEFAULT_TYPE
) -> None:
    warn = await _reply_text_and_track(msg, _not_allowed_usage_text())
    context.application.create_task(
        _delete_messages_later(
            context,
            msg.chat_id,
            [msg.message_id, warn.message_id],
            NOTICE_DELETE_TTL,
        )
    )


async def _reply_and_cleanup(
    msg,
    context: ContextTypes.DEFAULT_TYPE,
    text: str,
    delay_seconds: int = NOTICE_DELETE_TTL,
    **kwargs,
):
    """回复并在 delay_seconds 后删除命令+回复。默认 30 秒；0 表示立刻删。"""
    sent = await _reply_text_and_track(msg, text, **kwargs)
    context.application.create_task(
        _delete_messages_later(
            context,
            msg.chat_id,
            [msg.message_id, sent.message_id],
            delay_seconds,
        )
    )
    return sent


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    chat = update.effective_chat
    if not msg or not chat:
        return
    uid = msg.from_user.id if msg.from_user else None
    if not _is_private_super_admin(chat, uid):
        if not _is_allowed_chat(chat):
            await _reply_not_allowed_and_cleanup(msg, context)
            return
        if not _is_allowed_topic(msg):
            await _reply_not_allowed_and_cleanup(msg, context)
            return
    await _reply_text_and_track(
        msg,
        "已上线。发送：ds / gk + 空格 + 问题；img + 提示词生成图片；回复图片用 edit + 要求改图；/av 番号查封面，回复图片发 /av 或图片 caption 写 /av 可检索番号。"
    )


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    chat = update.effective_chat
    if not msg or not chat:
        return
    uid = msg.from_user.id if msg.from_user else None
    if not _is_private_super_admin(chat, uid):
        if not _is_allowed_chat(chat):
            await _reply_not_allowed_and_cleanup(msg, context)
            return
        if not _is_allowed_topic(msg):
            await _reply_not_allowed_and_cleanup(msg, context)
            return
    reply = await _reply_text_and_track(
            msg,
            "用法:\n"
            f"/ds 或 ds 你的问题 — 对话 ({DS_MODEL})\n"
            f"/gk 或 gk 你的问题 — 对话 ({GROK_MODEL})\n"
            "/img 或 img 提示词 — 生成图片\n"
            "/edit 或 edit 要求 — 回复图片改图（或上传图+写 caption）\n"
            "/av 番号 — 查询 R18.dev 封面；回复图片发 /av，或图片 caption 写 /av 检索番号（AVScan）\n"
            "/vid 或 vid 描述 — 生成视频（约 3 秒，要等 1-2 分钟）\n"
            "  回复图片或上传图片写 /vid 描述 — 图生视频\n"
            "直接回复文字继续聊 | 回复图片则改图\n"
            "/ip IP地址 — 查 IP 纯净度评分（多源聚合）\n"
            "/whois 域名 — 查域名 WHOIS 信息（注册商/时间/NS/状态）\n"
            "/ping 域名 — 从全球节点测延迟（支持指定 DNS/地区）\n"
            "/http URL — 从全球节点 HTTP 测速（支持指定地区）\n"
            "/context 模型名 — 查询 OpenRouter/models.dev 上的上下文长度\n"
            "/status [模型名] — 查看模型可用率（不带参数=全量；带模型名=查单个/模糊匹配）\n"
            "/new — 清空你在此聊天的记忆\n"
            "/clear — 删除你与 AI 的对话消息\n"
            "/force_new — 强制清空本群所有人上下文（仅超管）\n"
            "/force_clear — 强制删除全群与 AI 的对话消息（仅超管）\n"
            "/stop — 停止自己正在思考中的 AI 会话\n"
            "/force_stop — 强制终止所有正在思考的会话（仅超管）\n"
            "/ban [分钟] [用户ID] — soft ban（删发言，不改权限；默认永久，仅超管）\n"
            "/allow [用户ID] — 解除 soft ban；空发则全群解禁（仅超管）\n"
            "/pin 内容 — 更新每日置顶（仅超管）\n"
            "/unpin — 取消置顶（仅超管）\n"
            "/start — 启动说明"
        )
    # 30秒后自动删除 /help 命令和机器人回复
    asyncio.create_task(_auto_delete_after(msg, reply, context, delay=NOTICE_DELETE_TTL))


async def context_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Query a model's context limit from OpenRouter, then models.dev."""
    msg = update.effective_message
    chat = update.effective_chat
    if not msg or not chat:
        return
    uid = msg.from_user.id if msg.from_user else None
    if not _is_private_super_admin(chat, uid):
        if not _is_allowed_chat(chat):
            await _reply_not_allowed_and_cleanup(msg, context)
            return
        if not _is_allowed_topic(msg):
            await _reply_not_allowed_and_cleanup(msg, context)
            return

    model_name = " ".join(context.args or []).strip()
    if not model_name:
        await _reply_and_cleanup(
            msg,
            context,
            "🧭 用法：/context <模型名>\n例如：/context gpt-5.6-luna",
            NOTICE_DELETE_TTL,
        )
        return

    result = None
    source = ""
    openrouter_error = None
    try:
        openrouter_models = await _fetch_openrouter_models(model_name)
        result = _find_openrouter_model_context(openrouter_models, model_name)
        if result is not None:
            source = "OpenRouter"
    except Exception as e:
        openrouter_error = e
        logger.warning("context_cmd: OpenRouter lookup failed for model=%s: %s", model_name, e)

    models_dev_error = None
    if result is None:
        try:
            models = await _fetch_models_dev_models()
            result = _find_model_context(models, model_name)
            if result is not None:
                source = "models.dev"
        except Exception as e:
            models_dev_error = e
            logger.warning("context_cmd: models.dev lookup failed for model=%s: %s", model_name, e)

    if result is None and models_dev_error is not None:
        logger.error(
            "context_cmd fallback failed for model=%s: OpenRouter=%s; models.dev=%s",
            model_name,
            openrouter_error,
            models_dev_error,
        )
        await _reply_and_cleanup(
            msg,
            context,
            (
                "😵 OpenRouter 和 models.dev 暂时都没响应，稍后再试试吧。"
                if openrouter_error is not None
                else "😵 models.dev 暂时没响应，稍后再试试吧。"
            ),
            NOTICE_DELETE_TTL,
        )
        return

    if result is None:
        await _reply_and_cleanup(
            msg,
            context,
            f"🤔 没找到这个模型，或者暂时没有 context 数据：{model_name}",
            NOTICE_DELETE_TTL,
        )
        return

    model_id, context_limit = result
    await _reply_and_cleanup(
        msg,
        context,
        f"🤖 模型：{model_id}\n📚 上下文：{context_limit} tokens\n🔎 来源：{source}",
        NOTICE_DELETE_TTL,
    )


# ---------- /status 图片渲染 ----------

_STATUS_COLORS = {
    "bg": (13, 17, 23), "card": (22, 27, 34), "border": (48, 54, 61),
    "txt": (230, 237, 243), "sub": (139, 148, 158),
    "green": (63, 185, 80), "yellow": (210, 153, 34), "red": (248, 81, 73),
    "gray": (110, 118, 129), "blue": (88, 166, 255),
}


def _status_font(bold: bool = False):
    """容器 /app/fonts 优先，宿主机 fonts 目录兜底。"""
    name = "MapleMono-NF-CN-Bold.ttf" if bold else "MapleMono-NF-CN-Regular.ttf"
    for base in ("/app/fonts", os.path.expanduser("~/.local/share/fonts")):
        p = os.path.join(base, name)
        if os.path.exists(p):
            return p
    raise FileNotFoundError(f"status font not found: {name}")


def _status_fmt_pct(v: float | None) -> str:
    if v is None:
        return "—"
    s = f"{v:.1f}"
    return s[:-2] if s.endswith(".0") else s


def _status_icon_color(status: str):
    c = _STATUS_COLORS
    return c["green"] if status == "up" else c["red"] if status == "down" else c["yellow"]


def _status_pct_color(v: float | None):
    c = _STATUS_COLORS
    if v is None:
        return c["gray"]
    return c["green"] if v >= 90 else c["yellow"] if v >= 60 else c["red"]


def _render_status_image(data: dict, query: str = "") -> bytes | None:
    """渲染状态卡片 PNG（全量 / 模型查询两种模式）。query 无匹配返回 None。"""
    from io import BytesIO as _BytesIO

    c = _STATUS_COLORS
    W, PAD, ROW_H = 1080, 44, 42
    GROUPS = data.get("groups", [])
    SUMMARY = data.get("summary", {})

    def font(size, bold=False):
        return ImageFont.truetype(_status_font(bold), size)

    def fmt_pct(v):
        return _status_fmt_pct(v)

    # ---------- 构建内容行 ----------
    # rows: ("head", gname) | ("target", name, tup) | ("model", status, mname, up, latest, cnt, paused)
    rows: list[tuple] = []
    matches: list[tuple] = []
    q = query.strip().lower()

    if q:
        for g in GROUPS:
            gname = g.get("group", "默认")
            for t in g.get("targets", []):
                for m in t.get("models", []):
                    name = m.get("name", "")
                    if name.lower() == q:
                        matches.append((gname, t, m))
                    elif q in name.lower():
                        matches.append((gname, t, m))
        if not matches:
            return None
        group_order: list[str] = []
        for gn, _t, _m in matches:
            if gn not in group_order:
                group_order.append(gn)
        for gn in group_order:
            rows.append(("head", gn))
            for g2, t, m in matches:
                if g2 != gn:
                    continue
                okc, tc = m.get("ok_count"), m.get("total_count")
                rows.append(("model", m.get("status", "unknown"), m.get("name", "?"),
                             fmt_pct(m.get("uptime")), fmt_pct(m.get("latest_uptime")),
                             f"{okc}/{tc}" if okc is not None and tc is not None else "",
                             t.get("name", "?"), t.get("paused", False)))
    else:
        for g in GROUPS:
            gname = g.get("group", "默认")
            targets = g.get("targets", [])
            if not targets:
                continue
            rows.append(("head", gname))
            for t in targets:
                models = t.get("models", [])
                if not models:
                    continue
                rows.append(("target", t.get("name", "?"), t.get("uptime"),
                             t.get("paused", False)))
                for m in models:
                    okc, tc = m.get("ok_count"), m.get("total_count")
                    rows.append(("model", m.get("status", "unknown"), m.get("name", "?"),
                                 fmt_pct(m.get("uptime")), fmt_pct(m.get("latest_uptime")),
                                 f"{okc}/{tc}" if okc is not None and tc is not None else "",
                                 "", False))

    # ---------- 布局 ----------
    title_h = 66 if q else 64
    kpi_h = 30 if not q else 0
    body_h = 0
    for r in rows:
        body_h += 34 if r[0] == "head" else ROW_H
    footer_h = 44
    card_h = PAD + title_h + kpi_h + body_h + footer_h + PAD + (10 if q else 0)

    img = Image.new("RGB", (W, card_h), c["bg"])
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([12, 12, W - 12, card_h - 12], radius=18,
                        fill=c["card"], outline=c["border"], width=2)
    y = PAD

    # 标题
    if q:
        d.text((PAD, y), "模型查询", font=font(34, True), fill=c["txt"])
        d.text((PAD, y + 40), q, font=font(26, True), fill=c["blue"])
        tw = d.textlength(f"{len(matches)} 处", font=font(24, True))
        d.text((W - PAD - tw, y + 42), f"{len(matches)} 处", font=font(24, True), fill=c["sub"])
    else:
        d.text((PAD, y), "AI 模型可用率", font=font(34, True), fill=c["txt"])
        avail, total = SUMMARY.get("available", 0), SUMMARY.get("total_models", 0)
        t = f"● {avail}/{total} 在线"
        tw = d.textlength(t, font=font(26, True))
        d.text((W - PAD - tw, y + 6), t, font=font(26, True), fill=c["green"])
        y += title_h
        kpis = []
        if SUMMARY.get("uptime") is not None:
            kpis.append(("综合", fmt_pct(SUMMARY["uptime"])))
        if SUMMARY.get("latest_uptime") is not None:
            kpis.append(("最新", fmt_pct(SUMMARY["latest_uptime"])))
        if SUMMARY.get("abnormal_channels"):
            kpis.append(("异常", str(SUMMARY["abnormal_channels"])))
        if kpis:
            d.text((PAD, y), "  ·  ".join(f"{k} {v}%" for k, v in kpis),
                   font=font(24), fill=c["sub"])
        y += kpi_h + 8
    if q:
        y += title_h + 4

    # 右侧列布局（综合 / 最新 两列，列头画在目标行右侧）
    col_fs = 24
    col_w = d.textlength("100.0%", font=font(col_fs))
    n_cols = 2
    right_xs: list[tuple[float, str]] = []
    x_cursor = W - PAD
    for lb in reversed(["综合", "最新"][:n_cols]):
        x_cursor -= col_w
        right_xs.insert(0, (x_cursor, lb))
        x_cursor -= 36

    def draw_col_headers(yy: float) -> None:
        """列头小字，右对齐到数据列右边缘。"""
        for rx, lb in right_xs:
            tw = d.textlength(lb, font=font(18, True))
            d.text((rx + col_w - tw, yy), lb, font=font(18, True), fill=c["sub"])

    # 内容行
    for r in rows:
        if r[0] == "head":
            d.text((PAD, y), "━ " + r[1], font=font(24, True), fill=c["sub"])
            if q:
                # 查询模式无 target 行，列头画在分组行右侧
                draw_col_headers(y + 6)
            y += 34
        elif r[0] == "target":
            tname, tup, paused = r[1], r[2], r[3]
            tname = ("⏸ " if paused else "") + tname
            d.text((PAD, y + 4), tname, font=font(26, True), fill=c["txt"])
            draw_col_headers(y + 10)
            y += ROW_H
        else:
            status, mname, up, latest, cnt, tgt, paused = r[1], r[2], r[3], r[4], r[5], r[6], r[7]
            cy = y + ROW_H // 2
            d.ellipse([PAD + 2, cy - 7, PAD + 16, cy + 7],
                      fill=_status_icon_color(status))
            x = PAD + 30
            if tgt:
                tgt = ("⏸ " if paused else "") + tgt
                d.text((x, y + 6), tgt, font=font(24, True), fill=c["txt"])
                x += d.textlength(tgt, font=font(24, True)) + 14
                d.text((x, y + 8), "/", font=font(22), fill=c["sub"])
                x += d.textlength("/", font=font(22)) + 14
            d.text((x, y + 6), mname, font=font(24), fill=c["txt"])
            for (rx, lb), v in zip(right_xs, [up, latest]):
                try:
                    col_c = _status_pct_color(float(v))
                except ValueError:
                    col_c = c["gray"]
                d.text((rx, y + 6), v + "%", font=font(col_fs), fill=col_c)
            y += ROW_H

    y += 10
    ga = data.get("generated_at", "")
    try:
        cn = datetime.strptime(ga, "%Y-%m-%d %H:%M:%S").replace(
            tzinfo=ZoneInfo("UTC")).astimezone(ZoneInfo("Asia/Shanghai"))
        ts = cn.strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        ts = ga
    d.text((PAD, y + 4), f"北京时间 {ts}", font=font(20), fill=c["sub"])

    buf = _BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """从 status-monitor 拉取数据，渲染状态卡片图片。用法：/status [模型名]"""
    msg = update.effective_message
    chat = update.effective_chat
    if not msg or not chat:
        return
    uid = msg.from_user.id if msg.from_user else None
    if not _is_private_super_admin(chat, uid):
        if not _is_allowed_chat(chat):
            await _reply_not_allowed_and_cleanup(msg, context)
            return
        if not _is_allowed_topic(msg):
            await _reply_not_allowed_and_cleanup(msg, context)
            return

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(STATUS_API_URL)
            if resp.status_code != 200:
                await _reply_and_cleanup(
                    msg, context, f"❌ status-monitor 返回 {resp.status_code}")
                return
            data = resp.json()
    except Exception as e:
        await _reply_and_cleanup(msg, context, f"❌ 无法连接 status-monitor：{e}")
        return

    query = " ".join(context.args or []).strip()
    try:
        png = _render_status_image(data, query=query)
    except Exception as e:
        logger.error("status_cmd render failed: %s", e, exc_info=True)
        await _reply_and_cleanup(msg, context, f"❌ 渲染状态图片失败：{e}")
        return

    if png is None:
        await _reply_and_cleanup(msg, context, f"🔍 没找到包含「{escape(query)}」的模型")
        return

    sent = await msg.reply_photo(photo=BytesIO(png), filename="status.png")
    # 跟随全局约定：NOTICE_DELETE_TTL 秒后删除命令与图片
    context.application.create_task(
        _delete_messages_later(
            context,
            msg.chat_id,
            [msg.message_id, sent.message_id],
            NOTICE_DELETE_TTL,
        )
    )


async def new_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    chat = update.effective_chat
    if not msg or not chat:
        return
    uid = msg.from_user.id if msg.from_user else None
    if not _is_private_super_admin(chat, uid):
        if not _is_allowed_chat(chat):
            await _reply_not_allowed_and_cleanup(msg, context)
            return
        if not _is_allowed_topic(msg):
            await _reply_not_allowed_and_cleanup(msg, context)
            return
    key = _memory_key(update)
    if not key:
        logger.info("new_cmd: key is None, msg=%s chat=%s", msg, chat)
        await _reply_and_cleanup(msg, context, "无法识别当前会话，清空失败。", NOTICE_DELETE_TTL)
        return

    # Check DB before clear — for debugging
    chat_id, actor = key
    conn = sqlite3.connect(MEMORY_DB_PATH)
    try:
        before = conn.execute(
            "SELECT count(*) FROM chat_memory WHERE chat_id=? AND actor=?",
            (chat_id, actor),
        ).fetchone()[0]
    finally:
        conn.close()
    logger.info("new_cmd key=(chat_id=%s, actor=%r) rows_before=%s", chat_id, actor, before)

    async with MEMORY_LOCKS[key]:
        removed = _clear_memory(key)
    logger.info("new_cmd removed=%s", removed)
    if removed > 0:
        await _reply_and_cleanup(msg, context, "已清空你的上下文记忆。", NOTICE_DELETE_TTL)
    else:
        await _reply_and_cleanup(msg, context, "你当前没有可清空的上下文记忆。", NOTICE_DELETE_TTL)


async def clear_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """删除你与 AI 的对话消息（/clear）。"""
    msg = update.effective_message
    chat = update.effective_chat
    if not msg or not chat:
        return
    uid = msg.from_user.id if msg.from_user else None
    if not _is_private_super_admin(chat, uid):
        if not _is_allowed_chat(chat):
            await _reply_not_allowed_and_cleanup(msg, context)
            return
        if not _is_allowed_topic(msg):
            await _reply_not_allowed_and_cleanup(msg, context)
            return
    if not msg.from_user:
        await _reply_and_cleanup(msg, context, "无法识别发送者。", NOTICE_DELETE_TTL)
        return

    caller_uid = msg.from_user.id
    links = _load_user_message_links(chat.id, caller_uid)
    if not links:
        await _reply_and_cleanup(msg, context, "你当前没有与 AI 的对话消息。", NOTICE_DELETE_TTL)
        return

    # Collect all message IDs to delete
    delete_ids = [mid for pair in links for mid in pair]  # user_msg_id, bot_msg_id
    delete_ids.append(msg.message_id)  # the /clear command itself
    logger.info(
        "clear_cmd: chat=%s user=%s links=%s delete_ids=%s",
        chat.id, caller_uid, len(links), delete_ids,
    )

    # Delete messages in background
    context.application.create_task(
        _delete_messages_later(context, chat.id, delete_ids, 0)
    )

    # Remove links from DB
    _delete_user_message_links(chat.id, caller_uid)

    # Also clear the user's AI memory
    key = _memory_key(update)
    if key:
        async with MEMORY_LOCKS[key]:
            _clear_memory(key)

    await _reply_and_cleanup(
        msg, context, f"✅ 已删除 {len(links)} 条对话记录（{len(delete_ids)} 条消息），并清空记忆。", NOTICE_DELETE_TTL
    )


def _clear_chat_memory(chat_id: int) -> int:
    """Delete all memory rows for a given chat_id. Returns number of rows removed."""
    conn = sqlite3.connect(MEMORY_DB_PATH)
    try:
        cur = conn.execute(
            "DELETE FROM chat_memory WHERE chat_id=?",
            (chat_id,),
        )
        conn.commit()
        return cur.rowcount or 0
    finally:
        conn.close()


async def force_new_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Force-clear ALL context memory for the current chat (super admin only)."""
    msg = update.effective_message
    chat = update.effective_chat
    if not msg or not chat:
        return
    uid = msg.from_user.id if msg.from_user else None
    if uid != SUPER_ADMIN_ID:
        await _reply_and_cleanup(msg, context, "⛔ 无权限触发 /force_new。", NOTICE_DELETE_TTL)
        return

    removed = _clear_chat_memory(chat.id)
    logger.info("force_new_cmd chat_id=%s removed=%s", chat.id, removed)
    if removed > 0:
        await _reply_and_cleanup(msg, context, f"💥 已强制清空本群所有用户上下文（共 {removed} 条记忆）。", NOTICE_DELETE_TTL)
    else:
        await _reply_and_cleanup(msg, context, "当前群没有可清空的记忆。", NOTICE_DELETE_TTL)


async def force_clear_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """删除全群与 AI 的对话消息（超管命令，/force_clear）。"""
    msg = update.effective_message
    chat = update.effective_chat
    if not msg or not chat:
        return
    uid = msg.from_user.id if msg.from_user else None
    if uid != SUPER_ADMIN_ID:
        await _reply_and_cleanup(msg, context, "⛔ 无权限触发 /force_clear。", NOTICE_DELETE_TTL)
        return

    links = _load_chat_message_links(chat.id)
    if not links:
        await _reply_and_cleanup(msg, context, "当前群没有与 AI 的对话消息。", NOTICE_DELETE_TTL)
        return

    # Collect all message IDs: user_msg + bot_msg
    delete_ids = [pair[1] for pair in links] + [pair[2] for pair in links]
    delete_ids.append(msg.message_id)
    logger.info(
        "force_clear_cmd: chat=%s admin=%s links=%s delete_ids=%s",
        chat.id, uid, len(links), delete_ids,
    )

    # Delete messages in background
    context.application.create_task(
        _delete_messages_later(context, chat.id, delete_ids, 0)
    )

    # Remove all links for this chat
    _delete_chat_message_links(chat.id)

    # Also clear ALL memory for this chat
    mem_removed = _clear_chat_memory(chat.id)

    await _reply_and_cleanup(
        msg, context, f"💥 已删除全群 {len(links)} 条对话记录（{len(delete_ids)} 条消息），并清空 {mem_removed} 条记忆。", NOTICE_DELETE_TTL
    )


async def pin_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    chat = update.effective_chat
    if not msg or not chat:
        return

    uid = msg.from_user.id if msg.from_user else None
    if chat.type in {ChatType.GROUP, ChatType.SUPERGROUP, ChatType.CHANNEL}:
        if not _is_allowed_chat(chat):
            await _reply_not_allowed_and_cleanup(msg, context)
            return
        if not _is_allowed_topic(msg):
            await _reply_not_allowed_and_cleanup(msg, context)
            return
    elif chat.type == ChatType.PRIVATE:
        if uid != SUPER_ADMIN_ID:
            return
    if uid != SUPER_ADMIN_ID:
        await _reply_and_cleanup(msg, context, "⛔ 无权限触发 /pin。", NOTICE_DELETE_TTL)
        return

    # Preferred path: reply to a message (with native Telegram formatting) and
    # send /pin. The replied message's text + entities are preserved verbatim.
    pin_text: str = ""
    pin_entities = None
    replied = getattr(msg, "reply_to_message", None)
    if replied is not None:
        reply_text = getattr(replied, "text", None) or getattr(replied, "caption", None)
        if reply_text:
            pin_text = reply_text
            pin_entities = getattr(replied, "entities", None) or getattr(replied, "caption_entities", None)
    if not pin_text:
        # Fallback: use command args, or re-publish the stored pin as-is.
        pin_text = " ".join(context.args).strip() if context.args else ""
    if not pin_text:
        current = _load_managed_pin(PIN_TARGET_CHAT_ID, PIN_TARGET_TOPIC_ID)
        pin_text = (current or {}).get("pin_text") or _load_managed_pin_text(
            PIN_TARGET_CHAT_ID, PIN_TARGET_TOPIC_ID
        )
        if current and current.get("entities"):
            pin_entities = _deserialize_entities(current["entities"])

    try:
        managed_message_id = await _publish_managed_pin(
            context.bot,
            chat_id=PIN_TARGET_CHAT_ID,
            topic_id=PIN_TARGET_TOPIC_ID,
            text=pin_text,
            entities=pin_entities,
        )
    except Exception:
        logger.exception("Manual pin update failed")
        await _reply_and_cleanup(msg, context, "❌ 置顶失败，请检查机器人在目标话题是否有发言和置顶权限。", NOTICE_DELETE_TTL)
        return

    if chat.id == PIN_TARGET_CHAT_ID and (
        str(PIN_TARGET_TOPIC_ID) in ("0", "")
        or (_topic_id_of_message(msg, chat) or "") == str(PIN_TARGET_TOPIC_ID)
    ):
        delete_ids = [msg.message_id]
        replied = getattr(msg, "reply_to_message", None)
        if replied is not None:
            delete_ids.append(replied.message_id)
        context.application.create_task(
            _delete_messages_later(
                context,
                msg.chat_id,
                delete_ids,
                0,
            )
        )
    else:
        await _reply_and_cleanup(
            msg,
            context,
            f"✅ 已更新每日置顶内容，并立即置顶到目标话题。message_id={managed_message_id}", NOTICE_DELETE_TTL,
        )


async def unpin_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    chat = update.effective_chat
    if not msg or not chat:
        return

    uid = msg.from_user.id if msg.from_user else None
    if uid != SUPER_ADMIN_ID:
        if chat.type == ChatType.PRIVATE:
            return
        await _reply_and_cleanup(msg, context, "⛔ 无权限触发 /unpin。", NOTICE_DELETE_TTL)
        return

    current = _load_managed_pin(PIN_TARGET_CHAT_ID, PIN_TARGET_TOPIC_ID)
    if not current or current.get("disabled"):
        await _reply_and_cleanup(
            msg, context, "✅ 已处于未置顶状态，无需重复操作。", NOTICE_DELETE_TTL
        )
        return

    # Unpin and delete the current managed pin message
    old_message_id = current.get("message_id")
    if old_message_id:
        try:
            await context.bot.unpin_chat_message(
                chat_id=PIN_TARGET_CHAT_ID,
                message_id=old_message_id,
            )
        except Exception:
            logger.warning(
                "unpin_cmd: failed to unpin chat=%s message_id=%s",
                PIN_TARGET_CHAT_ID,
                old_message_id,
            )
        try:
            await context.bot.delete_message(
                chat_id=PIN_TARGET_CHAT_ID,
                message_id=old_message_id,
            )
        except Exception:
            logger.warning(
                "unpin_cmd: failed to delete chat=%s message_id=%s",
                PIN_TARGET_CHAT_ID,
                old_message_id,
            )

    # Mark as disabled — scheduler will skip until next /pin
    _save_managed_pin(
        PIN_TARGET_CHAT_ID,
        PIN_TARGET_TOPIC_ID,
        pin_text=current["pin_text"],
        message_id=None,
        disabled=True,
    )

    if chat.id == PIN_TARGET_CHAT_ID and (
        str(PIN_TARGET_TOPIC_ID) in ("0", "")
        or (_topic_id_of_message(msg, chat) or "") == str(PIN_TARGET_TOPIC_ID)
    ):
        context.application.create_task(
            _delete_messages_later(
                context,
                msg.chat_id,
                [msg.message_id],
                0,
            )
        )
    else:
        await _reply_and_cleanup(
            msg, context, "✅ 已取消置顶并停用每日自动置顶，下次 /pin 恢复。", NOTICE_DELETE_TTL,
        )


def _message_prompt_text(msg) -> str:
    return (getattr(msg, "text", None) or getattr(msg, "caption", None) or "").strip()


async def ai_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /ds and /gk commands by forwarding to on_text with prefix restored."""
    msg = update.effective_message
    if not msg:
        return
    cmd = (msg.text or "").split()[0].lstrip("/").split("@")[0].lower()
    if not context.args:
        await _reply_text_and_track(msg, f"请在 /{cmd} 后面写问题。")
        return
    context.user_data["_ds_prefix_text"] = f"{cmd} {' '.join(context.args)}"
    await on_text(update, context)


async def image_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    if not msg:
        return
    cmd = (_message_prompt_text(msg).split()[0] if _message_prompt_text(msg) else "").lstrip("/").split("@")[0].lower()
    prompt = " ".join(context.args).strip() if context.args else _clean_image_prompt(_message_prompt_text(msg))
    if context.user_data is not None:
        context.user_data["_image_prefix_text"] = f"{cmd} {prompt}".strip()
    await on_image_request(update, context)


def _format_r18dev_cover_caption(dvd_id: str, title: str) -> str:
    """Render bounded, escaped R18.dev metadata for Telegram HTML captions."""
    compact_title = " ".join((title or "").split())[:120]
    lines = [
        "🖼 <b>R18.dev 封面</b>",
        f"番号：<code>{escape(dvd_id)}</code>",
    ]
    if compact_title:
        lines.append(escape(compact_title))
    return "\n".join(lines)


async def _av_cover_cmd(
    msg, chat, context: ContextTypes.DEFAULT_TYPE, raw_cover_code: str
) -> None:
    """Fetch one R18.dev jacket for a validated `/av <番号>` command."""
    dvd_id = _normalize_av_cover_code(raw_cover_code)
    if not dvd_id:
        await _reply_and_cleanup(
            msg,
            context,
            "❌ 番号格式无效。用法：<code>/av ABP-001</code>。",
            NOTICE_DELETE_TTL,
            parse_mode=ParseMode.HTML,
        )
        return

    try:
        status = await _reply_text_and_track(msg, "🖼 正在查询 R18.dev 封面...")
    except Exception:
        logger.exception("r18dev status reply failed: chat=%s msg=%s", chat.id, msg.message_id)
        _schedule_av_cleanup(context, chat.id, msg)
        return

    cover_message = None
    try:
        title, cover_url = await _lookup_r18dev_cover(dvd_id)
        image_bytes = await _download_r18dev_cover(cover_url)
        cover_message = await msg.reply_photo(
            photo=_r18dev_cover_file(image_bytes),
            caption=_format_r18dev_cover_caption(dvd_id, title),
            parse_mode=ParseMode.HTML,
        )
        logger.info(
            "r18dev_cover_complete: chat=%s msg=%s cover_bytes=%s",
            chat.id,
            msg.message_id,
            len(image_bytes),
        )
        try:
            await context.bot.delete_message(
                chat_id=status.chat_id, message_id=status.message_id
            )
        except Exception:
            pass
    except R18DevNotFoundError:
        logger.info("r18dev cover not found: chat=%s msg=%s", chat.id, msg.message_id)
        await status.edit_text("🔎 R18.dev 没有找到这个番号的可用封面。")
    except R18DevRateLimitedError:
        logger.warning("r18dev rate limited: chat=%s msg=%s", chat.id, msg.message_id)
        await status.edit_text("⏳ R18.dev 当前请求过多，请稍后再试。")
    except Exception as exc:
        logger.warning(
            "r18dev cover lookup failed: chat=%s msg=%s error=%s",
            chat.id,
            msg.message_id,
            type(exc).__name__,
        )
        try:
            await status.edit_text("❌ R18.dev 封面服务暂时不可用，请稍后再试。")
        except Exception:
            logger.exception("r18dev failure reply edit failed: chat=%s msg=%s", chat.id, msg.message_id)
    finally:
        _schedule_av_cleanup(context, chat.id, msg, status, cover_message)


async def av_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Look up a DVD jacket by code, or search a replied/captioned image via AVScan."""
    msg = update.effective_message
    chat = update.effective_chat
    if not msg or not chat:
        return
    # The generic image-caption path may see `/av@other_bot`, too. Ignore it;
    # normal CommandHandler dispatch already limits text commands to this bot.
    if getattr(msg, "caption", None) and not _is_avscan_request(_message_prompt_text(msg)):
        return

    has_cover_argument, raw_cover_code = _av_cover_argument(msg)
    # A code lookup has no source image. In particular, do not delete an
    # unrelated image merely because the `/av <番号>` command replies to it.
    file_id, source_message = (None, None)
    if not has_cover_argument:
        file_id, source_message = _avscan_image_source(msg)

    uid = msg.from_user.id if msg.from_user else None
    if chat.type not in {ChatType.GROUP, ChatType.SUPERGROUP, ChatType.CHANNEL}:
        if not _is_private_super_admin(chat, uid):
            return
    if not _is_private_super_admin(chat, uid):
        if not _is_allowed_chat(chat) or not _is_allowed_topic(msg):
            warning = await _reply_text_and_track(msg, _not_allowed_usage_text())
            _schedule_av_cleanup(context, chat.id, source_message, msg, warning)
            return

    if has_cover_argument:
        await _av_cover_cmd(msg, chat, context, raw_cover_code or "")
        return

    if not file_id:
        await _reply_and_cleanup(
            msg,
            context,
            (
                "用法：<code>/av 番号</code> 查询 R18.dev 封面；"
                "或<b>回复一张图片</b>后发送 <code>/av</code>，"
                "或发送图片时在 caption 写 <code>/av</code>。"
            ),
            NOTICE_DELETE_TTL,
            parse_mode=ParseMode.HTML,
        )
        return

    try:
        status = await _reply_text_and_track(msg, "🔎 AVScan 检索中...")
    except Exception:
        logger.exception("avscan status reply failed: chat=%s msg=%s", chat.id, msg.message_id)
        _schedule_av_cleanup(context, chat.id, source_message, msg)
        return
    try:
        source_bytes = await _download_telegram_file(
            context,
            file_id,
            max_bytes=AVSCAN_MAX_SOURCE_BYTES,
        )
        upload_bytes = _prepare_avscan_image(source_bytes)
        payload = await _search_avscan(upload_bytes)
        result_text = _format_avscan_results(payload)
        await status.edit_text(
            result_text,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )
        raw_results = payload.get("results")
        result_count = len(raw_results) if isinstance(raw_results, list) else 0
        logger.info(
            "avscan_response_complete: chat=%s msg=%s results=%s upload_bytes=%s",
            chat.id,
            msg.message_id,
            result_count,
            len(upload_bytes),
        )
    except ValueError as exc:
        logger.info("avscan invalid image: chat=%s msg=%s reason=%s", chat.id, msg.message_id, exc)
        await status.edit_text(f"❌ {escape(str(exc))}", parse_mode=ParseMode.HTML)
    except AVScanRateLimitedError:
        logger.warning("avscan rate limited: chat=%s msg=%s", chat.id, msg.message_id)
        await status.edit_text("⏳ AVScan 当前请求过多，请稍后再试。")
    except Exception:
        logger.exception("avscan request failed: chat=%s msg=%s", chat.id, msg.message_id)
        await status.edit_text("❌ AVScan 服务暂时不可用，请稍后再试。")
    finally:
        # AVScan queries may expose sensitive media. Honor the bot-wide cleanup
        # TTL for all three items: source image, command/caption, and response.
        _schedule_av_cleanup(context, chat.id, source_message, msg, status)


async def on_image_request(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    chat = update.effective_chat
    if not msg or not chat:
        return

    # CommandHandler only recognizes message.text. Caption commands need to be
    # caught here, so `/av` works both as a photo caption and as a reply.
    if _is_avscan_request(_message_prompt_text(msg)):
        await av_cmd(update, context)
        return
    # /vid 分流：PTB 的 CommandHandler 只认 message.text，图片 caption 里的
    # /vid 不会触发 video_cmd，所以在这里接住转过去。
    if _is_video_request(_message_prompt_text(msg)):
        await video_cmd(update, context)
        return
    if chat.type not in {ChatType.GROUP, ChatType.SUPERGROUP, ChatType.CHANNEL}:
        if not _is_private_super_admin(chat, msg.from_user.id if msg.from_user else None):
            return
    override_text = context.user_data.pop("_image_prefix_text", None) if context.user_data is not None else None
    raw_text = override_text or _message_prompt_text(msg)
    is_generation = _is_image_generation_request(raw_text)
    is_edit = _is_image_edit_request(raw_text)
    reply_image_file_id = _reply_image_target(msg)
    own_image_file_id = _message_image_target(msg)

    if not _is_private_super_admin(chat, msg.from_user.id if msg.from_user else None):
        if not _is_allowed_chat(chat) or not _is_allowed_topic(msg):
            if is_generation or is_edit or reply_image_file_id:
                await _reply_not_allowed_and_cleanup(msg, context)
            return

    if not (is_generation or is_edit or reply_image_file_id or (own_image_file_id and is_edit)):
        # Forward ds/gk + photo to text conversation
        raw_lower = raw_text.strip().lower()
        if (
            (raw_lower.startswith("ds ") or raw_lower.startswith("gk ") or raw_lower.startswith("/ds ") or raw_lower.startswith("/gk "))
            and own_image_file_id
        ):
            context.user_data["_image_file_id"] = own_image_file_id
            context.user_data["_ds_prefix_text"] = raw_text
            await on_text(update, context)
        return

    prompt = _clean_image_prompt(raw_text)
    if not prompt:
        await _reply_text_and_track(msg, "请写清楚要画什么，或要怎么改图。")
        return

    image_prompt = prompt
    if _should_web_search_image_prompt(prompt):
        try:
            search_status = await _reply_text_and_track(msg, "先查资料，再画图...")
            web_results = await _web_search(prompt, WEB_SEARCH_MAX_RESULTS)
            web_results = await _fetch_web_result_contents(web_results, WEB_FETCH_MAX_RESULTS)
            web_context = _format_web_search_context(prompt, web_results)
            image_prompt = _enrich_image_prompt_with_web_context(prompt, web_context)
            fetched_count = sum(1 for item in web_results if item.get("content"))
            logger.info(
                "image_web_search: query=%r results=%s fetched=%s",
                prompt[:120],
                len(web_results),
                fetched_count,
            )
            try:
                await context.bot.delete_message(
                    chat_id=search_status.chat_id,
                    message_id=search_status.message_id,
                )
            except Exception:
                pass
        except Exception:
            logger.exception("image web_search failed; continuing with original prompt")

    edit_file_id = None
    if is_edit:
        edit_file_id = own_image_file_id or reply_image_file_id
    elif reply_image_file_id and not is_generation:
        edit_file_id = reply_image_file_id

    if is_edit and not edit_file_id:
        await _reply_text_and_track(msg, "请回复一张图片，或上传图片并在 caption 里写 /edit 修改要求。")
        return

    status = await _reply_text_and_track(msg, "改图中..." if edit_file_id else "画图中...")
    try:
        if edit_file_id:
            source_bytes = await _download_telegram_file(context, edit_file_id)
            image_bytes = await _edit_image(image_prompt, source_bytes)
            model_name = f"imagefree/{IMAGE_EDIT_MODEL}"
        else:
            image_bytes = await _generate_image(image_prompt)
            model_name = f"imagefree/{IMAGE_MODEL}"
        caption = f"模型: {model_name}\n提示词: {prompt[:850]}"
        await msg.reply_photo(photo=_photo_file(image_bytes), caption=caption)
        try:
            await context.bot.delete_message(chat_id=status.chat_id, message_id=status.message_id)
        except Exception:
            pass
        logger.info(
            "image_response_complete: chat=%s msg=%s mode=%s model=%s bytes=%s",
            chat.id,
            msg.message_id,
            "edit" if edit_file_id else "generate",
            model_name,
            len(image_bytes),
        )
    except Exception:
        logger.exception("image request failed")
        try:
            await context.bot.edit_message_text(
                chat_id=status.chat_id,
                message_id=status.message_id,
                text="图片服务暂时不可用，请稍后再试。",
            )
        except Exception:
            await _reply_text_and_track(msg, "图片服务暂时不可用，请稍后再试。")


async def video_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """生成视频。用法：/vid <描述>"""
    msg = update.effective_message
    chat = update.effective_chat
    if not msg or not chat:
        return

    uid = msg.from_user.id if msg.from_user else None
    if not _is_private_super_admin(chat, uid):
        if not _is_allowed_chat(chat) or not _is_allowed_topic(msg):
            await _reply_not_allowed_and_cleanup(msg, context)
            return

    if not VIDEO_API_KEY:
        await _reply_text_and_track(msg, "视频功能没配置好（缺 VIDEO_API_KEY）。")
        return

    override_text = context.user_data.pop("_video_prefix_text", None) if context.user_data is not None else None
    prompt = _clean_video_prompt(override_text or _message_prompt_text(msg))
    if not prompt:
        await _reply_text_and_track(
            msg,
            "用法:\n"
            "`/vid <描述>` — 文生视频\n"
            "回复一张图片写 `/vid <描述>`，或上传图片 caption 写 `/vid <描述>` — 图生视频",
        )
        return

    source_file_id = _video_source_image(msg)

    seconds = VIDEO_NUM_FRAMES / VIDEO_FRAME_RATE
    mode_label = "图生视频" if source_file_id else "生成视频"
    status = await _reply_text_and_track(
        msg, f"{mode_label}中... 约 {seconds:.1f} 秒时长，通常要等 1-2 分钟。"
    )

    async def on_progress(progress: int) -> None:
        try:
            await context.bot.edit_message_text(
                chat_id=status.chat_id,
                message_id=status.message_id,
                text=f"生成视频中... {progress}%",
            )
        except Exception:
            pass

    try:
        source_bytes = None
        if source_file_id:
            source_bytes = await _download_telegram_file(context, source_file_id)
        video_id = await _create_video_task(prompt, image_bytes=source_bytes)
        logger.info(
            "video_task_created: chat=%s id=%s mode=%s prompt=%r",
            chat.id, video_id, "i2v" if source_bytes else "t2v", prompt[:120],
        )
        video_url = await _poll_video_result(video_id, on_progress=on_progress)
        video_bytes = await _download_video(video_url)

        buf = BytesIO(video_bytes)
        buf.name = "video.mp4"
        mode_tag = " (图生视频)" if source_bytes else ""
        caption = f"模型: {VIDEO_MODEL}{mode_tag}\n提示词: {prompt[:850]}"
        await msg.reply_video(video=buf, caption=caption, supports_streaming=True)
        try:
            await context.bot.delete_message(chat_id=status.chat_id, message_id=status.message_id)
        except Exception:
            pass
        logger.info(
            "video_response_complete: chat=%s msg=%s id=%s bytes=%s",
            chat.id, msg.message_id, video_id, len(video_bytes),
        )
    except Exception:
        logger.exception("video request failed")
        try:
            await context.bot.edit_message_text(
                chat_id=status.chat_id,
                message_id=status.message_id,
                text="视频生成失败，请稍后再试。",
            )
        except Exception:
            await _reply_text_and_track(msg, "视频生成失败，请稍后再试。")


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    chat = update.effective_chat
    if not msg or not chat:
        return

    if chat.type not in {ChatType.GROUP, ChatType.SUPERGROUP, ChatType.CHANNEL}:
        if not _is_private_super_admin(chat, msg.from_user.id if msg.from_user else None):
            return

    # ai_cmd sets this override when handling /ds or /gk
    _ds_override = context.user_data.pop("_ds_prefix_text", None)
    _msg_text = _ds_override if _ds_override else (msg.text or "")

    if not _is_allowed_chat(chat) and not _is_private_super_admin(chat, msg.from_user.id if msg.from_user else None):
        raw = _msg_text.strip().lower()
        maybe_trigger = (
            _is_text_ai_prefix(raw)
            or _is_bot_mentioned(msg)
            or (
                msg.reply_to_message
                and msg.reply_to_message.from_user
                and BOT_ID
                and msg.reply_to_message.from_user.id == BOT_ID
            )
        )
        if maybe_trigger:
            await _reply_not_allowed_and_cleanup(msg, context)
        return

    raw_text = _msg_text.strip()

    if not _is_allowed_topic(msg) and not _is_private_super_admin(chat, msg.from_user.id if msg.from_user else None):
        maybe_trigger = (
            _is_text_ai_prefix(raw_text)
            or _is_bot_mentioned(msg)
            or (
                msg.reply_to_message
                and msg.reply_to_message.from_user
                and BOT_ID
                and msg.reply_to_message.from_user.id == BOT_ID
            )
        )
        if maybe_trigger:
            await _reply_not_allowed_and_cleanup(msg, context)
        return
    text_system_prompt = SYSTEM_PROMPT

    if not _need_reply(update):
        return

    # 并发限制：最多 MAX_CONCURRENT_DS 个请求同时处理
    global _ACTIVE_DS
    async with _ACTIVE_DS_LOCK:
        if _ACTIVE_DS >= MAX_CONCURRENT_DS:
            await _reply_text_and_track(msg, "🔄 当前请求过多，请稍后重试。")
            return
        _ACTIVE_DS += 1
    current_task = asyncio.current_task() or None
    if current_task:
        _ACTIVE_AI_TASKS.add(current_task)
        if msg.from_user:
            _ACTIVE_USER_TASKS[msg.from_user.id].add(current_task)

    # Handle image from on_image_request (ds + photo)
    image_file_id = context.user_data.pop("_image_file_id", None) if context.user_data is not None else None
    image_data_uri = None
    if image_file_id:
        try:
            image_bytes = await _download_telegram_file(context, image_file_id)
            image_b64 = base64.b64encode(image_bytes).decode()
            image_data_uri = "data:image/png;base64," + image_b64
        except Exception:
            logger.exception("Failed to download image for text conversation")

    logger.info(
        "on_text: text_flow chat=%s msg=%s",
        chat.id,
        msg.message_id,
    )

    user_prompt = _clean_prompt(_msg_text)
    if not user_prompt:
        await _reply_text_and_track(msg, "请给我一个具体问题。")
        if current_task:
            _ACTIVE_AI_TASKS.discard(current_task)
            _ACTIVE_USER_TASKS.get(msg.from_user.id if msg.from_user else 0, set()).discard(current_task)
        async with _ACTIVE_DS_LOCK:
            _ACTIVE_DS -= 1
        return
    text_model = _select_text_model(_msg_text, msg)

    reply = await _reply_text_and_track(
        msg,
        _render_quote_text("思考中...", text_model),
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
    )
    full_text = ""
    reasoning_text = ""
    last_push = 0.0
    key = _memory_key(update)

    if not key:
        await _edit_message_text_retry(
            context,
            chat_id=reply.chat_id,
            message_id=reply.message_id,
            text=_render_quote_text("无法识别发送者，无法建立上下文记忆。"),
            parse_mode=ParseMode.HTML,
        )
        if current_task:
            _ACTIVE_AI_TASKS.discard(current_task)
            _ACTIVE_USER_TASKS.get(msg.from_user.id if msg.from_user else 0, set()).discard(current_task)
        async with _ACTIVE_DS_LOCK:
            _ACTIVE_DS -= 1
        return

    web_context = None
    if _should_web_search(user_prompt):
        try:
            await _edit_message_text_retry(
                context,
                chat_id=reply.chat_id,
                message_id=reply.message_id,
                text=_render_quote_text("联网搜索中...", text_model),
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
            )
            web_results = await _web_search(user_prompt, WEB_SEARCH_MAX_RESULTS)
            web_results = await _fetch_web_result_contents(web_results, WEB_FETCH_MAX_RESULTS)
            web_context = _format_web_search_context(user_prompt, web_results)
            fetched_count = sum(1 for item in web_results if item.get("content"))
            logger.info(
                "web_search: query=%r results=%s fetched=%s model=%s",
                user_prompt[:120],
                len(web_results),
                fetched_count,
                text_model,
            )
        except Exception:
            logger.exception("web_search failed; continuing without web context")
            web_context = _format_web_search_context(user_prompt, [])

    async with MEMORY_LOCKS[key]:
        history = _load_memory(key)
        messages = _build_messages(
            user_prompt,
            history,
            text_system_prompt,
            web_context=web_context,
            image_data_uri=image_data_uri,
        )

    stream_done = False
    for attempt in range(AI_STREAM_TIMEOUT_RETRIES + 1):
        if attempt > 0:
            logger.warning(
                "AI stream timeout, retrying (%s/%s)",
                attempt,
                AI_STREAM_TIMEOUT_RETRIES,
            )
            full_text = ""
            reasoning_text = ""
            last_push = 0.0
            await asyncio.sleep(0.5)
        try:
            async for kind, piece in _ask_ai_stream(messages, text_model):
                if kind == "reasoning":
                    reasoning_text += piece
                    display = _render_quote_text(
                        "思考中...",
                        text_model,
                        reasoning=reasoning_text[:2000],
                    )
                else:
                    full_text += piece
                    visible_source, inline_reasoning = _split_visible_and_reasoning(full_text)
                    merged_reasoning = reasoning_text + inline_reasoning
                    visible_text = _format_visible_text(visible_source)
                    display = _render_quote_text(
                        visible_text[:3800],
                        text_model,
                        reasoning=merged_reasoning[:2000] if merged_reasoning else None,
                    )

                now = time.time()
                if now - last_push >= 0.8:
                    await _edit_message_text_retry(
                        context,
                        chat_id=reply.chat_id,
                        message_id=reply.message_id,
                        text=display,
                        parse_mode=ParseMode.HTML,
                        disable_web_page_preview=True,
                    )
                    last_push = now
            stream_done = True
            break
        except Exception as e:
            if _is_timeout_error(e) and attempt < AI_STREAM_TIMEOUT_RETRIES:
                continue
            logger.exception("AI request failed")
            await _edit_message_text_retry(
                context,
                chat_id=reply.chat_id,
                message_id=reply.message_id,
                text=_render_quote_text("AI 服务暂时不可用，请稍后再试。"),
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
            )
            if current_task:
                _ACTIVE_AI_TASKS.discard(current_task)
                _ACTIVE_USER_TASKS.get(msg.from_user.id if msg.from_user else 0, set()).discard(current_task)
            async with _ACTIVE_DS_LOCK:
                _ACTIVE_DS -= 1
            return

    if not stream_done:
        await _edit_message_text_retry(
            context,
            chat_id=reply.chat_id,
            message_id=reply.message_id,
            text=_render_quote_text("AI 服务暂时不可用，请稍后再试。"),
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )
        if current_task:
            _ACTIVE_AI_TASKS.discard(current_task)
            _ACTIVE_USER_TASKS.get(msg.from_user.id if msg.from_user else 0, set()).discard(current_task)
        async with _ACTIVE_DS_LOCK:
            _ACTIVE_DS -= 1
        return

    answer_text = full_text
    logger.info("text_raw_full_text: len=%s repr=%s", len(answer_text), repr(answer_text[:200]))
    final_visible_source, inline_reasoning = _split_visible_and_reasoning(answer_text)
    final_reasoning = reasoning_text + inline_reasoning
    final_visible = _format_visible_text(final_visible_source).strip()
    final_text = (final_visible or "我暂时没有生成到有效内容，请再试一次。")[:3800]
    logger.info(
        "text_response_complete: model=%s content_len=%s reasoning_len=%s",
        text_model,
        len(final_text),
        len(final_reasoning),
    )
    await _edit_message_text_retry(
        context,
        chat_id=reply.chat_id,
        message_id=reply.message_id,
        text=_render_quote_text(
            final_text,
            text_model,
            reasoning=final_reasoning[:2000] if final_reasoning else None,
        ),
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
    )

    async with MEMORY_LOCKS[key]:
        fresh_history = _load_memory(key)
        updated = [
            *fresh_history,
            {"role": "user", "content": user_prompt},
            {"role": "assistant", "content": final_text},
        ]
        _save_memory(key, updated)

    # Track message IDs for /clear and /force_clear
    if msg.from_user and reply:
        _save_message_link(chat.id, msg.from_user.id, msg.message_id, reply.message_id)

    async with _ACTIVE_DS_LOCK:
        _ACTIVE_DS -= 1
    if current_task:
        _ACTIVE_AI_TASKS.discard(current_task)
        _ACTIVE_USER_TASKS.get(msg.from_user.id if msg.from_user else 0, set()).discard(current_task)


async def _resolve_target_user(
    msg, chat, context: ContextTypes.DEFAULT_TYPE
):
    """解析目标用户。优先级：回复 > tg://user?id= > text_mention"""
    # 1. 回复的目标（唯一最可靠的方式）
    if msg.reply_to_message and msg.reply_to_message.from_user:
        return msg.reply_to_message.from_user

    raw_text = (msg.text or msg.caption or "")

    # 2. tg://user?id=xxx（从实体或纯文本中提取）
    if msg.entities:
        for ent in msg.entities:
            if ent.type in ("url", "text_link"):
                entity_text = msg.parse_entity(ent)
                m = TG_USER_URL_RE.search(entity_text)
                if m:
                    uid = int(m.group(1))
                    try:
                        member = await context.bot.get_chat_member(chat.id, uid)
                        if member and member.user:
                            return member.user
                    except Exception:
                        pass

    m = TG_USER_URL_RE.search(raw_text)
    if m:
        uid = int(m.group(1))
        try:
            member = await context.bot.get_chat_member(chat.id, uid)
            if member and member.user:
                return member.user
        except Exception:
            pass

    # 3. text_mention（极少情况，某些客户端粘贴用户名时产生）
    if msg.entities:
        for ent in msg.entities:
            if ent.type == "text_mention" and ent.user:
                return ent.user

    # 4. 从 args 提取用户 ID
    # 规则：/ban 10 8733956596 → minutes=10, user_id=8733956596
    #       /ban 8733956596    → 单一大数字(>=100000), 当作 user_id
    if context.args:
        args = context.args
        try:
            int(args[0])
            start = 1  # 第一个是分钟数，跳过
        except ValueError:
            start = 0  # 第一个不是数字，从 0 开始

        # 单参数且是大数字 → 当作 user_id
        if len(args) == 1:
            try:
                uid = int(args[0])
                if uid >= 100000:
                    member = await context.bot.get_chat_member(chat.id, uid)
                    if member and member.user:
                        return member.user
            except Exception:
                pass
        else:
            # 多参数，从 start 往后找数字作为 user_id
            for arg in args[start:]:
                try:
                    uid = int(arg)
                    member = await context.bot.get_chat_member(chat.id, uid)
                    if member and member.user:
                        return member.user
                except Exception:
                    pass

    return None


def _activity_ts_from_message(msg) -> int:
    dt = getattr(msg, "date", None)
    if dt is not None:
        try:
            return int(dt.timestamp())
        except Exception:
            pass
    return int(time.time())


def _is_soft_ban_protected_user(user_id: int) -> bool:
    """Bot 自身与超管不可被 soft ban（误操作防护）。群管理员不在此列。"""
    uid = int(user_id)
    if SUPER_ADMIN_ID and uid == int(SUPER_ADMIN_ID):
        return True
    if BOT_ID and uid == int(BOT_ID):
        return True
    return False


def _format_soft_ban_remaining(until_date: int, *, now: Optional[int] = None) -> str:
    """人类可读的剩余 soft ban 时长。"""
    if int(until_date) == SOFT_BAN_PERMANENT_UNTIL:
        return "永久"
    ts = int(now if now is not None else time.time())
    left = max(0, int(until_date) - ts)
    if left <= 0:
        return "即将解除"
    if left < 60:
        return f"{left} 秒"
    minutes = left // 60
    seconds = left % 60
    if minutes < 60:
        return f"{minutes} 分 {seconds} 秒" if seconds else f"{minutes} 分钟"
    hours = minutes // 60
    minutes = minutes % 60
    if hours < 48:
        return f"{hours} 小时 {minutes} 分" if minutes else f"{hours} 小时"
    days = hours // 24
    hours = hours % 24
    return f"{days} 天 {hours} 小时" if hours else f"{days} 天"


def _schedule_delete_messages(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    message_ids,
    delay_seconds: int = NOTICE_DELETE_TTL,
) -> None:
    """定时删除一组消息（命令 / 公告 / 再发言提醒）。"""
    ids = [int(m) for m in message_ids if m]
    if not ids:
        return
    context.application.create_task(
        _delete_messages_later(context, chat_id, ids, delay_seconds)
    )


async def enforce_soft_ban(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Soft ban: 被 /ban 的用户一发言就立刻删消息，并 @ 提醒剩余时长。"""
    msg = update.effective_message
    chat = update.effective_chat
    if not msg or not chat:
        return
    if chat.type not in {ChatType.GROUP, ChatType.SUPERGROUP}:
        return
    if not _is_allowed_chat(chat):
        return

    user = msg.from_user
    if not user or getattr(user, "is_bot", False):
        return
    if _is_soft_ban_protected_user(user.id):
        # 残留名单也不对超管/bot 删消息
        if _load_active_ban(chat.id, user.id):
            _remove_ban_record(chat.id, user.id)
            _remove_soft_ban_notice(chat.id, user.id)
        return

    active = _load_active_ban(chat.id, user.id)
    if not active:
        return

    until_date = active[3]
    display = _user_display_name(user, fallback_id=user.id)

    # 先删对方消息
    try:
        await context.bot.delete_message(chat_id=chat.id, message_id=msg.message_id)
        logger.info(
            "soft_ban_delete: chat=%s user=%s msg=%s until=%s",
            chat.id,
            user.id,
            msg.message_id,
            until_date,
        )
    except Exception:
        logger.warning(
            "soft_ban_delete failed chat=%s user=%s msg=%s",
            chat.id,
            user.id,
            getattr(msg, "message_id", None),
            exc_info=True,
        )

    # 编辑冷却（秒），避免 tp 被限
    key = f"{chat.id}:{user.id}"
    now_ts = int(time.time())
    last = _SOFT_BAN_LAST_NOTICE.get(key, 0)
    if now_ts - last < SOFT_BAN_REPEAT_COOLDOWN:
        return
    _SOFT_BAN_LAST_NOTICE[key] = now_ts

    mention = _html_user_mention(user.id, display)
    remaining = _format_soft_ban_remaining(until_date)

    notice = (
        f"🔇 {mention} 你已被禁言（剩余 {remaining}），"
        f"请勿重复发送消息。消息已被删除。"
    )

    old = _load_soft_ban_notice(chat.id, user.id)
    if old:
        # 编辑已有提醒，让剩余时间实时更新
        mid = old[0]
        try:
            await context.bot.edit_message_text(
                chat_id=chat.id,
                message_id=mid,
                text=notice,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
            )
            _save_soft_ban_notice(chat.id, user.id, mid, now_ts)
            # 取消旧定时删除任务，重新安排（延长 TTL 从最新编辑时刻算起）
            old_task = _SOFT_BAN_NOTICE_DELETE_TASKS.pop(key, None)
            if old_task and hasattr(old_task, 'cancel'):
                old_task.cancel()
            new_task = context.application.create_task(
                _delete_messages_later(context, chat.id, [mid], SOFT_BAN_NOTICE_TTL)
            )
            _SOFT_BAN_NOTICE_DELETE_TASKS[key] = new_task
            return
        except Exception:
            # 编辑失败（消息已被定时删除等），退到发新消息
            _remove_soft_ban_notice(chat.id, user.id)

    # 没有旧提醒或编辑失败，发新消息
    try:
        sent = await context.bot.send_message(
            chat_id=chat.id,
            text=notice,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )
        _save_soft_ban_notice(chat.id, user.id, sent.message_id, now_ts)
        # 取消旧任务（如果有残留）
        old_task = _SOFT_BAN_NOTICE_DELETE_TASKS.pop(key, None)
        if old_task and hasattr(old_task, 'cancel'):
            old_task.cancel()
        new_task = context.application.create_task(
            _delete_messages_later(context, chat.id, [sent.message_id], SOFT_BAN_NOTICE_TTL)
        )
        _SOFT_BAN_NOTICE_DELETE_TASKS[key] = new_task
    except Exception:
        logger.warning(
            "soft_ban_repeat_notice failed chat=%s user=%s",
            chat.id,
            user.id,
            exc_info=True,
        )


async def track_activity(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Silently record any group member activity for inactivity warnings.

    Runs in handler group=-1 so it always fires, independent of AI reply logic.
    Soft-banned users still get activity clock updates if their message was seen
    before deletion; they are not permission-muted.
    """
    if not INACTIVITY_WARN_ENABLED:
        return

    msg = update.effective_message
    chat = update.effective_chat
    if not msg or not chat:
        return
    if chat.type not in {ChatType.GROUP, ChatType.SUPERGROUP}:
        return
    if not _is_allowed_chat(chat):
        return

    user = msg.from_user
    if not user or getattr(user, "is_bot", False):
        return
    if BOT_ID and user.id == BOT_ID:
        return

    try:
        _touch_user_activity(
            chat.id,
            user.id,
            user.full_name or "",
            ts=_activity_ts_from_message(msg),
        )
    except Exception:
        logger.exception(
            "track_activity failed chat=%s user=%s",
            chat.id,
            user.id,
        )


async def on_chat_member(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Track joins / clean up leaves for inactivity; drop soft ban when user leaves."""
    result = update.chat_member or update.my_chat_member
    chat = update.effective_chat
    if not result or not chat:
        return
    if chat.type not in {ChatType.GROUP, ChatType.SUPERGROUP}:
        return
    if not _is_allowed_chat(chat):
        return

    user = getattr(result, "new_chat_member", None)
    user_obj = getattr(user, "user", None) if user is not None else None
    if user is None or user_obj is None:
        return
    if getattr(user_obj, "is_bot", False):
        return

    new_status = getattr(user, "status", None)
    old = getattr(result, "old_chat_member", None)
    old_status = getattr(old, "status", None) if old is not None else None

    left_statuses = _left_member_statuses()
    active_statuses = {
        ChatMemberStatus.MEMBER,
        ChatMemberStatus.ADMINISTRATOR,
        ChatMemberStatus.OWNER,
        getattr(ChatMemberStatus, "RESTRICTED", "restricted"),
        getattr(ChatMemberStatus, "CREATOR", "creator"),
    }

    try:
        if new_status in left_statuses:
            if INACTIVITY_WARN_ENABLED:
                _remove_user_activity(chat.id, user_obj.id)
            # 离群清 soft ban，避免重新入群仍被静默删消息
            if _load_active_ban(chat.id, user_obj.id):
                _remove_ban_record(chat.id, user_obj.id)
                _remove_soft_ban_notice(chat.id, user_obj.id)
                _SOFT_BAN_LAST_NOTICE.pop(f"{chat.id}:{user_obj.id}", None)
                _SOFT_BAN_NOTICE_DELETE_TASKS.pop(f"{chat.id}:{user_obj.id}", None)
                logger.info(
                    "soft_ban_cleared_on_leave: chat=%s user=%s",
                    chat.id,
                    user_obj.id,
                )
            return

        if not INACTIVITY_WARN_ENABLED:
            return

        # Treat join / rejoin / status restore as activity so the clock starts now.
        if new_status in active_statuses and old_status in left_statuses | {None}:
            _touch_user_activity(chat.id, user_obj.id, user_obj.full_name or "")
        elif new_status in active_statuses and old_status not in active_statuses:
            _touch_user_activity(chat.id, user_obj.id, user_obj.full_name or "")
    except Exception:
        logger.exception(
            "on_chat_member activity update failed chat=%s user=%s",
            chat.id,
            getattr(user_obj, "id", None),
        )


async def ban_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Soft ban：不改 TG 权限，目标用户一发言就删消息。

    用法：/ban [分钟数] [用户ID]
    - 默认永久 soft ban（直到超管 /allow）
    - 传分钟数做限时，例如回复某人后 /ban 10 → 10 分钟
    - 0/forever/perm/permanent/永久 或超大分钟 → 永久
    - 仅超管；需 bot 有删消息权限
    - 命令与机器人相关提醒约 30 秒后自动删除
    """
    msg = update.effective_message
    chat = update.effective_chat

    if not msg or not chat:
        return

    uid = msg.from_user.id if msg.from_user else None
    if uid != SUPER_ADMIN_ID:
        await _reply_and_cleanup(
            msg, context, "⛔ 无权限触发 /ban，仅超管可用。", SOFT_BAN_NOTICE_TTL
        )
        return

    if chat.type not in {ChatType.GROUP, ChatType.SUPERGROUP}:
        if not _is_private_super_admin(chat, uid):
            await _reply_and_cleanup(
                msg, context, "此命令仅限群组使用。", SOFT_BAN_NOTICE_TTL
            )
            return

    # 解析 soft ban 时长：默认永久（直到 /allow）；可传分钟数做限时
    ban_minutes = 0
    permanent = True
    if context.args:
        raw0 = (context.args[0] or "").strip().lower()
        if raw0 in {"0", "forever", "perm", "permanent", "永久"}:
            permanent = True
        else:
            try:
                val = int(context.args[0])
                # 单一大数字更像 user_id，不当分钟
                if len(context.args) == 1 and val >= 100000:
                    permanent = True
                elif val <= 0 or val >= 525600:
                    permanent = True
                else:
                    permanent = False
                    ban_minutes = max(1, val)
            except (ValueError, IndexError):
                permanent = True

    target_user = await _resolve_target_user(msg, chat, context)
    if not target_user:
        await _reply_and_cleanup(
            msg,
            context,
            "请回复一条消息来 soft ban 该用户。\n"
            "用法:\n"
            "• 回复某人 /ban → 永久\n"
            "• 回复某人 /ban 10 → 10 分钟\n"
            "• /ban 10 [用户ID] 或 tg://user?id=xxx\n"
            "效果：不改权限，对方发言会被机器人立刻删除。",
            SOFT_BAN_NOTICE_TTL,
        )
        return

    if _is_soft_ban_protected_user(target_user.id):
        if BOT_ID and target_user.id == BOT_ID:
            await _reply_and_cleanup(
                msg, context, "❌ 不能 soft ban 机器人自己。", SOFT_BAN_NOTICE_TTL
            )
        else:
            await _reply_and_cleanup(
                msg, context, "❌ 不能 soft ban 超管。", SOFT_BAN_NOTICE_TTL
            )
        return

    if permanent:
        until_date = SOFT_BAN_PERMANENT_UNTIL
    else:
        until_date = int(time.time()) + ban_minutes * 60

    target_name = _user_display_name(target_user)
    target_mention = _html_user_mention(target_user.id, target_name)

    try:
        admins = await context.bot.get_chat_administrators(chat.id)
        for a in admins:
            if a.user.id == target_user.id:
                logger.info(
                    "soft_ban: target is group admin chat=%s user=%s",
                    chat.id,
                    target_user.id,
                )
                break
    except Exception:
        pass

    if permanent:
        notice = (
            f"🔇 已 soft ban {target_mention}（永久）。"
            f"对方发言将被立刻删除，/allow 解除。"
        )
    else:
        notice = (
            f"🔇 已 soft ban {target_mention} {ban_minutes} 分钟。"
            f"对方发言将被立刻删除。"
        )

    ban_reply = await _reply_text_and_track(
        msg,
        notice,
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
    )
    reply_mid = ban_reply.message_id if ban_reply else 0
    # 命令 + 成功公告约半分钟后删除（限时 ban 到期仍会再发恢复提醒）
    _schedule_delete_messages(
        context, chat.id, [msg.message_id, reply_mid], SOFT_BAN_NOTICE_TTL
    )
    # 记录里不再依赖长期保留 cmd/reply id（已定时删）
    _save_ban_record(
        chat.id,
        target_user.id,
        target_user.full_name or "",
        until_date,
        0,
        0,
    )
    logger.info(
        "soft_ban: chat=%s admin=%s target=%s minutes=%s permanent=%s until=%s",
        chat.id,
        uid,
        target_user.id,
        ban_minutes if not permanent else 0,
        permanent,
        until_date,
    )


async def allow_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """解除 soft ban（仅超管）。

    - 回复/指定用户：解除该用户
    - 空发 /allow（不指定人）：解除本群全部 soft ban
    - 命令与相关提醒约 30 秒后自动删除
    """
    msg = update.effective_message
    chat = update.effective_chat

    if not msg or not chat:
        return

    uid = msg.from_user.id if msg.from_user else None
    if uid != SUPER_ADMIN_ID:
        await _reply_and_cleanup(
            msg, context, "⛔ 无权限触发 /allow，仅超管可用。", SOFT_BAN_NOTICE_TTL
        )
        return

    if chat.type not in {ChatType.GROUP, ChatType.SUPERGROUP}:
        if not _is_private_super_admin(chat, uid):
            await _reply_and_cleanup(
                msg, context, "此命令仅限群组使用。", SOFT_BAN_NOTICE_TTL
            )
            return

    has_reply = bool(msg.reply_to_message and msg.reply_to_message.from_user)
    has_args = bool(context.args)
    has_entity_target = False
    if msg.entities:
        for ent in msg.entities:
            if ent.type in ("url", "text_link", "text_mention", "mention"):
                if ent.type == "bot_command":
                    continue
                has_entity_target = True
                break
    raw_text = (msg.text or "")
    if TG_USER_URL_RE.search(raw_text):
        has_entity_target = True

    target_specified = has_reply or has_args or has_entity_target

    if not target_specified:
        cleared = _clear_all_bans_in_chat(chat.id)
        if not cleared:
            await _reply_and_cleanup(
                msg, context, "✅ 当前没有生效中的 soft ban。", SOFT_BAN_NOTICE_TTL
            )
            return
        for _cid, _uid, _name, _until, cmd_msg_id, reply_msg_id in cleared:
            for mid in (cmd_msg_id, reply_msg_id):
                if mid > 0:
                    try:
                        await context.bot.delete_message(chat_id=chat.id, message_id=mid)
                    except Exception:
                        pass
            _remove_soft_ban_notice(chat.id, _uid)
            _SOFT_BAN_LAST_NOTICE.pop(f"{chat.id}:{_uid}", None)
            _SOFT_BAN_NOTICE_DELETE_TASKS.pop(f"{chat.id}:{_uid}", None)
        names = ", ".join(
            _html_user_mention(u, n or f"用户{u}") for _, u, n, *_ in cleared[:20]
        )
        extra = f" 等共 {len(cleared)} 人" if len(cleared) > 20 else f"（{len(cleared)} 人）"
        sent = await _reply_text_and_track(
            msg,
            f"🔈 已解除本群全部 soft ban{extra}：{names}",
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )
        _schedule_delete_messages(
            context,
            chat.id,
            [msg.message_id, sent.message_id if sent else 0],
            SOFT_BAN_NOTICE_TTL,
        )
        logger.info(
            "allow_all: chat=%s admin=%s count=%s users=%s",
            chat.id,
            uid,
            len(cleared),
            [u for _, u, *_ in cleared],
        )
        return

    target_user = await _resolve_target_user(msg, chat, context)
    if not target_user:
        await _reply_and_cleanup(
            msg,
            context,
            "请回复一条消息来解除 soft ban。\n"
            "用法: /allow [用户ID]\n"
            "空发 /allow（不指定人）= 解除本群全部 soft ban。",
            SOFT_BAN_NOTICE_TTL,
        )
        return

    target_name = _user_display_name(target_user)
    target_mention = _html_user_mention(target_user.id, target_name)

    active = _load_active_ban(chat.id, target_user.id)
    if not active:
        await _reply_and_cleanup(
            msg,
            context,
            f"✅ {target_mention} 当前没有 soft ban。",
            SOFT_BAN_NOTICE_TTL,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )
        return

    _, _, _, until_date, cmd_msg_id, reply_msg_id = active
    remaining = (
        "永久"
        if until_date == SOFT_BAN_PERMANENT_UNTIL
        else _format_soft_ban_remaining(until_date)
    )
    logger.info(
        "allow: chat=%s admin=%s target=%s remaining=%s",
        chat.id,
        uid,
        target_user.id,
        remaining,
    )

    for mid in (cmd_msg_id, reply_msg_id):
        if mid > 0:
            try:
                await context.bot.delete_message(chat_id=chat.id, message_id=mid)
            except Exception:
                pass
    _remove_ban_record(chat.id, target_user.id)
    _remove_soft_ban_notice(chat.id, target_user.id)
    _SOFT_BAN_LAST_NOTICE.pop(f"{chat.id}:{target_user.id}", None)
    _SOFT_BAN_NOTICE_DELETE_TASKS.pop(f"{chat.id}:{target_user.id}", None)

    sent = await _reply_text_and_track(
        msg,
        f"🔈 已解除 {target_mention} 的 soft ban，请谨言慎行！",
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
    )
    _schedule_delete_messages(
        context,
        chat.id,
        [msg.message_id, sent.message_id if sent else 0],
        SOFT_BAN_NOTICE_TTL,
    )
    logger.info(
        "allow_done: chat=%s admin=%s target=%s",
        chat.id,
        uid,
        target_user.id,
    )


async def stop_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """停止自己正在思考中的 AI 会话。"""
    msg = update.effective_message
    chat = update.effective_chat
    if not msg or not chat or not msg.from_user:
        return

    uid = msg.from_user.id
    tasks = _ACTIVE_USER_TASKS.get(uid, set())
    count = len(tasks)
    if count == 0:
        await _reply_and_cleanup(msg, context, "你当前没有正在思考中的会话。", NOTICE_DELETE_TTL)
        return

    for task in list(tasks):
        task.cancel()
        _ACTIVE_AI_TASKS.discard(task)
    _ACTIVE_USER_TASKS[uid] = set()

    async with _ACTIVE_DS_LOCK:
        _ACTIVE_DS = max(0, _ACTIVE_DS - count)

    logger.info("stop_cmd: user=%s cancelled %s tasks", uid, count)
    await _reply_and_cleanup(msg, context, f"🛑 已停止 {count} 个正在思考的会话。", NOTICE_DELETE_TTL)


async def force_stop_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """强制终止所有正在进行的 AI 思考会话（超管命令）。"""
    msg = update.effective_message
    chat = update.effective_chat
    if not msg or not chat:
        return
    uid = msg.from_user.id if msg.from_user else None
    if uid != SUPER_ADMIN_ID:
        await _reply_and_cleanup(msg, context, "⛔ 无权限触发 /force_stop。", NOTICE_DELETE_TTL)
        return

    count = len(_ACTIVE_AI_TASKS)
    for task in list(_ACTIVE_AI_TASKS):
        task.cancel()

    _ACTIVE_USER_TASKS.clear()
    async with _ACTIVE_DS_LOCK:
        _ACTIVE_DS = 0

    logger.info("force_stop: cancelled %s tasks, reset _ACTIVE_DS", count)
    if count > 0:
        await _reply_and_cleanup(msg, context, f"🛑 已强制终止 {count} 个正在思考的会话。", NOTICE_DELETE_TTL)
    else:
        await _reply_and_cleanup(msg, context, "当前没有活跃的思考会话。", NOTICE_DELETE_TTL)


async def ping_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """从全球节点 ping 测试。用法：/ping <域名/IP> 或 /ping <DNS> <域名>"""
    msg = update.effective_message
    chat = update.effective_chat
    if not msg or not chat:
        return
    uid = msg.from_user.id if msg.from_user else None
    if not _is_private_super_admin(chat, uid):
        if not _is_allowed_chat(chat):
            await _reply_not_allowed_and_cleanup(msg, context)
            return
        if not _is_allowed_topic(msg):
            await _reply_not_allowed_and_cleanup(msg, context)
            return

    args = context.args
    if not args:
        await _reply_and_cleanup(
            msg, context,
            "用法:\n"
            "`/ping <域名>` — 全球节点 ping\n"
            "`/ping <域名> <地区/国家>` — 指定地区（例: as/eu/na/us/jp/cn）\n"
            "`/ping <DNS> <域名> [地区/国家]` — 指定 DNS 解析后 ping",
            NOTICE_DELETE_TTL,
        )
        return

    # 解析参数
    dns_server = None
    region = None
    target = None

    if len(args) == 1:
        target = args[0].strip().lower()
    elif len(args) == 2:
        # 判断第一个参数是 DNS IP 还是地区
        first = args[0].strip().lower()
        second = args[1].strip().lower()
        # 如果第一个是 IP（3个点，纯数字+点），视为 DNS
        ip_parts = first.split(".")
        if len(ip_parts) == 4 and all(p.isdigit() for p in ip_parts):
            dns_server = first
            target = second
        else:
            target = first
            region = second
    else:  # 3 args
        dns_server = args[0].strip().lower()
        target = args[1].strip().lower()
        region = args[2].strip().lower()

    # 清理目标（去掉协议/路径/端口）
    for prefix in ("http://", "https://"):
        if target.startswith(prefix):
            target = target[len(prefix):]
    target = target.split("/")[0].split(":")[0]

    # 先发一条"正在测"的消息
    sent = await msg.reply_text(f"🌍 正在从全球节点测试 `{target}` ...", parse_mode=ParseMode.MARKDOWN)

    try:
        ping_target = target
        dns_info = ""
        if dns_server:
            # 用 dig 通过指定 DNS 解析
            dig_proc = await asyncio.create_subprocess_exec(
                "dig", f"@{dns_server}", target, "+short",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            dig_out, dig_err = await asyncio.wait_for(dig_proc.communicate(), timeout=8)
            dig_text = dig_out.decode("utf-8", errors="replace").strip()
            dig_error = dig_err.decode("utf-8", errors="replace").strip()

            if dig_proc.returncode != 0 or not dig_text:
                reason = dig_error or "DNS 解析失败"
                await sent.edit_text(f"❌ DNS 解析 `{target}` @ `{dns_server}` 失败: {reason}")
                await _auto_delete_after(msg, sent, context)
                return

            ips = [l for l in dig_text.split("\n") if l.strip()]
            # 用标准库校验 IPv4，筛掉 CNAME/别名
            real_ips = []
            for line in ips:
                try:
                    # ipaddress 要求 strict=True 拒绝前导零等
                    ipaddress.IPv4Address(line.strip())
                    real_ips.append(line.strip())
                except ValueError:
                    pass
            if not real_ips:
                # dig 没返回 IP（比如全是 CNAME），回退用原域名
                logger.info("dig @%s %s 无 IPv4 结果，回退域名", dns_server, target)
                ping_target = target
            else:
                resolved_ip = real_ips[0]
                ping_target = resolved_ip
                dns_info = f"🌐 `{target}` → `{resolved_ip}` (via `{dns_server}`)\n\n"

        # 构建 Globalping locations
        locations_payload = []
        if region:
            # 已知大洲代码（2位）
            CONTINENTS = {"AS", "EU", "NA", "SA", "AF", "OC", "AN"}
            # 别名映射
            region_map = {
                "asia": "AS", "europe": "EU", "namerica": "NA",
                "southamerica": "SA", "africa": "AF", "oceania": "OC",
                "usa": "US", "japan": "JP", "china": "CN",
                "singapore": "SG", "hongkong": "HK",
                "korea": "KR", "germany": "DE", "france": "FR",
                "netherlands": "NL", "uk": "GB", "britain": "GB",
            }
            r = region_map.get(region, region.upper())
            if r in CONTINENTS:
                locations_payload = [{"continent": r}]
            else:
                locations_payload = [{"country": r}]

        # 通过 Globalping API 从多节点测试
        async with httpx.AsyncClient(timeout=60) as client:
            req_body = {
                "type": "ping",
                "target": ping_target,
                "measurementOptions": {"packets": 3},
            }
            if region:
                # 指定地区：请求 5 个
                req_body["limit"] = 5
                req_body["locations"] = locations_payload
            else:
                # 全球：按大洲均匀分配，取前5
                req_body["locations"] = [
                    {"continent": "NA", "limit": 3},
                    {"continent": "EU", "limit": 3},
                    {"continent": "AS", "limit": 3},
                    {"continent": "SA", "limit": 2},
                    {"continent": "AF", "limit": 2},
                    {"continent": "OC", "limit": 2},
                ]

            create_resp = await client.post(
                "https://api.globalping.io/v1/measurements",
                json=req_body,
                headers={"Accept": "application/json"},
            )
            create_data = create_resp.json()
            if "id" not in create_data:
                logger.warning("Globalping API error: body=%s response=%s", req_body, create_data)
                await sent.edit_text(f"❌ Globalping API 错误: {create_data}")
                await _auto_delete_after(msg, sent, context)
                return

            measurement_id = create_data["id"]

            # 轮询结果
            result_data = None
            for attempt in range(15):
                await asyncio.sleep(1.5)
                result_resp = await client.get(
                    f"https://api.globalping.io/v1/measurements/{measurement_id}",
                    headers={"Accept": "application/json"},
                )
                result_data = result_resp.json()
                if result_data.get("status") in ("finished", "failed"):
                    break

            if not result_data or result_data.get("status") not in ("finished", "failed"):
                await sent.edit_text(f"⏱ `{target}` 全球节点测试超时")
                await _auto_delete_after(msg, sent, context)
                return

        # 解析结果
        results = result_data.get("results") or []
        if not results:
            await sent.edit_text(f"❌ `{target}`: 所有节点均未返回结果")
            await _auto_delete_after(msg, sent, context)
            return

        # 按丢包率→平均延迟排序，取前5
        def _num(v, default):
            return default if v is None else v

        def _sort_key(r):
            stats = (r.get("result") or {}).get("stats") or {}
            loss = _num(stats.get("loss"), 100)
            avg = _num(stats.get("avg"), 9999)
            return (loss, avg)

        sorted_results = sorted(results, key=_sort_key)
        top5 = sorted_results[:5]
        total = len(results)

        lines = [f"🏓 **{target}**"]
        if not region and total > 5:
            lines[0] = f"🏓 **{target}** （展示延迟最低的 5/{total} 个节点）"
        if dns_info:
            lines.append(f"\n{dns_info}")

        for r in top5:
            probe = r.get("probe", {})
            res = r.get("result") or {}
            continent = probe.get("continent", "??")
            country = probe.get("country", "??")
            city = probe.get("city", "??")
            location = f"{city}, {country} ({continent})"
            stats = res.get("stats") or {}
            resolved = res.get("resolvedAddress", "")
            timings = res.get("timings") or []
            rtt_list = [t["rtt"] for t in timings if isinstance(t, dict) and "rtt" in t]
            loss = _num(stats.get("loss"), 100)
            status = res.get("status", "unknown")

            if status == "finished" and loss < 100 and rtt_list:
                rtt_str = ", ".join(f"{r:.1f}" for r in rtt_list)
                min_ms = _num(stats.get("min"), 0)
                avg_ms = _num(stats.get("avg"), 0)
                max_ms = _num(stats.get("max"), 0)
                icon = "🟢" if avg_ms < 100 else "🟡" if avg_ms < 300 else "🔴"
                lines.append(
                    f"\n{icon} **{location}**"
                    f"\n  `{ping_target}` ({resolved})"
                    f"\n  丢包: `{loss}%`  |  最小: `{min_ms:.1f}ms`  |  平均: `{avg_ms:.1f}ms`  |  最大: `{max_ms:.1f}ms`"
                    f"\n  RTT: `{rtt_str}`"
                )
            elif status == "finished" and loss == 100:
                lines.append(f"\n💀 **{location}** — 全部丢包")
            elif status == "failed":
                error = res.get("rawOutput", "节点异常")
                lines.append(f"\n⚠️ **{location}** — {error[:60]}")
            else:
                lines.append(f"\n⏳ **{location}** — 超时")

        text = "\n".join(lines)

    except httpx.HTTPError as e:
        await sent.edit_text(f"❌ 网络请求失败: {e}")
        await _auto_delete_after(msg, sent, context)
        return
    except Exception as e:
        logger.exception("ping_cmd failed")
        await sent.edit_text(f"❌ 异常: {e}")
        await _auto_delete_after(msg, sent, context)
        return

    await sent.edit_text(text, parse_mode=ParseMode.MARKDOWN)
    await _auto_delete_after(msg, sent, context)


async def http_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """从全球节点 HTTP 测速。用法：/http <URL> [地区]"""
    msg = update.effective_message
    chat = update.effective_chat
    if not msg or not chat:
        return
    uid = msg.from_user.id if msg.from_user else None
    if not _is_private_super_admin(chat, uid):
        if not _is_allowed_chat(chat):
            await _reply_not_allowed_and_cleanup(msg, context)
            return
        if not _is_allowed_topic(msg):
            await _reply_not_allowed_and_cleanup(msg, context)
            return

    args = context.args
    if not args:
        await _reply_and_cleanup(
            msg, context,
            "用法:\n"
            "`/http <URL>` — 全球节点 HTTP 测速\n"
            "`/http <URL> <地区>` — 指定地区\n"
            "`/http <DNS> <URL> [地区]` — 指定 DNS 解析后测速",
            NOTICE_DELETE_TTL,
        )
        return

    dns_server = None
    region = None
    url_arg = None

    if len(args) == 1:
        url_arg = args[0].strip().lower()
    elif len(args) == 2:
        first = args[0].strip().lower()
        second = args[1].strip().lower()
        ip_parts = first.split(".")
        if len(ip_parts) == 4 and all(p.isdigit() for p in ip_parts):
            dns_server = first
            url_arg = second
        else:
            url_arg = first
            region = second
    else:  # 3 args
        dns_server = args[0].strip().lower()
        url_arg = args[1].strip().lower()
        region = args[2].strip().lower()

    url = url_arg

    # 解析 URL
    if not url.startswith("http://") and not url.startswith("https://"):
        url = "https://" + url

    from urllib.parse import urlparse
    parsed = urlparse(url)
    host = parsed.hostname or ""
    port = parsed.port
    path = parsed.path or "/"
    if parsed.query:
        path += "?" + parsed.query
    protocol = "HTTPS" if parsed.scheme == "https" else "HTTP"

    if not host:
        await _reply_and_cleanup(msg, context, "❌ 无效 URL", NOTICE_DELETE_TTL)
        return

    sent = await msg.reply_text(f"🌍 正在从全球节点 HTTP 测速 `{host}` ...", parse_mode=ParseMode.MARKDOWN)

    try:
        # 构建 locations
        locations_payload = []
        if region:
            CONTINENTS = {"AS", "EU", "NA", "SA", "AF", "OC", "AN"}
            region_map = {
                "asia": "AS", "europe": "EU", "namerica": "NA",
                "southamerica": "SA", "africa": "AF", "oceania": "OC",
                "usa": "US", "japan": "JP", "china": "CN",
                "singapore": "SG", "hongkong": "HK",
                "korea": "KR", "germany": "DE", "france": "FR",
                "netherlands": "NL", "uk": "GB", "britain": "GB",
            }
            r = region_map.get(region, region.upper())
            if r in CONTINENTS:
                locations_payload = [{"continent": r}]
            else:
                locations_payload = [{"country": r}]

        async with httpx.AsyncClient(timeout=60) as client:
            req_body = {
                "type": "http",
                "target": host,
                "measurementOptions": {
                    "protocol": protocol,
                    "request": {"path": path, "method": "GET"},
                },
            }
            if port:
                req_body["measurementOptions"]["port"] = port
            if dns_server:
                req_body["measurementOptions"]["resolver"] = dns_server

            if region:
                req_body["limit"] = 5
                req_body["locations"] = locations_payload
            else:
                # 全球：按大洲均匀分配，取前5
                req_body["locations"] = [
                    {"continent": "NA", "limit": 3},
                    {"continent": "EU", "limit": 3},
                    {"continent": "AS", "limit": 3},
                    {"continent": "SA", "limit": 2},
                    {"continent": "AF", "limit": 2},
                    {"continent": "OC", "limit": 2},
                ]

            create_resp = await client.post(
                "https://api.globalping.io/v1/measurements",
                json=req_body,
                headers={"Accept": "application/json"},
            )
            create_data = create_resp.json()
            if "id" not in create_data:
                logger.warning("Globalping HTTP error: body=%s response=%s", req_body, create_data)
                await sent.edit_text(f"❌ Globalping API 错误: {create_data}")
                await _auto_delete_after(msg, sent, context)
                return

            measurement_id = create_data["id"]

            result_data = None
            for attempt in range(15):
                await asyncio.sleep(1.5)
                result_resp = await client.get(
                    f"https://api.globalping.io/v1/measurements/{measurement_id}",
                    headers={"Accept": "application/json"},
                )
                result_data = result_resp.json()
                if result_data.get("status") in ("finished", "failed"):
                    break

            if not result_data or result_data.get("status") not in ("finished", "failed"):
                await sent.edit_text(f"⏱ `{host}` HTTP 测速超时")
                await _auto_delete_after(msg, sent, context)
                return

        results = result_data.get("results") or []
        if not results:
            await sent.edit_text(f"❌ `{host}`: 所有节点均未返回结果")
            await _auto_delete_after(msg, sent, context)
            return

        # 按总耗时排序，取前5
        def _sort_key(r):
            res = r.get("result") or {}
            timings = res.get("timings") or {}
            return timings.get("total", 9999) if res.get("statusCode") else 9999

        sorted_results = sorted(results, key=_sort_key)
        top5 = sorted_results[:5]
        total = len(results)

        lines = [f"🌐 **{host}** （{protocol}）"]
        if not region and total > 5:
            lines[0] = f"🌐 **{host}** （{protocol}，展示最快 5/{total} 个节点）"

        for r in top5:
            probe = r.get("probe", {})
            res = r.get("result") or {}
            continent = probe.get("continent", "??")
            country = probe.get("country", "??")
            city = probe.get("city", "??")
            status_code = res.get("statusCode", "?")
            timings = res.get("timings") or {}
            total_ms = timings.get("total", "?")
            dns_ms = timings.get("dns", "?")
            tcp_ms = timings.get("tcp", "?")
            tls_ms = timings.get("tls", "—")
            fb_ms = timings.get("firstByte", "?")
            resolved = res.get("resolvedAddress", "")

            icon = "🟢" if (isinstance(total_ms, (int, float)) and total_ms < 1000) else \
                   "🟡" if (isinstance(total_ms, (int, float)) and total_ms < 3000) else "🔴"

            tls_part = f"TLS=`{tls_ms:.0f}ms`  " if isinstance(tls_ms, (int, float)) else ""
            lines.append(
                f"\n{icon} **{city}, {country} ({continent})**"
                f"\n  `{host}` → `{resolved}`  🔢 `{status_code}`"
                f"\n  ⏱ `{total_ms:.0f}ms`  DNS=`{dns_ms:.0f}ms`  TCP=`{tcp_ms:.0f}ms`  {tls_part}首字节=`{fb_ms:.0f}ms`"
            )

        text = "\n".join(lines)

    except httpx.HTTPError as e:
        await sent.edit_text(f"❌ 网络请求失败: {e}")
        await _auto_delete_after(msg, sent, context)
        return
    except Exception as e:
        logger.exception("http_cmd failed")
        await sent.edit_text(f"❌ 异常: {e}")
        await _auto_delete_after(msg, sent, context)
        return

    await sent.edit_text(text, parse_mode=ParseMode.MARKDOWN)
    await _auto_delete_after(msg, sent, context)


async def ip_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """查 IP 纯净度。用法：/ip <IP地址>"""
    msg = update.effective_message
    chat = update.effective_chat
    if not msg or not chat:
        return
    uid = msg.from_user.id if msg.from_user else None
    if not _is_private_super_admin(chat, uid):
        if not _is_allowed_chat(chat):
            await _reply_not_allowed_and_cleanup(msg, context)
            return
        if not _is_allowed_topic(msg):
            await _reply_not_allowed_and_cleanup(msg, context)
            return

    args = context.args
    if not args:
        await _reply_and_cleanup(
            msg,
            context,
            "用法: `/ip <IP>`  — 查 IP 纯净度评分", NOTICE_DELETE_TTL,
        )
        return

    ip = args[0].strip()
    if not is_valid_ip(ip):
        await _reply_and_cleanup(
            msg,
            context,
            f"❌ 无效的 IP 地址: `{ip}`", NOTICE_DELETE_TTL,
        )
        return

    try:
        result = check_ip(ip)
        report = format_report(result)
        await _reply_and_cleanup(
            msg,
            context,
            report, NOTICE_DELETE_TTL,
            parse_mode=ParseMode.HTML,
        )
    except Exception as e:
        logger.exception("ip_cmd failed for %s", ip)
        await _reply_and_cleanup(msg, context, f"❌ 检测异常: {e}", NOTICE_DELETE_TTL)


async def whois_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """查域名 WHOIS 信息。用法：/whois <域名>"""
    msg = update.effective_message
    chat = update.effective_chat
    if not msg or not chat:
        return
    uid = msg.from_user.id if msg.from_user else None
    if not _is_private_super_admin(chat, uid):
        if not _is_allowed_chat(chat):
            await _reply_not_allowed_and_cleanup(msg, context)
            return
        if not _is_allowed_topic(msg):
            await _reply_not_allowed_and_cleanup(msg, context)
            return

    args = context.args
    if not args:
        await _reply_and_cleanup(
            msg,
            context,
            "用法: `/whois <域名>`\n例如: `/whois linux.do`", NOTICE_DELETE_TTL,
        )
        return

    domain = args[0].strip().lower()
    # 清理协议、路径、端口、超链接等
    for prefix in ("http://", "https://"):
        if domain.startswith(prefix):
            domain = domain[len(prefix):]
    domain = domain.split("/")[0].split(":")[0].rstrip(".").strip()

    if not re.match(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$", domain):
        await _reply_and_cleanup(
            msg, context, f"❌ 无效的域名: `{domain}`", NOTICE_DELETE_TTL,
        )
        return

    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
            resp = await client.get(f"https://api.tian.hu/whois/{domain}")
            resp.raise_for_status()
            data = resp.json()

        if not data or data.get("code") != 200 or not data.get("data"):
            reason = (data or {}).get("message") or "无查询结果"
            await _reply_and_cleanup(
                msg, context, f"❌ WHOIS 查询失败: {reason}", NOTICE_DELETE_TTL,
            )
            return

        info = data["data"]
        fm = info.get("formatted") or {}
        dom = fm.get("domain") or {}

        # 解析字段（有的域名某些字段缺失）
        def _d(*keys, default="N/A"):
            for k in keys:
                v = dom.get(k)
                if v:
                    return v
            return default

        created = str(_d("created_date", "created_date_utc"))[:10]
        expired = str(_d("expired_date", "expired_date_utc"))[:10]
        updated = str(_d("updated_date", "updated_date_utc"))[:10]

        registrar = (fm.get("registrar") or {}).get("registrar_name") or "N/A"
        registrant = (fm.get("registrant") or {}).get("registrant_name") or "N/A"

        ns_list = dom.get("name_servers") or []
        status_list = dom.get("status") or []

        lines = [
            f"🔍 WHOIS `{info.get('domain') or domain}`",
            f"注册商: `{registrar}`",
            f"注册人: `{registrant}`",
            f"创建: `{created}` 到期: `{expired}` 更新: `{updated}`",
        ]
        if status_list:
            lines.append(f"状态: `{'`, `'.join(status_list)}`")
        if ns_list:
            lines.append(f"NS: `{'`, `'.join(ns_list)}`")

        await _reply_and_cleanup(
            msg, context, "\n".join(lines), NOTICE_DELETE_TTL,
        )
    except Exception as e:
        logger.exception("whois_cmd failed for %s", domain)
        await _reply_and_cleanup(
            msg, context, f"❌ WHOIS 查询异常: {e}", NOTICE_DELETE_TTL,
        )


async def post_init(application: Application) -> None:
    global BOT_USERNAME, BOT_ID
    me = await application.bot.get_me()
    BOT_USERNAME = me.username or ""
    BOT_ID = me.id
    commands = [
        BotCommand("ds", f"对话 ({DS_MODEL})"),
        BotCommand("gk", f"对话 ({GROK_MODEL})"),
        BotCommand("img", "生成图片"),
        BotCommand("edit", "修改图片"),
        BotCommand("av", "番号查 R18.dev 封面；图片检索 AVScan"),
        BotCommand("vid", "生成视频"),
        BotCommand("help", "查看帮助"),
        BotCommand("new", "清空你在当前聊天的记忆"),
        BotCommand("clear", "删除你与 AI 的对话消息"),
        BotCommand("force_new", "强制清空本群所有人上下文（仅超管）"),
        BotCommand("force_clear", "强制删除全群与 AI 的对话消息（仅超管）"),
        BotCommand("stop", "停止自己正在思考中的 AI 会话"),
        BotCommand("force_stop", "强制终止所有正在思考的会话（仅超管）"),
        BotCommand("ban", "soft ban：删对方发言（默认永久，仅超管）"),
        BotCommand("allow", "解除 soft ban；空发=全解（仅超管）"),
        BotCommand("pin", "更新目标话题的每日置顶（仅超管）"),
        BotCommand("unpin", "取消置顶并停用每日自动置顶（仅超管）"),
        BotCommand("start", "启动说明"),
        BotCommand("ip", "查 IP 纯净度评分"),
        BotCommand("whois", "查域名 WHOIS 信息"),
        BotCommand("ping", "ping 测试域名延迟"),
        BotCommand("http", "从全球节点 HTTP 测速"),
        BotCommand("context", "查询模型上下文长度"),
        BotCommand("status", "查看模型可用率（/status 模型名 查单个）"),
    ]
    await application.bot.set_my_commands(commands)
    await application.bot.set_my_commands(commands, scope=BotCommandScopeAllGroupChats())
    await application.bot.set_my_commands(
        commands, scope=BotCommandScopeAllPrivateChats()
    )
    application.create_task(_managed_pin_scheduler_loop(application))
    application.create_task(_ban_release_scheduler_loop(application))
    if INACTIVITY_WARN_ENABLED:
        application.create_task(_inactivity_scheduler_loop(application))
        logger.info(
            "Inactivity policy enabled: warn=%sd remind2=%sd kick=%sd grace=%sd "
            "interval=%ss max_warns=%s max_remind2=%s max_kick_warns=%s max_kicks=%s "
            "(no permission mutes; /ban is soft-delete)",
            INACTIVITY_DAYS,
            INACTIVITY_REMIND2_DAYS,
            INACTIVITY_KICK_DAYS,
            INACTIVITY_KICK_GRACE_DAYS,
            INACTIVITY_CHECK_INTERVAL,
            INACTIVITY_MAX_WARNS_PER_CYCLE,
            INACTIVITY_MAX_REMIND2_PER_CYCLE,
            INACTIVITY_MAX_KICK_WARNS_PER_CYCLE,
            INACTIVITY_MAX_KICKS_PER_CYCLE,
        )
    logger.info("Bot started as @%s", BOT_USERNAME)


def validate_env() -> None:
    missing = []
    if not TELEGRAM_BOT_TOKEN:
        missing.append("TELEGRAM_BOT_TOKEN")
    if not AI_API_KEY:
        missing.append("AI_API_KEY")

    if missing:
        raise RuntimeError(f"Missing required env vars: {', '.join(missing)}")


def main() -> None:
    validate_env()
    _init_memory_db()

    app = (
        Application.builder()
        .token(TELEGRAM_BOT_TOKEN)
        .concurrent_updates(MAX_CONCURRENT_UPDATES)
        .post_init(post_init)
        .build()
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("new", new_cmd))
    app.add_handler(CommandHandler("clear", clear_cmd))
    app.add_handler(CommandHandler("force_new", force_new_cmd))
    app.add_handler(CommandHandler("force_clear", force_clear_cmd))
    app.add_handler(CommandHandler("stop", stop_cmd))
    app.add_handler(CommandHandler("force_stop", force_stop_cmd))
    app.add_handler(CommandHandler("pin", pin_cmd))
    app.add_handler(CommandHandler("unpin", unpin_cmd))
    app.add_handler(CommandHandler(["img", "edit"], image_cmd))
    app.add_handler(CommandHandler("av", av_cmd))
    app.add_handler(CommandHandler("vid", video_cmd))
    app.add_handler(CommandHandler("ban", ban_cmd))
    app.add_handler(CommandHandler("allow", allow_cmd))
    app.add_handler(CommandHandler(["ds", "gk"], ai_cmd))
    app.add_handler(CommandHandler("ip", ip_cmd))
    app.add_handler(CommandHandler("whois", whois_cmd))
    app.add_handler(CommandHandler("ping", ping_cmd))
    app.add_handler(CommandHandler("http", http_cmd))
    app.add_handler(CommandHandler("context", context_cmd))
    app.add_handler(CommandHandler("status", status_cmd))
    # group=-2: soft ban 删消息（优先）
    app.add_handler(
        MessageHandler(filters.ChatType.GROUPS & ~filters.StatusUpdate.ALL, enforce_soft_ban),
        group=-2,
    )
    # group=-1: always record activity, even when later handlers short-circuit.
    app.add_handler(
        MessageHandler(filters.ChatType.GROUPS & ~filters.StatusUpdate.ALL, track_activity),
        group=-1,
    )
    app.add_handler(
        ChatMemberHandler(on_chat_member, ChatMemberHandler.CHAT_MEMBER),
        group=-1,
    )
    app.add_handler(MessageHandler((filters.PHOTO | filters.Document.IMAGE) & ~filters.COMMAND, on_image_request))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_image_request))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text), group=1)

    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
