// Vanilla JS, no build step, no framework — tab switching + fetch calls.

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
      button.addEventListener("click", async () => {
        if (!input.value) return;
        try {
          await fetchJson(`/api/qa/findings/${finding.id}/close`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ correction_note: input.value }),
          });
          await loadFindings();
        } catch (err) {
          alert(err.message);
        }
      });
      closeCell.append(input, button);
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

function initQaReportForm() {
  const form = document.getElementById("qa-report-form");
  const errorEl = document.getElementById("qa-report-error");
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    errorEl.textContent = "";
    const data = Object.fromEntries(new FormData(form));
    try {
      await fetchJson("/api/qa/findings", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(data),
      });
      form.reset();
      await loadFindings();
    } catch (err) {
      errorEl.textContent = err.message;
    }
  });
}

function showClickupError(message) {
  const el = document.getElementById("clickup-error");
  if (message) {
    el.textContent = message;
    el.hidden = false;
  } else {
    el.hidden = true;
  }
}

function renderList(elementId, items, labelFn, onClick) {
  const el = document.getElementById(elementId);
  el.innerHTML = "";
  for (const item of items) {
    const li = document.createElement("li");
    const button = document.createElement("button");
    button.textContent = labelFn(item);
    button.addEventListener("click", () => onClick(item));
    li.appendChild(button);
    el.appendChild(li);
  }
}

async function selectClickupList(list) {
  try {
    const tasks = await fetchJson(`/api/clickup/tasks?list_id=${encodeURIComponent(list.id)}`);
    showClickupError(null);
    renderList(
      "clickup-tasks", tasks,
      (t) => `${t.name} (${t.status ? t.status.status : "-"})`,
      () => {},
    );
  } catch (err) {
    showClickupError(err.message);
  }
}

async function selectClickupFolder(folder) {
  document.getElementById("clickup-tasks").innerHTML = "";
  try {
    const lists = await fetchJson(`/api/clickup/lists?folder_id=${encodeURIComponent(folder.id)}`);
    showClickupError(null);
    renderList("clickup-lists", lists, (l) => l.name, selectClickupList);
  } catch (err) {
    showClickupError(err.message);
  }
}

async function selectClickupSpace(space) {
  document.getElementById("clickup-folders").innerHTML = "";
  document.getElementById("clickup-lists").innerHTML = "";
  document.getElementById("clickup-tasks").innerHTML = "";
  try {
    // ClickUp splits a space's lists into folders (Projects) and
    // "folderless" lists shown directly — fetch both, since a list like
    // "Sprint backlog" only shows up once you look inside its folder.
    const [folders, folderlessLists] = await Promise.all([
      fetchJson(`/api/clickup/folders?space_id=${encodeURIComponent(space.id)}`),
      fetchJson(`/api/clickup/lists?space_id=${encodeURIComponent(space.id)}`),
    ]);
    showClickupError(null);
    renderList("clickup-folders", folders, (f) => f.name, selectClickupFolder);
    renderList("clickup-lists", folderlessLists, (l) => l.name, selectClickupList);
  } catch (err) {
    showClickupError(err.message);
  }
}

async function selectClickupTeam(team) {
  document.getElementById("clickup-spaces").innerHTML = "";
  document.getElementById("clickup-folders").innerHTML = "";
  document.getElementById("clickup-lists").innerHTML = "";
  document.getElementById("clickup-tasks").innerHTML = "";
  try {
    const spaces = await fetchJson(`/api/clickup/spaces?team_id=${encodeURIComponent(team.id)}`);
    showClickupError(null);
    renderList("clickup-spaces", spaces, (s) => s.name, selectClickupSpace);
  } catch (err) {
    showClickupError(err.message);
  }
}

async function initClickupBrowser() {
  try {
    const teams = await fetchJson("/api/clickup/teams");
    showClickupError(null);
    renderList("clickup-teams", teams, (t) => t.name, selectClickupTeam);
  } catch (err) {
    showClickupError(err.message);
  }
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

document.addEventListener("DOMContentLoaded", () => {
  initTabs();
  initQaFilters();
  initQaReportForm();
  loadFindings();
  initClickupBrowser();
  initTicketsTab();
});
