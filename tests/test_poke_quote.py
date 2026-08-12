from __future__ import annotations

import importlib.util
import sys
import types
import unittest
from pathlib import Path
from typing import Any


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
        pass

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


if __name__ == "__main__":
    unittest.main()
