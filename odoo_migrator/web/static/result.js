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

  function line(text, className) {
    const node = document.createElement("p");
    node.className = className || "";
    node.textContent = text;

    return node;
  }

  function render(data) {
    body.textContent = "";
    body.appendChild(
      line(
        data.status +
          " · attempt " +
          data.attempt +
          (data.finished_at ? " · " + data.finished_at : "") +
          " · job " +
          data.short,
        "result-summary-line mono"
      )
    );
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

    /* The agent's own words, and the human's reply. A <pre> because the
       diagnosis is written with line breaks that carry its structure. */
    (data.messages || []).forEach(function (message) {
      const wrap = document.createElement("article");
      wrap.className = "result-summary-message " + message.author;
      wrap.appendChild(
        line(message.author + (message.at ? " · " + message.at : ""), "mono")
      );
      const text = document.createElement("pre");
      text.textContent = message.text;
      wrap.appendChild(text);
      body.appendChild(wrap);
    });
    if (!data.messages.length && !(data.steps && data.steps.length)) {
      body.appendChild(line("This run recorded no steps and said nothing."));
    }
  }

  document.addEventListener("click", function (event) {
    const button = event.target.closest("[data-result-summary]");
    if (!button) {
      return;
    }
    const id = button.getAttribute("data-result-summary");
    title.textContent = button.getAttribute("data-result-summary-name") || "";
    panel.hidden = false;
    body.textContent = "loading…";
    fetch("/api/result/modules/" + id + "/summary")
      .then(function (response) {
        return response.ok ? response.json() : null;
      })
      .then(function (data) {
        if (data) {
          render(data);
        } else {
          body.textContent = "not available";
        }
      });
  });

  if (close) {
    close.addEventListener("click", function () {
      panel.hidden = true;
    });
  }
})();
