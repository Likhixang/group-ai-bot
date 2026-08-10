import importlib
import os
import sys
from types import SimpleNamespace

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")
os.environ.setdefault("AI_API_KEY", "test-key")
os.environ.setdefault("AI_BASE_URL", "http://127.0.0.1:8090/v1")
os.environ.setdefault("DS_MODEL", "deepseek-v4-flash")
os.environ.setdefault("GROK_MODEL", "grok-4.5")
os.environ.setdefault("AI_THINKING_MODEL", "deepseek-v4-pro")
os.environ.setdefault("OAI_MODEL", "gpt-5.5")
os.environ.setdefault("IMAGE_MODEL", "gpt-image-2")
os.environ.setdefault("IMAGE_EDIT_MODEL", "gpt-image-2")

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
bot = importlib.import_module("bot")


def test_clean_image_prompt_removes_img_and_edit_prefixes():
    assert bot._clean_image_prompt("img 画一只猫") == "画一只猫"
    assert bot._clean_image_prompt("/img 画一只猫") == "画一只猫"
    assert bot._clean_image_prompt("edit 改成夜景") == "改成夜景"
    assert bot._clean_image_prompt("/edit 改成夜景") == "改成夜景"


def test_clean_prompt_and_select_text_model_for_ds_and_gk():
    assert bot._clean_prompt("ds 你好") == "你好"
    assert bot._clean_prompt("/ds 你好") == "你好"
    assert bot._clean_prompt("gk 你好") == "你好"
    assert bot._clean_prompt("/gk 你好") == "你好"
    assert bot._select_text_model("ds 你好") == bot.DS_MODEL
    assert bot._select_text_model("/ds 你好") == bot.DS_MODEL
    assert bot._select_text_model("gk 你好") == bot.GROK_MODEL
    assert bot._select_text_model("/gk 你好") == bot.GROK_MODEL
    assert bot._is_text_ai_prefix("gk 测试") is True
    assert bot._is_text_ai_prefix("/gk 测试") is True


def test_image_request_detection_handles_prefixes_and_commands():
    assert bot._is_image_generation_request("img 赛博朋克城市") is True
    assert bot._is_image_generation_request("/img 赛博朋克城市") is True
    assert bot._is_image_generation_request("ds img 赛博朋克城市") is False
    assert bot._is_image_edit_request("edit 改成水彩") is True
    assert bot._is_image_edit_request("/edit 改成水彩") is True
    assert bot._is_image_edit_request("ds edit 改成水彩") is False


def test_reply_to_bot_photo_is_image_edit_target():
    bot.BOT_ID = 123
    replied = SimpleNamespace(
        from_user=SimpleNamespace(id=123, username="any_bot"),
        photo=[SimpleNamespace(file_id="file-1")],
        document=None,
    )
    msg = SimpleNamespace(reply_to_message=replied)
    assert bot._reply_image_target(msg) == "file-1"


def test_user_uploaded_photo_with_edit_caption_is_image_edit_target():
    msg = SimpleNamespace(
        reply_to_message=None,
        photo=[SimpleNamespace(file_id="small"), SimpleNamespace(file_id="large")],
        document=None,
    )
    assert bot._message_image_target(msg) == "large"


def test_image_prompt_web_search_decision_for_current_or_specific_subjects():
    assert bot._should_web_search_image_prompt("img 画一下最新的 Tesla Roadster 外观") is True
    assert bot._should_web_search_image_prompt("img 画一张 iPhone 18 发布会海报") is True
    assert bot._should_web_search_image_prompt("img 画一只白猫在月光下喝茶") is False
    assert bot._should_web_search_image_prompt("img 梵高风格的向日葵") is False


def test_enrich_image_prompt_with_web_context_includes_real_search_data():
    prompt = "画最新的 Tesla Roadster"
    web_context = "WEB_SEARCH_CONTEXT\nResult 1:\nTitle: Tesla Roadster 2026\nURL: https://example.com\nContent: updated body shape and LED strip"
    enriched = bot._enrich_image_prompt_with_web_context(prompt, web_context)
    assert prompt in enriched
    assert "updated body shape" in enriched
    assert "真实参考资料" in enriched
