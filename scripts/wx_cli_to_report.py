#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Convert WeChat messages from wx-cli/wechat-cli into report inputs.

Preferred source:
    wx history "群名" --since 2026-04-30 --until 2026-04-30 -n 5000 --json

Compatible fallback:
    wechat-cli history "群名" --start-time 2026-04-30 --end-time 2026-04-30 --limit 5000 --format json
"""

import argparse
import datetime as dt
import hashlib
import json
import os
import random
import re
import shutil
import sqlite3
import subprocess
import sys
import time
from collections import Counter, defaultdict

try:
    import jieba
    JIEBA_AVAILABLE = True
except ImportError:
    JIEBA_AVAILABLE = False


STOPWORDS = {
    "的", "了", "我", "是", "你", "在", "他", "她", "它", "我们", "你们", "他们",
    "这个", "那个", "就是", "可以", "不是", "没有", "一个", "一下", "现在",
    "感觉", "觉得", "应该", "可能", "已经", "还是", "什么", "怎么", "哈哈",
    "哈哈哈", "图片", "表情", "动画表情", "语音", "链接", "文件", "分享",
}

DEFAULT_WX_CACHE_DIR = os.path.expanduser("~/.wx-cli/cache")


def parse_arguments():
    parser = argparse.ArgumentParser(description="Use wx-cli/wechat-cli history output to generate stats.json and simplified_chat.txt.")
    parser.add_argument("--chatroom", help="WeChat chat/group name. Required unless --input-json includes a chat name.")
    parser.add_argument("--date", help="Single day filter, format YYYY-MM-DD.")
    parser.add_argument("--start", help="Start time/date passed to CLI.")
    parser.add_argument("--end", help="End time/date passed to CLI.")
    parser.add_argument("--limit", type=int, default=5000, help="Maximum messages to fetch from CLI.")
    parser.add_argument("--cli", choices=["auto", "wx", "wechat-cli"], default="auto", help="CLI command to use.")
    parser.add_argument("--binary", help="Explicit wx/wechat-cli binary path. Useful for a locally unpacked package.")
    parser.add_argument("--input-json", help="Use an existing wx-cli/wechat-cli JSON file instead of calling the CLI.")
    parser.add_argument("--raw-output", help="Save raw CLI JSON to this path.")
    parser.add_argument("--output-stats", default="stats.json", help="Path to output stats JSON.")
    parser.add_argument("--output-text", default="simplified_chat.txt", help="Path to output simplified chat text.")
    parser.add_argument(
        "--allow-unstable-senders",
        action="store_true",
        help="Allow legacy group JSON without sender_username. This can merge or misattribute members.",
    )
    return parser.parse_args()


def ensure_parent(path):
    parent = os.path.dirname(os.path.abspath(path))
    if parent:
        os.makedirs(parent, exist_ok=True)


def infer_cli_kind(binary):
    name = os.path.basename(binary)
    if name == "wx" or name.startswith("wx."):
        return "wx"
    return "wechat-cli"


def pick_cli(requested, binary=None):
    if binary:
        if not os.path.exists(binary):
            raise RuntimeError(f"找不到 CLI 文件：{binary}")
        return infer_cli_kind(binary), binary

    if requested != "auto":
        found = shutil.which(requested)
        if found:
            return requested, found
        raise RuntimeError(f"找不到命令：{requested}")
    for candidate in ("wx", "wechat-cli"):
        found = shutil.which(candidate)
        if found:
            return candidate, found
    raise RuntimeError("找不到 wx 或 wechat-cli。请先安装 @jackwener/wx-cli，或把本地 wechat-cli 包安装到 PATH。")


def extract_sessions(data):
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    if not isinstance(data, dict):
        return []
    for key in ("sessions", "data", "results"):
        value = data.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
        if isinstance(value, dict) and isinstance(value.get("sessions"), list):
            return [item for item in value["sessions"] if isinstance(item, dict)]
    return []


def resolve_wx_chat_username(executable, chatroom):
    """Resolve an exact display name to chatroom ID for a reliable retry."""
    result = subprocess.run(
        [executable, "sessions", "--json"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if result.returncode != 0:
        return None

    try:
        sessions = extract_sessions(load_json_text(result.stdout))
    except (ValueError, json.JSONDecodeError):
        return None

    exact_matches = [
        session for session in sessions
        if first_text(session, "chat", "name", "display_name") == chatroom
        or first_text(session, "username", "chat_id") == chatroom
    ]
    if len(exact_matches) != 1:
        return None
    return first_text(exact_matches[0], "username", "chat_id") or None


def execute_cli_command(cmd):
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def is_transient_history_lookup_error(result):
    output = f"{result.stdout}\n{result.stderr}"
    return result.returncode != 0 and "找不到" in output and "消息记录" in output


def retry_transient_history_command(cmd, attempts=5):
    """Retry the short wx-daemon refresh window, but not unrelated failures."""
    result = None
    for attempt in range(attempts):
        result = execute_cli_command(cmd)
        if result.returncode == 0 or not is_transient_history_lookup_error(result):
            return result
        if attempt + 1 < attempts:
            # A full message shard refresh normally takes 1-2 seconds and can
            # occasionally take 6+ seconds. Short retries only hit the same
            # decryption window again.
            time.sleep(min(2.0 * (attempt + 1), 6.0))
    return result


def run_cli(args):
    cli, executable = pick_cli(args.cli, args.binary)
    if not args.chatroom:
        raise RuntimeError("调用 CLI 时必须提供 --chatroom")

    start = args.start or args.date
    end = args.end or args.date

    if cli == "wx":
        cmd = [executable, "history", args.chatroom, "-n", str(args.limit), "--json"]
        if start:
            cmd.extend(["--since", start])
        if end:
            cmd.extend(["--until", end])
    else:
        cmd = [executable, "history", args.chatroom, "--limit", str(args.limit), "--format", "json"]
        if start:
            cmd.extend(["--start-time", start])
        if end:
            cmd.extend(["--end-time", end])

    result = execute_cli_command(cmd)
    retry_cmd = None
    retry_result = None
    if result.returncode != 0 and cli == "wx":
        chat_username = resolve_wx_chat_username(executable, args.chatroom)
        if chat_username or is_transient_history_lookup_error(result):
            retry_cmd = cmd.copy()
            retry_cmd[2] = chat_username or args.chatroom
            retry_result = retry_transient_history_command(retry_cmd)
            if retry_result.returncode == 0:
                return cli, retry_result.stdout

    if result.returncode != 0:
        retry_detail = ""
        if retry_cmd and retry_result:
            retry_detail = (
                f"\nRetry command: {' '.join(retry_cmd)}"
                f"\nRetry STDOUT:\n{retry_result.stdout}"
                f"\nRetry STDERR:\n{retry_result.stderr}"
            )
        raise RuntimeError(
            "读取微信聊天记录失败：\n"
            f"Command: {' '.join(cmd)}\n"
            f"STDOUT:\n{result.stdout}\n"
            f"STDERR:\n{result.stderr}"
            f"{retry_detail}"
        )
    return cli, result.stdout


def load_json_text(text):
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # Some wrappers may print hints before/after JSON. Keep a conservative fallback.
    starts = [i for i in (text.find("{"), text.find("[")) if i >= 0]
    ends = [i for i in (text.rfind("}"), text.rfind("]")) if i >= 0]
    if not starts or not ends:
        raise ValueError("CLI output is not valid JSON")
    fragment = text[min(starts):max(ends) + 1]
    return json.loads(fragment)


def load_source(args):
    if args.input_json:
        with open(args.input_json, "r", encoding="utf-8") as f:
            text = f.read()
        return "input-json", load_json_text(text), text

    cli, text = run_cli(args)
    return cli, load_json_text(text), text


def extract_messages(data):
    if isinstance(data, list):
        return data, {}
    if not isinstance(data, dict):
        return [], {}

    for key_path in (
        ("messages",),
        ("data", "messages"),
        ("result", "messages"),
        ("history", "messages"),
    ):
        cursor = data
        for key in key_path:
            if isinstance(cursor, dict):
                cursor = cursor.get(key)
            else:
                cursor = None
        if isinstance(cursor, list):
            return cursor, data

    return [], data


def load_wx_cache_manifest(cache_dir):
    manifest_path = os.path.join(cache_dir, "_mtimes.json")
    if not os.path.isfile(manifest_path):
        return {}
    try:
        with open(manifest_path, "r", encoding="utf-8") as f:
            manifest = json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}
    return manifest if isinstance(manifest, dict) else {}


def manifest_db_path(manifest, key):
    entry = manifest.get(key)
    if not isinstance(entry, dict):
        return None
    path = entry.get("path")
    return path if isinstance(path, str) and os.path.isfile(path) else None


def open_readonly_sqlite(path):
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True)


def table_exists(connection, table_name):
    row = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1",
        (table_name,),
    ).fetchone()
    return row is not None


def load_contact_display_map(contact_db_path):
    if not contact_db_path:
        return {}
    connection = open_readonly_sqlite(contact_db_path)
    try:
        if not table_exists(connection, "contact"):
            return {}
        rows = connection.execute(
            "SELECT username, remark, nick_name, alias FROM contact"
        ).fetchall()
    finally:
        connection.close()

    result = {}
    for username, remark, nick_name, alias in rows:
        if not username:
            continue
        # Reports should show the member's own WeChat nickname, not the local
        # address-book remark that only the current account can see.
        display_name = nick_name or remark or alias or username
        result[str(username)] = str(display_name)
    return result


def read_protobuf_varint(payload, offset):
    value = 0
    shift = 0
    while offset < len(payload) and shift < 70:
        byte = payload[offset]
        offset += 1
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return value, offset
        shift += 7
    raise ValueError("invalid protobuf varint")


def iter_protobuf_fields(payload):
    offset = 0
    while offset < len(payload):
        key, offset = read_protobuf_varint(payload, offset)
        field_number = key >> 3
        wire_type = key & 0x07
        if wire_type == 0:
            value, offset = read_protobuf_varint(payload, offset)
        elif wire_type == 1:
            value = payload[offset:offset + 8]
            offset += 8
        elif wire_type == 2:
            size, offset = read_protobuf_varint(payload, offset)
            value = payload[offset:offset + size]
            offset += size
        elif wire_type == 5:
            value = payload[offset:offset + 4]
            offset += 4
        else:
            raise ValueError(f"unsupported protobuf wire type: {wire_type}")
        yield field_number, wire_type, value


def load_group_nickname_map(contact_db_path, chat_username):
    """Read username -> group nickname from chat_room.ext_buffer protobuf."""
    if not contact_db_path or not chat_username:
        return {}
    connection = open_readonly_sqlite(contact_db_path)
    try:
        if not table_exists(connection, "chat_room"):
            return {}
        row = connection.execute(
            "SELECT ext_buffer FROM chat_room WHERE username=? LIMIT 1",
            (chat_username,),
        ).fetchone()
    finally:
        connection.close()
    if not row or not isinstance(row[0], bytes):
        return {}

    result = {}
    try:
        for field_number, wire_type, member_payload in iter_protobuf_fields(row[0]):
            if field_number != 1 or wire_type != 2:
                continue
            username = ""
            group_nickname = ""
            for member_field, member_wire, value in iter_protobuf_fields(member_payload):
                if member_wire != 2:
                    continue
                if member_field == 1:
                    username = value.decode("utf-8", errors="replace").strip()
                elif member_field == 2:
                    group_nickname = value.decode("utf-8", errors="replace").strip()
            if username and group_nickname:
                result[username] = group_nickname
    except (ValueError, TypeError):
        return {}
    return result


def enrich_wx_group_senders(data, cache_dir=None):
    """Recover stable sender IDs from wx's decrypted cache for legacy CLI JSON."""
    if not isinstance(data, dict) or not data.get("is_group"):
        return 0
    chat_username = first_text(data, "username", "chat_id")
    messages = data.get("messages")
    if not chat_username or not isinstance(messages, list):
        return 0

    cache_dir = os.path.expanduser(cache_dir or DEFAULT_WX_CACHE_DIR)
    manifest = load_wx_cache_manifest(cache_dir)
    if not manifest:
        return 0

    contact_db_path = manifest_db_path(manifest, "contact/contact.db")
    contact_display_map = load_contact_display_map(contact_db_path)
    group_nickname_map = load_group_nickname_map(contact_db_path, chat_username)
    table_name = f"Msg_{hashlib.md5(chat_username.encode('utf-8')).hexdigest()}"
    message_db_paths = []
    for key in sorted(manifest):
        if not key.startswith("message/message_"):
            continue
        path = manifest_db_path(manifest, key)
        if path and path not in message_db_paths:
            message_db_paths.append(path)

    connections = []
    for path in message_db_paths:
        try:
            connection = open_readonly_sqlite(path)
            if table_exists(connection, table_name) and table_exists(connection, "Name2Id"):
                connections.append(connection)
            else:
                connection.close()
        except sqlite3.Error:
            continue

    enriched_count = 0
    try:
        for message in messages:
            if not isinstance(message, dict) or first_text(message, "sender_username"):
                continue
            try:
                local_id = int(message.get("local_id"))
                timestamp = int(message.get("timestamp"))
            except (TypeError, ValueError):
                continue

            sender_username = ""
            for connection in connections:
                try:
                    sender_row = connection.execute(
                        f'SELECT real_sender_id FROM "{table_name}" '
                        "WHERE local_id=? AND create_time=? LIMIT 1",
                        (local_id, timestamp),
                    ).fetchone()
                    if not sender_row or not sender_row[0]:
                        continue
                    username_row = connection.execute(
                        "SELECT user_name FROM Name2Id WHERE rowid=? LIMIT 1",
                        (sender_row[0],),
                    ).fetchone()
                    if username_row and username_row[0]:
                        sender_username = str(username_row[0])
                        break
                except sqlite3.Error:
                    continue

            if not sender_username:
                continue
            group_nickname = group_nickname_map.get(sender_username, "")
            contact_display = contact_display_map.get(sender_username, sender_username)
            display_name = group_nickname or contact_display
            message["sender"] = display_name
            message["_sender_username"] = sender_username
            message["_sender_contact_display"] = contact_display
            if group_nickname:
                message["_sender_group_nickname"] = group_nickname
            enriched_count += 1
    finally:
        for connection in connections:
            connection.close()

    if enriched_count:
        metadata = data.setdefault("meta", {})
        if isinstance(metadata, dict):
            metadata["sender_identity_source"] = "wx_decrypted_cache"
    return enriched_count


def public_chat_output(data):
    """Return chat JSON without internal sender IDs or debug mapping fields."""
    if not isinstance(data, dict):
        return data
    output = dict(data)
    messages = data.get("messages")
    if not isinstance(messages, list):
        return output

    hidden_fields = {
        "sender_raw",
        "sender_username",
        "sender_id",
        "sender_contact_display",
        "sender_group_nickname",
    }
    output["messages"] = [
        {
            key: value
            for key, value in message.items()
            if key not in hidden_fields and not key.startswith("_sender_")
        }
        if isinstance(message, dict) else message
        for message in messages
    ]
    return output


def first_text(mapping, *keys):
    """Return the first non-empty scalar field as text."""
    for key in keys:
        value = mapping.get(key)
        if value is None or isinstance(value, (dict, list)):
            continue
        text = str(value).strip()
        if text:
            return text
    return ""


def extract_members(data):
    """Find an optional member list in common wx-cli/export wrappers."""
    if not isinstance(data, dict):
        return []

    candidates = [
        data.get("members"),
        data.get("data", {}).get("members") if isinstance(data.get("data"), dict) else None,
        data.get("result", {}).get("members") if isinstance(data.get("result"), dict) else None,
    ]
    for candidate in candidates:
        if isinstance(candidate, list):
            return [member for member in candidate if isinstance(member, dict)]
    return []


def build_member_index(data):
    """Build an exact stable-id index without guessing from display names."""
    member_by_id = {}
    display_names = set()

    for member in extract_members(data):
        sender_id = first_text(
            member,
            "username",
            "sender_username",
            "platformId",
            "platform_id",
            "user_id",
            "wxid",
        )
        group_name = first_text(member, "group_nickname", "groupNickname", "chatroom_nickname")
        contact_name = first_text(
            member,
            "contact_display",
            "accountName",
            "account_name",
            "nickname",
            "nick_name",
        )
        display_name = first_text(member, "display", "display_name") or group_name or contact_name

        for name in (group_name, contact_name, display_name):
            if name:
                display_names.add(name)

        if sender_id:
            member_by_id[sender_id] = {
                "sender_id": sender_id,
                "group_name": group_name,
                "contact_name": contact_name,
                "display_name": display_name,
            }

    return member_by_id, display_names


def looks_like_internal_sender_id(value):
    """Recognize only unambiguous WeChat IDs; custom usernames can be names too."""
    return bool(value) and (
        value.startswith("wxid_")
        or value.startswith("gh_")
        or value.endswith("@openim")
    )


def parse_time_value(value, default_date=None):
    if value is None:
        return None
    if isinstance(value, (int, float)):
        # wx-cli timestamps are seconds.
        return dt.datetime.fromtimestamp(value)
    if not isinstance(value, str):
        return None

    text = value.strip()
    formats = [
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y/%m/%d %H:%M:%S",
        "%Y/%m/%d %H:%M",
        "%Y-%m-%dT%H:%M:%S",
    ]
    for fmt in formats:
        try:
            return dt.datetime.strptime(text[:19], fmt)
        except ValueError:
            pass

    if default_date:
        for fmt in ("%H:%M:%S", "%H:%M"):
            try:
                parsed = dt.datetime.strptime(text, fmt)
                base = dt.datetime.strptime(default_date, "%Y-%m-%d")
                return base.replace(hour=parsed.hour, minute=parsed.minute, second=parsed.second)
            except ValueError:
                pass
    return None


def normalize_message(message, default_date=None, member_by_id=None, member_display_names=None):
    if not isinstance(message, dict):
        return None

    member_by_id = member_by_id or {}
    member_display_names = member_display_names or set()

    timestamp = message.get("timestamp") or message.get("create_time") or message.get("time_ts")
    parsed_time = parse_time_value(timestamp, default_date)
    if parsed_time is None:
        parsed_time = parse_time_value(message.get("time") or message.get("datetime") or message.get("created_at"), default_date)

    # Keep identity and presentation separate. New wx-cli versions expose
    # sender_username plus display fields; using a display string as an ID (or
    # vice versa) can silently attribute one member's messages to another.
    sender_id = first_text(
        message,
        "_sender_username",
        "sender_username",
        "sender_id",
        "senderId",
        "from_username",
        "fromUserName",
        "platformId",
        "platform_id",
    )
    raw_sender = first_text(message, "sender", "sender_name", "from_name", "from", "talker")
    message_group_name = first_text(
        message,
        "_sender_group_nickname",
        "sender_group_nickname",
        "groupNickname",
        "group_nickname",
        "chatroom_nickname",
    )
    message_contact_name = first_text(
        message,
        "_sender_contact_display",
        "sender_contact_display",
        "accountName",
        "account_name",
        "contact_display",
        "nickname",
        "nick_name",
    )
    explicit_display_name = first_text(message, "sender_name", "display_name", "from_name")

    # Some exporters put a stable ID directly in `sender`. Only treat it as an
    # ID when it is not also a known member display name. This matters for real
    # groups where one member's username equals another member's nickname.
    if not sender_id:
        if looks_like_internal_sender_id(raw_sender):
            sender_id = raw_sender
        elif raw_sender in member_by_id and raw_sender not in member_display_names:
            sender_id = raw_sender

    member = member_by_id.get(sender_id, {}) if sender_id else {}
    sender = (
        message_group_name
        or member.get("group_name")
        or explicit_display_name
        or message_contact_name
        or member.get("contact_name")
        or member.get("display_name")
        or (raw_sender if raw_sender and not raw_sender.endswith("@chatroom") else "")
        or sender_id
        or "未知成员"
    )
    sender_contact_name = message_contact_name or member.get("contact_name") or ""
    sender_key = f"id:{sender_id}" if sender_id else f"name:{sender}"

    content = message.get("content") or message.get("text") or message.get("message") or ""
    if isinstance(content, (dict, list)):
        content = json.dumps(content, ensure_ascii=False)
    content = str(content).replace("\r", "").strip()

    msg_type = str(message.get("type") or message.get("msg_type") or message.get("local_type") or "text")

    if not content:
        content = f"[{msg_type}]"

    return {
        "dt": parsed_time,
        "timestamp": int(parsed_time.timestamp()) if parsed_time else 0,
        "time": parsed_time.strftime("%Y-%m-%d %H:%M") if parsed_time else str(message.get("time") or ""),
        "sender": sender,
        "sender_id": sender_id,
        "sender_key": sender_key,
        "sender_contact_name": sender_contact_name,
        "content": content,
        "type": msg_type,
    }


def disambiguate_duplicate_display_names(messages):
    """Keep equal nicknames with different stable IDs as separate people."""
    keys_by_name = defaultdict(set)
    messages_by_key = defaultdict(list)
    for message in messages:
        keys_by_name[message["sender"]].add(message["sender_key"])
        messages_by_key[message["sender_key"]].append(message)

    for display_name, sender_keys in keys_by_name.items():
        if len(sender_keys) < 2:
            continue
        for sender_key in sender_keys:
            same_sender = messages_by_key[sender_key]
            contact_names = Counter(
                message["sender_contact_name"]
                for message in same_sender
                if message.get("sender_contact_name") and message["sender_contact_name"] != display_name
            )
            if contact_names:
                qualifier = contact_names.most_common(1)[0][0]
            else:
                sender_id = same_sender[0].get("sender_id") or sender_key
                qualifier = sender_id[-6:]
            label = f"{display_name}（{qualifier}）"
            for message in same_sender:
                message["sender"] = label


def normalize_messages(messages, default_date=None, source_data=None):
    member_by_id, member_display_names = build_member_index(source_data)
    normalized = [
        normalize_message(m, default_date, member_by_id, member_display_names)
        for m in messages
    ]
    normalized = [m for m in normalized if m]
    normalized.sort(key=lambda m: (m["timestamp"], m["time"]))
    disambiguate_duplicate_display_names(normalized)
    return normalized


def validate_group_sender_identity(source_data, messages, allow_unstable=False):
    """Stop before producing a report when a group source has no stable IDs."""
    is_group = isinstance(source_data, dict) and (
        source_data.get("is_group") is True or source_data.get("chat_type") == "group"
    )
    has_stable_sender = any(message.get("sender_id") for message in messages)
    if not is_group or not messages or has_stable_sender:
        return None

    identity_problem = (
        "当前聊天记录不包含 sender_username，群成员身份存在串人风险。"
        "请升级到会输出 sender_username/sender_group_nickname 的 wx-cli 版本。"
    )
    if not allow_unstable:
        raise RuntimeError(
            f"{identity_problem}已停止生成日报；如确认旧数据可接受，"
            "可显式添加 --allow-unstable-senders。"
        )
    return f"{identity_problem}已按 --allow-unstable-senders 继续处理旧数据。"


def generate_word_cloud(messages, top_n=60):
    words = []
    text = " ".join(m["content"] for m in messages if m["content"])
    if JIEBA_AVAILABLE:
        words = [w for w in jieba.cut(text) if len(w) > 1 and w not in STOPWORDS]
    else:
        words = [w for w in re.split(r"[\s,，。！？、:：;；/\\()\[\]{}<>《》\"'`]+", text) if len(w) > 1 and w not in STOPWORDS]

    counts = Counter(words).most_common(top_n)
    colors = ["#7a351f", "#287a5b", "#415f8f", "#d39b35", "#8b5a86", "#b45f2a"]
    random.seed(20260501)
    result = []
    max_count = counts[0][1] if counts else 1
    for word, count in counts:
        result.append({
            "text": word,
            "count": count,
            "size": int(min(40, max(14, 12 + (count / max_count) * 28))),
            "color": random.choice(colors),
            "left": random.randint(8, 88),
            "top": random.randint(44, 260),
            "rotate": random.randint(-12, 12),
        })
    return result


def generate_simplified_text(messages, chat_name, date_str, output_text):
    lines = [f"=== 群名称: {chat_name} | 日期: {date_str or '-'} | 消息总数: {len(messages)} ==="]
    if not messages:
        ensure_parent(output_text)
        with open(output_text, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
        return [output_text]

    groups = []
    current = []
    window_start = None
    for message in messages:
        ts = message["timestamp"]
        if window_start is None or ts - window_start <= 5 * 60:
            current.append(message)
            if window_start is None:
                window_start = ts
        else:
            groups.append(current)
            current = [message]
            window_start = ts
    if current:
        groups.append(current)

    for group in groups:
        start_time = group[0]["dt"].strftime("%H:%M") if group[0]["dt"] else group[0]["time"]
        end_time = group[-1]["dt"].strftime("%H:%M") if group[-1]["dt"] else group[-1]["time"]
        time_range = start_time if start_time == end_time else f"{start_time}~{end_time}"

        segments = []
        previous_sender_key = None
        for message in group:
            content = re.sub(r"\s+", " ", message["content"]).strip()
            if len(content) > 220:
                content = content[:220] + "..."
            sender = message["sender"]
            sender_key = message["sender_key"]
            if sender_key == previous_sender_key and segments:
                segments[-1] += "/" + content
            else:
                segments.append(f"{sender}:{content}")
                previous_sender_key = sender_key
        lines.append(f"[{time_range}] " + " | ".join(segments))

    ensure_parent(output_text)
    with open(output_text, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    return [output_text]


def build_stats(messages, meta, chat_name, date_str, output_text_paths, source_name):
    total_count = len(messages)
    active_user_keys = {m["sender_key"] for m in messages if m["sender_key"]}
    counts = Counter(m["sender_key"] for m in messages)
    display_name_by_key = {}
    for message in messages:
        display_name_by_key.setdefault(message["sender_key"], message["sender"])

    top_talkers = []
    for index, (sender_key, count) in enumerate(counts.most_common(3), 1):
        name = display_name_by_key[sender_key]
        common_words = []
        combined = " ".join(m["content"] for m in messages if m["sender_key"] == sender_key)
        if JIEBA_AVAILABLE:
            words = [w for w in jieba.cut(combined) if len(w) > 1 and w not in STOPWORDS]
            common_words = [w for w, _ in Counter(words).most_common(5)]
        top_talkers.append({
            "rank": index,
            "name": name,
            "count": count,
            "common_words": common_words,
        })

    night_messages = []
    for message in messages:
        when = message["dt"]
        if not when:
            continue
        if when.hour >= 23 or when.hour < 6:
            minutes_from_23 = (when.hour - 23 if when.hour >= 23 else when.hour + 1) * 60 + when.minute
            night_messages.append((minutes_from_23, message))

    night_owl = None
    if night_messages:
        _, latest = max(night_messages, key=lambda item: item[0])
        night_owl = {
            "name": latest["sender"],
            "last_time": latest["dt"].strftime("%H:%M") if latest["dt"] else latest["time"],
            "msg_count": sum(1 for _, m in night_messages if m["sender_key"] == latest["sender_key"]),
            "last_msg": latest["content"],
            "title": "熬夜冠军",
        }

    dated_messages = [m for m in messages if m["dt"]]
    if dated_messages:
        start = dated_messages[0]["dt"].strftime("%H:%M")
        end = dated_messages[-1]["dt"].strftime("%H:%M")
        inferred_date = dated_messages[0]["dt"].strftime("%Y-%m-%d")
    else:
        start = end = "-"
        inferred_date = date_str or "-"

    chat_from_meta = None
    if isinstance(meta, dict):
        chat_from_meta = meta.get("chat") or meta.get("name") or meta.get("display_name")
    final_chat_name = chat_name or chat_from_meta or "微信群"

    return {
        "meta": {
            "name": final_chat_name,
            "source": source_name,
            "date": date_str or inferred_date,
            "total_count": total_count,
            "active_user_count": len(active_user_keys),
            "time_range": f"{start} 至 {end}",
            "sender_identity": "stable_id" if any(m.get("sender_id") for m in messages) else "display_name_only",
        },
        "top_talkers": top_talkers,
        "night_owl": night_owl,
        "word_cloud": generate_word_cloud(messages),
        "name_avatar_map": {},
        "raw_text_paths": output_text_paths,
    }


def main():
    args = parse_arguments()
    try:
        source_name, data, raw_text = load_source(args)
        messages_raw, meta = extract_messages(data)
        enrich_wx_group_senders(data)
        chat_name = args.chatroom or (meta.get("chat") if isinstance(meta, dict) else None) or "微信群"
        date_str = args.date or args.start or ""
        if date_str and len(date_str) > 10:
            date_str = date_str[:10]

        messages = normalize_messages(messages_raw, date_str or None, meta)

        identity_warning = validate_group_sender_identity(
            meta,
            messages,
            allow_unstable=args.allow_unstable_senders,
        )
        if identity_warning:
            print(f"Warning: {identity_warning}", file=sys.stderr)

        if args.raw_output:
            ensure_parent(args.raw_output)
            with open(args.raw_output, "w", encoding="utf-8") as f:
                if isinstance(data, dict) and isinstance(data.get("messages"), list):
                    json.dump(public_chat_output(data), f, ensure_ascii=False, indent=2)
                else:
                    f.write(raw_text)

        text_paths = generate_simplified_text(messages, chat_name, date_str, args.output_text)
        stats = build_stats(messages, meta, chat_name, date_str, text_paths, source_name)

        ensure_parent(args.output_stats)
        with open(args.output_stats, "w", encoding="utf-8") as f:
            json.dump(stats, f, ensure_ascii=False, indent=2)

        print(f"Source: {source_name}")
        print(f"Messages: {len(messages)}")
        print(f"Stats saved to: {args.output_stats}")
        print(f"Simplified text saved to: {', '.join(text_paths)}")
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
