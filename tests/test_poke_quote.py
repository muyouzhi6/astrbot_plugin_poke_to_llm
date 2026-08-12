from __future__ import annotations

import importlib.util
import sys
import types
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch


PLUGIN_PATH = Path(__file__).resolve().parents[1] / "main.py"


def _decorator(*args: Any, **kwargs: Any):
    def wrapper(func):
        return func

    return wrapper


def load_plugin_module():
    astrbot = types.ModuleType("astrbot")
    api = types.ModuleType("astrbot.api")
    event = types.ModuleType("astrbot.api.event")
    filter_mod = types.ModuleType("astrbot.api.event.filter")
    star = types.ModuleType("astrbot.api.star")

    class Logger:
        def __getattr__(self, name):
            return lambda *args, **kwargs: None

    class AstrMessageEvent:
        def is_admin(self):
            return False

    class MessageChain:
        pass

    class Reply:
        def __init__(self, id):
            self.id = id

    class Context:
        pass

    class Star:
        def __init__(self, context):
            self.context = context

    for name in ("event_message_type", "on_decorating_result"):
        setattr(filter_mod, name, _decorator)
    filter_mod.EventMessageType = types.SimpleNamespace(ALL=object())
    event.AstrMessageEvent = AstrMessageEvent
    event.MessageChain = MessageChain
    event.filter = filter_mod
    components = types.ModuleType("astrbot.api.message_components")
    components.Reply = Reply
    star.Context = Context
    star.Star = Star
    api.logger = Logger()

    modules = {
        "astrbot": astrbot,
        "astrbot.api": api,
        "astrbot.api.event": event,
        "astrbot.api.event.filter": filter_mod,
        "astrbot.api.message_components": components,
        "astrbot.api.star": star,
    }
    old_modules = {name: sys.modules.get(name) for name in modules}
    sys.modules.update(modules)
    try:
        spec = importlib.util.spec_from_file_location("poke_to_llm_main", PLUGIN_PATH)
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        return module
    finally:
        for name, previous in old_modules.items():
            if previous is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = previous


class FakeEvent:
    def __init__(self, *, poke_trigger: bool, message_id: str):
        self._extras = {"_poke_trigger": poke_trigger}
        self.message_obj = types.SimpleNamespace(message_id=message_id)
        self.sent = []

    def get_extra(self, key: str, default: Any = None):
        return self._extras.get(key, default)

    def set_extra(self, key: str, value: Any) -> None:
        self._extras[key] = value

    async def send(self, chain):
        self.sent.append(chain)


class FakeMessageChain:
    def __init__(self, chain):
        self.chain = chain


class FakePokeEvent:
    def __init__(self, *, sender_id: str, is_admin: bool):
        self.message_obj = types.SimpleNamespace(
            raw_message={
                "post_type": "notice",
                "notice_type": "notify",
                "sub_type": "poke",
                "self_id": 999,
                "user_id": int(sender_id),
                "target_id": 999,
                "group_id": 123,
            }
        )
        self.unified_msg_origin = "aiocqhttp:GroupMessage:123"
        self._sender_id = sender_id
        self._is_admin = is_admin
        self._extras = {}

    def get_platform_name(self):
        return "aiocqhttp"

    def get_platform_id(self):
        return "aiocqhttp"

    def get_self_id(self):
        return "999"

    def get_sender_id(self):
        return self._sender_id

    def get_sender_name(self):
        return "Admin" if self._is_admin else "Member"

    def get_group_id(self):
        return "123"

    def is_admin(self):
        return self._is_admin

    def set_extra(self, key, value):
        self._extras[key] = value

    def request_llm(self, **kwargs):
        return kwargs


class PokeQuoteTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.module = load_plugin_module()
        self.plugin = object.__new__(self.module.PokeToLLM)

    async def test_synthetic_poke_quote_is_removed_at_send_time(self):
        synthetic_id = "6cfd202a47654302b11ede3618b19fb4"
        event = FakeEvent(poke_trigger=True, message_id=synthetic_id)
        invalid_reply = self.module.Reply(id=synthetic_id)
        valid_reply = self.module.Reply(id="12345")
        text = object()

        await self.plugin.suppress_synthetic_poke_quote(event)
        await event.send(FakeMessageChain([invalid_reply, valid_reply, text]))

        self.assertEqual(len(event.sent), 1)
        self.assertEqual(event.sent[0].chain, [valid_reply, text])
        self.assertTrue(event.get_extra("_poke_quote_filter_installed"))

    async def test_regular_message_is_unchanged(self):
        event = FakeEvent(poke_trigger=False, message_id="12345")
        original_send = event.send

        await self.plugin.suppress_synthetic_poke_quote(event)

        self.assertEqual(event.send, original_send)

    async def test_numeric_poke_message_id_keeps_normal_quote_path(self):
        event = FakeEvent(poke_trigger=True, message_id="12345")
        original_send = event.send

        await self.plugin.suppress_synthetic_poke_quote(event)

        self.assertEqual(event.send, original_send)


class CooldownTests(unittest.TestCase):
    def setUp(self):
        self.module = load_plugin_module()
        self.plugin = self.module.PokeToLLM(
            self.module.Context(),
            {"cooldown": 5.0, "group_cooldown": 15.0},
        )

    def test_different_users_in_same_group_share_cooldown(self):
        with patch.object(self.module.time, "monotonic", side_effect=[100.0, 106.0]):
            first = self.plugin._check_cooldown("user-a", "group-a")
            second = self.plugin._check_cooldown("user-b", "group-a")

        self.assertIsNone(first)
        self.assertEqual(second, "group")

    def test_group_cooldown_does_not_cross_groups(self):
        with patch.object(self.module.time, "monotonic", side_effect=[100.0, 101.0]):
            first = self.plugin._check_cooldown("user-a", "group-a")
            second = self.plugin._check_cooldown("user-b", "group-b")

        self.assertIsNone(first)
        self.assertIsNone(second)

    def test_rejected_poke_does_not_extend_group_cooldown(self):
        with patch.object(
            self.module.time,
            "monotonic",
            side_effect=[100.0, 110.0, 116.0],
        ):
            first = self.plugin._check_cooldown("user-a", "group-a")
            rejected = self.plugin._check_cooldown("user-b", "group-a")
            after_original_window = self.plugin._check_cooldown("user-b", "group-a")

        self.assertIsNone(first)
        self.assertEqual(rejected, "group")
        self.assertIsNone(after_original_window)

    def test_private_chat_only_uses_user_cooldown(self):
        with patch.object(self.module.time, "monotonic", side_effect=[100.0, 101.0]):
            first = self.plugin._check_cooldown("user-a", None)
            second = self.plugin._check_cooldown("user-b", None)

        self.assertIsNone(first)
        self.assertIsNone(second)

    def test_admin_bypasses_existing_user_and_group_cooldowns(self):
        with patch.object(self.module.time, "monotonic", return_value=100.0):
            self.assertIsNone(self.plugin._check_cooldown("admin", "group-a"))
            self.assertIsNone(
                self.plugin._check_cooldown("admin", "group-a", is_admin=True)
            )

    def test_admin_does_not_start_cooldowns_for_regular_users(self):
        with patch.object(self.module.time, "monotonic", side_effect=[101.0]):
            admin = self.plugin._check_cooldown(
                "admin",
                "group-a",
                is_admin=True,
            )
            regular = self.plugin._check_cooldown("user-a", "group-a")

        self.assertIsNone(admin)
        self.assertIsNone(regular)
        self.assertNotIn("admin", self.plugin._cooldown_map)

    def test_invalid_cooldowns_fall_back_to_defaults(self):
        plugin = self.module.PokeToLLM(
            self.module.Context(),
            {"cooldown": "nan", "group_cooldown": "inf"},
        )

        self.assertEqual(plugin._cooldown, 5.0)
        self.assertEqual(plugin._group_cooldown, 15.0)


class PromptTests(unittest.TestCase):
    def setUp(self):
        self.module = load_plugin_module()
        self.plugin = self.module.PokeToLLM(self.module.Context())

    def test_default_prompt_identifies_actor_and_scene(self):
        prompt = self.plugin._format_prompt("木有知", "1215198344", "215532038")

        self.assertIn("木有知（用户 ID：1215198344）", prompt)
        self.assertIn("在群聊里戳了你一下", prompt)
        self.assertIn("不要把其他群友的话误认成对方说的", prompt)

    def test_legacy_custom_prompt_remains_compatible(self):
        plugin = self.module.PokeToLLM(
            self.module.Context(),
            {"poke_prompt": "$username 戳了你，保留 $unknown"},
        )

        prompt = plugin._format_prompt("Alice", "123", None)

        self.assertEqual(prompt, "Alice 戳了你，保留 $unknown")


class PokeHandlerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.module = load_plugin_module()
        self.plugin = self.module.PokeToLLM(
            self.module.Context(),
            {"cooldown": 600.0, "group_cooldown": 600.0},
        )
        self.plugin._get_conversation = AsyncMock(return_value=object())
        self.plugin._group_cooldown_map["aiocqhttp:GroupMessage:123"] = (
            self.module.time.monotonic()
        )

    async def test_admin_event_bypasses_group_cooldown_and_requests_llm(self):
        event = FakePokeEvent(sender_id="1215198344", is_admin=True)

        results = [result async for result in self.plugin.on_poke(event)]

        self.assertEqual(len(results), 1)
        self.assertIn("Admin（用户 ID：1215198344）", results[0]["prompt"])
        self.assertTrue(event._extras["_poke_trigger"])

    async def test_regular_event_is_silent_during_group_cooldown(self):
        event = FakePokeEvent(sender_id="10001", is_admin=False)

        results = [result async for result in self.plugin.on_poke(event)]

        self.assertEqual(results, [])
        self.plugin._get_conversation.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
