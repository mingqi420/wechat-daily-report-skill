---
name: wechat-daily-report-skill
description: 生成微信群聊日报。优先用 jackwener/wx-cli 获取本地微信聊天记录，再输出新版日报 HTML 和 PNG，包含今日剧情、聊天气泡、资料收纳、问答、成员观察和成员过滤。
---

# WeChat Daily Report Skill

目标：把微信群聊天记录整理成一份可读、可分享、可归档的日报。

最终产物优先输出：

- `report.html`：可交互网页，支持侧边栏导航和成员过滤
- `report.png`：可发群、可放 Obsidian 的长图

## 工作流

### 1. 确认数据来源

优先根据用户已有数据选择路线：

- 用户希望用 `jackwener/wx-cli`：使用 `scripts/wx_cli_to_report.py`
- 用户给了本地 `wechat-cli-pkg.tar.gz`：解压后用 `scripts/wx_cli_to_report.py --binary <wechat-cli>`
- 已有 `stats.json` 和 `ai_content.json`：直接渲染
- 已有聊天文本、WeFlow 导出、其他 JSON：先整理为 `stats.json`，再按 `references/ai_prompt.md` 生成 `ai_content.json`
- 要读本机微信数据库：执行本仓库内置解密和分析脚本

不要默认承诺“安装到 Obsidian 后自动打通微信”。本 skill 负责生成日报素材；Obsidian 侧是归档和浏览。

### 2. wx-cli 路线

```bash
wx sessions
```

```bash
python3 scripts/wx_cli_to_report.py \
  --chatroom "<群名或 chatroom id>" \
  --date YYYY-MM-DD \
  --limit 5000 \
  --output-stats stats.json \
  --output-text simplified_chat.txt
```

群聊在内部按稳定 ID 分组，对外优先展示群昵称，没有群昵称时展示原始微信昵称。
优先使用新版 wx-cli 的
`sender_username`；旧版输出缺少该字段时，适配器会从 `~/.wx-cli/cache` 恢复
真实发送者并完成名称映射。新版字段和缓存都无法解析时默认停止，避免同名
成员或昵称/username 碰撞导致串人。仅在用户明确接受风险时使用
`--allow-unstable-senders`。

产物：

- `stats.json`
- `simplified_chat.txt`

如果使用本地 wechat-cli 包：

```bash
python3 scripts/wx_cli_to_report.py \
  --binary "<解压后的 wechat-cli 二进制路径>" \
  --chatroom "<群名或 chatroom id>" \
  --date YYYY-MM-DD \
  --limit 5000 \
  --output-stats stats.json \
  --output-text simplified_chat.txt
```

### 3. 生成 AI 内容

必须读取：

- `references/ai_prompt.md`
- `stats.json`
- `simplified_chat.txt` 或所有 `simplified_chat_*.txt`

必须产出：

- `ai_content.json`

要求：

- 只能保存合法 JSON
- 不要保存 Markdown 代码块
- `talker_profiles` 的 key 必须和 `stats.json` 里的 `top_talkers[].name` 完全一致
- `dialogues[].messages[].name` 必须尽量使用真实群昵称，成员过滤依赖这些名字

### 4. 渲染 HTML

```bash
python3 scripts/generate_report.py \
  --stats stats.json \
  --ai-content ai_content.json \
  --output report.html
```

### 5. 渲染 PNG 长图

```bash
python3 scripts/generate_report.py \
  --stats stats.json \
  --ai-content ai_content.json \
  --output report.png \
  --viewport-width 1180 \
  --viewport-height 1400 \
  --device-scale-factor 2
```

## 示例自检

在改模板或发布前，至少跑一次：

```bash
python3 -m py_compile scripts/*.py
python3 scripts/wx_cli_to_report.py \
  --input-json examples/wx_cli_history_sample.json \
  --chatroom "Dont哥 对谈群" \
  --date 2026-04-30 \
  --output-stats examples/wx_stats_from_sample.json \
  --output-text examples/wx_simplified_chat_sample.txt
python3 scripts/generate_report.py \
  --stats examples/sample_stats.json \
  --ai-content examples/sample_ai_content.json \
  --output examples/qun-ribao-demo.html
python3 scripts/generate_report.py \
  --stats examples/sample_stats.json \
  --ai-content examples/sample_ai_content.json \
  --output examples/qun-ribao-demo.png
```

## 输出风格

- 像一份“聊天现场复盘”，不要像普通统计海报
- 重点保留对话上下文、人物、时间和资料价值
- 优先生成可回看的事件线：发生了什么、谁说了什么、结论是什么
- HTML 适合托管和交互，PNG 适合发群和归档
