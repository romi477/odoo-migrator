/* The lamp of the module in work: a ball bouncing in a ring.

   Drawn in a canvas laid over the lamp, so the row keeps its dot's place: the
   ring is 16px round the dot's centre, and the ball bounces off its wall by the
   law of reflection, with a little chaos in the angle after each bounce so the
   path does not settle into a pattern. Only the running module has one; a
   module parked on a question keeps the still dot, since nothing moves while
   it waits on a person. Someone who asked their system for less motion gets
   the ring with the ball at rest.

   `stepBall` is plain arithmetic with no page in it, so it can be run on its
   own (the tests do, under node). */
(function () {
  "use strict";

  var SIZE = 16;
  var RING = SIZE / 2 - 1;
  var BALL = 1.7;
  var SPEED = 26;
  var LIMIT = RING - BALL;
  var RUNNING = { running: true, queued: true };

  function stepBall(ball, dt, rand) {
    ball.x += ball.vx * dt;
    ball.y += ball.vy * dt;
    var d = Math.hypot(ball.x, ball.y);
    if (d > ball.limit) {
      var nx = ball.x / d;
      var ny = ball.y / d;
      var dot = ball.vx * nx + ball.vy * ny;
      ball.vx -= 2 * dot * nx;
      ball.vy -= 2 * dot * ny;
      /* A little chaos: turn the new direction by up to a quarter radian. */
      var turn = (rand() - 0.5) * 0.5;
      var c = Math.cos(turn);
      var s = Math.sin(turn);
      var vx = ball.vx * c - ball.vy * s;
      ball.vy = ball.vx * s + ball.vy * c;
      ball.vx = vx;
      ball.x = nx * ball.limit;
      ball.y = ny * ball.limit;
    }

    return ball;
  }

  if (typeof module !== "undefined" && module.exports) {
    module.exports = { stepBall: stepBall };
  }
  if (typeof window === "undefined") {
    return;
  }

  var still = !!(window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches);
  var live = new Map();
  var frame = 0;
  var before = 0;

  function paint(entry) {
    var g = entry.g;
    g.clearRect(0, 0, SIZE, SIZE);
    g.strokeStyle = entry.color;
    g.fillStyle = entry.color;
    g.globalAlpha = 0.55;
    g.lineWidth = 1.1;
    g.beginPath();
    g.arc(SIZE / 2, SIZE / 2, RING, 0, Math.PI * 2);
    g.stroke();
    g.globalAlpha = 1;
    g.beginPath();
    g.arc(SIZE / 2 + entry.ball.x, SIZE / 2 + entry.ball.y, BALL, 0, Math.PI * 2);
    g.fill();
  }

  function make(lamp) {
    var canvas = document.createElement("canvas");
    var dpr = Math.min(window.devicePixelRatio || 1, 3);
    canvas.width = SIZE * dpr;
    canvas.height = SIZE * dpr;
    canvas.setAttribute("aria-hidden", "true");
    lamp.classList.add("has-ball");
    lamp.appendChild(canvas);
    var g = canvas.getContext("2d");
    g.scale(dpr, dpr);
    var heading = Math.random() * Math.PI * 2;
    var entry = {
      canvas: canvas,
      g: g,
      color: window.getComputedStyle(lamp).color,
      ball: {
        x: 0,
        y: 0,
        vx: Math.cos(heading) * SPEED,
        vy: Math.sin(heading) * SPEED,
        limit: LIMIT,
      },
    };
    paint(entry);

    return entry;
  }

  function drop(lamp, entry) {
    entry.canvas.remove();
    lamp.classList.remove("has-ball");
    live.delete(lamp);
  }

  function tick(now) {
    frame = 0;
    var dt = Math.min(0.05, (now - before) / 1000);
    before = now;
    live.forEach(function (entry, lamp) {
      if (!lamp.isConnected) {
        live.delete(lamp);

        return;
      }
      stepBall(entry.ball, dt, Math.random);
      paint(entry);
    });
    if (live.size && !still) {
      frame = window.requestAnimationFrame(tick);
    }
  }

  function wanted(row) {
    return (
      row.classList.contains("pipeline-queue-active") &&
      !!RUNNING[row.getAttribute("data-status")]
    );
  }

  /* Gives a ball to the lamp of every row of `root` that is running, and takes
     it off the ones that are not. Called whenever the rows are drawn again or
     a module's status changes. */
  function sync(root) {
    (root || document).querySelectorAll(".pipeline-queue-item").forEach(function (row) {
      var lamp = row.querySelector(".pipeline-lamp");
      if (!lamp) {
        return;
      }
      var has = live.has(lamp);
      if (wanted(row) && !has) {
        live.set(lamp, make(lamp));
      } else if (!wanted(row) && has) {
        drop(lamp, live.get(lamp));
      }
    });
    if (live.size && !still && !frame) {
      before = window.performance.now();
      frame = window.requestAnimationFrame(tick);
    }
  }

  window.lampBall = { sync: sync, stepBall: stepBall };
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", function () {
      sync(document);
    });
  } else {
    sync(document);
  }
})();
