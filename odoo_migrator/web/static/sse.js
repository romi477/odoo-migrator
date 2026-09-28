/* One EventSource per page. Named events patch a row, the lamp and the
   running count. They must not replace the page — a half-typed answer in
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
  const src = new EventSource("/sse");
  /* One stream per page, as the comment above says. Anything else that
     wants events takes this one: a second EventSource is a second
     subscriber and a second thread parked on the server for the page's
     whole life. */
  window.migratorSse = src;
  document.dispatchEvent(new CustomEvent("sse-ready", { detail: src }));

  src.addEventListener("module", function (event) {
    let data;
    try {
      data = JSON.parse(event.data);
    } catch (err) {
      return;
    }
    const migrationId = data.migration_id ? String(data.migration_id) : "";
    const pipe = document.querySelector("[data-pipeline]");
    const observer = document.querySelector("[data-observer]");
    const onThisMigration =
      migrationId &&
      ((pipe && pipe.getAttribute("data-migration-id") === migrationId) ||
        (observer && observer.getAttribute("data-migration-id") === migrationId));
    if (
      onThisMigration &&
      pipe &&
      pipe.getAttribute("data-migration-id") === migrationId
    ) {
      const inflight =
        data.status === "queued" ||
        data.status === "running" ||
        data.status === "waiting_for_human";
      const live = pipe.querySelector(".pipeline-live");
      const empty = pipe.querySelector("[data-pipeline-empty]");
      if (live) {
        live.hidden = !inflight;
      }
      if (empty) {
        empty.hidden = inflight;
      }
      if (inflight) {
        pipe.setAttribute("data-module-id", String(data.id));
        const name = pipe.querySelector("[data-pipeline-module]");
        if (name && data.technical_name) {
          name.textContent = data.technical_name;
        }
        const pipeBadge = pipe.querySelector("[data-pipeline-status]");
        if (pipeBadge && data.status) {
          pipeBadge.className = "badge " + data.status;
          pipeBadge.textContent = data.status;
        }
        const pipeStage = pipe.querySelector("[data-stage]");
        if (pipeStage) {
          const attempt = data.attempt ? " · attempt " + data.attempt : "";
          pipeStage.textContent = (data.stage || "") + attempt;
        }
        /* The states come from the job's own step rows. Deriving them from
           the stage would mean assuming every earlier step passed, which is
           the question the ribbon exists to answer. */
        if (data.steps && data.steps.length) {
          const ribbon = pipe.querySelector("[data-ribbon]");
          if (ribbon) {
            data.steps.forEach(function (step) {
              const node = ribbon.querySelector(
                '[data-step="' + step.id + '"]'
              );
              if (node) {
                node.setAttribute("data-state", step.state);
              }
            });
          }
        }
      }
    }
    if (onThisMigration) {
      const inflight =
        data.status === "queued" ||
        data.status === "running" ||
        data.status === "waiting_for_human";
      const sessions = document.querySelectorAll("[data-session]");
      for (let i = 0; i < sessions.length; i++) {
        if (data.current_session_id) {
          sessions[i].textContent = data.current_session_id;
        } else if (!inflight) {
          sessions[i].textContent = "No live session yet.";
        }
      }
    }

    const row = document.querySelector('[data-module-id="' + data.id + '"]');
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
      if (data.status === "failed") {
        row.classList.add("failed");
      } else {
        row.classList.remove("failed");
      }
      const reopen = row.querySelector("[data-result-reopen]");
      if (reopen) {
        const finished =
          data.status === "passed" ||
          data.status === "failed" ||
          data.status === "unknown";
        reopen.hidden = !finished;
      }
    } else if (data.status === "failed") {
      row.classList.add("failed");
    } else {
      row.classList.remove("failed");
    }
    const stage = row.querySelector("[data-stage]");
    if (stage) {
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
  });

  src.addEventListener("checkpoint", function (event) {
    let data;
    try {
      data = JSON.parse(event.data);
    } catch (err) {
      return;
    }
    const pipe = document.querySelector("[data-pipeline]");
    if (!pipe || pipe.getAttribute("data-migration-id") !== String(data.migration_id)) {
      return;
    }
    const ready = document.querySelector("[data-checkpoint-ready]");
    if (ready) {
      /* A link, not a reload: the page may hold a half-typed answer, and
         the review is built server-side. */
      ready.hidden = false;
    }
  });

  src.addEventListener("lamp", function (event) {
    let data;
    try {
      data = JSON.parse(event.data);
    } catch (err) {
      return;
    }
    const lamp = document.querySelector(".nav-end .attention-lamp");
    if (!lamp || !data.state) {
      return;
    }
    lamp.className = "attention-lamp " + data.state;
    lamp.setAttribute(
      "title",
      data.state === "needs-attention"
        ? "A module is waiting. Open it."
        : "Nothing needs attention."
    );
  });

  src.addEventListener("running", function (event) {
    let data;
    try {
      data = JSON.parse(event.data);
    } catch (err) {
      return;
    }
    const count = document.querySelector(".nav-end .running-count");
    if (!count) {
      return;
    }
    const n = data.count == null ? 0 : data.count;
    count.setAttribute("data-running", String(n));
    const figure = count.querySelector(".count");
    if (figure) {
      figure.textContent = String(n);
    } else {
      count.textContent = String(n);
    }
  });
})();
