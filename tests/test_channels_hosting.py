"""Tests for ArtifactHost, messaging channels, and the dispatcher.

Feishu is tested against a fake HTTP session so no credentials or network are
needed; live wiring is exercised only when real FEISHU_* env vars are provided.
"""

import asyncio
import json
import os
import tempfile
import unittest
from types import SimpleNamespace

from artifacts import ArtifactHost, HostedArtifact
from channels import ChannelDispatcher, ConsoleChannel, FeishuChannel, WeChatChannel, format_review


def run(coro):
    return asyncio.run(coro)


class TestArtifactHost(unittest.TestCase):
    def test_from_config(self):
        self.assertIsNone(ArtifactHost.from_config({}))
        self.assertIsNone(ArtifactHost.from_config({"hosting": {"type": "local_static", "public_base_url": "https://x/v", "local_root": "r"}}))
        host = ArtifactHost.from_config({"hosting": {"enabled": True, "type": "local_static", "public_base_url": "https://x/v", "local_root": "r"}})
        self.assertIsNotNone(host)
        with self.assertRaises(ValueError):
            ArtifactHost.from_config({"hosting": {"enabled": True, "type": "s3", "public_base_url": "https://x/v"}})

    def test_upload_copies_and_builds_url(self):
        with tempfile.TemporaryDirectory() as root:
            src = os.path.join(root, "final_video.mp4")
            open(src, "w", encoding="utf-8").write("video-bytes")
            host = ArtifactHost(public_base_url="https://cdn.example.com/sceneforge/", local_root=os.path.join(root, "pub"))
            art = run(host.upload(src))
            self.assertIsInstance(art, HostedArtifact)
            self.assertTrue(art.url.startswith("https://cdn.example.com/sceneforge/"))
            self.assertTrue(art.name.endswith("_final_video.mp4"))
            self.assertTrue(os.path.exists(art.local_path))
            self.assertEqual(art.size, len("video-bytes"))
            # stable name across repeated uploads of the same source path
            self.assertEqual(art.name, run(host.upload(src)).name)

    def test_upload_missing_raises(self):
        with tempfile.TemporaryDirectory() as root:
            host = ArtifactHost("https://x", os.path.join(root, "pub"))
            with self.assertRaises(FileNotFoundError):
                run(host.upload(os.path.join(root, "nope.mp4")))


class TestFormatReview(unittest.TestCase):
    def test_dict_and_defaults(self):
        text = format_review({"stage": "script", "summary": "一个温暖的故事"})
        self.assertIn("【剧本审核】", text)
        self.assertIn("一个温暖的故事", text)
        self.assertIn("请回复：", text)
        self.assertIn("通过", text)

    def test_object_with_options_and_refs(self):
        review = SimpleNamespace(stage="character", summary="3 个角色", artifact_refs=["a.json"], options=["通过", "重新生成 校长"])
        text = format_review(review)
        self.assertIn("【角色设定审核】", text)
        self.assertIn("- a.json", text)
        self.assertIn("重新生成 校长", text)


class TestConsoleChannel(unittest.TestCase):
    def test_send_and_receive(self):
        ch = ConsoleChannel(echo=False)
        run(ch.send_text("u1", "hi"))
        run(ch.send_artifact("u1", {"url": "http://x/v.mp4"}))
        self.assertEqual(ch.sent[0], {"kind": "text", "target": "u1", "text": "hi"})
        self.assertEqual(ch.sent[1]["url"], "http://x/v.mp4")
        ch.push_inbound("做一个短片")
        self.assertEqual(run(ch.receive()), ["做一个短片"])
        self.assertEqual(run(ch.receive()), [])  # drained


class FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


class FakeSession:
    def __init__(self, token_payload=None, message_payload=None):
        self.token_payload = token_payload or {"code": 0, "tenant_access_token": "t-abc", "expire": 7200}
        self.message_payload = message_payload or {"code": 0, "data": {"message_id": "om_1"}}
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if url.endswith("/tenant_access_token/internal"):
            return FakeResponse(self.token_payload)
        return FakeResponse(self.message_payload)


class TestFeishuChannel(unittest.TestCase):
    def test_send_text_fetches_token_then_posts_message(self):
        session = FakeSession()
        ch = FeishuChannel(app_id="cli", app_secret="sec", session=session)
        result = run(ch.send_text("ou_123", "你好"))
        self.assertEqual(result["code"], 0)
        # token call + message call
        self.assertEqual(len(session.calls), 2)
        msg_url, msg_kwargs = session.calls[1]
        self.assertTrue(msg_url.endswith("/im/v1/messages"))
        self.assertEqual(msg_kwargs["params"]["receive_id_type"], "open_id")
        self.assertEqual(msg_kwargs["headers"]["Authorization"], "Bearer t-abc")
        body = msg_kwargs["json"]
        self.assertEqual(body["receive_id"], "ou_123")
        self.assertEqual(json.loads(body["content"])["text"], "你好")
        # token cached -> no second token fetch
        run(ch.send_text("ou_123", "再来"))
        token_calls = [c for c in session.calls if c[0].endswith("/tenant_access_token/internal")]
        self.assertEqual(len(token_calls), 1)

    def test_token_error_raises(self):
        ch = FeishuChannel("cli", "sec", session=FakeSession(token_payload={"code": 99, "msg": "bad app"}))
        with self.assertRaises(RuntimeError):
            run(ch.send_text("ou_1", "x"))

    def test_send_artifact_formats_link(self):
        session = FakeSession()
        ch = FeishuChannel("cli", "sec", session=session)
        run(ch.send_artifact("ou_1", HostedArtifact(name="AI 老师进山村", url="https://x/v.mp4")))
        body = session.calls[1][1]["json"]
        text = json.loads(body["content"])["text"]
        self.assertIn("成片已生成", text)
        self.assertIn("https://x/v.mp4", text)
        self.assertIn("AI 老师进山村", text)


class TestChannelDispatcher(unittest.TestCase):
    def test_from_config_filters_enabled_and_expands_env(self):
        os.environ["FEISHU_APP_ID"] = "env-app"
        os.environ["FEISHU_APP_SECRET"] = "env-sec"
        try:
            cfg = {"messaging": {"outbound_enabled": True, "channels": [
                {"type": "feishu", "enabled": True, "app_id": "${FEISHU_APP_ID}", "app_secret": "${FEISHU_APP_SECRET}", "default_target": "ou_x"},
                {"type": "wechat", "enabled": False, "adapter": "personal_wechat"},
            ]}}
            disp = ChannelDispatcher.from_config(cfg)
        finally:
            del os.environ["FEISHU_APP_ID"]
            del os.environ["FEISHU_APP_SECRET"]
        self.assertEqual(len(disp.channels), 1)
        channel, target = disp.channels[0]
        self.assertIsInstance(channel, FeishuChannel)
        self.assertEqual(channel.app_id, "env-app")
        self.assertEqual(target, "ou_x")

    def test_from_config_none_when_no_enabled_channels(self):
        self.assertIsNone(ChannelDispatcher.from_config({}))
        self.assertIsNone(ChannelDispatcher.from_config({"messaging": {"channels": [{"type": "feishu", "enabled": False}]}}))

    def test_broadcast_routes_to_default_target(self):
        ch = ConsoleChannel(echo=False)
        disp = ChannelDispatcher(channels=[(ch, "u-default")])
        run(disp.broadcast_text("hello"))
        run(disp.broadcast_review({"stage": "final", "summary": "done"}))
        self.assertEqual(ch.sent[0]["target"], "u-default")
        self.assertIn("成片审核", ch.sent[1]["text"])

    def test_outbound_disabled_sends_nothing(self):
        ch = ConsoleChannel(echo=False)
        disp = ChannelDispatcher(channels=[(ch, "u")], outbound_enabled=False)
        self.assertEqual(run(disp.broadcast_text("x")), [])
        self.assertEqual(ch.sent, [])

    def test_wechat_stub_raises(self):
        with self.assertRaises(NotImplementedError):
            run(WeChatChannel().send_text("u", "x"))


if __name__ == "__main__":
    unittest.main()
