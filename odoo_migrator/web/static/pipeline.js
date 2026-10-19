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

  /* The module whose square was pressed. Rows are rebuilt on every queue
     event, and the one being stopped stays off until its run ends. */
  let stoppingId = panel.getAttribute("data-stopping-module") || "";

  /* Continue closes the review and starts the queue. A second press of it, a
     double click, has nothing left to answer: it is held off. Discard asks
     first and is left to the dialog. */
  panel.addEventListener("submit", function (event) {
    const go = event.target.closest('[data-queue-review] form[action="/run/continue"]');
    if (!go) {
      return;
    }
    const button = go.querySelector("button");
    if (!button || button.getAttribute("aria-busy") === "true") {
      event.preventDefault();

      return;
    }
    button.setAttribute("aria-busy", "true");
    setTimeout(function () {
      button.disabled = true;
    }, 0);
  });

  panel.addEventListener("submit", function (event) {
    const form = event.target.closest("[data-queue-stop]");
    if (!form) {
      return;
    }
    const button = form.querySelector("button");
    if (!button || button.getAttribute("aria-busy") === "true") {
      event.preventDefault();

      return;
    }
    stoppingId = form.getAttribute("data-module") || "";
    button.setAttribute("aria-busy", "true");
    button.title = HINT_STOPPING;
    /* After this turn: the submission has already taken the form's data. */
    setTimeout(function () {
      button.disabled = true;
    }, 0);
  });

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
        return (
          mod.id +
          ":" +
          mod.position +
          (mod.removable ? "x" : "") +
          (mod.play ? "p" : "") +
          (mod.review ? "r" + JSON.stringify(mod.review) : "")
        );
      }).join(",") +
      "|" +
      (active ? active.id + ":" + active.status : "")
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

  const HINT_STOPPING =
    "Stop was heard. The model turn in flight finishes first, then the run ends.";
  const IN_WORK = { running: true, waiting_for_human: true };

  /* The square on the module in work: stop this run now. The same form as
     _queue_play.html, made from nodes. */
  function stopSquare(mod) {
    const form = cell("form", "queue-stop", null);
    form.method = "post";
    form.action = "/run/modules/" + mod.id + "/stop";
    form.setAttribute("data-queue-stop", "");
    form.setAttribute("data-module", String(mod.id));
    const button = cell("button", "queue-play", null);
    button.type = "submit";
    const stopping = stoppingId === String(mod.id);
    if (stopping) {
      button.disabled = true;
      button.setAttribute("aria-busy", "true");
      button.title = HINT_STOPPING;
    } else if (mod.status === "waiting_for_human") {
      button.title =
        "Stop " + mod.technical_name + " now: the question is withdrawn and the run ends.";
    } else {
      button.title =
        "Stop " +
        mod.technical_name +
        " now: the model turn in flight finishes, then the run ends.";
    }
    button.setAttribute("aria-label", "Stop " + mod.technical_name + " now");
    const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    svg.setAttribute("class", "play-glyph stop-glyph");
    svg.setAttribute("viewBox", "0 0 24 24");
    svg.setAttribute("width", "18");
    svg.setAttribute("height", "18");
    svg.setAttribute("fill", "none");
    svg.setAttribute("stroke", "currentColor");
    svg.setAttribute("stroke-width", "1.6");
    svg.setAttribute("stroke-linejoin", "round");
    svg.setAttribute("aria-hidden", "true");
    const rect = document.createElementNS("http://www.w3.org/2000/svg", "rect");
    rect.setAttribute("x", "7");
    rect.setAttribute("y", "7");
    rect.setAttribute("width", "10");
    rect.setAttribute("height", "10");
    rect.setAttribute("rx", "2.5");
    svg.appendChild(rect);
    button.appendChild(svg);
    form.appendChild(button);

    return form;
  }

  /* Its run is in the review a finished batch waits on: Continue, which runs
     the queue again, and Discard run, which asks first. The same pair as _queue_play.html, made from
     nodes; confirm.js listens on the document, so a form built here asks too. */
  function reviewButtons(mod) {
    const box = cell("div", "queue-review", null);
    box.setAttribute("data-queue-review", "");
    const go = cell("form", "", null);
    go.method = "post";
    go.action = "/run/continue";
    const again = cell("input", "", null);
    again.type = "hidden";
    again.name = "run";
    again.value = "1";
    go.appendChild(again);
    const goButton = cell("button", "", "Continue");
    goButton.type = "submit";
    goButton.disabled = !!mod.review.unread;
    goButton.setAttribute("aria-label", "Continue " + mod.technical_name);
    goButton.title = mod.review.unread
      ? "A failed or waiting run is unread: dismiss it in the review below, or override there."
      : "Keep the files this run left and run the queue again. The module starts over at its first step, on those files.";
    go.appendChild(goButton);
    box.appendChild(go);
    const discard = mod.review.discard;
    if (!discard) {
      return box;
    }
    const label = "Discard the run of " + mod.technical_name;
    if (discard.files == null) {
      const off = cell("button", "discard-run", "Discard");
      off.type = "button";
      off.disabled = true;
      off.setAttribute("aria-label", label);
      off.title =
        "Its directory in the target project is not a git checkout, so its files cannot be put back.";
      box.appendChild(off);

      return box;
    }
    const form = cell("form", "", null);
    form.method = "post";
    form.action = "/run/modules/" + mod.id + "/discard-run";
    const files = discard.files
      ? " " +
        discard.files +
        " changed file" +
        (discard.files === 1 ? "" : "s") +
        " in its directory in the target project " +
        (discard.files === 1 ? "is" : "are") +
        " put back to the last commit, and files git does not know are removed."
      : " Its directory in the target project has no changes.";
    form.setAttribute("data-confirm-title", label + "?");
    form.setAttribute(
      "data-confirm-text",
      "Every run of this module is deleted with its log, findings and thread, and the module is queued again." +
        files +
        " The target container's database is not touched."
    );
    form.setAttribute("data-confirm-go", "Discard run");
    const button = cell("button", "discard-run", "Discard");
    button.type = "submit";
    button.setAttribute("aria-label", label);
    button.title = "Delete this run and queue the module again.";
    form.appendChild(button);
    box.appendChild(form);

    return box;
  }

  /* The play button: this module alone, where the queue's Run goes on through
     all of them. The same button as _queue_play.html, made from nodes. On the
     module in work, the square that stops it. */
  function playButton(mod) {
    if (mod.active && IN_WORK[mod.status]) {
      return stopSquare(mod);
    }
    if (mod.review) {
      return reviewButtons(mod);
    }
    if (stoppingId === String(mod.id)) {
      stoppingId = "";
    }
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

  /* Rows are rebuilt on each queue event, and a keyboard user's focus went
     with the node it was on. Where it was is kept as the module and the part
     of the row, and put back on the new one; a button that became another
     (a play that turned into a stop) hands it to the row's first live button. */
  function focusedPart() {
    const node = document.activeElement;
    if (!node || !panel.contains(node)) {
      return null;
    }
    const where = node.closest("[data-pipeline-queue-next]")
      ? "[data-pipeline-queue-next] "
      : "[data-pipeline-queue] ";
    const row = node.closest("[data-module-id]");
    if (!row || !node.closest("button")) {
      return null;
    }
    const id = row.getAttribute("data-module-id");
    let part = "button";
    if (node.closest("[data-queue-play]")) {
      part = "[data-queue-play]";
    } else if (node.closest("[data-queue-remove]")) {
      part = "[data-queue-remove]";
    } else if (node.closest("[data-queue-stop]")) {
      part = "[data-queue-stop] button";
    } else if (node.closest(".discard-run")) {
      part = ".discard-run";
    } else if (node.closest('form[action="/run/continue"]')) {
      part = 'form[action="/run/continue"] button';
    }

    return { where: where + '[data-module-id="' + id + '"] ', part: part };
  }

  function restoreFocus(kept) {
    if (!kept || (document.activeElement && panel.contains(document.activeElement))) {
      return;
    }
    const target =
      panel.querySelector(kept.where + kept.part + ":not(:disabled)") ||
      panel.querySelector(kept.where + "button:not(:disabled)");
    if (target) {
      target.focus();
    }
  }

  function renderQueue(data) {
    const migrationId = panel.getAttribute("data-migration-id");
    if (!migrationId || String(data.migration_id) !== migrationId) {
      return;
    }
    /* The hello a page without a migration of its own gets carries only the
       bar's count; it has no cards to draw. */
    if (!Array.isArray(data.modules)) {
      return;
    }
    const key = queueKey(data);
    if (key === lastQueueKey) {
      return;
    }
    lastQueueKey = key;
    /* A card that answers the review makes the reload notice moot, whichever
       of the two events came first. */
    if (data.modules.some(function (mod) { return mod.review; })) {
      const ready = document.querySelector("[data-checkpoint-ready]");
      if (ready) {
        ready.hidden = true;
      }
    }
    /* "Migrated, nothing to review" is about the run that ended; the next
       one takes it down. */
    if (!data.idle) {
      const done = document.querySelector('[data-checkpoint-ready][data-kind="passed"]');
      if (done) {
        done.hidden = true;
      }
    }
    /* The step chips read this: which steps are queued, and which of those can be taken out. */
    window.dispatchEvent(new CustomEvent("queue-changed", { detail: data.steps || [] }));
    panel.setAttribute("data-running", data.run_state === "running" ? "true" : "false");
    /* Work in hand: the queue is running, or a module is in flight (one run on
       its own, from a play button). The cables carry their pulse for either. */
    panel.setAttribute("data-working", data.idle ? "false" : "true");
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
      runBtn.disabled = data.run_state === "running" || !data.modules.length || !!data.review;
      runBtn.title = data.review
        ? "The last batch waits for review: Continue, or Discard its run, on the module's card."
        : "";
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
    const kept = focusedPart();
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
    restoreFocus(kept);
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
