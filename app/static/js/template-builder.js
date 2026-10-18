let state = null;
let root = null;
let activeSheetIndex = 0;
let selectedQuestionId = null;
let draftId = -1;
let statusMessage = "";
let statusType = "success";

const circled = ["", "①", "②", "③", "④", "⑤"];

function t(key, fallback = "") {
  const parts = key.split(".");
  let value = window.EZ_MARK_I18N || {};
  for (const part of parts) {
    value = value && value[part];
  }
  return typeof value === "string" ? value : fallback || key;
}

function defaultProblemTitle(problemNo) {
  return `${t("builder.problem_prefix", "Problem")} ${problemNo}`;
}

async function init() {
  root = document.querySelector("#template-builder-app");
  if (!root) return;
  const templateId = root.dataset.templateId;
  const response = await fetch(`/templates/${templateId}/builder/state`);
  if (!response.ok) {
    root.innerHTML = `<div class="alert-error"><p>${escapeHtml(t("builder.load_error", "Template builder state could not be loaded."))}</p></div>`;
    return;
  }
  state = await response.json();
  if (state.sheets.length === 0) {
    root.innerHTML = `<div class="alert-error"><p>${escapeHtml(t("builder.no_sheets", "This template has no physical sheets."))}</p></div>`;
    return;
  }
  prepareState();
  render();
}

function prepareState() {
  state.sheets.forEach((sheet) => {
    sheet.groups.forEach((group) => {
      // Server-synthesized groups (imported templates) arrive without ids.
      if (group.id == null) group.id = draftId--;
      group.questions.forEach((question) => {
        if (question.id == null) question.id = draftId--;
      });
      ensureGroupDefaults(group, sheet);
    });
  });
}

function render() {
  const sheet = state.sheets[activeSheetIndex] || state.sheets[0];
  root.innerHTML = `
    <div class="builder-editor">
      ${renderStatus()}
      ${renderTopBar(sheet)}
      ${renderSheetTabs()}
      <div class="builder-canvas-scroll">
        <main class="builder-paper" aria-label="Editable mark sheet">
          ${renderPaperHeader(sheet)}
          ${renderGroups(sheet)}
          <button class="button-secondary builder-add-group" type="button" data-action="add-group">
            ${escapeHtml(t("builder.add_group", "Add question group"))}
          </button>
        </main>
      </div>
      ${renderInspector(sheet)}
    </div>
  `;
}

function renderStatus() {
  if (!statusMessage) return "";
  const klass = statusType === "error" ? "alert-error" : "alert-success";
  return `<div class="${klass}" role="status"><p>${escapeHtml(statusMessage)}</p></div>`;
}

function renderTopBar(sheet) {
  const defaults = sheetDefaults(sheet);
  return `
    <div class="builder-toolbar">
      <div class="builder-toolbar-title">
        <strong>${escapeHtml(state.template.name)}</strong>
        <span>${escapeHtml(t(`builder.assessment_types.${state.template.assessment_type}`, state.template.assessment_type))}</span>
      </div>
      <label>${escapeHtml(t("builder.sheet_name", "Marksheet title"))}
        <input class="form-input" data-action="sheet-name" value="${escapeAttr(sheet.name)}">
      </label>
      <div class="builder-bulk">
        <span>${escapeHtml(t("builder.apply_to_sheet", "Apply to entire sheet"))}</span>
        <input type="number" step="0.25" min="0" value="${defaults.points}" data-sheet-points>
        ${sectionSelect("data-sheet-section", defaults.scoring_section_id || "")}
        <input type="number" min="2" max="5" value="${defaults.choice_count}" data-sheet-choices>
        <button class="button-secondary" type="button" data-action="apply-sheet">${escapeHtml(t("builder.apply", "Apply"))}</button>
      </div>
    </div>
  `;
}

function renderSheetTabs() {
  return `
    <div class="builder-tabs" role="tablist">
      ${state.sheets
        .map(
          (sheet, index) => `
            <button class="builder-tab ${index === activeSheetIndex ? "is-active" : ""}"
                    type="button"
                    data-action="switch-sheet"
                    data-index="${index}"
                    data-sheet-tab-label="${index}">
              ${escapeHtml(sheet.name)}
            </button>
          `,
        )
        .join("")}
    </div>
  `;
}

function renderPaperHeader(sheet) {
  const titleParts = [state.template.name];
  titleParts.push(sheet.name);
  return `
    <header class="builder-paper-header">
      <div class="builder-paper-title-row">
        <span class="builder-paper-marker" aria-hidden="true">■</span>
        <h2 data-paper-sheet-title>${escapeHtml(titleParts.join(" · "))}</h2>
      </div>
      <div class="builder-paper-fields">
        <span>${escapeHtml(t("builder.student", "Student"))}:________</span>
        <span>${escapeHtml(t("builder.name", "Name"))}:________</span>
        <span>${escapeHtml(t("builder.date", "Date"))}:____-__-__</span>
      </div>
    </header>
  `;
}

function renderGroups(sheet) {
  if (sheet.groups.length === 0) {
    return `<div class="builder-empty">${escapeHtml(t("builder.empty_sheet", "Add a question group to start building this sheet."))}</div>`;
  }
  return sheet.groups.map((group) => renderGroup(sheet, group)).join("");
}

function renderGroup(sheet, group) {
  ensureGroupDefaults(group, sheet);
  return `
    <section class="builder-problem" data-group-id="${group.id}">
      <div class="builder-problem-header">
        <input class="builder-problem-title"
               data-action="group-title"
               data-sheet-id="${sheet.id}"
               data-group-id="${group.id}"
               value="${escapeAttr(group.problem_title || `もんだい${group.problem_no}`)}">
        <span class="builder-problem-count">${group.questions.length} ${escapeHtml(t("builder.questions_count", "questions"))}</span>
        <div class="builder-problem-tools">
          <span>${escapeHtml(t("builder.defaults", "Defaults"))}</span>
          <input type="number" step="0.25" min="0" value="${group.default_points}" data-group-points>
          ${sectionSelect("data-group-section", group.default_scoring_section_id || "")}
          <input type="number" min="2" max="5" value="${group.default_choice_count}" data-group-choices>
          <button class="button-secondary" type="button" data-action="apply-group" data-group-id="${group.id}">${escapeHtml(t("builder.apply", "Apply"))}</button>
          <button class="button-secondary" type="button" data-action="add-question" data-group-id="${group.id}">+ ${escapeHtml(t("builder.add_question", "Question"))}</button>
          <button class="button-secondary" type="button" data-action="delete-group" data-group-id="${group.id}">${escapeHtml(t("builder.delete_group", "Delete"))}</button>
        </div>
      </div>
      <div class="builder-question-list">
        ${group.questions.map((question, index) => renderQuestion(group, question, index)).join("")}
      </div>
    </section>
  `;
}

function renderQuestion(group, question, index) {
  const selected = question.id === selectedQuestionId ? "is-selected" : "";
  const custom = questionDiffersFromGroupDefault(group, question);
  return `
    <div class="builder-question ${selected} ${custom ? "has-custom-settings" : ""}" data-action="select-question" data-question-id="${question.id}">
      <input class="builder-question-number"
             data-action="question-number"
             data-question-id="${question.id}"
             value="${escapeAttr(question.number || "")}"
             aria-label="${escapeAttr(t("builder.question_number", "Question number"))}">
      <div class="builder-bubbles" aria-label="${escapeAttr(t("builder.answer_choices", "Answer choices"))}">
        ${Array.from({ length: question.choice_count }, (_, index) => {
          const choice = String(index + 1);
          const active = question.correct_answer === choice ? "is-correct" : "";
          return `<button class="builder-answer-bubble ${active}" type="button" data-action="set-answer" data-question-id="${question.id}" data-answer="${choice}">${circled[index + 1]}</button>`;
        }).join("")}
      </div>
      <div class="builder-inline-settings">
        ${
          index > 0
            ? `<button class="button-secondary" type="button" data-action="split-group" data-group-id="${group.id}" data-question-id="${question.id}">${escapeHtml(t("builder.start_new_problem_here", "Start new problem here"))}</button>`
            : ""
        }
        <label>${escapeHtml(t("builder.points_short", "Pts"))} <input type="number" step="0.25" min="0" value="${question.points}" data-action="question-points" data-question-id="${question.id}"></label>
        <label>${escapeHtml(t("builder.section", "Section"))} ${sectionSelect(`data-action="question-section" data-question-id="${question.id}"`, question.scoring_section_id || "")}</label>
        <label>${escapeHtml(t("builder.choices", "Choices"))} <input type="number" min="2" max="5" value="${question.choice_count}" data-action="question-choices" data-question-id="${question.id}"></label>
        ${custom ? `<span class="builder-custom-indicator">${escapeHtml(t("builder.custom", "custom"))}</span>` : ""}
      </div>
    </div>
  `;
}

function renderInspector(sheet) {
  const selected = findQuestion(selectedQuestionId);
  if (!selected) {
    return `<aside class="builder-inspector"><p class="muted">${escapeHtml(t("builder.select_question_hint", "Select a question to edit its answer, points, section, and choices."))}</p></aside>`;
  }
  const { question, group } = selected;
  return `
    <aside class="builder-inspector">
      <strong>${escapeHtml(t("builder.question", "Question"))} ${escapeHtml(question.number)}</strong>
      <span>${escapeHtml(group.problem_title)} · ${escapeHtml(sheet.name)}</span>
      <div class="builder-inspector-row">
        <span>${escapeHtml(t("builder.answer", "Answer"))}</span>
        <strong>${question.correct_answer ? circled[Number(question.correct_answer)] : escapeHtml(t("builder.unset", "Unset"))}</strong>
      </div>
      <button class="button-secondary" type="button" data-action="delete-question" data-question-id="${question.id}">
        ${escapeHtml(t("builder.delete_question", "Delete question"))}
      </button>
    </aside>
  `;
}

function sectionSelect(attributes, selectedId) {
  return `
    <select ${attributes}>
      ${state.scoring_sections
        .map(
          (section) =>
            `<option value="${section.id}" ${Number(selectedId) === section.id ? "selected" : ""}>${escapeHtml(section.name)}</option>`,
        )
        .join("")}
    </select>
  `;
}

function updateQuestion(questionId, patch) {
  const found = findQuestion(questionId);
  if (!found) return;
  Object.assign(found.question, patch);
  if (Number(found.question.choice_count) < Number(found.question.correct_answer || 0)) {
    found.question.correct_answer = null;
  }
  render();
}

function addGroup() {
  const sheet = activeSheet();
  const defaults = sheetDefaults(sheet);
  sheet.groups.push({
    id: draftId--,
    problem_no: sheet.groups.length + 1,
    problem_title: defaultProblemTitle(sheet.groups.length + 1),
    default_points: defaults.points,
    default_scoring_section_id: defaults.scoring_section_id,
    default_choice_count: defaults.choice_count,
    questions: [],
  });
  renumberSheet(sheet);
  render();
}

function deleteGroup(groupId) {
  const sheet = activeSheet();
  sheet.groups = sheet.groups.filter((group) => group.id !== groupId);
  renumberSheet(sheet);
  render();
}

function addQuestion(groupId, defaults = null) {
  const group = findGroup(groupId);
  if (!group) return;
  if (defaults) {
    setGroupDefaults(group, defaults.points, defaults.sectionId, defaults.choices);
  }
  ensureGroupDefaults(group, activeSheet());
  group.questions.push(defaultQuestion(group, String(group.questions.length + 1)));
  renumberSheet(activeSheet());
  render();
}

function deleteQuestion(questionId) {
  const sheet = activeSheet();
  for (const group of sheet.groups) {
    group.questions = group.questions.filter((question) => question.id !== questionId);
  }
  if (selectedQuestionId === questionId) selectedQuestionId = null;
  renumberSheet(sheet);
  render();
}

function defaultQuestion(group, number) {
  return {
    id: draftId--,
    number: String(number || ""),
    correct_answer: null,
    choice_count: group.default_choice_count,
    points: group.default_points,
    scoring_section_id: group.default_scoring_section_id,
  };
}

function splitGroupBeforeQuestion(groupId, questionId) {
  const sheet = activeSheet();
  const groupIndex = sheet.groups.findIndex((group) => group.id === groupId);
  if (groupIndex < 0) return;
  const group = sheet.groups[groupIndex];
  const questionIndex = group.questions.findIndex((question) => question.id === questionId);
  if (questionIndex <= 0) return;

  const movedQuestions = group.questions.splice(questionIndex);
  const newProblemNo = groupIndex + 2;
  const newGroup = {
    id: draftId--,
    problem_no: newProblemNo,
    problem_title: defaultProblemTitle(newProblemNo),
    default_points: group.default_points,
    default_scoring_section_id: group.default_scoring_section_id,
    default_choice_count: group.default_choice_count,
    questions: movedQuestions,
  };
  sheet.groups.splice(groupIndex + 1, 0, newGroup);
  renumberSheet(sheet);
  render();
}

function applyToQuestions(questions, points, sectionId, choices) {
  questions.forEach((question) => {
    question.points = points;
    question.scoring_section_id = sectionId;
    question.choice_count = choices;
    if (Number(question.correct_answer || 0) > choices) question.correct_answer = null;
  });
  render();
}

function setGroupDefaults(group, points, sectionId, choices) {
  group.default_points = points;
  group.default_scoring_section_id = sectionId;
  group.default_choice_count = choices;
}

function ensureGroupDefaults(group, sheet) {
  if (group.default_points != null && group.default_scoring_section_id != null && group.default_choice_count != null) {
    return;
  }
  const firstQuestion = group.questions && group.questions.length ? group.questions[0] : null;
  const fallback = sheetDefaultsWithoutGroup(sheet, group);
  group.default_points = group.default_points ?? (firstQuestion ? firstQuestion.points : fallback.points);
  group.default_scoring_section_id =
    group.default_scoring_section_id ?? (firstQuestion ? firstQuestion.scoring_section_id : fallback.scoring_section_id);
  group.default_choice_count =
    group.default_choice_count ?? (firstQuestion ? firstQuestion.choice_count : fallback.choice_count);
}

function sheetDefaults(sheet) {
  for (const group of sheet.groups || []) {
    if (group.default_points != null && group.default_scoring_section_id != null && group.default_choice_count != null) {
      return {
        points: group.default_points,
        scoring_section_id: group.default_scoring_section_id,
        choice_count: group.default_choice_count,
      };
    }
    if (group.questions && group.questions.length) {
      return {
        points: group.questions[0].points,
        scoring_section_id: group.questions[0].scoring_section_id,
        choice_count: group.questions[0].choice_count,
      };
    }
  }
  return appFallbackDefaults();
}

function sheetDefaultsWithoutGroup(sheet, excludedGroup) {
  const peerGroups = (sheet.groups || []).filter((group) => group !== excludedGroup);
  return sheetDefaults({ ...sheet, groups: peerGroups });
}

function appFallbackDefaults() {
  return {
    points: 1,
    scoring_section_id: firstSection() ? firstSection().id : null,
    choice_count: 4,
  };
}

function questionDiffersFromGroupDefault(group, question) {
  return (
    Number(question.points) !== Number(group.default_points) ||
    Number(question.choice_count) !== Number(group.default_choice_count) ||
    Number(question.scoring_section_id) !== Number(group.default_scoring_section_id)
  );
}

async function save() {
  statusMessage = "";
  render();
  const payload = JSON.parse(JSON.stringify(state));
  payload.sheets.forEach((sheet) => {
    sheet.groups.forEach((group) => {
      if (group.id < 0) group.id = null;
      group.questions.forEach((question) => {
        if (question.id < 0) question.id = null;
      });
    });
  });
  const response = await fetch(`/templates/${state.template.id}/builder/state`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  const data = await response.json().catch(() => ({
    error: t("builder.save_error_unexpected", "Template could not be saved because the server returned an unexpected response."),
  }));
  if (!response.ok) {
    statusType = "error";
    statusMessage = data.error || t("builder.save_error", "Template could not be saved.");
    render();
    return;
  }
  state = data.state;
  prepareState();
  statusType = "success";
  statusMessage = t("builder.saved", "Template saved.");
  render();
}

function activeSheet() {
  return state.sheets[activeSheetIndex];
}

function firstSection() {
  return state.scoring_sections.length ? state.scoring_sections[0] : null;
}

function findGroup(groupId) {
  return activeSheet().groups.find((group) => group.id === groupId);
}

function findQuestion(questionId) {
  for (const sheet of state.sheets) {
    for (const group of sheet.groups) {
      const question = group.questions.find((item) => item.id === questionId);
      if (question) return { sheet, group, question };
    }
  }
  return null;
}

function renumberSheet(sheet) {
  sheet.groups.forEach((group, groupIndex) => {
    group.problem_no = groupIndex + 1;
    if (!group.problem_title) group.problem_title = defaultProblemTitle(group.problem_no);
    group.questions.forEach((question, questionIndex) => {
      if (question.number == null || String(question.number).trim() === "") {
        question.number = String(questionIndex + 1);
      } else {
        question.number = String(question.number);
      }
    });
  });
}

function paperTitle(sheet) {
  const parts = [state.template.name];
  parts.push(sheet.name);
  return parts.join(" · ");
}

function escapeHtml(value) {
  return String(value == null ? "" : value)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#039;");
}

function escapeAttr(value) {
  return escapeHtml(value);
}

document.addEventListener("click", (event) => {
  const target = event.target.closest("[data-action], [data-builder-save]");
  if (!target || !root || !root.contains(target)) {
    if (target && target.matches("[data-builder-save]")) save();
    return;
  }
  const action = target.dataset.action;
  if (target.matches("[data-builder-save]")) save();
  if (action === "switch-sheet") {
    activeSheetIndex = Number(target.dataset.index);
    selectedQuestionId = null;
    render();
  }
  if (action === "add-group") addGroup();
  if (action === "delete-group") deleteGroup(Number(target.dataset.groupId));
  if (action === "add-question") {
    const panel = target.closest(".builder-problem");
    addQuestion(Number(target.dataset.groupId), defaultsFromGroupPanel(panel));
  }
  if (action === "split-group") {
    splitGroupBeforeQuestion(Number(target.dataset.groupId), Number(target.dataset.questionId));
  }
  if (action === "delete-question") deleteQuestion(Number(target.dataset.questionId));
  if (action === "select-question") {
    selectedQuestionId = Number(target.dataset.questionId);
    render();
  }
  if (action === "set-answer") {
    selectedQuestionId = Number(target.dataset.questionId);
    updateQuestion(selectedQuestionId, { correct_answer: target.dataset.answer });
  }
  if (action === "apply-group") {
    const group = findGroup(Number(target.dataset.groupId));
    const panel = target.closest(".builder-problem");
    const points = Number(panel.querySelector("[data-group-points]").value || 0);
    const sectionId = Number(panel.querySelector("[data-group-section]").value);
    const choices = Number(panel.querySelector("[data-group-choices]").value || 4);
    setGroupDefaults(group, points, sectionId, choices);
    applyToQuestions(group.questions, points, sectionId, choices);
  }
  if (action === "apply-sheet") {
    const toolbar = target.closest(".builder-toolbar");
    const points = Number(toolbar.querySelector("[data-sheet-points]").value || 0);
    const sectionId = Number(toolbar.querySelector("[data-sheet-section]").value);
    const choices = Number(toolbar.querySelector("[data-sheet-choices]").value || 4);
    activeSheet().groups.forEach((group) => setGroupDefaults(group, points, sectionId, choices));
    applyToQuestions(activeSheet().groups.flatMap((group) => group.questions), points, sectionId, choices);
  }
});

function defaultsFromGroupPanel(panel) {
  return {
    points: Number(panel.querySelector("[data-group-points]").value || 0),
    sectionId: Number(panel.querySelector("[data-group-section]").value),
    choices: Number(panel.querySelector("[data-group-choices]").value || 4),
  };
}

document.addEventListener("input", (event) => {
  const target = event.target;
  if (!root || !root.contains(target)) return;
  if (target.dataset.action === "sheet-name") {
    activeSheet().name = target.value;
    const tab = root.querySelector(`[data-sheet-tab-label="${activeSheetIndex}"]`);
    const title = root.querySelector("[data-paper-sheet-title]");
    if (tab) tab.textContent = target.value;
    if (title) title.textContent = paperTitle(activeSheet());
  }
  if (target.dataset.action === "group-title") {
    const group = findGroup(Number(target.dataset.groupId));
    if (group) group.problem_title = target.value;
  }
  if (target.dataset.action === "question-points") {
    updateQuestion(Number(target.dataset.questionId), { points: Number(target.value || 0) });
  }
  if (target.dataset.action === "question-choices") {
    updateQuestion(Number(target.dataset.questionId), {
      choice_count: Number(target.value || 4),
    });
  }
  if (target.dataset.action === "question-number") {
    const found = findQuestion(Number(target.dataset.questionId));
    if (found) found.question.number = target.value;
  }
});

document.addEventListener("change", (event) => {
  const target = event.target;
  if (!root || !root.contains(target)) return;
  if (target.dataset.action === "question-section") {
    updateQuestion(Number(target.dataset.questionId), {
      scoring_section_id: Number(target.value),
    });
  }
});

init();
