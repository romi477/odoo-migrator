/* One EventSource per page. Named events patch a row, the lamp and the
   queue count. They must not replace the page — a half-typed answer in
   an Alpine field has to survive a background update. */
function fillResultCornerGlyph(glyph, kind) {
  glyph.className = "";
  glyph.removeAttribute("class");
  if (kind === "done") {
    glyph.className = "result-corner-glyph-check";
    glyph.textContent = "✓";
  } else if (kind === "waiting") {
    glyph.className = "result-corner-glyph-pause";
    glyph.textContent = "";
  } else if (kind === "excluded") {
    glyph.className = "result-corner-glyph-excluded";
    glyph.textContent = "";
  }
}

(function () {
  if (typeof EventSource === "undefined") {
    return;
  }
  const INFLIGHT = { queued: true, running: true, waiting_for_human: true };
  /* A run that is going or parked: what the pipeline card is for. `queued`
     is not one — Reopen sends it for a module nobody is running. */
  const LIVE = { running: true, waiting_for_human: true };

  /* The open migration goes with the request, so the hello says what it
     looks like now. The stream drops every few minutes and on every
     restart, and whatever was published meanwhile is gone. */
  const opened = document.querySelector("[data-open-migration]");
  const migration = opened ? opened.getAttribute("data-open-migration") : "";
  const src = new EventSource(
    "/sse" + (migration ? "?migration=" + encodeURIComponent(migration) : "")
  );
  /* One stream per page, as the comment above says. Anything else that
     wants events takes this one: a second EventSource is a second
     subscriber and a second thread parked on the server for the page's
     whole life. */
  window.migratorSse = src;
  document.dispatchEvent(new CustomEvent("sse-ready", { detail: src }));

  function parse(event) {
    try {
      return JSON.parse(event.data);
    } catch (err) {
      return null;
    }
  }

  function pipeFor(migrationId) {
    const pipe = document.querySelector("[data-pipeline]");
    if (!pipe || !migrationId || pipe.getAttribute("data-migration-id") !== migrationId) {
      return null;
    }

    return pipe;
  }

  /* The machine: gears turn while a module is in it, rest when it waits for a
     person, and a finished one leaves at the output (passed) or stays at the
     input (anything else). A page that reconnects hears "idle" and must not
     wipe the result of the run it just watched end. */
  const PASSED = { passed: true, migrated: true };

  function setMachine(pipe, state, name) {
    const machine = pipe.querySelector(".machine");
    if (!machine) {
      return;
    }
    const keep = { done: true, failed: true };
    if (state === "idle" && keep[machine.getAttribute("data-machine")]) {
      return;
    }
    const goes = {
      run: ["in", ""],
      stuck: ["in", ""],
      failed: ["in", ""],
      done: ["", "out"],
      idle: ["", ""],
    }[state];
    machine.setAttribute("data-machine", state);
    /* Only a machine at work throws sparks, and paintCard says when. */
    if (state !== "run") {
      machine.setAttribute("data-lane", "");
    }
    ["in", "out"].forEach(function (side) {
      const slot = machine.querySelector('[data-machine-slot="' + side + '"]');
      const label = slot && slot.querySelector("[data-machine-name]");
      if (!slot || !label) {
        return;
      }
      const holds = goes.indexOf(side) !== -1 && !!name;
      slot.classList.toggle("is-full", holds);
      label.textContent = holds ? name : "no module";
    });
  }

  /* Nothing is running: the card folds to the machine at rest. A page showing
     hand edits keeps them; that list is the way out of a refused start. */
  function idleCard(pipe, data) {
    const live = pipe.querySelector(".pipeline-live");
    const edits = pipe.querySelector("[data-hand-edits]");
    if (live) {
      live.hidden = true;
    }
    if (data && data.status) {
      setMachine(
        pipe,
        PASSED[data.status] ? "done" : "failed",
        data.technical_name || ""
      );
    } else {
      setMachine(pipe, "idle", "");
    }
    /* Stop and Run share a place on the queue card, outside this card. */
    const stop = document.querySelector("[data-pipeline-stop]");
    if (stop) {
      stop.hidden = true;
    }
    const run = document.querySelector('form[action="/run/queue/run"]');
    if (run) {
      run.hidden = false;
    }
    const ask = pipe.querySelector("[data-pipeline-ask]");
    if (ask) {
      ask.hidden = true;
    }
  }

  /* The card follows the module it shows, and takes a new one only when
     that one is running or parked. Any module of the migration used to
     repaint it: a Reopen on Result swapped a running card for a queued
     module and pointed Stop at it, and the review mark on a dependent
     (`passed`) folded the card while its run went on. */
  function paintCard(pipe, data) {
    const shown = pipe.getAttribute("data-module-id") || "";
    const same = shown === String(data.id);
    if (!same && !LIVE[data.status]) {
      return;
    }
    /* Someone is reading an older attempt. Another module taking the card, or
       this one folding it, would swap the page under them: it was read as the
       run starting over. attempts.js says so and holds the event until Follow. */
    if (pipe.hasAttribute("data-reading") && (!same || !INFLIGHT[data.status])) {
      pipe.dispatchEvent(new CustomEvent("card-held", { detail: data }));

      return;
    }
    if (!INFLIGHT[data.status]) {
      idleCard(pipe, data);

      return;
    }
    const live = pipe.querySelector(".pipeline-live");
    if (live) {
      live.hidden = false;
    }
    setMachine(
      pipe,
      data.status === "waiting_for_human" ? "stuck" : "run",
      data.technical_name || ""
    );
    /* The failure lane is at work while one of its steps is running: sparks
       until the lap is mended and goes on. */
    const machine = pipe.querySelector(".machine");
    if (machine && data.status !== "waiting_for_human") {
      const failing = (data.lane || []).some(function (step) {
        return step.state === "running";
      });
      machine.setAttribute("data-lane", failing ? "failure" : "");
    }
    if (!same) {
      pipe.setAttribute("data-module-id", String(data.id));
      pipe.removeAttribute("data-stopping");
    }
    const pipeStage = pipe.querySelector("[data-stage]");
    if (pipeStage) {
      const attempt = data.attempt ? " · attempt " + data.attempt : "";
      pipeStage.textContent = (data.stage || "") + attempt;
    }
    /* The states come from the job's own step rows. Deriving them from
       the stage would mean assuming every earlier step passed, which is
       the question the ribbon exists to answer. attempts.js paints them,
       and leaves them alone while someone is reading an older attempt. */
    if (data.steps && data.steps.length) {
      pipe.dispatchEvent(new CustomEvent("ribbon-live", { detail: data }));
    }
    const parked = data.status === "waiting_for_human";
    const stop = document.querySelector("[data-pipeline-stop]");
    const run = document.querySelector('form[action="/run/queue/run"]');
    if (run) {
      run.hidden = !!LIVE[data.status];
    }
    if (stop) {
      /* A module in the queue is stopped from its own card; this one is for
         a run with no card there, started from Tree. */
      const carded = document.querySelector(
        '.pipeline-queue-item[data-module-id="' + data.id + '"]'
      );
      stop.hidden = !LIVE[data.status] || !!carded;
      stop.setAttribute("action", "/run/modules/" + data.id + "/stop");
      const button = stop.querySelector("button");
      if (button) {
        button.title =
          stop.getAttribute(parked ? "data-hint-parked" : "data-hint-running") || "";
        /* A Stop asked for on another module is not this one's. */
        if (!same) {
          button.disabled = false;
          button.removeAttribute("aria-busy");
          button.textContent = button.getAttribute("data-label") || button.textContent;
        }
      }
    }
    const ask = pipe.querySelector("[data-pipeline-ask]");
    if (ask) {
      const wasHidden = ask.hidden;
      ask.hidden = !parked;
      if (parked) {
        ask.setAttribute("action", "/run/modules/" + data.id + "/answer");
        const question = ask.querySelector(".question");
        if (question && data.question) {
          question.textContent = data.question;
        }
        const label = "Answer " + (data.technical_name || "");
        const box = ask.querySelector("textarea");
        if (box) {
          box.setAttribute("aria-label", label.trim());
          /* A new question, not a repaint of the one being answered. */
          if (wasHidden) {
            box.value = "";
          }
        }
        const submit = ask.querySelector("button[type=submit]");
        if (submit) {
          submit.setAttribute("aria-label", label.trim());
        }
      }
    }
  }

  const readingPipe = document.querySelector("[data-pipeline]");
  if (readingPipe) {
    readingPipe.addEventListener("card-follow", function (event) {
      paintCard(readingPipe, event.detail);
    });
  }

  /* One module's rows elsewhere on the page: the queue lamps, a Result
     card, the Tree node card. The pipeline card is not one of them; it
     carries `data-module-id` too, and is painted above. */
  function paintRows(data) {
    if (data.status) {
      document
        .querySelectorAll('.pipeline-queue-item[data-module-id="' + data.id + '"]')
        .forEach(function (item) {
          item.setAttribute("data-status", data.status);
        });
    }
    const row = document.querySelector(
      '[data-module-id="' + data.id + '"]:not([data-pipeline]):not(.pipeline-queue-item)'
    );
    if (!row) {
      return;
    }
    const badge = row.querySelector(".badge");
    if (badge && data.status) {
      badge.className = "badge " + data.status;
      badge.textContent = data.status;
    }
    if (row.classList.contains("result-card")) {
      let corner = row.querySelector(".result-corner-badge");
      let kind = "";
      if (data.status === "passed") {
        kind = "done";
      } else if (data.status === "waiting_for_human") {
        kind = "waiting";
      }
      if (kind) {
        if (!corner) {
          corner = document.createElement("span");
          corner.className = "result-corner-badge " + kind;
          const glyph = document.createElement("span");
          glyph.setAttribute("aria-hidden", "true");
          fillResultCornerGlyph(glyph, kind);
          corner.appendChild(glyph);
          row.insertBefore(corner, row.firstChild);
        } else {
          corner.className = "result-corner-badge " + kind;
          let glyph = corner.querySelector("[aria-hidden]");
          if (!glyph) {
            glyph = document.createElement("span");
            glyph.setAttribute("aria-hidden", "true");
            corner.appendChild(glyph);
          }
          fillResultCornerGlyph(glyph, kind);
        }
        row.setAttribute("data-result-corner", kind);
      } else if (corner && row.getAttribute("data-result-corner") !== "excluded") {
        corner.remove();
        row.removeAttribute("data-result-corner");
      }
      /* Sent with the module events that touch it; one that does not say
         leaves the mark as it is. */
      const review = row.querySelector("[data-result-review]");
      if (review && typeof data.needs_review === "boolean") {
        review.hidden = !data.needs_review;
      }
      const reopen = row.querySelector("[data-result-reopen]");
      if (reopen) {
        const finished =
          data.status === "passed" ||
          data.status === "failed" ||
          data.status === "unknown" ||
          data.status === "migrated" ||
          data.status === "stopped";
        reopen.hidden = !finished;
      }
    }
    if (data.status === "failed") {
      row.classList.add("failed");
    } else {
      row.classList.remove("failed");
    }
    const stage = row.querySelector("[data-stage]");
    if (stage && "stage" in data) {
      const attempt = data.attempt ? " · attempt " + data.attempt : "";
      stage.textContent = (data.stage || "") + attempt;
    }
    const ask = row.querySelector(".ask");
    if (ask && data.status) {
      if (data.status === "waiting_for_human") {
        ask.hidden = false;
        const question = ask.querySelector(".question");
        if (question && data.question) {
          question.textContent = data.question;
        }
      } else {
        ask.hidden = true;
      }
    }
  }

  src.addEventListener("module", function (event) {
    const data = parse(event);
    if (!data) {
      return;
    }
    const migrationId = data.migration_id ? String(data.migration_id) : "";
    const pipe = pipeFor(migrationId);
    if (pipe) {
      paintCard(pipe, data);
    }
    const observer = document.querySelector("[data-observer]");
    const onThisMigration =
      !!pipe ||
      (observer && migrationId && observer.getAttribute("data-migration-id") === migrationId);
    if (onThisMigration && (!pipe || pipe.getAttribute("data-module-id") === String(data.id))) {
      const sessions = document.querySelectorAll("[data-session]");
      for (let i = 0; i < sessions.length; i++) {
        if (data.current_session_id) {
          sessions[i].textContent = data.current_session_id;
        } else if (!INFLIGHT[data.status]) {
          sessions[i].textContent = "No live session yet.";
        }
      }
    }
    paintRows(data);
  });

  /* The hello's word that nothing of this migration is in flight. */
  src.addEventListener("pipeline_idle", function (event) {
    const data = parse(event);
    const pipe = data && pipeFor(String(data.migration_id));
    if (pipe) {
      idleCard(pipe);
    }
  });

  /* The hello's statuses, for the rows of a page that missed events while
     its stream was down. */
  src.addEventListener("module_states", function (event) {
    const data = parse(event);
    if (!data || !data.modules) {
      return;
    }
    data.modules.forEach(function (row) {
      if (row.struck_out) {
        return;
      }
      paintRows({
        id: row.id,
        status: row.status,
        question: row.question,
        needs_review: row.needs_review,
        migration_id: data.migration_id,
      });
    });
  });

  src.addEventListener("checkpoint", function (event) {
    const data = parse(event);
    if (!data || !pipeFor(String(data.migration_id))) {
      return;
    }
    const ready = document.querySelector("[data-checkpoint-ready]");
    /* A queue card that carries the review is answered there, and the queue
       event before this one drew it: no notice, and nothing to reload. */
    if (ready && !document.querySelector("[data-queue-review]")) {
      /* A link, not a reload: the page may hold a half-typed answer, and
         the review is built server-side. */
      ready.hidden = false;
    }
  });

  src.addEventListener("lamp", function (event) {
    const data = parse(event);
    const lamp = document.querySelector(".nav-end .attention-lamp");
    if (!lamp || !data || !data.state) {
      return;
    }
    lamp.className = "attention-lamp " + data.state;
    lamp.setAttribute(
      "title",
      data.state === "needs-attention"
        ? "A module is waiting for you or has failed. Open it."
        : data.state === "running"
          ? "A migration is running."
          : "Nothing needs attention."
    );
  });

  /* The bar's counter is the open migration's queue. Every queue event
     carries it, so it moves with the queue panel and needs no event of its own. */
  src.addEventListener("queue", function (event) {
    const data = parse(event);
    const count = document.querySelector(".nav-end .queue-count");
    if (!count || !data || !data.count) {
      return;
    }
    if (String(data.migration_id) !== count.getAttribute("data-migration-id")) {
      return;
    }
    count.setAttribute("data-queued", String(data.count.queued));
    count.setAttribute("title", data.count.title);
    count.querySelector(".count").textContent = data.count.text;
    count.querySelector(".unit").textContent = data.count.unit;
  });
})();
