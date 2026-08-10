from __future__ import annotations

import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from types import SimpleNamespace


class _Logger:
    def __getattr__(self, _name):
        return lambda *_args, **_kwargs: None


def _decorator(*_args, **_kwargs):
    def decorate(function):
        return function

    return decorate


def _command_group(_name):
    def decorate(function):
        function.command = lambda *_args, **_kwargs: _decorator()
        return function

    return decorate


class _Star:
    def __init__(self, context):
        self.context = context


class _StarTools:
    @classmethod
    def get_data_dir(cls, _name):
        return Path(tempfile.gettempdir()) / "raise-test-data"


class _Plain:
    def __init__(self, text=""):
        self.text = text


class _MessageChain:
    def __init__(self, chain=None):
        self.chain = list(chain or [])


def _install_astrbot_stubs() -> None:
    if "astrbot.api" in sys.modules:
        return
    astrbot = types.ModuleType("astrbot")
    astrbot.__path__ = []
    api = types.ModuleType("astrbot.api")
    api.__path__ = []
    api.AstrBotConfig = dict
    api.logger = _Logger()
    event = types.ModuleType("astrbot.api.event")
    event.AstrMessageEvent = object
    event.MessageChain = _MessageChain
    event.filter = types.SimpleNamespace(
        command_group=_command_group,
        permission_type=_decorator,
        event_message_type=_decorator,
        on_llm_request=_decorator,
        after_message_sent=_decorator,
        PermissionType=types.SimpleNamespace(ADMIN="admin"),
        EventMessageType=types.SimpleNamespace(GROUP_MESSAGE="group"),
    )
    components = types.ModuleType("astrbot.api.message_components")
    components.Plain = _Plain
    star = types.ModuleType("astrbot.api.star")
    star.Context = object
    star.Star = _Star
    star.StarTools = _StarTools
    sys.modules.update(
        {
            "astrbot": astrbot,
            "astrbot.api": api,
            "astrbot.api.event": event,
            "astrbot.api.message_components": components,
            "astrbot.api.star": star,
        }
    )


_install_astrbot_stubs()

from astrbot_plugin_raise.core import GrowthState
from astrbot_plugin_raise.main import RaisePlugin


class _Context:
    def __init__(self):
        self.calls: list[dict] = []

    async def llm_generate(self, **kwargs):
        self.calls.append(kwargs)
        target = "莉芙" if '"bot_id": "bot_a"' in kwargs["prompt"] else "Sirin"
        if target == "莉芙":
            text = '{"intimacy":66,"trust":44,"reason":"双向独立推断"}'
        else:
            text = '{"intimacy":80,"trust":70,"reason":"对原型较亲近"}'
        return SimpleNamespace(completion_text=text)


class RelationshipMigrationTests(unittest.IsolatedAsyncioTestCase):
    def test_botmesh_config_discovery_is_anchored_to_data_root(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            data_dir = root / "plugin_data" / "astrbot_plugin_raise"
            data_dir.mkdir(parents=True)
            plugin = RaisePlugin.__new__(RaisePlugin)
            plugin.data_dir = data_dir

            candidates = plugin._botmesh_config_candidates()
            self.assertEqual(1, len(candidates))
            self.assertTrue(candidates[0].resolve().is_relative_to(root))
            self.assertEqual({}, plugin._read_botmesh_config())

            candidates[0].parent.mkdir(parents=True)
            candidates[0].write_text('{"bots": []}', encoding="utf-8")
            self.assertEqual({"bots": []}, plugin._read_botmesh_config())

    async def test_expired_fallback_is_retried_instead_of_becoming_permanent(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            data_dir = root / "plugin_data" / "astrbot_plugin_raise"
            data_dir.mkdir(parents=True)
            config_dir = root / "config"
            config_dir.mkdir()
            botmesh = {
                "bots": [
                    {"bot_id": "source", "display_name": "来源"},
                    {"bot_id": "target", "display_name": "目标"},
                ],
                "persona_profiles": [
                    {
                        "bot_id": "source",
                        "group_id": "group",
                        "personality_prompt": "完整人格",
                        "worldview_prompt": "完整世界观",
                    }
                ],
                "relations": [
                    {
                        "source_bot_id": "source",
                        "target_bot_id": "target",
                        "group_id": "group",
                        "relation_type": "定向关系",
                        "affinity": 0.3,
                        "trust": 0.4,
                    }
                ],
            }
            (config_dir / "astrbot_plugin_botmesh_config.json").write_text(
                json.dumps(botmesh, ensure_ascii=False), encoding="utf-8"
            )
            plugin = RaisePlugin.__new__(RaisePlugin)
            plugin.context = _Context()
            plugin.config = {
                "initial_source": "auto",
                "extraction_provider_id": "provider",
            }
            plugin.data_dir = data_dir
            scope = {
                "scope_id": "scope",
                "bot_id": "source",
                "logical_group_id": "group",
                "memory_key": "source",
                "display_name": "来源",
                "umo": "umo",
            }
            relation = plugin._relationship_candidates(scope)["target"]
            source_profile = plugin._resolved_persona_profile(
                botmesh, bot_id="source", group_id="group"
            )
            target_profile = plugin._target_profile(
                botmesh,
                target={
                    "member_id": "target",
                    "kind": "bot",
                    "target_bot_id": "target",
                },
                group_id="group",
            )
            fingerprint = plugin._relationship_fingerprint(
                source_profile=source_profile,
                target_profile=target_profile,
                relationship=relation,
            )
            state = GrowthState(
                scope_id="scope",
                bot_id="source",
                logical_group_id="group",
                members={
                    "target": {
                        "member_id": "target",
                        "name": "目标",
                        "kind": "bot",
                        "target_bot_id": "target",
                        "intimacy": 30,
                        "trust": 40,
                        "events": [],
                        "relationship_schema_version": 2,
                        "baseline_source": "relation_fallback",
                        "baseline_fingerprint": fingerprint,
                        "baseline_retry_after": 0,
                    }
                },
                relationship_schema_version=2,
            )
            path = data_dir / "growth" / "state.json"

            changed = await plugin._reconcile_relationships(state, path, scope)

            self.assertTrue(changed)
            self.assertEqual(
                state.members["target"]["baseline_source"],
                "persona_worldview_llm",
            )
            self.assertEqual(state.members["target"]["baseline_retry_after"], 0)
            self.assertEqual(len(plugin.context.calls), 1)

    async def test_missing_reverse_bot_is_backfilled_and_history_is_replayed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            data_dir = root / "plugin_data" / "astrbot_plugin_raise"
            data_dir.mkdir(parents=True)
            config_dir = root / "config"
            config_dir.mkdir()
            bot_a = "bot_a"
            bot_b = "bot_b"
            group = "group"
            botmesh = {
                "bots": [
                    {"bot_id": bot_a, "display_name": "莉芙"},
                    {"bot_id": bot_b, "display_name": "蔚来"},
                ],
                "users": [
                    {
                        "user_id": "Sirin",
                        "display_name": "Sirin",
                        "description": "Sirin 是莉芙的原型",
                    }
                ],
                "persona_profiles": [
                    {"bot_id": bot_b, "group_id": "", "personality_prompt": "蔚来全局人格"},
                    {"bot_id": bot_b, "group_id": group, "personality_prompt": "蔚来群人格", "worldview_prompt": "蔚来群世界观"},
                    {"bot_id": bot_a, "group_id": group, "personality_prompt": "莉芙群人格", "worldview_prompt": "莉芙群世界观"},
                ],
                "relations": [
                    {"source_bot_id": bot_b, "target_bot_id": bot_a, "group_id": group, "relation_type": "蔚来对莉芙", "affinity": 0.4, "trust": 0.4},
                    {"source_bot_id": bot_b, "target_bot_id": "Sirin", "group_id": group, "relation_type": "原型", "affinity": 1.0, "trust": 1.0},
                ],
            }
            (config_dir / "astrbot_plugin_botmesh_config.json").write_text(
                json.dumps(botmesh, ensure_ascii=False), encoding="utf-8"
            )
            plugin = RaisePlugin.__new__(RaisePlugin)
            plugin.context = _Context()
            plugin.config = {
                "initial_source": "auto",
                "initial_intimacy": 0,
                "initial_trust": 0,
                "extraction_provider_id": "provider",
                "extraction_max_tokens": 300,
                "extraction_timeout_seconds": 10,
            }
            plugin.data_dir = data_dir
            state = GrowthState(
                scope_id="scope",
                bot_id=bot_b,
                logical_group_id=group,
                memory_key="蔚来",
                display_name="蔚来",
                intimacy=83,
                trust=86,
                members={
                    "Sirin": {
                        "member_id": "Sirin",
                        "name": "Sirin",
                        "kind": "user",
                        "intimacy": 100,
                        "trust": 100,
                        "events": [
                            {"at": 1, "intimacy_delta": 2, "trust_delta": 1}
                        ],
                    }
                },
            )
            path = data_dir / "growth" / "state.json"
            scope = {
                "scope_id": "scope",
                "bot_id": bot_b,
                "logical_group_id": group,
                "memory_key": "蔚来",
                "display_name": "蔚来",
                "umo": "umo",
            }

            changed = await plugin._reconcile_relationships(state, path, scope)

            self.assertTrue(changed)
            self.assertIn(bot_a, state.members)
            reverse = state.members[bot_a]
            self.assertEqual((reverse["intimacy"], reverse["trust"]), (66, 44))
            self.assertEqual(reverse["baseline_source"], "persona_worldview_llm")
            self.assertEqual((state.members["Sirin"]["intimacy"], state.members["Sirin"]["trust"]), (82, 71))
            self.assertEqual(state.relationship_schema_version, 2)
            self.assertEqual((state.intimacy, state.trust), (74, 58))
            prompts = "\n".join(call["prompt"] for call in plugin.context.calls)
            self.assertIn("蔚来群人格", prompts)
            self.assertIn("蔚来群世界观", prompts)
            self.assertIn("莉芙群世界观", prompts)
            self.assertIn("Sirin 是莉芙的原型", prompts)
            self.assertTrue(path.is_file())


if __name__ == "__main__":
    unittest.main()
