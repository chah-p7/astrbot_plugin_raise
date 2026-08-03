from __future__ import annotations

import json
import random
import re
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


ATTRIBUTE_KEYS = ("intimacy", "trust")
ATTRIBUTE_LABELS = {"intimacy": "好感", "trust": "信任"}
MAX_RETAINED_EVENTS = 500
RELATIONSHIP_SCHEMA_VERSION = 2
INTIMACY_TIERS = ((0, "初识"), (21, "熟悉"), (51, "亲密"), (76, "羁绊"))
TRUST_TIERS = ((0, "戒备"), (21, "认可"), (51, "信赖"), (76, "托付"))
HABIT_MILESTONES = (1, 3, 5, 10, 20)
ULTIMATE_DEFAULT_THRESHOLDS = {
    "intimacy": 100,
    "trust": 100,
    "habits": 3,
    "level": 10,
    "special_events": 3,
}

DEFAULT_EVENTS: list[dict[str, Any]] = [
    {
        "id": "rain_window",
        "title": "雨天的窗边",
        "weight": 10,
        "condition": {},
        "story": "傍晚突然下起雨，她站在窗边看了一会儿，回头看向你，像是有什么话想说。",
        "options": [
            {
                "key": "A",
                "text": "把伞递过去，说送她回去",
                "deltas": {"intimacy": 2, "trust": 1},
                "outcome": "她接过伞，眼睛弯了一下，轻声说了句谢谢。",
            },
            {
                "key": "B",
                "text": "陪她一起淋雨走",
                "deltas": {"intimacy": 3, "trust": -1},
                "outcome": "雨里她笑出声，说你傻，却一直没松开手。",
            },
            {
                "key": "C",
                "text": "只是叮嘱她早点回去",
                "deltas": {"intimacy": -1},
                "outcome": "她点点头，脸上没什么表情，转身走了。",
            },
        ],
    },
    {
        "id": "riverside_market",
        "title": "河边的集市",
        "weight": 10,
        "condition": {},
        "story": "傍晚的河边集市人来人往，她停在卖糖画的小摊前，回头朝你招了招手。",
        "options": [
            {
                "key": "A",
                "text": "走过去陪她一起挑",
                "deltas": {"intimacy": 1, "trust": 2},
                "outcome": "她挑了半天，最后把糖画递给你，说这个像你。",
            },
            {
                "key": "B",
                "text": "买两份，分她一份",
                "deltas": {"intimacy": 2, "trust": 1},
                "outcome": "她愣了一下，接过去咬了一口，耳根有点红。",
            },
            {
                "key": "C",
                "text": "站着等她，不过去",
                "deltas": {"intimacy": 0},
                "outcome": "她买完自己那份，回头看了你一眼，没说什么。",
            },
        ],
    },
    {
        "id": "midnight_tea",
        "title": "深夜的热茶",
        "weight": 8,
        "condition": {"min_intimacy": 30},
        "story": "夜深了，她还没睡，捧着杯热茶坐在桌边，看起来有心事。",
        "options": [
            {
                "key": "A",
                "text": "坐下来安静陪她喝完这杯茶",
                "deltas": {"intimacy": 1, "trust": 2},
                "outcome": "她没说话，但肩膀慢慢松了下来。",
            },
            {
                "key": "B",
                "text": "讲个笑话想逗她开心",
                "deltas": {"intimacy": 2, "trust": -1},
                "outcome": "她笑了一下，又安静下去，说想自己待会儿。",
            },
            {
                "key": "C",
                "text": "问她要不要早点休息",
                "deltas": {"intimacy": 0, "trust": 1},
                "outcome": "她点点头，说喝完这杯就去睡。",
            },
        ],
    },
    {
        "id": "cinema_seat",
        "title": "电影院的座位",
        "weight": 6,
        "condition": {"min_intimacy": 50},
        "story": "电影看到一半，她轻轻往你这边靠了靠，呼吸声在安静的放映厅里变得明显。",
        "options": [
            {
                "key": "A",
                "text": "把外套递给她披上",
                "deltas": {"intimacy": 1, "trust": 1},
                "outcome": "她把外套裹紧了一点，嘴角微微翘起。",
            },
            {
                "key": "B",
                "text": "任她靠过来，什么也不说",
                "deltas": {"intimacy": 3},
                "outcome": "电影散场时，她很久都没先起身。",
            },
            {
                "key": "C",
                "text": "坐直一点，拉开些距离",
                "deltas": {"intimacy": -2},
                "outcome": "她轻轻坐了回去，后半场再没靠过来。",
            },
        ],
    },
    {
        "id": "bond_contract",
        "title": "羁绊的约定",
        "weight": 3,
        "special": True,
        "condition": {"min_intimacy": 80},
        "story": "夜深人静，她突然很认真地看着你，说有一件想了很久的事，想跟你约定下来。",
        "options": [
            {
                "key": "A",
                "text": "认真听她说完，郑重答应",
                "deltas": {"intimacy": 3, "trust": 3, "exp": 50},
                "outcome": "她轻轻笑了，说这个约定她会记很久很久。",
            },
            {
                "key": "B",
                "text": "笑着打岔，说太晚了改天再说",
                "deltas": {"intimacy": -2, "trust": -2},
                "outcome": "她沉默了一会儿，说那就改天吧，声音比平时轻。",
            },
        ],
    },
    {
        "id": "rain_night_confession",
        "title": "雨夜的坦白",
        "weight": 2,
        "special": True,
        "condition": {"min_intimacy": 85, "min_trust": 70},
        "story": "雨下了一整夜，她坐在你旁边，说想把一直没敢说出口的话说完。",
        "options": [
            {
                "key": "A",
                "text": "安静听她把话说完",
                "deltas": {"intimacy": 3, "trust": 3, "exp": 80},
                "outcome": "说完之后她像是卸下了什么，靠在你肩头睡着了。",
            },
            {
                "key": "B",
                "text": "打断她，说现在不想听这些",
                "deltas": {"intimacy": -3, "trust": -2},
                "outcome": "她点点头，把剩下的话咽了回去，雨声格外清楚。",
            },
        ],
    },
    {
        "id": "apple_tree",
        "title": "苹果树的邀约",
        "ultimate": True,
        "condition": {
            "min_intimacy": 90,
            "min_trust": 85,
            "min_level": 8,
            "min_habits": 3,
            "min_special_events": 2,
        },
        "story": "最近她总梦见一片山坡，山坡上只有一棵苹果树。她说，等哪天准备好了，"
        "想让你陪她一起走到那棵树下去。",
        "options": [
            {
                "key": "A",
                "text": "陪她走到树下，让她成为那棵苹果树",
                "ending": "apple_tree",
                "ending_label": "成为苹果树",
                "deltas": {"intimacy": 3, "trust": 3, "exp": 100},
                "outcome": "她站在树下回头看了你很久，然后慢慢变成了一棵树。"
                "风来的时候，满树的苹果轻轻摇。",
            },
            {
                "key": "B",
                "text": "拉住她，请她留下来",
                "ending": "stay",
                "ending_label": "留下来",
                "deltas": {"intimacy": 3, "trust": 2, "exp": 80},
                "outcome": "她愣了愣，然后把手放回你手里，说那就再陪你走一段。",
            },
            {
                "key": "C",
                "text": "提议一起离开这片山坡",
                "ending": "leave_together",
                "ending_label": "一起离开",
                "deltas": {"intimacy": 2, "trust": 3, "exp": 80},
                "outcome": "她想了想，笑着点头。你们没有去苹果树，"
                "而是沿着山坡的另一边走了下去。",
            },
        ],
    },
]


def _cumulative_exp(level: int) -> int:
    """到达指定等级所需的累计经验：Lv.2=100，Lv.3=250，Lv.4=450……"""
    return 25 * max(1, int(level)) * (max(1, int(level)) + 1) - 50


cumulative_exp = _cumulative_exp


def level_for_exp(exp: int) -> int:
    exp = max(0, int(exp))
    level = 1
    while _cumulative_exp(level + 1) <= exp:
        level += 1
    return level


def next_level_exp(level: int) -> int:
    return _cumulative_exp(max(1, int(level)) + 1)


def clamp(value: int, minimum: int, maximum: int) -> int:
    return max(int(minimum), min(int(maximum), int(value)))


def level_progress(exp: int) -> tuple[int, int, int, float]:
    """返回 (当前等级, 本级起始经验, 下一级所需经验, 本级进度 0~1)。"""
    exp = max(0, int(exp))
    level = level_for_exp(exp)
    base = _cumulative_exp(level)
    nxt = _cumulative_exp(level + 1)
    span = max(1, nxt - base)
    progress = min(1.0, max(0.0, (exp - base) / span))
    return level, base, nxt, progress


def tier_progress(
    value: int,
    tiers: tuple[tuple[int, str], ...],
    *,
    max_value: int = 100,
) -> dict[str, Any]:
    """返回当前档位、下一档位与进度（0~1）。"""
    value = max(0, min(max(1, int(max_value)), int(value or 0)))
    current_start = 0
    current_label = tiers[0][1] if tiers else ""
    next_start: int | None = None
    next_label = ""
    for start, label in tiers:
        if value >= start:
            current_start = start
            current_label = label
        elif next_start is None:
            next_start = start
            next_label = label
    if next_start is None:
        if value < max_value:
            # 已经到达最高档位，但离属性上限还有距离：下一目标是满值。
            next_start = max_value
            next_label = "满值"
        else:
            return {
                "current": current_label,
                "next": "",
                "next_start": None,
                "progress": 1.0,
            }
    span = max(1, next_start - current_start)
    progress = min(1.0, max(0.0, (value - current_start) / span))
    return {
        "current": current_label,
        "next": next_label,
        "next_start": next_start,
        "progress": progress,
    }


def progress_bar(progress: float, width: int = 10) -> str:
    filled = int(round(max(0.0, min(1.0, float(progress))) * max(1, int(width))))
    return "▓" * filled + "░" * (max(1, int(width)) - filled)


def normalize_habit(value: str) -> str:
    text = re.sub(r"\s+", " ", str(value or "").strip())
    text = re.sub(r"^[「『\"'“”]+|[」』\"'“”]+$", "", text)
    if len(text) < 2 or len(text) > 40:
        return ""
    return text


@dataclass
class GrowthState:
    scope_id: str
    bot_id: str
    logical_group_id: str
    memory_key: str = ""
    display_name: str = ""
    intimacy: int = 0
    trust: int = 0
    exp: int = 0
    habits: list[str] = field(default_factory=list)
    habit_candidates: dict[str, int] = field(default_factory=dict)
    events: list[dict[str, Any]] = field(default_factory=list)
    last_event_at: float = 0.0
    event_count: int = 0
    init_status: str = "pending"
    init_note: str = ""
    members: dict[str, dict[str, Any]] = field(default_factory=dict)
    relationship_schema_version: int = 1
    special_events_completed: list[str] = field(default_factory=list)
    ultimate_achieved_at: float = 0.0
    ending: dict[str, Any] = field(default_factory=dict)
    updated_at: float = 0.0

    def level(self) -> int:
        return level_for_exp(self.exp)

    def next_exp(self) -> int:
        return next_level_exp(self.level())

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": 2,
            "scope_id": self.scope_id,
            "bot_id": self.bot_id,
            "logical_group_id": self.logical_group_id,
            "memory_key": self.memory_key,
            "display_name": self.display_name,
            "intimacy": self.intimacy,
            "trust": self.trust,
            "exp": self.exp,
            "habits": list(self.habits),
            "habit_candidates": dict(self.habit_candidates),
            "events": list(self.events),
            "last_event_at": self.last_event_at,
            "event_count": self.event_count,
            "init_status": self.init_status,
            "init_note": self.init_note,
            "members": dict(self.members),
            "relationship_schema_version": self.relationship_schema_version,
            "special_events_completed": list(self.special_events_completed),
            "ultimate_achieved_at": self.ultimate_achieved_at,
            "ending": dict(self.ending),
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(
        cls,
        payload: dict[str, Any],
        *,
        scope_id: str,
        bot_id: str,
        logical_group_id: str,
    ) -> "GrowthState":
        payload = payload if isinstance(payload, dict) else {}
        return cls(
            scope_id=str(scope_id or ""),
            bot_id=str(bot_id or ""),
            logical_group_id=str(logical_group_id or ""),
            memory_key=str(payload.get("memory_key") or ""),
            display_name=str(payload.get("display_name") or ""),
            intimacy=clamp(int(payload.get("intimacy", 0) or 0), 0, 100),
            trust=clamp(int(payload.get("trust", 0) or 0), 0, 100),
            exp=max(0, int(payload.get("exp", 0) or 0)),
            habits=[
                normalize_habit(item)
                for item in payload.get("habits", [])
                if normalize_habit(item)
            ],
            habit_candidates={
                str(key): max(1, int(value or 1))
                for key, value in dict(payload.get("habit_candidates", {}) or {}).items()
                if str(key).strip()
            },
            events=list(payload.get("events", []) or [])[-MAX_RETAINED_EVENTS:],
            last_event_at=float(payload.get("last_event_at", 0) or 0),
            event_count=max(0, int(payload.get("event_count", 0) or 0)),
            init_status=str(payload.get("init_status") or "done"),
            init_note=str(payload.get("init_note") or ""),
            members=dict(payload.get("members") or {}),
            relationship_schema_version=max(
                1, int(payload.get("relationship_schema_version", 1) or 1)
            ),
            special_events_completed=[
                str(item or "").strip()
                for item in payload.get("special_events_completed", []) or []
                if str(item or "").strip()
            ],
            ultimate_achieved_at=float(payload.get("ultimate_achieved_at", 0) or 0),
            ending=dict(payload.get("ending") or {}),
            updated_at=float(payload.get("updated_at", 0) or 0),
        )


def load_state(
    path: Path,
    *,
    scope_id: str,
    bot_id: str,
    logical_group_id: str,
) -> GrowthState:
    payload: dict[str, Any] = {}
    if path.is_file():
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            payload = {}
    return GrowthState.from_dict(
        payload,
        scope_id=scope_id,
        bot_id=bot_id,
        logical_group_id=logical_group_id,
    )


def save_state(path: Path, state: GrowthState) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    tmp.write_text(
        json.dumps(state.to_dict(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    for attempt in range(5):
        try:
            tmp.replace(path)
            break
        except PermissionError:
            if attempt >= 4:
                raise
            # Windows can briefly lock the destination while another atomic
            # replace completes.  Linux normally succeeds on the first try.
            time.sleep(0.01 * (attempt + 1))


def daily_positive_deltas(
    state: GrowthState,
    now: float,
    relationship: dict[str, Any] | None = None,
) -> dict[str, int]:
    """统计今天已消耗的正向变化额度。"""
    if isinstance(relationship, dict):
        return _positive_deltas_from_events(
            list(relationship.get("events", []) or []), now
        )
    return _positive_deltas_from_events(state.events, now)


def _positive_deltas_from_events(
    events: list[dict[str, Any]],
    now: float,
) -> dict[str, int]:
    today = time.strftime("%Y-%m-%d", time.localtime(float(now)))
    result = {"intimacy": 0, "trust": 0}
    for item in events:
        at = float(item.get("at", 0) or 0)
        if time.strftime("%Y-%m-%d", time.localtime(at)) != today:
            continue
        result["intimacy"] += max(0, int(item.get("intimacy_delta", 0) or 0))
        result["trust"] += max(0, int(item.get("trust_delta", 0) or 0))
    return result


def get_member(
    state: GrowthState,
    *,
    user_id: str,
    user_name: str = "",
    kind: str = "user",
    target_bot_id: str = "",
    initial_intimacy: int = 0,
    initial_trust: int = 0,
) -> dict[str, Any]:
    key = str(user_id or "").strip() or str(user_name or "").strip() or "unknown"
    member = state.members.get(key)
    if member is None:
        member = {
            "member_id": key,
            "name": str(user_name or "").strip() or key,
            "kind": "bot" if kind == "bot" else "user",
            "target_bot_id": str(target_bot_id or "").strip(),
            "intimacy": clamp(int(initial_intimacy or 0), 0, 100),
            "trust": clamp(int(initial_trust or 0), 0, 100),
            "events": [],
            "created_at": time.time(),
        }
        state.members[key] = member
    if user_name:
        member["name"] = str(user_name or "").strip() or member.get("name", key)
    if kind == "bot" and target_bot_id:
        member["kind"] = "bot"
        member["target_bot_id"] = str(target_bot_id or "").strip()
    return member


def sync_aggregate_from_members(state: GrowthState) -> tuple[int, int]:
    """Keep legacy overview fields as a read-only average of dyadic relations."""
    members = [
        member
        for member in state.members.values()
        if isinstance(member, dict) and str(member.get("member_id") or "").strip()
    ]
    if not members:
        return state.intimacy, state.trust
    state.intimacy = clamp(
        round(
            sum(int(member.get("intimacy", 0) or 0) for member in members)
            / len(members)
        ),
        0,
        100,
    )
    state.trust = clamp(
        round(
            sum(int(member.get("trust", 0) or 0) for member in members)
            / len(members)
        ),
        0,
        100,
    )
    return state.intimacy, state.trust


def replay_relationship(
    baseline_intimacy: int,
    baseline_trust: int,
    events: list[dict[str, Any]],
    *,
    attribute_min: int = 0,
    attribute_max: int = 100,
) -> tuple[int, int]:
    """Replay earned history on a newly generated persona/worldview baseline."""
    intimacy = clamp(int(baseline_intimacy or 0), attribute_min, attribute_max)
    trust = clamp(int(baseline_trust or 0), attribute_min, attribute_max)
    for event in sorted(
        (item for item in events if isinstance(item, dict)),
        key=lambda item: float(item.get("at", 0) or 0),
    ):
        intimacy = clamp(
            intimacy + int(event.get("intimacy_delta", 0) or 0),
            attribute_min,
            attribute_max,
        )
        trust = clamp(
            trust + int(event.get("trust_delta", 0) or 0),
            attribute_min,
            attribute_max,
        )
    return intimacy, trust


def events_today(state: GrowthState, now: float) -> int:
    today = time.strftime("%Y-%m-%d", time.localtime(float(now)))
    return sum(
        1
        for item in state.events
        if str(item.get("source_kind", "") or "").startswith("raise_event")
        and time.strftime(
            "%Y-%m-%d",
            time.localtime(float(item.get("at", 0) or 0)),
        )
        == today
    )


def load_event_templates(data_dir: Path) -> list[dict[str, Any]]:
    path = Path(data_dir) / "events.json"
    if not path.is_file():
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(DEFAULT_EVENTS, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        tmp.replace(path)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return list(DEFAULT_EVENTS)
    if not isinstance(payload, list):
        return list(DEFAULT_EVENTS)
    return [
        item
        for item in payload
        if isinstance(item, dict)
        and str(item.get("id") or "").strip()
        and str(item.get("title") or "").strip()
        and isinstance(item.get("options"), list)
        and item["options"]
    ]


def eligible_events(
    templates: list[dict[str, Any]],
    state: GrowthState,
    relationship: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    relationship = relationship if isinstance(relationship, dict) else {}
    relation_intimacy = int(relationship.get("intimacy", state.intimacy) or 0)
    relation_trust = int(relationship.get("trust", state.trust) or 0)
    result: list[dict[str, Any]] = []
    for template in templates:
        if template.get("ultimate"):
            continue
        condition = template.get("condition")
        condition = dict(condition) if isinstance(condition, dict) else {}
        try:
            min_intimacy = int(condition.get("min_intimacy", 0) or 0)
            min_trust = int(condition.get("min_trust", 0) or 0)
            min_level = int(condition.get("min_level", 1) or 1)
            min_habits = int(condition.get("min_habits", 0) or 0)
            min_special_events = int(condition.get("min_special_events", 0) or 0)
        except (TypeError, ValueError):
            continue
        if relation_intimacy < min_intimacy or relation_trust < min_trust:
            continue
        if state.level() < min_level:
            continue
        habit_count = len([item for item in state.habits if item])
        special_count = len(
            {str(item or "").strip() for item in state.special_events_completed}
        )
        if habit_count < min_habits:
            continue
        if special_count < min_special_events:
            continue
        options = [
            option
            for option in template.get("options", [])
            if isinstance(option, dict)
            and str(option.get("key") or "").strip()
            and str(option.get("text") or "").strip()
            and isinstance(option.get("deltas"), dict)
        ]
        if not options:
            continue
        result.append({**template, "options": options})
    return result


def choose_event(
    templates: list[dict[str, Any]],
    state: GrowthState,
    relationship: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    pool = eligible_events(templates, state, relationship)
    if not pool:
        return None
    weights: list[float] = []
    for item in pool:
        try:
            weight = max(0.0, float(item.get("weight", 1) or 1))
        except (TypeError, ValueError):
            weight = 1.0
        weights.append(weight)
    return random.choices(pool, weights=weights, k=1)[0]


def match_event_choice(
    text: str,
    options: list[dict[str, Any]],
) -> dict[str, Any] | None:
    raw = str(text or "").strip()
    cleaned = re.sub(r"\[@[^\]]*\]|<@[^>]*>|\[回复[^\]]*\]", " ", raw)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    token = cleaned[:6].strip().rstrip("。.!！？?")
    normalized = token.casefold().lstrip("选我挑").strip()
    key_map = {
        "a": "A",
        "1": "A",
        "甲": "A",
        "b": "B",
        "2": "B",
        "乙": "B",
        "c": "C",
        "3": "C",
        "丙": "C",
        "d": "D",
        "4": "D",
        "丁": "D",
    }
    direct = key_map.get(normalized)
    for option in options:
        key = str(option.get("key") or "").strip().upper()
        if direct and key == direct:
            return option
    for option in options:
        option_text = str(option.get("text") or "").strip()
        if len(option_text) >= 3 and option_text[:3] in cleaned:
            return option
    return None


def merge_event_story_text(
    template: dict[str, Any],
    rewritten: dict[str, Any] | None,
) -> dict[str, Any]:
    """Overlay LLM-authored prose without allowing it to alter mechanics.

    Option keys and all non-prose fields (deltas, habits, endings, etc.) come
    exclusively from the trusted template. A malformed option rewrite is
    ignored as a unit instead of partially corrupting the event.
    """
    source = dict(template) if isinstance(template, dict) else {}
    payload = dict(rewritten) if isinstance(rewritten, dict) else {}
    merged = dict(source)
    for field, maximum in (("title", 80), ("story", 4000)):
        value = str(payload.get(field) or "").strip()
        if value:
            merged[field] = value[:maximum]

    original_options = [
        dict(option)
        for option in source.get("options", [])
        if isinstance(option, dict) and str(option.get("key") or "").strip()
    ]
    raw_options = payload.get("options")
    if not isinstance(raw_options, list) or not original_options:
        return merged
    rewritten_by_key: dict[str, dict[str, Any]] = {}
    for option in raw_options:
        if not isinstance(option, dict):
            continue
        key = str(option.get("key") or "").strip().upper()
        if key and key not in rewritten_by_key:
            rewritten_by_key[key] = option
    original_keys = {
        str(option.get("key") or "").strip().upper()
        for option in original_options
    }
    if set(rewritten_by_key) != original_keys:
        return merged

    safe_options: list[dict[str, Any]] = []
    for original in original_options:
        key = str(original.get("key") or "").strip().upper()
        rewrite = rewritten_by_key[key]
        safe = dict(original)
        safe["key"] = key
        text = str(rewrite.get("text") or "").strip()
        outcome = str(rewrite.get("outcome") or "").strip()
        if text:
            safe["text"] = text[:500]
        if outcome:
            safe["outcome"] = outcome[:2000]
        safe_options.append(safe)
    merged["options"] = safe_options
    return merged


def normalize_context_event(
    payload: dict[str, Any] | None,
    *,
    event_id: str,
    max_single_delta: int = 3,
) -> dict[str, Any] | None:
    """Validate a context-generated ordinary event into a safe template."""
    if not isinstance(payload, dict):
        return None
    title = str(payload.get("title") or "").strip()[:80]
    story = str(payload.get("story") or "").strip()[:4000]
    raw_options = payload.get("options")
    if not title or not story or not isinstance(raw_options, list):
        return None
    if not 2 <= len(raw_options) <= 4:
        return None

    maximum = max(1, min(10, int(max_single_delta or 3)))
    expected_keys = ["A", "B", "C", "D"][: len(raw_options)]
    options: list[dict[str, Any]] = []
    has_positive = False
    has_cost_or_neutral = False
    for index, raw in enumerate(raw_options):
        if not isinstance(raw, dict):
            return None
        key = str(raw.get("key") or "").strip().upper()
        if key != expected_keys[index]:
            return None
        text = str(raw.get("text") or "").strip()[:500]
        outcome = str(raw.get("outcome") or "").strip()[:2000]
        if not text or not outcome:
            return None
        deltas = raw.get("deltas")
        if not isinstance(deltas, dict):
            return None
        try:
            intimacy = int(deltas.get("intimacy", 0) or 0)
            trust = int(deltas.get("trust", 0) or 0)
            exp = int(deltas.get("exp", 0) or 0)
        except (TypeError, ValueError):
            return None
        intimacy = clamp(intimacy, -maximum, maximum)
        trust = clamp(trust, -maximum, maximum)
        # Ordinary events gain most experience from positive relationship
        # movement; keep any explicit bonus deliberately small.
        exp = clamp(exp, 0, 20)
        has_positive = has_positive or intimacy > 0 or trust > 0 or exp > 0
        has_cost_or_neutral = has_cost_or_neutral or (
            intimacy <= 0 and trust <= 0 and exp == 0
        )
        option: dict[str, Any] = {
            "key": key,
            "text": text,
            "deltas": {
                "intimacy": intimacy,
                "trust": trust,
                "exp": exp,
            },
            "outcome": outcome,
        }
        habit = normalize_habit(str(raw.get("habit") or ""))
        if habit:
            option["habit"] = habit
        options.append(option)
    if not has_positive or not has_cost_or_neutral:
        return None
    return {
        "id": str(event_id or "context_event")[:160],
        "title": title,
        "story": story,
        "weight": 1,
        "condition": {},
        "options": options,
        "generated_from_context": True,
    }


def apply_event(
    state: GrowthState,
    template: dict[str, Any],
    option: dict[str, Any],
    *,
    now: float | None = None,
    max_single_delta: int = 3,
    attribute_min: int = 0,
    attribute_max: int = 100,
    daily_intimacy_cap: int = 15,
    daily_trust_cap: int = 12,
    habit_promote_count: int = 2,
    user_id: str = "",
    user_name: str = "",
    member_kind: str = "user",
    target_bot_id: str = "",
    member_initial_intimacy: int = 0,
    member_initial_trust: int = 0,
) -> dict[str, Any]:
    now = float(now if now is not None else time.time())
    deltas = dict(option.get("deltas") or {})
    try:
        intimacy_delta = int(deltas.get("intimacy", 0) or 0)
    except (TypeError, ValueError):
        intimacy_delta = 0
    try:
        trust_delta = int(deltas.get("trust", 0) or 0)
    except (TypeError, ValueError):
        trust_delta = 0
    try:
        exp_bonus = int(deltas.get("exp", 0) or 0)
    except (TypeError, ValueError):
        exp_bonus = 0
    reason = (
        f"奇遇「{str(template.get('title') or '未知事件')}」"
        f"· 选择{str(option.get('key') or '').strip()}"
    )
    applied = apply_delta(
        state,
        intimacy_delta=intimacy_delta,
        trust_delta=trust_delta,
        reason=reason,
        source_kind="raise_event",
        user_excerpt=str(option.get("text") or "").strip(),
        habit_candidate=str(option.get("habit") or "").strip(),
        now=now,
        max_single_delta=max_single_delta,
        attribute_min=attribute_min,
        attribute_max=attribute_max,
        daily_intimacy_cap=daily_intimacy_cap,
        daily_trust_cap=daily_trust_cap,
        habit_promote_count=habit_promote_count,
        user_id=user_id,
        user_name=user_name,
        member_kind=member_kind,
        target_bot_id=target_bot_id,
        member_initial_intimacy=member_initial_intimacy,
        member_initial_trust=member_initial_trust,
    )
    if exp_bonus:
        state.exp += max(0, exp_bonus)
        applied["exp_gained"] = applied.get("exp_gained", 0) + max(0, exp_bonus)
    state.last_event_at = now
    state.event_count += 1
    if template.get("special"):
        event_id = str(
            template.get("id") or template.get("template_id") or ""
        ).strip()
        if event_id and event_id not in state.special_events_completed:
            state.special_events_completed.append(event_id)
    if template.get("ultimate"):
        ending_id = str(option.get("ending") or "").strip()
        if ending_id:
            state.ending = {
                "event_id": str(
                    template.get("id") or template.get("template_id") or ""
                ).strip(),
                "title": str(template.get("title") or "未知事件"),
                "ending_id": ending_id,
                "label": str(option.get("ending_label") or "").strip() or ending_id,
                "option_key": str(option.get("key") or "").strip(),
                "outcome": str(option.get("outcome") or "").strip(),
                "ended_at": now,
                "announced": False,
            }
    return applied


def _normalize_setting_value(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number < 0:
        return None
    if number <= 1.0:
        return number * 100.0
    return min(100.0, number)


def directed_relationships(
    config: dict[str, Any],
    *,
    bot_id: str,
    group_id: str,
) -> dict[str, dict[str, Any]]:
    """Resolve directed BotMesh relationships with group rows overriding global."""
    bot_id = str(bot_id or "").strip()
    if not bot_id:
        return {}
    relations = config.get("relations", [])
    if not isinstance(relations, list):
        return {}
    users_by_id = {
        str(item.get("user_id") or "").strip(): item
        for item in config.get("users", [])
        if isinstance(item, dict) and str(item.get("user_id") or "").strip()
    }
    bots_by_id = {
        str(item.get("bot_id") or "").strip(): item
        for item in config.get("bots", [])
        if isinstance(item, dict) and str(item.get("bot_id") or "").strip()
    }
    group_relations: dict[str, dict[str, Any]] = {}
    global_relations: dict[str, dict[str, Any]] = {}
    for raw in relations:
        if not isinstance(raw, dict):
            continue
        if str(raw.get("source_bot_id") or "").strip() != bot_id:
            continue
        target = str(raw.get("target_bot_id") or "").strip()
        if not target:
            continue
        raw_group = str(raw.get("group_id") or "").strip()
        relation = {
            "target_id": target,
            "relation_type": str(raw.get("relation_type") or "").strip(),
            "affinity": _normalize_setting_value(raw.get("affinity")),
            "trust": _normalize_setting_value(raw.get("trust")),
            "familiarity": _normalize_setting_value(raw.get("familiarity")),
        }
        if raw_group == str(group_id or "").strip() and group_id:
            group_relations[target] = relation
        elif not raw_group:
            global_relations[target] = relation
    merged: dict[str, dict[str, Any]] = dict(global_relations)
    merged.update(group_relations)
    result: dict[str, dict[str, Any]] = {}
    for target, relation in merged.items():
        is_bot = target in bots_by_id or target.startswith("bot_")
        label = str(
            (
                bots_by_id.get(target, {}).get("display_name")
                or bots_by_id.get(target, {}).get("nickname")
                or target
            )
            if is_bot
            else (users_by_id.get(target, {}).get("display_name") or target)
        )
        result[target] = {
            "member_id": target,
            "name": label,
            "kind": "bot" if is_bot else "user",
            "target_bot_id": target if is_bot else "",
            **relation,
        }
    return result


def setting_baseline(
    config: dict[str, Any],
    *,
    bot_id: str,
    group_id: str,
) -> dict[str, Any] | None:
    """从 BotMesh 关系配置中提取设定初始值（0-100）。"""
    merged = directed_relationships(config, bot_id=bot_id, group_id=group_id)
    if not merged:
        return None
    per_target: dict[str, dict[str, Any]] = {}
    intimacy_values: list[float] = []
    trust_values: list[float] = []
    for target, relation in merged.items():
        affinity = relation.get("affinity")
        trust = relation.get("trust")
        if affinity is None and trust is None:
            continue
        per_target[target] = {
            "member_id": relation.get("member_id") or target,
            "name": relation.get("name") or target,
            "kind": relation.get("kind") or "user",
            "target_bot_id": relation.get("target_bot_id") or "",
            "intimacy": round(affinity) if affinity is not None else 0,
            "trust": round(trust) if trust is not None else 0,
            "relation_type": relation.get("relation_type", ""),
        }
        if affinity is not None:
            intimacy_values.append(affinity)
        if trust is not None:
            trust_values.append(trust)
    if not per_target or (not intimacy_values and not trust_values):
        return None
    return {
        "intimacy": int(round(sum(intimacy_values) / len(intimacy_values)))
        if intimacy_values
        else 0,
        "trust": int(round(sum(trust_values) / len(trust_values)))
        if trust_values
        else 0,
        "per_target": per_target,
        "count": len(per_target),
        "source": "relation",
    }


def build_relation_summary(
    config: dict[str, Any],
    *,
    bot_id: str,
    group_id: str,
) -> str:
    baseline = setting_baseline(config, bot_id=bot_id, group_id=group_id)
    if baseline is None:
        return "无显式关系设定"
    lines = []
    for item in baseline["per_target"].values():
        lines.append(
            f"- {item['name']}（{'Bot' if item['kind'] == 'bot' else '用户'}）："
            f"好感={item['intimacy']}，信任={item['trust']}"
            + (f"，关系={item['relation_type']}" if item.get("relation_type") else "")
        )
    return "\n".join(lines)


def ultimate_progress(
    state: GrowthState,
    thresholds: dict[str, Any] | None = None,
    relationship: dict[str, Any] | None = None,
) -> dict[str, Any]:
    cfg = dict(ULTIMATE_DEFAULT_THRESHOLDS)
    if isinstance(thresholds, dict):
        for key in cfg:
            try:
                cfg[key] = max(1, int(thresholds.get(key, cfg[key]) or cfg[key]))
            except (TypeError, ValueError):
                pass
    habit_count = len([item for item in state.habits if item])
    special_count = len(
        {str(item or "").strip() for item in state.special_events_completed}
    )
    relationship = relationship if isinstance(relationship, dict) else {}
    relation_intimacy = int(relationship.get("intimacy", state.intimacy) or 0)
    relation_trust = int(relationship.get("trust", state.trust) or 0)
    components = {
        "intimacy": min(1.0, max(0.0, relation_intimacy / cfg["intimacy"])),
        "trust": min(1.0, max(0.0, relation_trust / cfg["trust"])),
        "habits": min(1.0, max(0.0, habit_count / cfg["habits"])),
        "level": min(1.0, max(0.0, state.level() / cfg["level"])),
        "special_events": min(
            1.0,
            max(0.0, special_count / cfg["special_events"]),
        ),
    }
    achieved = all(value >= 1.0 for value in components.values())
    return {
        "thresholds": cfg,
        "components": components,
        "overall": sum(components.values()) / len(components),
        "achieved": achieved,
    }


def is_ultimate_achieved(
    state: GrowthState,
    thresholds: dict[str, Any] | None = None,
    relationship: dict[str, Any] | None = None,
) -> bool:
    return bool(ultimate_progress(state, thresholds, relationship)["achieved"])


def ultimate_unlock_progress(
    template: dict[str, Any],
    state: GrowthState,
    relationship: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """计算终极事件模板的解锁进度（条件全部满足时 achieved=True）。"""
    condition = template.get("condition")
    condition = dict(condition) if isinstance(condition, dict) else {}

    def value(key: str, default: int) -> int:
        try:
            return max(0, int(condition.get(key, default) or default))
        except (TypeError, ValueError):
            return max(0, int(default))

    min_intimacy = value("min_intimacy", 0)
    min_trust = value("min_trust", 0)
    min_level = value("min_level", 1)
    min_habits = value("min_habits", 0)
    min_special_events = value("min_special_events", 0)
    habit_count = len([item for item in state.habits if item])
    special_count = len(
        {str(item or "").strip() for item in state.special_events_completed}
    )
    relationship = relationship if isinstance(relationship, dict) else {}
    relation_intimacy = int(relationship.get("intimacy", state.intimacy) or 0)
    relation_trust = int(relationship.get("trust", state.trust) or 0)
    components = {
        "intimacy": (
            min(1.0, max(0.0, relation_intimacy / min_intimacy))
            if min_intimacy
            else 1.0
        ),
        "trust": (
            min(1.0, max(0.0, relation_trust / min_trust))
            if min_trust
            else 1.0
        ),
        "level": (
            min(1.0, max(0.0, state.level() / min_level))
            if min_level
            else 1.0
        ),
        "habits": (
            min(1.0, max(0.0, habit_count / min_habits))
            if min_habits
            else 1.0
        ),
        "special_events": (
            min(1.0, max(0.0, special_count / min_special_events))
            if min_special_events
            else 1.0
        ),
    }
    return {
        "components": components,
        "overall": sum(components.values()) / len(components),
        "achieved": all(value >= 1.0 for value in components.values()),
        "thresholds": {
            "intimacy": min_intimacy,
            "trust": min_trust,
            "level": min_level,
            "habits": min_habits,
            "special_events": min_special_events,
        },
    }


def is_ultimate_unlocked(
    template: dict[str, Any],
    state: GrowthState,
    relationship: dict[str, Any] | None = None,
) -> bool:
    return bool(ultimate_unlock_progress(template, state, relationship)["achieved"])


def apply_delta(
    state: GrowthState,
    *,
    intimacy_delta: int,
    trust_delta: int,
    reason: str,
    source_kind: str,
    user_excerpt: str = "",
    habit_candidate: str = "",
    now: float | None = None,
    max_single_delta: int = 3,
    attribute_min: int = 0,
    attribute_max: int = 100,
    daily_intimacy_cap: int = 15,
    daily_trust_cap: int = 12,
    habit_promote_count: int = 2,
    user_id: str = "",
    user_name: str = "",
    member_kind: str = "user",
    target_bot_id: str = "",
    member_initial_intimacy: int = 0,
    member_initial_trust: int = 0,
) -> dict[str, Any]:
    now = float(now if now is not None else time.time())
    max_single_delta = max(1, int(max_single_delta))
    requested_intimacy = clamp(
        int(intimacy_delta or 0), -max_single_delta, max_single_delta
    )
    requested_trust = clamp(
        int(trust_delta or 0), -max_single_delta, max_single_delta
    )
    member: dict[str, Any] | None = None
    member_key = str(user_id or "").strip() or str(user_name or "").strip()
    if member_key:
        member = get_member(
            state,
            user_id=user_id,
            user_name=user_name,
            kind=member_kind,
            target_bot_id=target_bot_id,
            initial_intimacy=member_initial_intimacy,
            initial_trust=member_initial_trust,
        )
        member_used = _positive_deltas_from_events(member.get("events", []), now)
        if requested_intimacy > 0:
            requested_intimacy = min(
                requested_intimacy,
                max(0, int(daily_intimacy_cap) - member_used["intimacy"]),
            )
        if requested_trust > 0:
            requested_trust = min(
                requested_trust,
                max(0, int(daily_trust_cap) - member_used["trust"]),
            )
        before_intimacy = int(member.get("intimacy", 0) or 0)
        before_trust = int(member.get("trust", 0) or 0)
        member["intimacy"] = clamp(
            before_intimacy + requested_intimacy,
            int(attribute_min),
            int(attribute_max),
        )
        member["trust"] = clamp(
            before_trust + requested_trust,
            int(attribute_min),
            int(attribute_max),
        )
        intimacy_delta = int(member["intimacy"]) - before_intimacy
        trust_delta = int(member["trust"]) - before_trust
        member.setdefault("events", []).append(
            {
                "at": now,
                "source_kind": str(source_kind or "unknown"),
                "intimacy_delta": intimacy_delta,
                "trust_delta": trust_delta,
                "reason": str(reason or "").strip()[:200],
            }
        )
        member["events"] = member["events"][-100:]
        sync_aggregate_from_members(state)
    else:
        # Compatibility path for callers without a relationship target.
        used = daily_positive_deltas(state, now)
        if requested_intimacy > 0:
            requested_intimacy = min(
                requested_intimacy,
                max(0, int(daily_intimacy_cap) - used["intimacy"]),
            )
        if requested_trust > 0:
            requested_trust = min(
                requested_trust,
                max(0, int(daily_trust_cap) - used["trust"]),
            )
        before_intimacy = state.intimacy
        before_trust = state.trust
        state.intimacy = clamp(
            state.intimacy + requested_intimacy,
            int(attribute_min),
            int(attribute_max),
        )
        state.trust = clamp(
            state.trust + requested_trust,
            int(attribute_min),
            int(attribute_max),
        )
        intimacy_delta = state.intimacy - before_intimacy
        trust_delta = state.trust - before_trust

    exp_gained = max(0, intimacy_delta) + max(0, trust_delta)
    state.exp += exp_gained

    habit_promoted = ""
    candidate = normalize_habit(habit_candidate)
    if candidate and candidate not in state.habits:
        state.habit_candidates[candidate] = (
            state.habit_candidates.get(candidate, 0) + 1
        )
        promote_count = max(1, int(habit_promote_count))
        if state.habit_candidates[candidate] >= promote_count:
            state.habits.append(candidate)
            state.habit_candidates.pop(candidate, None)
            habit_promoted = candidate

    event = {
        "at": now,
        "source_kind": str(source_kind or "unknown"),
        "intimacy_delta": intimacy_delta,
        "trust_delta": trust_delta,
        "exp_gained": exp_gained,
        "reason": str(reason or "").strip()[:300],
        "habit_candidate": candidate,
        "habit_promoted": habit_promoted,
        "user_excerpt": str(user_excerpt or "").strip()[:120],
        "member_id": str(member.get("member_id") or "") if member else "",
        "member_name": str(member.get("name") or "") if member else "",
        "member_kind": str(member.get("kind") or "") if member else "",
        "target_bot_id": str(member.get("target_bot_id") or "") if member else "",
    }
    state.events.append(event)
    state.events = state.events[-MAX_RETAINED_EVENTS:]
    state.updated_at = now
    return {
        "intimacy_delta": intimacy_delta,
        "trust_delta": trust_delta,
        "exp_gained": exp_gained,
        "habit_promoted": habit_promoted,
        "member_id": str(member.get("member_id") or "") if member else "",
        "member_name": str(member.get("name") or "") if member else "",
        "relationship_intimacy": int(member.get("intimacy", state.intimacy))
        if member
        else state.intimacy,
        "relationship_trust": int(member.get("trust", state.trust))
        if member
        else state.trust,
    }
