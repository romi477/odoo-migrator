/* The ribbon on Pipeline after load, and the attempt picker above it.

   A retry loops back into the pipeline part way, so one attempt is not the
   whole run. The picker reads any attempt back; while it shows an older
   one, live updates do not repaint the ribbon under the reader. Picking
   the latest attempt follows the run again. */
(function () {
  const pipe = document.querySelector("[data-pipeline]");
  if (!pipe) {
    return;
  }
  /* 0 follows the live attempt; a number holds that attempt on screen. */
  let viewing = 0;
  let live = null;

  function picker() {
    return pipe.querySelector("[data-attempt-picker]");
  }

  function boxTitle(step) {
    if (step.state === "earlier") {
      return step.id + ": done in attempt " + step.attempt + ", not run again";
    }

    return "Read what " + step.id + " does, on Flow";
  }

  function laneNode(step) {
    const item = document.createElement("li");
    item.className = "ribbon-node";
    item.setAttribute("data-state", step.state);
    const box = document.createElement("a");
    box.className = "ribbon-box";
    box.href = "/flow#" + encodeURIComponent(step.id);
    box.title = boxTitle(step);
    const led = document.createElement("span");
    led.className = "ribbon-led";
    led.setAttribute("aria-hidden", "true");
    const name = document.createElement("span");
    name.className = "ribbon-name";
    name.textContent = step.id;
    box.appendChild(led);
    box.appendChild(name);
    item.appendChild(box);

    return item;
  }

  function paint(view) {
    const ribbon = pipe.querySelector("[data-ribbon]");
    if (ribbon && view.steps) {
      view.steps.forEach(function (step) {
        const node = ribbon.querySelector('[data-step="' + CSS.escape(step.id) + '"]');
        if (!node) {
          return;
        }
        node.setAttribute("data-state", step.state);
        const box = node.querySelector(".ribbon-box");
        if (box) {
          box.title = boxTitle(step);
        }
      });
    }
    const lane = pipe.querySelector("[data-ribbon-lane]");
    if (lane) {
      lane.querySelectorAll(".ribbon-node").forEach(function (node) {
        node.remove();
      });
      (view.lane || []).forEach(function (step) {
        lane.appendChild(laneNode(step));
      });
      lane.hidden = !(view.lane && view.lane.length);
    }
  }

  function markPicker(total, shown) {
    const nav = picker();
    if (!nav) {
      return;
    }
    const buttons = nav.querySelectorAll("[data-attempt]");
    if (buttons.length !== total) {
      buttons.forEach(function (button) {
        button.remove();
      });
      for (let n = 1; n <= total; n++) {
        const button = document.createElement("button");
        button.type = "button";
        button.className = "attempt-pick";
        button.setAttribute("data-attempt", String(n));
        button.title = "Show attempt " + n;
        button.textContent = String(n);
        nav.appendChild(button);
      }
    }
    nav.querySelectorAll("[data-attempt]").forEach(function (button) {
      const on = Number(button.getAttribute("data-attempt")) === shown;
      button.classList.toggle("is-active", on);
      if (on) {
        button.setAttribute("aria-current", "true");
      } else {
        button.removeAttribute("aria-current");
      }
    });
    nav.setAttribute("data-attempts", String(total));
    nav.hidden = total < 2;
  }

  pipe.addEventListener("ribbon-live", function (event) {
    const data = event.detail;
    const nav = picker();
    /* A new job starts on its own first attempt, whatever was held. */
    if (nav && data.job_id && nav.getAttribute("data-job") !== data.job_id) {
      nav.setAttribute("data-job", data.job_id);
      viewing = 0;
    }
    live = data;
    const total = data.attempt || 1;
    if (!viewing || viewing === total) {
      viewing = 0;
      paint(data);
    }
    markPicker(total, viewing || total);
  });

  pipe.addEventListener("click", function (event) {
    const button = event.target.closest("[data-attempt]");
    const nav = picker();
    if (!button || !nav || !nav.contains(button)) {
      return;
    }
    const wanted = Number(button.getAttribute("data-attempt"));
    const total = Number(nav.getAttribute("data-attempts")) || wanted;
    const job = nav.getAttribute("data-job");
    if (wanted === total && live) {
      viewing = 0;
      paint(live);
      markPicker(total, total);

      return;
    }
    fetch("/api/jobs/" + encodeURIComponent(job) + "/attempts/" + wanted)
      .then(function (response) {
        return response.ok ? response.json() : null;
      })
      .then(function (view) {
        if (!view) {
          return;
        }
        viewing = wanted === view.attempts ? 0 : wanted;
        paint(view);
        markPicker(view.attempts, wanted);
      });
  });
})();
