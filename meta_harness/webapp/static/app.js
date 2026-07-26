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

async function fetchJson(url, options) {
  const response = await fetch(url, options);
  const body = await response.json().catch(() => ({}));
  if (!response.ok) {
    const detail = body.detail || `Request failed (${response.status})`;
    throw new Error(detail);
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
      `<td>${finding.id}</td><td>${finding.project}</td><td>${finding.route}</td>` +
      `<td class="${severityClass(finding.severity)}">${finding.severity}</td>` +
      `<td>${finding.status}</td><td>${finding.clickup_task_id || "-"}</td>` +
      `<td>${finding.observation}</td>`;
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

async function initClickupBrowser() {
  try {
    const teams = await fetchJson("/api/clickup/teams");
    showClickupError(null);
    renderList("clickup-teams", teams, (t) => t.name, async (team) => {
      document.getElementById("clickup-lists").innerHTML = "";
      document.getElementById("clickup-tasks").innerHTML = "";
      try {
        const spaces = await fetchJson(`/api/clickup/spaces?team_id=${encodeURIComponent(team.id)}`);
        showClickupError(null);
        renderList("clickup-spaces", spaces, (s) => s.name, async (space) => {
          document.getElementById("clickup-tasks").innerHTML = "";
          try {
            const lists = await fetchJson(`/api/clickup/lists?space_id=${encodeURIComponent(space.id)}`);
            showClickupError(null);
            renderList("clickup-lists", lists, (l) => l.name, async (list) => {
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
            });
          } catch (err) {
            showClickupError(err.message);
          }
        });
      } catch (err) {
        showClickupError(err.message);
      }
    });
  } catch (err) {
    showClickupError(err.message);
  }
}

document.addEventListener("DOMContentLoaded", () => {
  initTabs();
  initQaFilters();
  initQaReportForm();
  loadFindings();
  initClickupBrowser();
});
