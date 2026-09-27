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
  var follow = panel.querySelector("[data-run-log-follow]");
  var job = panel.getAttribute("data-job") || "";
  var last = 0;

  function atBottom() {
    return list.scrollHeight - list.scrollTop - list.clientHeight < 40;
  }

  function append(line) {
    if (line.id <= last) {
      return;
    }
    last = line.id;
    var item = document.createElement("li");
    item.className = "run-log-line " + (line.level || "info");
    var when = document.createElement("span");
    when.className = "run-log-at mono";
    /* The stored timestamp is UTC. Only the clock matters when reading a
       run back, so this shows time of day and not a date. */
    when.textContent = String(line.at).slice(11, 19);
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
        fetch("/api/log/" + line.id)
          .then(function (response) {
            return response.text();
          })
          .then(function (text) {
            /* Which line this body belongs to, and put it where the eye
               already is: the panel scrolls, so an unlabelled block at the
               foot of it reads as part of whatever is on screen. */
            var chosen = list.querySelector(".is-open");
            if (chosen) {
              chosen.classList.remove("is-open");
            }
            item.classList.add("is-open");
            body.hidden = false;
            body.textContent = when.textContent + "  " + who.textContent + "\n\n" + text;
            body.scrollIntoView({ block: "nearest" });
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
      } else if (!job) {
        panel.setAttribute("data-job", data.job_id);
        job = data.job_id;
      }
      if (empty) {
        empty.hidden = true;
      }
      append(data);
    });
  }
})();
