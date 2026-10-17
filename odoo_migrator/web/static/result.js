/* One module's last run, opened from its card.

   The checkpoint thread used to sit at the top of this screen as one flat
   list for the whole migration: a paragraph about one module above thirty
   cards, with nothing saying which module it was about or when it was
   written. */
(function () {
  const panel = document.querySelector("[data-result-summary-panel]");
  if (!panel) {
    return;
  }
  const title = panel.querySelector("[data-result-summary-title]");
  const body = panel.querySelector("[data-result-summary-body]");
  const close = panel.querySelector("[data-result-summary-close]");
  const copyButton = panel.querySelector("[data-result-summary-copy]");

  function line(text, className) {
    const node = document.createElement("p");
    node.className = className || "";
    node.textContent = text;

    return node;
  }

  function count(value) {
    return Number(value).toLocaleString("en-US");
  }

  function render(data) {
    body.textContent = "";
    body.appendChild(
      line(
        data.status +
          " · attempt " +
          data.attempt +
          (data.finished_at ? " · " + window.localStamp(data.finished_at) : "") +
          " · job " +
          data.short,
        "result-summary-line mono"
      )
    );
    /* What the model cost, summed over the job. Cache reads are most of a
       long step's input and are billed differently, so they are named
       apart from "in". A module that never called the model has none. */
    const spent = data.tokens;
    if (spent && (spent.in || spent.out)) {
      body.appendChild(
        line(
          "tokens · " +
            count(spent.in) +
            " in · " +
            count(spent.out) +
            " out" +
            (spent.cached ? " · " + count(spent.cached) + " cached" : ""),
          "result-summary-line mono"
        )
      );
    }
    if (data.steps && data.steps.length) {
      const steps = document.createElement("ol");
      steps.className = "result-summary-steps";
      data.steps.forEach(function (step) {
        const item = document.createElement("li");
        item.className = step.ok === false ? "is-failed" : "";
        item.textContent =
          step.attempt +
          " · " +
          step.id +
          (step.tokens
            ? " · " + count(step.tokens.in) + "+" + count(step.tokens.out) + " tokens"
            : "") +
          (step.output ? " — " + step.output : "");
        steps.appendChild(item);
      });
      body.appendChild(steps);
    }
    /* What happened to the tests. A step list that says `write_test` says a
       test was written and nothing about whether it ran — and the pipeline's
       own `tests` step runs before that one, so for a module that shipped
       none the only run there is is the one inside `write_test`. */
    const testing = document.createElement("section");
    testing.className = "result-summary-tests";
    const head = document.createElement("h3");
    head.textContent = "Tests";
    testing.appendChild(head);
    if (data.testing && data.testing.length) {
      const rows = document.createElement("ol");
      data.testing.forEach(function (row) {
        const item = document.createElement("li");
        item.className = row.ok === false ? "is-failed" : "";
        item.textContent =
          row.step + " · " + row.what + " — " + row.detail;
        rows.appendChild(item);
      });
      testing.appendChild(rows);
    } else {
      testing.appendChild(
        line("Nothing was listed, written or run.", "result-summary-line")
      );
    }
    body.appendChild(testing);

    /* Where the run actually happened. The daemon keeps its own journal per
       session, and finding the id meant grepping the run log for the line
       that printed it. A side listed twice was reopened, which is ordinary:
       a rewrite invalidates the session on the target container. */
    const shells = document.createElement("section");
    shells.className = "result-summary-sessions";
    const shellHead = document.createElement("h3");
    shellHead.textContent = "Sheller sessions";
    shells.appendChild(shellHead);
    if (data.sessions && data.sessions.length) {
      const rows = document.createElement("ol");
      data.sessions.forEach(function (row) {
        const item = document.createElement("li");
        item.className = "mono";
        item.textContent =
          row.side +
          " · " +
          row.session_id +
          (row.steps.length ? " — " + row.steps.join(", ") : "") +
          " · " +
          row.calls +
          (row.calls === 1 ? " command" : " commands");
        rows.appendChild(item);
      });
      shells.appendChild(rows);
    } else {
      shells.appendChild(
        line("No shell was opened on this run.", "result-summary-line")
      );
    }
    body.appendChild(shells);

    /* The agent's own words, and the human's reply. A <pre> because the
       diagnosis is written with line breaks that carry its structure. */
    (data.messages || []).forEach(function (message) {
      const wrap = document.createElement("article");
      wrap.className = "result-summary-message " + message.author;
      wrap.appendChild(
        line(
          message.author +
            (message.at ? " · " + window.localStamp(message.at) : ""),
          "mono"
        )
      );
      const text = document.createElement("pre");
      text.textContent = message.text;
      wrap.appendChild(text);
      body.appendChild(wrap);
    });
    if (!(data.messages && data.messages.length) && !(data.steps && data.steps.length)) {
      body.appendChild(line("This run recorded no steps and said nothing."));
    }
  }

  /* The whole window as text, for pasting into a chat or an issue. Read off
     the rendered blocks in the order they sit, so it says what the panel
     says: the title, then each heading, line, list item and message. */
  function asText() {
    const rows = [title.textContent];
    body.querySelectorAll("h3, p, li, pre").forEach(function (node) {
      const text = node.textContent.trim();
      if (!text) {
        return;
      }
      if (node.tagName === "H3") {
        rows.push("", text);
      } else if (node.tagName === "LI") {
        rows.push("- " + text);
      } else {
        rows.push(text);
      }
    });

    return rows.join("\n");
  }

  /* The panel opens under the whole board, and the card that was pressed
     may be a screen above it. Bring it to the top of the view, smoothly
     unless the reader asked for less movement. It is done twice — on open,
     and again once the summary is drawn: at first the panel is one line
     tall, and the page has no room to scroll it to the top until it grows. */
  function reveal() {
    panel.scrollIntoView({
      block: "start",
      behavior: window.matchMedia("(prefers-reduced-motion: reduce)").matches
        ? "auto"
        : "smooth",
    });
  }

  /* The newest press wins. Two quick presses answered in either order,
     and the slower answer drew one module's run under the other's name. */
  let asked = 0;

  document.addEventListener("click", function (event) {
    const button = event.target.closest("[data-result-summary]");
    if (!button) {
      return;
    }
    const id = button.getAttribute("data-result-summary");
    const mine = ++asked;
    title.textContent = button.getAttribute("data-result-summary-name") || "";
    panel.hidden = false;
    reveal();
    body.textContent = "loading…";
    fetch("/api/result/modules/" + encodeURIComponent(id) + "/summary")
      .then(function (response) {
        return response.ok ? response.json() : null;
      })
      .catch(function () {
        return { failed: true };
      })
      .then(function (data) {
        if (mine !== asked) {
          return;
        }
        if (data && data.failed) {
          body.textContent = "not available: the app did not answer";
        } else if (data) {
          render(data);
        } else {
          body.textContent = "not available";
        }
        reveal();
      });
  });

  if (copyButton) {
    copyButton.addEventListener("click", function () {
      window.copyText(asText(), copyButton);
    });
  }

  if (close) {
    close.addEventListener("click", function () {
      panel.hidden = true;
    });
  }
})();

/* The counters over the board, kept to what the modules are now. The cards
   followed the run and the figures above them stayed as the page loaded. */
(function () {
  const board = document.querySelector("[data-result]");
  const progress = document.querySelector("[data-result-progress]");
  if (!board || !progress) {
    return;
  }
  const migration = board.getAttribute("data-migration-id");
  let queued = null;

  function counter(selector, value) {
    const node = progress.querySelector(selector);
    if (!node) {
      return;
    }
    node.hidden = !value;
    const figure = node.querySelector("[data-figure]");
    if (figure) {
      figure.textContent = String(value || 0);
    }
  }

  function refresh() {
    queued = null;
    fetch("/api/result/progress")
      .then(function (response) {
        return response.ok ? response.json() : null;
      })
      .catch(function () {
        return null;
      })
      .then(function (data) {
        if (!data || String(data.migration_id) !== migration) {
          return;
        }
        const done = progress.querySelector("[data-progress-done]");
        const bad = progress.querySelector("[data-progress-bad]");
        if (done) {
          done.style.setProperty("--x", data.pct_done);
        }
        if (bad) {
          bad.style.setProperty("--x", data.pct_bad);
        }
        const count = progress.querySelector("[data-progress-count]");
        if (count) {
          count.textContent = data.done + " of " + data.total + " modules";
        }
        counter("[data-progress-migrated]", data.migrated);
        counter("[data-progress-waiting]", data.waiting);
        counter("[data-progress-failed]", data.failed);
      });
  }

  function listen(src) {
    src.addEventListener("module", function (event) {
      let data;
      try {
        data = JSON.parse(event.data);
      } catch (err) {
        return;
      }
      if (String(data.migration_id) !== migration || queued) {
        return;
      }
      /* A burst of events is one request. */
      queued = setTimeout(refresh, 400);
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
