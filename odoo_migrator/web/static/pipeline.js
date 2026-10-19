/* Pipeline queue: compact next row, expandable full list, SSE updates. */
(function () {
  /* The ribbon is painted by the server on load and by the `module` event
     after that, both from the job's own step rows. This file used to mark
     the node whose text matched the stage line, which said only where the
     run was and never whether that step had failed. */

  /* Stop takes effect after the model turn in flight, which can take
     minutes. Saying so at the press, and refusing a second one, is what
     tells the person it was heard. */
  const stopForm = document.querySelector("[data-pipeline-stop]");
  if (stopForm) {
    stopForm.addEventListener("submit", function (event) {
      const button = stopForm.querySelector("button");
      if (!button) {
        return;
      }
      if (button.getAttribute("aria-busy") === "true") {
        event.preventDefault();

        return;
      }
      button.setAttribute("aria-busy", "true");
      button.textContent = "Stopping…";
      button.title = stopForm.getAttribute("data-hint-stopping") || button.title;
    });
  }

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
      (data.idle ? "idle" : "busy") +
      "|" +
      (data.steps ? data.steps.length : 0) +
      "|" +
      data.modules.map(function (mod) {
        return mod.id + ":" + mod.position + (mod.removable ? "x" : "") + (mod.play ? "p" : "");
      }).join(",") +
      "|" +
      (active ? active.id : "")
    );
  }

  function cell(tag, className, text) {
    const node = document.createElement(tag);
    node.className = className;
    if (text != null) {
      node.textContent = String(text);
    }

    return node;
  }

  /* The last card takes itself back out of the queue. The same button as
     the page draws (_queue_remove.html), made from nodes. */
  function removeButton(mod) {
    const button = cell("button", "queue-remove", null);
    const label = "Remove " + mod.technical_name + " from the queue";
    button.type = "button";
    button.setAttribute("data-queue-remove", "");
    button.setAttribute("data-module", String(mod.id));
    button.title = label;
    button.setAttribute("aria-label", label);
    const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    svg.setAttribute("class", "card-glyph");
    svg.setAttribute("viewBox", "0 0 16 16");
    svg.setAttribute("width", "12");
    svg.setAttribute("height", "12");
    svg.setAttribute("fill", "none");
    svg.setAttribute("stroke", "currentColor");
    svg.setAttribute("stroke-width", "1.6");
    svg.setAttribute("stroke-linecap", "round");
    svg.setAttribute("aria-hidden", "true");
    const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
    path.setAttribute("d", "M4 4l8 8M12 4l-8 8");
    svg.appendChild(path);
    button.appendChild(svg);

    return button;
  }

  /* The play button: this module alone, where the queue's Run goes on through
     all of them. The same button as _queue_play.html, made from nodes. */
  function playButton(mod) {
    const button = cell("button", "queue-play", null);
    button.type = "button";
    button.setAttribute("data-queue-play", "");
    button.setAttribute("data-module", String(mod.id));
    button.disabled = !!mod.play;
    button.title =
      mod.play || "Run " + mod.technical_name + " only: the rest of the queue waits";
    button.setAttribute("aria-label", "Run " + mod.technical_name + " only");
    const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    svg.setAttribute("class", "play-glyph");
    svg.setAttribute("viewBox", "0 0 24 24");
    svg.setAttribute("width", "18");
    svg.setAttribute("height", "18");
    svg.setAttribute("fill", "none");
    svg.setAttribute("stroke", "currentColor");
    svg.setAttribute("stroke-width", "1.6");
    svg.setAttribute("stroke-linejoin", "round");
    svg.setAttribute("aria-hidden", "true");
    const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
    path.setAttribute("d", "M10.41 6.10 Q7.00 4.00 7.00 8.00 L7.00 16.00 Q7.00 20.00 10.41 17.90 L16.59 14.10 Q20.00 12.00 16.59 9.90 Z");
    svg.appendChild(path);
    button.appendChild(svg);

    return button;
  }

  /* Built from nodes, as every other row on the page is. The name is data
     from the project on disk, and went in through innerHTML. */
  function buildRow(mod, tag) {
    const row = document.createElement(tag);
    row.className = "pipeline-queue-item" + (mod.active ? " pipeline-queue-active" : "");
    row.setAttribute("data-module-id", String(mod.id));
    row.setAttribute("data-status", mod.status || "");
    row.appendChild(cell("span", "pipeline-queue-order", mod.position));
    if (mod.migration_rank != null && mod.step != null) {
      row.appendChild(cell("span", "graph-node-index", "#" + mod.migration_rank + "/" + mod.step));
    }
    /* The last card can be taken back out; every card can be run on its own. */
    if (mod.removable) {
      row.appendChild(removeButton(mod));
    }
    row.appendChild(playButton(mod));
    row.appendChild(cell("span", "name mono", mod.technical_name));
    row.appendChild(cell("span", "badge", "step " + mod.step));

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
    /* The step chips read this: which steps are queued, and which of those can be taken out. */
    window.dispatchEvent(new CustomEvent("queue-changed", { detail: data.steps || [] }));
    panel.setAttribute("data-running", data.run_state === "running" ? "true" : "false");
    /* The queue's edge and the cables into and out of it are colourless while
       it is empty. */
    panel.setAttribute("data-has-items", data.modules.length ? "true" : "false");
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
    const queueStop = panel.querySelector('form[action="/run/queue/stop"]');
    const clearBtn = panel.querySelector('form[action="/run/queue/clear"] button');
    if (runBtn) {
      runBtn.disabled = data.run_state === "running" || !data.modules.length;
    }
    if (queueStop) {
      queueStop.hidden = data.run_state !== "running";
    }
    if (clearBtn) {
      const pending = (data.steps && data.steps.length) || data.modules.length;
      clearBtn.disabled = !data.idle || !pending;
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
    document.querySelectorAll("form.runner-bar-form").forEach(function (form) {
      form.addEventListener("submit", function (event) {
        const submitter = event.submitter;
        const action =
          (submitter && submitter.getAttribute("formaction")) || form.action;
        if (
          !action ||
          (action.indexOf("/run/queue/add") === -1 &&
            action.indexOf("/run/queue/remove-step") === -1)
        ) {
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
            /* A refusal from the guard or a crash is not JSON. */
            return response
              .json()
              .catch(function () {
                return { error: "The app answered " + response.status + "." };
              })
              .then(function (payload) {
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

  /* One listener on the panel: rows are rebuilt on every change. */
  panel.addEventListener("click", function (event) {
    const play = event.target.closest("[data-queue-play]");
    const button = play || event.target.closest("[data-queue-remove]");
    if (!button || button.disabled) {
      return;
    }
    button.disabled = true;
    showStartError("");
    const data = new FormData();
    data.append("module", button.getAttribute("data-module"));
    fetch(play ? "/run/queue/run-module" : "/run/queue/remove", {
      method: "POST",
      headers: { Accept: "application/json" },
      body: data,
    })
      .then(function (response) {
        return response
          .json()
          .catch(function () {
            return { error: "The app answered " + response.status + "." };
          })
          .then(function (payload) {
            return { ok: response.ok, payload: payload };
          });
      })
      .then(function (result) {
        if (!result.ok) {
          button.disabled = false;
          showStartError(result.payload.error || "Could not update the queue.");

          return;
        }
        renderQueue(result.payload);
      })
      .catch(function () {
        button.disabled = false;
        showStartError("Could not update the queue.");
      });
  });

  /* sse.js owns the page's one EventSource; a second one here was a second
     connection and a second server thread for as long as the page was open. */
  function listen(src) {
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
      /* The active row is the one running or parked. A module reopened or
         marked for review is not, and used to take the mark off the one
         that was. */
      panel.querySelectorAll('[data-module-id="' + data.id + '"]').forEach(function (row) {
        row.setAttribute("data-status", data.status || "");
      });
      if (data.status === "running" || data.status === "waiting_for_human") {
        setActive(data.id);
      } else if (panel.querySelector('.pipeline-queue-active[data-module-id="' + data.id + '"]')) {
        setActive(null);
      }
      });
  }

  if (window.migratorSse) {
    listen(window.migratorSse);
  } else {
    document.addEventListener("sse-ready", function (ready) {
      listen(ready.detail);
    });
  }
})();
