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

  const submitButton = event.target.querySelector('button[type="submit"]');
  submitButton.disabled = true;
  setThinking(true, "Claude is analyzing your document — this can take a minute…");
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
    card.innerHTML =
      `<h3>${escapeHtml(entry.ticket.title)} ` +
      `<span class="priority-badge priority-${priority}">${priority}</span> ` +
      `<span class="category-badge category-${category}">${category}</span></h3>` +
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

function initTicketsTab() {
  document.getElementById("tickets-generate-form").addEventListener("submit", onGenerateTickets);
  document.getElementById("tickets-create-all").addEventListener("click", onCreateAllTickets);
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

async function onPickerSpaceChange() {
  const spaceSelect = document.getElementById("qa-picker-space");
  const folderSelect = document.getElementById("qa-picker-folder");
  const ticketSelect = document.getElementById("qa-picker-ticket");
  const spaceId = spaceSelect.value;
  const space = clickupSpaces.find((s) => String(s.id) === spaceId);
  currentSpaceName = space ? space.name : null;
  renderProjectDropdown();

  folderSelect.innerHTML = '<option value="">Select a space first</option>';
  folderSelect.disabled = true;
  ticketSelect.innerHTML = '<option value="">Select a folder/list first</option>';
  ticketSelect.disabled = true;
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
  renderProjectDropdown();
}

function renderProjectDropdown() {
  const select = document.getElementById("qa-review-project");
  const options = new Set(knownProjects);
  if (currentSpaceName) options.add(currentSpaceName);
  const previousValue = select.value;
  select.innerHTML = "";
  for (const project of options) {
    const option = document.createElement("option");
    option.value = project;
    option.textContent = project;
    select.appendChild(option);
  }
  if (options.has(previousValue)) {
    select.value = previousValue;
  } else if (currentSpaceName && options.has(currentSpaceName)) {
    select.value = currentSpaceName;
  }
}

function selectClickupTask(task) {
  selectedTicket = { id: task.id, name: task.name };
  document.getElementById("qa-review-selected").textContent = `Selected: ${task.name} (${task.id})`;
  document.getElementById("qa-review-analyze-btn").disabled = false;
  document.getElementById("qa-review-result").hidden = true;
  showQaReviewError(null);
  showQaReviewSuccess(null);
  renderProjectDropdown();
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

function renderReviewResult(runId, review, finding) {
  const container = document.getElementById("qa-review-result");
  container.hidden = false;
  const severity = escapeHtml(review.severity);
  container.innerHTML =
    `<h3>${escapeHtml(review.ticket_name)} <span class="priority-badge priority-${severity}">${severity}</span></h3>` +
    `<p>${escapeHtml(review.observation)}</p>` +
    `<div class="ticket-status"></div>`;

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
          }),
        });
        renderReviewResult(runId, review, persistedFinding);
        showQaReviewSuccess(`Finding #${persistedFinding.id} persisted.`);
        refreshKnownProjects();
        loadFindings();
      } catch (err) {
        button.disabled = false;
        showQaReviewError(err.message);
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
  setReviewThinking(true, "Claude is analyzing this ticket…");
  try {
    const result = await fetchJson("/api/qa/reviews", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ticket_id: selectedTicket.id, project, persist: false }),
    });
    renderReviewResult(result.run_id, result.review, result.finding);
    showQaReviewSuccess("Analysis complete — review the result below.");
    await loadReviewRuns();
  } catch (err) {
    showQaReviewError(err.message);
  } finally {
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
  setReviewThinking(true, "Replaying — re-fetching and re-analyzing the same ticket…");
  try {
    const result = await fetchJson(`/api/qa/reviews/${encodeURIComponent(runId)}/replay`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ persist: false }),
    });
    renderReviewResult(result.run_id, result.review, result.finding);
    showQaReviewSuccess("Replay complete — review the result below.");
    await loadReviewRuns();
  } catch (err) {
    showQaReviewError(err.message);
  } finally {
    setReviewThinking(false);
  }
}

function initQaReviewPanel() {
  document.getElementById("qa-review-form").addEventListener("submit", onAnalyzeReview);
  document.getElementById("qa-review-replay-btn").addEventListener("click", onReplayReview);
  initTicketPicker();
  refreshKnownProjects();
  loadReviewRuns();
}

document.addEventListener("DOMContentLoaded", () => {
  initTabs();
  initQaFilters();
  initQaReportForm();
  loadFindings();
  initTicketsTab();
  initQaReviewPanel();
});
