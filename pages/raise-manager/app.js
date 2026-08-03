const bridge = window.AstrBotPluginPage;
const $ = (id) => document.getElementById(id);
const esc = (value) => String(value ?? "").replace(/[&<>"']/g, (ch) => ({
  "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
})[ch]);

const state = {
  groups: [],
  labels: {},
  current: null,
  selectedGroup: null,
  selectedBot: null,
  activeTab: "overview",
  busy: false,
  isEmpty: false,
};

function setStatus(text = "", kind = "") {
  $("status").textContent = text;
  if (kind) $("status").dataset.kind = kind;
  else delete $("status").dataset.kind;
}

function setBusy(busy) {
  state.busy = busy;
  for (const button of document.querySelectorAll("button")) button.disabled = busy;
  $("group").disabled = busy;
  $("bot").disabled = busy;
}

function fmtTime(ts) {
  if (!ts) return "—";
  const d = new Date(ts * 1000);
  const pad = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

function progressWidth(value, max) {
  const span = Math.max(1, Number(max) || 1);
  return Math.max(0, Math.min(100, Math.round((Number(value) / span) * 100)));
}

function switchTab(tab) {
  state.activeTab = tab;
  for (const button of document.querySelectorAll(".tab")) {
    button.classList.toggle("is-active", button.dataset.tab === tab);
  }
  for (const name of ["overview", "members", "events", "templates", "config"]) {
    $(`${name}Panel`).hidden = name !== tab;
  }
}

function selectedBotOption() {
  const file = $("bot").value;
  if (!file) return null;
  if (file.startsWith("empty:")) {
    const botId = file.slice(6);
    return state.selectedGroup?.bots?.find((bot) => bot.bot_id === botId) || null;
  }
  return state.selectedGroup?.bots?.find((bot) => bot.file === file) || null;
}

async function loadOverview() {
  setBusy(true);
  try {
    const data = await bridge.apiGet("overview");
    state.groups = data.groups || [];
    state.labels = data.labels || {};
    const allBots = state.groups.flatMap((group) => group.bots || []);
    $("heroStates").textContent = allBots.length;
    $("heroEndings").textContent = allBots.filter((bot) => bot.ending?.ending_id).length;

    const groupSelect = $("group");
    groupSelect.innerHTML = "";
    if (!state.groups.length) {
      groupSelect.innerHTML = '<option value="">还没有群聊/角色</option>';
      $("detail").hidden = true;
      $("emptyDetail").hidden = false;
      setStatus("还没有配置角色，请先在 botmesh 配置好群绑定", "error");
      return;
    }
    $("emptyDetail").hidden = true;
    for (const group of state.groups) {
      const option = document.createElement("option");
      option.value = group.group_id;
      option.textContent = group.label || group.group_id || "未映射群聊";
      groupSelect.appendChild(option);
    }
    groupSelect.selectedIndex = 0;
    $("detail").hidden = false;
    await onGroupChange();
    setStatus(`已加载 ${state.groups.length} 个群 · ${allBots.length} 个角色`, "success");
  } catch (error) {
    setStatus("加载失败：" + error, "error");
  } finally {
    setBusy(false);
  }
}

async function onGroupChange() {
  const groupId = $("group").value;
  state.selectedGroup = state.groups.find((group) => group.group_id === groupId) || null;
  const bots = state.selectedGroup?.bots || [];
  const botSelect = $("bot");
  botSelect.innerHTML = "";
  for (const bot of bots) {
    const option = document.createElement("option");
    option.value = bot.file || `empty:${bot.bot_id}`;
    option.textContent = `${bot.name || bot.bot_id}${bot.memory_key ? `（${bot.memory_key}）` : ""}${bot.ending?.label ? " 🏆" : ""}${bot.has_state ? "" : " · 尚未开始"}`;
    botSelect.appendChild(option);
  }
  if (bots.length) botSelect.selectedIndex = 0;
  await loadState();
}

async function loadState() {
  const bot = selectedBotOption();
  if (!bot) {
    $("detail").hidden = true;
    return;
  }
  setBusy(true);
  try {
    if (bot.file) {
      state.current = await bridge.apiGet("state", { file: bot.file });
    } else {
      state.current = await bridge.apiGet("state", {
        group_id: state.selectedGroup.group_id,
        bot_id: bot.bot_id,
      });
    }
    renderState(state.current);
    setStatus("");
  } catch (error) {
    setStatus("读取详情失败：" + error, "error");
  } finally {
    setBusy(false);
  }
}

function renderState(data) {
  state.isEmpty = Boolean(data.empty);
  const actionButtons = [
    $("correctGlobal"), $("resetState"), $("triggerEvent"),
    $("clearPending"), $("saveEvents"), $("saveConfig"),
  ];
  for (const button of actionButtons) button.disabled = state.busy || state.isEmpty;

  if (data.empty) {
    $("stateName").textContent = data.name || data.bot_id || "—";
    $("stateMeta").textContent = `${data.group_label || data.logical_group_id || ""} · 尚未开始养成`;
    $("stateBadges").innerHTML = '<span class="badge">尚未开始</span>';
    $("statLevel").textContent = "—";
    $("levelExp").textContent = "在该群互动后自动创建状态";
    $("levelBar").style.width = "0%";
    $("statIntimacy").textContent = "—";
    $("statTrust").textContent = "—";
    $("statHabits").textContent = "—";
    $("statEvents").textContent = "—";
    $("goals").innerHTML = '<div class="empty-state"><p>开始互动后这里会显示目标进度。</p></div>';
    $("members").innerHTML = '<div class="empty-state"><p>还没有成员数据。</p></div>';
    $("events").innerHTML = '<div class="empty-state"><p>还没有成长记录。</p></div>';
    $("pending").textContent = "暂无待定事件";
    return;
  }

  const row = state.selectedGroup?.bots?.find((bot) => bot.file === $("bot").value) || {};
  $("stateName").textContent = data.display_name || row.name || data.bot_id || "—";
  $("stateMeta").textContent = `${row.memory_key || data.memory_key || ""} · ${state.selectedGroup?.label || ""}`;

  const badges = [];
  if (data.ending?.label) badges.push(`<span class="badge badge-ultimate">🏆 ${esc(data.ending.label)}</span>`);
  if (data.init_status === "done") badges.push(`<span class="badge badge-done">初始值：${esc(data.init_note || "已生成")}</span>`);
  else badges.push('<span class="badge">初始值生成中…</span>');
  $("stateBadges").innerHTML = badges.join(" ");

  $("statLevel").textContent = `Lv.${data.level}`;
  $("levelExp").textContent = `${data.exp} / ${data.next_exp}`;
  $("levelBar").style.width = `${progressWidth(data.exp - (data.level_base || 0), (data.next_exp || 0) - (data.level_base || 0))}%`;
  $("statIntimacy").textContent = `${data.intimacy}/100`;
  $("statTrust").textContent = `${data.trust}/100`;
  $("statHabits").textContent = `${(data.habits || []).length} 个`;
  $("statEvents").textContent = `奇遇 ${data.event_count} 次 · 特殊 ${(data.special_events_completed || []).length}`;

  renderGoals(data);
  renderMembers(data);
  renderEvents(data);

  const pending = data.pending;
  $("pending").textContent = pending
    ? `✨ ${pending.title || "事件"}${pending.target_member?.name ? ` · 指定 ${pending.target_member.name} 回应` : ""}\n${pending.story || ""}`
    : "暂无待定事件";
}

function renderGoals(data) {
  const rows = (data.goals_lines || [])
    .map((line) => String(line).trim())
    .filter(Boolean)
    .map((clean) => `<div class="goal-row"><span class="name">${esc(clean)}</span></div>`);
  $("goals").innerHTML = rows.length ? rows.join("") : '<div class="empty-state"><p>暂无目标数据</p></div>';
}

function renderMembers(data) {
  const members = Object.values(data.members || {});
  if (!members.length) {
    $("members").innerHTML = '<div class="empty-state"><p>还没有成员互动记录，初始关系会自动从设定带入。</p></div>';
    return;
  }
  $("members").innerHTML = members.map((member) => {
    const initial = String(member.name || member.member_id || "?").trim().slice(0, 1).toUpperCase();
    const kindLabel = member.kind === "bot" ? "Bot" : "成员";
    return `
      <div class="member-card">
        <div class="member-top">
          <div class="member-id">
            <span class="avatar">${esc(initial)}</span>
            <div>
              <strong>${esc(member.name || member.member_id)}</strong>
              <small>${kindLabel} · ${esc(member.baseline_source || "待生成基线")}</small>
            </div>
          </div>
          <span class="badge ${member.kind === "bot" ? "badge-ultimate" : "badge-done"}">${kindLabel}</span>
        </div>
        <div class="member-stats">
          <div class="member-stat">
            <span><b>好感</b><em>${Number(member.intimacy) || 0}/100</em></span>
            <div class="progress"><i style="width:${Number(member.intimacy) || 0}%"></i></div>
          </div>
          <div class="member-stat">
            <span><b>信任</b><em>${Number(member.trust) || 0}/100</em></span>
            <div class="progress"><i style="width:${Number(member.trust) || 0}%"></i></div>
          </div>
        </div>
        ${member.baseline_reason ? `<p class="sub">基线依据：${esc(member.baseline_reason)}</p>` : ""}
        <div class="member-edit">
          <input data-member="${esc(member.member_id)}" data-field="intimacy" type="number" min="0" max="100" value="${Number(member.intimacy) || 0}" />
          <input data-member="${esc(member.member_id)}" data-field="trust" type="number" min="0" max="100" value="${Number(member.trust) || 0}" />
          <button class="button button-secondary button-small member-save" data-member="${esc(member.member_id)}" type="button">保存</button>
        </div>
      </div>`;
  }).join("");
}

function renderEvents(data) {
  const events = (data.events || []).slice().reverse().slice(0, 20);
  if (!events.length) {
    $("events").innerHTML = '<div class="empty-state"><p>暂无成长记录</p></div>';
    return;
  }
  $("events").innerHTML = events.map((item) => {
    const parts = [];
    if (Number(item.intimacy_delta)) parts.push(`好感${Number(item.intimacy_delta) > 0 ? "+" : ""}${item.intimacy_delta}`);
    if (Number(item.trust_delta)) parts.push(`信任${Number(item.trust_delta) > 0 ? "+" : ""}${item.trust_delta}`);
    if (Number(item.exp_gained)) parts.push(`经验+${item.exp_gained}`);
    if (item.habit_promoted) parts.push(`新习惯：${item.habit_promoted}`);
    const delta = parts.join("，") || "无变化";
    const kind = item.source_kind === "raise_event" ? "✨ 奇遇" : item.source_kind === "raise_ending" ? "🏆 终局" : "对话成长";
    return `
      <div class="event-row">
        <span class="time">${fmtTime(item.at)}</span>
        <span class="delta">${esc(delta)}</span>
        <span class="reason">${esc(item.reason || "")} <span class="badge">${esc(kind)}</span></span>
      </div>`;
  }).join("");
}

async function apiPost(endpoint, body) {
  setBusy(true);
  const prevGroup = $("group").value;
  const prevFile = $("bot").value;
  try {
    const result = await bridge.apiPost(endpoint, body);
    setStatus("已保存", "success");
    await loadOverview();
    if (prevGroup) {
      const groupOption = Array.from($("group").options).find(
        (option) => option.value === prevGroup
      );
      if (groupOption) {
        $("group").value = prevGroup;
        await onGroupChange();
      }
    }
    if (prevFile) {
      const botOption = Array.from($("bot").options).find(
        (option) => option.value === prevFile
      );
      if (botOption) {
        $("bot").value = prevFile;
        await loadState();
      }
    }
    return result;
  } catch (error) {
    setStatus("操作失败：" + error, "error");
  } finally {
    setBusy(false);
  }
}

function bindEvents() {
  $("group").addEventListener("change", onGroupChange);
  $("bot").addEventListener("change", loadState);
  $("reload").addEventListener("click", loadOverview);

  for (const button of document.querySelectorAll(".tab")) {
    button.addEventListener("click", () => switchTab(button.dataset.tab));
  }

  $("correctGlobal").addEventListener("click", () => {
    apiPost("correct", {
      file: $("bot").value,
      exp: $("correctExp").value === "" ? null : Number($("correctExp").value),
    });
  });

  document.addEventListener("click", (event) => {
    const target = event.target;
    if (!target.classList?.contains("member-save")) return;
    const memberId = target.dataset.member;
    const intimacy = document.querySelector(`[data-member="${CSS.escape(memberId)}"][data-field="intimacy"]`)?.value;
    const trust = document.querySelector(`[data-member="${CSS.escape(memberId)}"][data-field="trust"]`)?.value;
    apiPost("correct", {
      file: $("bot").value,
      member_id: memberId,
      intimacy: intimacy === "" ? null : Number(intimacy),
      trust: trust === "" ? null : Number(trust),
    });
  });

  $("resetState").addEventListener("click", () => {
    if (!confirm("确认重置该角色的全部养成状态？旧文件会保留备份。")) return;
    apiPost("reset", { file: $("bot").value });
  });

  $("triggerEvent").addEventListener("click", async () => {
    setBusy(true);
    try {
      const result = await bridge.apiPost("trigger_event", { file: $("bot").value });
      if (result.sent) {
        setStatus("事件已触发并发送到群", "success");
      } else {
        setStatus("事件已准备，但 QQ 官方平台不支持主动发送；可复制事件内容到群内，或用 /raise event", "error");
        $("pending").textContent = result.message;
      }
      await loadOverview();
    } catch (error) {
      setStatus("触发失败：" + error, "error");
    } finally {
      setBusy(false);
    }
  });

  $("clearPending").addEventListener("click", () => {
    apiPost("clear_pending", { file: $("bot").value });
  });

  $("saveEvents").addEventListener("click", async () => {
    setBusy(true);
    try {
      const templates = JSON.parse($("eventsText").value);
      await bridge.apiPost("events_save", { templates });
      setStatus("事件模板已保存", "success");
    } catch (error) {
      setStatus("保存失败（JSON 可能不合法）：" + error, "error");
    } finally {
      setBusy(false);
    }
  });

  $("generateEvent").addEventListener("click", async () => {
    const summary = $("generateSummary").value.trim();
    if (!summary) {
      setStatus("请先输入奇遇/任务概要", "error");
      return;
    }
    setBusy(true);
    try {
      const result = await bridge.apiPost("events_generate", {
        summary,
        kind: $("generateKind").value,
        extra: $("generateExtra").value.trim(),
      });
      let templates = [];
      try {
        templates = JSON.parse($("eventsText").value);
      } catch {
        templates = [];
      }
      if (!Array.isArray(templates)) templates = [];
      const existingIds = new Set(templates.map((item) => item.id));
      if (result.template && !existingIds.has(result.template.id)) {
        templates.push(result.template);
      }
      $("eventsText").value = JSON.stringify(templates, null, 2);
      await bridge.apiPost("events_save", { templates });
      setStatus(
        `已生成并保存：${result.template ? result.template.title : "事件模板"}`,
        "success",
      );
    } catch (error) {
      setStatus("生成失败：" + error, "error");
    } finally {
      setBusy(false);
    }
  });

  $("saveConfig").addEventListener("click", async () => {
    setBusy(true);
    try {
      const config = JSON.parse($("configText").value);
      await bridge.apiPost("config", { config });
      setStatus("配置已保存", "success");
    } catch (error) {
      setStatus("保存失败（JSON 可能不合法）：" + error, "error");
    } finally {
      setBusy(false);
    }
  });
}

async function loadEditors() {
  try {
    const [events, config] = await Promise.all([
      bridge.apiGet("events"),
      bridge.apiGet("config"),
    ]);
    $("eventsText").value = JSON.stringify(events.templates || [], null, 2);
    $("configText").value = JSON.stringify(config.config || {}, null, 2);
  } catch (error) {
    setStatus("加载编辑区失败：" + error, "error");
  }
}

(async () => {
  await bridge.ready();
  bindEvents();
  await Promise.all([loadOverview(), loadEditors()]);
})();
