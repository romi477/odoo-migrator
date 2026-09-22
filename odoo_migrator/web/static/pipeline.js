/* Pipeline queue: compact next row, expandable full list, SSE updates. */
(function () {
  const panel = document.querySelector("[data-pipeline-queue-panel]");
  if (!panel) {
    return;
  }

  function queueItemMarkup(mod) {
    const active = mod.active ? " pipeline-queue-active" : "";
    const rank =
      mod.migration_rank != null && mod.step != null
        ? "#" + mod.migration_rank + "/" + mod.step
        : "";
    return (
      '<span class="pipeline-queue-order">' +
      mod.position +
      "</span>" +
      (rank
        ? '<span class="graph-node-index">' + rank + "</span>"
        : "") +
      '<span class="pipeline-lamp" aria-hidden="true"></span>' +
      '<span class="name mono">' +
      mod.technical_name +
      "</span>" +
      '<span class="badge">step ' +
      mod.step +
      "</span>"
    );
  }

  function buildRow(mod, tag) {
    const row = document.createElement(tag);
    row.className = "pipeline-queue-item" + (mod.active ? " pipeline-queue-active" : "");
    row.setAttribute("data-module-id", String(mod.id));
    row.innerHTML = queueItemMarkup(mod);

    return row;
  }

  function setActive(moduleId) {
    panel.querySelectorAll(".pipeline-queue-item").forEach(function (row) {
      row.classList.remove("pipeline-queue-active");
    });
    if (moduleId == null) {
      return;
    }
    panel.querySelectorAll('[data-module-id="' + moduleId + '"]').forEach(function (row) {
      row.classList.add("pipeline-queue-active");
    });
  }

  function renderQueue(data) {
    const migrationId = panel.getAttribute("data-migration-id");
    if (!migrationId || String(data.migration_id) !== migrationId) {
      return;
    }
    panel.setAttribute("data-running", data.run_state === "running" ? "true" : "false");
    const runBtn = panel.querySelector('form[action="/run/queue/run"] button');
    const stopBtn = panel.querySelector('form[action="/run/queue/stop"] button');
    if (runBtn) {
      runBtn.disabled = data.run_state === "running";
    }
    if (stopBtn) {
      stopBtn.disabled = data.run_state !== "running";
    }
    const meta = panel.querySelector("[data-pipeline-queue-meta]");
    if (meta) {
      const steps = data.steps ? data.steps.length : 0;
      const pending = data.modules.length;
      const runLabel = data.run_state === "running" ? "Running" : "Stopped";
      if (!pending) {
        meta.textContent = "Empty · Stopped";
      } else {
        meta.textContent =
          pending +
          " pending · " +
          steps +
          " step" +
          (steps === 1 ? "" : "s") +
          " · " +
          runLabel;
      }
    }
    const empty = panel.querySelector("[data-pipeline-queue-empty]");
    const body = panel.querySelector(".pipeline-queue-body");
    if (!data.modules.length) {
      if (body) {
        body.hidden = true;
      }
      if (empty) {
        empty.hidden = false;
      }

      return;
    }
    if (body) {
      body.hidden = false;
    }
    if (empty) {
      empty.hidden = true;
    }
    const nextSlot = panel.querySelector("[data-pipeline-queue-next]");
    if (nextSlot) {
      nextSlot.textContent = "";
      nextSlot.appendChild(buildRow(data.modules[0], "div"));
    }
    const list = panel.querySelector("[data-pipeline-queue]");
    if (list) {
      list.textContent = "";
      data.modules.forEach(function (mod) {
        list.appendChild(buildRow(mod, "li"));
      });
    }
    const toggle = panel.querySelector("[data-pipeline-queue-toggle]");
    if (toggle) {
      toggle.hidden = data.modules.length <= 1;
    }
  }

  if (typeof EventSource !== "undefined") {
    const src = new EventSource("/sse");
    src.addEventListener("queue", function (event) {
      let data;
      try {
        data = JSON.parse(event.data);
      } catch (err) {
        return;
      }
      renderQueue(data);
    });
    src.addEventListener("module", function (event) {
      let data;
      try {
        data = JSON.parse(event.data);
      } catch (err) {
        return;
      }
      const migrationId = panel.getAttribute("data-migration-id");
      if (!migrationId || String(data.migration_id) !== migrationId) {
        return;
      }
      const inflight =
        data.status === "queued" ||
        data.status === "running" ||
        data.status === "waiting_for_human";
      if (inflight) {
        setActive(data.id);
      } else {
        setActive(null);
      }
    });
  }
})();
