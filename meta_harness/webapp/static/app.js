// Vanilla JS, no build step, no framework.
//
// The ClickUp and Linear tabs are the SAME code: everything below is written
// once against a tracker descriptor (see TRACKERS) and then instantiated once
// per tracker. That's what keeps the two tabs structurally identical — a
// change to the flow lands in both automatically instead of being copied.

// --- Shared helpers ---------------------------------------------------------

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

// Polls GET /api/progress/{token} while a long agent call is in flight, so the
// loading indicator shows what's actually happening. onSteps receives the FULL
// list of steps so far. Returns a stop function — call it once the main request
// settles; it does one final read first, to catch a step recorded just before
// the response landed.
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
      // ignore — best-effort final flush
    }
  };
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

function screenshotUrl(screenshotPath) {
  const name = screenshotPath.split("/").pop();
  return `/api/qa/screenshots/${encodeURIComponent(name)}`;
}

// Mirrors qa_flow.review_passed on the backend: minor counts as a pass,
// major/critical as a fail. Only used to preview the proposed move.
const PASSING_SEVERITIES = ["minor"];
function reviewPassed(severity) {
  return PASSING_SEVERITIES.includes(severity);
}

// The placeholder is always the first (empty-valued) option, including when
// there is nothing to choose — for optional dropdowns like Linear's project
// that empty state IS the valid answer ("No project"), so it must not be
// relabelled as an error.
function fillSelect(select, options, placeholder, { keepValue = true } = {}) {
  const previous = select.value;
  select.innerHTML = "";
  const first = document.createElement("option");
  first.value = "";
  first.textContent = placeholder;
  select.appendChild(first);
  for (const option of options) {
    const el = document.createElement("option");
    el.value = option.value;
    el.textContent = option.label;
    select.appendChild(el);
  }
  if (keepValue && options.some((o) => String(o.value) === previous)) select.value = previous;
}

// Base URLs are shared across both tabs — one project can be reviewed from
// either tracker, and it should keep the same app URL either way.
let projectBaseUrls = {};

async function loadProjectConfig() {
  try {
    const data = await fetchJson("/api/qa/project-config");
    projectBaseUrls = data.projects || {};
  } catch (err) {
    projectBaseUrls = {};
  }
}

// --- Tracker descriptors ----------------------------------------------------
// Everything tracker-specific lives here. The panel code below never mentions
// ClickUp or Linear directly.

const TRACKERS = {
  clickup: {
    key: "clickup",
    label: "ClickUp",
    noun: "ticket",
    Noun: "Ticket",
    createdIdField: "clickup_task_id",

    async loadScopes() {
      const teams = await fetchJson("/api/clickup/teams");
      const spacesByTeam = await Promise.all(
        teams.map((team) => fetchJson(`/api/clickup/spaces?team_id=${encodeURIComponent(team.id)}`)),
      );
      return spacesByTeam.flat().map((space) => ({
        id: space.id,
        name: space.name,
        statuses: (space.statuses || []).map((st) => ({ value: st.status, label: st.status })),
      }));
    },

    // ClickUp nests lists under optional folders; flattened into one labeled
    // dropdown so the user never has to make two separate picks.
    async loadContainers(scope) {
      const [folders, looseLists] = await Promise.all([
        fetchJson(`/api/clickup/folders?space_id=${encodeURIComponent(scope.id)}`),
        fetchJson(`/api/clickup/lists?space_id=${encodeURIComponent(scope.id)}`),
      ]);
      const containers = looseLists.map((list) => ({ id: list.id, name: list.name }));
      const listsByFolder = await Promise.all(
        folders.map((folder) => fetchJson(`/api/clickup/lists?folder_id=${encodeURIComponent(folder.id)}`)),
      );
      folders.forEach((folder, index) => {
        for (const list of listsByFolder[index]) {
          containers.push({ id: list.id, name: `${folder.name} / ${list.name}` });
        }
      });
      return containers;
    },

    async loadStatuses(scope) {
      // Already carried on the space object — no extra request needed.
      return scope.statuses || [];
    },

    containerRequired: true,

    async loadTickets(scope, container) {
      if (!container) return [];
      const tasks = await fetchJson(`/api/clickup/tasks?list_id=${encodeURIComponent(container.id)}`);
      return tasks.map((task) => ({
        id: task.id,
        name: task.name,
        stateLabel: task.status ? task.status.status : null,
      }));
    },

    createBody(tickets, scope, container) {
      return { tickets, list_id: container ? container.id : null };
    },
    createUrl: "/api/tickets/create",

    qaExtra() {
      return {};
    },
  },

  linear: {
    key: "linear",
    label: "Linear",
    noun: "issue",
    Noun: "Issue",
    createdIdField: "linear_issue_id",

    async loadScopes() {
      const teams = await fetchJson("/api/linear/teams");
      return teams.map((team) => ({ id: team.id, name: team.name, key: team.key }));
    },

    async loadContainers(scope) {
      const projects = await fetchJson(`/api/linear/projects?team_id=${encodeURIComponent(scope.id)}`);
      return projects.map((project) => ({ id: project.id, name: project.name }));
    },

    // Linear workflow states are per-team and need their own request.
    async loadStatuses(scope) {
      const states = await fetchJson(`/api/linear/states?team_id=${encodeURIComponent(scope.id)}`);
      return states.map((state) => ({ value: state.id, label: state.name }));
    },

    containerRequired: false,

    async loadTickets(scope) {
      const issues = await fetchJson(`/api/linear/issues?team_id=${encodeURIComponent(scope.id)}`);
      return issues.map((issue) => ({
        id: issue.id,
        name: `${issue.identifier} — ${issue.title}`,
        stateLabel: issue.state ? issue.state.name : null,
      }));
    },

    createBody(tickets, scope, container) {
      return { team_id: scope.id, tickets, project_id: container ? container.id : null };
    },
    createUrl: "/api/linear/issues",

    // Linear needs the team id to escalate a critical finding into a new issue.
    qaExtra(scope) {
      return { linear_team_id: scope ? scope.id : null };
    },
  },
};

// --- Ticket rendering (shared) ----------------------------------------------

// Criteria arrive as {name, given, when, then} so each tracker can lay them
// out its own way; this is the compact one-line form used in previews.
function criterionText(criterion) {
  if (!criterion) return "";
  if (criterion.text) return criterion.name ? `${criterion.name}: ${criterion.text}` : criterion.text;
  const clause = `Dado que ${criterion.given}, cuando ${criterion.when}, entonces ${criterion.then}.`;
  return criterion.name ? `${criterion.name}: ${clause}` : clause;
}

function ticketBadges(ticket) {
  return (
    `<span class="badge priority-${escapeHtml(ticket.priority)}">${escapeHtml(ticket.priority)}</span>` +
    `<span class="badge category-${escapeHtml(ticket.category)}">${escapeHtml(ticket.category)}</span>` +
    (ticket.parent_title ? `<span class="badge priority-low">subticket</span>` : "")
  );
}

function ticketBodyHtml(ticket) {
  const parts = [];
  if (ticket.epic) parts.push(`<p class="ticket-epic">${escapeHtml(ticket.epic)}</p>`);

  const routeBits = [];
  if (ticket.ui_route) routeBits.push(`<span><strong>Screen:</strong> <span class="mono">${escapeHtml(ticket.ui_route)}</span></span>`);
  if (ticket.backend_endpoint) routeBits.push(`<span><strong>Endpoint:</strong> <span class="mono">${escapeHtml(ticket.backend_endpoint)}</span></span>`);
  if (ticket.parent_title) routeBits.push(`<span><strong>Subticket of:</strong> ${escapeHtml(ticket.parent_title)}</span>`);
  if (routeBits.length) parts.push(`<div class="meta-row">${routeBits.join("")}</div>`);

  if (ticket.user_story) {
    parts.push(
      `<div class="ticket-section"><div class="ticket-section-label">User story</div>` +
        `<p class="user-story">${escapeHtml(ticket.user_story)}</p></div>`,
    );
  }
  parts.push(
    `<div class="ticket-section"><div class="ticket-section-label">Description</div>` +
      `<p>${escapeHtml(ticket.description)}</p></div>`,
  );
  if (ticket.acceptance_criteria && ticket.acceptance_criteria.length) {
    const items = ticket.acceptance_criteria.map((c) => `<li>${escapeHtml(criterionText(c))}</li>`).join("");
    parts.push(
      `<div class="ticket-section"><div class="ticket-section-label">Acceptance criteria</div><ul>${items}</ul></div>`,
    );
  }
  if (ticket.technical_notes) {
    parts.push(
      `<div class="ticket-section"><div class="ticket-section-label">Technical notes</div>` +
        `<p>${escapeHtml(ticket.technical_notes)}</p></div>`,
    );
  }
  return parts.join("");
}

function renderTicketCard(entry, index, tracker, onCreate) {
  const ticket = entry.ticket;
  const card = document.createElement("div");
  card.className = "ticket-card";

  const parts = [`<h3>${escapeHtml(ticket.title)}${ticketBadges(ticket)}</h3>`, ticketBodyHtml(ticket)];

  const planning = [];
  if (ticket.sprint) planning.push(`<span><strong>Sprint:</strong> ${escapeHtml(ticket.sprint)}</span>`);
  if (ticket.due_date) planning.push(`<span><strong>Due:</strong> ${escapeHtml(ticket.due_date)}</span>`);
  if (ticket.assignee_name) {
    planning.push(`<span><strong>Assignee:</strong> ${escapeHtml(ticket.assignee_name)}</span>`);
  }
  if (planning.length) parts.push(`<div class="meta-row">${planning.join("")}</div>`);

  parts.push(`<div class="ticket-status"></div>`);
  card.innerHTML = parts.join("");

  const statusEl = card.querySelector(".ticket-status");
  if (entry.status === "created") {
    statusEl.textContent = `✓ Added to ${tracker.label} (${entry.createdId})`;
    statusEl.classList.add("ticket-status-ok");
  } else if (entry.status === "error") {
    statusEl.textContent = `✗ Could not add: ${entry.error}`;
    statusEl.classList.add("ticket-status-error");
  } else if (entry.status === "creating") {
    statusEl.textContent = "Adding…";
  }

  if (entry.status !== "created" && entry.status !== "creating") {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "btn-success";
    button.textContent = `Add this ${tracker.noun} to ${tracker.label}`;
    button.addEventListener("click", () => onCreate(index));
    card.appendChild(button);
  }
  return card;
}

// --- One tracker panel ------------------------------------------------------

function createTrackerPanel(tracker) {
  const prefix = tracker.key;
  const el = (name) => document.getElementById(`${prefix}-${name}`);

  const state = {
    scopes: [],
    scope: null,
    containers: [],
    container: null,
    statuses: [],
    tickets: [],
    selectedTicket: null,
    proposed: [], // [{ ticket, status, createdId, error }]
    members: [], // [{ email, username, clickup_id, linear_id, selected }]
    chatHistory: [],
    bulkResults: [],
    bulkReadme: "",
    moduleReport: "",
    knownProjects: [],
  };

  // --- small UI helpers, all scoped to this panel ---------------------------

  const banner = (name, message) => {
    const node = el(name);
    if (!node) return;
    if (message) {
      node.textContent = message;
      node.hidden = false;
    } else {
      node.hidden = true;
    }
  };

  const thinking = (name, visible, message) => {
    const node = el(`${name}-thinking`);
    node.hidden = !visible;
    if (visible) node.querySelector(".thinking-text").textContent = message || "Working…";
  };

  const phaseLog = (name, steps) => {
    const node = el(`${name}-phase-log`);
    if (!node) return;
    node.innerHTML = steps.map((step) => `<li>${escapeHtml(step)}</li>`).join("");
    node.scrollTop = node.scrollHeight;
  };

  const projectName = () => (state.scope ? state.scope.name : "");

  // --- Step 1: destination --------------------------------------------------

  function renderDestinationSummary() {
    const node = el("destination-summary");
    if (!state.scope) {
      node.className = "selected-callout is-empty";
      node.textContent = "No destination selected yet.";
      return;
    }
    node.className = "selected-callout";
    const where = state.container
      ? `<strong>${escapeHtml(state.scope.name)}</strong> › <strong>${escapeHtml(state.container.name)}</strong>`
      : `<strong>${escapeHtml(state.scope.name)}</strong>`;
    node.innerHTML = `New ${tracker.noun}s go to ${where}. QA reviews are recorded under project “${escapeHtml(projectName())}”.`;
  }

  async function loadScopes() {
    banner("scope-error", null);
    thinking("scope", true, `Loading ${tracker.label}…`);
    try {
      state.scopes = await tracker.loadScopes();
      fillSelect(
        el("scope"),
        state.scopes.map((s) => ({ value: s.id, label: s.name })),
        "Select…",
      );
      refreshProjectFilterOptions();
    } catch (err) {
      banner("scope-error", err.message);
    } finally {
      thinking("scope", false);
    }
  }

  async function onScopeChange() {
    const scopeId = el("scope").value;
    state.scope = state.scopes.find((s) => String(s.id) === scopeId) || null;
    state.container = null;
    state.containers = [];
    state.statuses = [];
    resetTicketSelection();
    clearBulkResults();
    renderDestinationSummary();
    updateBaseUrlField();
    refreshProjectFilterOptions();

    const containerSelect = el("container");
    containerSelect.innerHTML = '<option value="">Select above first</option>';
    containerSelect.disabled = true;
    if (!state.scope) {
      fillSelect(el("qa-status-pass"), [], "Leave unchanged");
      fillSelect(el("qa-status-fail"), [], "Leave unchanged");
      return;
    }

    banner("scope-error", null);
    thinking("scope", true, "Loading destinations and statuses…");
    try {
      const [containers, statuses] = await Promise.all([
        tracker.loadContainers(state.scope),
        tracker.loadStatuses(state.scope),
      ]);
      state.containers = containers;
      state.statuses = statuses;
      fillSelect(
        containerSelect,
        containers.map((c) => ({ value: c.id, label: c.name })),
        tracker.containerRequired ? "Select…" : "No project",
      );
      containerSelect.disabled = false;
      fillSelect(el("qa-status-pass"), statuses, "Leave unchanged");
      fillSelect(el("qa-status-fail"), statuses, "Leave unchanged");
      if (!tracker.containerRequired) await loadTickets();
    } catch (err) {
      banner("scope-error", err.message);
    } finally {
      thinking("scope", false);
    }
    // Roster depends only on the scope, so refresh it as soon as one is picked.
    loadTeamMembers();
  }

  async function onContainerChange() {
    const containerId = el("container").value;
    state.container = state.containers.find((c) => String(c.id) === containerId) || null;
    renderDestinationSummary();
    resetTicketSelection();
    clearBulkResults();
    await loadTickets();
  }

  async function loadTickets() {
    // One fetch feeds every ticket dropdown on the tab (QA review and the
    // module check), so they can never drift out of sync.
    const selects = [el("qa-ticket"), el("module-ticket")];
    if (!state.scope || (tracker.containerRequired && !state.container)) {
      selects.forEach((select) => {
        select.innerHTML = '<option value="">Choose a destination in step 1 first</option>';
        select.disabled = true;
      });
      el("qa-bulk-run-btn").disabled = true;
      el("module-check-all-btn").disabled = true;
      return;
    }
    thinking("scope", true, `Loading ${tracker.noun}s…`);
    try {
      state.tickets = await tracker.loadTickets(state.scope, state.container);
      const options = state.tickets.map((t) => ({
        value: t.id,
        label: t.stateLabel ? `${t.name} (${t.stateLabel})` : t.name,
      }));
      selects.forEach((select) => {
        fillSelect(select, options, `Select a ${tracker.noun}…`, { keepValue: false });
        select.disabled = state.tickets.length === 0;
      });
      el("qa-bulk-run-btn").disabled = state.tickets.length === 0;
      el("module-check-all-btn").disabled = state.tickets.length === 0;
    } catch (err) {
      banner("scope-error", err.message);
      selects.forEach((select) => {
        select.innerHTML = '<option value="">Failed to load</option>';
        select.disabled = true;
      });
    } finally {
      thinking("scope", false);
    }
  }

  // --- Step 2: generation ---------------------------------------------------

  function setMode(mode) {
    el("mode-document").hidden = mode !== "document";
    el("mode-chat").hidden = mode !== "chat";
    el("mode-switch")
      .querySelectorAll("button")
      .forEach((b) => b.classList.toggle("active", b.dataset.mode === mode));
  }

  function startNumbers() {
    return {
      start_mundane: el("start-mundane").value || "1",
      start_backend: el("start-backend").value || "1",
      start_frontend: el("start-frontend").value || "1",
      start_deployment: el("start-deployment").value || "1",
    };
  }

  function renderProposed() {
    const container = el("tickets-list");
    container.innerHTML = "";
    state.proposed.forEach((entry, index) => {
      container.appendChild(renderTicketCard(entry, index, tracker, createOne));
    });
    el("generate-actions").hidden = state.proposed.length === 0;
  }

  function acceptGenerated(body, successMessage) {
    state.proposed = body.tickets.map((ticket) => ({ ticket, status: "pending" }));
    banner("generate-warnings", body.warnings && body.warnings.length ? `Note: ${body.warnings.join("; ")}` : null);
    renderProposed();
    banner("generate-success", successMessage);
  }

  async function onGenerateFromDocument(event) {
    event.preventDefault();
    const file = el("file-input").files[0];
    if (!file) return;
    banner("generate-error", null);
    banner("generate-success", null);
    banner("generate-warnings", null);

    const formData = new FormData();
    formData.append("file", file);
    Object.entries(startNumbers()).forEach(([key, value]) => formData.append(key, value));
    const emails = selectedMemberEmails();
    if (emails.length) formData.append("team_emails_text", emails.join("\n"));
    if (tracker.key === "linear" && state.scope) formData.append("linear_team_id", state.scope.id);
    const start = el("project-start").value;
    const end = el("project-end").value;
    if (start) formData.append("project_start", start);
    if (end) formData.append("project_end", end);
    const token = newProgressToken();
    formData.append("progress_token", token);

    const submit = event.target.querySelector('button[type="submit"]');
    submit.disabled = true;
    thinking("generate", true, "Starting…");
    const stop = pollProgress(token, (steps) => {
      thinking("generate", true, steps[steps.length - 1]);
      phaseLog("generate", steps);
    });
    try {
      const body = await fetchJson("/api/tickets/generate", { method: "POST", body: formData });
      acceptGenerated(body, `${body.tickets.length} ${tracker.noun}(s) drafted — review them below, then add them.`);
    } catch (err) {
      banner("generate-error", err.message);
    } finally {
      await stop();
      submit.disabled = false;
      thinking("generate", false);
    }
  }

  // --- chat mode ------------------------------------------------------------

  // The conversation holds plain text turns AND drafted-ticket turns. A
  // ticket turn renders as a wide bubble with its own Add/Discard buttons, so
  // the user never has to hunt for the result somewhere further down the page.
  function renderChat() {
    const log = el("chat-log");
    if (!state.chatHistory.length) {
      log.innerHTML =
        `<div class="chat-empty">Describe what you need — for example “necesito una pantalla para exportar ` +
        `candidatos a Excel”. You'll get back a fully formatted ${tracker.noun} you can review and add.</div>`;
      return;
    }

    log.innerHTML = "";
    state.chatHistory.forEach((turn, turnIndex) => {
      if (turn.kind !== "tickets") {
        const bubble = document.createElement("div");
        bubble.className = `chat-msg chat-msg-${turn.role === "assistant" ? "bot" : "user"}`;
        bubble.textContent = turn.content;
        log.appendChild(bubble);
        return;
      }

      const bubble = document.createElement("div");
      bubble.className = "chat-msg chat-msg-ticket";
      const heading =
        turn.entries.length === 1
          ? `Here's the ${tracker.noun} I drafted:`
          : `Here are the ${turn.entries.length} ${tracker.noun}s I drafted:`;
      bubble.innerHTML = `<p class="ticket-section-label">${escapeHtml(heading)}</p>`;

      turn.entries.forEach((entry) => {
        const block = document.createElement("div");
        block.className = entry.ticket.parent_title ? "chat-ticket-body chat-subticket" : "chat-ticket-body";
        block.innerHTML =
          `<div class="chat-ticket-title">${escapeHtml(entry.ticket.title)}${ticketBadges(entry.ticket)}</div>` +
          ticketBodyHtml(entry.ticket) +
          `<div class="ticket-status"></div>`;
        const statusEl = block.querySelector(".ticket-status");
        if (entry.status === "created") {
          statusEl.textContent = `✓ Added to ${tracker.label} (${entry.createdId})`;
          statusEl.classList.add("ticket-status-ok");
        } else if (entry.status === "error") {
          statusEl.textContent = `✗ Could not add: ${entry.error}`;
          statusEl.classList.add("ticket-status-error");
        } else if (entry.status === "creating") {
          statusEl.textContent = "Adding…";
        }
        bubble.appendChild(block);
      });

      const pending = turn.entries.filter((e) => e.status !== "created");
      if (pending.length && turn.status !== "discarded") {
        const actions = document.createElement("div");
        actions.className = "chat-ticket-actions";

        const add = document.createElement("button");
        add.type = "button";
        add.className = "btn-success";
        add.textContent =
          turn.entries.length === 1
            ? `Add ${tracker.noun} to ${tracker.label}`
            : `Add all ${turn.entries.length} to ${tracker.label}`;
        add.disabled = turn.entries.some((e) => e.status === "creating");
        add.addEventListener("click", () => addChatTickets(turnIndex));

        const discard = document.createElement("button");
        discard.type = "button";
        discard.textContent = "Discard";
        discard.addEventListener("click", () => {
          turn.status = "discarded";
          renderChat();
        });

        actions.append(add, discard);
        bubble.appendChild(actions);
      } else if (turn.status === "discarded") {
        const note = document.createElement("div");
        note.className = "field-note";
        note.textContent = "Discarded.";
        bubble.appendChild(note);
      }

      log.appendChild(bubble);
    });

    log.scrollTop = log.scrollHeight;
  }

  async function addChatTickets(turnIndex) {
    if (!destinationReady()) return;
    const turn = state.chatHistory[turnIndex];
    const pending = turn.entries.filter((e) => e.status !== "created");
    if (!pending.length) return;
    pending.forEach((e) => (e.status = "creating"));
    renderChat();
    try {
      await postCreate(pending);
    } catch (err) {
      pending.forEach((e) => {
        e.status = "error";
        e.error = err.message;
      });
    }
    renderChat();
    loadTickets();
  }

  async function onChatSubmit(event) {
    event.preventDefault();
    const input = el("chat-input");
    const idea = input.value.trim();
    if (!idea) return;
    banner("generate-error", null);
    banner("generate-success", null);
    banner("generate-warnings", null);

    // Only the plain-text turns are conversation context for the model; a
    // drafted-ticket turn is replayed as the summary line it produced.
    const history = state.chatHistory
      .filter((turn) => turn.kind !== "tickets" || turn.summary)
      .map((turn) => ({ role: turn.role, content: turn.kind === "tickets" ? turn.summary : turn.content }));
    state.chatHistory.push({ role: "user", content: idea });
    renderChat();
    input.value = "";

    const submit = event.target.querySelector('button[type="submit"]');
    submit.disabled = true;
    const token = newProgressToken();
    thinking("generate", true, "Starting…");
    const stop = pollProgress(token, (steps) => {
      thinking("generate", true, steps[steps.length - 1]);
      phaseLog("generate", steps);
    });
    try {
      const body = await fetchJson("/api/tickets/from-idea", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ idea, history, ...startNumbers(), progress_token: token }),
      });
      state.chatHistory.push({
        role: "assistant",
        kind: "tickets",
        entries: body.tickets.map((ticket) => ({ ticket, status: "pending" })),
        // Replayed as this model's own prior turn when the user refines.
        summary: body.tickets.map((t) => `- ${t.title}: ${t.description}`).join("\n"),
      });
      renderChat();
      banner("generate-warnings", body.warnings && body.warnings.length ? `Note: ${body.warnings.join("; ")}` : null);
    } catch (err) {
      state.chatHistory.push({ role: "assistant", content: `Sorry — ${err.message}` });
      renderChat();
      banner("generate-error", err.message);
    } finally {
      await stop();
      submit.disabled = false;
      thinking("generate", false);
    }
  }

  function onChatReset() {
    state.chatHistory = [];
    state.proposed = [];
    renderChat();
    renderProposed();
    banner("generate-success", null);
    banner("generate-error", null);
    banner("generate-warnings", null);
  }

  // --- creating -------------------------------------------------------------

  function destinationReady() {
    if (!state.scope) {
      banner("generate-error", "Choose a destination in step 1 first.");
      return false;
    }
    if (tracker.containerRequired && !state.container) {
      banner("generate-error", `Choose a ${tracker.key === "clickup" ? "list" : "project"} in step 1 first.`);
      return false;
    }
    return true;
  }

  function applyResult(entry, result) {
    if (result && result.ok) {
      entry.status = "created";
      entry.createdId = result[tracker.createdIdField];
    } else {
      entry.status = "error";
      entry.error = (result && result.error) || "Unknown error";
    }
  }

  async function postCreate(entries) {
    const body = await fetchJson(tracker.createUrl, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(tracker.createBody(entries.map((e) => e.ticket), state.scope, state.container)),
    });
    entries.forEach((entry, i) => applyResult(entry, body.results[i]));
  }

  async function createOne(index) {
    if (!destinationReady()) return;
    const entry = state.proposed[index];
    entry.status = "creating";
    renderProposed();
    try {
      await postCreate([entry]);
    } catch (err) {
      entry.status = "error";
      entry.error = err.message;
    }
    renderProposed();
    loadTickets();
  }

  async function onCreateAll() {
    if (!destinationReady()) return;
    const pending = state.proposed.filter((e) => e.status !== "created");
    if (!pending.length) return;
    banner("generate-success", null);
    banner("generate-error", null);
    pending.forEach((e) => (e.status = "creating"));
    renderProposed();
    const button = el("create-all");
    button.disabled = true;
    thinking("generate", true, `Adding ${pending.length} ${tracker.noun}(s) to ${tracker.label}…`);
    try {
      await postCreate(pending);
      const created = pending.filter((e) => e.status === "created").length;
      const failed = pending.length - created;
      banner(
        "generate-success",
        failed
          ? `Added ${created} of ${pending.length} — ${failed} failed, see the cards below.`
          : `Added ${created} ${tracker.noun}(s) to ${tracker.label}.`,
      );
    } catch (err) {
      banner("generate-error", err.message);
      pending.forEach((e) => (e.status = "pending"));
    } finally {
      button.disabled = false;
      thinking("generate", false);
      renderProposed();
      loadTickets();
    }
  }

  function onClearTickets() {
    state.proposed = [];
    renderProposed();
    banner("generate-success", null);
    banner("generate-warnings", null);
  }

  // --- auto-detected team members ------------------------------------------

  function renderTeamMembers() {
    const node = el("team-members");
    if (!state.members.length) {
      node.innerHTML = state.scope
        ? '<span class="field-note">No assignable members found in this workspace.</span>'
        : '<span class="field-note">Choose a destination in step 1 to load your team.</span>';
      return;
    }
    node.innerHTML = state.members
      .map(
        (member, index) =>
          `<label class="member-chip">` +
          `<input type="checkbox" data-member="${index}"${member.selected ? " checked" : ""}>` +
          `<span class="member-chip-name">${escapeHtml(member.username)}</span>` +
          `<span class="member-chip-email">${escapeHtml(member.email)}</span>` +
          `</label>`,
      )
      .join("");
    node.querySelectorAll("input[data-member]").forEach((input) => {
      input.addEventListener("change", () => {
        state.members[Number(input.dataset.member)].selected = input.checked;
      });
    });
  }

  async function loadTeamMembers() {
    if (!state.scope) {
      state.members = [];
      renderTeamMembers();
      return;
    }
    const button = el("refresh-team-btn");
    button.disabled = true;
    try {
      const params = new URLSearchParams({ tracker: tracker.key });
      if (tracker.key === "linear") params.set("linear_team_id", state.scope.id);
      const members = await fetchJson(`/api/tickets/team-members?${params.toString()}`);
      // Everyone is opted in by default: the common case is "assign across the
      // whole team", so it should need no clicks at all.
      state.members = members.map((member) => ({ ...member, selected: true }));
    } catch (err) {
      state.members = [];
      el("team-members").innerHTML = `<span class="error-text">${escapeHtml(err.message)}</span>`;
      return;
    } finally {
      button.disabled = false;
    }
    renderTeamMembers();
  }

  function setAllMembers(selected) {
    state.members.forEach((member) => (member.selected = selected));
    renderTeamMembers();
  }

  function selectedMemberEmails() {
    return state.members.filter((m) => m.selected).map((m) => m.email);
  }

  // --- Step 3: QA review ----------------------------------------------------

  function resetTicketSelection() {
    state.selectedTicket = null;
    const node = el("qa-selected");
    node.className = "selected-callout is-empty";
    node.textContent = `No ${tracker.noun} selected.`;
    el("qa-analyze-btn").disabled = true;
    el("qa-result").hidden = true;
    banner("qa-error", null);
    banner("qa-success", null);
  }

  function onTicketChange() {
    const id = el("qa-ticket").value;
    if (!id) {
      resetTicketSelection();
      return;
    }
    const ticket = state.tickets.find((t) => String(t.id) === id);
    if (!ticket) return;
    state.selectedTicket = ticket;
    const node = el("qa-selected");
    node.className = "selected-callout";
    node.innerHTML = `Reviewing <strong>${escapeHtml(ticket.name)}</strong>`;
    el("qa-analyze-btn").disabled = false;
    el("qa-result").hidden = true;
    banner("qa-error", null);
    banner("qa-success", null);
  }

  function updateBaseUrlField() {
    el("qa-base-url").value = projectBaseUrls[projectName()] || "";
    el("qa-base-url-status").textContent = "";
  }

  async function onSaveBaseUrl() {
    const project = projectName();
    const baseUrl = el("qa-base-url").value.trim();
    const status = el("qa-base-url-status");
    status.className = "success-text";
    if (!project) {
      status.className = "error-text";
      status.textContent = "Choose a destination in step 1 first.";
      return;
    }
    if (!baseUrl) {
      status.className = "error-text";
      status.textContent = "Enter a URL first.";
      return;
    }
    try {
      const data = await fetchJson("/api/qa/project-config", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ project, base_url: baseUrl }),
      });
      projectBaseUrls = data.projects || {};
      status.textContent = "Saved.";
    } catch (err) {
      status.className = "error-text";
      status.textContent = err.message;
    }
  }

  function qaBody(extra) {
    return {
      project: projectName(),
      tracker: tracker.key,
      ...tracker.qaExtra(state.scope),
      ...extra,
    };
  }

  function renderQaResult(review, finding, reportMarkdown) {
    const container = el("qa-result");
    container.hidden = false;
    const severity = escapeHtml(review.severity);
    const verdict = reviewPassed(review.severity) ? "PASS" : "FAIL";

    let html =
      `<h3>${escapeHtml(review.ticket_name)}` +
      `<span class="badge severity-${severity}">${severity} · ${verdict}</span></h3>` +
      `<div class="ticket-section"><div class="ticket-section-label">What QA found</div>` +
      `<p>${escapeHtml(review.observation)}</p></div>`;

    const rows = [];
    if (review.route) rows.push(`<div><strong>Route checked:</strong> <span class="mono">${escapeHtml(review.route)}</span></div>`);
    if (review.status_code != null) rows.push(`<div><strong>HTTP status:</strong> ${review.status_code}</div>`);
    if (review.http_error) rows.push(`<div class="error-text"><strong>Error:</strong> ${escapeHtml(review.http_error)}</div>`);
    if (rows.length) html += `<div class="evidence">${rows.join("")}</div>`;
    if (review.screenshot_path) {
      html += `<img class="evidence-image" src="${screenshotUrl(review.screenshot_path)}" alt="Screenshot evidence">`;
    }

    html += `<div class="ticket-status"></div><div class="report-actions"></div>`;
    container.innerHTML = html;

    const actions = container.querySelector(".report-actions");
    if (finding) {
      for (const [href, text] of [
        [`/api/qa/findings/${finding.id}/report.md`, "⬇ Markdown"],
        [`/api/qa/findings/${finding.id}/report.pdf`, "⬇ PDF"],
      ]) {
        const link = document.createElement("a");
        link.href = href;
        link.textContent = text;
        link.setAttribute("download", "");
        actions.appendChild(link);
      }
    } else if (reportMarkdown) {
      const button = document.createElement("button");
      button.type = "button";
      button.textContent = "⬇ Markdown";
      button.addEventListener("click", () => downloadTextFile(`qa-review-${review.ticket_id}.md`, reportMarkdown));
      actions.appendChild(button);
    }

    const statusEl = container.querySelector(".ticket-status");
    if (finding) {
      const linked = finding[tracker.createdIdField];
      statusEl.textContent = `✓ Saved as finding #${finding.id}${linked ? ` · linked ${tracker.label} item ${linked}` : ""}`;
      statusEl.classList.add("ticket-status-ok");
    } else {
      statusEl.textContent = "Preview only — nothing saved yet.";
      const button = document.createElement("button");
      button.type = "button";
      button.className = "btn-success";
      button.textContent = `Save this result${moveTargetLabel(review.severity) ? ` and move the ${tracker.noun}` : ""}`;
      button.addEventListener("click", () => persistReview(review, button));
      container.appendChild(button);
    }
  }

  function moveTargetLabel(severity) {
    const value = reviewPassed(severity) ? el("qa-status-pass").value : el("qa-status-fail").value;
    if (!value) return null;
    const match = state.statuses.find((s) => String(s.value) === value);
    return match ? match.label : value;
  }

  async function persistReview(review, button) {
    button.disabled = true;
    banner("qa-error", null);
    banner("qa-success", null);
    const token = newProgressToken();
    let lastStep = "";
    thinking("qa", true, "Saving…");
    const stop = pollProgress(token, (steps) => {
      lastStep = steps[steps.length - 1];
      thinking("qa", true, lastStep);
      phaseLog("qa", steps);
    });
    try {
      const finding = await fetchJson("/api/qa/reviews/commit", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(
          qaBody({
            ticket_id: review.ticket_id,
            ticket_name: review.ticket_name,
            observation: review.observation,
            severity: review.severity,
            pass_status: el("qa-status-pass").value || null,
            fail_status: el("qa-status-fail").value || null,
            route: review.route || null,
            status_code: review.status_code != null ? review.status_code : null,
            http_error: review.http_error || null,
            screenshot_path: review.screenshot_path || null,
            progress_token: token,
          }),
        ),
      });
      await stop();
      renderQaResult(review, finding);
      const moved = /^(Moved|Finding persisted, but could not move)/.test(lastStep) ? ` ${lastStep}` : "";
      banner("qa-success", `Finding #${finding.id} saved.${moved}`);
      await Promise.all([loadFindings(), loadTickets()]);
    } catch (err) {
      await stop();
      button.disabled = false;
      banner("qa-error", err.message);
    } finally {
      thinking("qa", false);
    }
  }

  async function onAnalyze() {
    if (!state.selectedTicket) return;
    if (!projectName()) {
      banner("qa-error", "Choose a destination in step 1 first.");
      return;
    }
    banner("qa-error", null);
    banner("qa-success", null);
    const token = newProgressToken();
    el("qa-analyze-btn").disabled = true;
    thinking("qa", true, "Starting…");
    const stop = pollProgress(token, (steps) => {
      thinking("qa", true, steps[steps.length - 1]);
      phaseLog("qa", steps);
    });
    try {
      const result = await fetchJson("/api/qa/reviews", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(qaBody({ ticket_id: state.selectedTicket.id, persist: false, progress_token: token })),
      });
      renderQaResult(result.review, result.finding, result.report_markdown);
      banner("qa-success", "Review complete — check the result below, then save it if it looks right.");
      await loadReviewRuns();
    } catch (err) {
      banner("qa-error", err.message);
    } finally {
      await stop();
      el("qa-analyze-btn").disabled = false;
      thinking("qa", false);
    }
  }

  async function loadReviewRuns() {
    const select = el("qa-runs-select");
    try {
      const runs = await fetchJson("/api/qa/reviews");
      fillSelect(
        select,
        runs.map((run) => ({ value: run.run_id, label: `${run.ticket_id || "-"} · ${run.started_at}` })),
        "Select a recorded review…",
      );
    } catch (err) {
      banner("qa-error", err.message);
    }
  }

  async function onReplay() {
    const runId = el("qa-runs-select").value;
    if (!runId) return;
    banner("qa-error", null);
    banner("qa-success", null);
    const token = newProgressToken();
    thinking("qa", true, "Starting…");
    const stop = pollProgress(token, (steps) => {
      thinking("qa", true, steps[steps.length - 1]);
      phaseLog("qa", steps);
    });
    try {
      const result = await fetchJson(`/api/qa/reviews/${encodeURIComponent(runId)}/replay`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          persist: false,
          progress_token: token,
          ...tracker.qaExtra(state.scope),
        }),
      });
      renderQaResult(result.review, result.finding, result.report_markdown);
      banner("qa-success", "Replay complete — check the result below.");
    } catch (err) {
      banner("qa-error", err.message);
    } finally {
      await stop();
      thinking("qa", false);
    }
  }

  // --- bulk QA --------------------------------------------------------------

  function clearBulkResults() {
    state.bulkResults = [];
    state.bulkReadme = "";
    const node = el("qa-bulk-results");
    if (node) node.hidden = true;
    const body = document.querySelector(`#${prefix}-qa-bulk-table tbody`);
    if (body) body.innerHTML = "";
  }

  function renderBulkResults() {
    const tbody = document.querySelector(`#${prefix}-qa-bulk-table tbody`);
    tbody.innerHTML = "";
    for (const entry of state.bulkResults) {
      const row = document.createElement("tr");
      if (entry.error) {
        row.innerHTML =
          `<td>${escapeHtml(entry.ticket_name)}</td>` +
          `<td class="error-text">Could not review: ${escapeHtml(entry.error)}</td><td>—</td><td>—</td>`;
      } else {
        const passed = reviewPassed(entry.review.severity);
        const target = moveTargetLabel(entry.review.severity);
        const bits = [];
        if (entry.review.route) bits.push(escapeHtml(entry.review.route));
        if (entry.review.status_code != null) bits.push(`HTTP ${entry.review.status_code}`);
        let evidence = bits.join(" · ") || "—";
        if (entry.review.screenshot_path) {
          evidence += ` <a href="${screenshotUrl(entry.review.screenshot_path)}" target="_blank" rel="noopener">screenshot</a>`;
        }
        row.innerHTML =
          `<td>${escapeHtml(entry.ticket_name)}</td>` +
          `<td><span class="badge ${severityClass(entry.review.severity)}">${escapeHtml(entry.review.severity)} · ${passed ? "PASS" : "FAIL"}</span></td>` +
          `<td>${evidence}</td>` +
          `<td>${target ? escapeHtml(target) : "Leave unchanged"}</td>`;
      }
      tbody.appendChild(row);
    }
    el("qa-bulk-results").hidden = false;
  }

  async function onRunBulk() {
    if (!state.tickets.length) return;
    if (!projectName()) {
      banner("qa-error", "Choose a destination in step 1 first.");
      return;
    }
    clearBulkResults();
    banner("qa-error", null);
    banner("qa-success", null);
    const button = el("qa-bulk-run-btn");
    button.disabled = true;
    const token = newProgressToken();
    thinking("qa", true, "Starting…");
    const stop = pollProgress(token, (steps) => {
      thinking("qa", true, steps[steps.length - 1]);
      phaseLog("qa", steps);
    });
    try {
      const body = await fetchJson("/api/qa/reviews/bulk", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(
          qaBody({
            ticket_ids: state.tickets.map((t) => t.id),
            progress_token: token,
            pass_status: el("qa-status-pass").value || null,
            fail_status: el("qa-status-fail").value || null,
          }),
        ),
      });
      state.bulkResults = body.results.map((r) => {
        const ticket = state.tickets.find((t) => String(t.id) === r.ticket_id);
        return {
          ticket_id: r.ticket_id,
          ticket_name: ticket ? ticket.name : r.ticket_id,
          review: r.review,
          error: r.error,
        };
      });
      state.bulkReadme = body.readme_markdown || "";
      renderBulkResults();
      const ok = state.bulkResults.filter((r) => !r.error).length;
      const failed = state.bulkResults.length - ok;
      banner(
        "qa-success",
        failed
          ? `Reviewed ${ok} of ${state.bulkResults.length} — ${failed} could not be reviewed.`
          : `Reviewed ${ok} ${tracker.noun}(s). Nothing is saved yet — check the table, then confirm.`,
      );
    } catch (err) {
      banner("qa-error", err.message);
    } finally {
      await stop();
      thinking("qa", false);
      button.disabled = false;
    }
  }

  async function onConfirmBulk() {
    const items = state.bulkResults
      .filter((r) => r.review && !r.error)
      .map((r) =>
        qaBody({
          ticket_id: r.review.ticket_id,
          ticket_name: r.review.ticket_name,
          observation: r.review.observation,
          severity: r.review.severity,
          pass_status: el("qa-status-pass").value || null,
          fail_status: el("qa-status-fail").value || null,
          route: r.review.route || null,
          status_code: r.review.status_code != null ? r.review.status_code : null,
          http_error: r.review.http_error || null,
          screenshot_path: r.review.screenshot_path || null,
        }),
      );
    if (!items.length) return;
    banner("qa-error", null);
    banner("qa-success", null);
    const button = el("qa-bulk-confirm-btn");
    button.disabled = true;
    const token = newProgressToken();
    thinking("qa", true, "Starting…");
    const stop = pollProgress(token, (steps) => {
      thinking("qa", true, steps[steps.length - 1]);
      phaseLog("qa", steps);
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
        const errors = body.results.filter((r) => r.error).map((r) => `${r.ticket_id}: ${r.error}`).join("; ");
        banner("qa-error", `${failed} finding(s) could not be saved — ${errors}`);
      }
      banner("qa-success", `Saved ${ok} of ${body.results.length} finding(s) and applied the configured moves.`);
      clearBulkResults();
      await Promise.all([loadFindings(), loadTickets()]);
    } catch (err) {
      banner("qa-error", err.message);
    } finally {
      await stop();
      thinking("qa", false);
      button.disabled = false;
    }
  }

  // --- Module relevance -----------------------------------------------------

  const VERDICT_LABELS = {
    related: "Belongs to this module",
    partially_related: "Partially related",
    unrelated: "Does not belong to this module",
  };

  function renderModuleResult(result) {
    const container = el("module-result");
    container.hidden = false;
    const verdict = escapeHtml(result.verdict);
    const percent = Math.round((result.confidence || 0) * 100);

    let html =
      `<h3>${escapeHtml(result.ticket_name)}` +
      `<span class="badge verdict-${verdict}">${escapeHtml(VERDICT_LABELS[result.verdict] || result.verdict)}</span></h3>` +
      `<div class="meta-row"><span><strong>Module:</strong> ${escapeHtml(result.module_name)}</span></div>` +
      `<div class="ticket-section"><div class="ticket-section-label">Confidence — ${percent}%</div>` +
      `<div class="confidence-bar"><span style="width:${percent}%"></span></div></div>` +
      `<div class="ticket-section"><div class="ticket-section-label">Reasoning</div>` +
      `<p>${escapeHtml(result.rationale)}</p></div>`;

    if (result.matched_aspects && result.matched_aspects.length) {
      html +=
        `<div class="ticket-section"><div class="ticket-section-label">Overlaps with the module</div><ul>` +
        result.matched_aspects.map((a) => `<li>${escapeHtml(a)}</li>`).join("") +
        `</ul></div>`;
    }
    if (result.module_gaps && result.module_gaps.length) {
      html +=
        `<div class="ticket-section"><div class="ticket-section-label">Falls outside the module</div><ul>` +
        result.module_gaps.map((g) => `<li>${escapeHtml(g)}</li>`).join("") +
        `</ul></div>`;
    }
    container.innerHTML = html;
  }

  function clearModuleBulk() {
    state.moduleReport = "";
    el("module-bulk-results").hidden = true;
    document.querySelector(`#${prefix}-module-bulk-table tbody`).innerHTML = "";
    el("module-aligned").innerHTML = "";
    banner("module-summary", null);
  }

  function renderModuleBulk(body) {
    // The aligned list is the answer to the question being asked, so it goes
    // first and in plain language; the table underneath is the evidence.
    const aligned = el("module-aligned");
    if (body.aligned.length) {
      aligned.innerHTML =
        `<div class="ticket-section-label">These ${tracker.noun}s belong to “${escapeHtml(body.module_name)}”</div>` +
        body.aligned
          .map(
            (r) =>
              `<div>${r.verdict === "related" ? "✅" : "🟡"} <strong>${escapeHtml(r.ticket_name)}</strong> — ` +
              `${escapeHtml(VERDICT_LABELS[r.verdict] || r.verdict)} (${Math.round(r.confidence * 100)}%)</div>`,
          )
          .join("");
    } else {
      aligned.innerHTML = `<div>No ${tracker.noun} in this list belongs to “${escapeHtml(body.module_name)}”.</div>`;
    }

    const tbody = document.querySelector(`#${prefix}-module-bulk-table tbody`);
    tbody.innerHTML = "";
    for (const item of body.results) {
      const row = document.createElement("tr");
      if (item.error || !item.relevance) {
        row.innerHTML =
          `<td class="mono">${escapeHtml(item.ticket_id)}</td>` +
          `<td class="error-text">Could not analyze</td><td>—</td><td>${escapeHtml(item.error || "")}</td>`;
      } else {
        const r = item.relevance;
        row.innerHTML =
          `<td>${escapeHtml(r.ticket_name)}</td>` +
          `<td><span class="badge verdict-${escapeHtml(r.verdict)}">${escapeHtml(VERDICT_LABELS[r.verdict] || r.verdict)}</span></td>` +
          `<td>${Math.round(r.confidence * 100)}%</td>` +
          `<td>${escapeHtml(r.rationale)}</td>`;
      }
      tbody.appendChild(row);
    }

    const s = body.summary;
    banner(
      "module-summary",
      `${s.related} belong to this module, ${s.partially_related} partially, ${s.unrelated} do not` +
        (s.failed ? `, ${s.failed} could not be analyzed` : "") +
        ` — out of ${s.analyzed + s.failed} ${tracker.noun}(s).`,
    );
    el("module-bulk-results").hidden = false;
  }

  function moduleInputs() {
    const moduleName = el("module-name").value.trim();
    const moduleContext = el("module-context").value.trim();
    if (!moduleName) {
      banner("module-error", "Give the module a name.");
      return null;
    }
    if (!moduleContext) {
      banner("module-error", "Paste the module's documentation — the verdict is based on it.");
      return null;
    }
    return { moduleName, moduleContext };
  }

  async function onCheckAllModule() {
    banner("module-error", null);
    if (!state.tickets.length) {
      banner("module-error", `Choose a destination in step 1 so there are ${tracker.noun}s to check.`);
      return;
    }
    const inputs = moduleInputs();
    if (!inputs) return;

    clearModuleBulk();
    el("module-result").hidden = true;
    const button = el("module-check-all-btn");
    button.disabled = true;
    const token = newProgressToken();
    thinking("module", true, "Starting…");
    const stop = pollProgress(token, (steps) => {
      thinking("module", true, steps[steps.length - 1]);
      phaseLog("module", steps);
    });
    try {
      const body = await fetchJson("/api/tickets/module-relevance/bulk", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          ticket_ids: state.tickets.map((t) => t.id),
          tracker: tracker.key,
          module_name: inputs.moduleName,
          module_context: inputs.moduleContext,
          progress_token: token,
        }),
      });
      state.moduleReport = body.report_markdown || "";
      renderModuleBulk(body);
    } catch (err) {
      banner("module-error", err.message);
    } finally {
      await stop();
      thinking("module", false);
      button.disabled = false;
    }
  }

  async function onCheckModule() {
    const ticketId = el("module-ticket").value;
    const moduleName = el("module-name").value.trim();
    const moduleContext = el("module-context").value.trim();
    banner("module-error", null);
    if (!ticketId) {
      banner("module-error", `Pick a ${tracker.noun} to check.`);
      return;
    }
    if (!moduleName) {
      banner("module-error", "Give the module a name.");
      return;
    }
    if (!moduleContext) {
      banner("module-error", "Paste the module's documentation — the verdict is based on it.");
      return;
    }
    el("module-result").hidden = true;
    clearModuleBulk();
    const button = el("module-check-btn");
    button.disabled = true;
    const token = newProgressToken();
    thinking("module", true, "Starting…");
    const stop = pollProgress(token, (steps) => {
      thinking("module", true, steps[steps.length - 1]);
      phaseLog("module", steps);
    });
    try {
      const result = await fetchJson("/api/tickets/module-relevance", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          ticket_id: ticketId,
          tracker: tracker.key,
          module_name: moduleName,
          module_context: moduleContext,
          progress_token: token,
        }),
      });
      renderModuleResult(result);
    } catch (err) {
      banner("module-error", err.message);
    } finally {
      await stop();
      thinking("module", false);
      button.disabled = false;
    }
  }

  // --- Step 5: findings -----------------------------------------------------

  function projectOptions() {
    const options = new Set(state.knownProjects);
    for (const scope of state.scopes) options.add(scope.name);
    return Array.from(options).sort();
  }

  function refreshProjectFilterOptions() {
    const options = projectOptions();

    const filter = el("filter-project");
    const previousFilter = filter.value;
    filter.innerHTML = '<option value="">Any project</option>';
    for (const name of options) {
      const option = document.createElement("option");
      option.value = name;
      option.textContent = name;
      filter.appendChild(option);
    }
    filter.value = previousFilter;

    // The manual-report form defaults to whatever destination is selected,
    // so the common case needs no extra pick.
    fillSelect(
      el("report-project"),
      options.map((name) => ({ value: name, label: name })),
      "Select a project…",
    );
    if (!el("report-project").value && projectName()) el("report-project").value = projectName();
  }

  async function onReportFinding() {
    const status = el("report-status");
    status.className = "success-text";
    const project = el("report-project").value;
    const route = el("report-route").value.trim();
    const observation = el("report-observation").value.trim();
    if (!project || !route || !observation) {
      status.className = "error-text";
      status.textContent = "Project, screen/endpoint and description are all required.";
      return;
    }
    const button = el("report-submit");
    button.disabled = true;
    try {
      await fetchJson("/api/qa/findings", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          project,
          route,
          observation,
          severity: el("report-severity").value,
          tracker: tracker.key,
          ...tracker.qaExtra(state.scope),
        }),
      });
      el("report-route").value = "";
      el("report-observation").value = "";
      status.textContent = "Finding reported.";
      await loadFindings();
    } catch (err) {
      status.className = "error-text";
      status.textContent = err.message;
    } finally {
      button.disabled = false;
    }
  }

  function renderCounts(counts) {
    el("findings-counts").innerHTML = [
      ["Total", counts.total],
      ["Open", counts.open],
      ["Acknowledged", counts.acknowledged],
      ["Closed", counts.closed],
      ["Critical", counts.critical],
      ["Major", counts.major],
      ["Minor", counts.minor],
    ]
      .map(([label, value]) => `<span class="count-chip">${label} <strong>${value}</strong></span>`)
      .join("");
  }

  function renderFindings(findings) {
    const tbody = document.querySelector(`#${prefix}-findings-table tbody`);
    tbody.innerHTML = "";
    if (!findings.length) {
      tbody.innerHTML = '<tr><td colspan="8" class="field-note">No findings recorded yet.</td></tr>';
      return;
    }
    for (const finding of findings) {
      const row = document.createElement("tr");
      const linked = finding.clickup_task_id || finding.linear_issue_id || "—";
      row.innerHTML =
        `<td>${escapeHtml(finding.id)}</td>` +
        `<td>${escapeHtml(finding.project)}</td>` +
        `<td>${escapeHtml(finding.route)}</td>` +
        `<td><span class="badge ${severityClass(finding.severity)}">${escapeHtml(finding.severity)}</span></td>` +
        `<td>${escapeHtml(finding.status)}</td>` +
        `<td class="mono">${escapeHtml(linked)}</td>` +
        `<td>${escapeHtml(finding.observation)}</td>`;

      const cell = document.createElement("td");
      if (finding.status !== "closed") {
        const input = document.createElement("input");
        input.type = "text";
        input.placeholder = "What was corrected?";
        const button = document.createElement("button");
        button.type = "button";
        button.textContent = "Close";
        const feedback = document.createElement("div");
        feedback.className = "error-text";
        button.addEventListener("click", async () => {
          if (!input.value.trim()) {
            feedback.textContent = "Describe the correction first.";
            return;
          }
          feedback.textContent = "";
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
        cell.append(input, button, feedback);
      }
      row.appendChild(cell);
      tbody.appendChild(row);
    }
  }

  async function loadFindings() {
    const params = new URLSearchParams();
    for (const name of ["project", "severity", "status"]) {
      const value = el(`filter-${name}`).value;
      if (value) params.set(name, value);
    }
    try {
      const data = await fetchJson(`/api/qa/findings?${params.toString()}`);
      renderCounts(data.counts);
      renderFindings(data.findings);
      state.knownProjects = Array.from(new Set(data.findings.map((f) => f.project))).sort();
      refreshProjectFilterOptions();
      banner("findings-error", null);
    } catch (err) {
      banner("findings-error", err.message);
    }
  }

  // --- wiring ---------------------------------------------------------------

  function init() {
    el("scope").addEventListener("change", onScopeChange);
    el("container").addEventListener("change", onContainerChange);

    el("mode-switch")
      .querySelectorAll("button")
      .forEach((button) => button.addEventListener("click", () => setMode(button.dataset.mode)));

    el("generate-form").addEventListener("submit", onGenerateFromDocument);
    el("chat-form").addEventListener("submit", onChatSubmit);
    el("chat-reset").addEventListener("click", onChatReset);
    el("create-all").addEventListener("click", onCreateAll);
    el("clear-tickets").addEventListener("click", onClearTickets);
    el("refresh-team-btn").addEventListener("click", loadTeamMembers);
    el("team-all-btn").addEventListener("click", () => setAllMembers(true));
    el("team-none-btn").addEventListener("click", () => setAllMembers(false));

    el("qa-ticket").addEventListener("change", onTicketChange);
    el("qa-base-url-save").addEventListener("click", onSaveBaseUrl);
    el("qa-analyze-btn").addEventListener("click", onAnalyze);
    el("qa-replay-btn").addEventListener("click", onReplay);
    el("qa-bulk-run-btn").addEventListener("click", onRunBulk);
    el("qa-bulk-confirm-btn").addEventListener("click", onConfirmBulk);
    el("qa-bulk-readme-btn").addEventListener("click", () => {
      if (state.bulkReadme) downloadTextFile("QA-README.md", state.bulkReadme);
    });

    for (const name of ["project", "severity", "status"]) {
      el(`filter-${name}`).addEventListener("change", loadFindings);
    }
    el("report-submit").addEventListener("click", onReportFinding);
    el("module-check-btn").addEventListener("click", onCheckModule);
    el("module-check-all-btn").addEventListener("click", onCheckAllModule);
    el("module-report-btn").addEventListener("click", () => {
      if (state.moduleReport) downloadTextFile("module-report.md", state.moduleReport);
    });

    renderChat();
    renderTeamMembers();
    loadScopes();
    loadFindings();
    loadReviewRuns();
  }

  return { init };
}

// --- Tabs + boot ------------------------------------------------------------

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

document.addEventListener("DOMContentLoaded", async () => {
  initTabs();
  await loadProjectConfig();
  for (const tracker of Object.values(TRACKERS)) {
    createTrackerPanel(tracker).init();
  }
});
