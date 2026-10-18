// JS-driven peer-review builder (session + template).
//
// All paper geometry comes from the server layout endpoint (the same layout service
// used by PDF generation and scanning) so the preview can never drift from the printed
// sheet. The preview is the editing surface: a faithful SVG render of the sheet with a
// thin HTML overlay that lets the teacher rename / add / reorder / delete competency
// columns and edit furigana directly on the paper. This module only renders that geometry
// and persists edits via the existing endpoints; it never recomputes bubble/column math.

let root = null;
let base = "";
let state = null;
let layout = null;
let activePage = 0;
// Competency id whose on-sheet editor card is currently open (null = none).
let editingCompId = null;
const timers = {};

const I18N = window.PR_I18N || {};

function t(key) {
  return typeof I18N[key] === "string" ? I18N[key] : key;
}

function escapeHtml(value) {
  return String(value == null ? "" : value)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

function attr(value) {
  return escapeHtml(value);
}

function debounce(key, fn, delay = 500) {
  if (timers[key]) clearTimeout(timers[key]);
  timers[key] = setTimeout(fn, delay);
}

function setStatus(text, isError = false) {
  const el = document.getElementById("pr-save-status");
  if (!el) return;
  el.textContent = text || "";
  el.classList.toggle("is-error", Boolean(isError));
}

function readJsonScript(id) {
  const el = document.getElementById(id);
  if (!el) return null;
  try {
    return JSON.parse(el.textContent);
  } catch (err) {
    return null;
  }
}

// ---- API helpers -----------------------------------------------------------

async function getJson(url) {
  const res = await fetch(url, { headers: { Accept: "application/json" } });
  return res.ok ? res.json() : null;
}

async function postForm(url, data) {
  const body = new URLSearchParams();
  Object.entries(data).forEach(([key, value]) => body.append(key, value));
  const res = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
    body,
    redirect: "manual",
  });
  if (res.type === "opaqueredirect" || res.status === 0 || res.ok) return { ok: true };
  let detail = "";
  try {
    detail = (await res.json()).detail || "";
  } catch (err) {
    /* ignore */
  }
  return { ok: false, detail };
}

async function postJson(url, data) {
  const res = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json", Accept: "application/json" },
    body: JSON.stringify(data),
  });
  if (res.ok) return { ok: true, data: await res.json() };
  let detail = "";
  try {
    detail = (await res.json()).detail || "";
  } catch (err) {
    /* ignore */
  }
  return { ok: false, detail };
}

async function refreshState() {
  const [nextState, nextLayout] = await Promise.all([
    getJson(`${base}/builder/state`),
    getJson(`${base}/layout`),
  ]);
  if (nextState) state = nextState;
  if (nextLayout) layout = nextLayout;
  // Drop an open editor whose column no longer exists (e.g. after delete).
  if (editingCompId != null && !(state.competencies || []).some((c) => c.id === editingCompId)) {
    editingCompId = null;
  }
  render();
}

async function refreshLayout() {
  // Light refresh used while typing: re-render only the faithful sheet + overlay so the
  // preview tracks the edit, leaving the open editor card (and its focus) untouched.
  const nextLayout = await getJson(`${base}/layout`);
  if (nextLayout) layout = nextLayout;
  renderCanvasPreviewOnly();
}

// ---- Faithful sheet (SVG) --------------------------------------------------

function buildSvg(L) {
  const sep = (L.column_separators_mm || [])
    .map(
      (x) =>
        `<line x1="${x}" y1="${L.header_top_mm}" x2="${x}" y2="${L.table_bottom_mm}" stroke="#9aa7b4" stroke-width="0.3"/>`
    )
    .join("");
  const markers = Object.values(L.marker_positions || {})
    .map((m) => `<rect x="${m.x_mm}" y="${m.y_mm}" width="6" height="6" fill="#111"/>`)
    .join("");
  const columns = (L.competency_columns || [])
    .map((col) => {
      // Per-segment ruby (positions resolved server-side) drawn above each base run; falls
      // back to one whole-title furigana when no per-segment ruby is set. Mirrors the PDF.
      let furi = "";
      if (col.ruby_positions && col.ruby_positions.length) {
        furi = col.ruby_positions
          .map(
            (r) =>
              `<text x="${r.x_mm}" y="${r.y_mm}" font-size="2" text-anchor="middle" fill="#667">${escapeHtml(String(r.reading).slice(0, 16))}</text>`
          )
          .join("");
      } else if (col.name_furigana) {
        furi = `<text x="${col.center_x_mm}" y="${L.furigana_y_mm}" font-size="2" text-anchor="middle" fill="#667">${escapeHtml(col.name_furigana.slice(0, 20))}</text>`;
      }
      // Auto-fit title: render the layout-chosen 1-2 lines at the chosen size (pt->mm) so
      // the preview wraps/shrinks exactly like the PDF. No score legend row.
      const fitFont = (col.header_font_pt || 8) * 0.3528;
      const lines = col.header_lines && col.header_lines.length ? col.header_lines : [col.name];
      const lineGap = fitFont * 1.05;
      const name = lines
        .map((line, i) => {
          const dy = (i - (lines.length - 1) / 2) * lineGap;
          return `<text x="${col.center_x_mm}" y="${L.name_y_mm + dy}" font-size="${fitFont}" font-weight="700" text-anchor="middle" fill="#222">${escapeHtml(line)}</text>`;
        })
        .join("");
      return `${furi}${name}`;
    })
    .join("");
  // Very light alternating row shading (~#F6F6F6), matching ROW_STRIPE_GRAY in the PDF.
  // Drawn under the grid lines, names and bubbles so it never hides a mark.
  const stripes = (L.target_rows || [])
    .map((row, idx) => {
      if (idx % 2 !== 1) return "";
      const top = row.row_top_mm;
      const bottom = row.row_bottom_mm;
      if (top == null || bottom == null) return "";
      return `<rect x="${L.table_left_mm}" y="${top}" width="${L.table_right_mm - L.table_left_mm}" height="${bottom - top}" fill="#f6f6f6" stroke="none"/>`;
    })
    .join("");
  // Use the server-computed row geometry (row_center_y_mm / separator_y_mm) directly so
  // the preview can never drift from the printed sheet. Never re-derive row math here.
  const lastRowIndex = (L.target_rows || []).length - 1;
  const rows = (L.target_rows || [])
    .map((row, idx) => {
      const cy = row.row_center_y_mm != null ? row.row_center_y_mm : row.y_mm;
      const sepY = row.separator_y_mm != null ? row.separator_y_mm : cy + 4;
      const sepLine =
        idx < lastRowIndex
          ? `<line x1="${L.table_left_mm}" y1="${sepY}" x2="${L.table_right_mm}" y2="${sepY}" stroke="#eef0f2" stroke-width="0.2"/>`
          : "";
      return `<text x="${L.target_column.x_mm}" y="${cy + 1}" font-size="2.8" fill="#111">${escapeHtml(String(row.display_label).slice(0, 26))}</text>${sepLine}`;
    })
    .join("");
  const bubbles = (L.rating_bubbles || [])
    .map(
      (b) =>
        `<circle cx="${b.x_mm}" cy="${b.y_mm}" r="${b.radius_mm}" fill="#ffffff" stroke="#9aa7b4" stroke-width="0.3"/>` +
        `<text x="${b.x_mm}" y="${b.y_mm + 1}" font-size="2.1" text-anchor="middle" fill="#9aa7b4">${b.score}</text>`
    )
    .join("");
  const title = escapeHtml(String(state.form.title || "").slice(0, 60));
  const qr = L.qr || { x_mm: 168, y_mm: 14, size_mm: 26 };
  // Keep the title clear of the top-left detection marker (same content-left the PDF uses).
  const contentLeft = L.content_left_mm != null ? L.content_left_mm : 22;
  return `<svg class="pr-paper" viewBox="0 0 ${L.page_width_mm} ${L.page_height_mm}" preserveAspectRatio="xMidYMid meet" role="img" aria-label="${attr(title)}">
    <rect x="0" y="0" width="${L.page_width_mm}" height="${L.page_height_mm}" fill="#ffffff" stroke="#d0d7de" stroke-width="0.4"/>
    ${markers}
    <text x="${contentLeft}" y="18" font-size="5" font-weight="700" fill="#111">${title}</text>
    <rect x="${qr.x_mm}" y="${qr.y_mm}" width="${qr.size_mm}" height="${qr.size_mm}" fill="none" stroke="#bbb" stroke-width="0.4"/>
    <text x="${qr.x_mm + qr.size_mm / 2}" y="${qr.y_mm + qr.size_mm / 2}" font-size="3" text-anchor="middle" fill="#bbb">QR</text>
    <rect x="${L.table_left_mm}" y="${L.header_top_mm}" width="${L.table_right_mm - L.table_left_mm}" height="${L.table_top_mm - L.header_top_mm}" fill="#f3f6fa" stroke="none"/>
    ${stripes}
    ${sep}
    <line x1="${L.table_left_mm}" y1="${L.header_top_mm}" x2="${L.table_right_mm}" y2="${L.header_top_mm}" stroke="#9aa7b4" stroke-width="0.3"/>
    <line x1="${L.table_left_mm}" y1="${L.table_top_mm}" x2="${L.table_right_mm}" y2="${L.table_top_mm}" stroke="#9aa7b4" stroke-width="0.35"/>
    <line x1="${L.table_left_mm}" y1="${L.table_bottom_mm}" x2="${L.table_right_mm}" y2="${L.table_bottom_mm}" stroke="#9aa7b4" stroke-width="0.3"/>
    <text x="${L.target_column.x_mm}" y="${L.name_y_mm}" font-size="3.2" font-weight="700" fill="#333">${escapeHtml(t("target"))}</text>
    ${columns}
    ${rows}
    ${bubbles}
  </svg>`;
}

// ---- Interactive overlay (edit columns directly on the sheet) --------------

function pct(value, total) {
  return total ? (value / total) * 100 : 0;
}

function overlayHtml(L) {
  // Transparent hit-regions over each competency header (click to open its editor) plus an
  // "add column" affordance in the right margin. Positioned by the same mm coordinates the
  // SVG uses, so the controls always sit exactly over their columns.
  const pw = L.page_width_mm;
  const ph = L.page_height_mm;
  const top = pct(L.header_top_mm, ph);
  const height = pct(L.table_top_mm - L.header_top_mm, ph);
  const cols = (L.competency_columns || [])
    .map((col) => {
      const left = pct(col.x_mm, pw);
      const width = pct(col.width_mm, pw);
      const active = editingCompId === col.competency_id ? " is-active" : "";
      return `<button type="button" class="pr-col-hit${active}" data-action="col-edit" data-id="${col.competency_id}" style="left:${left}%;top:${top}%;width:${width}%;height:${height}%" title="${attr(t("edit_title_hint"))}" aria-label="${attr(t("edit_title_hint"))}"><span class="pr-col-ico" aria-hidden="true">✎</span></button>`;
    })
    .join("");
  const addLeft = pct(L.table_right_mm + 1.5, pw);
  const addBtn = `<button type="button" class="pr-col-add" data-action="col-add" style="left:${addLeft}%;top:${top}%;height:${height}%" title="${attr(t("add_column"))}" aria-label="${attr(t("add_column"))}">＋</button>`;
  return cols + addBtn;
}

function colEditorHtml(L) {
  if (editingCompId == null) return "";
  const comps = state.competencies || [];
  const comp = comps.find((c) => c.id === editingCompId);
  if (!comp) return "";
  const index = comps.findIndex((c) => c.id === editingCompId);
  const col = (L.competency_columns || []).find((c) => c.competency_id === editingCompId);
  // Anchor the card under the column header; clamp so it stays on-screen for far-right columns.
  const left = col ? Math.min(pct(col.x_mm, L.page_width_mm), 62) : 18;
  const top = pct(L.table_top_mm, L.page_height_mm);
  const tooLong = tooLongCompetencyIds().has(comp.id)
    ? `<p class="pr-item-warn">⚠ ${escapeHtml(t("warn_text_too_long"))}</p>`
    : "";
  return `<div class="pr-col-card" style="left:${left}%;top:${top}%" role="dialog" aria-label="${attr(t("competency"))}">
    <div class="pr-col-card-head">
      <span class="pr-col-card-title">${escapeHtml(t("competency"))}</span>
      <div class="pr-col-card-tools">
        <button class="btn btn-ghost btn-sm" type="button" data-action="comp-move" data-id="${comp.id}" data-dir="up" title="${attr(t("move_up"))}" aria-label="${attr(t("move_up"))}" ${index <= 0 ? "disabled" : ""}>◀</button>
        <button class="btn btn-ghost btn-sm" type="button" data-action="comp-move" data-id="${comp.id}" data-dir="down" title="${attr(t("move_down"))}" aria-label="${attr(t("move_down"))}" ${index >= comps.length - 1 ? "disabled" : ""}>▶</button>
        <button class="btn btn-ghost btn-sm is-danger" type="button" data-action="comp-delete" data-id="${comp.id}" title="${attr(t("delete"))}" aria-label="${attr(t("delete"))}">🗑</button>
        <button class="btn btn-ghost btn-sm" type="button" data-action="col-close" title="${attr(t("close"))}" aria-label="${attr(t("close"))}">✕</button>
      </div>
    </div>
    <label class="form-label">${escapeHtml(t("competency_name"))}</label>
    <input class="form-input pr-col-name" data-action="comp-name" data-id="${comp.id}" value="${attr(comp.name)}">
    <label class="form-label">${escapeHtml(t("furigana_label"))}</label>
    <input class="form-input pr-furigana" data-action="comp-furigana" data-id="${comp.id}" placeholder="${attr(t("furigana_placeholder"))}" value="${attr(comp.name_furigana)}">
    ${rubyEditorHtml(comp)}
    <label class="form-label">${escapeHtml(t("short_label_label"))}</label>
    <input class="form-input" data-action="comp-short" data-id="${comp.id}" placeholder="${attr(t("short_label_placeholder"))}" value="${attr(comp.short_label)}">
    ${tooLong}
  </div>`;
}

function focusEditorTitle() {
  const el = document.querySelector('#pr-col-editor [data-action="comp-name"]');
  if (el) {
    el.focus();
    el.select();
  }
}

function activeLayout() {
  const pages = (layout && layout.pages) || [];
  if (activePage >= pages.length) activePage = 0;
  const page = pages[activePage];
  return page ? page.layout : null;
}

function pageTabsHtml() {
  const pages = (layout && layout.pages) || [];
  if (pages.length <= 1) return "";
  const tabs = pages
    .map(
      (page, index) =>
        `<button type="button" class="pr-tab ${index === activePage ? "is-active" : ""}" data-action="page-tab" data-index="${index}">${escapeHtml(
          fmt(t("page_tab"), { count: page.page_no })
        )}</button>`
    )
    .join("");
  return `<div class="pr-tabs" id="pr-page-tabs">${tabs}</div>`;
}

function renderCanvas() {
  const host = document.getElementById("pr-canvas");
  if (!host) return;
  const L = activeLayout();
  const aspect = L ? `${L.page_width_mm} / ${L.page_height_mm}` : "297 / 210";
  host.innerHTML = `${pageTabsHtml()}
    <div class="pr-paper-host" id="pr-paper-host" style="aspect-ratio:${aspect}">
      <div class="pr-svg-layer" id="pr-svg-layer">${L ? buildSvg(L) : ""}</div>
      <div class="pr-overlay" id="pr-overlay">${L ? overlayHtml(L) : ""}</div>
      <div class="pr-col-editor" id="pr-col-editor">${L ? colEditorHtml(L) : ""}</div>
    </div>
    <p class="muted text-xs pr-canvas-note">${escapeHtml(t("preview_note"))}</p>`;
}

function renderCanvasPreviewOnly() {
  const L = activeLayout();
  const svgLayer = document.getElementById("pr-svg-layer");
  const overlay = document.getElementById("pr-overlay");
  if (!svgLayer || !overlay) {
    renderCanvas();
    return;
  }
  svgLayer.innerHTML = L ? buildSvg(L) : "";
  overlay.innerHTML = L ? overlayHtml(L) : "";
  // The open editor card (#pr-col-editor) is intentionally left untouched so typing keeps focus.
}

// ---- Shared helpers --------------------------------------------------------

function pageBreakTargetIds() {
  // First target on every page after the first marks a manual/auto page break.
  const breaks = new Set();
  (state.pages || []).forEach((page, index) => {
    if (index > 0 && page.target_ids.length) breaks.add(page.target_ids[0]);
  });
  return breaks;
}

function fmt(template, params) {
  return String(template).replace(/\{(\w+)\}/g, (_m, key) =>
    params[key] === undefined ? "" : params[key]
  );
}

function orientationOption(value, current) {
  const label = { auto: "orientation_auto", portrait: "orientation_portrait", landscape: "orientation_landscape" }[value];
  return `<option value="${value}" ${current === value ? "selected" : ""}>${escapeHtml(t(label))}</option>`;
}

function densityOption(value, current) {
  const label = { standard: "density_standard", compact: "density_compact", relaxed: "density_relaxed" }[value];
  return `<option value="${value}" ${current === value ? "selected" : ""}>${escapeHtml(t(label))}</option>`;
}

function tooLongCompetencyIds() {
  // Competency ids whose printed title overflows even at the minimum font, gathered from
  // the layout the preview/PDF share, so the warning matches the printed sheet.
  const ids = new Set();
  ((layout && layout.pages) || []).forEach((page) => {
    ((page.layout && page.layout.competency_columns) || []).forEach((col) => {
      if (col.header_too_long) ids.add(col.competency_id);
    });
  });
  return ids;
}

function pageSplitHtml() {
  // The real, current split (e.g. "Page 1: 20 · Page 2: 2") read straight from the
  // paginated pages so the teacher sees exactly how rows land on paper.
  const pages = (state.pages || []).filter((p) => (p.target_ids || []).length);
  if (pages.length < 2) return "";
  const parts = pages
    .map((p) => escapeHtml(fmt(t("split_item"), { page: p.page_no, count: p.target_ids.length })))
    .join(" · ");
  return `<p class="pr-plan-split">${escapeHtml(t("split_label"))}: ${parts}</p>`;
}

function planFixHints() {
  // Concrete, specific fixes for raising rows-per-page, derived from the plan's "what if"
  // capacities plus the long-title items the preview/PDF already flag. Only shown when the
  // layout was reduced or does not fit, so a comfortable layout stays uncluttered.
  const plan = state.page_plan;
  const hints = [];
  const actual = plan.students_per_page;
  if (plan.has_furigana && plan.students_per_page_no_furigana > actual) {
    hints.push(fmt(t("fix_hide_furigana"), { count: plan.students_per_page_no_furigana }));
  }
  if (state.form.layout_density !== "compact" && plan.students_per_page_compact > actual) {
    hints.push(fmt(t("fix_use_compact"), { count: plan.students_per_page_compact }));
  }
  if (tooLongCompetencyIds().size) {
    hints.push(t("fix_short_label"));
  }
  if (actual < plan.max_students_per_page) {
    hints.push(fmt(t("fix_set_per_page"), { count: actual }));
  }
  return hints;
}

function planSummaryHtml() {
  const plan = state.page_plan;
  if (!plan) return "";
  const willBe = plan.orientation === "landscape" ? t("will_be_landscape") : t("will_be_portrait");
  const resolved =
    state.form.page_orientation_mode === "auto" ? `${t("auto_prefix")}${willBe}` : willBe;
  const perPage = fmt(t("per_page_label"), { max: plan.max_students_per_page, actual: plan.students_per_page });
  const reduced = (plan.layout_warnings || []).some((w) => w.code === "per_page_reduced");
  const doesNotFit = (plan.layout_warnings || []).some((w) => w.code === "does_not_fit");
  const warnings = (plan.layout_warnings || [])
    .map((w) => {
      if (w.code === "per_page_reduced") return fmt(t("warn_reduced"), { max: w.max, actual: w.actual });
      if (w.code === "does_not_fit") return t("warn_does_not_fit");
      return "";
    })
    .filter(Boolean)
    .map((text) => `<p class="pr-plan-warn">⚠ ${escapeHtml(text)}</p>`)
    .join("");
  // Offer fixes when rows were reduced (vertical levers). Width-only "does not fit" already
  // has its own targeted message (reduce competencies / scale), so don't double up there.
  const hints = reduced && !doesNotFit ? planFixHints() : [];
  const hintsHtml = hints.length
    ? `<div class="pr-plan-hints"><p class="pr-plan-hints-head">${escapeHtml(t("fix_intro"))}</p><ul>${hints
        .map((h) => `<li>${escapeHtml(h)}</li>`)
        .join("")}</ul></div>`
    : "";
  return `<div class="pr-plan ${plan.fits ? "" : "is-error"}">
    <p class="pr-plan-result">${escapeHtml(resolved)}</p>
    <p class="pr-plan-perpage">${escapeHtml(perPage)}</p>
    ${pageSplitHtml()}
    ${warnings}
    ${hintsHtml}
  </div>`;
}

function rubyEditorHtml(comp) {
  // Per-segment ruby furigana editor: a list of base→reading rows plus an add row. The
  // base text must appear in the competency name; the server positions each reading above
  // its run in the printed title. Suppressed visually-only note when a short label is set
  // (per-segment ruby does not apply to the short label).
  const ruby = comp.name_ruby || [];
  const rows = ruby
    .map(
      (seg, index) => `<div class="pr-ruby-row">
        <input class="form-input pr-ruby-base" data-action="comp-ruby-base" data-id="${comp.id}" data-index="${index}" value="${attr(seg.base_text)}" placeholder="${attr(t("ruby_base_placeholder"))}">
        <span class="pr-ruby-arrow" aria-hidden="true">→</span>
        <input class="form-input pr-ruby-reading" data-action="comp-ruby-reading" data-id="${comp.id}" data-index="${index}" value="${attr(seg.reading)}" placeholder="${attr(t("ruby_reading_placeholder"))}">
        <button class="btn btn-ghost btn-sm is-danger" type="button" data-action="comp-ruby-delete" data-id="${comp.id}" data-index="${index}" aria-label="${attr(t("delete"))}">✕</button>
      </div>`
    )
    .join("");
  const note = comp.short_label
    ? `<p class="pr-ruby-note">${escapeHtml(t("ruby_short_label_note"))}</p>`
    : "";
  return `<details class="pr-ruby" ${ruby.length ? "open" : ""}>
      <summary class="form-label pr-ruby-summary">${escapeHtml(t("ruby_label"))}${ruby.length ? ` (${ruby.length})` : ""}</summary>
      <div class="pr-ruby-body">
        ${note}
        <div class="pr-ruby-rows">${rows}</div>
        <div class="pr-ruby-add">
          <input class="form-input pr-ruby-base" data-add-ruby-base data-id="${comp.id}" placeholder="${attr(t("ruby_base_placeholder"))}">
          <span class="pr-ruby-arrow" aria-hidden="true">→</span>
          <input class="form-input pr-ruby-reading" data-add-ruby-reading data-id="${comp.id}" placeholder="${attr(t("ruby_reading_placeholder"))}">
          <button class="btn btn-secondary btn-sm" type="button" data-action="comp-ruby-add" data-id="${comp.id}">${escapeHtml(t("ruby_add"))}</button>
        </div>
      </div>
    </details>`;
}

function rubyFromDom(id, skipIndex) {
  // Collect the current base→reading rows for a competency from the DOM, dropping the row
  // at ``skipIndex`` (used when deleting) and any incomplete rows.
  const rows = [];
  root
    .querySelectorAll(`[data-action="comp-ruby-base"][data-id="${id}"]`)
    .forEach((baseEl) => {
      const index = baseEl.dataset.index;
      if (skipIndex != null && String(index) === String(skipIndex)) return;
      const readingEl = root.querySelector(
        `[data-action="comp-ruby-reading"][data-id="${id}"][data-index="${index}"]`
      );
      const baseVal = baseEl.value.trim();
      const reading = readingEl ? readingEl.value.trim() : "";
      if (baseVal && reading) rows.push({ base_text: baseVal, reading });
    });
  return rows;
}

// ---- Toolbar (settings) ----------------------------------------------------

function renderToolbar() {
  const el = document.getElementById("pr-toolbar");
  if (!el) return;
  const f = state.form;
  el.innerHTML = `<div class="pr-toolbar-fields">
      <div class="form-field pr-tb-title">
        <label class="form-label">${escapeHtml(t("title"))}</label>
        <input class="form-input" data-field="title" value="${attr(f.title)}">
      </div>
      <div class="form-field pr-tb-num">
        <label class="form-label">${escapeHtml(t("score_min"))}</label>
        <input class="form-input" type="number" min="0" max="9" data-field="score_min" value="${attr(f.score_min)}">
      </div>
      <div class="form-field pr-tb-num">
        <label class="form-label">${escapeHtml(t("score_max"))}</label>
        <input class="form-input" type="number" min="1" max="9" data-field="score_max" value="${attr(f.score_max)}">
      </div>
      <div class="form-field pr-tb-num">
        <label class="form-label">${escapeHtml(t("students_per_page_short"))}</label>
        <input class="form-input" type="number" min="1" max="40" data-field="students_per_page" value="${attr(f.students_per_page)}">
      </div>
      <div class="form-field">
        <label class="form-label">${escapeHtml(t("orientation"))}</label>
        <select class="form-select" data-field="page_orientation_mode">
          ${orientationOption("auto", f.page_orientation_mode)}
          ${orientationOption("portrait", f.page_orientation_mode)}
          ${orientationOption("landscape", f.page_orientation_mode)}
        </select>
      </div>
      <div class="form-field">
        <label class="form-label">${escapeHtml(t("density"))}</label>
        <select class="form-select" data-field="layout_density">
          ${densityOption("standard", f.layout_density)}
          ${densityOption("compact", f.layout_density)}
          ${densityOption("relaxed", f.layout_density)}
        </select>
      </div>
    </div>
    ${planSummaryHtml()}`;
}

// ---- Side panel (roster + rater groups) ------------------------------------

function templateNoteCard() {
  // Templates have no real roster; targets/pages belong to sessions created later.
  return `<section class="panel">
    <div class="panel-header"><h2>${escapeHtml(t("roster"))}</h2></div>
    <div class="panel-body">
      <p class="pr-empty">${escapeHtml(t("template_roster_note"))}</p>
    </div>
  </section>`;
}

function targetsCard() {
  const breaks = pageBreakTargetIds();
  if (!state.targets || !state.targets.length) {
    return `<section class="panel">
      <div class="panel-header"><h2>${escapeHtml(t("roster"))}</h2></div>
      <div class="panel-body">
        <p class="pr-empty">${escapeHtml(t("no_targets"))}</p>
        <a class="btn btn-primary btn-sm" href="${attr(t("students_url"))}">${escapeHtml(t("add_students"))}</a>
      </div>
    </section>`;
  }
  const rows = state.targets
    .map((target, index) => {
      const divider = breaks.has(target.id) ? `<div class="pr-pagebreak"></div>` : "";
      const breakBtn =
        index > 0 && target.active
          ? `<button class="btn btn-ghost btn-sm" type="button" data-action="page-break" data-id="${target.id}" title="${attr(t("page_break_before"))}">⤵</button>`
          : "";
      return `${divider}<div class="pr-item">
        <input class="form-input" data-action="target-label" data-id="${target.id}" value="${attr(target.display_label)}">
        <select class="form-select" data-action="target-active" data-id="${target.id}">
          <option value="true" ${target.active ? "selected" : ""}>${escapeHtml(t("included"))}</option>
          <option value="false" ${!target.active ? "selected" : ""}>${escapeHtml(t("excluded"))}</option>
        </select>
        ${breakBtn}
      </div>`;
    })
    .join("");
  return `<details class="panel" open>
    <summary class="panel-header"><h2>${escapeHtml(t("roster"))}</h2></summary>
    <div class="panel-body">
      <p class="pr-empty pr-session-note">${escapeHtml(t("session_roster_note"))}</p>
      <div class="pr-item-list">${rows}</div>
      <div class="pr-add-row">
        <button class="btn btn-secondary btn-sm" type="button" data-action="auto-paginate">${escapeHtml(t("auto_paginate"))}</button>
        <button class="btn btn-secondary btn-sm" type="button" data-action="remove-breaks">${escapeHtml(t("remove_page_breaks"))}</button>
      </div>
    </div>
  </details>`;
}

function groupsCard() {
  const items = (state.rater_groups || [])
    .map(
      (group) => `<div class="pr-item">
        <input class="form-input" data-action="group-name" data-id="${group.id}" value="${attr(group.name)}">
        <input class="form-input pr-weight" type="number" min="0" max="100" step="0.1" data-action="group-weight" data-id="${group.id}" value="${attr(group.weight_percent)}" title="${attr(t("weight_percent"))}">
        <select class="form-select" data-action="group-active" data-id="${group.id}">
          <option value="true" ${group.active ? "selected" : ""}>${escapeHtml(t("active"))}</option>
          <option value="false" ${!group.active ? "selected" : ""}>${escapeHtml(t("inactive"))}</option>
        </select>
        <button class="btn btn-ghost btn-sm is-danger" type="button" data-action="group-delete" data-id="${group.id}" aria-label="${attr(t("delete"))}">✕</button>
      </div>`
    )
    .join("");
  const list = items || `<p class="pr-empty">${escapeHtml(t("no_groups"))}</p>`;
  return `<details class="panel pr-advanced">
    <summary class="panel-header"><h2>${escapeHtml(t("advanced_settings"))} · ${escapeHtml(t("rater_groups"))}</h2></summary>
    <div class="panel-body">
      <div class="pr-item-list">${list}</div>
      <div class="pr-add-row">
        <input class="form-input" data-add="group-name" placeholder="${attr(t("rater_group"))}">
        <input class="form-input pr-weight" type="number" min="0" max="100" step="0.1" data-add="group-weight" value="100">
        <button class="btn btn-secondary btn-sm" type="button" data-action="group-add">${escapeHtml(t("add_group"))}</button>
      </div>
    </div>
  </details>`;
}

function renderSide() {
  const el = document.getElementById("pr-side");
  if (!el) return;
  const rosterCard = state.is_template ? templateNoteCard() : targetsCard();
  el.innerHTML = rosterCard + groupsCard();
}

// ---- Top-level render ------------------------------------------------------

function render() {
  renderToolbar();
  renderCanvas();
  renderSide();
}

// ---- Event handling --------------------------------------------------------

function competencyPayload(id, rubyOverride) {
  const name = root.querySelector(`[data-action="comp-name"][data-id="${id}"]`);
  const furi = root.querySelector(`[data-action="comp-furigana"][data-id="${id}"]`);
  const short = root.querySelector(`[data-action="comp-short"][data-id="${id}"]`);
  const ruby = rubyOverride != null ? rubyOverride : rubyFromDom(id);
  return {
    name: name ? name.value : "",
    name_furigana: furi ? furi.value : "",
    short_label: short ? short.value : "",
    name_ruby_json: JSON.stringify(ruby),
    description: "",
  };
}

function targetPayload(id) {
  const label = root.querySelector(`[data-action="target-label"][data-id="${id}"]`);
  const active = root.querySelector(`[data-action="target-active"][data-id="${id}"]`);
  return {
    display_label: label ? label.value : "",
    active: active ? active.value : "true",
  };
}

function groupPayload(id) {
  const name = root.querySelector(`[data-action="group-name"][data-id="${id}"]`);
  const weight = root.querySelector(`[data-action="group-weight"][data-id="${id}"]`);
  const active = root.querySelector(`[data-action="group-active"][data-id="${id}"]`);
  return {
    name: name ? name.value : "",
    weight_percent: weight ? weight.value : "0",
    active: active ? active.value : "true",
    description: "",
  };
}

async function run(promise, { full = true } = {}) {
  setStatus(t("saving"));
  const result = await promise;
  if (!result.ok) {
    setStatus(result.detail || t("save_error"), true);
    return false;
  }
  setStatus(t("saved"));
  if (full) await refreshState();
  else await refreshLayout();
  return true;
}

async function addColumn() {
  setStatus(t("saving"));
  const res = await postForm(`${base}/competencies`, {
    name: t("new_item_default"),
    name_furigana: "",
    short_label: "",
    description: "",
  });
  if (!res.ok) {
    setStatus(res.detail || t("save_error"), true);
    return;
  }
  setStatus(t("saved"));
  await refreshState();
  // Open the freshly added column (highest display_order -> last) for immediate rename.
  const comps = state.competencies || [];
  if (comps.length) {
    editingCompId = comps[comps.length - 1].id;
    renderCanvas();
    focusEditorTitle();
  }
}

async function saveSettings() {
  const prev = {
    score_min: state.form.score_min,
    score_max: state.form.score_max,
    students_per_page: state.form.students_per_page,
    page_orientation_mode: state.form.page_orientation_mode,
    layout_density: state.form.layout_density,
  };
  const payload = {
    title: root.querySelector('[data-field="title"]').value,
    score_min: Number(root.querySelector('[data-field="score_min"]').value),
    score_max: Number(root.querySelector('[data-field="score_max"]').value),
    students_per_page: Number(root.querySelector('[data-field="students_per_page"]').value),
    page_orientation_mode: root.querySelector('[data-field="page_orientation_mode"]').value,
    layout_density: root.querySelector('[data-field="layout_density"]').value,
  };
  setStatus(t("saving"));
  const res = await postJson(`${base}/builder/state`, payload);
  if (!res.ok) {
    setStatus(res.detail || t("save_error"), true);
    return;
  }
  if (res.data) state = res.data;
  setStatus(t("saved"));
  // Score scale, students-per-page, or orientation can change how many rows fit, so
  // re-paginate (best effort; ignored if the layout is locked) and refresh fully.
  const layoutAffecting =
    payload.score_min !== prev.score_min ||
    payload.score_max !== prev.score_max ||
    payload.students_per_page !== prev.students_per_page ||
    payload.page_orientation_mode !== prev.page_orientation_mode ||
    payload.layout_density !== prev.layout_density;
  if (layoutAffecting) {
    if (!state.is_template) {
      await postForm(`${base}/pages/auto-paginate`, {});
    }
    await refreshState();
  } else {
    await refreshLayout();
  }
}

function onInput(event) {
  const el = event.target;
  if (el.dataset.field) {
    if (el.tagName === "SELECT") return; // selects save on change, not input
    debounce("settings", saveSettings);
  } else if (
    el.dataset.action === "comp-name" ||
    el.dataset.action === "comp-furigana" ||
    el.dataset.action === "comp-short" ||
    el.dataset.action === "comp-ruby-base" ||
    el.dataset.action === "comp-ruby-reading"
  ) {
    const id = el.dataset.id;
    debounce(`comp-${id}`, () =>
      run(postForm(`${base}/competencies/${id}`, competencyPayload(id)), { full: false })
    );
  } else if (el.dataset.action === "target-label") {
    const id = el.dataset.id;
    debounce(`target-${id}`, () =>
      run(postForm(`${base}/targets/${id}`, targetPayload(id)), { full: false })
    );
  } else if (el.dataset.action === "group-name" || el.dataset.action === "group-weight") {
    const id = el.dataset.id;
    debounce(`group-${id}`, () => run(postForm(`${base}/groups/${id}`, groupPayload(id))));
  }
}

function onChange(event) {
  const el = event.target;
  const id = el.dataset.id;
  if (el.dataset.field) {
    saveSettings();
  } else if (el.dataset.action === "target-active") {
    run(postForm(`${base}/targets/${id}`, targetPayload(id)));
  } else if (el.dataset.action === "group-active") {
    run(postForm(`${base}/groups/${id}`, groupPayload(id)));
  }
}

function onClick(event) {
  const el = event.target.closest("[data-action]");
  if (!el) return;
  const action = el.dataset.action;
  const id = el.dataset.id;
  if (action === "page-tab") {
    activePage = Number(el.dataset.index) || 0;
    renderCanvas();
  } else if (action === "col-edit") {
    // Toggle the on-sheet editor for this column.
    editingCompId = editingCompId === Number(id) ? null : Number(id);
    renderCanvas();
    if (editingCompId != null) focusEditorTitle();
  } else if (action === "col-add") {
    addColumn();
  } else if (action === "col-close") {
    editingCompId = null;
    renderCanvas();
  } else if (action === "comp-ruby-add") {
    const baseInput = root.querySelector(`[data-add-ruby-base][data-id="${id}"]`);
    const readingInput = root.querySelector(`[data-add-ruby-reading][data-id="${id}"]`);
    if (!baseInput.value.trim() || !readingInput.value.trim()) return;
    const next = rubyFromDom(id).concat([
      { base_text: baseInput.value.trim(), reading: readingInput.value.trim() },
    ]);
    run(postForm(`${base}/competencies/${id}`, competencyPayload(id, next)));
  } else if (action === "comp-ruby-delete") {
    const next = rubyFromDom(id, el.dataset.index);
    run(postForm(`${base}/competencies/${id}`, competencyPayload(id, next)));
  } else if (action === "comp-move") {
    if (el.disabled) return;
    run(postForm(`${base}/competencies/${id}/move`, { direction: el.dataset.dir }));
  } else if (action === "comp-delete") {
    // Safe delete: confirm first (deletion is blocked server-side once ratings exist).
    if (!window.confirm(t("delete_competency_confirm"))) return;
    editingCompId = null;
    run(postForm(`${base}/competencies/${id}/delete`, {}));
  } else if (action === "page-break") {
    run(postForm(`${base}/pages/page-break`, { target_id: id }));
  } else if (action === "auto-paginate") {
    run(postForm(`${base}/pages/auto-paginate`, {}));
  } else if (action === "remove-breaks") {
    run(postForm(`${base}/pages/page-break/remove`, {}));
  } else if (action === "group-add") {
    const name = root.querySelector('[data-add="group-name"]');
    const weight = root.querySelector('[data-add="group-weight"]');
    if (!name.value.trim()) return;
    run(
      postForm(`${base}/groups`, {
        name: name.value,
        weight_percent: weight.value || "0",
        description: "",
      })
    );
  } else if (action === "group-delete") {
    run(postForm(`${base}/groups/${id}/delete`, {}));
  }
}

function onKeydown(event) {
  if (event.key === "Escape" && editingCompId != null) {
    editingCompId = null;
    renderCanvas();
  }
}

function shellHtml() {
  return `<div class="pr-toolbar2" id="pr-toolbar"></div>
    <div class="pr-workspace">
      <div class="pr-canvas-col">
        <div class="pr-canvas-head">
          <span class="type-pill peer">${escapeHtml(t("anonymous"))}</span>
          <span class="muted text-sm">${escapeHtml(t("edit_on_sheet_hint"))}</span>
        </div>
        <div class="pr-canvas" id="pr-canvas"></div>
      </div>
      <aside class="pr-side" id="pr-side"></aside>
    </div>`;
}

function init() {
  root = document.getElementById("pr-builder-app");
  if (!root) return;
  base = root.dataset.base;
  state = readJsonScript("pr-initial-state");
  layout = readJsonScript("pr-initial-layout");
  if (!state || !layout) return;
  root.innerHTML = shellHtml();
  root.addEventListener("input", onInput);
  root.addEventListener("change", onChange);
  root.addEventListener("click", onClick);
  document.addEventListener("keydown", onKeydown);
  render();
}

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", init);
} else {
  init();
}
