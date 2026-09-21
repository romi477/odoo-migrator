/* One EventSource per page. Named events patch a row, the lamp and the
   running count. They must not replace the page — a half-typed answer in
   an Alpine field has to survive a background update. */
(function () {
  if (typeof EventSource === "undefined") {
    return;
  }
  const src = new EventSource("/sse");

  src.addEventListener("module", function (event) {
    let data;
    try {
      data = JSON.parse(event.data);
    } catch (err) {
      return;
    }
    const pipe = document.querySelector("[data-pipeline]");
    if (
      pipe &&
      data.migration_id &&
      pipe.getAttribute("data-migration-id") === String(data.migration_id)
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
      }
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
    count.textContent = String(n);
  });
})();
