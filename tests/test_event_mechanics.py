from __future__ import annotations

import unittest

from astrbot_plugin_raise.core import (
    GrowthState,
    apply_event,
    apply_delta,
    choose_event,
    directed_relationships,
    match_event_choice,
    merge_event_story_text,
    normalize_context_event,
    replay_relationship,
    sync_aggregate_from_members,
)


def _state() -> GrowthState:
    return GrowthState(
        scope_id="group",
        bot_id="bot",
        logical_group_id="group",
        intimacy=50,
        trust=50,
    )


class EventMechanicsTests(unittest.TestCase):
    def test_story_rewrite_preserves_all_trusted_mechanics(self):
        template = {
            "id": "trusted",
            "special": True,
            "options": [
                {
                    "key": "A",
                    "text": "old A",
                    "deltas": {"intimacy": 3, "trust": 2, "exp": 50},
                    "habit": "记住约定",
                    "outcome": "old outcome A",
                },
                {
                    "key": "B",
                    "text": "old B",
                    "deltas": {"intimacy": -2, "trust": -1},
                    "ending": "stay",
                    "ending_label": "留下来",
                    "outcome": "old outcome B",
                },
            ],
        }
        rewritten = {
            "title": "上下文标题",
            "story": "上下文剧情",
            "options": [
                {"key": "A", "text": "new A", "outcome": "new outcome A"},
                {"key": "B", "text": "new B", "outcome": "new outcome B"},
            ],
        }

        merged = merge_event_story_text(template, rewritten)

        self.assertEqual(merged["title"], "上下文标题")
        self.assertEqual(merged["options"][0]["text"], "new A")
        self.assertEqual(merged["options"][0]["deltas"], template["options"][0]["deltas"])
        self.assertEqual(merged["options"][0]["habit"], "记住约定")
        self.assertEqual(merged["options"][1]["ending"], "stay")
        self.assertEqual(merged["options"][1]["ending_label"], "留下来")

    def test_incomplete_option_rewrite_is_ignored_as_a_unit(self):
        template = {
            "options": [
                {"key": "A", "text": "old A", "deltas": {"intimacy": 2}},
                {"key": "B", "text": "old B", "deltas": {"trust": -1}},
            ]
        }
        merged = merge_event_story_text(
            template,
            {"options": [{"key": "A", "text": "new A"}]},
        )
        self.assertEqual(merged["options"], template["options"])

    def test_context_event_is_dynamic_but_bounded(self):
        payload = {
            "title": "刚才约好的夜跑",
            "story": "你们接着刚才的约定走到门口。",
            "special": True,
            "ultimate": True,
            "options": [
                {
                    "key": "A",
                    "text": "一起出发",
                    "deltas": {"intimacy": 99, "trust": 4, "exp": 999},
                    "habit": "一起运动",
                    "ending": "forged",
                    "outcome": "她跟上了你的脚步。",
                },
                {
                    "key": "B",
                    "text": "今晚先休息",
                    "deltas": {"intimacy": 0, "trust": 0, "exp": 0},
                    "outcome": "你们把计划留到了明天。",
                },
            ],
        }

        event = normalize_context_event(payload, event_id="context_1", max_single_delta=3)

        self.assertIsNotNone(event)
        assert event is not None
        self.assertEqual(event["options"][0]["deltas"], {"intimacy": 3, "trust": 3, "exp": 20})
        self.assertNotIn("special", event)
        self.assertNotIn("ultimate", event)
        self.assertNotIn("ending", event["options"][0])

    def test_context_event_requires_a_non_reward_choice(self):
        payload = {
            "title": "无代价奖励",
            "story": "每个选择都领奖。",
            "options": [
                {"key": "A", "text": "A", "deltas": {"intimacy": 1}, "outcome": "A"},
                {"key": "B", "text": "B", "deltas": {"trust": 1}, "outcome": "B"},
            ],
        }
        self.assertIsNone(normalize_context_event(payload, event_id="bad"))

    def test_positive_and_negative_ordinary_results_apply(self):
        event = normalize_context_event(
            {
                "title": "选择",
                "story": "现在要做决定。",
                "options": [
                    {"key": "A", "text": "靠近", "deltas": {"intimacy": 2, "trust": 1}, "outcome": "靠近了。"},
                    {"key": "B", "text": "拒绝", "deltas": {"intimacy": -2, "trust": -1}, "outcome": "拒绝了。"},
                ],
            },
            event_id="context_apply",
        )
        assert event is not None
        positive = _state()
        applied_positive = apply_event(positive, event, event["options"][0], now=1000)
        self.assertEqual(applied_positive["intimacy_delta"], 2)
        self.assertEqual(applied_positive["trust_delta"], 1)
        negative = _state()
        applied_negative = apply_event(negative, event, event["options"][1], now=1000)
        self.assertEqual(applied_negative["intimacy_delta"], -2)
        self.assertEqual(applied_negative["trust_delta"], -1)

    def test_special_reward_survives_story_localization(self):
        template = {
            "id": "bond_contract",
            "title": "羁绊",
            "special": True,
            "options": [
                {"key": "A", "text": "答应", "deltas": {"intimacy": 3, "trust": 3, "exp": 50}, "outcome": "约定成立。"}
            ],
        }
        merged = merge_event_story_text(
            template,
            {"options": [{"key": "A", "text": "郑重答应", "outcome": "她记住了。"}]},
        )
        state = _state()
        applied = apply_event(state, merged, merged["options"][0], now=1000)
        self.assertGreaterEqual(applied["exp_gained"], 50)
        self.assertEqual(state.special_events_completed, ["bond_contract"])

    def test_ultimate_ending_survives_story_localization(self):
        template = {
            "id": "apple_tree",
            "title": "苹果树",
            "ultimate": True,
            "options": [
                {
                    "key": "A",
                    "text": "留下",
                    "ending": "stay",
                    "ending_label": "留下来",
                    "deltas": {"intimacy": 3, "trust": 2, "exp": 80},
                    "outcome": "她留下了。",
                }
            ],
        }
        merged = merge_event_story_text(
            template,
            {"options": [{"key": "A", "text": "握住她的手", "outcome": "她点了点头。"}]},
        )
        state = _state()
        apply_event(state, merged, merged["options"][0], now=1000)
        self.assertEqual(state.ending["ending_id"], "stay")
        self.assertEqual(state.ending["label"], "留下来")

    def test_fourth_choice_can_be_selected(self):
        options = [{"key": key, "text": key} for key in "ABCD"]
        self.assertEqual(match_event_choice("4", options)["key"], "D")
        self.assertEqual(match_event_choice("丁", options)["key"], "D")

    def test_member_relationships_update_independently(self):
        state = _state()
        state.members = {
            "alice": {
                "member_id": "alice",
                "name": "Alice",
                "kind": "user",
                "intimacy": 20,
                "trust": 30,
                "events": [],
            },
            "bot_b": {
                "member_id": "bot_b",
                "name": "Bot B",
                "kind": "bot",
                "target_bot_id": "bot_b",
                "intimacy": 80,
                "trust": 70,
                "events": [],
            },
        }
        sync_aggregate_from_members(state)

        applied = apply_delta(
            state,
            intimacy_delta=3,
            trust_delta=2,
            reason="Alice helped",
            source_kind="raise_growth",
            user_id="alice",
            user_name="Alice",
            now=1000,
        )

        self.assertEqual((state.members["alice"]["intimacy"], state.members["alice"]["trust"]), (23, 32))
        self.assertEqual((state.members["bot_b"]["intimacy"], state.members["bot_b"]["trust"]), (80, 70))
        self.assertEqual((state.intimacy, state.trust), (52, 51))
        self.assertEqual(applied["member_id"], "alice")
        self.assertEqual(state.events[-1]["member_id"], "alice")

    def test_daily_caps_are_per_member(self):
        state = _state()
        for member_id in ("alice", "bob"):
            state.members[member_id] = {
                "member_id": member_id,
                "name": member_id,
                "kind": "user",
                "intimacy": 10,
                "trust": 10,
                "events": [],
            }
        first = apply_delta(
            state,
            intimacy_delta=3,
            trust_delta=0,
            reason="",
            source_kind="raise_growth",
            user_id="alice",
            now=1000,
            daily_intimacy_cap=3,
        )
        second = apply_delta(
            state,
            intimacy_delta=3,
            trust_delta=0,
            reason="",
            source_kind="raise_growth",
            user_id="alice",
            now=1001,
            daily_intimacy_cap=3,
        )
        other = apply_delta(
            state,
            intimacy_delta=3,
            trust_delta=0,
            reason="",
            source_kind="raise_growth",
            user_id="bob",
            now=1002,
            daily_intimacy_cap=3,
        )
        self.assertEqual(first["intimacy_delta"], 3)
        self.assertEqual(second["intimacy_delta"], 0)
        self.assertEqual(other["intimacy_delta"], 3)

    def test_replay_moves_history_onto_new_persona_baseline(self):
        events = [
            {"at": 1, "intimacy_delta": 3, "trust_delta": 1},
            {"at": 2, "intimacy_delta": -2, "trust_delta": 2},
        ]
        self.assertEqual(replay_relationship(40, 50, events), (41, 53))

    def test_directed_group_relationships_are_not_assumed_symmetric(self):
        config = {
            "bots": [
                {"bot_id": "bot_a", "display_name": "A"},
                {"bot_id": "bot_b", "display_name": "B"},
            ],
            "relations": [
                {"source_bot_id": "bot_a", "target_bot_id": "bot_b", "group_id": "", "relation_type": "global A to B", "affinity": 0.9, "trust": 0.8},
                {"source_bot_id": "bot_b", "target_bot_id": "bot_a", "group_id": "", "relation_type": "global B to A", "affinity": 0.4, "trust": 0.3},
                {"source_bot_id": "bot_a", "target_bot_id": "bot_b", "group_id": "g", "relation_type": "group A to B", "affinity": 0.6, "trust": 0.5},
            ],
        }
        a_to_b = directed_relationships(config, bot_id="bot_a", group_id="g")
        b_to_a = directed_relationships(config, bot_id="bot_b", group_id="g")
        self.assertEqual((a_to_b["bot_b"]["affinity"], a_to_b["bot_b"]["trust"]), (60.0, 50.0))
        self.assertEqual((b_to_a["bot_a"]["affinity"], b_to_a["bot_a"]["trust"]), (40.0, 30.0))

    def test_event_conditions_use_target_relationship(self):
        state = _state()
        state.intimacy = 90
        state.trust = 90
        templates = [
            {
                "id": "high_relation",
                "condition": {"min_intimacy": 80, "min_trust": 80},
                "options": [{"key": "A", "text": "A", "deltas": {"intimacy": 1}}],
            }
        ]
        self.assertIsNone(choose_event(templates, state, {"intimacy": 20, "trust": 20}))
        self.assertIsNotNone(choose_event(templates, state, {"intimacy": 90, "trust": 90}))


if __name__ == "__main__":
    unittest.main()
