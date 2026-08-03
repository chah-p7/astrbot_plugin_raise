from __future__ import annotations

import asyncio
import hashlib
import importlib
import inspect
import json
import random
import re
import time
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import Plain
from astrbot.api.star import Context, Star, StarTools

try:
    from astrbot.api.web import error_response, json_response, request
except ImportError:
    error_response = None
    json_response = None
    request = None

from .core import (
    GrowthState,
    HABIT_MILESTONES,
    INTIMACY_TIERS,
    RELATIONSHIP_SCHEMA_VERSION,
    TRUST_TIERS,
    apply_delta,
    apply_event,
    choose_event,
    clamp,
    cumulative_exp,
    daily_positive_deltas,
    directed_relationships,
    events_today,
    is_ultimate_unlocked,
    level_progress,
    load_event_templates,
    load_state,
    match_event_choice,
    merge_event_story_text,
    next_level_exp,
    normalize_context_event,
    progress_bar,
    replay_relationship,
    save_state,
    setting_baseline,
    sync_aggregate_from_members,
    tier_progress,
    ultimate_unlock_progress,
    level_for_exp,
)
from .extractor import (
    extract_growth_delta,
    generate_context_event,
    generate_event_template,
    infer_event_outcome,
    infer_event_story,
    infer_member_initial_values,
    judge_event_trigger,
)


PLUGIN_NAME = "astrbot_plugin_raise"
APOLOGY_RE = re.compile(r"(?:对不起|抱歉|我错了|我的错|不该|是我不好|原谅我)")


def _query_value(request_obj: Any, key: str, default: str = "") -> Any:
    """读取插件 Web 请求的查询参数（兼容 query/args 两种形态）。"""
    for attribute in ("query", "args"):
        values = getattr(request_obj, attribute, None)
        getter = getattr(values, "get", None)
        if callable(getter):
            return getter(key, default)
    return default


class RaisePlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.context = context
        self.config = config
        self.data_dir = StarTools.get_data_dir(PLUGIN_NAME)
        self._tasks: set[asyncio.Task[Any]] = set()
        self._state_locks: dict[str, asyncio.Lock] = {}
        self._register_web_apis()

    async def initialize(self) -> None:
        task = asyncio.create_task(
            self._delayed_relationship_migration(),
            name="raise-relationship-schema-v2-migration",
        )
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _delayed_relationship_migration(self) -> None:
        # AstrBot loads plugins before model providers. Give providers time to
        # register, otherwise every baseline would immediately take fallback.
        await asyncio.sleep(8)
        await self._migrate_existing_relationships()

    async def terminate(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        self._tasks.clear()

    # =========================
    # 作用域与状态
    # =========================

    def _botmesh_scope(self, event: AstrMessageEvent) -> dict[str, Any]:
        try:
            integration = importlib.import_module(
                "astrbot_plugin_botmesh.integration"
            )
            method = getattr(integration, "get_chat_history_scope", None)
            if callable(method):
                result = method(
                    umo=str(event.unified_msg_origin or ""),
                    event=event,
                )
                if inspect.isawaitable(result):
                    # 同步路径下不会出现 awaitable，这里仅做防御。
                    return {}
                return dict(result) if isinstance(result, dict) else {}
        except (ImportError, AttributeError):
            pass
        except Exception as exc:
            logger.debug("[Raise] 读取 BotMesh 作用域失败: %s", exc)
        return {}

    def _botmesh_scope_fallback(self, event: AstrMessageEvent) -> dict[str, Any]:
        config = self._read_botmesh_config()
        if not config:
            return {}
        umo = str(event.unified_msg_origin or "")
        parts = umo.split(":", 2)
        platform_id = parts[0].strip() if parts else ""
        raw_group_id = parts[2].strip() if len(parts) == 3 else ""
        try:
            event_bot_id = str(event.get_self_id() or "").strip()
        except Exception:
            event_bot_id = ""
        bots = [
            item
            for item in config.get("bots", [])
            if isinstance(item, dict)
        ]
        bot = next(
            (
                item
                for item in bots
                if str(item.get("platform_id") or "") == platform_id
                or str(item.get("bot_id") or "") == event_bot_id
                or str(item.get("bot_id") or "").removeprefix("bot_")
                == event_bot_id
                or str(item.get("account_id") or "") == event_bot_id
            ),
            None,
        )
        if bot is None or not raw_group_id:
            return {}
        bot_id = str(bot.get("bot_id") or "").strip()
        binding = next(
            (
                item
                for item in config.get("group_bindings", [])
                if isinstance(item, dict)
                and str(item.get("bot_id") or "") == bot_id
                and str(item.get("platform_group_id") or "") == raw_group_id
            ),
            None,
        )
        if binding is None:
            return {}
        group_id = str(binding.get("group_id") or "").strip()
        if not group_id:
            return {}
        profiles = [
            item
            for item in config.get("persona_profiles", [])
            if isinstance(item, dict)
            and str(item.get("bot_id") or "") == bot_id
        ]
        identity: dict[str, Any] = {}
        for profile in profiles:
            if not str(profile.get("group_id") or "").strip():
                identity.update(
                    {
                        key: value
                        for key, value in profile.items()
                        if value not in ("", None)
                    }
                )
        for profile in profiles:
            if str(profile.get("group_id") or "") == group_id:
                identity.update(
                    {
                        key: value
                        for key, value in profile.items()
                        if value not in ("", None)
                    }
                )
        memory_key = str(
            identity.get("memory_key")
            or identity.get("soul_identity")
            or identity.get("self_identity")
            or bot_id
        ).strip()
        display_name = str(
            bot.get("display_name")
            or bot.get("nickname")
            or bot.get("account_id")
            or bot_id
        ).strip()
        identity.update(
            {
                "memory_key": memory_key,
                "bot_id": bot_id,
                "group_id": group_id,
                "account_label": display_name,
            }
        )
        return {
            "scope_id": f"botmesh:{group_id}",
            "logical_group_id": group_id,
            "bot_id": bot_id,
            "bot_display_name": display_name,
            "platform_id": platform_id,
            "raw_group_id": raw_group_id,
            "identity_state": identity,
            "memory_key": memory_key,
            "selectors": [
                f"botmesh:{group_id}",
                raw_group_id,
                f"{platform_id}:{raw_group_id}",
                f"{platform_id}:GroupMessage:{raw_group_id}",
            ],
        }

    def _scope_for_event(self, event: AstrMessageEvent) -> dict[str, Any]:
        mapped = self._botmesh_scope(event)
        if not str(mapped.get("bot_id") or "").strip():
            mapped = self._botmesh_scope_fallback(event)
        bot_id = str(mapped.get("bot_id") or "").strip()
        if not bot_id:
            try:
                bot_id = str(event.get_self_id() or "").strip()
            except Exception:
                bot_id = ""
        logical_group_id = str(mapped.get("logical_group_id") or "").strip()
        identity = mapped.get("identity_state")
        identity = dict(identity) if isinstance(identity, dict) else {}
        account_label = str(
            mapped.get("bot_display_name")
            or identity.get("account_label")
            or identity.get("display_name")
            or bot_id
        ).strip()
        memory_key = str(
            identity.get("memory_key")
            or identity.get("soul_identity")
            or identity.get("self_identity")
            or bot_id
        ).strip()
        display_name = str(
            identity.get("display_name")
            or identity.get("self_name")
            or identity.get("self_identity")
            or identity.get("soul_identity")
            or memory_key
            or account_label
        ).strip()
        scope_id = f"botmesh:{logical_group_id}" if logical_group_id else str(
            event.unified_msg_origin or ""
        )
        return {
            "scope_id": scope_id,
            "bot_id": bot_id,
            "logical_group_id": logical_group_id,
            "display_name": display_name,
            "account_label": account_label,
            "identity": identity,
            "memory_key": memory_key,
            "umo": str(event.unified_msg_origin or ""),
        }

    def _state_path(self, scope_id: str, identity_key: str = "") -> Path:
        material = f"{scope_id or 'unknown'}|{identity_key or 'default'}"
        digest = hashlib.sha256(material.encode("utf-8")).hexdigest()[:24]
        return self.data_dir / "growth" / f"{digest}.json"

    def _state_lock_for_scope(self, scope: dict[str, Any]) -> asyncio.Lock:
        identity_key = scope.get("memory_key") or scope.get("bot_id") or "unknown"
        path = self._state_path(str(scope.get("scope_id") or ""), str(identity_key))
        return self._state_locks.setdefault(str(path), asyncio.Lock())

    def _read_botmesh_config(self) -> dict[str, Any]:
        candidates = [
            self.data_dir.parent / "config" / "astrbot_plugin_botmesh_config.json",
            self.data_dir.parent.parent
            / "config"
            / "astrbot_plugin_botmesh_config.json",
            Path("/root/data/config/astrbot_plugin_botmesh_config.json"),
        ]
        for path in candidates:
            if path.is_file():
                try:
                    payload = json.loads(
                        path.read_text(encoding="utf-8-sig")
                    )
                    return payload if isinstance(payload, dict) else {}
                except (OSError, ValueError, TypeError):
                    continue
        return {}

    @staticmethod
    def _resolved_persona_profile(
        config: dict[str, Any],
        *,
        bot_id: str,
        group_id: str,
    ) -> dict[str, Any]:
        profiles = [
            item
            for item in config.get("persona_profiles", [])
            if isinstance(item, dict)
            and str(item.get("bot_id") or "").strip() == str(bot_id or "").strip()
        ]
        global_profile = next(
            (item for item in profiles if not str(item.get("group_id") or "").strip()),
            {},
        )
        exact_profile = next(
            (
                item
                for item in profiles
                if str(item.get("group_id") or "").strip()
                == str(group_id or "").strip()
                and str(group_id or "").strip()
            ),
            {},
        )

        def section(key: str) -> str:
            exact = str(exact_profile.get(key) or "").strip()
            global_value = str(global_profile.get(key) or "").strip()
            return exact or global_value

        personality = section("personality_prompt")
        worldview = section("worldview_prompt")
        if not personality and not worldview:
            personality = section("system_prompt") or section("prompt")
        identity: dict[str, Any] = {}
        for profile in (global_profile, exact_profile):
            for key in (
                "self_identity",
                "body_identity",
                "soul_identity",
                "identity_note",
                "memory_key",
                "account_label",
            ):
                value = profile.get(key)
                if value not in (None, ""):
                    identity[key] = value
        bot = next(
            (
                item
                for item in config.get("bots", [])
                if isinstance(item, dict)
                and str(item.get("bot_id") or "").strip() == str(bot_id or "").strip()
            ),
            {},
        )
        return {
            "bot_id": str(bot_id or "").strip(),
            "name": str(
                bot.get("display_name")
                or bot.get("nickname")
                or bot.get("account_id")
                or bot_id
            ).strip(),
            "identity": identity,
            "personality": personality[:20000],
            "worldview": worldview[:20000],
        }

    @staticmethod
    def _target_profile(
        config: dict[str, Any],
        *,
        target: dict[str, Any],
        group_id: str,
    ) -> dict[str, Any]:
        target_id = str(target.get("member_id") or target.get("key") or "").strip()
        if str(target.get("kind") or "") == "bot" or str(
            target.get("target_bot_id") or ""
        ).strip():
            bot_id = str(target.get("target_bot_id") or target_id).strip()
            return RaisePlugin._resolved_persona_profile(
                config,
                bot_id=bot_id,
                group_id=group_id,
            )
        user = next(
            (
                item
                for item in config.get("users", [])
                if isinstance(item, dict)
                and str(item.get("user_id") or "").strip() == target_id
            ),
            {},
        )
        return {
            "user_id": target_id,
            "name": str(
                target.get("name")
                or user.get("display_name")
                or target_id
            ).strip(),
            "description": str(user.get("description") or "").strip()[:4000],
            "aliases": [
                str(item or "").strip()
                for item in user.get("aliases", []) or []
                if str(item or "").strip()
            ][:20],
        }

    def _relationship_candidates(
        self,
        scope: dict[str, Any],
    ) -> dict[str, dict[str, Any]]:
        return directed_relationships(
            self._read_botmesh_config(),
            bot_id=str(scope.get("bot_id") or ""),
            group_id=str(scope.get("logical_group_id") or ""),
        )

    def _relationship_fingerprint(
        self,
        *,
        source_profile: dict[str, Any],
        target_profile: dict[str, Any],
        relationship: dict[str, Any],
    ) -> str:
        material = json.dumps(
            {
                "schema": RELATIONSHIP_SCHEMA_VERSION,
                "initial_source": str(self.config.get("initial_source", "auto") or "auto"),
                "source": source_profile,
                "target": target_profile,
                "relationship": {
                    key: relationship.get(key)
                    for key in ("relation_type", "affinity", "trust", "familiarity")
                },
            },
            ensure_ascii=False,
            sort_keys=True,
            default=str,
        )
        return hashlib.sha256(material.encode("utf-8")).hexdigest()[:24]

    async def _infer_relationship_baseline(
        self,
        scope: dict[str, Any],
        target: dict[str, Any],
        relationship: dict[str, Any],
    ) -> dict[str, Any]:
        config = self._read_botmesh_config()
        source_profile = self._resolved_persona_profile(
            config,
            bot_id=scope["bot_id"],
            group_id=scope["logical_group_id"],
        )
        target_profile = self._target_profile(
            config,
            target=target,
            group_id=scope["logical_group_id"],
        )
        fingerprint = self._relationship_fingerprint(
            source_profile=source_profile,
            target_profile=target_profile,
            relationship=relationship,
        )
        initial_source = str(
            self.config.get("initial_source", "auto") or "auto"
        ).strip()
        if initial_source not in {"auto", "relation", "llm", "fixed"}:
            initial_source = "auto"
        fixed_intimacy = self._cfg_int("initial_intimacy", 0, 0, 100)
        fixed_trust = self._cfg_int("initial_trust", 0, 0, 100)

        if initial_source == "fixed":
            return {
                "intimacy": fixed_intimacy,
                "trust": fixed_trust,
                "source": "fixed",
                "reason": "管理员固定初始值",
                "fingerprint": fingerprint,
            }
        if initial_source == "relation":
            return {
                "intimacy": clamp(
                    int(relationship.get("affinity", fixed_intimacy) or 0), 0, 100
                ),
                "trust": clamp(
                    int(relationship.get("trust", fixed_trust) or 0), 0, 100
                ),
                "source": "relation",
                "reason": str(relationship.get("relation_type") or "关系表基线"),
                "fingerprint": fingerprint,
            }

        inferred: dict[str, Any] = {}
        if source_profile.get("personality") or source_profile.get("worldview"):
            inferred = await infer_member_initial_values(
                self.context,
                config=self.config,
                umo=str(scope.get("umo") or ""),
                source_profile=source_profile,
                target_profile=target_profile,
                relationship={
                    key: relationship.get(key)
                    for key in ("relation_type", "affinity", "trust", "familiarity")
                },
            )
        if inferred:
            return {
                "intimacy": clamp(int(inferred.get("intimacy", 0) or 0), 0, 100),
                "trust": clamp(int(inferred.get("trust", 0) or 0), 0, 100),
                "source": "persona_worldview_llm",
                "reason": str(inferred.get("reason") or "").strip(),
                "fingerprint": fingerprint,
            }

        affinity = relationship.get("affinity")
        trust = relationship.get("trust")
        return {
            "intimacy": clamp(
                int(affinity if affinity is not None else fixed_intimacy), 0, 100
            ),
            "trust": clamp(int(trust if trust is not None else fixed_trust), 0, 100),
            "source": "relation_fallback" if relationship else "fixed_fallback",
            "reason": "人格/世界观推断失败，采用安全回退基线",
            "fingerprint": fingerprint,
        }

    async def _reconcile_relationships(
        self,
        state,
        path: Path,
        scope: dict[str, Any],
        *,
        current_member: dict[str, Any] | None = None,
    ) -> bool:
        candidates = self._relationship_candidates(scope)
        if isinstance(current_member, dict):
            key = str(current_member.get("key") or current_member.get("member_id") or "").strip()
            if key and key not in candidates:
                candidates[key] = {
                    "member_id": key,
                    "name": str(current_member.get("name") or key),
                    "kind": str(current_member.get("kind") or "user"),
                    "target_bot_id": str(current_member.get("target_bot_id") or ""),
                    "relation_type": "",
                    "affinity": None,
                    "trust": None,
                    "familiarity": None,
                }
        changed = False
        config = self._read_botmesh_config()
        source_profile = self._resolved_persona_profile(
            config,
            bot_id=scope["bot_id"],
            group_id=scope["logical_group_id"],
        )
        for key, relationship in candidates.items():
            target = {
                "member_id": key,
                "name": str(relationship.get("name") or key),
                "kind": str(relationship.get("kind") or "user"),
                "target_bot_id": str(relationship.get("target_bot_id") or ""),
            }
            target_profile = self._target_profile(
                config,
                target=target,
                group_id=scope["logical_group_id"],
            )
            expected_fingerprint = self._relationship_fingerprint(
                source_profile=source_profile,
                target_profile=target_profile,
                relationship=relationship,
            )
            member = state.members.get(key)
            retryable_fallback = (
                isinstance(member, dict)
                and str(member.get("baseline_source") or "")
                in {"relation_fallback", "fixed_fallback"}
            )
            retry_after = (
                float(member.get("baseline_retry_after", 0) or 0)
                if isinstance(member, dict)
                else 0.0
            )
            if (
                isinstance(member, dict)
                and int(member.get("relationship_schema_version", 1) or 1)
                >= RELATIONSHIP_SCHEMA_VERSION
                and str(member.get("baseline_fingerprint") or "")
                == expected_fingerprint
                and (not retryable_fallback or time.time() < retry_after)
            ):
                if target["name"] and member.get("name") != target["name"]:
                    member["name"] = target["name"]
                    changed = True
                continue

            baseline = await self._infer_relationship_baseline(
                scope,
                target,
                relationship,
            )
            events = list(member.get("events", []) or []) if isinstance(member, dict) else []
            intimacy, trust = replay_relationship(
                baseline["intimacy"],
                baseline["trust"],
                events,
                attribute_min=self._cfg_int("attribute_min", 0, 0, 100),
                attribute_max=self._cfg_int("attribute_max", 100, 1, 100),
            )
            created_at = (
                float(member.get("created_at", 0) or 0)
                if isinstance(member, dict)
                else 0.0
            )
            state.members[key] = {
                **(member if isinstance(member, dict) else {}),
                "member_id": key,
                "name": target["name"],
                "kind": target["kind"],
                "target_bot_id": target["target_bot_id"],
                "intimacy": intimacy,
                "trust": trust,
                "events": events[-100:],
                "created_at": created_at or time.time(),
                "relationship_schema_version": RELATIONSHIP_SCHEMA_VERSION,
                "baseline_intimacy": baseline["intimacy"],
                "baseline_trust": baseline["trust"],
                "baseline_source": baseline["source"],
                "baseline_reason": baseline["reason"],
                "baseline_fingerprint": baseline["fingerprint"],
                "baseline_generated_at": time.time(),
                "baseline_retry_after": (
                    time.time() + 300
                    if baseline["source"] in {"relation_fallback", "fixed_fallback"}
                    else 0.0
                ),
            }
            changed = True
            logger.info(
                "[Raise] 定向关系基线已生成 scope=%s source=%s target=%s via=%s 好感=%d 信任=%d",
                scope["scope_id"],
                scope["bot_id"],
                key,
                baseline["source"],
                baseline["intimacy"],
                baseline["trust"],
            )
        if state.relationship_schema_version < RELATIONSHIP_SCHEMA_VERSION:
            state.relationship_schema_version = RELATIONSHIP_SCHEMA_VERSION
            changed = True
        state.init_status = "done"
        state.init_note = f"persona_worldview:v2:{len(state.members)}"
        if state.members:
            before = (state.intimacy, state.trust)
            sync_aggregate_from_members(state)
            changed = changed or before != (state.intimacy, state.trust)
        if changed:
            state.updated_at = time.time()
            save_state(path, state)
        return changed

    async def _migrate_existing_relationships(self) -> None:
        growth_dir = self.data_dir / "growth"
        if not growth_dir.is_dir():
            return
        migrated = 0
        failed = 0
        for path in sorted(growth_dir.glob("*.json")):
            try:
                payload = json.loads(path.read_text(encoding="utf-8-sig"))
                if not isinstance(payload, dict):
                    continue
                state = load_state(
                    path,
                    scope_id=str(payload.get("scope_id") or ""),
                    bot_id=str(payload.get("bot_id") or ""),
                    logical_group_id=str(payload.get("logical_group_id") or ""),
                )
                scope = {
                    "scope_id": state.scope_id,
                    "bot_id": state.bot_id,
                    "logical_group_id": state.logical_group_id,
                    "memory_key": state.memory_key,
                    "display_name": state.display_name,
                    "identity": {},
                    "umo": f"raise:migration:{state.scope_id}",
                }
                async with self._state_lock_for_scope(scope):
                    if await self._reconcile_relationships(state, path, scope):
                        migrated += 1
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                failed += 1
                logger.exception("[Raise] 定向关系迁移失败 file=%s: %s", path.name, exc)
        logger.info(
            "[Raise] 定向关系迁移完成 migrated=%d failed=%d schema=%d",
            migrated,
            failed,
            RELATIONSHIP_SCHEMA_VERSION,
        )

    def _management_labels(self) -> dict[str, dict[str, str]]:
        try:
            integration = importlib.import_module(
                "astrbot_plugin_botmesh.integration"
            )
            method = getattr(integration, "get_management_labels", None)
            result = method() if callable(method) else {}
            return dict(result) if isinstance(result, dict) else {}
        except (ImportError, AttributeError):
            return {}
        except Exception as exc:
            logger.debug("[Raise] 读取 BotMesh 标签失败: %s", exc)
            return {}

    def _load_state(self, event: AstrMessageEvent):
        scope = self._scope_for_event(event)
        identity_key = scope["memory_key"] or scope["bot_id"] or "unknown"
        path = self._state_path(scope["scope_id"], identity_key)
        fresh = not path.exists()
        state = load_state(
            path,
            scope_id=scope["scope_id"],
            bot_id=scope["bot_id"],
            logical_group_id=scope["logical_group_id"],
        )
        if scope["display_name"] and state.display_name != scope["display_name"]:
            state.display_name = scope["display_name"]
        if not state.memory_key:
            state.memory_key = scope["memory_key"]
        if fresh:
            initial_source = str(
                self.config.get("initial_source", "auto") or "auto"
            ).strip()
            if initial_source == "fixed":
                state.intimacy = self._cfg_int("initial_intimacy", 0, 0, 100)
                state.trust = self._cfg_int("initial_trust", 0, 0, 100)
                state.exp = max(0, self._cfg_int("initial_exp", 0, 0, 100000))
                state.init_status = "done"
                state.init_note = "fixed"
            else:
                state.init_status = "pending"
            save_state(path, state)
        return state, path, scope

    def _setting_baseline(
        self,
        scope: dict[str, Any],
    ) -> dict[str, Any] | None:
        config = self._read_botmesh_config()
        if not config:
            return None
        return setting_baseline(
            config,
            bot_id=scope["bot_id"],
            group_id=scope["logical_group_id"],
        )

    def _canonical_sender(
        self,
        scope: dict[str, Any],
        event: AstrMessageEvent,
    ) -> dict[str, Any]:
        try:
            sender_id = str(event.get_sender_id() or "").strip()
        except Exception:
            sender_id = ""
        try:
            sender_name = str(event.get_sender_name() or "").strip()
        except Exception:
            sender_name = ""
        labels = self._management_labels()
        bot_ids = labels.get("bot_ids") if isinstance(labels, dict) else {}
        bots = labels.get("bots") if isinstance(labels, dict) else {}
        if sender_id and isinstance(bot_ids, dict) and sender_id in bot_ids:
            target_bot_id = str(bot_ids.get(sender_id) or "").strip()
            name = ""
            if isinstance(bots, dict):
                name = str(bots.get(sender_id) or "").strip()
            return {
                "key": target_bot_id or sender_id,
                "name": name or sender_name,
                "kind": "bot",
                "target_bot_id": target_bot_id,
            }
        config = self._read_botmesh_config()
        for user in config.get("users", []):
            if not isinstance(user, dict):
                continue
            user_id = str(user.get("user_id") or "").strip()
            if not user_id:
                continue
            display_name = str(user.get("display_name") or "").strip()
            raw_aliases = user.get("aliases") or []
            aliases = [
                str(item or "").strip()
                for item in raw_aliases
                if isinstance(item, str) and str(item).strip()
            ]
            raw_account_ids = user.get("account_ids") or []
            if isinstance(raw_account_ids, str):
                raw_account_ids = [raw_account_ids]
            if isinstance(raw_account_ids, list):
                aliases.extend(
                    str(item or "").strip()
                    for item in raw_account_ids
                    if str(item or "").strip()
                )
            account_id = str(user.get("account_id") or "").strip()
            sender_id_fold = sender_id.casefold()
            sender_name_fold = sender_name.casefold()
            if sender_id_fold and sender_id_fold in {
                account_id.casefold(),
                *[alias.casefold() for alias in aliases],
            }:
                return {
                    "key": user_id,
                    "name": display_name or sender_name,
                    "kind": "user",
                    "target_bot_id": "",
                }
            if sender_name_fold:
                name_candidates = {
                    display_name.casefold(),
                    *[alias.casefold() for alias in aliases],
                }
                if (
                    sender_name_fold in name_candidates
                    or sender_name_fold.rstrip("_") in name_candidates
                    or sender_name_fold
                    in {candidate.rstrip("_") for candidate in name_candidates}
                ):
                    return {
                        "key": user_id,
                        "name": display_name or sender_name,
                        "kind": "user",
                        "target_bot_id": "",
                    }
        return {
            "key": sender_id or sender_name or "unknown",
            "name": sender_name,
            "kind": "user",
            "target_bot_id": "",
        }

    def _member_initial_values(
        self,
        state,
        scope: dict[str, Any],
        canonical: dict[str, Any],
    ) -> tuple[int, int]:
        member = state.members.get(canonical["key"])
        if isinstance(member, dict):
            return (
                int(member.get("intimacy", 0) or 0),
                int(member.get("trust", 0) or 0),
            )
        baseline = self._setting_baseline(scope)
        if baseline is not None:
            per_target = baseline.get("per_target") or {}
            target = per_target.get(canonical["key"])
            if isinstance(target, dict):
                return (
                    int(target.get("intimacy", 0) or 0),
                    int(target.get("trust", 0) or 0),
                )
        return (
            self._cfg_int("initial_intimacy", 0, 0, 100),
            self._cfg_int("initial_trust", 0, 0, 100),
        )

    async def _ensure_initialized(
        self,
        event: AstrMessageEvent,
        state,
        path: Path,
        scope: dict[str, Any],
    ) -> None:
        canonical = self._canonical_sender(scope, event)
        await self._reconcile_relationships(
            state,
            path,
            scope,
            current_member=canonical,
        )

    # =========================
    # 上下文注入
    # =========================

    @filter.on_llm_request(priority=85)
    async def inject_growth_state(self, event: AstrMessageEvent, req: Any) -> None:
        if not bool(self.config.get("enabled", True)):
            return
        if not event.get_group_id():
            return
        if not self._is_session_enabled(event):
            return
        state, _path, scope = self._load_state(event)
        await self._ensure_initialized(event, state, _path, scope)
        canonical = self._canonical_sender(scope, event)
        member = state.members.get(canonical["key"])
        block = self._build_growth_block(state, scope, member=member)
        existing = str(getattr(req, "system_prompt", "") or "")
        if block:
            if "<growth_state>" in existing:
                existing = re.sub(
                    r"\s*<growth_state>.*?</growth_state>\s*",
                    "\n",
                    existing,
                    flags=re.S,
                ).strip()
            req.system_prompt = f"{existing}\n{block}".strip()
        pending = self._load_pending(scope)
        if isinstance(pending, dict):
            event_block = self._build_active_event_block(pending)
            if event_block:
                req.system_prompt = f"{req.system_prompt}\n{event_block}".strip()

    def _build_active_event_block(self, pending: dict[str, Any]) -> str:
        title = str(pending.get("title") or "").strip()
        story = str(pending.get("story") or "").strip()
        if not title and not story:
            return ""
        lines = [
            "<active_event>",
            "当前正有一段奇遇剧情在进行中，你的言行要自然融入这段剧情，不要跳出角色：",
        ]
        if title:
            lines.append(f"事件：{title}")
        if story:
            lines.append(f"剧情：{story}")
        options = pending.get("options") or []
        valid_options = [
            option
            for option in options
            if isinstance(option, dict)
            and str(option.get("key") or "").strip()
            and str(option.get("text") or "").strip()
        ]
        if valid_options:
            lines.append("正在等待选择（群员回复对应选项即可推进剧情）：")
            for option in valid_options:
                lines.append(
                    f"- {option.get('key')}. {option.get('text')}"
                )
        lines.extend(
            [
                "不要向用户复述以上内部信息或选项清单之外的幕后机制；",
                "如果用户没有选择选项而是正常说话，就继续以角色身份回应，剧情保持进行中。",
                "</active_event>",
            ]
        )
        return "\n".join(lines)

    def _build_growth_block(
        self,
        state,
        scope: dict[str, Any],
        member: dict[str, Any] | None = None,
    ) -> str:
        if not state.bot_id:
            return ""
        lines = [
            "<growth_state>",
            "以下是你的养成成长状态（内部参考，不要向用户复述具体数值或幕后机制）：",
            f"等级：Lv.{state.level()}（经验 {state.exp}/{state.next_exp()}）",
        ]
        if isinstance(state.ending, dict) and state.ending.get("ending_id"):
            lines.append(
                f"你已历经终局：{state.ending.get('label') or '未知结局'}。"
                "后续言行应与这个结局一致。"
            )
        if isinstance(member, dict) and member.get("member_id"):
            lines.append(
                f"你对当前对话者 {member.get('name') or member.get('member_id')} 的独立关系："
                f"好感={clamp(int(member.get('intimacy', 0) or 0), 0, 100)}，"
                f"信任={clamp(int(member.get('trust', 0) or 0), 0, 100)}"
            )
        else:
            lines.append(
                f"群内关系概况（成员均值）：好感={clamp(state.intimacy, 0, 100)}，"
                f"信任={clamp(state.trust, 0, 100)}"
            )
        relation_events = (
            list(member.get("events", []) or [])
            if isinstance(member, dict)
            else state.events
        )
        recent = [
            item
            for item in relation_events[-5:]
            if int(item.get("intimacy_delta", 0) or 0)
            or int(item.get("trust_delta", 0) or 0)
            or item.get("habit_promoted")
        ][-3:]
        if recent:
            lines.append("最近成长：")
            for item in recent:
                parts = []
                d_intimacy = int(item.get("intimacy_delta", 0) or 0)
                d_trust = int(item.get("trust_delta", 0) or 0)
                if d_intimacy:
                    parts.append(f"好感{d_intimacy:+d}")
                if d_trust:
                    parts.append(f"信任{d_trust:+d}")
                reason = str(item.get("reason", "") or "").strip()
                promoted = str(item.get("habit_promoted", "") or "").strip()
                label = "、".join(parts) or ("新习惯：" + promoted if promoted else "成长")
                lines.append(f"- {label}：{reason or '无记录'}")
        habits = [item for item in state.habits if item]
        if habits:
            lines.append(f"已养成习惯：{'、'.join(habits)}")
        lines.extend(
            [
                "规则：只根据这些状态调整语气、主动程度、信任深度和亲密度；",
                "不得覆盖 BotMesh Persona 中的身份设定；",
                "不要把数值直接告诉用户，也不要用'养成系统''好感度+1'这类表达。",
                "</growth_state>",
            ]
        )
        return "\n".join(lines)

    def _build_goals_lines(
        self,
        state,
        member: dict[str, Any] | None = None,
    ) -> list[str]:
        lines: list[str] = []
        relationship = member if isinstance(member, dict) else {}
        relation_intimacy = int(relationship.get("intimacy", state.intimacy) or 0)
        relation_trust = int(relationship.get("trust", state.trust) or 0)
        level, base, nxt, level_progress_value = level_progress(state.exp)
        lines.append(
            f"· 等级 Lv.{level} {progress_bar(level_progress_value)} "
            f"{state.exp}/{nxt} → 升到 Lv.{level + 1}"
        )
        intimacy_tier = tier_progress(relation_intimacy, INTIMACY_TIERS)
        trust_tier = tier_progress(relation_trust, TRUST_TIERS)
        if intimacy_tier.get("next_start") is not None:
            lines.append(
                f"· 好感 {relation_intimacy}/100 [{intimacy_tier['current']}] "
                f"{progress_bar(intimacy_tier['progress'])} "
                f"→ {intimacy_tier['next']}（{intimacy_tier['next_start']}）"
            )
        else:
            lines.append(f"· 好感 {relation_intimacy}/100 [{intimacy_tier['current']}]（已满）")
        if trust_tier.get("next_start") is not None:
            lines.append(
                f"· 信任 {relation_trust}/100 [{trust_tier['current']}] "
                f"{progress_bar(trust_tier['progress'])} "
                f"→ {trust_tier['next']}（{trust_tier['next_start']}）"
            )
        else:
            lines.append(f"· 信任 {relation_trust}/100 [{trust_tier['current']}]（已满）")
        habit_count = len([item for item in state.habits if item])
        habit_next = next(
            (milestone for milestone in HABIT_MILESTONES if milestone > habit_count),
            None,
        )
        if habit_next is not None:
            lines.append(f"· 习惯 {habit_count} 个 {progress_bar(habit_count / habit_next)} → 养成 {habit_next} 个")
        else:
            lines.append(f"· 习惯 {habit_count} 个（已达当前里程碑上限）")
        used = daily_positive_deltas(state, time.time(), relationship or None)
        daily_intimacy_cap = self._cfg_int("daily_intimacy_cap", 15, 0, 100)
        daily_trust_cap = self._cfg_int("daily_trust_cap", 12, 0, 100)
        lines.append(
            f"· 今日成长：好感 +{used['intimacy']}/{daily_intimacy_cap}，"
            f"信任 +{used['trust']}/{daily_trust_cap}"
        )
        if self._cfg_bool("ultimate_enabled", True):
            lines.append("")
            if isinstance(state.ending, dict) and state.ending.get("ending_id"):
                lines.append(
                    f"🏆 终局已达成：{state.ending.get('label') or '未知结局'}"
                )
            else:
                finale = self._ultimate_template()
                if finale is not None:
                    ultimate = ultimate_unlock_progress(finale, state, relationship or None)
                    thresholds = ultimate["thresholds"]
                    components = ultimate["components"]
                    lines.append(
                        f"🏆 终极目标：{finale.get('title') or '未知事件'}"
                    )
                    if ultimate["achieved"]:
                        lines.append("· 解锁条件已满足，事件将在下次互动时到来")
                    else:
                        lines.append(
                            f"· 好感 {relation_intimacy}/{thresholds['intimacy']} "
                            f"{progress_bar(components['intimacy'])}"
                        )
                        lines.append(
                            f"· 信任 {relation_trust}/{thresholds['trust']} "
                            f"{progress_bar(components['trust'])}"
                        )
                        special_count = len(
                            set(
                                str(item or "").strip()
                                for item in state.special_events_completed
                            )
                        )
                        lines.append(
                            f"· 习惯 {habit_count}/{thresholds['habits']} "
                            f"{progress_bar(components['habits'])}"
                        )
                        lines.append(
                            f"· 等级 Lv.{state.level()}/{thresholds['level']} "
                            f"{progress_bar(components['level'])}"
                        )
                        lines.append(
                            f"· 特殊奇遇 {special_count}/{thresholds['special_events']} "
                            f"{progress_bar(components['special_events'])}"
                        )
                        lines.append(
                            f"· 解锁总进度 {int(round(ultimate['overall'] * 100))}%"
                        )
        return lines

    # =========================
    # 对话后更新
    # =========================

    @filter.after_message_sent(priority=85)
    async def capture_reply(self, event: AstrMessageEvent) -> None:
        if not bool(self.config.get("enabled", True)):
            return
        if not event.get_group_id():
            return
        if not self._is_session_enabled(event):
            return
        result = event.get_result()
        if result is None or not result.is_llm_result():
            return
        assistant_message = "".join(
            str(component.text or "")
            for component in result.chain
            if isinstance(component, Plain)
        ).strip()
        if not assistant_message:
            return
        user_message = str(event.get_message_str() or "").strip()
        task = asyncio.create_task(
            self._update_growth(
                event=event,
                user_message=user_message,
                assistant_message=assistant_message,
            ),
            name=f"raise-growth-{int(time.time() * 1000)}",
        )
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _update_growth(
        self,
        *,
        event: AstrMessageEvent,
        user_message: str,
        assistant_message: str,
    ) -> None:
        scope = self._scope_for_event(event)
        async with self._state_lock_for_scope(scope):
            await self._update_growth_unlocked(
                event=event,
                user_message=user_message,
                assistant_message=assistant_message,
            )

    async def _update_growth_unlocked(
        self,
        *,
        event: AstrMessageEvent,
        user_message: str,
        assistant_message: str,
    ) -> None:
        state, path, scope = self._load_state(event)
        await self._ensure_initialized(event, state, path, scope)
        canonical = self._canonical_sender(scope, event)
        current_relationship = state.members.get(canonical["key"])
        if not isinstance(current_relationship, dict):
            current_relationship = {}
        botmesh_config = self._read_botmesh_config()
        source_profile = self._resolved_persona_profile(
            botmesh_config,
            bot_id=scope["bot_id"],
            group_id=scope["logical_group_id"],
        )
        target_profile = self._target_profile(
            botmesh_config,
            target={
                "member_id": canonical["key"],
                "name": canonical["name"],
                "kind": canonical["kind"],
                "target_bot_id": canonical["target_bot_id"],
            },
            group_id=scope["logical_group_id"],
        )
        relationship_setting = self._relationship_candidates(scope).get(
            canonical["key"], {}
        )
        member_initial_intimacy, member_initial_trust = self._member_initial_values(
            state,
            scope,
            canonical,
        )
        delta = await extract_growth_delta(
            self.context,
            config=self.config,
            umo=scope["umo"],
            identity=scope["identity"],
            user_message=user_message,
            assistant_message=assistant_message,
            intimacy=int(current_relationship.get("intimacy", state.intimacy) or 0),
            trust=int(current_relationship.get("trust", state.trust) or 0),
            level=state.level(),
            source_profile=source_profile,
            target_profile=target_profile,
            relationship=relationship_setting,
        )
        intimacy_delta = int(delta.get("intimacy_delta", 0) or 0)
        trust_delta = int(delta.get("trust_delta", 0) or 0)
        reason = str(delta.get("reason", "") or "").strip()
        habit_candidate = str(delta.get("habit_candidate", "") or "").strip()

        if not intimacy_delta and not trust_delta and APOLOGY_RE.search(user_message):
            intimacy_delta = 1
            reason = reason or "用户真诚道歉，关系得到修复"

        applied = apply_delta(
            state,
            intimacy_delta=intimacy_delta,
            trust_delta=trust_delta,
            reason=reason,
            source_kind="raise_growth",
            user_excerpt=user_message,
            habit_candidate=habit_candidate,
            now=time.time(),
            max_single_delta=self._cfg_int("max_single_delta", 3, 1, 10),
            attribute_min=self._cfg_int("attribute_min", 0, 0, 100),
            attribute_max=self._cfg_int("attribute_max", 100, 1, 100),
            daily_intimacy_cap=self._cfg_int("daily_intimacy_cap", 15, 0, 100),
            daily_trust_cap=self._cfg_int("daily_trust_cap", 12, 0, 100),
            habit_promote_count=self._cfg_int("habit_promote_count", 2, 1, 10),
            user_id=canonical["key"],
            user_name=canonical["name"],
            member_kind=canonical["kind"],
            target_bot_id=canonical["target_bot_id"],
            member_initial_intimacy=member_initial_intimacy,
            member_initial_trust=member_initial_trust,
        )
        save_state(path, state)
        await self._record_memory_exchange(
            scope=scope,
            state=state,
            applied=applied,
            reason=reason,
            user_message=user_message,
            assistant_message=assistant_message,
        )
        await self._maybe_trigger_event(
            event,
            state,
            path,
            scope,
            trigger_member=canonical,
            user_message=user_message,
            assistant_message=assistant_message,
        )
        logger.info(
            "[Raise] scope=%s bot=%s 好感%+d 信任%+d 经验+%d habit=%s",
            scope["scope_id"],
            scope["bot_id"],
            applied["intimacy_delta"],
            applied["trust_delta"],
            applied["exp_gained"],
            applied["habit_promoted"] or "-",
        )

    @staticmethod
    def _format_ending_message(state) -> str:
        ending = state.ending if isinstance(state.ending, dict) else {}
        label = str(ending.get("label") or "未知结局")
        outcome = str(ending.get("outcome") or "").strip()
        lines = [f"🏆 终局：{label}"]
        if outcome:
            lines.append(outcome)
        lines.append(
            f"（{state.display_name or state.bot_id} 的故事走到了这个结局）"
        )
        return "\n".join(lines)

    async def _record_ending_memory(
        self,
        *,
        scope: dict[str, Any],
        state,
    ) -> None:
        ending = state.ending if isinstance(state.ending, dict) else {}
        summary = (
            f"[终局事件] {state.display_name or scope['bot_id']}："
            f"「{ending.get('title') or '未知事件'}」→ "
            f"结局：{ending.get('label') or '未知结局'}；"
            f"{ending.get('outcome') or ''}"
        )
        try:
            integration = importlib.import_module(
                "astrbot_plugin_botmesh_memory.integration"
            )
            method = getattr(integration, "record_exchange", None)
            if callable(method):
                result = method(
                    umo=scope["umo"],
                    bot_id=scope["bot_id"],
                    logical_group_id=scope["logical_group_id"],
                    user_message="",
                    assistant_message=summary,
                    source_kind="raise_ending",
                    extract=False,
                    summarize=False,
                )
                if inspect.isawaitable(result):
                    result = await result
                if not isinstance(result, dict) or not result.get("success"):
                    logger.warning(
                        "[Raise] 终局记忆未落库：%s",
                        result.get("error", "write_not_acknowledged")
                        if isinstance(result, dict)
                        else "write_not_acknowledged",
                    )
        except (ImportError, AttributeError):
            pass
        except Exception as exc:
            logger.debug("[Raise] 写入终局记忆失败: %s", exc)

    async def _record_memory_exchange(
        self,
        *,
        scope: dict[str, Any],
        state,
        applied: dict[str, Any],
        reason: str,
        user_message: str,
        assistant_message: str,
    ) -> None:
        summary_parts = [
            f"[养成更新] {state.display_name or scope['bot_id']} Lv.{state.level()}："
            f"对 {applied.get('member_name') or applied.get('member_id') or '当前成员'} "
            f"好感{applied['intimacy_delta']:+d}→{applied.get('relationship_intimacy', state.intimacy)}，"
            f"信任{applied['trust_delta']:+d}→{applied.get('relationship_trust', state.trust)}"
        ]
        if reason:
            summary_parts.append(f"原因：{reason}")
        if applied.get("habit_promoted"):
            summary_parts.append(f"新习惯：{applied['habit_promoted']}")
        summary = "\n".join(summary_parts)
        try:
            integration = importlib.import_module(
                "astrbot_plugin_botmesh_memory.integration"
            )
            method = getattr(integration, "record_exchange", None)
            if callable(method):
                result = method(
                    umo=scope["umo"],
                    bot_id=scope["bot_id"],
                    logical_group_id=scope["logical_group_id"],
                    user_message=user_message,
                    assistant_message=summary,
                    source_kind="raise_growth",
                    extract=False,
                    summarize=False,
                )
                if inspect.isawaitable(result):
                    result = await result
                if not isinstance(result, dict) or not result.get("success"):
                    logger.warning(
                        "[Raise] 养成记忆未落库：%s",
                        result.get("error", "write_not_acknowledged")
                        if isinstance(result, dict)
                        else "write_not_acknowledged",
                    )
        except (ImportError, AttributeError):
            pass
        except Exception as exc:
            logger.debug("[Raise] 写入 BotMesh 记忆失败: %s", exc)

    # =========================
    # 奇遇事件
    # =========================

    def _pending_path(self, scope: dict[str, Any]) -> Path:
        identity_key = scope["memory_key"] or scope["bot_id"] or "unknown"
        material = f"{scope['scope_id']}|{identity_key}|pending"
        digest = hashlib.sha256(material.encode("utf-8")).hexdigest()[:24]
        return self.data_dir / "pending" / f"{digest}.json"

    def _load_pending(self, scope: dict[str, Any]) -> dict[str, Any] | None:
        path = self._pending_path(scope)
        if not path.is_file():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return None
        return payload if isinstance(payload, dict) else None

    def _save_pending(self, scope: dict[str, Any], payload: dict[str, Any]) -> None:
        path = self._pending_path(scope)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        tmp.replace(path)

    def _clear_pending(self, scope: dict[str, Any]) -> None:
        path = self._pending_path(scope)
        try:
            if path.exists():
                path.unlink()
        except OSError:
            pass

    @staticmethod
    def _format_event_message(
        template: dict[str, Any],
        target_member: dict[str, Any] | None = None,
    ) -> str:
        lines = [
            f"✨ 奇遇 · {template.get('title') or '未知事件'}",
            str(template.get("story") or ""),
            "",
            "请选择（回复 A / B / C）：",
        ]
        for option in template.get("options", []):
            if isinstance(option, dict):
                lines.append(f"{option.get('key')}. {option.get('text')}")
        if isinstance(target_member, dict) and target_member.get("member_id"):
            lines.append(
                ""
                + f"（这条奇遇指定由 {target_member.get('name') or target_member.get('member_id')} 回应）"
            )
        return "\n".join(lines)

    @staticmethod
    def _format_event_outcome(
        template: dict[str, Any],
        option: dict[str, Any],
        applied: dict[str, Any],
    ) -> str:
        parts = []
        d_intimacy = int(applied.get("intimacy_delta", 0) or 0)
        d_trust = int(applied.get("trust_delta", 0) or 0)
        exp_gained = int(applied.get("exp_gained", 0) or 0)
        if d_intimacy:
            parts.append(f"好感{d_intimacy:+d}")
        if d_trust:
            parts.append(f"信任{d_trust:+d}")
        if exp_gained:
            parts.append(f"经验+{exp_gained}")
        label = "，".join(parts) if parts else "关系没有明显变化"
        outcome = str(option.get("outcome") or "").strip()
        return f"✨ 奇遇结果：{outcome}\n（{label}）"

    async def _send_plain(self, event: AstrMessageEvent, text: str) -> None:
        await event.send(event.chain_result([Plain(str(text or ""))]))

    async def _localize_event_story(
        self,
        scope: dict[str, Any],
        state,
        template: dict[str, Any],
        *,
        conversation_context: str = "",
        trigger_hint: str = "",
        relationship: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if not self._cfg_bool("event_llm_story", True):
            return template
        try:
            relation = relationship if isinstance(relationship, dict) else {}
            state_summary = (
                f"等级Lv.{state.level()}，"
                f"好感{int(relation.get('intimacy', state.intimacy) or 0)}，"
                f"信任{int(relation.get('trust', state.trust) or 0)}"
            )
            result = await infer_event_story(
                self.context,
                config=self.config,
                umo=scope["umo"],
                identity=scope.get("identity") or {},
                state_summary=state_summary,
                template=template,
                conversation_context=conversation_context,
                trigger_hint=trigger_hint,
            )
        except Exception as exc:
            logger.debug("[Raise] 奇遇剧情润色失败，使用模板原文: %s", exc)
            return template
        if not isinstance(result, dict):
            return template
        return merge_event_story_text(template, result)

    async def _contextual_event_template(
        self,
        scope: dict[str, Any],
        state,
        *,
        conversation_context: str = "",
        trigger_hint: str = "",
        relationship: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """Build an ordinary event from chat context with a safe fallback.

        Special events keep their trusted mechanics and only receive prose
        localization. Ordinary events may be generated from the current chat,
        but the generated payload is normalized before it can enter state.
        """
        relation = relationship if isinstance(relationship, dict) else {}
        fallback = choose_event(
            load_event_templates(self.data_dir),
            state,
            relation or None,
        )
        if fallback is None:
            return None
        if fallback.get("special") or not str(conversation_context or "").strip():
            return await self._localize_event_story(
                scope,
                state,
                fallback,
                conversation_context=conversation_context,
                trigger_hint=trigger_hint,
                relationship=relation or None,
            )
        if not self._cfg_bool("event_llm_story", True):
            return fallback

        state_summary = (
            f"等级Lv.{state.level()}，"
            f"好感{int(relation.get('intimacy', state.intimacy) or 0)}，"
            f"信任{int(relation.get('trust', state.trust) or 0)}"
        )
        try:
            generated = await generate_context_event(
                self.context,
                config=self.config,
                umo=scope["umo"],
                identity=scope.get("identity") or {},
                state_summary=state_summary,
                conversation_context=conversation_context,
                trigger_hint=trigger_hint,
            )
            contextual = normalize_context_event(
                generated,
                event_id=f"context_{int(time.time())}_{uuid.uuid4().hex[:8]}",
                max_single_delta=self._cfg_int("max_single_delta", 3, 1, 10),
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.debug("[Raise] 上下文奇遇生成失败，回退事件模板: %s", exc)
            contextual = None
        if contextual is not None:
            logger.info(
                "[Raise] 已根据近期对话生成奇遇 scope=%s event=%s",
                scope["scope_id"],
                contextual["id"],
            )
            return contextual
        logger.info("[Raise] 上下文不足或生成结果无效，回退事件模板 scope=%s", scope["scope_id"])
        return await self._localize_event_story(
            scope,
            state,
            fallback,
            conversation_context=conversation_context,
            trigger_hint=trigger_hint,
            relationship=relation or None,
        )

    async def _recent_conversation(
        self,
        scope: dict[str, Any],
        *,
        user_message: str = "",
        assistant_message: str = "",
        limit: int = 8,
    ) -> tuple[str, bool]:
        """读取最近群聊记录作为事件上下文，返回 (文本, 历史接口是否可用)。"""
        try:
            limit = max(1, min(int(limit), 40))
        except (TypeError, ValueError):
            limit = 8
        lines: list[str] = []
        history_ok = False
        try:
            integration = importlib.import_module(
                "astrbot_plugin_chat_history_context.integration"
            )
            method = getattr(integration, "query_history", None)
            if callable(method):
                now = time.time()
                records = await method(
                    umo=scope["umo"],
                    logical_group_id=scope["logical_group_id"],
                    start_ts=now - 6 * 3600,
                    end_ts=now,
                    limit=limit,
                )
                history_ok = True
                for item in records[-limit:]:
                    if not isinstance(item, dict):
                        continue
                    content = str(item.get("content") or "").strip()
                    if not content:
                        continue
                    name = str(
                        item.get("sender_name")
                        or item.get("canonical_sender_id")
                        or item.get("sender_id")
                        or "成员"
                    ).strip()
                    lines.append(f"{name}: {content[:200]}")
        except Exception as exc:
            logger.debug("[Raise] 读取近期对话失败: %s", exc)
        if user_message.strip():
            lines.append(f"触发者: {user_message.strip()[:200]}")
        if assistant_message.strip():
            lines.append(
                f"{scope.get('display_name') or scope.get('bot_id') or 'Bot'}: "
                f"{assistant_message.strip()[:200]}"
            )
        text = "\n".join(lines[-limit:]).strip()
        return text[:2500], history_ok

    async def _judge_event_trigger(
        self,
        scope: dict[str, Any],
        state,
        conversation_context: str,
        relationship: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """调用 LLM 判定当前对话是否适合插入奇遇，失败时返回全 False 兜底。"""
        try:
            relation = relationship if isinstance(relationship, dict) else {}
            judgment = await judge_event_trigger(
                self.context,
                config=self.config,
                umo=scope["umo"],
                identity=scope.get("identity") or {},
                state_summary=(
                    f"等级Lv.{state.level()}，"
                    f"好感{int(relation.get('intimacy', state.intimacy) or 0)}，"
                    f"信任{int(relation.get('trust', state.trust) or 0)}"
                ),
                conversation_context=conversation_context,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.debug("[Raise] 奇遇触发判定失败: %s", exc)
            judgment = {
                "trigger": False,
                "score": 0.0,
                "reason": "",
                "hint": "",
            }
        try:
            score = float(judgment.get("score", 0.0) or 0.0)
        except (TypeError, ValueError):
            score = 0.0
        score = max(0.0, min(1.0, score))
        return {
            "trigger": bool(judgment.get("trigger", False)),
            "score": score,
            "reason": str(judgment.get("reason", "") or "").strip(),
            "hint": str(judgment.get("hint", "") or "").strip(),
        }

    def _event_target_member(
        self,
        state,
        trigger_member: dict[str, Any] | None,
    ) -> dict[str, Any] | None:
        mode = str(
            self.config.get("event_target_mode", "trigger") or "trigger"
        ).strip()
        if mode == "anyone":
            return None
        if mode == "best_relation":
            candidates = [
                member
                for member in state.members.values()
                if isinstance(member, dict)
            ]
            if not candidates:
                return self._as_member_payload(trigger_member)
            best = max(
                candidates,
                key=lambda member: int(member.get("intimacy", 0) or 0)
                + int(member.get("trust", 0) or 0),
            )
            return dict(best)
        if mode == "trigger" and trigger_member is None:
            candidates = [
                member
                for member in state.members.values()
                if isinstance(member, dict)
            ]
            if candidates:
                return dict(
                    max(
                        candidates,
                        key=lambda member: int(member.get("intimacy", 0) or 0)
                        + int(member.get("trust", 0) or 0),
                    )
                )
        return self._as_member_payload(trigger_member)

    @staticmethod
    def _as_member_payload(
        canonical: dict[str, Any] | None,
    ) -> dict[str, Any] | None:
        if not isinstance(canonical, dict):
            return None
        member_id = str(
            canonical.get("member_id")
            or canonical.get("key")
            or ""
        ).strip()
        if not member_id:
            return None
        return {
            "member_id": member_id,
            "name": str(canonical.get("name") or "").strip(),
            "kind": str(canonical.get("kind") or "user"),
            "target_bot_id": str(canonical.get("target_bot_id") or "").strip(),
        }

    async def _maybe_trigger_event(
        self,
        event: AstrMessageEvent,
        state,
        path: Path,
        scope: dict[str, Any],
        trigger_member: dict[str, Any] | None = None,
        *,
        user_message: str = "",
        assistant_message: str = "",
    ) -> None:
        if self._load_pending(scope) is not None:
            return
        now = time.time()
        context_limit = self._cfg_int("event_context_messages", 8, 1, 40)
        conversation_context = ""
        trigger_hint = ""
        target_member = self._event_target_member(state, trigger_member)
        target_id = str(
            (target_member or {}).get("member_id")
            or (trigger_member or {}).get("key")
            or ""
        ).strip()
        target_relationship = state.members.get(target_id)
        if not isinstance(target_relationship, dict):
            target_relationship = {}
        if (
            self._cfg_bool("ultimate_enabled", True)
            and not (
                isinstance(state.ending, dict) and state.ending.get("ending_id")
            )
            and self._cfg_bool("ultimate_auto_trigger", True)
        ):
            finale = self._ultimate_template()
            if finale is not None and is_ultimate_unlocked(
                finale, state, target_relationship or None
            ):
                conversation_context, _history_ok = await self._recent_conversation(
                    scope,
                    user_message=user_message,
                    assistant_message=assistant_message,
                    limit=context_limit,
                )
                template = await self._localize_event_story(
                    scope,
                    state,
                    finale,
                    conversation_context=conversation_context,
                    relationship=target_relationship or None,
                )
                expire_minutes = self._cfg_int("event_expire_minutes", 30, 5, 1440)
                pending = {
                    "id": str(template.get("id") or ""),
                    "template_id": str(template.get("id") or ""),
                    "title": str(template.get("title") or "未知事件"),
                    "story": str(template.get("story") or ""),
                    "options": template.get("options", []),
                    "special": bool(template.get("special")),
                    "ultimate": bool(template.get("ultimate")),
                    "target_member": target_member,
                    "conversation_context": conversation_context,
                    "created_at": now,
                    "expires_at": now + expire_minutes * 60,
                }
                self._save_pending(scope, pending)
                await self._send_plain(
                    event,
                    self._format_event_message(template, target_member),
                )
                logger.info(
                    "[Raise] 终极事件触发 scope=%s event=%s",
                    scope["scope_id"],
                    pending["template_id"],
                )
                return
        if not bool(self.config.get("event_enabled", True)):
            return
        min_interval = (
            self._cfg_int("event_min_interval_hours", 24, 0, 24 * 30) * 3600
        )
        if state.last_event_at and (now - state.last_event_at) < min_interval:
            return
        max_per_day = self._cfg_int("event_max_per_day", 1, 1, 10)
        if events_today(state, now) >= max_per_day:
            return
        try:
            chance = float(self.config.get("event_trigger_chance", 0.02) or 0.02)
        except (TypeError, ValueError):
            chance = 0.02
        chance = max(0.0, min(1.0, chance))
        mode = str(
            self.config.get("event_trigger_mode", "hybrid") or "hybrid"
        ).strip()
        if mode not in {"random", "context", "hybrid"}:
            mode = "hybrid"
        trigger = False
        min_random_score = 0.4
        try:
            min_random_score = float(
                self.config.get("event_random_min_score", 0.4) or 0.4
            )
        except (TypeError, ValueError):
            min_random_score = 0.4
        min_random_score = max(0.0, min(1.0, min_random_score))
        threshold = 0.75
        try:
            threshold = float(
                self.config.get("event_context_threshold", 0.75) or 0.75
            )
        except (TypeError, ValueError):
            threshold = 0.75
        threshold = max(0.0, min(1.0, threshold))
        random_hit = random.random() <= chance
        if mode in {"context", "hybrid"}:
            conversation_context, history_ok = await self._recent_conversation(
                scope,
                user_message=user_message,
                assistant_message=assistant_message,
                limit=context_limit,
            )
            judgment = await self._judge_event_trigger(
                scope,
                state,
                conversation_context,
                target_relationship or None,
            )
            score = float(judgment.get("score", 0.0) or 0.0)
            strong = bool(judgment.get("trigger", False)) or score >= threshold
            trigger_hint = str(judgment.get("hint", "") or "").strip()
            judge_reason = str(judgment.get("reason", "") or "").strip()
            context_available = bool(conversation_context) or history_ok
            if mode == "context":
                # 历史接口不可用且没有上下文时退化为随机，避免功能停摆。
                trigger = strong if context_available else random_hit
            else:
                # hybrid：强节点直接触发；随机兜底必须满足最低相关性，
                # 避免与上下文无关的突兀奇遇。
                trigger = strong or (
                    random_hit
                    and (not context_available or score >= min_random_score)
                )
            logger.info(
                "[Raise] 奇遇触发判定 scope=%s mode=%s score=%.2f strong=%s "
                "random_hit=%s reason=%s",
                scope["scope_id"],
                mode,
                score,
                strong,
                random_hit,
                judge_reason or "-",
            )
        else:
            # random：先掷骰子，命中后再用上下文相关性门槛过滤。
            trigger = random_hit
            if trigger:
                conversation_context, history_ok = await self._recent_conversation(
                    scope,
                    user_message=user_message,
                    assistant_message=assistant_message,
                    limit=context_limit,
                )
                if conversation_context or history_ok:
                    judgment = await self._judge_event_trigger(
                        scope,
                        state,
                        conversation_context,
                        target_relationship or None,
                    )
                    score = float(judgment.get("score", 0.0) or 0.0)
                    trigger_hint = str(
                        judgment.get("hint", "") or ""
                    ).strip()
                    judge_reason = str(
                        judgment.get("reason", "") or ""
                    ).strip()
                    if score < min_random_score:
                        trigger = False
                        logger.info(
                            "[Raise] 随机奇遇被相关性门槛拦截 scope=%s score=%.2f "
                            "min=%.2f reason=%s",
                            scope["scope_id"],
                            score,
                            min_random_score,
                            judge_reason or "-",
                        )
        if not trigger:
            return
        template = await self._contextual_event_template(
            scope,
            state,
            conversation_context=conversation_context,
            trigger_hint=trigger_hint,
            relationship=target_relationship or None,
        )
        if template is None:
            return
        expire_minutes = self._cfg_int("event_expire_minutes", 30, 5, 1440)
        pending = {
            "id": str(template.get("id") or ""),
            "template_id": str(template.get("id") or ""),
            "title": str(template.get("title") or "未知事件"),
            "story": str(template.get("story") or ""),
            "options": template.get("options", []),
            "special": bool(template.get("special")),
            "ultimate": bool(template.get("ultimate")),
            "target_member": target_member,
            "conversation_context": conversation_context,
            "created_at": now,
            "expires_at": now + expire_minutes * 60,
        }
        self._save_pending(scope, pending)
        await self._send_plain(
            event,
            self._format_event_message(template, target_member),
        )
        logger.info(
            "[Raise] 触发奇遇 scope=%s event=%s",
            scope["scope_id"],
            pending["template_id"],
        )

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE, priority=150)
    async def on_group_message_for_event(self, event: AstrMessageEvent) -> None:
        if not event.get_group_id():
            return
        scope = self._scope_for_event(event)
        async with self._state_lock_for_scope(scope):
            await self._on_group_message_for_event_unlocked(event)

    async def _on_group_message_for_event_unlocked(
        self, event: AstrMessageEvent
    ) -> None:
        if not bool(self.config.get("event_enabled", True)) and not bool(
            self.config.get("ultimate_enabled", True)
        ):
            return
        if not event.get_group_id():
            return
        if not self._is_session_enabled(event):
            return
        scope = self._scope_for_event(event)
        pending = self._load_pending(scope)
        if pending is None:
            return
        now = time.time()
        if now > float(pending.get("expires_at", 0) or 0):
            self._clear_pending(scope)
            return
        option = match_event_choice(
            str(event.get_message_str() or ""),
            pending.get("options", []),
        )
        if option is None:
            return
        target_member = pending.get("target_member")
        if isinstance(target_member, dict) and target_member.get("member_id"):
            canonical = self._canonical_sender(scope, event)
            if canonical["key"] != str(target_member.get("member_id") or ""):
                return
        state, path, scope = self._load_state(event)
        await self._ensure_initialized(event, state, path, scope)
        canonical = self._canonical_sender(scope, event)
        member_initial_intimacy, member_initial_trust = self._member_initial_values(
            state,
            scope,
            canonical,
        )
        applied = apply_event(
            state,
            pending,
            option,
            now=now,
            max_single_delta=self._cfg_int("max_single_delta", 3, 1, 10),
            attribute_min=self._cfg_int("attribute_min", 0, 0, 100),
            attribute_max=self._cfg_int("attribute_max", 100, 1, 100),
            daily_intimacy_cap=self._cfg_int("daily_intimacy_cap", 15, 0, 100),
            daily_trust_cap=self._cfg_int("daily_trust_cap", 12, 0, 100),
            habit_promote_count=self._cfg_int("habit_promote_count", 2, 1, 10),
            user_id=canonical["key"],
            user_name=canonical["name"],
            member_kind=canonical["kind"],
            target_bot_id=canonical["target_bot_id"],
            member_initial_intimacy=member_initial_intimacy,
            member_initial_trust=member_initial_trust,
        )
        member = state.members.get(canonical["key"])
        if not isinstance(member, dict):
            member = {}
        save_state(path, state)
        if (
            isinstance(state.ending, dict)
            and state.ending.get("ending_id")
            and not state.ending.get("announced")
        ):
            state.ending["announced"] = True
            save_state(path, state)
            await self._record_ending_memory(scope=scope, state=state)
            await self._send_plain(event, self._format_ending_message(state))
        self._clear_pending(scope)
        await self._record_event_memory(
            scope=scope,
            state=state,
            template=pending,
            option=option,
            applied=applied,
        )
        message = self._format_event_outcome(pending, option, applied)
        if self._cfg_bool("event_llm_narrative", True):
            try:
                narrative = await infer_event_outcome(
                    self.context,
                    config=self.config,
                    umo=scope["umo"],
                    identity=scope.get("identity") or {},
                    state_summary=(
                        f"等级Lv.{state.level()}，"
                        f"好感{int(member.get('intimacy', state.intimacy) or 0)}，"
                        f"信任{int(member.get('trust', state.trust) or 0)}"
                    ),
                    template=pending,
                    option=option,
                    conversation_context=str(
                        pending.get("conversation_context") or ""
                    ),
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.debug("[Raise] 奇遇结局续写失败，使用静态结果: %s", exc)
                narrative = ""
            if narrative:
                message = narrative
        await self._send_plain(event, message)
        event.should_call_llm(False)
        event.stop_event()
        logger.info(
            "[Raise] 奇遇结算 scope=%s event=%s option=%s 好感%+d 信任%+d",
            scope["scope_id"],
            pending.get("template_id", ""),
            option.get("key", ""),
            applied["intimacy_delta"],
            applied["trust_delta"],
        )

    async def _record_event_memory(
        self,
        *,
        scope: dict[str, Any],
        state,
        template: dict[str, Any],
        option: dict[str, Any],
        applied: dict[str, Any],
    ) -> None:
        summary = (
            f"[奇遇事件] {state.display_name or scope['bot_id']}："
            f"「{template.get('title') or '未知事件'}」选择"
            f"{option.get('key')}（{option.get('text')}）；"
            f"好感{applied['intimacy_delta']:+d}，"
            f"信任{applied['trust_delta']:+d}，经验+{applied['exp_gained']}"
        )
        try:
            integration = importlib.import_module(
                "astrbot_plugin_botmesh_memory.integration"
            )
            method = getattr(integration, "record_exchange", None)
            if callable(method):
                result = method(
                    umo=scope["umo"],
                    bot_id=scope["bot_id"],
                    logical_group_id=scope["logical_group_id"],
                    user_message=str(option.get("text") or ""),
                    assistant_message=summary,
                    source_kind="raise_event",
                    extract=False,
                    summarize=False,
                )
                if inspect.isawaitable(result):
                    result = await result
                if not isinstance(result, dict) or not result.get("success"):
                    logger.warning(
                        "[Raise] 奇遇记忆未落库：%s",
                        result.get("error", "write_not_acknowledged")
                        if isinstance(result, dict)
                        else "write_not_acknowledged",
                    )
        except (ImportError, AttributeError):
            pass
        except Exception as exc:
            logger.debug("[Raise] 写入奇遇记忆失败: %s", exc)

    # =========================
    # 配置辅助
    # =========================

    def _cfg_int(self, key: str, default: int, minimum: int, maximum: int) -> int:
        try:
            value = int(self.config.get(key, default))
        except (TypeError, ValueError):
            value = default
        return max(minimum, min(maximum, value))

    def _cfg_bool(self, key: str, default: bool) -> bool:
        value = self.config.get(key, default)
        if isinstance(value, str):
            return value.strip().casefold() in {"1", "true", "yes", "on", "是"}
        return bool(value)

    @staticmethod
    def _text(value: Any) -> str:
        return str(value or "").strip()

    def _ultimate_template(self) -> dict[str, Any] | None:
        templates = load_event_templates(self.data_dir)
        finals = [
            template
            for template in templates
            if isinstance(template, dict) and template.get("ultimate")
        ]
        if not finals:
            return None
        configured_id = str(
            self.config.get("ultimate_event_id", "") or ""
        ).strip()
        if configured_id:
            for template in finals:
                if str(template.get("id") or "") == configured_id:
                    return template
        return finals[0]

    def _is_session_enabled(self, event: AstrMessageEvent) -> bool:
        mode = str(self.config.get("session_list_mode", "whitelist") or "whitelist").strip()
        if mode == "none":
            return True
        targets = self._session_targets(event)
        if not targets:
            return mode != "whitelist"
        entries = {
            str(item or "").casefold().strip()
            for item in (self.config.get("session_list", []) or [])
            if str(item or "").strip()
        }
        matched = bool(entries & targets)
        return matched if mode == "whitelist" else not matched

    @staticmethod
    def _session_targets(event: AstrMessageEvent) -> set[str]:
        targets: set[str] = set()
        umo = str(event.unified_msg_origin or "").strip()
        if umo:
            targets.add(umo.casefold())
        group_id = ""
        try:
            group_id = str(event.get_group_id() or "").strip()
        except Exception:
            group_id = ""
        if group_id:
            targets.add(group_id.casefold())
        platform = ""
        for getter_name in ("get_platform_id", "get_platform_name"):
            getter = getattr(event, getter_name, None)
            if not callable(getter):
                continue
            try:
                value = str(getter() or "").strip()
            except Exception:
                value = ""
            if value:
                platform = value
                break
        if platform and group_id:
            targets.add(f"{platform}:{group_id}".casefold())
            targets.add(f"{platform}/{group_id}".casefold())
        if umo and ":" in umo:
            parts = umo.split(":", 2)
            if len(parts) == 3 and parts[0] and parts[2]:
                targets.add(f"{parts[0]}:{parts[2]}".casefold())
                targets.add(f"{parts[0]}/{parts[2]}".casefold())
        return targets

    def _session_selector(self, event: AstrMessageEvent) -> str:
        umo = str(event.unified_msg_origin or "").strip()
        if umo:
            return umo
        group_id = ""
        try:
            group_id = str(event.get_group_id() or "").strip()
        except Exception:
            group_id = ""
        return group_id or umo

    # =========================
    # 命令
    # =========================

    @filter.command_group("raise")
    def raise_group(self):
        """养成系统：查看与维护当前角色的成长状态。"""
        pass

    @raise_group.command("status")
    async def raise_status(self, event: AstrMessageEvent):
        if not event.get_group_id():
            yield event.plain_result("该命令只能在群聊中使用。")
            return
        if not self._is_session_enabled(event):
            yield event.plain_result("当前群未开启养成系统，可用 /raise enable 开启。")
            return
        state, _path, scope = self._load_state(event)
        await self._ensure_initialized(event, state, _path, scope)
        canonical = self._canonical_sender(scope, event)
        member = state.members.get(canonical["key"])
        relationship_name = (
            str(member.get("name") or canonical["name"] or canonical["key"])
            if isinstance(member, dict)
            else str(canonical["name"] or canonical["key"])
        )
        lines = [
            f"🌱 养成状态 · {state.display_name or scope['bot_id']}",
            f"等级 Lv.{state.level()}（经验 {state.exp}/{state.next_exp()}）",
            f"当前关系：{state.display_name or scope['bot_id']} → {relationship_name}",
            f"💗 好感 {int(member.get('intimacy', 0) or 0) if isinstance(member, dict) else state.intimacy}/100",
            f"🤝 信任 {int(member.get('trust', 0) or 0) if isinstance(member, dict) else state.trust}/100",
            f"群内关系概况（成员均值）：好感 {state.intimacy}，信任 {state.trust}",
        ]
        if state.habits:
            lines.append(f"📖 已养成习惯：{'、'.join(state.habits)}")
        if state.members:
            lines.append(f"👥 已建立关系的成员：{len(state.members)}（/raise members 查看）")
        relationship_events = (
            list(member.get("events", []) or [])
            if isinstance(member, dict)
            else state.events
        )
        recent = [
            item
            for item in relationship_events[-10:]
            if int(item.get("intimacy_delta", 0) or 0)
            or int(item.get("trust_delta", 0) or 0)
            or item.get("habit_promoted")
        ][-5:]
        if recent:
            lines.append("")
            lines.append("🕐 最近成长：")
            for item in reversed(recent):
                stamp = time.strftime(
                    "%m-%d %H:%M",
                    time.localtime(float(item.get("at", 0) or 0)),
                )
                parts = []
                d_intimacy = int(item.get("intimacy_delta", 0) or 0)
                d_trust = int(item.get("trust_delta", 0) or 0)
                if d_intimacy:
                    parts.append(f"好感{d_intimacy:+d}")
                if d_trust:
                    parts.append(f"信任{d_trust:+d}")
                promoted = str(item.get("habit_promoted", "") or "").strip()
                label = "、".join(parts) or (f"新习惯：{promoted}" if promoted else "成长")
                reason = str(item.get("reason", "") or "").strip()
                lines.append(f"· {stamp} {label}" + (f"：{reason}" if reason else ""))
        else:
            lines.append("")
            lines.append("还没有成长记录，多和角色互动试试。")
        lines.append("")
        lines.append("🎯 下一目标：")
        lines.extend(self._build_goals_lines(state, member if isinstance(member, dict) else None))
        lines.append("查看详细目标：/raise goals")
        yield event.plain_result("\n".join(lines))

    @raise_group.command("goals")
    async def raise_goals(self, event: AstrMessageEvent):
        if not event.get_group_id():
            yield event.plain_result("该命令只能在群聊中使用。")
            return
        if not self._is_session_enabled(event):
            yield event.plain_result("当前群未开启养成系统，可用 /raise enable 开启。")
            return
        state, _path, scope = self._load_state(event)
        await self._ensure_initialized(event, state, _path, scope)
        canonical = self._canonical_sender(scope, event)
        member = state.members.get(canonical["key"])
        target_name = (
            member.get("name") if isinstance(member, dict) else canonical.get("name")
        ) or canonical["key"]
        lines = [
            f"🎯 养成目标 · {state.display_name or scope['bot_id']} → {target_name}"
        ]
        lines.extend(self._build_goals_lines(state, member if isinstance(member, dict) else None))
        yield event.plain_result("\n".join(lines))

    @raise_group.command("members")
    async def raise_members(self, event: AstrMessageEvent):
        if not event.get_group_id():
            yield event.plain_result("该命令只能在群聊中使用。")
            return
        if not self._is_session_enabled(event):
            yield event.plain_result("当前群未开启养成系统，可用 /raise enable 开启。")
            return
        state, _path, scope = self._load_state(event)
        await self._ensure_initialized(event, state, _path, scope)
        if not state.members:
            yield event.plain_result(
                "还没有成员关系；首次出现时会按当前人格、世界观和定向关系生成。"
            )
            return
        lines = ["👥 群内独立关系（当前角色 → 成员）："]
        for key, member in sorted(
            state.members.items(),
            key=lambda item: -(
                int(item[1].get("intimacy", 0) or 0)
                + int(item[1].get("trust", 0) or 0)
            ),
        ):
            kind_label = "Bot" if member.get("kind") == "bot" else "成员"
            name = member.get("name") or key
            lines.append(
                f"· {name}（{kind_label}）：好感 {member.get('intimacy', 0)}，"
                f"信任 {member.get('trust', 0)}"
                + (
                    f"\n  基线依据：{member.get('baseline_reason')}"
                    if member.get("baseline_reason")
                    else ""
                )
            )
        yield event.plain_result("\n".join(lines))

    @raise_group.command("event")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def raise_event(self, event: AstrMessageEvent):
        """手动触发一次奇遇事件（管理员，用于测试/调节奏）。"""
        if not event.get_group_id():
            yield event.plain_result("该命令只能在群聊中使用。")
            return
        if not self._is_session_enabled(event):
            yield event.plain_result("当前群未开启养成系统，可用 /raise enable 开启。")
            return
        if not bool(self.config.get("event_enabled", True)):
            yield event.plain_result("奇遇事件已关闭（event_enabled=false）。")
            return
        state, _path, scope = self._load_state(event)
        if self._load_pending(scope) is not None:
            yield event.plain_result("当前已有进行中的奇遇事件，先完成选择或等待过期。")
            return
        await self._ensure_initialized(event, state, _path, scope)
        canonical = self._canonical_sender(scope, event)
        target_member = self._event_target_member(state, canonical)
        relationship = state.members.get(canonical["key"])
        if not isinstance(relationship, dict):
            relationship = {}
        template = None
        if (
            self._cfg_bool("ultimate_enabled", True)
            and not (
                isinstance(state.ending, dict) and state.ending.get("ending_id")
            )
        ):
            finale = self._ultimate_template()
            if finale is not None and is_ultimate_unlocked(
                finale, state, relationship or None
            ):
                template = finale
        context_limit = self._cfg_int("event_context_messages", 8, 1, 40)
        conversation_context, _history_ok = await self._recent_conversation(
            scope,
            limit=context_limit,
        )
        if template is not None:
            template = await self._localize_event_story(
                scope,
                state,
                template,
                conversation_context=conversation_context,
                relationship=relationship or None,
            )
        else:
            template = await self._contextual_event_template(
                scope,
                state,
                conversation_context=conversation_context,
                relationship=relationship or None,
            )
        if template is None:
            yield event.plain_result("当前没有符合条件的事件模板。")
            return
        now = time.time()
        expire_minutes = self._cfg_int("event_expire_minutes", 30, 5, 1440)
        pending = {
            "id": str(template.get("id") or ""),
            "template_id": str(template.get("id") or ""),
            "title": str(template.get("title") or "未知事件"),
            "story": str(template.get("story") or ""),
            "options": template.get("options", []),
            "special": bool(template.get("special")),
            "ultimate": bool(template.get("ultimate")),
            "target_member": target_member,
            "conversation_context": conversation_context,
            "created_at": now,
            "expires_at": now + expire_minutes * 60,
        }
        self._save_pending(scope, pending)
        yield event.plain_result(
            self._format_event_message(template, target_member)
            + "\n\n（管理员手动触发，等待群员回复选项）"
        )

    @raise_group.command("log")
    async def raise_log(self, event: AstrMessageEvent):
        if not event.get_group_id():
            yield event.plain_result("该命令只能在群聊中使用。")
            return
        if not self._is_session_enabled(event):
            yield event.plain_result("当前群未开启养成系统，可用 /raise enable 开启。")
            return
        state, _path, _scope = self._load_state(event)
        await self._ensure_initialized(event, state, _path, _scope)
        if not state.events:
            yield event.plain_result("还没有成长日志。")
            return
        lines = ["📜 养成日志（最近 20 条）："]
        for item in reversed(state.events[-20:]):
            stamp = time.strftime(
                "%m-%d %H:%M",
                time.localtime(float(item.get("at", 0) or 0)),
            )
            parts = []
            d_intimacy = int(item.get("intimacy_delta", 0) or 0)
            d_trust = int(item.get("trust_delta", 0) or 0)
            if d_intimacy:
                parts.append(f"好感{d_intimacy:+d}")
            if d_trust:
                parts.append(f"信任{d_trust:+d}")
            promoted = str(item.get("habit_promoted", "") or "").strip()
            label = "、".join(parts) or (f"新习惯：{promoted}" if promoted else "无变化")
            reason = str(item.get("reason", "") or "").strip()
            lines.append(f"· {stamp} {label}" + (f"：{reason}" if reason else ""))
        yield event.plain_result("\n".join(lines))

    @raise_group.command("correct")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def raise_correct(self, event: AstrMessageEvent, args: str = ""):
        """管理员修正数值，如 /raise correct 好感=70 信任=55"""
        if not self._is_session_enabled(event):
            yield event.plain_result("当前群未开启养成系统。")
            return
        state, path, scope = self._load_state(event)
        await self._ensure_initialized(event, state, path, scope)
        canonical = self._canonical_sender(scope, event)
        member = state.members.get(canonical["key"])
        if not isinstance(member, dict):
            yield event.plain_result("当前成员关系尚未建立，稍后再试。")
            return
        intimacy_value = _parse_attribute_arg(args, "好感")
        trust_value = _parse_attribute_arg(args, "信任")
        if intimacy_value is None and trust_value is None:
            yield event.plain_result(
                "用法：/raise correct 好感=70 信任=55（只填一项也可以）"
            )
            return
        if intimacy_value is not None:
            member["intimacy"] = clamp(intimacy_value, 0, 100)
        if trust_value is not None:
            member["trust"] = clamp(trust_value, 0, 100)
        member["manual_corrected_at"] = time.time()
        sync_aggregate_from_members(state)
        state.updated_at = time.time()
        save_state(path, state)
        yield event.plain_result(
            f"已修正 {state.display_name or scope['bot_id']} → "
            f"{member.get('name') or canonical['key']}："
            f"好感={member.get('intimacy', 0)}，信任={member.get('trust', 0)}"
        )

    @raise_group.command("reset")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def raise_reset(self, event: AstrMessageEvent):
        if not self._is_session_enabled(event):
            yield event.plain_result("当前群未开启养成系统。")
            return
        state, path, scope = self._load_state(event)
        if path.exists():
            backup = path.with_suffix(f".bak-{int(time.time())}")
            path.replace(backup)
        yield event.plain_result(
            f"已重置 {state.display_name or scope['bot_id']} 的养成状态。"
        )

    @raise_group.command("enable")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def raise_enable(self, event: AstrMessageEvent):
        if not event.get_group_id():
            yield event.plain_result("该命令只能在群聊中使用。")
            return
        selector = self._session_selector(event)
        entries = [
            str(item or "").strip()
            for item in (self.config.get("session_list", []) or [])
            if str(item or "").strip()
        ]
        if selector not in entries:
            entries.append(selector)
        self.config["session_list"] = entries
        self.config["session_list_mode"] = "whitelist"
        result = self.config.save_config()
        if inspect.isawaitable(result):
            await result
        yield event.plain_result(
            f"已在本群开启养成系统。\n保存标识：{selector}\n"
            "从下一轮对话开始，本群会积累养成状态并注入成长上下文。"
        )

    @raise_group.command("disable")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def raise_disable(self, event: AstrMessageEvent):
        if not event.get_group_id():
            yield event.plain_result("该命令只能在群聊中使用。")
            return
        selector = self._session_selector(event)
        entries = [
            str(item or "").strip()
            for item in (self.config.get("session_list", []) or [])
            if str(item or "").strip() and str(item).strip() != selector
        ]
        self.config["session_list"] = entries
        result = self.config.save_config()
        if inspect.isawaitable(result):
            await result
        yield event.plain_result(
            f"已从养成名单移除当前群（{selector}）。"
            "若名单已空，养成系统将不再在任何群生效。"
        )

    # =========================
    # 网页管理
    # =========================

    def _register_web_apis(self) -> None:
        register = getattr(self.context, "register_web_api", None)
        if not callable(register) or request is None:
            logger.warning("[Raise] 当前 AstrBot 不支持插件 Page")
            return
        register(
            f"/{PLUGIN_NAME}/overview",
            self.page_overview,
            ["GET"],
            "养成总览",
        )
        register(
            f"/{PLUGIN_NAME}/state",
            self.page_state,
            ["GET"],
            "养成详情",
        )
        register(
            f"/{PLUGIN_NAME}/correct",
            self.page_correct,
            ["POST"],
            "修正养成数值",
        )
        register(
            f"/{PLUGIN_NAME}/reset",
            self.page_reset,
            ["POST"],
            "重置养成状态",
        )
        register(
            f"/{PLUGIN_NAME}/trigger_event",
            self.page_trigger_event,
            ["POST"],
            "触发奇遇/终局事件",
        )
        register(
            f"/{PLUGIN_NAME}/pending",
            self.page_pending,
            ["GET"],
            "待定事件",
        )
        register(
            f"/{PLUGIN_NAME}/clear_pending",
            self.page_clear_pending,
            ["POST"],
            "清除待定事件",
        )
        register(
            f"/{PLUGIN_NAME}/events",
            self.page_events,
            ["GET"],
            "事件模板",
        )
        register(
            f"/{PLUGIN_NAME}/events_save",
            self.page_events_save,
            ["POST"],
            "保存事件模板",
        )
        register(
            f"/{PLUGIN_NAME}/events_generate",
            self.page_events_generate,
            ["POST"],
            "从概要生成事件模板",
        )
        register(
            f"/{PLUGIN_NAME}/config",
            self.page_config,
            ["GET", "POST"],
            "养成配置",
        )

    def _load_state_by_file(
        self,
        filename: str,
    ) -> tuple[GrowthState | None, Path | None, dict[str, Any] | None]:
        name = str(filename or "").strip()
        if not name or "/" in name or "\\" in name or ".." in name:
            return None, None, None
        path = self.data_dir / "growth" / name
        if not path.is_file():
            return None, None, None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return None, None, None
        if not isinstance(payload, dict):
            return None, None, None
        state = GrowthState.from_dict(
            payload,
            scope_id=str(payload.get("scope_id") or ""),
            bot_id=str(payload.get("bot_id") or ""),
            logical_group_id=str(payload.get("logical_group_id") or ""),
        )
        scope = {
            "scope_id": state.scope_id,
            "bot_id": state.bot_id,
            "logical_group_id": state.logical_group_id,
            "memory_key": state.memory_key,
            "display_name": state.display_name,
            "identity": {},
            "umo": "",
        }
        return state, path, scope

    def _bot_display_name(self, bot_id: str) -> str:
        bot_id = str(bot_id or "").strip()
        if not bot_id:
            return ""
        config = self._read_botmesh_config()
        for item in config.get("bots", []):
            if not isinstance(item, dict):
                continue
            if str(item.get("bot_id") or "") != bot_id:
                continue
            return str(
                item.get("display_name")
                or item.get("nickname")
                or item.get("account_id")
                or bot_id
            ).strip()
        labels = self._management_labels()
        bots = labels.get("bots") if isinstance(labels, dict) else {}
        if isinstance(bots, dict) and bots.get(bot_id):
            return str(bots.get(bot_id)).strip()
        return bot_id

    def _memory_key_for(
        self,
        bot_id: str,
        logical_group_id: str,
    ) -> str:
        config = self._read_botmesh_config()
        profiles = [
            item
            for item in config.get("persona_profiles", [])
            if isinstance(item, dict)
            and str(item.get("bot_id") or "") == str(bot_id or "")
        ]
        identity: dict[str, Any] = {}
        for profile in profiles:
            if not str(profile.get("group_id") or "").strip():
                identity.update(
                    {
                        key: value
                        for key, value in profile.items()
                        if value not in ("", None)
                    }
                )
        for profile in profiles:
            if str(profile.get("group_id") or "") == str(logical_group_id or ""):
                identity.update(
                    {
                        key: value
                        for key, value in profile.items()
                        if value not in ("", None)
                    }
                )
        return str(
            identity.get("memory_key")
            or identity.get("soul_identity")
            or identity.get("self_identity")
            or ""
        ).strip()

    async def _send_plain_to_group(self, scope: dict[str, Any], text: str) -> bool:
        config = self._read_botmesh_config()
        bots = {
            str(item.get("bot_id") or ""): item
            for item in config.get("bots", [])
            if isinstance(item, dict) and str(item.get("bot_id") or "").strip()
        }
        bindings = config.get("group_bindings", [])
        if not isinstance(bindings, list):
            return False
        for binding in bindings:
            if not isinstance(binding, dict):
                continue
            if str(binding.get("bot_id") or "") != scope["bot_id"]:
                continue
            if str(binding.get("group_id") or "") != scope["logical_group_id"]:
                continue
            raw_group = str(binding.get("platform_group_id") or "").strip()
            bot = bots.get(scope["bot_id"], {})
            platform = str(bot.get("platform_id") or "").strip()
            if not raw_group or not platform:
                continue
            session = SimpleNamespace(
                platform_name=platform,
                message_type="GroupMessage",
                session_id=raw_group,
            )
            try:
                sent = await self.context.send_message(
                    session,
                    MessageChain(chain=[Plain(text)]),
                )
                return bool(sent)
            except Exception as exc:
                logger.debug("[Raise] 网页触发消息发送失败: %s", exc)
                return False
        return False

    async def page_overview(self):
        labels = self._management_labels()
        config = self._read_botmesh_config()
        group_labels = labels.get("groups") if isinstance(labels, dict) else {}
        bot_labels = labels.get("bots") if isinstance(labels, dict) else {}
        bots_cfg = {
            str(item.get("bot_id") or ""): item
            for item in config.get("bots", [])
            if isinstance(item, dict) and str(item.get("bot_id") or "").strip()
        }

        def bot_name(bot_id: str) -> str:
            return str(
                bots_cfg.get(bot_id, {}).get("display_name")
                or bots_cfg.get(bot_id, {}).get("nickname")
                or (bot_labels.get(bot_id) if isinstance(bot_labels, dict) else "")
                or bot_id
            )

        groups: dict[str, dict[str, Any]] = {}
        bindings = config.get("group_bindings", [])
        if isinstance(bindings, list):
            for binding in bindings:
                if not isinstance(binding, dict):
                    continue
                gid = str(binding.get("group_id") or "").strip()
                bot_id = str(binding.get("bot_id") or "").strip()
                if not gid or not bot_id:
                    continue
                group = groups.setdefault(
                    gid,
                    {
                        "group_id": gid,
                        "label": str(
                            group_labels.get(gid) or gid
                        )
                        if isinstance(group_labels, dict)
                        else gid,
                        "bots": {},
                    },
                )
                group["bots"].setdefault(
                    bot_id,
                    {
                        "bot_id": bot_id,
                        "name": bot_name(bot_id),
                        "memory_key": self._memory_key_for(bot_id, gid),
                        "file": None,
                        "has_state": False,
                        "ending": {},
                    },
                )

        growth_dir = self.data_dir / "growth"
        if growth_dir.is_dir():
            for path in sorted(growth_dir.glob("*.json")):
                try:
                    payload = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, ValueError, TypeError):
                    continue
                if not isinstance(payload, dict):
                    continue
                exp = max(0, int(payload.get("exp", 0) or 0))
                bot_id = str(payload.get("bot_id") or "")
                gid = str(payload.get("logical_group_id") or "")
                row = {
                    "bot_id": bot_id,
                    "name": bot_name(bot_id),
                    "memory_key": str(payload.get("memory_key") or ""),
                    "file": path.name,
                    "has_state": True,
                    "intimacy": int(payload.get("intimacy", 0) or 0),
                    "trust": int(payload.get("trust", 0) or 0),
                    "exp": exp,
                    "level": level_for_exp(exp),
                    "next_exp": next_level_exp(level_for_exp(exp)),
                    "habits": [
                        item for item in payload.get("habits", []) if item
                    ],
                    "members": len(payload.get("members", {}) or {}),
                    "init_status": str(payload.get("init_status") or "done"),
                    "init_note": str(payload.get("init_note") or ""),
                    "ending": dict(payload.get("ending") or {}),
                    "event_count": int(payload.get("event_count", 0) or 0),
                    "updated_at": float(payload.get("updated_at", 0) or 0),
                }
                key = gid or "__unmapped__"
                group = groups.setdefault(
                    key,
                    {
                        "group_id": gid,
                        "label": str(
                            group_labels.get(gid) or (gid or "未映射群聊")
                        )
                        if isinstance(group_labels, dict)
                        else (gid or "未映射群聊"),
                        "bots": {},
                    },
                )
                existing = group["bots"].get(bot_id)
                if isinstance(existing, dict):
                    existing.update(row)
                    existing["name"] = existing.get("name") or bot_name(bot_id)
                else:
                    group["bots"][bot_id] = row

        result_groups = []
        for gid, group in groups.items():
            bots = sorted(
                group["bots"].values(),
                key=lambda item: str(item.get("name") or ""),
            )
            result_groups.append({**group, "bots": bots})
        result_groups.sort(key=lambda item: str(item.get("label") or ""))
        return json_response({"groups": result_groups, "labels": labels})

    async def page_state(self):
        file = self._text(_query_value(request, "file"))
        group_id = self._text(_query_value(request, "group_id"))
        bot_id = self._text(_query_value(request, "bot_id"))
        state: GrowthState | None = None
        path: Path | None = None
        scope: dict[str, Any] | None = None
        if file:
            state, path, scope = self._load_state_by_file(file)
            if state is None or path is None or scope is None:
                return error_response("找不到状态文件（file 参数无效）", status_code=400)
        else:
            if not group_id or not bot_id:
                return error_response(
                    "需要 file 或 group_id+bot_id 参数",
                    status_code=400,
                )
            labels = self._management_labels()
            group_labels = labels.get("groups") if isinstance(labels, dict) else {}
            return json_response(
                {
                    "empty": True,
                    "bot_id": bot_id,
                    "name": self._bot_display_name(bot_id),
                    "logical_group_id": group_id,
                    "group_label": str(
                        group_labels.get(group_id) or group_id
                    )
                    if isinstance(group_labels, dict)
                    else group_id,
                    "memory_key": self._memory_key_for(bot_id, group_id),
                }
            )
        if not state.display_name:
            state.display_name = self._bot_display_name(scope["bot_id"])
        pending = self._load_pending(scope)
        payload = state.to_dict()
        payload["level"] = state.level()
        payload["next_exp"] = state.next_exp()
        payload["level_base"] = cumulative_exp(state.level())
        payload["goals_lines"] = self._build_goals_lines(state)
        payload["pending"] = pending
        finale = self._ultimate_template()
        if finale is not None:
            payload["ultimate"] = {
                "event_id": str(finale.get("id") or ""),
                "title": str(finale.get("title") or ""),
                "unlock": ultimate_unlock_progress(finale, state),
            }
        else:
            payload["ultimate"] = None
        return json_response(payload)

    async def page_correct(self):
        payload = await request.json(default={})
        if not isinstance(payload, dict):
            return error_response("请求内容必须是 JSON 对象", status_code=400)
        state, path, scope = self._load_state_by_file(self._text(payload.get("file")))
        if state is None or path is None or scope is None:
            return error_response("找不到状态文件", status_code=400)
        member_id = self._text(payload.get("member_id"))
        changed = False
        if member_id:
            member = state.members.get(member_id)
            if not isinstance(member, dict):
                return error_response(f"成员 {member_id} 不存在", status_code=400)
            if payload.get("intimacy") is not None:
                member["intimacy"] = clamp(
                    int(payload["intimacy"]), 0, 100
                )
                changed = True
            if payload.get("trust") is not None:
                member["trust"] = clamp(int(payload["trust"]), 0, 100)
                changed = True
            if changed:
                member["manual_corrected_at"] = time.time()
                sync_aggregate_from_members(state)
        else:
            if payload.get("exp") is not None:
                state.exp = max(0, int(payload["exp"]))
                changed = True
        if not changed:
            return error_response("没有可修正的字段", status_code=400)
        state.updated_at = time.time()
        save_state(path, state)
        return json_response({"ok": True, "state": state.to_dict()})

    async def page_reset(self):
        payload = await request.json(default={})
        if not isinstance(payload, dict):
            return error_response("请求内容必须是 JSON 对象", status_code=400)
        state, path, scope = self._load_state_by_file(self._text(payload.get("file")))
        if state is None or path is None or scope is None:
            return error_response("找不到状态文件", status_code=400)
        if path.exists():
            backup = path.with_suffix(f".bak-{int(time.time())}")
            path.replace(backup)
        self._clear_pending(scope)
        return json_response({"ok": True, "backup": backup.name})

    async def page_trigger_event(self):
        payload = await request.json(default={})
        if not isinstance(payload, dict):
            return error_response("请求内容必须是 JSON 对象", status_code=400)
        state, path, scope = self._load_state_by_file(self._text(payload.get("file")))
        if state is None or path is None or scope is None:
            return error_response("找不到状态文件", status_code=400)
        if self._load_pending(scope) is not None:
            return error_response("当前已有进行中的事件", status_code=400)
        target_member = self._event_target_member(state, None)
        target_id = str((target_member or {}).get("member_id") or "").strip()
        relationship = state.members.get(target_id)
        if not isinstance(relationship, dict):
            relationship = {}
        template = None
        if (
            self._cfg_bool("ultimate_enabled", True)
            and not (
                isinstance(state.ending, dict) and state.ending.get("ending_id")
            )
        ):
            finale = self._ultimate_template()
            if finale is not None and is_ultimate_unlocked(
                finale, state, relationship or None
            ):
                template = finale
        context_limit = self._cfg_int("event_context_messages", 8, 1, 40)
        conversation_context, _history_ok = await self._recent_conversation(
            scope,
            limit=context_limit,
        )
        if template is not None:
            template = await self._localize_event_story(
                scope,
                state,
                template,
                conversation_context=conversation_context,
                relationship=relationship or None,
            )
        else:
            template = await self._contextual_event_template(
                scope,
                state,
                conversation_context=conversation_context,
                relationship=relationship or None,
            )
        if template is None:
            return error_response("没有符合条件的事件模板", status_code=400)
        now = time.time()
        expire_minutes = self._cfg_int("event_expire_minutes", 30, 5, 1440)
        pending = {
            "id": str(template.get("id") or ""),
            "template_id": str(template.get("id") or ""),
            "title": str(template.get("title") or "未知事件"),
            "story": str(template.get("story") or ""),
            "options": template.get("options", []),
            "special": bool(template.get("special")),
            "ultimate": bool(template.get("ultimate")),
            "target_member": target_member,
            "conversation_context": conversation_context,
            "created_at": now,
            "expires_at": now + expire_minutes * 60,
        }
        self._save_pending(scope, pending)
        message = self._format_event_message(template, target_member)
        sent = await self._send_plain_to_group(scope, message)
        return json_response(
            {
                "ok": True,
                "sent": sent,
                "template_id": pending["template_id"],
                "message": message,
            }
        )

    async def page_pending(self):
        state, _path, scope = self._load_state_by_file(
            self._text(_query_value(request, "file"))
        )
        if state is None or scope is None:
            return error_response("找不到状态文件", status_code=400)
        pending = self._load_pending(scope)
        return json_response({"pending": pending})

    async def page_clear_pending(self):
        payload = await request.json(default={})
        if not isinstance(payload, dict):
            return error_response("请求内容必须是 JSON 对象", status_code=400)
        state, _path, scope = self._load_state_by_file(self._text(payload.get("file")))
        if state is None or scope is None:
            return error_response("找不到状态文件", status_code=400)
        self._clear_pending(scope)
        return json_response({"ok": True})

    async def page_events(self):
        templates = load_event_templates(self.data_dir)
        return json_response(
            {
                "templates": templates,
                "path": str(self.data_dir / "events.json"),
            }
        )

    async def page_events_save(self):
        payload = await request.json(default={})
        if not isinstance(payload, dict):
            return error_response("请求内容必须是 JSON 对象", status_code=400)
        templates = payload.get("templates")
        if not isinstance(templates, list):
            return error_response("templates 必须是数组", status_code=400)
        valid = [
            item
            for item in templates
            if isinstance(item, dict)
            and str(item.get("id") or "").strip()
            and str(item.get("title") or "").strip()
            and isinstance(item.get("options"), list)
            and item["options"]
        ]
        if not valid:
            return error_response("没有有效的事件模板", status_code=400)
        path = self.data_dir / "events.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(valid, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        tmp.replace(path)
        return json_response({"ok": True, "count": len(valid)})

    def _worldview_summary(self) -> str:
        config = self._read_botmesh_config()
        lines: list[str] = []
        for bot in config.get("bots", []):
            if not isinstance(bot, dict):
                continue
            bot_id = str(bot.get("bot_id") or "")
            name = str(
                bot.get("display_name")
                or bot.get("nickname")
                or bot_id
            )
            notes: list[str] = []
            for profile in config.get("persona_profiles", []):
                if not isinstance(profile, dict):
                    continue
                if str(profile.get("bot_id") or "") != bot_id:
                    continue
                identity_parts = []
                for key in ("self_identity", "body_identity", "soul_identity"):
                    value = str(profile.get(key) or "").strip()
                    if value:
                        identity_parts.append(f"{key}={value}")
                note = str(profile.get("identity_note") or "").strip()
                if note:
                    identity_parts.append(note)
                if identity_parts:
                    group = str(profile.get("group_id") or "全局")
                    notes.append(f"{group}：{'；'.join(identity_parts)}")
            lines.append(
                f"角色 {name}（{bot_id}）"
                + (f"：{'；'.join(notes)}" if notes else "")
            )
        for user in config.get("users", []):
            if not isinstance(user, dict):
                continue
            display_name = str(user.get("display_name") or "").strip()
            if not display_name:
                continue
            description = str(user.get("description") or "").strip()
            lines.append(f"用户 {display_name}：{description or '无描述'}")
        return "\n".join(lines[:40]) or "无额外世界观信息"

    def _sanitize_generated_template(
        self,
        template: dict[str, Any],
        kind: str,
    ) -> dict[str, Any] | None:
        result: dict[str, Any] = {
            "id": f"evt_{uuid.uuid4().hex[:8]}",
            "title": str(template.get("title") or "未命名奇遇").strip()[:40],
            "story": str(template.get("story") or "").strip()[:800],
            "weight": 5,
            "condition": {},
        }
        if not result["title"] or not result["story"]:
            return None
        try:
            weight = int(template.get("weight", 5) or 5)
            result["weight"] = max(1, min(20, weight))
        except (TypeError, ValueError):
            pass
        condition = (
            template.get("condition")
            if isinstance(template.get("condition"), dict)
            else {}
        )
        for key in (
            "min_intimacy",
            "min_trust",
            "min_level",
            "min_habits",
            "min_special_events",
        ):
            try:
                value = max(0, int(condition.get(key, 0) or 0))
            except (TypeError, ValueError):
                value = 0
            if value > 0:
                result["condition"][key] = value
        if kind == "special":
            result["special"] = True
        if kind == "ultimate":
            result["ultimate"] = True
        max_delta = self._cfg_int("max_single_delta", 3, 1, 10)
        options: list[dict[str, Any]] = []
        seen_keys: set[str] = set()
        raw_options = template.get("options", [])
        if not isinstance(raw_options, list):
            return None
        for index, raw in enumerate(raw_options):
            if not isinstance(raw, dict):
                continue
            key = str(raw.get("key") or "").strip().upper()
            if not key or key in seen_keys:
                key = chr(ord("A") + index)
            if key in seen_keys:
                continue
            seen_keys.add(key)
            text = str(raw.get("text") or "").strip()[:80]
            outcome = str(raw.get("outcome") or "").strip()[:300]
            if not text:
                continue
            deltas = raw.get("deltas") if isinstance(raw.get("deltas"), dict) else {}
            try:
                d_intimacy = max(
                    -max_delta,
                    min(max_delta, int(deltas.get("intimacy", 0) or 0)),
                )
            except (TypeError, ValueError):
                d_intimacy = 0
            try:
                d_trust = max(
                    -max_delta,
                    min(max_delta, int(deltas.get("trust", 0) or 0)),
                )
            except (TypeError, ValueError):
                d_trust = 0
            try:
                d_exp = max(0, min(100, int(deltas.get("exp", 0) or 0)))
            except (TypeError, ValueError):
                d_exp = 0
            option: dict[str, Any] = {
                "key": key,
                "text": text,
                "deltas": {
                    "intimacy": d_intimacy,
                    "trust": d_trust,
                    "exp": d_exp,
                },
                "outcome": outcome,
            }
            if kind == "ultimate":
                ending = str(raw.get("ending") or "").strip()
                if not ending:
                    ending = key.lower()
                label = str(raw.get("ending_label") or "").strip() or ending
                option["ending"] = ending
                option["ending_label"] = label
            options.append(option)
        if len(options) < 2:
            return None
        result["options"] = options
        return result

    async def page_events_generate(self):
        payload = await request.json(default={})
        if not isinstance(payload, dict):
            return error_response("请求内容必须是 JSON 对象", status_code=400)
        summary = self._text(payload.get("summary"))
        if not summary:
            return error_response("summary 不能为空", status_code=400)
        kind = self._text(payload.get("kind")) or "normal"
        if kind not in {"normal", "special", "ultimate"}:
            kind = "normal"
        extra = self._text(payload.get("extra"))
        generated = await generate_event_template(
            self.context,
            config=self.config,
            umo="",
            worldview_summary=self._worldview_summary(),
            story_summary=summary,
            kind=kind,
            extra=extra,
        )
        if not isinstance(generated, dict):
            return error_response(
                "生成失败：模型没有返回有效模板，请稍后重试",
                status_code=500,
            )
        template = self._sanitize_generated_template(generated, kind)
        if template is None:
            return error_response(
                "生成结果不合法（缺少标题/剧情或选项不足 2 个）",
                status_code=422,
            )
        return json_response({"ok": True, "template": template})

    async def page_config(self):
        if request.method == "POST":
            payload = await request.json(default={})
            if not isinstance(payload, dict):
                return error_response("请求内容必须是 JSON 对象", status_code=400)
            updates = payload.get("config")
            if not isinstance(updates, dict):
                return error_response("config 必须是对象", status_code=400)
            for key, value in updates.items():
                self.config[str(key)] = value
            result = self.config.save_config()
            if inspect.isawaitable(result):
                await result
            return json_response({"ok": True, "config": dict(self.config)})
        return json_response({"config": dict(self.config)})


def _parse_attribute_arg(
    args: str,
    label: str,
) -> int | None:
    text = str(args or "").strip()
    pattern = rf"(?:{re.escape(label)}\s*[=:：]\s*(\d+)|(?:^|\s)(\d+)\s*(?:{re.escape(label)}))"
    match = re.search(pattern, text)
    if not match:
        return None
    raw = match.group(1) or match.group(2)
    if raw is None:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None
