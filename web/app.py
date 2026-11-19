#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""微信群日报 Web 控制台。

启动:
    python3 web/app.py
默认监听 http://127.0.0.1:8765
"""

import json
import os
import shutil
import subprocess
import sys
import threading
import time
import uuid
from collections import deque

from flask import Flask, jsonify, render_template, request, send_from_directory

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS_DIR = os.path.join(BASE_DIR, "scripts")
REPORTS_DIR = os.path.join(BASE_DIR, "reports")
OUTPUT_DIR = os.path.join(BASE_DIR, "web", "output")
CONFIG_DIR = os.path.join(BASE_DIR, "config")
AI_CONFIG_PATH = os.path.join(CONFIG_DIR, "ai_agent.json")
AI_CONFIG_EXAMPLE_PATH = os.path.join(CONFIG_DIR, "ai_agent.example.json")

sys.path.insert(0, SCRIPTS_DIR)

app = Flask(
    __name__,
    template_folder=os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates"),
    static_folder=os.path.join(os.path.dirname(os.path.abspath(__file__)), "static"),
)

# ── task store ──────────────────────────────────────────────────────────────
_tasks: dict = {}
_tasks_lock = threading.Lock()


def _make_task(task_id, label):
    task = {
        "id": task_id,
        "label": label,
        "status": "running",   # running | done | error
        "logs": deque(maxlen=200),
        "result": None,
        "started_at": time.time(),
        "finished_at": None,
    }
    with _tasks_lock:
        _tasks[task_id] = task
    return task


def _append_log(task, line):
    task["logs"].append(line)


def _finish(task, status, result=None, error=None):
    task["status"] = status
    task["result"] = result or error
    task["finished_at"] = time.time()


# ── helpers ──────────────────────────────────────────────────────────────────
def _find_cli():
    for name in ("wx", "wechat-cli"):
        path = shutil.which(name)
        if path:
            return name, path
    return None, None


def _run_script(cmd, timeout=300):
    """Run a subprocess, yield stdout+stderr lines."""
    proc = subprocess.Popen(
        cmd,
        cwd=BASE_DIR,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
    )
    lines = []
    for raw in proc.stdout:
        line = raw.rstrip("\n")
        lines.append(line)
        yield line
    proc.wait(timeout=timeout)
    if proc.returncode != 0:
        raise RuntimeError(f"命令退出码 {proc.returncode}:\n" + "\n".join(lines[-10:]))


# ── pages ────────────────────────────────────────────────────────────────────
@app.route("/")
def index():
    return render_template("index.html")


# ── API: list groups ─────────────────────────────────────────────────────────
@app.route("/api/groups")
def api_groups():
    kind, exe = _find_cli()
    if not exe:
        return jsonify({"ok": False, "error": "找不到 wx 或 wechat-cli，请先安装并加入 PATH"})
    try:
        result = subprocess.run(
            [exe, "sessions", "--json"],
            capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=30,
        )
        if result.returncode != 0:
            return jsonify({"ok": False, "error": result.stderr.strip() or "wx sessions 失败"})
        data = json.loads(result.stdout)
        sessions = data if isinstance(data, list) else data.get("sessions", [])
        groups = []
        for s in sessions:
            name = s.get("chat") or s.get("name") or s.get("display_name") or s.get("username", "")
            username = s.get("username") or s.get("chat_id") or ""
            is_group = s.get("is_group", True)
            if name and is_group:
                groups.append({"name": name, "username": username})
        return jsonify({"ok": True, "groups": groups})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)})


# ── API: export chat records ─────────────────────────────────────────────────
@app.route("/api/export", methods=["POST"])
def api_export():
    body = request.get_json(force=True)
    chatroom = (body.get("chatroom") or "").strip()
    date = (body.get("date") or "").strip()
    start = (body.get("start") or date).strip()
    end = (body.get("end") or date).strip()

    if not chatroom:
        return jsonify({"ok": False, "error": "请选择或输入群名称"})
    if not start and not end:
        return jsonify({"ok": False, "error": "请至少选择一个日期"})

    task_id = str(uuid.uuid4())[:8]
    task = _make_task(task_id, f"导出 {chatroom} {start}~{end}")

    def worker():
        try:
            ensure_dir(OUTPUT_DIR)
            stats_path = os.path.join(OUTPUT_DIR, f"{task_id}_stats.json")
            text_path = os.path.join(OUTPUT_DIR, f"{task_id}_chat.txt")
            raw_path = os.path.join(OUTPUT_DIR, f"{task_id}_raw.json")

            py = sys.executable
            cmd = [
                py, os.path.join(SCRIPTS_DIR, "wx_cli_to_report.py"),
                "--chatroom", chatroom,
                "--start", start or date,
                "--end", end or date,
                "--limit", "5000",
                "--output-stats", stats_path,
                "--output-text", text_path,
                "--raw-output", raw_path,
            ]
            _append_log(task, "$ " + " ".join(cmd))
            for line in _run_script(cmd):
                _append_log(task, line)

            _finish(task, "done", {
                "task_id": task_id,
                "stats_path": stats_path,
                "text_path": text_path,
                "raw_path": raw_path,
            })
        except Exception as exc:
            _append_log(task, f"错误: {exc}")
            _finish(task, "error", error=str(exc))

    threading.Thread(target=worker, daemon=True).start()
    return jsonify({"ok": True, "task_id": task_id})


# ── API: generate report ─────────────────────────────────────────────────────
@app.route("/api/generate", methods=["POST"])
def api_generate():
    body = request.get_json(force=True)
    task_id_src = (body.get("task_id") or "").strip()   # from export
    chatroom = (body.get("chatroom") or "").strip()
    date = (body.get("date") or "").strip()

    # Find stats file
    if task_id_src:
        stats_path = os.path.join(OUTPUT_DIR, f"{task_id_src}_stats.json")
    else:
        stats_path = os.path.join(BASE_DIR, "stats.json")

    if not os.path.isfile(stats_path):
        return jsonify({"ok": False, "error": f"找不到 stats.json，请先导出聊天记录"})

    ai_path = os.path.join(BASE_DIR, "ai_content.json")
    # 优先用本次导出任务生成的 AI 内容，其次项目根目录，最后回退到全局示例
    if task_id_src:
        task_ai = os.path.join(OUTPUT_DIR, f"{task_id_src}_ai_content.json")
        if os.path.isfile(task_ai):
            ai_path = task_ai
    if not os.path.isfile(ai_path):
        output_ai = os.path.join(OUTPUT_DIR, "ai_content.json")
        if os.path.isfile(output_ai):
            ai_path = output_ai

    task_id = str(uuid.uuid4())[:8]
    label = f"生成日报 {chatroom or ''} {date or ''}"
    task = _make_task(task_id, label)

    def worker():
        try:
            ensure_dir(OUTPUT_DIR)
            html_path = os.path.join(OUTPUT_DIR, f"{task_id}_report.html")
            png_path = os.path.join(OUTPUT_DIR, f"{task_id}_report.png")

            py = sys.executable

            # Step 1: HTML
            cmd_html = [
                py, os.path.join(SCRIPTS_DIR, "generate_report.py"),
                "--stats", stats_path,
                "--output", html_path,
            ]
            if os.path.isfile(ai_path):
                cmd_html += ["--ai-content", ai_path]
            _append_log(task, "$ " + " ".join(cmd_html))
            for line in _run_script(cmd_html):
                _append_log(task, line)

            # Step 2: PNG
            cmd_png = [
                py, os.path.join(SCRIPTS_DIR, "generate_report.py"),
                "--stats", stats_path,
                "--output", png_path,
                "--viewport-width", "1180",
                "--viewport-height", "1400",
                "--device-scale-factor", "2",
            ]
            if os.path.isfile(ai_path):
                cmd_png += ["--ai-content", ai_path]
            _append_log(task, "$ " + " ".join(cmd_png))
            for line in _run_script(cmd_png, timeout=120):
                _append_log(task, line)

            _finish(task, "done", {
                "task_id": task_id,
                "html_file": f"{task_id}_report.html",
                "png_file": f"{task_id}_report.png",
            })
        except Exception as exc:
            _append_log(task, f"错误: {exc}")
            _finish(task, "error", error=str(exc))

    threading.Thread(target=worker, daemon=True).start()
    return jsonify({"ok": True, "task_id": task_id})


# ── API: AI agent config ─────────────────────────────────────────────────────
def _mask_key(key):
    if not key:
        return ""
    if len(key) <= 8:
        return "*" * len(key)
    return key[:4] + "*" * (len(key) - 8) + key[-4:]


def _load_ai_config():
    # 复用脚本里的三级合并逻辑（文件 + 环境变量），保证前后端看到同一份配置
    import generate_ai_content as ai
    merged = dict(ai.DEFAULT_CONFIG)
    merged.update(ai.load_file_config(AI_CONFIG_PATH))
    for key, env in (("base_url", "WECHAT_AI_BASE_URL"), ("model", "WECHAT_AI_MODEL"), ("api_key", "WECHAT_AI_API_KEY")):
        if os.environ.get(env):
            merged[key] = os.environ[env]
    if not merged.get("api_key") and os.environ.get("OPENAI_API_KEY"):
        merged["api_key"] = os.environ["OPENAI_API_KEY"]
    env_verify = os.environ.get("WECHAT_AI_VERIFY_SSL")
    if env_verify is not None:
        merged["verify_ssl"] = env_verify.strip().lower() not in ("0", "false", "no", "off")
    return merged


@app.route("/api/ai-config", methods=["GET", "POST"])
def api_ai_config():
    if request.method == "GET":
        cfg = _load_ai_config()
        has_key = bool(cfg.get("api_key"))
        return jsonify({
            "ok": True,
            "config_exists": os.path.isfile(AI_CONFIG_PATH),
            "has_api_key": has_key,
            "api_key_masked": _mask_key(cfg.get("api_key", "")),
            "config": {k: v for k, v in cfg.items() if k != "api_key"},
        })

    body = request.get_json(force=True)
    incoming = body.get("config") or {}
    current = {}
    if os.path.isfile(AI_CONFIG_PATH):
        try:
            with open(AI_CONFIG_PATH, "r", encoding="utf-8") as f:
                current = json.load(f)
        except (OSError, json.JSONDecodeError):
            current = {}
    if not isinstance(current, dict):
        current = {}

    # 白名单字段，避免把 _comment 之外的脏数据写进去
    allowed = (
        "enabled", "provider", "base_url", "model", "temperature",
        "max_tokens", "timeout", "json_mode", "max_chat_chars", "verify_ssl",
    )
    for key in allowed:
        if key in incoming:
            current[key] = incoming[key]
    # api_key 仅在前端显式提交非空时更新，避免掩码回显覆盖真实密钥
    if body.get("api_key"):
        current["api_key"] = body["api_key"]
    current.setdefault("provider", "openai-compatible")

    os.makedirs(CONFIG_DIR, exist_ok=True)
    with open(AI_CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(current, f, ensure_ascii=False, indent=2)
    try:
        os.chmod(AI_CONFIG_PATH, 0o600)
    except OSError:
        pass
    return jsonify({"ok": True, "saved": True})


# ── API: generate ai_content via AI agent ────────────────────────────────────
@app.route("/api/generate-ai", methods=["POST"])
def api_generate_ai():
    body = request.get_json(force=True)
    task_id_src = (body.get("task_id") or "").strip()
    chatroom = (body.get("chatroom") or "").strip()
    date = (body.get("date") or "").strip()

    if task_id_src:
        stats_path = os.path.join(OUTPUT_DIR, f"{task_id_src}_stats.json")
        chat_path = os.path.join(OUTPUT_DIR, f"{task_id_src}_chat.txt")
    else:
        stats_path = os.path.join(BASE_DIR, "stats.json")
        chat_path = os.path.join(BASE_DIR, "simplified_chat.txt")

    if not os.path.isfile(stats_path):
        return jsonify({"ok": False, "error": "找不到 stats.json，请先导出聊天记录"})

    cfg = _load_ai_config()
    if not cfg.get("api_key"):
        return jsonify({"ok": False, "error": "尚未配置 AI Agent 的 api_key，请先在下方「AI Agent 配置」填写"})

    out_ai = (
        os.path.join(OUTPUT_DIR, f"{task_id_src}_ai_content.json")
        if task_id_src else os.path.join(BASE_DIR, "ai_content.json")
    )

    task_id = str(uuid.uuid4())[:8]
    task = _make_task(task_id, f"生成 AI 内容 {chatroom or ''} {date or ''}")

    def worker():
        try:
            ensure_dir(OUTPUT_DIR)
            py = sys.executable
            cmd = [
                py, os.path.join(SCRIPTS_DIR, "generate_ai_content.py"),
                "--stats", stats_path,
                "--output", out_ai,
            ]
            # 有对应聊天文本时显式传入，否则交给脚本从 stats.raw_text_paths 自动发现
            if os.path.isfile(chat_path):
                cmd += ["--chat", chat_path]
            # 用刚保存的配置文件，确保前后端看到同一份配置
            if os.path.isfile(AI_CONFIG_PATH):
                cmd += ["--config", AI_CONFIG_PATH]
            _append_log(task, "$ " + " ".join(cmd))
            for line in _run_script(cmd, timeout=int(cfg.get("timeout", 180)) + 60):
                _append_log(task, line)
            _finish(task, "done", {
                "task_id": task_id,
                "src_task": task_id_src,
                "ai_file": os.path.basename(out_ai) if task_id_src else None,
                "ai_path": out_ai,
            })
        except Exception as exc:
            _append_log(task, f"错误: {exc}")
            _finish(task, "error", error=str(exc))

    threading.Thread(target=worker, daemon=True).start()
    return jsonify({"ok": True, "task_id": task_id})


# ── API: task status ─────────────────────────────────────────────────────────
@app.route("/api/task/<task_id>")
def api_task(task_id):
    with _tasks_lock:
        task = _tasks.get(task_id)
    if not task:
        return jsonify({"ok": False, "error": "任务不存在"})
    return jsonify({
        "ok": True,
        "id": task["id"],
        "label": task["label"],
        "status": task["status"],
        "logs": list(task["logs"]),
        "result": task["result"],
        "elapsed": round((task["finished_at"] or time.time()) - task["started_at"], 1),
    })


# ── static output files ──────────────────────────────────────────────────────
@app.route("/output/<path:filename>")
def serve_output(filename):
    return send_from_directory(OUTPUT_DIR, filename)


# ── API: history records ─────────────────────────────────────────────────────
# output 目录里按任务前缀组织文件：{task_id}_stats.json / _chat.txt / _raw.json
# / _report.html / _report.png。历史列表按 task_id 聚合成一条记录。
_SUFFIX_KINDS = {
    "_stats.json": "stats",
    "_chat.txt": "chat",
    "_raw.json": "raw",
    "_ai_content.json": "ai",
    "_report.html": "html",
    "_report.png": "png",
}


def _group_history():
    records = {}
    if not os.path.isdir(OUTPUT_DIR):
        return []
    for name in os.listdir(OUTPUT_DIR):
        path = os.path.join(OUTPUT_DIR, name)
        if not os.path.isfile(path):
            continue
        for suffix, kind in _SUFFIX_KINDS.items():
            if name.endswith(suffix):
                task_id = name[: -len(suffix)]
                rec = records.setdefault(task_id, {"task_id": task_id, "files": []})
                try:
                    st = os.stat(path)
                except OSError:
                    continue
                rec["files"].append({"kind": kind, "name": name, "size": st.st_size})
                rec["mtime"] = max(rec.get("mtime", 0), st.st_mtime)
                break
    result = list(records.values())
    for rec in result:
        rec["has_report"] = any(f["kind"] == "html" for f in rec["files"])
        rec["has_ai"] = any(f["kind"] == "ai" for f in rec["files"])
        rec["has_stats"] = any(f["kind"] == "stats" for f in rec["files"])
        rec["has_chat"] = any(f["kind"] == "chat" for f in rec["files"])
    result.sort(key=lambda r: r.get("mtime", 0), reverse=True)
    return result


@app.route("/api/history")
def api_history():
    return jsonify({"ok": True, "records": _group_history()})


@app.route("/api/clear", methods=["POST"])
def api_clear():
    body = request.get_json(silent=True) or {}
    task_ids = body.get("task_ids")

    removed, failed = [], []
    if task_ids:
        wanted = {str(t) for t in task_ids}
    else:
        wanted = None  # 全部清除

    for name in os.listdir(OUTPUT_DIR) if os.path.isdir(OUTPUT_DIR) else []:
        path = os.path.join(OUTPUT_DIR, name)
        if not os.path.isfile(path) or name == ".gitkeep":
            continue
        task_id = None
        for suffix in _SUFFIX_KINDS:
            if name.endswith(suffix):
                task_id = name[: -len(suffix)]
                break
        if task_id is None:            # 非生成文件，跳过避免误删
            continue
        if wanted is not None and task_id not in wanted:
            continue
        try:
            os.remove(path)
            removed.append(name)
        except OSError as exc:
            failed.append(f"{name}: {exc}")

    # 同步清掉内存中的任务状态
    with _tasks_lock:
        if wanted is None:
            _tasks.clear()
        else:
            for tid in wanted:
                _tasks.pop(tid, None)

    return jsonify({"ok": True, "removed": len(removed), "failed": failed})


# ── utils ────────────────────────────────────────────────────────────────────
def ensure_dir(path):
    os.makedirs(path, exist_ok=True)


# ── entry ────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    ensure_dir(OUTPUT_DIR)
    print("微信日报控制台  http://127.0.0.1:8765")
    app.run(host="127.0.0.1", port=8765, debug=False, threaded=True)
