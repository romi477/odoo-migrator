/* The Engine starts the way a car does.

   A run that starts with nothing in hand is a cold start: a spark runs down
   the cable from the queue into the Engine card, the plate fills amber from
   left to right with a stutter at first, like a starter turning over, a
   spark runs along the input belt into the gears, and only then do the gears
   turn, picking up speed. Between two modules of a running queue the engine is
   warm and there is none of it: the gears slow while one module leaves and
   pick up again for the next. When the work ends the gears run down to a stop.

   Only the drawing waits: the run started on the server at the press, and
   the card fills from its events as it always does. The machine's own
   `data-machine` says whether it runs; this file listens to it.

   The gears are turned from here, not by a CSS animation, so that they can
   speed up and run down from wherever they stand. A machine this file drives
   carries `data-driven`; one it does not (no script, reduced motion) keeps
   the CSS animation. */
(function () {
  if (window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches) {
    return;
  }
  /* The sequence's length, the sum of its parts in style.css: the cable
     (0.45s), the plate (1.5s after it) and the belt (0.45s after that). */
  const CRANK = 2400;
  /* Full speed: the large gear turns once in 6s, the small one, eight teeth
     against twelve, once in 4s the other way. */
  const BIG = 60;
  const SMALL = -90;
  /* Seconds from rest to full speed, and from full speed to rest. */
  const SPIN_UP = 0.8;
  const RUN_DOWN = 1.4;
  /* Run and Continue reload the page; the press is remembered across it. */
  const PRESSED = "engine-pressed";
  const FRESH = 8000;

  function pressedJustNow() {
    try {
      const at = Number(sessionStorage.getItem(PRESSED) || 0);
      sessionStorage.removeItem(PRESSED);

      return at > 0 && Date.now() - at < FRESH;
    } catch (err) {
      return false;
    }
  }

  function rememberPress() {
    try {
      sessionStorage.setItem(PRESSED, String(Date.now()));
    } catch (err) {
      /* Without storage the reload shows a warm engine: nothing worse. */
    }
  }

  document.addEventListener("submit", function (event) {
    const form = event.target;
    const action = form && form.getAttribute && form.getAttribute("action");
    if (action === "/run/queue/run" || action === "/run/continue") {
      rememberPress();
    }
  });

  function drive(panel, options) {
    const machine = panel.querySelector(".machine");
    if (!machine) {
      return null;
    }
    machine.setAttribute("data-driven", "");
    const queue = options.queue || null;
    let warm = !!options.warm;
    let speed = 0;
    let target = 0;
    let big = 0;
    let small = 0;
    let last = 0;
    let frame = 0;
    let crank = 0;
    let caught = 0;

    function draw() {
      machine.style.setProperty("--gear-big", big.toFixed(2) + "deg");
      machine.style.setProperty("--gear-small", small.toFixed(2) + "deg");
    }

    function tick(now) {
      const step = last ? Math.min((now - last) / 1000, 0.1) : 0;
      last = now;
      if (speed < target) {
        speed = Math.min(target, speed + step / SPIN_UP);
      } else if (speed > target) {
        speed = Math.max(target, speed - step / RUN_DOWN);
      }
      big = (big + BIG * speed * step) % 360;
      small = (small + SMALL * speed * step) % 360;
      draw();
      if (speed === 0 && target === 0) {
        frame = 0;
        last = 0;

        return;
      }
      frame = requestAnimationFrame(tick);
    }

    function turn(to) {
      target = to;
      if (!frame && (speed !== 0 || target !== 0)) {
        last = 0;
        frame = requestAnimationFrame(tick);
      }
    }

    function on() {
      clearTimeout(crank);
      panel.setAttribute("data-engine", "on");
      warm = true;
      turn(machine.getAttribute("data-machine") === "run" ? 1 : 0);
    }

    function ignite() {
      clearTimeout(crank);
      clearTimeout(caught);
      /* Off first, so that a second cold start plays the CSS from its start. */
      panel.setAttribute("data-engine", "off");
      void panel.offsetWidth;
      panel.setAttribute("data-engine", "crank");
      warm = true;
      crank = setTimeout(function () {
        on();
        panel.classList.add("is-caught");
        caught = setTimeout(function () {
          panel.classList.remove("is-caught");
        }, 700);
      }, CRANK);
    }

    function follow() {
      const running = machine.getAttribute("data-machine") === "run";
      if (!running) {
        /* A start that ends before it has caught: no point finishing it. */
        if (panel.getAttribute("data-engine") === "crank") {
          on();
        }
        turn(0);

        return;
      }
      if (panel.getAttribute("data-engine") === "crank") {
        return;
      }
      if (warm) {
        on();

        return;
      }
      ignite();
    }

    new MutationObserver(follow).observe(machine, {
      attributes: true,
      attributeFilter: ["data-machine"],
    });
    /* Nothing in hand: the engine is cold, and the next run starts it. */
    if (queue) {
      new MutationObserver(function () {
        if (queue.getAttribute("data-working") === "false") {
          warm = false;
        }
      }).observe(queue, { attributes: true, attributeFilter: ["data-working"] });
    }

    draw();
    if (machine.getAttribute("data-machine") === "run") {
      if (warm) {
        speed = 1;
        on();
      } else {
        ignite();
      }
    } else {
      panel.setAttribute("data-engine", "off");
    }

    return {
      cold: function () {
        warm = false;
      },
    };
  }

  /* Runner: the Engine card. A page that loads on a run in progress shows a
     warm engine, unless it is the page Run or Continue just reloaded. */
  const pipe = document.querySelector(".workspace-panel[data-pipeline]");
  if (pipe) {
    const queue = document.querySelector("[data-pipeline-queue-panel]");
    const pressed = pressedJustNow();
    const working = queue && queue.getAttribute("data-working") === "true";
    const running = !!pipe.querySelector('.machine[data-machine="run"]');
    drive(pipe, { queue: queue, warm: !pressed && (running || working) });
  }

  /* /components: a machine with a Start button, to watch the start again. */
  document.querySelectorAll("[data-engine-demo]").forEach(function (demo) {
    const panel = demo.querySelector(".workspace-panel");
    const button = demo.querySelector("[data-engine-demo-toggle]");
    const machine = panel && panel.querySelector(".machine");
    const engine = panel && drive(panel, { warm: false });
    if (!engine || !button || !machine) {
      return;
    }
    button.addEventListener("click", function () {
      const running = machine.getAttribute("data-machine") === "run";
      if (!running) {
        engine.cold();
      }
      machine.setAttribute("data-machine", running ? "idle" : "run");
      button.textContent = running ? "Start" : "Stop";
    });
  });
})();
