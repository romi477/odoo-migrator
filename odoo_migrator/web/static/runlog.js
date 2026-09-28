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
  var last = 0;
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
            return response.text();
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

  function showJob(id) {
    /* A different job, so the name kept for `remember` is not this one's.
       The live panel above knows whose run this is; it is updated by the
       same event that brought this line. */
    var live = document.querySelector("[data-pipeline-module]");
    named = live ? live.textContent.trim() : "";
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

  function closeBody() {
    var chosen = list.querySelector(".is-open");
    if (chosen) {
      chosen.classList.remove("is-open");
    }
    bodyPanel.hidden = true;
    body.textContent = "";
  }

  /* Copying is why the log exists: a failed run gets pasted into a chat or
     an issue. `navigator.clipboard` is the good path, and it is refused
     often enough — a permissions policy, an embedded view, a browser that
     wants a stronger user gesture — that a button relying on it alone is a
     button that sometimes silently does nothing. The textarea fallback
     needs no permission. */
  function copy(text, button) {
    function done(ok) {
      button.classList.add(ok ? "is-copied" : "is-failed");
      setTimeout(function () {
        button.classList.remove("is-copied");
        button.classList.remove("is-failed");
      }, 1200);
    }
    function viaTextarea() {
      var pad = document.createElement("textarea");
      pad.value = text;
      pad.setAttribute("readonly", "");
      pad.style.position = "fixed";
      pad.style.opacity = "0";
      document.body.appendChild(pad);
      pad.select();
      var ok = false;
      try {
        ok = document.execCommand("copy");
      } catch (err) {
        ok = false;
      }
      document.body.removeChild(pad);
      done(ok);
    }
    if (!navigator.clipboard) {
      viaTextarea();
      return;
    }
    navigator.clipboard.writeText(text).then(function () {
      done(true);
    }, viaTextarea);
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
  if (clear) {
    clear.addEventListener("click", function () {
      if (!job) {
        return;
      }
      fetch("/api/jobs/" + job + "/log", { method: "DELETE" }).then(function () {
        list.innerHTML = "";
        last = 0;
        closeBody();
        forget();
        if (empty) {
          empty.textContent = "This log was cleared.";
          empty.hidden = false;
        }
      });
    });
  }

  function load() {
    if (!job) {
      return;
    }
    fetch("/api/jobs/" + job + "/log?after=" + last)
      .then(function (response) {
        return response.json();
      })
      .then(function (data) {
        (data.lines || []).forEach(append);
        if (last > 0 && empty) {
          empty.hidden = true;
        }
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
    src.addEventListener("log", function (event) {
      var data;
      try {
        data = JSON.parse(event.data);
      } catch (err) {
        return;
      }
      /* A job that started after this page was rendered is the one to
         follow: the panel was showing the previous run until now. */
      if (job && data.job_id !== job) {
        panel.setAttribute("data-job", data.job_id);
        job = data.job_id;
        last = 0;
        list.innerHTML = "";
        showJob(data.job_id);
      } else if (!job) {
        panel.setAttribute("data-job", data.job_id);
        job = data.job_id;
        showJob(data.job_id);
      }
      if (empty) {
        empty.hidden = true;
      }
      append(data);
    });
  }
})();
