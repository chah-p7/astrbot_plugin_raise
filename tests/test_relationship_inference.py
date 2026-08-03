from __future__ import annotations

import unittest
from types import SimpleNamespace

from astrbot_plugin_raise.extractor import (
    build_extraction_prompt,
    infer_member_initial_values,
)


class _Context:
    def __init__(self, response: str):
        self.response = response
        self.calls: list[dict] = []

    async def llm_generate(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(completion_text=self.response)


class RelationshipInferenceTests(unittest.IsolatedAsyncioTestCase):
    def test_ongoing_delta_prompt_keeps_persona_worldview_and_direction(self):
        prompt = build_extraction_prompt(
            identity={"bot_id": "source"},
            user_message="我会守约",
            assistant_message="我记住了。",
            intimacy=50,
            trust=40,
            level=3,
            source_profile={
                "name": "来源角色",
                "personality": "谨慎但重视承诺",
                "worldview": "世界观正文",
            },
            target_profile={"name": "目标角色", "personality": "目标人格"},
            relationship={"relation_type": "仍在观察对方是否可靠"},
        )

        self.assertIn("谨慎但重视承诺", prompt)
        self.assertIn("世界观正文", prompt)
        self.assertIn("目标人格", prompt)
        self.assertIn("来源角色 → 当前目标", prompt)
        self.assertIn("仍在观察对方是否可靠", prompt)

    async def test_full_persona_worldview_and_direction_are_sent(self):
        context = _Context(
            '{"intimacy":72,"trust":41,"reason":"在意很深，但仍保留戒心"}'
        )
        result = await infer_member_initial_values(
            context,
            config={
                "extraction_provider_id": "provider",
                "extraction_max_tokens": 300,
                "extraction_timeout_seconds": 10,
            },
            umo="platform:GroupMessage:group",
            source_profile={
                "name": "来源角色",
                "personality": "完整人格内容",
                "worldview": "完整世界观内容",
                "identity": {"self_identity": "source"},
            },
            target_profile={
                "name": "目标角色",
                "personality": "目标人格内容",
                "worldview": "目标世界观内容",
            },
            relationship={
                "relation_type": "想接近但害怕被拒绝",
                "affinity": 90,
                "trust": 80,
            },
        )

        self.assertEqual(result["intimacy"], 72)
        self.assertEqual(result["trust"], 41)
        self.assertEqual(len(context.calls), 1)
        prompt = context.calls[0]["prompt"]
        system = context.calls[0]["system_prompt"]
        self.assertIn("完整人格内容", prompt)
        self.assertIn("完整世界观内容", prompt)
        self.assertIn("目标世界观内容", prompt)
        self.assertIn("来源角色 → 目标对象", prompt)
        self.assertIn("不得机械照抄", system)
        self.assertIn("必须分别判断", system)

    async def test_invalid_model_response_fails_closed(self):
        context = _Context("not json")
        result = await infer_member_initial_values(
            context,
            config={"extraction_provider_id": "provider"},
            umo="umo",
            source_profile={},
            target_profile={},
            relationship={},
        )
        self.assertEqual(result, {})


if __name__ == "__main__":
    unittest.main()
