const $ = (id) => document.getElementById(id);
const bridge = window.AstrBotPluginView || window.AstrBotPluginPage;
const state = { archives: [], loaded: null, draft: null, selected: "world", editing: false, dirty: false, saving: false };
const clone = (value) => JSON.parse(JSON.stringify(value));

function node(tag, attributes = {}, children = []) {
  const element = document.createElement(tag);
  for (const [key, value] of Object.entries(attributes)) {
    if (key === "text") element.textContent = value;
    else if (key === "class") element.className = value;
    else if (key.startsWith("on")) element.addEventListener(key.slice(2), value);
    else if (value !== undefined) element[key] = value;
  }
  for (const child of children) element.append(child);
  return element;
}
function notify(text, error = false) {
  $("message").textContent = text;
  $("message").classList.toggle("error", error);
  $("message").hidden = !text;
}
function dirty() {
  state.dirty = true;
  $("save-state").textContent = "有未保存的修改";
  $("save").disabled = state.saving;
}
function canLeave() {
  if (state.saving) return false;
  return !state.dirty || window.confirm("有尚未保存的修改，确定放弃这些修改吗？");
}
function card(title, description, content, extra) {
  const header = node("div", { class: "card-header" }, [node("h2", { text: title })]);
  if (extra) header.append(extra);
  return node("section", { class: "card" }, [header, node("p", { class: "card-description", text: description }), ...content]);
}
function field(label, value, onInput, { long = false, required = true, input = false } = {}) {
  const control = node(input ? "input" : "textarea", {
    value, disabled: !state.editing, required, class: long ? "long" : "",
    oninput: (event) => { onInput(event.target.value); dirty(); },
  });
  if (!input) control.rows = long ? 7 : 3;
  control.setAttribute("aria-label", label);
  return node("label", { class: "field" }, [node("span", { class: "field-label", text: label }), control]);
}
function mapEditor(title, description, data, key) {
  const entries = Object.entries(data[key]);
  const content = [];
  if (!entries.length) content.push(node("p", { class: "quiet-text", text: "暂无记录" }));
  const sync = () => { data[key] = Object.fromEntries(entries); dirty(); };
  for (let index = 0; index < entries.length; index++) {
    const keyInput = node("input", { value: entries[index][0], required: true, placeholder: "条目名称", disabled: !state.editing });
    keyInput.setAttribute("aria-label", `${title}：条目名称`);
    keyInput.addEventListener("input", () => {
      const next = keyInput.value;
      const duplicate = entries.some(([name], other) => other !== index && name === next);
      keyInput.setCustomValidity(duplicate ? "条目名称不能重复" : "");
      if (!duplicate) { entries[index][0] = next; sync(); }
      else dirty();
    });
    keyInput.addEventListener("change", () => { if (key === "factions" && keyInput.checkValidity()) renderEditor(); });
    const valueInput = node("textarea", { value: entries[index][1], rows: 2, required: true, placeholder: "填写内容", disabled: !state.editing,
      oninput: (event) => { entries[index][1] = event.target.value; sync(); } });
    valueInput.setAttribute("aria-label", `${title}：内容`);
    const row = node("div", { class: "map-row" }, [keyInput, valueInput]);
    if (state.editing) row.append(node("button", { class: "remove", type: "button", text: "×", title: "删除此条目", onclick: () => { entries.splice(index, 1); sync(); renderEditor(); } }));
    content.push(row);
  }
  if (state.editing) content.push(node("button", { class: "add", type: "button", text: "+ 添加条目", onclick: () => {
    if (entries.some(([name]) => !name.trim())) { notify("请先填写空白条目的名称", true); return; }
    entries.push(["", ""]); sync(); renderEditor();
  } }));
  return card(title, description, content);
}
function relationsEditor(book) {
  const content = [];
  if (!book.relations.length) content.push(node("p", { class: "quiet-text", text: "暂无势力关系" }));
  book.relations.forEach((relation, index) => {
    const select = (key, label) => {
      const control = node("select", { disabled: !state.editing, required: true,
        onchange: (event) => { relation[key] = event.target.value; dirty(); } });
      control.setAttribute("aria-label", label);
      const factions = Object.keys(book.factions);
      if (!factions.includes(relation[key])) control.append(node("option", { value: relation[key], text: `${relation[key]}（请重新选择）` }));
      for (const name of factions) control.append(node("option", { value: name, text: name }));
      control.value = relation[key];
      return control;
    };
    const row = node("div", { class: "relation-row" }, [select("source", "关系所属势力"), node("span", { class: "relation-arrow", text: "→" }), select("target", "关系目标势力")]);
    if (state.editing) row.append(node("button", { class: "remove", type: "button", text: "×", title: "删除此关系", onclick: () => { book.relations.splice(index, 1); dirty(); renderEditor(); } }));
    row.append(node("textarea", { rows: 2, value: relation.value, required: true, disabled: !state.editing,
      oninput: (event) => { relation.value = event.target.value; dirty(); } }));
    row.querySelector("textarea").setAttribute("aria-label", "势力关系内容");
    content.push(row);
  });
  if (state.editing) content.push(node("button", { class: "add", type: "button", text: "+ 添加势力关系", onclick: () => {
    const names = Object.keys(book.factions).filter((name) => name.trim());
    if (names.length < 2) { notify("请先添加至少两个势力", true); return; }
    book.relations.push({ source: names[0], target: names[1], value: "" }); dirty(); renderEditor();
  } }));
  return card("势力关系", "只记录组织之间的联盟、敌对或臣属关系，人物私人关系保存在角色卡中。", content);
}
function memoriesEditor(role) {
  const content = [];
  if (!role.memories.length) content.push(node("p", { class: "quiet-text", text: "暂无重要经历" }));
  role.memories.forEach((memory, index) => {
    const checkbox = node("input", { type: "checkbox", checked: memory.known, disabled: !state.editing,
      onchange: (event) => { memory.known = event.target.checked; dirty(); } });
    const head = node("div", { class: "memory-head" }, [node("label", { class: "checkbox-label" }, [checkbox, document.createTextNode("角色本人知道这段经历")])]);
    if (state.editing) head.append(node("button", { class: "remove", type: "button", text: "×", title: "删除此段经历", onclick: () => { role.memories.splice(index, 1); dirty(); renderEditor(); } }));
    content.push(node("div", { class: "memory-row" }, [head, field("经历内容", memory.text, (value) => { memory.text = value; })]));
  });
  if (state.editing) content.push(node("button", { class: "add", type: "button", text: "+ 添加经历", onclick: () => { role.memories.push({ text: "", known: true }); dirty(); renderEditor(); } }));
  return card("重要经历", "当前状态可以变化，旧经历依然保留。未勾选的经历仅供叙事者参考。", content);
}
function renderNavigation() {
  const documentData = state.draft?.document;
  const roles = documentData?.characters || [];
  $("role-count").textContent = String(roles.length);
  $("world-nav").disabled = !documentData;
  $("world-nav").classList.toggle("active", state.selected === "world");
  $("role-search").disabled = !documentData;
  $("role-list").replaceChildren();
  const query = $("role-search").value.trim().toLowerCase();
  for (const role of roles) {
    if (!role.name.toLowerCase().includes(query)) continue;
    const button = node("button", { class: `nav-item${state.selected === role.id ? " active" : ""}`, onclick: () => choose(role.id) }, [
      node("span", { class: "role-avatar", text: role.name.slice(0, 1) }),
      node("span", {}, [node("span", { text: role.name }), node("span", { class: "role-meta", text: role.controller === "user" ? "用户控制角色" : "独立角色档案" })]),
    ]);
    button.setAttribute("aria-label", `查看角色 ${role.name}`);
    $("role-list").append(button);
  }
}
function renderEditor() {
  if (!state.draft) return;
  const data = state.draft.document;
  const role = data.characters.find((item) => item.id === state.selected);
  const isWorld = state.selected === "world";
  $("eyebrow").textContent = isWorld ? "世界书" : "人设卡";
  $("page-title").textContent = isWorld ? "世界背景与势力" : role.name;
  $("breadcrumb").textContent = isWorld ? "世界书" : role.name;
  $("page-description").textContent = isWorld ? "宏观背景、世界事件与势力关系，和人物关系分别生长。" : "核心设定、当前状态、认知与成长，共同构成这个角色。";
  $("edit").hidden = state.editing;
  $("cancel").hidden = !state.editing;
  $("save").hidden = !state.editing;
  $("save").disabled = !state.dirty || state.saving;
  $("save-state").textContent = state.dirty ? "有未保存的修改" : "已保存";
  $("edit-note").hidden = !state.editing;
  const sections = [];
  if (isWorld) {
    if (data.world_base) sections.push(card("基础世界背景", "保留导入时的背景原文，当前世界状态可随剧情继续推进。", [field("基础世界背景原文", data.world_base, (value) => { data.world_base = value; }, { long: true })]));
    if (data.world || state.editing) sections.push(card("世界背景与固定规则", "作者设定的长期规则、叙事方式与用户控制权。只有你手动修改时才会改变。", [field("世界规则", data.world, (value) => { data.world = value; }, { long: true, required: false })], node("span", { class: "pill", text: "固定设定" })));
    sections.push(mapEditor("世界状态", "当前年份、时代局势等会随故事推进的宏观事实。", data.worldbook, "facts"));
    sections.push(mapEditor("势力档案", "记录各个组织的现状、资源与宏观目标。", data.worldbook, "factions"));
    sections.push(relationsEditor(data.worldbook));
    const events = data.worldbook.events;
    const eventRows = events.map((text, index) => node("div", { class: "memory-row" }, [field(`世界事件 ${index + 1}`, text, (value) => { events[index] = value; }), ...(state.editing ? [node("button", { class: "remove", type: "button", text: "删除事件", onclick: () => { events.splice(index, 1); dirty(); renderEditor(); } })] : [])]));
    if (!eventRows.length) eventRows.push(node("p", { class: "quiet-text", text: "尚无重大世界事件" }));
    if (state.editing) eventRows.push(node("button", { class: "add", type: "button", text: "+ 添加世界事件", onclick: () => { events.push(""); dirty(); renderEditor(); } }));
    sections.push(card("世界大事记", "保留对整个世界产生影响的重要事件。", eventRows));
  } else {
    const controller = node("select", { value: role.controller, disabled: !state.editing, onchange: (event) => { role.controller = event.target.value; dirty(); } }, [node("option", { value: "ai", text: "AI 演绎" }), node("option", { value: "user", text: "用户独占控制" })]);
    controller.value = role.controller;
    sections.push(card("角色身份", "用户控制的角色只记录档案，AI 不会因此获得代写权限。", [node("div", { class: "two-columns" }, [field("角色名称", role.name, (value) => { role.name = value; }, { input: true }), node("label", { class: "field" }, [node("span", { class: "field-label", text: "控制方式" }), controller])])]));
    if (role.base) sections.push(card("基础角色设定", "保留导入的设定原文；其中的初始状态由后续剧情和结构化状态更新。", [field("基础角色设定原文", role.base, (value) => { role.base = value; }, { long: true })]));
    if (role.core || state.editing) sections.push(card("核心设定", "稳定身份、性格底线、外貌、永久特征与绝对禁忌。", [field("角色核心", role.core, (value) => { role.core = value; }, { long: true, required: !role.base })], node("span", { class: "pill", text: "固定设定" })));
    sections.push(mapEditor("当前状态", "身体、位置与人物之间的私人关系。", role, "state"));
    sections.push(mapEditor("角色认知", "这个角色知道、误信或不知道的事，与世界客观事实区分。", role, "knowledge"));
    sections.push(mapEditor("性格成长", "有剧情依据的持久变化，不会自动抹去角色底色。", role, "growth"));
    sections.push(memoriesEditor(role));
  }
  if (state.editing) sections.unshift(card("档案名称", "用于在会话档案列表中识别这段故事。", [field("档案名称", state.draft.title, (value) => { state.draft.title = value; }, { input: true })]));
  $("editor").replaceChildren(...sections);
  renderNavigation();
}
function choose(selected) {
  if (!canLeave()) return;
  state.draft = clone(state.loaded);
  state.selected = selected;
  state.editing = false;
  state.dirty = false;
  notify(""); renderEditor();
}
async function openArchive(id) {
  const previous = state.loaded?.id;
  if (!canLeave()) { $("archive-picker").value = previous || ""; return; }
  try {
    $("archive-picker").disabled = true;
    let data = await bridge.apiGet(`archives/${id}`);
    if (!data.document) {
      notify("已同步会话，正在读取当前人设并初始化世界书与角色卡…");
      await bridge.apiPost(`archives/${id}/initialize`, {});
      data = await bridge.apiGet(`archives/${id}`);
    }
    state.loaded = data; state.draft = clone(data); state.selected = "world"; state.editing = false; state.dirty = false;
    const entry = state.archives.find((item) => item.id === id);
    $("archive-origin").textContent = entry?.origin || (entry?.conversation ? `会话 ${entry.conversation}` : "当前会话的独立档案");
    $("empty").hidden = true; $("workspace").hidden = false;
    notify(""); renderEditor();
  } catch (error) {
    $("archive-picker").value = previous || "";
    notify(error.message || "读取档案失败", true);
  } finally { $("archive-picker").disabled = state.archives.length === 0; }
}
async function refresh() {
  if (!canLeave()) return;
  try {
    const result = await bridge.apiGet("archives");
    state.archives = result.archives;
    $("archive-picker").replaceChildren(...state.archives.map((item) => node("option", { value: item.id, text: `${item.title} · ${(item.conversation || item.id).slice(0, 8)}` })));
    if (!state.archives.length) {
      state.loaded = null; state.draft = null; state.editing = false; state.dirty = false;
      $("empty").hidden = false; $("workspace").hidden = true; $("empty-text").textContent = "尚未发现 AstrBot 会话。创建会话并选择人设后，点击同步会话即可初始化；普通对话也会自动初始化。";
      $("archive-picker").append(node("option", { text: "暂无会话档案" }));
      $("archive-picker").disabled = true; renderNavigation(); return;
    }
    const id = state.archives.some((item) => item.id === state.loaded?.id) ? state.loaded.id : state.archives[0].id;
    // The discard decision was already made before refreshing.
    state.dirty = false;
    $("archive-picker").value = id;
    await openArchive(id);
  } catch (error) { notify(error.message || "刷新失败", true); }
}
async function save() {
  if (!state.editing || !state.dirty || state.saving) return;
  for (const control of $("editor").querySelectorAll("input,textarea,select")) {
    if (!control.checkValidity()) { control.reportValidity(); return; }
  }
  const book = state.draft.document.worldbook;
  const pairs = new Set();
  for (const relation of book.relations) {
    const pair = JSON.stringify([relation.source, relation.target]);
    if (!(relation.source in book.factions) || !(relation.target in book.factions) || relation.source === relation.target || pairs.has(pair)) {
      notify("请检查势力关系：必须选择两个不同且存在的势力，同一方向不能重复。", true); return;
    }
    pairs.add(pair);
  }
  state.saving = true; $("save").disabled = true; $("save").textContent = "正在保存…"; $("cancel").disabled = true;
  for (const control of $("editor").querySelectorAll("input,textarea,select,button")) control.disabled = true;
  try {
    const result = await bridge.apiPost(`archives/${state.loaded.id}/save`, { revision: state.loaded.revision, title: state.draft.title, document: state.draft.document });
    state.draft.revision = result.revision; state.loaded = clone(state.draft); state.dirty = false; state.editing = false;
    const index = state.archives.findIndex((item) => item.id === state.loaded.id);
    if (index >= 0) { state.archives[index].title = state.loaded.title; $("archive-picker").options[index].text = `${state.loaded.title} · ${(state.archives[index].conversation || state.loaded.id).slice(0, 8)}`; }
    notify("修改已保存，下次请求时会读取新的档案。"); renderEditor();
  } catch (error) { notify(error.message || "保存失败，修改内容仍保留在页面中", true); }
  finally {
    state.saving = false; $("save").disabled = !state.dirty; $("save").textContent = "保存修改"; $("cancel").disabled = false;
    for (const control of $("editor").querySelectorAll("input,textarea,select,button")) control.disabled = !state.editing;
  }
}
$("world-nav").addEventListener("click", () => choose("world"));
$("role-search").addEventListener("input", renderNavigation);
$("archive-picker").addEventListener("change", (event) => openArchive(event.target.value));
$("refresh").addEventListener("click", refresh);
$("edit").addEventListener("click", () => { state.editing = true; notify(""); renderEditor(); });
$("cancel").addEventListener("click", () => { if (!canLeave()) return; state.draft = clone(state.loaded); state.editing = false; state.dirty = false; renderEditor(); notify("已取消修改"); });
$("save").addEventListener("click", save);
window.addEventListener("beforeunload", (event) => { if (state.dirty) { event.preventDefault(); event.returnValue = ""; } });
if (!bridge) {
  $("empty-text").textContent = "请从 AstrBot WebUI 的插件详情页打开此界面。";
  $("refresh").disabled = true;
} else {
  await bridge.ready();
  document.documentElement.dataset.theme = "light";
  await refresh();
}
