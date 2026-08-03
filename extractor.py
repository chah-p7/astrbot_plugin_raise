from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any


logger = logging.getLogger(__name__)


EXTRACTION_SYSTEM_PROMPT = """你是养成系统的事件判定器，只负责从一次真实对话中判断关系变化。
你不是聊天角色，不输出面向用户的话，不扮演任何人。

规则：
1. 只依据对话中真实发生的互动，并从给定来源角色的具体人格与世界观判断；同一行为对不同角色可能产生不同影响。禁止为了刷数值而加分，没有明显变化时全部填 0。
2. 输出严格 JSON（不要 Markdown）：{
  "intimacy_delta": 整数（-3 到 3，好感变化），
  "trust_delta": 整数（-3 到 3，信任变化），
  "reason": "一句话说明原因，基于事实",
  "habit_candidate": "" 或 "从对话中观察到的、可能反复出现的习惯/偏好/能力，不超过 20 字"
}
3. 数值只能小幅变化：体贴、关心、记得细节、道歉、信任举动等可给正分；敷衍、欺骗、伤害、失信给负分。
4. 普通寒暄、角色扮演叙事、与养成无关的内容不给分。
5. 角色不知道自己的数值；reason 不得让角色复述数值或提及“养成系统”“好感度”等幕后概念。
6. habit_candidate 必须是能由旁观者重复观察到的行为或偏好，而不是一次性事件。"""

INITIAL_INFERENCE_SYSTEM_PROMPT = """你是养成系统的初始设定生成器，只负责从角色设定中推断初始数值。
你不是聊天角色，不输出面向用户的话。

规则：
1. 只基于给定的角色身份与关系设定推断，禁止编造没有依据的内容。
2. 关系设定中已明确给出 affinity/trust 数值时，必须直接采用（0-1 换算为 0-100）。
3. 没有显式数值时，根据关系类型、称呼、语气、身份描述合理推断，范围 0-100。
4. 输出严格 JSON（不要 Markdown）：{
  "intimacy": 0-100,
  "trust": 0-100,
  "exp": 0-500,
  "reason": "一句话说明依据"
}"""

EVENT_STORY_SYSTEM_PROMPT = """你是养成系统的奇遇剧情润色器，根据角色世界观把事件剧情改写得更贴合人设。
你不是聊天角色，不输出面向用户的话。

规则：
1. 只改写文字，绝不改动选项结构、选项数量、选项 key 或任何数值。
2. 剧情要符合给定的角色身份、关系设定与当前状态，不引入设定外的新角色或新事件。
3. 若提供了「最近对话内容」，剧情必须像紧接着这段对话自然发生的一样，
   可以呼应对话里出现的称呼、话题、地点或情绪，但不能与对话事实冲突。
4. 输出严格 JSON（不要 Markdown）：{
  "title": "标题",
  "story": "剧情正文",
  "options": [{"key": "A", "text": "选项文案", "outcome": "选择后结果文案"}]
}
5. options 的 key 必须与输入完全一致。
6. 不出现好感度、数值、养成系统等幕后概念。"""

CONTEXT_EVENT_SYSTEM_PROMPT = """你是养成系统的动态奇遇设计器。请从最近真实对话中自然延伸出一个此刻会发生的普通奇遇。
你不是聊天角色，不输出面向用户的话。

规则：
1. 事件必须具体呼应最近对话中的话题、地点、情绪、邀约、愿望或未解决的问题；
   禁止无依据地套用雨天、电影院、热茶等通用模板。上下文不足时输出空对象 {}。
2. 不新增设定外的重要人物，不改变角色身份，不替用户做决定。
3. 输出严格 JSON（不要 Markdown）：{
  "title": "15字内标题",
  "story": "紧接当前对话的剧情，2-4句",
  "options": [
    {
      "key": "A",
      "text": "玩家可做出的选择",
      "deltas": {"intimacy": -3到3, "trust": -3到3, "exp": 0到20},
      "habit": "可选；该选择体现的可重复习惯，20字内",
      "outcome": "选择后的客观结果"
    }
  ]
}
4. 生成 2-4 个选项，key 必须按 A/B/C/D 顺序且不重复。
5. 至少一个选择有正向变化；至少一个选择应为中性、拒绝或带有合理代价，不能所有选项都奖励。
6. 数值只表示本次选择的机械效果，不写进 story/text/outcome；不得生成 special、ultimate、ending 等字段。
7. 不出现好感度、信任值、经验、养成系统等幕后概念。"""

MEMBER_INITIAL_INFERENCE_SYSTEM_PROMPT = """你是养成系统的定向关系基线分析器。你要判断“来源角色对目标对象”的初始关系，而不是反向关系，也不是群体平均值。
你不是聊天角色，不输出面向用户的话。

规则：
1. 综合来源角色的完整人格、世界观、身份，以及目标对象的身份/人格和定向关系描述进行判断。
2. affinity/trust/familiarity 数字只是旧关系表提供的参考先验，不得机械照抄；当详细人格、世界观或关系叙述与数字冲突时，以详细设定为主，并在 reason 中简述依据。
3. 好感表示亲近、在意、依恋或欣赏；信任表示愿意暴露脆弱、托付重要事项及相信对方不会伤害/背叛。占有、依赖、畏惧、服从等不能自动等同于高信任。
4. 莉芙→蔚来与蔚来→莉芙必须分别判断，不假设关系对称。
5. 输出严格 JSON（不要 Markdown）：{
  "intimacy": 0-100 的整数,
  "trust": 0-100 的整数,
  "reason": "一句话说明人格/世界观/关系依据"
}
6. 禁止编造设定中没有的共同经历。"""

EVENT_TRIGGER_SYSTEM_PROMPT = """你是养成系统的奇遇触发判定器，只判断「当前这段对话是否适合插入一次奇遇事件」。
你不是聊天角色，不输出面向用户的话，不扮演任何人。

规则：
1. 只看最近对话内容中是否出现适合奇遇的节点：情绪高点（喜悦/失落/感动/紧张/害怕）、
   剧情推进（提到重要的人/地点/回忆/约定/愿望/秘密）、明确邀约或需要抉择的时刻、
   关系升温或出现裂痕。
2. 普通寒暄、闲聊琐事、问无关问题、命令指令、重复机械的对话都不适合，score 必须低。
3. score 表示「此刻插入奇遇的自然程度」，0.0-1.0；score 达到阈值才视为强节点。
4. 输出严格 JSON（不要 Markdown）：{
  "trigger": true 或 false,
  "score": 0.0-1.0,
  "reason": "一句话说明依据（基于对话事实）",
  "hint": "一句可用的剧情钩子（不适合触发时留空）"
}"""

EVENT_OUTCOME_SYSTEM_PROMPT = """你是养成系统的奇遇结局续写器，负责在玩家做出选择后，以角色身份续写一小段剧情/对话。
你不是系统、不是旁白，不输出面向用户的话，不解释机制。

规则：
1. 输出严格 JSON（不要 Markdown）：{"reply": "角色的一句话或一小段戏（1-3 句，口语化，贴合人设）"}
2. 承接「选择结果文案」继续往下演，不能复述结果文案本身；
   不能出现好感度、信任值、经验、养成系统等幕后概念。
3. 必须符合给定的角色身份与当前状态，呼应最近对话中的称呼与关系氛围。"""

EVENT_GENERATION_SYSTEM_PROMPT = """你是养成系统的奇遇/任务事件设计师，根据用户给出的故事概要生成事件模板。
你不是聊天角色，不输出面向用户的话。

规则：
1. 只输出一个 JSON 对象（不要 Markdown、注释、解释）：
{
  "title": "事件标题（15 字内）",
  "story": "剧情正文（贴合世界观，2-4 句）",
  "weight": 整数 1-20（出现权重），
  "condition": {
    "min_intimacy": 0-100,
    "min_trust": 0-100,
    "min_level": 1-100,
    "min_habits": 0-20,
    "min_special_events": 0-10
  }（没有门槛的键省略或写 0），
  "options": [
    {
      "key": "A",
      "text": "选项文案（20 字内）",
      "deltas": {"intimacy": -3~3, "trust": -3~3, "exp": 0~100},
      "outcome": "选择后的结果文案"
    }
  ]
}
2. options 2-4 个，key 依次为 A/B/C/D；好感/信任变化必须是小幅（-3~3），
   经验加成 0-100；至少一个选项有正向结果，至少一个选项有代价或风险。
3. 剧情与选项必须贴合给定的世界观、角色与关系，不引入设定外的新角色。
4. 不出现“好感度”“养成系统”等幕后概念。
5. 终局事件：每个选项必须额外带 "ending": "结局标识（如 apple_tree）",
   "ending_label": "结局名（如 成为苹果树）"。"""


def _clean_json(text: str) -> str:
    raw = str(text or "").strip()
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw, re.S | re.I)
    if fenced:
        return fenced.group(1)
    start = raw.find("{")
    end = raw.rfind("}")
    if start != -1 and end != -1 and end > start:
        return raw[start : end + 1]
    return raw


def parse_delta_payload(text: str) -> dict[str, Any]:
    try:
        payload = json.loads(_clean_json(text))
    except (ValueError, TypeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def build_extraction_prompt(
    *,
    identity: dict[str, Any],
    user_message: str,
    assistant_message: str,
    intimacy: int,
    trust: int,
    level: int,
    source_profile: dict[str, Any] | None = None,
    target_profile: dict[str, Any] | None = None,
    relationship: dict[str, Any] | None = None,
) -> str:
    identity_text = json.dumps(
        {
            key: identity.get(key)
            for key in (
                "bot_id",
                "memory_key",
                "self_identity",
                "body_identity",
                "soul_identity",
                "account_label",
            )
            if identity.get(key)
        },
        ensure_ascii=False,
    )
    return (
        "锁定身份（不得修改）：\n"
        f"{identity_text or '无'}\n\n"
        "来源角色的完整人格与世界观（由谁产生好感/信任）：\n"
        f"{json.dumps(source_profile or {}, ensure_ascii=False)}\n\n"
        "当前对话目标：\n"
        f"{json.dumps(target_profile or {}, ensure_ascii=False)}\n\n"
        "来源角色 → 当前目标的定向关系：\n"
        f"{json.dumps(relationship or {}, ensure_ascii=False)}\n\n"
        f"当前养成参考值：好感={intimacy}，信任={trust}，等级={level}（仅供判定参考）。\n\n"
        f"本轮用户消息：\n{user_message or '[无；这是 Bot 主动发言]'}\n\n"
        f"当前 Bot 最终公开回复：\n{assistant_message}\n\n"
        "按规则输出 JSON。"
    )


async def _provider_id(context: Any, config: Any, umo: str) -> str:
    provider_id = str(config.get("extraction_provider_id", "") or "").strip()
    if not provider_id:
        try:
            provider_id = await context.get_current_chat_provider_id(umo)
        except Exception:
            provider_id = ""
    return provider_id


def _identity_text(identity: dict[str, Any]) -> str:
    return json.dumps(
        {
            key: identity.get(key)
            for key in (
                "bot_id",
                "memory_key",
                "self_identity",
                "body_identity",
                "soul_identity",
                "account_label",
                "identity_note",
            )
            if identity.get(key)
        },
        ensure_ascii=False,
    )


async def extract_growth_delta(
    context: Any,
    *,
    config: Any,
    umo: str,
    identity: dict[str, Any],
    user_message: str,
    assistant_message: str,
    intimacy: int,
    trust: int,
    level: int,
    source_profile: dict[str, Any] | None = None,
    target_profile: dict[str, Any] | None = None,
    relationship: dict[str, Any] | None = None,
) -> dict[str, Any]:
    provider_id = await _provider_id(context, config, umo)
    prompt = build_extraction_prompt(
        identity=identity,
        user_message=user_message,
        assistant_message=assistant_message,
        intimacy=intimacy,
        trust=trust,
        level=level,
        source_profile=source_profile,
        target_profile=target_profile,
        relationship=relationship,
    )
    try:
        max_tokens = int(config.get("extraction_max_tokens", 300) or 300)
        timeout = int(config.get("extraction_timeout_seconds", 45) or 45)
        response = await asyncio.wait_for(
            context.llm_generate(
                chat_provider_id=provider_id,
                prompt=prompt,
                system_prompt=EXTRACTION_SYSTEM_PROMPT,
                max_tokens=max(100, min(max_tokens, 1000)),
                temperature=0.2,
            ),
            timeout=max(10, min(timeout, 120)),
        )
        text = str(getattr(response, "completion_text", "") or "")
    except asyncio.CancelledError:
        raise
    except Exception:
        return {}
    payload = parse_delta_payload(text)
    try:
        intimacy_delta = int(payload.get("intimacy_delta", 0) or 0)
    except (TypeError, ValueError):
        intimacy_delta = 0
    try:
        trust_delta = int(payload.get("trust_delta", 0) or 0)
    except (TypeError, ValueError):
        trust_delta = 0
    return {
        "intimacy_delta": intimacy_delta,
        "trust_delta": trust_delta,
        "reason": str(payload.get("reason", "") or "").strip(),
        "habit_candidate": str(payload.get("habit_candidate", "") or "").strip(),
    }


async def infer_initial_values(
    context: Any,
    *,
    config: Any,
    umo: str,
    identity: dict[str, Any],
    relation_summary: str,
) -> dict[str, Any]:
    provider_id = await _provider_id(context, config, umo)
    identity_text = _identity_text(identity)
    prompt = (
        "角色身份：\n"
        f"{identity_text or '无'}\n\n"
        "群关系设定：\n"
        f"{relation_summary or '无'}\n\n"
        "按规则输出 JSON。"
    )
    try:
        max_tokens = int(config.get("extraction_max_tokens", 300) or 300)
        timeout = int(config.get("extraction_timeout_seconds", 45) or 45)
        response = await asyncio.wait_for(
            context.llm_generate(
                chat_provider_id=provider_id,
                prompt=prompt,
                system_prompt=INITIAL_INFERENCE_SYSTEM_PROMPT,
                max_tokens=max(100, min(max_tokens, 1000)),
                temperature=0.2,
            ),
            timeout=max(10, min(timeout, 120)),
        )
        text = str(getattr(response, "completion_text", "") or "")
    except asyncio.CancelledError:
        raise
    except Exception:
        return {}
    payload = parse_delta_payload(text)
    if not payload or not all(key in payload for key in ("intimacy", "trust")):
        return {}
    result: dict[str, Any] = {}
    for key in ("intimacy", "trust", "exp"):
        try:
            result[key] = int(payload.get(key, 0) or 0)
        except (TypeError, ValueError):
            result[key] = 0
    result["reason"] = str(payload.get("reason", "") or "").strip()
    return result


async def infer_member_initial_values(
    context: Any,
    *,
    config: Any,
    umo: str,
    source_profile: dict[str, Any],
    target_profile: dict[str, Any],
    relationship: dict[str, Any],
) -> dict[str, Any]:
    """Infer one directed relationship from resolved persona and worldview."""
    provider_id = await _provider_id(context, config, umo)
    prompt = (
        "来源角色（产生这份好感/信任的一方）：\n"
        f"{json.dumps(source_profile, ensure_ascii=False)}\n\n"
        "目标对象：\n"
        f"{json.dumps(target_profile, ensure_ascii=False)}\n\n"
        "来源角色 → 目标对象的关系设定：\n"
        f"{json.dumps(relationship, ensure_ascii=False)}\n\n"
        "请只输出这一个方向的初始关系 JSON。"
    )
    try:
        max_tokens = int(config.get("extraction_max_tokens", 300) or 300)
        timeout = int(config.get("extraction_timeout_seconds", 45) or 45)
        response = await asyncio.wait_for(
            context.llm_generate(
                chat_provider_id=provider_id,
                prompt=prompt,
                system_prompt=MEMBER_INITIAL_INFERENCE_SYSTEM_PROMPT,
                max_tokens=max(160, min(max_tokens, 800)),
                temperature=0.2,
            ),
            timeout=max(10, min(timeout, 120)),
        )
        text = str(getattr(response, "completion_text", "") or "")
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.warning(
            "[Raise] 定向关系模型推断失败 provider=%s: %s",
            provider_id or "<current>",
            exc,
        )
        return {}
    payload = parse_delta_payload(text)
    if not payload or not all(key in payload for key in ("intimacy", "trust")):
        return {}
    result: dict[str, Any] = {}
    for key in ("intimacy", "trust"):
        try:
            result[key] = max(0, min(100, int(payload.get(key, 0) or 0)))
        except (TypeError, ValueError):
            return {}
    result["reason"] = str(payload.get("reason", "") or "").strip()[:300]
    return result


async def infer_event_story(
    context: Any,
    *,
    config: Any,
    umo: str,
    identity: dict[str, Any],
    state_summary: str,
    template: dict[str, Any],
    conversation_context: str = "",
    trigger_hint: str = "",
) -> dict[str, Any] | None:
    provider_id = await _provider_id(context, config, umo)
    identity_text = _identity_text(identity)
    options = template.get("options", [])
    options_json = (
        json.dumps(options, ensure_ascii=False)
        if isinstance(options, list)
        else "[]"
    )
    prompt = (
        "角色身份：\n"
        f"{identity_text or '无'}\n\n"
        f"当前状态摘要：{state_summary or '无'}\n\n"
        + (
            "最近对话内容（剧情要像紧接着这段对话自然发生，不与其冲突）：\n"
            f"{conversation_context}\n\n"
            if conversation_context
            else ""
        )
        + (f"触发时的剧情钩子：{trigger_hint}\n\n" if trigger_hint else "")
        + "原始事件模板：\n"
        + f"标题：{template.get('title') or ''}\n"
        + f"剧情：{template.get('story') or ''}\n"
        + "选项：\n"
        + f"{options_json}\n\n"
        + "按规则输出润色后的 JSON，结构与原始模板一致。"
    )
    try:
        max_tokens = int(config.get("extraction_max_tokens", 300) or 300)
        timeout = int(config.get("extraction_timeout_seconds", 45) or 45)
        response = await asyncio.wait_for(
            context.llm_generate(
                chat_provider_id=provider_id,
                prompt=prompt,
                system_prompt=EVENT_STORY_SYSTEM_PROMPT,
                max_tokens=max(200, min(max_tokens, 1200)),
                temperature=0.9,
            ),
            timeout=max(10, min(timeout, 120)),
        )
        text = str(getattr(response, "completion_text", "") or "")
    except asyncio.CancelledError:
        raise
    except Exception:
        return None
    payload = parse_delta_payload(text)
    if not payload:
        return None
    result: dict[str, Any] = {
        "title": str(payload.get("title") or "").strip(),
        "story": str(payload.get("story") or "").strip(),
    }
    raw_options = payload.get("options")
    if isinstance(raw_options, list):
        template_keys = {
            str(item.get("key") or "").strip().upper()
            for item in options
            if isinstance(item, dict)
        }
        rewritten: list[dict[str, Any]] = []
        seen: set[str] = set()
        for item in raw_options:
            if not isinstance(item, dict):
                continue
            key = str(item.get("key") or "").strip().upper()
            if key not in template_keys or key in seen:
                continue
            seen.add(key)
            rewritten.append(
                {
                    "key": key,
                    "text": str(item.get("text") or "").strip(),
                    "outcome": str(item.get("outcome") or "").strip(),
                }
            )
        if len(rewritten) == len(template_keys) and rewritten:
            result["options"] = rewritten
    return result if result.get("title") or result.get("story") else None


async def generate_context_event(
    context: Any,
    *,
    config: Any,
    umo: str,
    identity: dict[str, Any],
    state_summary: str,
    conversation_context: str,
    trigger_hint: str = "",
) -> dict[str, Any] | None:
    """Generate one ordinary event directly from the current conversation."""
    conversation_context = str(conversation_context or "").strip()
    if not conversation_context:
        return None
    provider_id = await _provider_id(context, config, umo)
    prompt = (
        "角色身份与世界观：\n"
        f"{_identity_text(identity) or '无'}\n\n"
        f"当前关系阶段：{state_summary or '无'}\n\n"
        "最近真实对话：\n"
        f"{conversation_context}\n\n"
        + (f"触发判定给出的剧情钩子：{trigger_hint}\n\n" if trigger_hint else "")
        + "请生成一个只属于这段对话的普通奇遇，并按规则输出 JSON。"
    )
    try:
        max_tokens = int(config.get("extraction_max_tokens", 300) or 300)
        timeout = int(config.get("extraction_timeout_seconds", 45) or 45)
        response = await asyncio.wait_for(
            context.llm_generate(
                chat_provider_id=provider_id,
                prompt=prompt,
                system_prompt=CONTEXT_EVENT_SYSTEM_PROMPT,
                max_tokens=max(500, min(max_tokens, 1600)),
                temperature=0.8,
            ),
            timeout=max(15, min(timeout, 180)),
        )
        text = str(getattr(response, "completion_text", "") or "")
    except asyncio.CancelledError:
        raise
    except Exception:
        return None
    payload = parse_delta_payload(text)
    return payload if isinstance(payload, dict) and payload else None


async def judge_event_trigger(
    context: Any,
    *,
    config: Any,
    umo: str,
    identity: dict[str, Any],
    state_summary: str,
    conversation_context: str,
) -> dict[str, Any]:
    provider_id = await _provider_id(context, config, umo)
    identity_text = _identity_text(identity)
    prompt = (
        "角色身份：\n"
        f"{identity_text or '无'}\n\n"
        f"当前养成参考值：{state_summary or '无'}（仅供判断关系阶段）\n\n"
        "最近对话内容：\n"
        f"{conversation_context or '无'}\n\n"
        "按规则输出 JSON。"
    )
    try:
        max_tokens = int(config.get("extraction_max_tokens", 300) or 300)
        timeout = int(config.get("extraction_timeout_seconds", 45) or 45)
        response = await asyncio.wait_for(
            context.llm_generate(
                chat_provider_id=provider_id,
                prompt=prompt,
                system_prompt=EVENT_TRIGGER_SYSTEM_PROMPT,
                max_tokens=max(120, min(max_tokens, 600)),
                temperature=0.2,
            ),
            timeout=max(10, min(timeout, 90)),
        )
        text = str(getattr(response, "completion_text", "") or "")
    except asyncio.CancelledError:
        raise
    except Exception:
        return {"trigger": False, "score": 0.0, "reason": "", "hint": ""}
    payload = parse_delta_payload(text)
    try:
        score = float(payload.get("score", 0.0) or 0.0)
    except (TypeError, ValueError):
        score = 0.0
    score = max(0.0, min(1.0, score))
    trigger = bool(payload.get("trigger", False)) or score >= 0.75
    return {
        "trigger": trigger,
        "score": score,
        "reason": str(payload.get("reason", "") or "").strip(),
        "hint": str(payload.get("hint", "") or "").strip(),
    }


async def infer_event_outcome(
    context: Any,
    *,
    config: Any,
    umo: str,
    identity: dict[str, Any],
    state_summary: str,
    template: dict[str, Any],
    option: dict[str, Any],
    conversation_context: str = "",
) -> str:
    provider_id = await _provider_id(context, config, umo)
    identity_text = _identity_text(identity)
    options = template.get("options", [])
    options_json = (
        json.dumps(options, ensure_ascii=False)
        if isinstance(options, list)
        else "[]"
    )
    prompt = (
        "角色身份：\n"
        f"{identity_text or '无'}\n\n"
        f"当前状态摘要：{state_summary or '无'}\n\n"
        + (
            "最近对话内容：\n"
            f"{conversation_context}\n\n"
            if conversation_context
            else ""
        )
        + "事件：\n"
        + f"标题：{template.get('title') or ''}\n"
        + f"剧情：{template.get('story') or ''}\n"
        + f"玩家选择了：{option.get('key')} {option.get('text') or ''}\n"
        + f"选择结果文案：{option.get('outcome') or ''}\n\n"
        + "按规则输出续写后的 JSON。"
    )
    try:
        max_tokens = int(config.get("extraction_max_tokens", 300) or 300)
        timeout = int(config.get("extraction_timeout_seconds", 45) or 45)
        response = await asyncio.wait_for(
            context.llm_generate(
                chat_provider_id=provider_id,
                prompt=prompt,
                system_prompt=EVENT_OUTCOME_SYSTEM_PROMPT,
                max_tokens=max(150, min(max_tokens, 800)),
                temperature=0.9,
            ),
            timeout=max(10, min(timeout, 90)),
        )
        text = str(getattr(response, "completion_text", "") or "")
    except asyncio.CancelledError:
        raise
    except Exception:
        return ""
    payload = parse_delta_payload(text)
    reply = str(payload.get("reply", "") or "").strip()
    if len(reply) < 2:
        return ""
    return reply[:600]


async def generate_event_template(
    context: Any,
    *,
    config: Any,
    umo: str,
    worldview_summary: str,
    story_summary: str,
    kind: str,
    extra: str = "",
) -> dict[str, Any] | None:
    provider_id = await _provider_id(context, config, umo)
    kind_line = "事件类型：普通奇遇。"
    if kind == "special":
        kind_line = "事件类型：特殊事件（计入终极目标解锁进度）。"
    elif kind == "ultimate":
        kind_line = "事件类型：终局事件（每个选项必须带 ending 与 ending_label）。"
    prompt = (
        "世界观与角色设定：\n"
        f"{worldview_summary or '无'}\n\n"
        "用户提供的故事概要：\n"
        f"{story_summary or '无'}\n\n"
        + (f"额外要求：\n{extra}\n\n" if extra else "")
        + kind_line
        + "\n\n按规则输出 JSON。"
    )
    try:
        max_tokens = int(config.get("extraction_max_tokens", 300) or 300)
        timeout = int(config.get("extraction_timeout_seconds", 45) or 45)
        response = await asyncio.wait_for(
            context.llm_generate(
                chat_provider_id=provider_id,
                prompt=prompt,
                system_prompt=EVENT_GENERATION_SYSTEM_PROMPT,
                max_tokens=max(400, min(max_tokens, 2000)),
                temperature=0.8,
            ),
            timeout=max(15, min(timeout, 180)),
        )
        text = str(getattr(response, "completion_text", "") or "")
    except asyncio.CancelledError:
        raise
    except Exception:
        return None
    payload = parse_delta_payload(text)
    if not isinstance(payload, dict):
        return None
    return payload
