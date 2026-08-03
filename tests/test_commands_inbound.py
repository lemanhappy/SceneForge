"""Tests for UserCommand parsing and Feishu inbound event handling."""

import asyncio
import hashlib
import json
import unittest

from commands import parse_user_command
from channels.feishu_inbound import event_to_command, handle_event, verify_signature
from services.feishu_server import FeishuWebhookHandler


def _run(coro):
    return asyncio.run(coro)


class TestParseUserCommand(unittest.TestCase):
    def test_approve(self):
        self.assertEqual(parse_user_command("通过").command_type, "approve")
        self.assertEqual(parse_user_command("发布").command_type, "approve")
        self.assertEqual(parse_user_command("OK").command_type, "approve")

    def test_revise_strips_prefix(self):
        cmd = parse_user_command("修改：主角改成女老师，结尾更温暖")
        self.assertEqual(cmd.command_type, "revise")
        self.assertEqual(cmd.text, "主角改成女老师，结尾更温暖")

    def test_revise_english(self):
        cmd = parse_user_command("revise: make it warmer")
        self.assertEqual(cmd.command_type, "revise")
        self.assertEqual(cmd.text, "make it warmer")

    def test_regenerate_extracts_shot_number(self):
        cmd = parse_user_command("重生成第 4 镜，人物表情太僵")
        self.assertEqual(cmd.command_type, "regenerate")
        self.assertEqual(cmd.shot_idx, 4)

    def test_regenerate_without_number(self):
        cmd = parse_user_command("重新生成这个镜头")
        self.assertEqual(cmd.command_type, "regenerate")
        self.assertIsNone(cmd.shot_idx)

    def test_status_pause_resume_cancel(self):
        self.assertEqual(parse_user_command("查看状态").command_type, "status")
        self.assertEqual(parse_user_command("暂停").command_type, "pause")
        self.assertEqual(parse_user_command("继续").command_type, "resume")
        self.assertEqual(parse_user_command("取消").command_type, "cancel")

    def test_default_is_new_topic(self):
        cmd = parse_user_command("做一个关于AI老师进山村的公益短片", source="feishu", session_id="s1")
        self.assertEqual(cmd.command_type, "new_topic")
        self.assertEqual(cmd.source, "feishu")
        self.assertEqual(cmd.session_id, "s1")


class TestVerifySignature(unittest.TestCase):
    def test_matches_expected_sha256(self):
        ts, nonce, key, body = "1700000000", "abc", "sk-enc", '{"a":1}'
        expected = hashlib.sha256((ts + nonce + key).encode() + body.encode()).hexdigest()
        self.assertTrue(verify_signature(ts, nonce, key, body, expected))
        self.assertFalse(verify_signature(ts, nonce, key, body, "deadbeef"))


class TestHandleEvent(unittest.TestCase):
    def test_url_verification_challenge(self):
        body = json.dumps({"type": "url_verification", "challenge": "ch-123", "token": "t"})
        self.assertEqual(handle_event(body), {"kind": "challenge", "challenge": "ch-123"})

    def test_text_message_event_decoded(self):
        body = {
            "header": {"event_type": "im.message.receive_v1"},
            "event": {
                "message": {"message_type": "text", "message_id": "om_1", "content": json.dumps({"text": "通过"})},
                "sender": {"sender_id": {"open_id": "ou_123"}},
            },
        }
        parsed = handle_event(json.dumps(body))
        self.assertEqual(parsed["kind"], "message")
        self.assertEqual(parsed["text"], "通过")
        self.assertEqual(parsed["sender_id"], "ou_123")
        self.assertEqual(parsed["message_id"], "om_1")
        cmd = event_to_command(parsed, session_id="s1")
        self.assertEqual(cmd.command_type, "approve")
        self.assertEqual(cmd.source, "feishu")

    def test_non_text_event_ignored(self):
        body = {"header": {"event_type": "im.message.receive_v1"},
                "event": {"message": {"message_type": "image"}}}
        parsed = handle_event(json.dumps(body))
        self.assertEqual(parsed["kind"], "ignored")
        self.assertIsNone(event_to_command(parsed))

    def test_encrypted_without_key_raises(self):
        with self.assertRaises(RuntimeError):
            handle_event(json.dumps({"encrypt": "deadbeef"}))


class FakeTrigger:
    def __init__(self):
        self.commands = []

    async def handle_command(self, command, sender_id="", channel=None):
        self.commands.append((command.command_type, sender_id, channel))
        return {"ok": True, "command_type": command.command_type}


class FakeChannel:
    def __init__(self):
        self.sent = []

    async def send_text(self, target, text):
        self.sent.append((target, text))


def _text_event(text, sender="ou_1", event_id="evt-default"):
    return json.dumps({
        "header": {"event_type": "im.message.receive_v1", "event_id": event_id},
        "event": {
            "message": {"message_type": "text", "message_id": "om_1", "content": json.dumps({"text": text})},
            "sender": {"sender_id": {"open_id": sender}},
        },
    })


class TestFeishuWebhookHandler(unittest.TestCase):
    def test_challenge_passthrough(self):
        handler = FeishuWebhookHandler(FakeTrigger())
        resp = _run(handler.handle_request(json.dumps({"type": "url_verification", "challenge": "ch-1"})))
        self.assertEqual(resp["status"], 200)
        self.assertEqual(resp["body"]["challenge"], "ch-1")

    def test_message_dispatches_and_acks(self):
        trigger, channel = FakeTrigger(), FakeChannel()
        handler = FeishuWebhookHandler(trigger, channel=channel)
        resp = _run(handler.handle_request(_text_event("通过")))
        self.assertEqual(resp["status"], 200)
        self.assertTrue(resp["body"]["ok"])
        self.assertEqual(trigger.commands, [("approve", "ou_1", "feishu")])
        self.assertEqual(channel.sent[0][0], "ou_1")  # acked the sender

    def test_non_message_ignored(self):
        trigger = FakeTrigger()
        handler = FeishuWebhookHandler(trigger)
        body = json.dumps({"header": {"event_type": "x"}, "event": {"message": {"message_type": "image"}}})
        resp = _run(handler.handle_request(body))
        self.assertTrue(resp["body"]["ignored"])
        self.assertEqual(trigger.commands, [])

    def test_invalid_signature_rejected(self):
        handler = FeishuWebhookHandler(FakeTrigger(), encrypt_key="k")
        headers = {"X-Lark-Signature": "bad", "X-Lark-Request-Timestamp": "1", "X-Lark-Request-Nonce": "n"}
        resp = _run(handler.handle_request(_text_event("通过"), headers=headers))
        self.assertEqual(resp["status"], 401)

    def test_valid_signature_passes(self):
        key, ts, nonce = "k", "1", "n"
        body = _text_event("通过")
        sig = hashlib.sha256((ts + nonce + key).encode() + body.encode()).hexdigest()
        handler = FeishuWebhookHandler(FakeTrigger(), encrypt_key=key)
        headers = {"X-Lark-Signature": sig, "X-Lark-Request-Timestamp": ts, "X-Lark-Request-Nonce": nonce}
        resp = _run(handler.handle_request(body, headers=headers))
        self.assertEqual(resp["status"], 200)

    def test_duplicate_event_id_is_deduped(self):
        trigger = FakeTrigger()
        handler = FeishuWebhookHandler(trigger)
        body = _text_event("通过", event_id="evt-123")
        r1 = _run(handler.handle_request(body))
        r2 = _run(handler.handle_request(body))  # Feishu re-delivery, same event_id
        self.assertEqual(r1["status"], 200)
        self.assertEqual(r2["status"], 200)
        self.assertTrue(r2["body"].get("dedup"))
        self.assertEqual(len(trigger.commands), 1)  # dispatched only once

    def test_distinct_event_ids_both_dispatch(self):
        trigger = FakeTrigger()
        handler = FeishuWebhookHandler(trigger)
        _run(handler.handle_request(_text_event("通过", event_id="a")))
        _run(handler.handle_request(_text_event("修改：再快点", event_id="b")))
        self.assertEqual(len(trigger.commands), 2)

    def test_from_config_reads_feishu_section(self):
        config = {"messaging": {"channels": [
            {"type": "feishu", "enabled": True, "encrypt_key": "ek", "default_target": "ou_def"},
        ]}}
        handler = FeishuWebhookHandler.from_config(config, FakeTrigger())
        self.assertEqual(handler.encrypt_key, "ek")
        self.assertEqual(handler.reply_target, "ou_def")


if __name__ == "__main__":
    unittest.main()
