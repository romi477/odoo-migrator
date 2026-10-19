/* The ribbon on Runner after load, and the attempt picker above it.

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
  /* The last event the card did not take while an older attempt was read. */
  let held = null;

  function moved() {
    return pipe.querySelector("[data-attempt-moved]");
  }

  /* `data-reading` is what sse.js looks at before it swaps the card. */
  function reading(on) {
    pipe.toggleAttribute("data-reading", on);
    if (!on) {
      held = null;
      const note = moved();
      if (note) {
        note.hidden = true;
      }
    }
  }

  function follow() {
    const event = held;
    reading(false);
    viewing = 0;
    if (event) {
      pipe.dispatchEvent(new CustomEvent("card-follow", { detail: event }));
    } else if (live) {
      paint(live);
      markPicker(live.attempt || 1, live.attempt || 1);
    }
  }

  function picker() {
    return pipe.querySelector("[data-attempt-picker]");
  }

  function boxTitle(step) {
    if (step.state === "earlier") {
      return step.id + ": done in attempt " + step.attempt + ", not run again";
    }
    if (step.state === "skipped") {
      return step.id + ": not run. It runs when a step before it raises " + step.when + ", and none did";
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

  /* Steps with no model end in milliseconds: lint, upgrade, tests, verdict
     came done in one event and the ribbon jumped to its end. A live update
     that moves several steps at once lights them one after the other, a beat
     apart, in the order they ran. The states are the run's; only the drawing
     takes its time, under two seconds for the whole stock flow. A later
     update takes over from one still being drawn. */
  const BEAT = 260;
  const ARRIVED = { done: true, failed: true, skipped: true, earlier: true };
  const still = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  let drawing = 0;
  /* How long the last live paint takes to draw, in ms; sse.js waits it out
     before it folds a card whose run has just ended. */
  let drawn = 0;

  function paint(view, live) {
    const ribbon = pipe.querySelector("[data-ribbon]");
    drawing += 1;
    const turn = drawing;
    const paced = !!live && !still;
    let wait = 0;
    if (ribbon && view.steps) {
      view.steps.forEach(function (step) {
        const node = ribbon.querySelector('[data-step="' + CSS.escape(step.id) + '"]');
        if (!node) {
          return;
        }
        const box = node.querySelector(".ribbon-box");
        if (box) {
          box.title = boxTitle(step);
        }
        if (node.getAttribute("data-state") === step.state) {
          return;
        }
        const show = function () {
          if (turn !== drawing) {
            return;
          }
          node.setAttribute("data-state", step.state);
          if (paced && ARRIVED[step.state]) {
            node.classList.remove("is-arriving");
            void node.offsetWidth;
            node.classList.add("is-arriving");
          }
        };
        if (paced && wait) {
          setTimeout(show, wait);
        } else {
          show();
        }
        if (paced) {
          wait += BEAT;
        }
      });
    }
    drawn = wait;
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
      paint(data, true);
      data.drawFor = drawn;
    }
    markPicker(total, viewing || total);
  });

  pipe.addEventListener("card-held", function (event) {
    held = event.detail;
    const note = moved();
    const text = note && note.querySelector("[data-attempt-moved-text]");
    if (!note || !text) {
      return;
    }
    const other = String(held.id) !== (pipe.getAttribute("data-module-id") || "");
    text.textContent = other
      ? (held.technical_name || "Another module") + " is running."
      : "This run has ended.";
    note.hidden = false;
  });

  pipe.addEventListener("click", function (event) {
    if (event.target.closest("[data-attempt-follow]")) {
      follow();

      return;
    }
    const button = event.target.closest("[data-attempt]");
    const nav = picker();
    if (!button || !nav || !nav.contains(button)) {
      return;
    }
    const wanted = Number(button.getAttribute("data-attempt"));
    const total = Number(nav.getAttribute("data-attempts")) || wanted;
    const job = nav.getAttribute("data-job");
    if (wanted === total && live) {
      follow();

      return;
    }
    fetch("/api/jobs/" + encodeURIComponent(job) + "/attempts/" + wanted)
      .then(function (response) {
        return response.ok ? response.json() : null;
      })
      .catch(function () {
        return null;
      })
      .then(function (view) {
        if (!view) {
          return;
        }
        viewing = wanted === view.attempts ? 0 : wanted;
        reading(viewing !== 0);
        paint(view);
        markPicker(view.attempts, wanted);
      });
  });
})();
