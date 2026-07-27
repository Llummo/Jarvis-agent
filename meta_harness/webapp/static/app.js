// Vanilla JS, no build step, no framework — tab switching + fetch calls.

let currentSpaceName = null; // last-picked ClickUp space, used as the default review project
let selectedTicket = null; // { id, name } — set by picking a ticket in the cascading dropdowns
let knownProjects = []; // distinct project names seen in existing QA findings, for the dropdown

let clickupSpaces = []; // [{ id, name }] flattened across every team
let clickupFoldersOrLists = []; // [{ id, name, kind: "folder"|"list" }] for the selected space
let clickupTickets = []; // tasks currently loaded for the selected folder/list

function initTabs() {
  document.querySelectorAll(".tab-button").forEach((button) => {
    button.addEventListener("click", () => {
      if (button.disabled) return;
      document.querySelectorAll(".tab-button").forEach((b) => b.classList.remove("active"));
      document.querySelectorAll(".tab-panel").forEach((p) => (p.hidden = true));
      button.classList.add("active");
      document.getElementById(`tab-${button.dataset.tab}`).hidden = false;
    });
  });
}

function escapeHtml(value) {
  const div = document.createElement("div");
  div.textContent = value == null ? "" : String(value);
  return div.innerHTML;
}

const STATUS_CODE_LABELS = {
  400: "Invalid request",
  404: "Not found",
  409: "Conflict",
  422: "Invalid request data",
  502: "Upstream service failed",
  503: "Service unavailable",
  500: "Internal server error",
};

function statusCodeLabel(status) {
  return STATUS_CODE_LABELS[status] || `Request failed (HTTP ${status})`;
}

function formatErrorDetail(body) {
  if (body && typeof body.detail === "string") return body.detail;
  if (body && Array.isArray(body.detail)) {
    // FastAPI/Pydantic validation errors: a list of {msg, loc, ...} objects.
    return body.detail.map((entry) => entry.msg || JSON.stringify(entry)).join("; ");
  }
  if (body && Object.keys(body).length) return JSON.stringify(body);
  return null;
}

async function fetchJson(url, options) {
  const response = await fetch(url, options);
  const body = await response.json().catch(() => ({}));
  if (!response.ok) {
    const label = statusCodeLabel(response.status);
    const detail = formatErrorDetail(body);
    throw new Error(detail ? `${label}: ${detail}` : label);
  }
  return body;
}

function newProgressToken() {
  if (window.crypto && crypto.randomUUID) return crypto.randomUUID();
  return `${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

// Polls GET /api/progress/{token} while a long agent call (Claude, ClickUp
// fetch, retry-with-repair) is in flight, so the loading indicator can show
// what's actually happening instead of a single static message. onSteps
// receives the FULL list of steps so far (not just the latest one) — a
// message that only shows the last step flashes by too fast to read on a
// fast operation, which is why "Starting…" was all that was ever visible.
// Returns a stop function — call it once the main request settles; it does
// one final read first, to catch a step recorded just before the response
// landed.
function pollProgress(token, onSteps) {
  let stopped = false;
  let timer = null;
  const tick = async () => {
    if (stopped) return;
    try {
      const data = await fetchJson(`/api/progress/${encodeURIComponent(token)}`);
      if (data.steps && data.steps.length) onSteps(data.steps);
    } catch (err) {
      // best-effort — a polling failure must never interrupt the main action
    }
    if (!stopped) timer = setTimeout(tick, 500);
  };
  tick();
  return async () => {
    stopped = true;
    if (timer) clearTimeout(timer);
    try {
      const data = await fetchJson(`/api/progress/${encodeURIComponent(token)}`);
      if (data.steps && data.steps.length) onSteps(data.steps);
    } catch (err) {
      // ignore — this is just a best-effort final flush
    }
  };
}

function renderPhaseLog(logId, steps) {
  const el = document.getElementById(logId);
  if (!el) return;
  el.innerHTML = steps.map((step) => `<li>${escapeHtml(step)}</li>`).join("");
  el.scrollTop = el.scrollHeight;
}

function downloadTextFile(filename, content, mimeType = "text/markdown") {
  const blob = new Blob([content], { type: mimeType });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  document.body.appendChild(link);
  link.click();
  document.body.removeChild(link);
  URL.revokeObjectURL(url);
}

function severityClass(severity) {
  if (severity === "critical") return "severity-critical";
  if (severity === "major") return "severity-major";
  return "severity-minor";
}

async function loadFindings() {
  const form = document.getElementById("qa-filters");
  const params = new URLSearchParams(new FormData(form));
  for (const [key, value] of [...params.entries()]) {
    if (!value) params.delete(key);
  }

  const data = await fetchJson(`/api/qa/findings?${params.toString()}`);
  renderCounts(data.counts);
  renderFindings(data.findings);
}

function renderCounts(counts) {
  const el = document.getElementById("qa-counts");
  el.textContent =
    `total: ${counts.total} | open: ${counts.open} | acknowledged: ${counts.acknowledged} | ` +
    `closed: ${counts.closed} | critical: ${counts.critical} | major: ${counts.major} | minor: ${counts.minor}`;
}

function renderFindings(findings) {
  const tbody = document.querySelector("#qa-table tbody");
  tbody.innerHTML = "";
  for (const finding of findings) {
    const row = document.createElement("tr");

    const closeCell = document.createElement("td");
    if (finding.status !== "closed") {
      const input = document.createElement("input");
      input.type = "text";
      input.placeholder = "correction note";
      const button = document.createElement("button");
      button.textContent = "Close";
      const feedback = document.createElement("div");
      feedback.className = "error-text";
      button.addEventListener("click", async () => {
        if (!input.value) return;
        feedback.textContent = "";
        feedback.className = "error-text";
        button.disabled = true;
        try {
          await fetchJson(`/api/qa/findings/${finding.id}/close`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ correction_note: input.value }),
          });
          await loadFindings();
        } catch (err) {
          feedback.textContent = err.message;
          button.disabled = false;
        }
      });
      closeCell.append(input, button, feedback);
    }

    row.innerHTML =
      `<td>${escapeHtml(finding.id)}</td><td>${escapeHtml(finding.project)}</td><td>${escapeHtml(finding.route)}</td>` +
      `<td class="${severityClass(finding.severity)}">${escapeHtml(finding.severity)}</td>` +
      `<td>${escapeHtml(finding.status)}</td><td>${escapeHtml(finding.clickup_task_id || "-")}</td>` +
      `<td>${escapeHtml(finding.observation)}</td>`;
    row.appendChild(closeCell);
    tbody.appendChild(row);
  }
}

function initQaFilters() {
  document.getElementById("qa-filters").addEventListener("submit", (event) => {
    event.preventDefault();
    loadFindings();
  });
}

function showQaReportSuccess(message) {
  const el = document.getElementById("qa-report-success");
  el.textContent = message || "";
}

function initQaReportForm() {
  const form = document.getElementById("qa-report-form");
  const errorEl = document.getElementById("qa-report-error");
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    errorEl.textContent = "";
    showQaReportSuccess(null);
    const data = Object.fromEntries(new FormData(form));
    try {
      await fetchJson("/api/qa/findings", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(data),
      });
      form.reset();
      showQaReportSuccess("Finding reported.");
      await loadFindings();
    } catch (err) {
      errorEl.textContent = err.message;
    }
  });
}

// --- Generate Tickets tab -------------------------------------------------

let proposedTickets = []; // [{ ticket, status: "pending"|"creating"|"created"|"error", clickup_task_id, error }]

function showTicketsError(message) {
  const el = document.getElementById("tickets-error");
  if (message) {
    el.textContent = message;
    el.hidden = false;
  } else {
    el.hidden = true;
  }
}

function showTicketsSuccess(message) {
  const el = document.getElementById("tickets-success");
  if (message) {
    el.textContent = message;
    el.hidden = false;
  } else {
    el.hidden = true;
  }
}

function renderTicketWarnings(warnings) {
  const el = document.getElementById("tickets-warnings");
  if (warnings && warnings.length) {
    el.textContent = `Warnings: ${warnings.join("; ")}`;
    el.hidden = false;
  } else {
    el.hidden = true;
  }
}

function setThinking(visible, message) {
  const el = document.getElementById("tickets-thinking");
  el.hidden = !visible;
  if (visible) el.querySelector(".thinking-text").textContent = message;
}

async function onGenerateTickets(event) {
  event.preventDefault();
  const file = document.getElementById("tickets-file-input").files[0];
  if (!file) return;
  showTicketsError(null);
  showTicketsSuccess(null);
  renderTicketWarnings([]);
  const formData = new FormData();
  formData.append("file", file);
  formData.append("start_mundane", document.getElementById("start-mundane").value || "1");
  formData.append("start_backend", document.getElementById("start-backend").value || "1");
  formData.append("start_frontend", document.getElementById("start-frontend").value || "1");
  formData.append("start_deployment", document.getElementById("start-deployment").value || "1");
  const token = newProgressToken();
  formData.append("progress_token", token);

  const submitButton = event.target.querySelector('button[type="submit"]');
  submitButton.disabled = true;
  setThinking(true, "Starting…");
  const stopPolling = pollProgress(token, (steps) => {
    setThinking(true, steps[steps.length - 1]);
    renderPhaseLog("tickets-phase-log", steps);
  });
  try {
    const body = await fetchJson("/api/tickets/generate", { method: "POST", body: formData });
    proposedTickets = body.tickets.map((ticket) => ({ ticket, status: "pending" }));
    renderTicketWarnings(body.warnings);
    document.getElementById("tickets-actions").hidden = proposedTickets.length === 0;
    renderProposedTickets();
    showTicketsSuccess(`Analyzed document — ${proposedTickets.length} ticket(s) proposed for review below.`);
  } catch (err) {
    showTicketsError(err.message);
  } finally {
    await stopPolling();
    submitButton.disabled = false;
    setThinking(false);
  }
}

function applyTicketResult(entry, result) {
  if (result.ok) {
    entry.status = "created";
    entry.clickup_task_id = result.clickup_task_id;
  } else {
    entry.status = "error";
    entry.error = result.error;
  }
}

async function createOneTicket(index) {
  const entry = proposedTickets[index];
  entry.status = "creating";
  renderProposedTickets();
  const listId = document.getElementById("tickets-list-id").value || null;
  try {
    const body = await fetchJson("/api/tickets/create", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ tickets: [entry.ticket], list_id: listId }),
    });
    applyTicketResult(entry, body.results[0]);
  } catch (err) {
    entry.status = "error";
    entry.error = err.message;
  }
  renderProposedTickets();
}

async function onCreateAllTickets() {
  const pending = proposedTickets.filter((e) => e.status !== "created");
  if (!pending.length) return;
  showTicketsSuccess(null);
  pending.forEach((e) => (e.status = "creating"));
  renderProposedTickets();
  const createAllButton = document.getElementById("tickets-create-all");
  createAllButton.disabled = true;
  setThinking(true, `Creating ${pending.length} ticket(s) in ClickUp…`);
  const listId = document.getElementById("tickets-list-id").value || null;
  try {
    const body = await fetchJson("/api/tickets/create", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ tickets: pending.map((e) => e.ticket), list_id: listId }),
    });
    pending.forEach((entry, i) => applyTicketResult(entry, body.results[i]));
    const created = pending.filter((e) => e.status === "created").length;
    const failed = pending.length - created;
    showTicketsSuccess(
      failed
        ? `Created ${created} of ${pending.length} ticket(s) — ${failed} failed, see details below.`
        : `Created ${created} ticket(s) in ClickUp.`,
    );
  } catch (err) {
    showTicketsError(err.message);
    pending.forEach((e) => (e.status = "pending"));
  }
  createAllButton.disabled = false;
  setThinking(false);
  renderProposedTickets();
}

function renderProposedTickets() {
  const container = document.getElementById("tickets-list");
  container.innerHTML = "";
  proposedTickets.forEach((entry, index) => {
    const card = document.createElement("div");
    card.className = "ticket-card";
    const acItems = entry.ticket.acceptance_criteria.map((c) => `<li>${escapeHtml(c)}</li>`).join("");
    const priority = escapeHtml(entry.ticket.priority);
    const category = escapeHtml(entry.ticket.category);
    const userStory = entry.ticket.user_story
      ? `<p class="user-story">${escapeHtml(entry.ticket.user_story)}</p>`
      : "";
    card.innerHTML =
      `<h3>${escapeHtml(entry.ticket.title)} ` +
      `<span class="priority-badge priority-${priority}">${priority}</span> ` +
      `<span class="category-badge category-${category}">${category}</span></h3>` +
      userStory +
      `<p>${escapeHtml(entry.ticket.description)}</p>` +
      (acItems ? `<ul>${acItems}</ul>` : "") +
      `<div class="ticket-status"></div>`;

    const statusEl = card.querySelector(".ticket-status");
    if (entry.status === "created") {
      statusEl.textContent = `Created: ${entry.clickup_task_id}`;
      statusEl.classList.add("ticket-status-ok");
    } else if (entry.status === "error") {
      statusEl.textContent = `Failed: ${entry.error}`;
      statusEl.classList.add("ticket-status-error");
    } else if (entry.status === "creating") {
      statusEl.textContent = "Creating...";
    }

    if (entry.status !== "created" && entry.status !== "creating") {
      const button = document.createElement("button");
      button.textContent = "Create";
      button.addEventListener("click", () => createOneTicket(index));
      card.appendChild(button);
    }
    container.appendChild(card);
  });
}

async function loadTicketsListOptions() {
  // Every real ClickUp list across every space/folder, flattened into one
  // dropdown labeled "space / folder / list" — replaces having to know and
  // type a raw ClickUp list id by hand.
  const select = document.getElementById("tickets-list-id");
  try {
    const teams = await fetchJson("/api/clickup/teams");
    const spacesByTeam = await Promise.all(
      teams.map((team) => fetchJson(`/api/clickup/spaces?team_id=${encodeURIComponent(team.id)}`)),
    );
    const spaces = spacesByTeam.flat();
    const perSpace = await Promise.all(
      spaces.map(async (space) => {
        const [folders, folderlessLists] = await Promise.all([
          fetchJson(`/api/clickup/folders?space_id=${encodeURIComponent(space.id)}`),
          fetchJson(`/api/clickup/lists?space_id=${encodeURIComponent(space.id)}`),
        ]);
        const entries = folderlessLists.map((list) => ({ id: list.id, label: `${space.name} / ${list.name}` }));
        const listsByFolder = await Promise.all(
          folders.map((folder) => fetchJson(`/api/clickup/lists?folder_id=${encodeURIComponent(folder.id)}`)),
        );
        folders.forEach((folder, i) => {
          for (const list of listsByFolder[i]) {
            entries.push({ id: list.id, label: `${space.name} / ${folder.name} / ${list.name}` });
          }
        });
        return entries;
      }),
    );
    const listOptions = perSpace.flat();
    select.innerHTML = "";
    const placeholder = document.createElement("option");
    placeholder.value = "";
    placeholder.textContent = "Default (Sprint backlog)";
    select.appendChild(placeholder);
    for (const entry of listOptions) {
      const option = document.createElement("option");
      option.value = entry.id;
      option.textContent = entry.label;
      select.appendChild(option);
    }
  } catch (err) {
    showTicketsError(`Could not load ClickUp lists: ${err.message}`);
  }
}

function initTicketsTab() {
  document.getElementById("tickets-generate-form").addEventListener("submit", onGenerateTickets);
  document.getElementById("tickets-create-all").addEventListener("click", onCreateAllTickets);
  loadTicketsListOptions();
}

// --- Review Ticket (QA Flow) panel ------------------------------------------

function showPickerError(message) {
  const el = document.getElementById("qa-picker-error");
  if (message) {
    el.textContent = message;
    el.hidden = false;
  } else {
    el.hidden = true;
  }
}

function setPickerThinking(visible, message) {
  const el = document.getElementById("qa-picker-thinking");
  el.hidden = !visible;
  if (visible) el.querySelector(".thinking-text").textContent = message;
}

function resetSelectedTicket() {
  selectedTicket = null;
  document.getElementById("qa-review-selected").textContent = "No ticket selected.";
  document.getElementById("qa-review-analyze-btn").disabled = true;
  document.getElementById("qa-review-result").hidden = true;
  showQaReviewError(null);
  showQaReviewSuccess(null);
}

async function loadPickerSpaces() {
  const spaceSelect = document.getElementById("qa-picker-space");
  showPickerError(null);
  setPickerThinking(true, "Loading ClickUp spaces…");
  try {
    const teams = await fetchJson("/api/clickup/teams");
    const spacesByTeam = await Promise.all(
      teams.map((team) => fetchJson(`/api/clickup/spaces?team_id=${encodeURIComponent(team.id)}`)),
    );
    clickupSpaces = spacesByTeam.flat();
    renderProjectDropdowns();
    spaceSelect.innerHTML = "";
    if (!clickupSpaces.length) {
      spaceSelect.innerHTML = '<option value="">No spaces found</option>';
      return;
    }
    const placeholder = document.createElement("option");
    placeholder.value = "";
    placeholder.textContent = "Select a space…";
    spaceSelect.appendChild(placeholder);
    for (const space of clickupSpaces) {
      const option = document.createElement("option");
      option.value = space.id;
      option.textContent = space.name;
      spaceSelect.appendChild(option);
    }
  } catch (err) {
    showPickerError(err.message);
    spaceSelect.innerHTML = '<option value="">Failed to load spaces</option>';
  } finally {
    setPickerThinking(false);
  }
}

function populateStatusSelect(select, statuses) {
  const previousValue = select.value;
  select.innerHTML = "";
  const placeholder = document.createElement("option");
  placeholder.value = "";
  placeholder.textContent = "(leave as-is)";
  select.appendChild(placeholder);
  for (const st of statuses) {
    const option = document.createElement("option");
    option.value = st.status;
    option.textContent = st.status;
    select.appendChild(option);
  }
  if (statuses.some((st) => st.status === previousValue)) select.value = previousValue;
}

function populateStatusSelectsForSpace(spaceId) {
  // ClickUp status names are per-space (lists inherit their space's status
  // workflow unless overridden), and the space objects already carry a
  // `statuses` array — no extra fetch needed.
  const space = clickupSpaces.find((s) => String(s.id) === String(spaceId));
  const statuses = (space && space.statuses) || [];
  populateStatusSelect(document.getElementById("qa-status-pass"), statuses);
  populateStatusSelect(document.getElementById("qa-status-fail"), statuses);
}

async function onPickerSpaceChange() {
  const spaceSelect = document.getElementById("qa-picker-space");
  const folderSelect = document.getElementById("qa-picker-folder");
  const ticketSelect = document.getElementById("qa-picker-ticket");
  const spaceId = spaceSelect.value;
  const space = clickupSpaces.find((s) => String(s.id) === spaceId);
  currentSpaceName = space ? space.name : null;
  renderProjectDropdowns();
  populateStatusSelectsForSpace(spaceId);

  folderSelect.innerHTML = '<option value="">Select a space first</option>';
  folderSelect.disabled = true;
  ticketSelect.innerHTML = '<option value="">Select a folder/list first</option>';
  ticketSelect.disabled = true;
  document.getElementById("qa-bulk-run-btn").disabled = true;
  hideBulkResults();
  resetSelectedTicket();

  if (!spaceId) return;

  showPickerError(null);
  setPickerThinking(true, "Loading folders and lists…");
  try {
    const [folders, folderlessLists] = await Promise.all([
      fetchJson(`/api/clickup/folders?space_id=${encodeURIComponent(spaceId)}`),
      fetchJson(`/api/clickup/lists?space_id=${encodeURIComponent(spaceId)}`),
    ]);
    clickupFoldersOrLists = [
      ...folders.map((f) => ({ id: f.id, name: f.name, kind: "folder" })),
      ...folderlessLists.map((l) => ({ id: l.id, name: l.name, kind: "list" })),
    ];
    folderSelect.innerHTML = "";
    if (!clickupFoldersOrLists.length) {
      folderSelect.innerHTML = '<option value="">No folders or lists found</option>';
      return;
    }
    const placeholder = document.createElement("option");
    placeholder.value = "";
    placeholder.textContent = "Select a folder or list…";
    folderSelect.appendChild(placeholder);
    for (const entry of clickupFoldersOrLists) {
      const option = document.createElement("option");
      option.value = `${entry.kind}:${entry.id}`;
      option.textContent = entry.kind === "folder" ? `${entry.name} (folder)` : entry.name;
      folderSelect.appendChild(option);
    }
    folderSelect.disabled = false;
  } catch (err) {
    showPickerError(err.message);
    folderSelect.innerHTML = '<option value="">Failed to load</option>';
  } finally {
    setPickerThinking(false);
  }
}

async function onPickerFolderChange() {
  const folderSelect = document.getElementById("qa-picker-folder");
  const ticketSelect = document.getElementById("qa-picker-ticket");
  const value = folderSelect.value;
  ticketSelect.innerHTML = '<option value="">Select a folder/list first</option>';
  ticketSelect.disabled = true;
  document.getElementById("qa-bulk-run-btn").disabled = true;
  hideBulkResults();
  resetSelectedTicket();
  if (!value) return;

  const [kind, id] = value.split(":");
  showPickerError(null);
  setPickerThinking(true, "Loading tickets…");
  try {
    // A folderless list is already a single list; a folder (ClickUp "Project")
    // can contain several lists, so fetch and merge tasks across all of them —
    // this is what lets the user go straight from folder to ticket without an
    // extra "pick a list" step in the common single-list case.
    const lists = kind === "list" ? [{ id, name: null }] : await fetchJson(`/api/clickup/lists?folder_id=${encodeURIComponent(id)}`);
    if (!lists.length) {
      ticketSelect.innerHTML = '<option value="">No lists found in this folder</option>';
      return;
    }
    const tasksByList = await Promise.all(
      lists.map((list) => fetchJson(`/api/clickup/tasks?list_id=${encodeURIComponent(list.id)}`)),
    );
    clickupTickets = [];
    tasksByList.forEach((tasks, i) => {
      for (const task of tasks) {
        clickupTickets.push({ ...task, __listName: lists[i].name });
      }
    });
    ticketSelect.innerHTML = "";
    if (!clickupTickets.length) {
      ticketSelect.innerHTML = '<option value="">No tickets found</option>';
      return;
    }
    const placeholder = document.createElement("option");
    placeholder.value = "";
    placeholder.textContent = "Select a ticket…";
    ticketSelect.appendChild(placeholder);
    const multipleLists = lists.length > 1;
    for (const task of clickupTickets) {
      const option = document.createElement("option");
      option.value = task.id;
      const status = task.status ? task.status.status : "-";
      const prefix = multipleLists && task.__listName ? `[${task.__listName}] ` : "";
      option.textContent = `${prefix}${task.name} (${status})`;
      ticketSelect.appendChild(option);
    }
    ticketSelect.disabled = false;
    document.getElementById("qa-bulk-run-btn").disabled = false;
  } catch (err) {
    showPickerError(err.message);
    ticketSelect.innerHTML = '<option value="">Failed to load</option>';
  } finally {
    setPickerThinking(false);
  }
}

function onPickerTicketChange() {
  const ticketSelect = document.getElementById("qa-picker-ticket");
  const taskId = ticketSelect.value;
  if (!taskId) {
    resetSelectedTicket();
    return;
  }
  const task = clickupTickets.find((t) => String(t.id) === taskId);
  if (task) selectClickupTask(task);
}

function initTicketPicker() {
  document.getElementById("qa-picker-space").addEventListener("change", onPickerSpaceChange);
  document.getElementById("qa-picker-folder").addEventListener("change", onPickerFolderChange);
  document.getElementById("qa-picker-ticket").addEventListener("change", onPickerTicketChange);
  loadPickerSpaces();
}

async function refreshKnownProjects() {
  try {
    const data = await fetchJson("/api/qa/findings");
    const projects = new Set(data.findings.map((f) => f.project));
    knownProjects = Array.from(projects).sort();
  } catch (err) {
    knownProjects = [];
  }
  renderProjectDropdowns();
}

function projectOptionsUnion() {
  // Every project name this app actually knows about: previously reported/
  // reviewed findings, plus every real ClickUp space — a fixed, enumerable
  // set, which is exactly what makes this a dropdown instead of free text.
  const options = new Set(knownProjects);
  for (const space of clickupSpaces) options.add(space.name);
  return Array.from(options).sort();
}

function populateProjectSelect(select, { includeAnyOption = false, preferredValue = null } = {}) {
  const options = projectOptionsUnion();
  const previousValue = select.value;
  const hadPlaceholder = select.querySelector('option[value=""][disabled]') !== null;
  select.innerHTML = "";
  if (includeAnyOption) {
    const option = document.createElement("option");
    option.value = "";
    option.textContent = "any project";
    select.appendChild(option);
  } else if (hadPlaceholder || !options.length) {
    const option = document.createElement("option");
    option.value = "";
    option.disabled = true;
    option.textContent = "Select a project…";
    if (!options.includes(previousValue)) option.selected = true;
    select.appendChild(option);
  }
  for (const project of options) {
    const option = document.createElement("option");
    option.value = project;
    option.textContent = project;
    select.appendChild(option);
  }
  if (options.includes(previousValue)) {
    select.value = previousValue;
  } else if (preferredValue && options.includes(preferredValue)) {
    select.value = preferredValue;
  }
}

let projectBaseUrls = {}; // { projectName: baseUrl } — enables real screenshot + status-code checks

async function loadProjectConfig() {
  try {
    const data = await fetchJson("/api/qa/project-config");
    projectBaseUrls = data.projects || {};
  } catch (err) {
    projectBaseUrls = {};
  }
  updateBaseUrlField();
}

function updateBaseUrlField() {
  const project = document.getElementById("qa-review-project").value;
  document.getElementById("qa-project-base-url").value = projectBaseUrls[project] || "";
  document.getElementById("qa-project-base-url-status").textContent = "";
}

async function onSaveProjectBaseUrl() {
  const project = document.getElementById("qa-review-project").value;
  const baseUrl = document.getElementById("qa-project-base-url").value.trim();
  const statusEl = document.getElementById("qa-project-base-url-status");
  statusEl.textContent = "";
  statusEl.className = "success-text";
  if (!project) {
    statusEl.textContent = "Pick a project first.";
    statusEl.className = "error-text";
    return;
  }
  if (!baseUrl) {
    statusEl.textContent = "Enter a URL first.";
    statusEl.className = "error-text";
    return;
  }
  try {
    const data = await fetchJson("/api/qa/project-config", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ project, base_url: baseUrl }),
    });
    projectBaseUrls = data.projects || {};
    statusEl.textContent = "Saved.";
  } catch (err) {
    statusEl.textContent = err.message;
    statusEl.className = "error-text";
  }
}

function renderProjectDropdowns() {
  populateProjectSelect(document.getElementById("qa-review-project"), { preferredValue: currentSpaceName });
  populateProjectSelect(document.getElementById("qa-report-project"));
  populateProjectSelect(document.getElementById("qa-filter-project"), { includeAnyOption: true });
  updateBaseUrlField();
}

function selectClickupTask(task) {
  selectedTicket = { id: task.id, name: task.name };
  document.getElementById("qa-review-selected").textContent = `Selected: ${task.name} (${task.id})`;
  document.getElementById("qa-review-analyze-btn").disabled = false;
  document.getElementById("qa-review-result").hidden = true;
  showQaReviewError(null);
  showQaReviewSuccess(null);
  renderProjectDropdowns();
}

function showQaReviewError(message) {
  const el = document.getElementById("qa-review-error");
  if (message) {
    el.textContent = message;
    el.hidden = false;
  } else {
    el.hidden = true;
  }
}

function showQaReviewSuccess(message) {
  const el = document.getElementById("qa-review-success");
  if (message) {
    el.textContent = message;
    el.hidden = false;
  } else {
    el.hidden = true;
  }
}

function setReviewThinking(visible, message) {
  const el = document.getElementById("qa-review-thinking");
  el.hidden = !visible;
  if (visible) el.querySelector(".thinking-text").textContent = message;
}

function screenshotUrl(screenshotPath) {
  const name = screenshotPath.split("/").pop();
  return `/api/qa/screenshots/${encodeURIComponent(name)}`;
}

function renderReviewResult(runId, review, finding, reportMarkdown) {
  const container = document.getElementById("qa-review-result");
  container.hidden = false;
  const severity = escapeHtml(review.severity);

  let html =
    `<h3>${escapeHtml(review.ticket_name)} <span class="priority-badge priority-${severity}">${severity}</span></h3>` +
    `<p>${escapeHtml(review.observation)}</p>`;

  const evidenceRows = [];
  if (review.route) evidenceRows.push(`<strong>Route checked:</strong> ${escapeHtml(review.route)}`);
  if (review.status_code != null) evidenceRows.push(`<strong>HTTP status:</strong> ${review.status_code}`);
  if (review.http_error) evidenceRows.push(`<strong class="error-text">Error:</strong> ${escapeHtml(review.http_error)}`);
  if (evidenceRows.length) {
    html += `<div class="evidence">${evidenceRows.map((row) => `<div>${row}</div>`).join("")}</div>`;
  }
  if (review.screenshot_path) {
    html += `<img class="evidence-image" src="${screenshotUrl(review.screenshot_path)}" alt="Screenshot evidence">`;
  }

  html += `<div class="ticket-status"></div><div class="report-actions"></div>`;
  container.innerHTML = html;

  const reportActions = container.querySelector(".report-actions");
  if (finding) {
    const mdLink = document.createElement("a");
    mdLink.href = `/api/qa/findings/${finding.id}/report.md`;
    mdLink.textContent = "⬇ Markdown";
    mdLink.setAttribute("download", "");
    const pdfLink = document.createElement("a");
    pdfLink.href = `/api/qa/findings/${finding.id}/report.pdf`;
    pdfLink.textContent = "⬇ PDF";
    pdfLink.setAttribute("download", "");
    reportActions.append(mdLink, pdfLink);
  } else if (reportMarkdown) {
    const mdButton = document.createElement("button");
    mdButton.type = "button";
    mdButton.textContent = "⬇ Markdown";
    mdButton.addEventListener("click", () => downloadTextFile(`qa-review-${review.ticket_id}.md`, reportMarkdown));
    reportActions.appendChild(mdButton);
  }

  const statusEl = container.querySelector(".ticket-status");
  if (finding) {
    statusEl.textContent = `Persisted: finding #${finding.id}${finding.clickup_task_id ? ` (ClickUp: ${finding.clickup_task_id})` : ""}`;
    statusEl.classList.add("ticket-status-ok");
  } else {
    statusEl.textContent = "Not persisted (dry-run)";
    const button = document.createElement("button");
    button.textContent = "Persist this finding";
    button.addEventListener("click", async () => {
      button.disabled = true;
      showQaReviewSuccess(null);
      showQaReviewError(null);
      const token = newProgressToken();
      let lastStep = "";
      setReviewThinking(true, "Persisting…");
      const stopPolling = pollProgress(token, (steps) => {
        lastStep = steps[steps.length - 1];
        setReviewThinking(true, lastStep);
        renderPhaseLog("qa-review-phase-log", steps);
      });
      try {
        const persistedFinding = await fetchJson("/api/qa/reviews/commit", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            ticket_id: review.ticket_id,
            ticket_name: review.ticket_name,
            observation: review.observation,
            severity: review.severity,
            project: document.getElementById("qa-review-project").value,
            pass_status: document.getElementById("qa-status-pass").value || null,
            fail_status: document.getElementById("qa-status-fail").value || null,
            route: review.route || null,
            status_code: review.status_code != null ? review.status_code : null,
            http_error: review.http_error || null,
            screenshot_path: review.screenshot_path || null,
            progress_token: token,
          }),
        });
        await stopPolling();
        renderReviewResult(runId, review, persistedFinding);
        const statusNote = /^(Moved|Finding persisted, but could not move)/.test(lastStep) ? ` ${lastStep}` : "";
        showQaReviewSuccess(`Finding #${persistedFinding.id} persisted.${statusNote}`);
        refreshKnownProjects();
        loadFindings();
      } catch (err) {
        await stopPolling();
        button.disabled = false;
        showQaReviewError(err.message);
      } finally {
        setReviewThinking(false);
      }
    });
    container.appendChild(button);
  }
}

async function onAnalyzeReview(event) {
  event.preventDefault();
  if (!selectedTicket) return;
  showQaReviewError(null);
  showQaReviewSuccess(null);
  const project = document.getElementById("qa-review-project").value;
  if (!project) {
    showQaReviewError("Pick a project first.");
    return;
  }
  const token = newProgressToken();
  setReviewThinking(true, "Starting…");
  const stopPolling = pollProgress(token, (steps) => {
    setReviewThinking(true, steps[steps.length - 1]);
    renderPhaseLog("qa-review-phase-log", steps);
  });
  try {
    const result = await fetchJson("/api/qa/reviews", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ticket_id: selectedTicket.id, project, persist: false, progress_token: token }),
    });
    renderReviewResult(result.run_id, result.review, result.finding, result.report_markdown);
    showQaReviewSuccess("Analysis complete — review the result below.");
    await loadReviewRuns();
  } catch (err) {
    showQaReviewError(err.message);
  } finally {
    await stopPolling();
    setReviewThinking(false);
  }
}

async function loadReviewRuns() {
  const select = document.getElementById("qa-review-runs-select");
  try {
    const runs = await fetchJson("/api/qa/reviews");
    select.innerHTML = "";
    for (const run of runs) {
      const option = document.createElement("option");
      option.value = run.run_id;
      option.textContent = `${run.run_id} — ${run.ticket_id || "-"} (${run.started_at})`;
      select.appendChild(option);
    }
  } catch (err) {
    showQaReviewError(err.message);
  }
}

async function onReplayReview() {
  const select = document.getElementById("qa-review-runs-select");
  const runId = select.value;
  if (!runId) return;
  showQaReviewError(null);
  showQaReviewSuccess(null);
  const token = newProgressToken();
  setReviewThinking(true, "Starting…");
  const stopPolling = pollProgress(token, (steps) => {
    setReviewThinking(true, steps[steps.length - 1]);
    renderPhaseLog("qa-review-phase-log", steps);
  });
  try {
    const result = await fetchJson(`/api/qa/reviews/${encodeURIComponent(runId)}/replay`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ persist: false, progress_token: token }),
    });
    renderReviewResult(result.run_id, result.review, result.finding, result.report_markdown);
    showQaReviewSuccess("Replay complete — review the result below.");
    await loadReviewRuns();
  } catch (err) {
    showQaReviewError(err.message);
  } finally {
    await stopPolling();
    setReviewThinking(false);
  }
}

// --- Bulk QA sweep: dry-run analyze every ticket in the current list, ------
// --- then confirm to persist + apply status moves for the ones you keep ---

let bulkReviewResults = []; // [{ ticket_id, ticket_name, review, error }]
let bulkReadmeMarkdown = ""; // QA-README.md content for the current bulk sweep

// Mirrors qa_flow.review_passed on the backend: a minor result counts as a
// pass, major/critical as a fail — same threshold, kept in sync by hand
// since this is only used to preview the proposed status before commit.
const PASSING_SEVERITIES = ["minor"];
function reviewPassed(severity) {
  return PASSING_SEVERITIES.includes(severity);
}

function showBulkError(message) {
  const el = document.getElementById("qa-bulk-error");
  if (message) {
    el.textContent = message;
    el.hidden = false;
  } else {
    el.hidden = true;
  }
}

function showBulkSuccess(message) {
  const el = document.getElementById("qa-bulk-success");
  if (message) {
    el.textContent = message;
    el.hidden = false;
  } else {
    el.hidden = true;
  }
}

function setBulkThinking(visible, message) {
  const el = document.getElementById("qa-bulk-thinking");
  el.hidden = !visible;
  if (visible) el.querySelector(".thinking-text").textContent = message;
}

function clearBulkTable() {
  bulkReviewResults = [];
  bulkReadmeMarkdown = "";
  document.getElementById("qa-bulk-results").hidden = true;
  document.querySelector("#qa-bulk-table tbody").innerHTML = "";
}

function onDownloadBulkReadme() {
  if (!bulkReadmeMarkdown) return;
  downloadTextFile("QA-README.md", bulkReadmeMarkdown);
}

function hideBulkResults() {
  clearBulkTable();
  showBulkError(null);
  showBulkSuccess(null);
}

function renderBulkResults() {
  const tbody = document.querySelector("#qa-bulk-table tbody");
  tbody.innerHTML = "";
  const passStatus = document.getElementById("qa-status-pass").value;
  const failStatus = document.getElementById("qa-status-fail").value;
  for (const entry of bulkReviewResults) {
    const row = document.createElement("tr");
    if (entry.error) {
      row.innerHTML =
        `<td>${escapeHtml(entry.ticket_name)}</td>` +
        `<td class="error-text">Error: ${escapeHtml(entry.error)}</td><td>-</td><td>-</td>`;
    } else {
      const passed = reviewPassed(entry.review.severity);
      const targetStatus = passed ? passStatus : failStatus;
      const evidenceParts = [];
      if (entry.review.route) evidenceParts.push(escapeHtml(entry.review.route));
      if (entry.review.status_code != null) evidenceParts.push(`HTTP ${entry.review.status_code}`);
      let evidenceCell = evidenceParts.join(" — ") || "-";
      if (entry.review.screenshot_path) {
        evidenceCell += ` <a href="${screenshotUrl(entry.review.screenshot_path)}" target="_blank" rel="noopener">screenshot</a>`;
      }
      row.innerHTML =
        `<td>${escapeHtml(entry.ticket_name)}</td>` +
        `<td class="${severityClass(entry.review.severity)}">${escapeHtml(entry.review.severity)} (${passed ? "pass" : "fail"})</td>` +
        `<td>${evidenceCell}</td>` +
        `<td>${targetStatus ? escapeHtml(targetStatus) : "(leave as-is)"}</td>`;
    }
    tbody.appendChild(row);
  }
  document.getElementById("qa-bulk-results").hidden = false;
}

async function onRunBulkQa() {
  if (!clickupTickets.length) return;
  const project = document.getElementById("qa-review-project").value;
  if (!project) {
    showBulkError("Pick a project first.");
    return;
  }
  hideBulkResults();
  const runBtn = document.getElementById("qa-bulk-run-btn");
  runBtn.disabled = true;
  const token = newProgressToken();
  setBulkThinking(true, "Starting…");
  const stopPolling = pollProgress(token, (steps) => {
    setBulkThinking(true, steps[steps.length - 1]);
    renderPhaseLog("qa-bulk-phase-log", steps);
  });
  const passStatus = document.getElementById("qa-status-pass").value || null;
  const failStatus = document.getElementById("qa-status-fail").value || null;
  try {
    const body = await fetchJson("/api/qa/reviews/bulk", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        ticket_ids: clickupTickets.map((t) => t.id), project, progress_token: token,
        pass_status: passStatus, fail_status: failStatus,
      }),
    });
    bulkReviewResults = body.results.map((r) => {
      const ticket = clickupTickets.find((t) => String(t.id) === r.ticket_id);
      return { ticket_id: r.ticket_id, ticket_name: ticket ? ticket.name : r.ticket_id, review: r.review, error: r.error };
    });
    bulkReadmeMarkdown = body.readme_markdown || "";
    renderBulkResults();
    const ok = bulkReviewResults.filter((r) => !r.error).length;
    const failed = bulkReviewResults.length - ok;
    showBulkSuccess(
      failed
        ? `Reviewed ${ok} of ${bulkReviewResults.length} ticket(s) — ${failed} failed to analyze, see table below.`
        : `Reviewed ${ok} ticket(s) — check the proposed results below, then confirm to save.`,
    );
  } catch (err) {
    showBulkError(err.message);
  } finally {
    await stopPolling();
    setBulkThinking(false);
    runBtn.disabled = false;
  }
}

async function onConfirmBulkQa() {
  const project = document.getElementById("qa-review-project").value;
  const passStatus = document.getElementById("qa-status-pass").value || null;
  const failStatus = document.getElementById("qa-status-fail").value || null;
  const items = bulkReviewResults
    .filter((r) => r.review && !r.error)
    .map((r) => ({
      ticket_id: r.review.ticket_id,
      ticket_name: r.review.ticket_name,
      observation: r.review.observation,
      severity: r.review.severity,
      project,
      pass_status: passStatus,
      fail_status: failStatus,
      route: r.review.route || null,
      status_code: r.review.status_code != null ? r.review.status_code : null,
      http_error: r.review.http_error || null,
      screenshot_path: r.review.screenshot_path || null,
    }));
  if (!items.length) return;
  showBulkError(null);
  showBulkSuccess(null);
  const confirmBtn = document.getElementById("qa-bulk-confirm-btn");
  confirmBtn.disabled = true;
  const token = newProgressToken();
  setBulkThinking(true, "Starting…");
  const stopPolling = pollProgress(token, (steps) => {
    setBulkThinking(true, steps[steps.length - 1]);
    renderPhaseLog("qa-bulk-phase-log", steps);
  });
  try {
    const body = await fetchJson("/api/qa/reviews/bulk/commit", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ items, progress_token: token }),
    });
    const ok = body.results.filter((r) => r.finding).length;
    const failed = body.results.length - ok;
    if (failed) {
      const errorList = body.results.filter((r) => r.error).map((r) => `${r.ticket_id}: ${r.error}`).join("; ");
      showBulkError(`${failed} finding(s) failed to persist — ${errorList}`);
    }
    showBulkSuccess(`Persisted ${ok} of ${body.results.length} finding(s), applying any configured status moves.`);
    clearBulkTable();
    refreshKnownProjects();
    loadFindings();
  } catch (err) {
    showBulkError(err.message);
  } finally {
    await stopPolling();
    setBulkThinking(false);
    confirmBtn.disabled = false;
  }
}

function initQaReviewPanel() {
  document.getElementById("qa-review-form").addEventListener("submit", onAnalyzeReview);
  document.getElementById("qa-review-replay-btn").addEventListener("click", onReplayReview);
  document.getElementById("qa-bulk-run-btn").addEventListener("click", onRunBulkQa);
  document.getElementById("qa-bulk-confirm-btn").addEventListener("click", onConfirmBulkQa);
  document.getElementById("qa-bulk-readme-btn").addEventListener("click", onDownloadBulkReadme);
  document.getElementById("qa-review-project").addEventListener("change", updateBaseUrlField);
  document.getElementById("qa-project-base-url-save").addEventListener("click", onSaveProjectBaseUrl);
  initTicketPicker();
  refreshKnownProjects();
  loadReviewRuns();
  loadProjectConfig();
}

document.addEventListener("DOMContentLoaded", () => {
  initTabs();
  initQaFilters();
  initQaReportForm();
  loadFindings();
  initTicketsTab();
  initQaReviewPanel();
});
