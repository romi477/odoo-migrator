/* Module tree: Cytoscape canvas, strike-out, marquee.

   Dragging a node onto another used to put both in a batch group: state
   nothing drew and nothing on the screens ran, written by any drag that
   happened to end over a node. A drag only moves the node now. */
(function () {
  const holder = document.getElementById("module-graph");
  const canvas = document.getElementById("graph-canvas");
  if (!holder || !canvas || typeof cytoscape === "undefined") {
    return;
  }

  const modules = JSON.parse(holder.textContent || "[]");
  const names = new Set(modules.map(function (row) {
    return row.technical_name;
  }));

  /* The step each row carries is the server's (`graph.topological_steps`):
     the wave the module runs in. This file used to compute its own, and the
     two had to be kept in step by hand. */
  /* Cytoscape's colour parser does not understand oklch(); a custom
     property from getComputedStyle is still the specified oklch string.
     Paint it onto a probe so the browser resolves rgb(), which it does
     parse. Hex is the fallback if the token is missing. */
  const probe = document.createElement("span");
  probe.style.cssText = "position:absolute;width:0;height:0;overflow:hidden;";
  document.body.appendChild(probe);
  const STEP_COLOURS = {
    1: { fill: "#1a8fb0", edge: "#5ecde8" },
    2: { fill: "#1a9a4a", edge: "#4edc7a" },
    3: { fill: "#d48a14", edge: "#f0c040" },
    4: { fill: "#d04098", edge: "#f070c0" },
    5: { fill: "#5a4ad4", edge: "#9080f0" },
    6: { fill: "#d04038", edge: "#f07068" },
    7: { fill: "#d4b014", edge: "#f0d050" },
    8: { fill: "#9030d0", edge: "#c070f0" },
    more: { fill: "#4a5870", edge: "#8a9ab0" },
  };

  function toHex(rgb) {
    const match = /^rgba?\((\d+),\s*(\d+),\s*(\d+)/.exec(rgb);
    if (!match) {
      return "";
    }

    return (
      "#" +
      [match[1], match[2], match[3]]
        .map(function (part) {
          return ("0" + Number(part).toString(16)).slice(-2);
        })
        .join("")
    );
  }

  function paint(value) {
    probe.style.color = "rgb(1, 2, 3)";
    probe.style.color = value;
    const resolved = getComputedStyle(probe).color;
    if (!resolved || resolved === "rgb(1, 2, 3)") {
      return "";
    }

    return toHex(resolved);
  }

  const text = paint("var(--text)") || "#e4dff0";
  const muted = paint("var(--muted)") || "#9a90b0";
  const border = paint("var(--border)") || "#3a3550";
  const panel = paint("var(--panel-raised)") || "#2a2540";
  const cyan = paint("var(--cyan)") || "#4eb8d0";
  const glowGap = 3;
  const glowRingGap = 3;

  function colourForStep(step, kind) {
    const key = kind === "edge" ? "edge" : "fill";
    if (step == null) {
      return kind === "edge" ? border : panel;
    }
    const pair = STEP_COLOURS[step] || STEP_COLOURS.more;

    return pair[key];
  }

  function indexText(row) {
    const step = row.step == null ? "" : String(row.step);
    const rankNum =
      row.migration_rank != null
        ? Number(row.migration_rank)
        : Number(row.node_id) >= 1
          ? row.node_id
          : Number(row.module_id || row.id);
    const rank = Number(rankNum) >= 1 ? "#" + rankNum : "";
    if (rank && step) {
      return rank + "/" + step;
    }

    return rank || (step ? "/" + step : "");
  }

  function moduleLabel(name, struck) {
    if (struck) {
      return name + "\n(out)";
    }

    return name;
  }

  const elements = [];
  modules.forEach(function (row) {
    elements.push({
      data: {
        id: row.technical_name,
        module_id: row.id,
        node_id: row.node_id,
        migration_rank: row.migration_rank,
        label: moduleLabel(row.technical_name, row.struck_out),
        struck_out: row.struck_out,
        absent: row.absent_from_odoo,
        status: row.status,
        step: row.step,
        notes: row.notes || "",
        question: row.question || "",
        has_assumptions: row.has_assumptions,
        path_problem: row.path_problem || "",
        step_color: colourForStep(row.step, "fill"),
        step_edge: colourForStep(row.step, "edge"),
      },
    });
    /* One edge per dependency. A single parent drew one of them: on a real
       database that lost half of the 256 edges, and batch order is read off
       these edges. */
    (row.depends || []).forEach(function (dep) {
      if (names.has(dep)) {
        elements.push({
          data: {
            id: dep + "->" + row.technical_name,
            source: dep,
            target: row.technical_name,
          },
        });
      }
    });
  });

  const cy = cytoscape({
    container: canvas,
    elements: elements,
    boxSelectionEnabled: canvas.getAttribute("data-box-select") === "true",
    selectionType: "single",
    style: [
      {
        selector: "node",
        style: {
          label: "data(label)",
          color: text,
          "background-color": "data(step_color)",
          "border-color": "data(step_edge)",
          "border-width": 2,
          "font-family": "ui-monospace, SFMono-Regular, Menlo, monospace",
          "font-size": 11,
          "text-valign": "center",
          "text-halign": "center",
          "text-wrap": "wrap",
          "text-max-width": 140,
          width: "label",
          height: "label",
          padding: "18px 12px 10px",
          shape: "round-rectangle",
        },
      },
      {
        selector: "node:selected",
        style: {
          "overlay-opacity": 0,
        },
      },
      {
        selector: "node.struck",
        style: {
          color: muted,
          opacity: 0.5,
          "border-style": "dashed",
          "border-color": muted,
          "background-opacity": 0.35,
        },
      },
      {
        selector: "edge",
        style: {
          width: 1,
          "line-color": border,
          "target-arrow-color": border,
          "target-arrow-shape": "triangle",
          "curve-style": "bezier",
        },
      },
    ],
    layout: {
      name: "breadthfirst",
      directed: true,
      spacingFactor: 1.15,
      padding: 16,
    },
  });

  function syncStruckVisual(node, struck) {
    const out = !!struck;
    node.data("struck_out", out);
    if (out) {
      node.addClass("struck");
    } else {
      node.removeClass("struck");
    }
    node.data("label", moduleLabel(node.id(), out));
  }

  cy.nodes().forEach(function (node) {
    if (node.data("struck_out")) {
      syncStruckVisual(node, true);
    }
  });

  const steps = [];
  modules.forEach(function (row) {
    if (row.step != null && steps.indexOf(row.step) === -1) {
      steps.push(row.step);
    }
  });
  steps.sort(function (a, b) {
    return a - b;
  });
  const legend = document.getElementById("step-legend");
  if (legend && steps.length) {
    legend.hidden = false;
    legend.replaceChildren();
    steps.forEach(function (step) {
      const item = document.createElement("li");
      const button = document.createElement("button");
      button.type = "button";
      const stepClass =
        step >= 1 && step <= 8 ? "step-" + step : "step-more";
      button.className = "graph-node " + stepClass;
      button.setAttribute("data-step", String(step));
      button.setAttribute("aria-pressed", "false");
      button.textContent = "step " + step;
      item.appendChild(button);
      legend.appendChild(item);
    });
  } else if (legend) {
    legend.hidden = true;
  }

  const badges = document.getElementById("graph-badges");
  const selectionRings = document.getElementById("graph-selection-rings");

  function appendGlowRing(parent, x, y, width, height) {
    const ring = document.createElement("span");
    ring.className = "graph-selection-ring";
    ring.style.setProperty("--x", Math.round(x) + "px");
    ring.style.setProperty("--y", Math.round(y) + "px");
    ring.style.setProperty("--w", Math.round(width) + "px");
    ring.style.setProperty("--h", Math.round(height) + "px");
    parent.appendChild(ring);
  }

  function placeSelectionRings() {
    if (!selectionRings) {
      return;
    }
    selectionRings.replaceChildren();
    cy.nodes(":selected").forEach(function (node) {
      const box = node.renderedBoundingBox({
        includeLabels: false,
        includeOverlays: false,
      });
      const width = box.x2 - box.x1;
      const height = box.y2 - box.y1;
      const line = 1;
      const innerPad = glowGap + line;
      appendGlowRing(
        selectionRings,
        box.x1 - innerPad,
        box.y1 - innerPad,
        width + innerPad * 2,
        height + innerPad * 2,
      );
      const outerPad = glowGap + line + glowRingGap + line;
      appendGlowRing(
        selectionRings,
        box.x1 - outerPad,
        box.y1 - outerPad,
        width + outerPad * 2,
        height + outerPad * 2,
      );
    });
  }

  /* What the graph module table counts as done (`graph.DONE`): passed here,
     or already installed on the target before the run. Tree drew every
     module alike, so nothing on it said which ones were finished; Result
     has said so with this corner since it was built. */
  const DONE_STATUSES = { passed: "Passed", migrated: "Already on the target" };

  /* A done module cannot be excluded (`graph._finished`): the server
     refuses, so the card does not offer it. */
  function isDone(node) {
    return !node.data("struck_out") && !!DONE_STATUSES[node.data("status")];
  }

  function placeDoneCorner(node, box) {
    const said = DONE_STATUSES[node.data("status")];
    if (!said || node.data("struck_out")) {
      return;
    }
    const corner = document.createElement("span");
    corner.className = "result-corner-badge done graph-node-done";
    corner.title = said;
    const glyph = document.createElement("span");
    glyph.className = "result-corner-glyph-check";
    glyph.textContent = "\u2713";
    corner.appendChild(glyph);
    corner.style.setProperty("--x", Math.round(box.x2) + "px");
    corner.style.setProperty("--y", Math.round(box.y1) + "px");
    badges.appendChild(corner);
  }

  function placeBadges() {
    if (!badges) {
      return;
    }
    badges.replaceChildren();
    cy.nodes().forEach(function (node) {
      placeDoneCorner(
        node,
        node.renderedBoundingBox({ includeLabels: false, includeOverlays: false }),
      );
      const text = indexText({
        node_id: node.data("node_id"),
        migration_rank: node.data("migration_rank"),
        module_id: node.data("module_id"),
        step: node.data("step"),
      });
      if (!text) {
        return;
      }
      const box = node.renderedBoundingBox({
        includeLabels: false,
        includeOverlays: false,
      });
      const width = box.x2 - box.x1;
      const height = box.y2 - box.y1;
      /* Sit inside the rounded rect. A 2px inset lands on the curve and
         reads as text outside the card, and a second copy in the label
         put the same index in the centre once zoomed. */
      const insetX = Math.max(10, width * 0.08);
      const insetY = Math.max(8, height * 0.18);
      const font = Math.max(9, Math.min(height * 0.2, 14));
      const badge = document.createElement("span");
      badge.className = "graph-node-index";
      badge.textContent = text;
      badge.style.setProperty("--x", Math.round(box.x1 + insetX) + "px");
      badge.style.setProperty("--y", Math.round(box.y1 + insetY) + "px");
      badge.style.setProperty("--index-size", Math.round(font) + "px");
      badges.appendChild(badge);
    });
  }
  function paintGraphChrome() {
    placeBadges();
    placeSelectionRings();
  }

  cy.on("render", paintGraphChrome);
  paintGraphChrome();

  /* The lamp sends the module id here. A module waiting for a human is
     already on the card. Opening the right migration and then leaving
     the human to find one stalled node in a hundred is not opening it. */
  const opened = document.getElementById("node-card");
  const focus =
    new URLSearchParams(window.location.search).get("focus") ||
    (opened && opened.getAttribute("data-module-id")) ||
    "";
  if (focus) {
    const stalled = cy.nodes().filter(function (node) {
      return String(node.data("module_id")) === String(focus);
    });
    if (stalled.length) {
      cy.nodes().unselect();
      stalled.select();
      cy.center(stalled);
    }
  }

  function selectedModules() {
    return cy.nodes(":selected").filter(function (node) {
      return node.data("module_id");
    });
  }

  function applyStrikeRows(body) {
    const error = document.getElementById("node-card-error");
    (body.modules || []).forEach(function (row) {
      cy.nodes()
        .filter(function (node) {
          return String(node.data("module_id")) === String(row.id);
        })
        .forEach(function (node) {
          syncStruckVisual(node, row.struck_out);
        });
    });
    if (error) {
      error.hidden = true;
      error.textContent = "";
    }
    refreshCard();
  }

  function applySkip(moduleId, wanted) {
    return fetch("/tree/modules/" + moduleId + "/strike", {
      method: "POST",
      headers: {
        "Content-Type": "application/x-www-form-urlencoded",
        Accept: "application/json",
      },
      body: "struck=" + wanted,
    }).then(function (response) {
      return response.json().then(function (body) {
        const error = document.getElementById("node-card-error");
        if (!response.ok) {
          if (error) {
            error.hidden = false;
            error.textContent = body.error || "Could not update the module.";
          }

          return;
        }
        applyStrikeRows(body);
      });
    });
  }

  function applyLeaveOut(moduleIds) {
    const body = moduleIds
      .map(function (id) {
        return "module_ids=" + encodeURIComponent(id);
      })
      .join("&");
    return fetch("/tree/modules/leave-out", {
      method: "POST",
      headers: {
        "Content-Type": "application/x-www-form-urlencoded",
        Accept: "application/json",
      },
      body: body,
    }).then(function (response) {
      return response.json().then(function (payload) {
        const error = document.getElementById("node-card-error");
        if (!response.ok) {
          if (error) {
            error.hidden = false;
            error.textContent = payload.error || "Could not update the module.";
          }

          return;
        }
        applyStrikeRows(payload);
      });
    });
  }

  function applyBulk(url) {
    return fetch(url, {
      method: "POST",
      headers: { Accept: "application/json" },
    }).then(function (response) {
      return response.json().then(function (payload) {
        const error = document.getElementById("node-card-error");
        if (!response.ok) {
          if (error) {
            error.hidden = false;
            error.textContent = payload.error || "Could not update the modules.";
          }

          return;
        }
        applyStrikeRows(payload);
      });
    });
  }

  function applyRestore(moduleIds) {
    const body = moduleIds
      .map(function (id) {
        return "module_ids=" + encodeURIComponent(id);
      })
      .join("&");
    return fetch("/tree/modules/restore", {
      method: "POST",
      headers: {
        "Content-Type": "application/x-www-form-urlencoded",
        Accept: "application/json",
      },
      body: body,
    }).then(function (response) {
      return response.json().then(function (payload) {
        const error = document.getElementById("node-card-error");
        if (!response.ok) {
          if (error) {
            error.hidden = false;
            error.textContent = payload.error || "Could not update the module.";
          }

          return;
        }
        applyStrikeRows(payload);
      });
    });
  }

  const card = document.getElementById("node-card");
  const cardEmpty = document.getElementById("node-card-empty");
  const cardBody = document.getElementById("node-card-body");

  function cardDisplayStatus(node) {
    if (node.data("struck_out")) {
      return "excluded";
    }

    return node.data("status") || "queued";
  }

  function setSkipHint(group, nodes) {
    const footnote = document.getElementById("node-card-skip-hint");
    const hintEl = document.getElementById("node-card-skip-hint-text");
    if (!footnote || !hintEl) {
      return;
    }
    if (group && nodes && nodes.length > 1) {
      hintEl.textContent =
        "Exclude — the selection and every module that depends on it, " +
        "except the ones already migrated. " +
        "Include — the selection, with depends chains upward only.";

      return;
    }
    if (nodes && nodes.length === 1 && isDone(nodes[0])) {
      hintEl.textContent =
        "Already migrated — it stays in the plan. Reopen it on Result to run it again.";

      return;
    }
    const struck =
      nodes && nodes.length === 1 && nodes[0].data("struck_out");
    hintEl.textContent = struck
      ? "Include — this module's depends chain upward."
      : "Exclude — skip this module; parents stay in the plan.";
  }

  function syncSelectionActions(mode, nodes) {
    const skipEl = document.getElementById("node-card-skip");
    const includeEl = document.getElementById("node-card-include");
    if (!card || !skipEl) {
      return;
    }
    card.setAttribute("data-selection", mode);
    const multi = mode === "many";
    if (includeEl) {
      includeEl.hidden = !multi;
    }
    if (!multi) {
      return;
    }
    skipEl.textContent = "Exclude";
    skipEl.classList.add("primary");
    if (includeEl) {
      includeEl.textContent = "Include";
      includeEl.classList.add("primary");
    }
    const anyIn = nodes.some(function (node) {
      return !node.data("struck_out") && !isDone(node);
    });
    const anyOut = nodes.some(function (node) {
      return node.data("struck_out");
    });
    skipEl.disabled = !anyIn;
    if (includeEl) {
      includeEl.disabled = !anyOut;
    }
  }

  function updateLegendPressed() {
    if (!legend) {
      return;
    }
    legend.querySelectorAll("[data-step]").forEach(function (button) {
      const step = Number(button.getAttribute("data-step"));
      const onStep = cy.nodes().filter(function (node) {
        return Number(node.data("step")) === step;
      });
      const selected = onStep.filter(":selected");
      const all =
        onStep.length > 0 && selected.length === onStep.length;
      button.setAttribute("aria-pressed", all ? "true" : "false");
    });
  }

  function fillGroupCard(nodes) {
    if (!card || !cardBody || !cardEmpty || !nodes.length) {
      return;
    }
    const stepKeys = [];
    nodes.forEach(function (node) {
      const step = node.data("step");
      if (stepKeys.indexOf(step) === -1) {
        stepKeys.push(step);
      }
    });
    stepKeys.sort(function (a, b) {
      return Number(a) - Number(b);
    });
    const sameStep = stepKeys.length === 1;
    card.setAttribute("data-module-id", "");
    cardEmpty.hidden = true;
    cardBody.hidden = false;
    const indexEl = document.getElementById("node-card-index");
    const nameEl = document.getElementById("node-card-name");
    const statusEl = document.getElementById("node-card-status");
    const skipEl = document.getElementById("node-card-skip");
    const askForm = document.getElementById("node-card-ask");
    const assumptionsEl = document.getElementById("node-card-assumptions");
    if (indexEl) {
      indexEl.textContent = "";
    }
    if (nameEl) {
      nameEl.textContent = sameStep
        ? "step " + stepKeys[0] + " · " + nodes.length + " modules"
        : nodes.length + " modules selected";
    }
    if (statusEl) {
      statusEl.hidden = true;
    }
    const pathEl = document.getElementById("node-card-path");
    if (pathEl) {
      pathEl.hidden = true;
    }
    if (assumptionsEl) {
      assumptionsEl.hidden = true;
    }
    syncSelectionActions("many", nodes);
    setSkipHint(true, nodes);
    if (askForm) {
      askForm.hidden = true;
    }
  }

  function fillCard(node) {
    if (!card || !cardBody || !cardEmpty || !node || !node.data("module_id")) {
      return;
    }
    const moduleId = node.data("module_id");
    const idx = indexText({
      node_id: node.data("node_id"),
      migration_rank: node.data("migration_rank"),
      module_id: node.data("module_id"),
      step: node.data("step"),
    });
    card.setAttribute("data-module-id", String(moduleId));
    cardEmpty.hidden = true;
    cardBody.hidden = false;
    const indexEl = document.getElementById("node-card-index");
    const nameEl = document.getElementById("node-card-name");
    const statusEl = document.getElementById("node-card-status");
    const skipEl = document.getElementById("node-card-skip");
    const askForm = document.getElementById("node-card-ask");
    const questionEl = document.getElementById("node-card-question");
    const assumptionsEl = document.getElementById("node-card-assumptions");
    if (indexEl) {
      indexEl.textContent = idx;
    }
    if (nameEl) {
      nameEl.textContent = node.id();
    }
    const display = cardDisplayStatus(node);
    const struck = display === "excluded";
    if (statusEl) {
      statusEl.hidden = false;
      statusEl.className = "badge " + display;
      statusEl.textContent = display;
    }
    /* Not a status: the module keeps its own. Says it cannot start until
       its directory is mounted, and why. */
    const pathEl = document.getElementById("node-card-path");
    if (pathEl) {
      const problem = node.data("path_problem");
      pathEl.hidden = !problem;
      pathEl.title = problem
        ? "The app does not know where this module is on disk: " + problem
        : "";
    }
    syncSelectionActions("one", [node]);
    if (skipEl) {
      skipEl.disabled = isDone(node);
      skipEl.textContent = struck ? "Include" : "Exclude";
      skipEl.classList.add("primary");
    }
    setSkipHint(false, [node]);
    if (askForm) {
      askForm.action = "/tree/modules/" + moduleId + "/answer";
      askForm.hidden = struck || node.data("status") !== "waiting_for_human";
      const answerBox = askForm.querySelector("textarea[name='answer']");
      const answerBtn = askForm.querySelector("button[type='submit']");
      if (answerBox) {
        answerBox.setAttribute("aria-label", "Answer for " + node.id());
      }
      if (answerBtn) {
        answerBtn.setAttribute("aria-label", "Answer " + node.id());
      }
    }
    if (questionEl) {
      questionEl.textContent = node.data("question") || "";
    }
    if (assumptionsEl) {
      assumptionsEl.hidden = !node.data("has_assumptions");
    }
  }

  function hideCard() {
    if (!card || !cardBody || !cardEmpty) {
      return;
    }
    card.setAttribute("data-module-id", "");
    cardEmpty.hidden = false;
    cardBody.hidden = true;
    const footnote = document.getElementById("node-card-skip-hint");
    if (footnote) {
      footnote.hidden = true;
    }
    updateLegendPressed();
  }

  function refreshCard() {
    const picks = selectedModules();
    if (picks.length === 0) {
      hideCard();

      return;
    }
    const footnote = document.getElementById("node-card-skip-hint");
    if (footnote) {
      footnote.hidden = false;
    }
    if (picks.length === 1) {
      fillCard(picks[0]);
    } else {
      fillGroupCard(picks);
    }
    updateLegendPressed();
  }

  if (legend) {
    legend.addEventListener("click", function (event) {
      const button = event.target.closest("[data-step]");
      if (!button) {
        return;
      }
      const step = Number(button.getAttribute("data-step"));
      cy.elements().unselect();
      cy.nodes()
        .filter(function (node) {
          return Number(node.data("step")) === step;
        })
        .select();
      refreshCard();
    });
  }

  cy.on("select unselect", "node", function () {
    refreshCard();
  });

  if (card && selectedModules().length === 1) {
    fillCard(selectedModules()[0]);
    updateLegendPressed();
  }

  const leaveOutAll = document.getElementById("tree-leave-out-all");
  if (leaveOutAll) {
    leaveOutAll.addEventListener("click", function () {
      applyBulk("/tree/modules/leave-out-all");
    });
  }

  const includeAll = document.getElementById("tree-include-all");
  if (includeAll) {
    includeAll.addEventListener("click", function () {
      applyBulk("/tree/modules/include-all");
    });
  }

  function selectionModuleIds(nodes) {
    return nodes.map(function (node) {
      return node.data("module_id");
    });
  }

  const cardSkip = document.getElementById("node-card-skip");
  if (cardSkip && card) {
    cardSkip.addEventListener("click", function () {
      const picks = selectedModules();
      if (!picks.length || cardSkip.disabled) {
        return;
      }
      if (card.getAttribute("data-selection") === "many") {
        const active = picks.filter(function (node) {
          return !node.data("struck_out");
        });
        if (!active.length) {
          return;
        }
        applyLeaveOut(selectionModuleIds(active));

        return;
      }
      const node = picks[0];
      if (node.data("struck_out")) {
        applyRestore([node.data("module_id")]);

        return;
      }
      applyLeaveOut([node.data("module_id")]);
    });
  }

  const cardInclude = document.getElementById("node-card-include");
  if (cardInclude && card) {
    cardInclude.addEventListener("click", function () {
      const picks = selectedModules();
      if (!picks.length || cardInclude.disabled) {
        return;
      }
      const struck = picks.filter(function (node) {
        return node.data("struck_out");
      });
      if (!struck.length) {
        return;
      }
      applyRestore(selectionModuleIds(struck));
    });
  }
})();
