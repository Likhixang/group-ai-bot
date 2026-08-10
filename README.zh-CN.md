[English](README.md) | [中文](README.zh-CN.md)

# Group AI Bot (Docker Compose)

这是一个 Telegram 群组/频道机器人，支持接入 OpenAI 兼容 API。

## 1. 准备环境变量

复制模板：

```bash
cp .env.example .env
```

编辑 `.env` 并填写：

- `TELEGRAM_BOT_TOKEN`
- `AI_BASE_URL`
- `AI_API_KEY`
- （可选）`AI_MODEL`
- （可选）`OAI_MODEL`
- （可选）`IMAGE_MODEL`
- （可选）`IMAGE_EDIT_MODEL`

- （可选）`MEMORY_MAX_TURNS`

- （可选）`SUPER_ADMIN_ID`
- （可选）`MEMORY_DB_PATH`
- （可选）`ALLOWED_GROUP_IDS`
- （可选）`ALLOWED_CHAT_USERNAMES`
- （可选）`ALLOWED_TOPIC_IDS`

- （可选）`TIMED_TOPIC_CHAT_ID`
- （可选）`TIMED_TOPIC_ID`
- （可选）`TIMED_TOPIC_TZ`
- （可选）`TIMED_OPEN_HOUR`
- （可选）`TIMED_CLOSE_HOUR`
- （可选）`PIN_TARGET_CHAT_ID`
- （可选）`PIN_TARGET_TOPIC_ID`
- （可选）`PIN_TZ`
- （可选）`PIN_HOUR`
- （可选）`PIN_MINUTE`
- （可选）`DEFAULT_DAILY_PIN_TEXT`

## 2. 启动

```bash
docker compose up -d --build
```

查看日志：

```bash
docker compose logs -f group-ai-bot
```

## 3. 群里使用

- `grok 你的问题`（文本快模：`grok-4-1-fast`）
- `grokt 你的问题`（文本深度思考：`grok-4-1-thinking`）
- `oai 你的问题` 或 `/oai 你的问题`（GPT 模型：`gpt-5.5`）
- `img 你的描述词` 或 `/img 你的描述词`：用 `gpt-image-2` 生成图片
- 回复机器人文字消息可继续文本上下文（不用前缀）
- 回复机器人生成的图片并输入修改要求，会自动改图
- `edit 修改要求` 或 `/edit 修改要求`：修改被回复的图片；也支持上传图片并在 caption 写改图要求
- `/new` 清空你在当前聊天的上下文记忆
- `/new_group` 清空本群的群聊记忆（仅超管）
- `/pin 内容` 更新目标话题的每日置顶内容并立即替换旧的受管置顶（仅超管）
- 回复一条带原生格式（粗体/斜体/链接/代码等）的消息再发 `/pin`，可保留该消息的 Telegram 格式并置顶；无参数 `/pin` 按已存储内容重发（含格式）
- 可按话题ID配置不同策略（如 `1` 文明安全、`45807` 保留默认风格）
- 可配置某个话题按时段自动开关（如北京时间 `23:00-05:00` 开放，其他时间关闭）
- 可配置目标话题每日定时自动发送并置顶一条受管消息（默认北京时间 `00:01`）
- 群聊记忆会记录本群最近聊天（默认 50 条，超出自动删旧留新）

## 4. 记忆持久化

- 默认使用 SQLite：`/data/memory.db`
- `docker compose` 已挂载持久卷，容器重启后记忆不丢

## 5. 安全建议

- 不要把 `.env` 提交到 git。
- `AI_API_KEY`、`TELEGRAM_BOT_TOKEN` 只放环境变量。
