/* The run log on Pipeline: history on open, then live lines over SSE.

   A line's body is fetched when it is clicked and not before. Prompts and
   model answers carry the customer's source, and this tool gets
   demonstrated on a projector. */
(function () {
  var panel = document.querySelector("[data-run-log-panel]");
  if (!panel) {
    return;
  }
  var list = panel.querySelector("[data-run-log]");
  var empty = panel.querySelector("[data-run-log-empty]");
  var body = panel.querySelector("[data-run-log-body]");
  var bodyPanel = panel.querySelector("[data-run-log-body-panel]");
  var bodyLabel = panel.querySelector("[data-run-log-body-label]");
  var follow = panel.querySelector("[data-run-log-follow]");
  var job = panel.getAttribute("data-job") || "";
  /* The panel shows its own migration's runs. Two migrations run side by
     side, and a panel that followed every job hopped between them. */
  var migration = panel.getAttribute("data-migration-id") || "";
  var last = 0;
  /* Live lines wait here while the history is being read. Appended first,
     a live line moved `last` past the whole history, which was then
     dropped as already shown. */
  var loading = false;
  var held = [];
  /* Clear empties the card, name and all. What that name was is kept here,
     so the first line of the next run puts it back rather than leaving a
     list of lines belonging to nothing. */
  var name = panel.querySelector(".run-log-job");
  var jobId = panel.querySelector("[data-run-log-id]");
  var named = name ? name.textContent : "";

  function forget() {
    if (name) {
      name.textContent = "";
    }
    if (jobId) {
      jobId.hidden = true;
    }
  }

  function remember() {
    if (name && !name.textContent) {
      name.textContent = named;
    }
    if (jobId && jobId.hidden) {
      jobId.hidden = false;
    }
  }

  function atBottom() {
    return list.scrollHeight - list.scrollTop - list.clientHeight < 40;
  }

  function append(line) {
    if (line.id <= last) {
      return;
    }
    last = line.id;
    remember();
    if (empty) {
      empty.hidden = true;
    }
    var item = document.createElement("li");
    item.className = "run-log-line " + (line.level || "info");
    var when = document.createElement("span");
    when.className = "run-log-at mono";
    /* The stored timestamp is UTC and arrives with its offset. Only the
       clock matters when reading a run back, so this shows time of day and
       not a date — in the reader's zone, which slicing the string did not
       do: it showed UTC while the header said "last run". */
    when.textContent = window.localClock(line.at);
    var who = document.createElement("span");
    who.className = "run-log-source mono";
    who.textContent = line.source || "";
    var text = document.createElement("span");
    text.className = "run-log-text";
    text.textContent = line.text || "";
    item.appendChild(when);
    item.appendChild(who);
    item.appendChild(text);
    if (line.has_detail) {
      var open = document.createElement("button");
      open.type = "button";
      open.className = "run-log-open";
      open.textContent = "body";
      open.addEventListener("click", function () {
        /* A second click closes it. Without that the pane stayed open on
           a line that had scrolled away, describing nothing on screen. */
        if (item.classList.contains("is-open")) {
          closeBody();
          return;
        }
        fetch("/api/log/" + line.id)
          .then(function (response) {
            return response.text().then(function (text) {
              return response.ok ? text : "not available (" + response.status + ")";
            });
          })
          .catch(function () {
            return "not available: the app did not answer";
          })
          .then(function (text) {
            var chosen = list.querySelector(".is-open");
            if (chosen) {
              chosen.classList.remove("is-open");
            }
            item.classList.add("is-open");
            bodyPanel.hidden = false;
            bodyLabel.textContent = when.textContent + "  " + who.textContent;
            body.textContent = text;
            bodyPanel.scrollIntoView({ block: "nearest" });
          });
      });
      item.appendChild(open);
    }
    var stick = follow.checked && atBottom();
    list.appendChild(item);
    if (stick) {
      list.scrollTop = list.scrollHeight;
    }
  }

  function showJob(id, module) {
    /* A different job, so the name kept for `remember` is not this one's.
       The name travels on the line. Reading it off the live panel above
       meant reading it before that panel had been told: a job's first line
       lands before the progress that renames it, so the new run wore the
       previous module's name until the first step started. */
    named = module || "";
    if (name) {
      name.textContent = named;
    }
    if (!jobId) {
      return;
    }
    jobId.hidden = false;
    jobId.setAttribute("data-run-log-id", id);
    jobId.textContent = "job " + id.slice(0, 8);
    jobId.title = "Copy this job's id: " + id;
  }

  /* Named for the run that has just ended, because that is the one whose
     lines are leaving the panel. */
  function handoverLine(ended) {
    var item = document.createElement("li");
    item.className = "run-log-switch mono";
    item.textContent =
      (named ? named + " · " : "") +
      "job " +
      ended.slice(0, 8) +
      " ended. A new run starts below.";

    return item;
  }

  function closeBody() {
    var chosen = list.querySelector(".is-open");
    if (chosen) {
      chosen.classList.remove("is-open");
    }
    bodyPanel.hidden = true;
    body.textContent = "";
  }

  /* Copying is why the log exists: a failed run gets pasted into a chat or
     an issue. The clipboard work is in copy.js, which Result shares. */
  function copy(text, button) {
    window.copyText(text, button);
  }

  function asText() {
    var rows = [];
    if (job) {
      rows.push("job " + job);
    }
    var items = list.querySelectorAll(".run-log-line");
    for (var i = 0; i < items.length; i++) {
      var cells = items[i].querySelectorAll("span");
      var parts = [];
      for (var j = 0; j < cells.length; j++) {
        parts.push(cells[j].textContent);
      }
      rows.push(parts.join("  "));
    }
    return rows.join("\n");
  }

  if (jobId) {
    jobId.addEventListener("click", function () {
      copy(jobId.getAttribute("data-run-log-id"), jobId);
    });
  }
  var copyAll = panel.querySelector("[data-run-log-copy]");
  if (copyAll) {
    copyAll.addEventListener("click", function () {
      copy(asText(), copyAll);
    });
  }
  var copyBody = panel.querySelector("[data-run-log-body-copy]");
  if (copyBody) {
    copyBody.addEventListener("click", function () {
      copy(body.textContent, copyBody);
    });
  }
  var closer = panel.querySelector("[data-run-log-body-close]");
  if (closer) {
    closer.addEventListener("click", closeBody);
  }
  var clear = panel.querySelector("[data-run-log-clear]");

  function wipe() {
    fetch("/api/jobs/" + encodeURIComponent(job) + "/log", { method: "DELETE" })
      .then(function (response) {
        if (!response.ok) {
          throw new Error(String(response.status));
        }
        list.replaceChildren();
        last = 0;
        closeBody();
        forget();
        if (empty) {
          empty.textContent = "This log was cleared.";
          empty.hidden = false;
        }
      })
      .catch(function () {
        clear.classList.add("is-failed");
        clear.title = "The log was not cleared: the app refused or did not answer.";
        setTimeout(function () {
          clear.classList.remove("is-failed");
          clear.title = "Delete this job's log";
        }, 2400);
      });
  }

  if (clear) {
    clear.addEventListener("click", function () {
      if (!job) {
        return;
      }
      /* Deleted is deleted: the lines are not kept anywhere else. */
      if (typeof window.askToConfirm === "function") {
        window.askToConfirm(
          {
            title: "Clear this job's log?",
            text: "Every line of this job's log is deleted. The job, its steps and its result stay.",
            go: "Clear",
          },
          wipe
        );
      } else {
        wipe();
      }
    });
  }

  /* The history after `last`, in pages: a long job has more lines than one
     answer carries. */
  var PAGE = 1000;

  function load() {
    if (!job || loading) {
      return;
    }
    loading = true;
    var reading = job;
    fetch("/api/jobs/" + encodeURIComponent(job) + "/log?after=" + last)
      .then(function (response) {
        if (!response.ok) {
          throw new Error(String(response.status));
        }
        return response.json();
      })
      .then(function (data) {
        if (reading !== job) {
          return;
        }
        var lines = data.lines || [];
        lines.forEach(append);
        if (last > 0 && empty) {
          empty.hidden = true;
        }
        if (lines.length >= PAGE) {
          loading = false;
          load();

          return;
        }
        loading = false;
        held.splice(0).forEach(append);
      })
      .catch(function () {
        loading = false;
        held.splice(0).forEach(append);
      });
  }

  load();

  /* sse.js owns the page's one EventSource. Deferred scripts run in
     document order and that one is at the foot of the body, so this may
     have to wait for it. */
  if (window.migratorSse) {
    listen(window.migratorSse);
  } else {
    document.addEventListener("sse-ready", function (ready) {
      listen(ready.detail);
    });
  }

  function listen(src) {
    /* A stream that comes back after a drop missed whatever was written
       meanwhile; the history after `last` has it. */
    var dropped = false;
    src.addEventListener("error", function () {
      dropped = true;
    });
    src.addEventListener("open", function () {
      if (dropped) {
        dropped = false;
        load();
      }
    });
    src.addEventListener("log", function (event) {
      var data;
      try {
        data = JSON.parse(event.data);
      } catch (err) {
        return;
      }
      if (migration && data.migration_id && String(data.migration_id) !== migration) {
        return;
      }
      /* A job that started after this page was rendered is the one to
         follow: the panel was showing the previous run until now. */
      if (job && data.job_id !== job) {
        /* The panel holds one job: Clear, Copy and the body fetch all act
           on that one. Wiping it silently read as the same run starting
           over at `understand` — which is what a queue of three modules
           looks like from here. The previous run is still on record; this
           says so rather than leaving a list that lost its history. */
        var handover = handoverLine(job);
        panel.setAttribute("data-job", data.job_id);
        job = data.job_id;
        last = 0;
        loading = false;
        held = [];
        list.replaceChildren();
        list.appendChild(handover);
        showJob(data.job_id, data.module);
      } else if (!job) {
        panel.setAttribute("data-job", data.job_id);
        job = data.job_id;
        showJob(data.job_id, data.module);
      }
      if (empty) {
        empty.hidden = true;
      }
      if (loading) {
        held.push(data);
      } else {
        append(data);
      }
      /* A line that arrived is a job at work. The panel is folded by default
         when nothing runs, and opens itself for this — unless the reader has
         already chosen. */
      panel.dispatchEvent(new CustomEvent("run-log-live"));
    });
  }
})();
