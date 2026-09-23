/* Pipeline queue: compact next row, expandable full list, SSE updates. */
(function () {
  const panel = document.querySelector("[data-pipeline-queue-panel]");
  if (!panel) {
    return;
  }

  let lastQueueKey = "";

  function queueKey(data) {
    const active = data.modules.find(function (mod) {
      return mod.active;
    });

    return (
      String(data.migration_id) +
      "|" +
      data.run_state +
      "|" +
      (data.steps ? data.steps.length : 0) +
      "|" +
      data.modules.map(function (mod) {
        return mod.id + ":" + mod.position;
      }).join(",") +
      "|" +
      (active ? active.id : "")
    );
  }

  function queueItemMarkup(mod) {
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

  function ensureQueueScaffold() {
    let body = panel.querySelector(".pipeline-queue-body");
    if (!body) {
      body = document.createElement("div");
      body.className = "pipeline-queue-body";
      const empty = panel.querySelector("[data-pipeline-queue-empty]");
      if (empty) {
        panel.insertBefore(body, empty);
      } else {
        panel.appendChild(body);
      }
    }
    if (!body.querySelector("[data-pipeline-queue-next]")) {
      const next = document.createElement("div");
      next.className = "pipeline-queue-next";
      next.setAttribute("data-pipeline-queue-next", "");
      body.appendChild(next);
    }
    if (!body.querySelector("[data-pipeline-queue]")) {
      const list = document.createElement("ol");
      list.className = "pipeline-queue-list";
      list.setAttribute("data-pipeline-queue", "");
      body.appendChild(list);
    }

    return body;
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
    const key = queueKey(data);
    if (key === lastQueueKey) {
      return;
    }
    lastQueueKey = key;
    panel.setAttribute("data-running", data.run_state === "running" ? "true" : "false");
    panel.setAttribute(
      "data-queue-expandable",
      data.modules.length > 1 ? "true" : "false",
    );
    const head = panel.querySelector("[data-pipeline-queue-head]");
    if (head) {
      head.classList.toggle("is-expandable", data.modules.length > 1);
      head.setAttribute(
        "tabindex",
        data.modules.length > 1 ? "0" : "-1",
      );
    }
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
    if (!data.modules.length) {
      const body = panel.querySelector(".pipeline-queue-body");
      if (body) {
        body.hidden = true;
      }
      if (empty) {
        empty.hidden = false;
      }

      return;
    }
    ensureQueueScaffold();
    const body = panel.querySelector(".pipeline-queue-body");
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
  }

  function showStartError(message) {
    const errorEl = document.getElementById("pipeline-start-error");
    if (!errorEl) {
      return;
    }
    if (message) {
      errorEl.textContent = message;
      errorEl.hidden = false;
    } else {
      errorEl.textContent = "";
      errorEl.hidden = true;
    }
  }

  function wireQueueAddForms() {
    document.querySelectorAll("form.pipeline-start").forEach(function (form) {
      form.addEventListener("submit", function (event) {
        const submitter = event.submitter;
        const action =
          (submitter && submitter.getAttribute("formaction")) || form.action;
        if (!action || action.indexOf("/run/queue/add") === -1) {
          return;
        }
        event.preventDefault();
        if (form.dataset.queueSubmitting === "true") {
          return;
        }
        form.dataset.queueSubmitting = "true";
        showStartError("");
        const data = new FormData(form);
        if (action.indexOf("add-all") !== -1) {
          data.delete("step");
        }
        fetch(action, {
          method: "POST",
          headers: { Accept: "application/json" },
          body: data,
        })
          .then(function (response) {
            return response.json().then(function (payload) {
              return { ok: response.ok, payload: payload };
            });
          })
          .then(function (result) {
            if (!result.ok) {
              showStartError(result.payload.error || "Could not update the queue.");

              return;
            }
            renderQueue(result.payload);
          })
          .catch(function () {
            showStartError("Could not update the queue.");
          })
          .finally(function () {
            delete form.dataset.queueSubmitting;
          });
      });
    });
  }

  wireQueueAddForms();

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
