/* The two wires of the run's card: out of the machine's input slot, down and
   round into the first step, and out of the last step, along and round up into
   the output slot. Elbows with a rounded corner and a small head, the way the
   header's hook leads from the migration into Tree.

   Drawn from measured boxes, not laid out by the stylesheet: the number of
   steps is the cartridge's, and the ribbon wraps onto a second line when they
   do not fit. The wires follow the first and the last step wherever they land. */
(function () {
  const SVG = "http://www.w3.org/2000/svg";
  const RADIUS = 9;
  const HEAD = 5;
  /* How far in from the slot's outer edge each wire leaves it: the ribbon's
     side padding is wider, so the corner always has room. */
  const INSET = 12;

  function wire(panel) {
    let svg = panel.querySelector(":scope > .runner-wires");
    if (!svg) {
      svg = document.createElementNS(SVG, "svg");
      svg.setAttribute("class", "runner-wires");
      svg.setAttribute("aria-hidden", "true");
      panel.appendChild(svg);
    }

    return svg;
  }

  function el(name, attrs) {
    const node = document.createElementNS(SVG, name);
    Object.keys(attrs).forEach(function (key) {
      node.setAttribute(key, attrs[key]);
    });

    return node;
  }

  /* The steps on one line. A flow with more steps than the card is wide
     wrapped one or two of them onto a line of their own, under the rest. The
     ribbon is scaled down to fit, as a whole, to no less than SMALLEST. Past
     that it keeps that size and snakes in columns: the first top to bottom,
     the next bottom to top, and so on. Two columns meet in the same row, so
     the step from one into the next is a plain arrow along the row and the
     flow never jumps back. Measured at full size each time, so a wider card,
     or a flow with fewer steps, gets its full size back. */
  const SMALLEST = 0.78;

  function unsnake(ribbon, nodes) {
    ribbon.classList.remove("is-snake");
    ribbon.style.removeProperty("--snake-columns");
    nodes.forEach(function (node) {
      node.style.removeProperty("--snake-row");
      node.style.removeProperty("--snake-column");
      node.removeAttribute("data-snake");
      node.removeAttribute("data-snake-turn");
    });
  }

  function snake(ribbon, nodes, rows) {
    const columns = Math.ceil(nodes.length / rows);
    ribbon.classList.add("is-snake");
    ribbon.style.setProperty("--snake-columns", String(columns));
    nodes.forEach(function (node, index) {
      const column = Math.floor(index / rows);
      const place = index % rows;
      const up = column % 2 === 1;
      node.style.setProperty("--snake-row", String(up ? rows - place : place + 1));
      node.style.setProperty("--snake-column", String(column + 1));
      node.setAttribute("data-snake", up ? "up" : "down");
      node.toggleAttribute("data-snake-turn", column > 0 && place === 0);
    });
  }

  function fit(ribbon) {
    const nodes = Array.prototype.slice.call(ribbon.querySelectorAll(":scope > .ribbon-node"));
    ribbon.style.setProperty("--fit", "1");
    unsnake(ribbon, nodes);
    if (!nodes.length) {
      return;
    }
    const style = getComputedStyle(ribbon);
    const room = ribbon.clientWidth - parseFloat(style.paddingLeft) - parseFloat(style.paddingRight);
    let natural = 0;
    nodes.forEach(function (node) {
      natural += node.getBoundingClientRect().width;
    });
    if (room <= 0 || natural <= room) {
      return;
    }
    const scale = Math.floor((room / natural) * 100) / 100;
    if (scale >= SMALLEST) {
      ribbon.style.setProperty("--fit", String(scale));

      return;
    }
    ribbon.style.setProperty("--fit", String(SMALLEST));
    /* The grid's columns are as wide as their widest step, so a guess from
       the total can come out too wide: one more row, so one column fewer,
       until it fits. */
    for (let rows = Math.max(2, Math.ceil((natural * SMALLEST) / room)); rows <= nodes.length; rows++) {
      snake(ribbon, nodes, rows);
      if (ribbon.scrollWidth <= ribbon.clientWidth + 1) {
        break;
      }
    }
  }

  /* The failure lane forks from the step that failed. It used to be a row of
     its own, centred under the pipeline, and `diagnose` stood mid-card with
     nothing to say which step it was looking at. The row now starts under the
     step that failed, as far as it fits, and a wire runs down from that step
     into the lane's first. In a snake the step below would be in the way, so
     the wire leaves from the step's side, in the gap between columns. */
  function fork(svg, ribbon, box) {
    const lane = ribbon.parentElement && ribbon.parentElement.querySelector(".ribbon-lane");
    if (!lane) {
      return;
    }
    const failed = ribbon.querySelectorAll(':scope > .ribbon-node[data-state="failed"] .ribbon-box');
    const into = lane.querySelector(".ribbon-node .ribbon-box");
    lane.style.setProperty("--lane-start", "0px");
    if (lane.hidden || !failed.length || !into) {
      lane.removeAttribute("data-forked");

      return;
    }
    lane.setAttribute("data-forked", "");
    const from = box(failed[failed.length - 1]);
    const snaked = ribbon.classList.contains("is-snake");
    const x0 = snaked ? from.left - 5 : (from.left + from.right) / 2;
    /* As far right as the step that failed, and no further than the lane's
       own row has room for. */
    const nodes = lane.querySelectorAll(".ribbon-node");
    const room = lane.getBoundingClientRect().right - nodes[nodes.length - 1].getBoundingClientRect().right;
    const first = box(into);
    const shift = Math.max(0, Math.min(x0 - (first.left + first.right) / 2, room));
    lane.style.setProperty("--lane-start", shift + "px");
    const to = box(into);
    const x1 = (to.left + to.right) / 2;
    const y0 = snaked ? from.middle : from.bottom + 1;
    const bend = (box(ribbon).bottom + to.top) / 2;
    const end = to.top - 1;
    let d = "M" + (snaked ? from.left - 1 : x0) + " " + y0;
    if (snaked) {
      d += " H" + x0;
    }
    if (Math.abs(x1 - x0) < 1) {
      d += " V" + (end - HEAD);
    } else {
      const r = Math.min(RADIUS / 2, Math.abs(x1 - x0) / 2, (end - bend) / 2);
      const way = x1 > x0 ? 1 : -1;
      d +=
        " V" + (bend - r) +
        " Q" + x0 + " " + bend + " " + (x0 + way * r) + " " + bend +
        " H" + (x1 - way * r) +
        " Q" + x1 + " " + bend + " " + x1 + " " + (bend + r) +
        " V" + (end - HEAD);
    }
    svg.appendChild(el("path", { class: "runner-wire runner-wire-fork", d: d }));
    svg.appendChild(
      el("path", {
        class: "runner-wire-head runner-wire-fork",
        d:
          "M" + (x1 - 4) + " " + (end - HEAD) +
          " L" + x1 + " " + end +
          " L" + (x1 + 4) + " " + (end - HEAD) + " Z",
      })
    );
  }

  function draw(panel) {
    const svg = wire(panel);
    svg.textContent = "";
    const live = panel.querySelector(".pipeline-live");
    const inSlot = panel.querySelector('[data-machine-slot="in"]');
    const outSlot = panel.querySelector('[data-machine-slot="out"]');
    const ribbon = panel.querySelector(".ribbon:not(.ribbon-lane)");
    if (!live || live.hidden || !inSlot || !outSlot || !ribbon) {
      return;
    }
    fit(ribbon);
    const boxes = ribbon.querySelectorAll(".ribbon-box");
    if (!boxes.length) {
      return;
    }
    const origin = panel.getBoundingClientRect();
    const box = function (node) {
      const rect = node.getBoundingClientRect();

      return {
        left: rect.left - origin.left,
        right: rect.right - origin.left,
        top: rect.top - origin.top,
        bottom: rect.bottom - origin.top,
        middle: rect.top - origin.top + rect.height / 2,
      };
    };
    /* Each step's place along the ribbon, for the arrows that turn from the
       input's colour to the output's. */
    boxes.forEach(function (node, index) {
      const step = node.closest(".ribbon-node");
      if (step) {
        step.style.setProperty("--along", boxes.length > 1 ? String(index / (boxes.length - 1)) : "0");
      }
    });
    fork(svg, ribbon, box);
    const from = box(inSlot);
    const to = box(outSlot);
    const first = box(boxes[0]);
    const last = box(boxes[boxes.length - 1]);
    /* A slot hidden at a narrow width, or a ribbon laid out over it, has no
       room for a corner: no wire rather than a crossed one. */
    if (!from.right || first.middle <= from.bottom + RADIUS || last.middle <= to.bottom + RADIUS) {
      return;
    }

    const x0 = from.left + INSET;
    const into = first.left - 2;
    svg.appendChild(
      el("path", {
        class: "runner-wire runner-wire-in",
        d:
          "M" + x0 + " " + from.bottom +
          " V" + (first.middle - RADIUS) +
          " Q" + x0 + " " + first.middle + " " + (x0 + RADIUS) + " " + first.middle +
          " H" + (into - HEAD),
      })
    );
    svg.appendChild(
      el("path", {
        class: "runner-wire-head runner-wire-in",
        d:
          "M" + (into - HEAD) + " " + (first.middle - 4) +
          " L" + into + " " + first.middle +
          " L" + (into - HEAD) + " " + (first.middle + 4) + " Z",
      })
    );

    /* The last step is in the last column, at its top or its bottom: either
       way nothing stands to its right, and the way out leaves along its row. */
    const x1 = to.right - INSET;
    const up = to.bottom + 1;
    svg.appendChild(
      el("path", {
        class: "runner-wire runner-wire-out",
        d:
          "M" + (last.right + 2) + " " + last.middle +
          " H" + (x1 - RADIUS) +
          " Q" + x1 + " " + last.middle + " " + x1 + " " + (last.middle - RADIUS) +
          " V" + (up + HEAD),
      })
    );
    svg.appendChild(
      el("path", {
        class: "runner-wire-head runner-wire-out",
        d:
          "M" + (x1 - 4) + " " + (up + HEAD) +
          " L" + x1 + " " + up +
          " L" + (x1 + 4) + " " + (up + HEAD) + " Z",
      })
    );
  }

  function watch(panel) {
    let queued = false;
    const later = function () {
      if (queued) {
        return;
      }
      queued = true;
      requestAnimationFrame(function () {
        queued = false;
        draw(panel);
      });
    };
    /* The card changes size when the ribbon wraps, the picker or the lane
       shows, or the window is resized; the ribbon is rebuilt by attempts.js;
       the live part is shown and hidden by sse.js. */
    new ResizeObserver(later).observe(panel);
    new MutationObserver(function (records) {
      /* The wires' own redraw is a mutation too; it must not call for another. */
      const elsewhere = records.some(function (record) {
        const target = record.target;

        return !(target.closest && target.closest(".runner-wires"));
      });
      if (elsewhere) {
        later();
      }
    }).observe(panel, {
      childList: true,
      subtree: true,
      attributes: true,
      /* A step that fails is where the lane forks from. */
      attributeFilter: ["hidden", "data-state"],
    });
    later();
  }

  document.querySelectorAll(".workspace-panel").forEach(function (panel) {
    if (panel.querySelector(".machine") && panel.querySelector(".ribbon")) {
      watch(panel);
    }
  });
})();
