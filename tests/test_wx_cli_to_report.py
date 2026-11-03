import hashlib
import json
import sqlite3
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from scripts.wx_cli_to_report import (
    build_stats,
    enrich_wx_group_senders,
    generate_simplified_text,
    normalize_messages,
    public_chat_output,
    retry_transient_history_command,
    run_cli,
    validate_group_sender_identity,
)


def protobuf_varint(value):
    encoded = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            encoded.append(byte | 0x80)
        else:
            encoded.append(byte)
            return bytes(encoded)


def protobuf_text_field(field_number, value):
    payload = value.encode("utf-8")
    return protobuf_varint((field_number << 3) | 2) + protobuf_varint(len(payload)) + payload


class SenderIdentityTests(unittest.TestCase):
    def test_explicit_display_name_wins_over_sender_id(self):
        messages = normalize_messages(
            [
                {
                    "timestamp": 1777511400,
                    "sender": "wxid_alice",
                    "sender_name": "群里的小爱",
                    "content": "你好",
                }
            ],
            "2026-04-30",
        )

        self.assertEqual(messages[0]["sender"], "群里的小爱")
        self.assertEqual(messages[0]["sender_id"], "wxid_alice")

    def test_stable_wx_cli_fields_are_kept_separate(self):
        messages = normalize_messages(
            [
                {
                    "timestamp": 1777511400,
                    "sender": "可能重复的昵称",
                    "sender_username": "wxid_alice",
                    "sender_contact_display": "爱丽丝",
                    "sender_group_nickname": "项目小爱",
                    "content": "第一条",
                }
            ],
            "2026-04-30",
        )

        self.assertEqual(messages[0]["sender"], "项目小爱")
        self.assertEqual(messages[0]["sender_id"], "wxid_alice")
        self.assertEqual(messages[0]["sender_key"], "id:wxid_alice")

    def test_member_table_resolves_a_real_internal_id(self):
        source = {
            "members": [
                {
                    "username": "wxid_alice",
                    "group_nickname": "项目小爱",
                    "contact_display": "爱丽丝",
                }
            ]
        }
        messages = normalize_messages(
            [{"timestamp": 1777511400, "sender": "wxid_alice", "content": "你好"}],
            "2026-04-30",
            source,
        )

        self.assertEqual(messages[0]["sender"], "项目小爱")
        self.assertEqual(messages[0]["sender_id"], "wxid_alice")

    def test_display_name_is_not_remapped_as_another_members_username(self):
        source = {
            "members": [
                {"username": "wxid_alice", "display": "alice"},
                {"username": "alice", "display": "bob"},
            ]
        }
        messages = normalize_messages(
            [{"timestamp": 1777511400, "sender": "alice", "content": "属于 alice"}],
            "2026-04-30",
            source,
        )

        self.assertEqual(messages[0]["sender"], "alice")
        self.assertEqual(messages[0]["sender_id"], "")

    def test_same_nickname_with_different_ids_is_not_merged(self):
        raw_messages = [
            {
                "timestamp": 1777511400,
                "sender": "小王",
                "sender_username": "wxid_wang_a",
                "sender_contact_display": "王甲",
                "sender_group_nickname": "小王",
                "content": "甲的消息",
            },
            {
                "timestamp": 1777511460,
                "sender": "小王",
                "sender_username": "wxid_wang_b",
                "sender_contact_display": "王乙",
                "sender_group_nickname": "小王",
                "content": "乙的消息",
            },
        ]
        messages = normalize_messages(raw_messages, "2026-04-30")

        self.assertEqual(messages[0]["sender"], "小王（王甲）")
        self.assertEqual(messages[1]["sender"], "小王（王乙）")
        self.assertNotEqual(messages[0]["sender_key"], messages[1]["sender_key"])

        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir) / "simplified.txt"
            generate_simplified_text(messages, "测试群", "2026-04-30", str(output))
            text = output.read_text(encoding="utf-8")

        self.assertIn("小王（王甲）:甲的消息", text)
        self.assertIn("小王（王乙）:乙的消息", text)

        stats = build_stats(messages, {}, "测试群", "2026-04-30", [], "test")
        self.assertEqual(stats["meta"]["active_user_count"], 2)
        self.assertEqual(
            {talker["name"] for talker in stats["top_talkers"]},
            {"小王（王甲）", "小王（王乙）"},
        )

    def test_legacy_wx_output_is_corrected_from_decrypted_cache(self):
        chat_username = "123@chatroom"
        table_name = f"Msg_{hashlib.md5(chat_username.encode('utf-8')).hexdigest()}"
        data = {
            "chat": "测试群",
            "chat_type": "group",
            "is_group": True,
            "username": chat_username,
            "messages": [
                {
                    "local_id": 23158,
                    "timestamp": 1793678800,
                    "sender": "错误名称",
                    "content": "长得挺好看",
                },
                {
                    "local_id": 23159,
                    "timestamp": 1793678801,
                    "sender": "另一个错误名称",
                    "content": "没有群昵称",
                }
            ],
        }

        with tempfile.TemporaryDirectory() as temp_dir:
            cache_dir = Path(temp_dir)
            contact_db = cache_dir / "contact.db"
            message_db = cache_dir / "message.db"

            connection = sqlite3.connect(contact_db)
            connection.execute(
                "CREATE TABLE contact ("
                "username TEXT, remark TEXT, nick_name TEXT, alias TEXT)"
            )
            connection.executemany(
                "INSERT INTO contact VALUES (?, ?, ?, ?)",
                [
                    ("sender_user", "我的本地备注", "Salt", ""),
                    ("sender_without_group_name", "另一个备注", "原始微信昵称", ""),
                ],
            )
            member_payload = (
                protobuf_text_field(1, "sender_user")
                + protobuf_text_field(2, "群内昵称")
            )
            room_payload = (
                protobuf_varint((1 << 3) | 2)
                + protobuf_varint(len(member_payload))
                + member_payload
            )
            connection.execute(
                "CREATE TABLE chat_room (username TEXT, ext_buffer BLOB)"
            )
            connection.execute(
                "INSERT INTO chat_room VALUES (?, ?)",
                (chat_username, room_payload),
            )
            connection.commit()
            connection.close()

            connection = sqlite3.connect(message_db)
            connection.execute("CREATE TABLE Name2Id (user_name TEXT PRIMARY KEY, is_session INTEGER)")
            connection.execute(
                f'CREATE TABLE "{table_name}" ('
                "local_id INTEGER, real_sender_id INTEGER, create_time INTEGER)"
            )
            connection.execute(
                "INSERT INTO Name2Id (rowid, user_name, is_session) VALUES (?, ?, ?)",
                (165, "sender_user", 0),
            )
            connection.execute(
                "INSERT INTO Name2Id (rowid, user_name, is_session) VALUES (?, ?, ?)",
                (166, "sender_without_group_name", 0),
            )
            connection.executemany(
                f'INSERT INTO "{table_name}" VALUES (?, ?, ?)',
                [
                    (23158, 165, 1793678800),
                    (23159, 166, 1793678801),
                ],
            )
            connection.commit()
            connection.close()

            manifest = {
                "contact/contact.db": {"path": str(contact_db)},
                "message/message_4.db": {"path": str(message_db)},
            }
            (cache_dir / "_mtimes.json").write_text(
                json.dumps(manifest),
                encoding="utf-8",
            )

            count = enrich_wx_group_senders(data, str(cache_dir))

        self.assertEqual(count, 2)
        message = data["messages"][0]
        self.assertEqual(message["sender"], "群内昵称")
        self.assertNotIn("sender_raw", message)
        self.assertEqual(message["_sender_username"], "sender_user")
        self.assertEqual(data["messages"][1]["sender"], "原始微信昵称")
        self.assertEqual(data["meta"]["sender_identity_source"], "wx_decrypted_cache")

        public_message = public_chat_output(data)["messages"][0]
        self.assertEqual(public_message["sender"], "群内昵称")
        self.assertNotIn("sender_raw", public_message)
        self.assertNotIn("sender_username", public_message)
        self.assertFalse(any(key.startswith("_sender_") for key in public_message))

    def test_legacy_group_without_stable_ids_fails_closed(self):
        messages = normalize_messages(
            [{"timestamp": 1777511400, "sender": "小王", "content": "无法确认身份"}],
            "2026-04-30",
        )
        source = {"chat_type": "group", "is_group": True}

        with self.assertRaisesRegex(RuntimeError, "串人风险"):
            validate_group_sender_identity(source, messages)

        warning = validate_group_sender_identity(source, messages, allow_unstable=True)
        self.assertIn("sender_username", warning)

    @patch("scripts.wx_cli_to_report.pick_cli", return_value=("wx", "/fake/wx"))
    @patch("scripts.wx_cli_to_report.subprocess.run")
    def test_wx_history_retries_with_exact_chatroom_id(self, run_mock, _pick_cli_mock):
        run_mock.side_effect = [
            SimpleNamespace(returncode=1, stdout="", stderr="错误: 找不到群消息记录"),
            SimpleNamespace(
                returncode=0,
                stdout='{"sessions":[{"chat":"测试群","username":"123@chatroom"}]}',
                stderr="",
            ),
            SimpleNamespace(
                returncode=0,
                stdout='{"chat":"测试群","messages":[]}',
                stderr="",
            ),
        ]
        args = Namespace(
            cli="wx",
            binary=None,
            chatroom="测试群",
            limit=50,
            start=None,
            end=None,
            date="2026-11-03",
        )

        cli, output = run_cli(args)

        self.assertEqual(cli, "wx")
        self.assertIn('"messages":[]', output)
        self.assertEqual(run_mock.call_count, 3)
        retry_command = run_mock.call_args_list[2].args[0]
        self.assertEqual(retry_command[2], "123@chatroom")

    @patch("scripts.wx_cli_to_report.time.sleep")
    @patch("scripts.wx_cli_to_report.subprocess.run")
    def test_transient_daemon_lookup_is_retried(self, run_mock, sleep_mock):
        failure = SimpleNamespace(
            returncode=1,
            stdout="",
            stderr="错误: 找不到 测试群 的消息记录",
        )
        success = SimpleNamespace(returncode=0, stdout='{"messages":[]}', stderr="")
        run_mock.side_effect = [failure, failure, success]

        result = retry_transient_history_command(
            ["/fake/wx", "history", "123@chatroom", "--json"]
        )

        self.assertEqual(result.returncode, 0)
        self.assertEqual(run_mock.call_count, 3)
        self.assertEqual(sleep_mock.call_count, 2)
        self.assertEqual(
            [call.args[0] for call in sleep_mock.call_args_list],
            [2.0, 4.0],
        )


if __name__ == "__main__":
    unittest.main()
