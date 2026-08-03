# 养成系统（astrbot_plugin_raise）

基于 BotMesh 的养成 MVP：每次对话后由 LLM 判定好感/信任的小幅变化，
累计经验升级，习惯候选重复出现后自动转正，并把成长摘要写入 BotMesh 结构化记忆。

## 命令

- `/raise enable`：把当前群加入养成白名单（管理员）
- `/raise disable`：把当前群移出养成名单（管理员）
- `/raise status`：当前角色的养成面板（等级/好感/信任/习惯/最近成长）
- `/raise goals`：当前角色的养成目标（等级/档位/习惯/今日成长额度）
- `/raise members`：按群员/Bot 查看各自的好感与信任
- `/raise event`：管理员手动触发一次奇遇事件
- `/raise log`：最近 20 条成长日志
- `/raise correct 好感=70 信任=55`：管理员修正数值
- `/raise reset`：管理员重置养成状态

## 生效群

默认仅白名单群生效（`session_list_mode=whitelist`）。管理员在目标群发送
`/raise enable` 即可开启；也可在插件配置里直接编辑 `session_list`（支持群 ID、
完整 UMO 或 平台:群ID），或把模式改为 `blacklist`/`none`。

## 存储与初始值

- 养成状态按“逻辑群 + 记忆身份”分开存储：同一群里每个角色的好感/信任/经验互相独立；
  灵魂互换场景下，成长会跟随 memory_key（灵魂）走，而不是账号。
- 群内关系按“当前角色 → 目标成员”分别存储：每个群员（Sirin/Saya 等）和每个 Bot
  都有独立的好感/信任；莉芙→蔚来与蔚来→莉芙是两份不同数据，不假设对称。
- `initial_source=auto`（默认）会读取当前逻辑群解析后的完整人格、世界观、结构化身份、
  目标对象设定（含普通用户 description）及定向关系，由 LLM 分别生成成员基线；BotMesh 的 affinity/trust/familiarity
  只作为参考先验，不再机械照抄。推断失败才回退关系表或固定值。
- 旧状态升级时会自动补齐 BotMesh 中缺失的成员，并把该成员已有历史增减按时间顺序重放到
  新人格基线上；不会清空经验、习惯、奇遇或互动历史。
- 启动迁移会等待模型提供商就绪；模型暂不可用时采用安全回退，并在冷却后自动重试，
  不会把回退关系永久固化。
- 总览好感/信任只是全部成员关系的派生均值；新互动只修改当前对话者对应的关系，
  每日正向上限也按成员分别计算。
- `initial_source=relation` 可强制直接使用关系表；`initial_source=llm` 强制人格世界观推断；
  `initial_source=fixed` 使用 `initial_intimacy`/`initial_trust`/`initial_exp`。

## 奇遇事件

- 触发分三种模式（`event_trigger_mode`）：
  - `random`：每次成功互动后按 `event_trigger_chance` 概率触发；
  - `context`：先读取最近群聊，由 LLM 判定当前是否处于适合奇遇的剧情节点
    （情绪高点、剧情推进、邀约、抉择时刻等），评分达到 `event_context_threshold`
    才触发；
  - `hybrid`（默认）：强剧情节点直接触发，普通互动仍按概率随机触发。
- 随机兜底不会在无关闲聊时硬触发：random/hybrid 模式下，随机命中后仍会由
  LLM 复核对话相关性，评分低于 `event_random_min_score`（默认 0.4）就放弃本次触发，
  避免奇遇与上下文完全不相干、显得突兀。
- 无论哪种模式都受最小间隔（`event_min_interval_hours`）与每日上限
  （`event_max_per_day`）约束；`/raise event` 可手动触发。
- 事件包含剧情与 2-4 个选项；群员回复 A/B/C/D 或 1/2/3/4 后自动结算。
- `event_target_mode=trigger` 时只有触发事件的成员能选择；`best_relation` 指定关系最好的成员；
  `anyone` 时先回复者胜出。事件消息会标明由谁回应。
- `event_llm_story=true` 时，普通奇遇会结合最近对话（`event_context_messages` 条）
  按世界观现场生成剧情、2-4 个选择及每个选择的受限奖励，让奇遇真正承接当下话题；
  生成内容必须通过结构、数值限幅和「至少一个正向、一个中性/代价选项」校验，失败时回退模板。
- 特殊奇遇与终极事件只让 LLM 改写剧情文字；奖励、解锁进度和结局字段始终由可信模板控制，
  避免动态内容破坏长期养成目标。
- 事件进行中时，进行中的奇遇（标题/剧情/选项）会注入 LLM 上下文（`<active_event>`），
  角色会自然融入剧情；选择选项后 `event_llm_narrative=true` 时由 LLM 以角色身份
  续写一小段对话，失败时回退为模板静态结果文案。
- 不同选项对应不同的好感/信任/经验变化，变化同样受单次与每日上限约束；
  结算结果与事件摘要会写入 BotMesh 记忆。
- 事件模板存放在插件数据目录的 `events.json`，可自行增删改；`condition`
  可设置 `min_intimacy`/`min_trust`/`min_level` 解锁门槛；带 `special: true` 的
  事件计入终极目标。

## 终极目标

- 终极目标是一个**叙事终局事件**：`events.json` 中带 `ultimate: true` 的模板
  （如“苹果树的邀约”），有自己的剧情与 A/B/C 选项，每个选项导向不同结局。
- 模板的 `condition`（好感/信任/等级/习惯/特殊奇遇数量）是**解锁门槛**；
  门槛全部满足后，事件会在下一次互动时自动触发（`ultimate_auto_trigger`），
  不再走随机概率。
- 选定选项后记录结局（如“成为苹果树”），广播终局消息并写入长期记忆；
  `/raise goals` 可查看解锁进度，达成后显示结局。
- 多套终局模板时可指定 `ultimate_event_id`，留空使用第一套。

## 机制

- 单次变化限制在 `max_single_delta` 内，正向变化按成员分别受每日上限约束，防刷分且互不串分。
- 经验只增不减；等级曲线为累计经验 100/250/450/700/1000……
- 习惯候选被观察到 `habit_promote_count` 次后自动转正，避免 LLM 一次幻觉直接写入。
- 角色不会在对话中复述数值；养成只影响语气、主动程度与信任深度，不覆盖 BotMesh Persona。
