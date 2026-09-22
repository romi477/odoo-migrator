/* Module tree: Cytoscape canvas, strike-out, marquee, drag into batch groups. */
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

  function stepsFromDepends(rows) {
    const known = new Set(rows.map(function (row) {
      return row.technical_name;
    }));
    const children = {};
    rows.forEach(function (row) {
      children[row.technical_name] = [];
    });
    rows.forEach(function (row) {
      (row.depends || []).forEach(function (dep) {
        if (known.has(dep)) {
          children[dep].push(row.technical_name);
        }
      });
    });
    const roots = rows
      .filter(function (row) {
        return !(row.depends || []).some(function (dep) {
          return known.has(dep);
        });
      })
      .map(function (row) {
        return row.technical_name;
      })
      .sort();
    const rank = {};
    roots.forEach(function (name) {
      rank[name] = 1;
    });
    const queue = roots.slice();
    while (queue.length) {
      const name = queue.shift();
      (children[name] || []).forEach(function (child) {
        if (rank[child] == null) {
          rank[child] = rank[name] + 1;
          queue.push(child);
        }
      });
    }
    const leftover = rows
      .map(function (row) {
        return row.technical_name;
      })
      .filter(function (name) {
        return rank[name] == null;
      });
    if (leftover.length) {
      let bump = 1;
      Object.keys(rank).forEach(function (name) {
        if (rank[name] >= bump) {
          bump = rank[name] + 1;
        }
      });
      leftover.forEach(function (name) {
        rank[name] = bump;
      });
    }

    return rank;
  }

  const ranks = stepsFromDepends(modules);
  modules.forEach(function (row) {
    row.step = ranks[row.technical_name];
  });
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

  const elements = [];
  modules.forEach(function (row) {
    elements.push({
      data: {
        id: row.technical_name,
        module_id: row.id,
        node_id: row.node_id,
        migration_rank: row.migration_rank,
        label: row.technical_name,
        struck_out: row.struck_out,
        absent: row.absent_from_odoo,
        batch_group: row.batch_group,
        status: row.status,
        step: row.step,
        notes: row.notes || "",
        question: row.question || "",
        has_assumptions: row.has_assumptions,
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
    selectionType: "additive",
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
        style: { "border-color": cyan, "border-width": 2 },
      },
      {
        selector: "node[struck_out = true]",
        style: {
          "text-decoration": "line-through",
          color: muted,
          opacity: 0.55,
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

  const steps = [];
  modules.forEach(function (row) {
    if (row.step != null && steps.indexOf(row.step) === -1) {
      steps.push(row.step);
    }
  });
  steps.sort(function (a, b) {
    return a - b;
  });
  if (steps.length) {
    const legend = document.createElement("ul");
    legend.className = "step-legend";
    steps.forEach(function (step) {
      const item = document.createElement("li");
      item.className = "graph-node step-" + step;
      item.textContent = "step " + step;
      legend.appendChild(item);
    });
    const stage = canvas.closest(".graph-stage") || canvas;
    stage.after(legend);
  }

  const badges = document.getElementById("graph-badges");
  function placeBadges() {
    if (!badges) {
      return;
    }
    badges.replaceChildren();
    cy.nodes().forEach(function (node) {
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
  cy.on("render", placeBadges);
  placeBadges();

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

  function nextGroup() {
    let high = 0;
    cy.nodes().forEach(function (node) {
      const group = node.data("batch_group");
      if (typeof group === "number" && group > high) {
        high = group;
      }
    });
    return high + 1;
  }

  function postBatch(moduleId, group) {
    const body = "batch_group=" + (group == null ? "" : String(group));
    return fetch("/tree/modules/" + moduleId + "/batch", {
      method: "POST",
      headers: { "Content-Type": "application/x-www-form-urlencoded" },
      body: body,
    });
  }

  function selectedModules() {
    return cy.nodes(":selected").filter(function (node) {
      return node.data("module_id");
    });
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
        if (error) {
          error.hidden = true;
          error.textContent = "";
        }
        (body.modules || []).forEach(function (row) {
          cy.nodes()
            .filter(function (node) {
              return String(node.data("module_id")) === String(row.id);
            })
            .data("struck_out", row.struck_out);
        });
        const open = selectedModules();
        if (open.length === 1) {
          fillCard(open[0]);
        }
      });
    });
  }

  const card = document.getElementById("node-card");
  const cardEmpty = document.getElementById("node-card-empty");
  const cardBody = document.getElementById("node-card-body");

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
    if (statusEl) {
      statusEl.className = "badge " + (node.data("status") || "queued");
      statusEl.textContent = node.data("status") || "queued";
    }
    if (skipEl) {
      skipEl.textContent = node.data("struck_out") ? "Undo skip" : "Skip";
    }
    if (askForm) {
      askForm.action = "/tree/modules/" + moduleId + "/answer";
      askForm.hidden = node.data("status") !== "waiting_for_human";
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
  }

  const strike = document.getElementById("tree-strike");
  if (strike) {
    strike.addEventListener("click", function () {
      const picks = selectedModules();
      if (picks.length === 0) {
        return;
      }
      /* One decision for the whole selection, not a toggle each. Toggling
         made a single press strike some and un-strike others, so a marquee
         over a group containing an earlier mistake made it worse. Anything
         unstruck in the selection means strike; otherwise bring them back.
         Pressing again is then an exact undo. */
      const wanted = picks.some(function (node) {
        return !node.data("struck_out");
      })
        ? "1"
        : "0";
      Promise.all(
        picks.map(function (node) {
          return applySkip(node.data("module_id"), wanted);
        })
      );
    });
  }

  const ungroup = document.getElementById("tree-ungroup");
  if (ungroup) {
    ungroup.addEventListener("click", function () {
      const picks = selectedModules();
      if (picks.length === 0) {
        return;
      }
      /* Group only ever assigned the next number, so a wrong grouping could
         not be undone — regrouping just climbed. */
      Promise.all(
        picks.map(function (node) {
          return postBatch(node.data("module_id"), null);
        })
      ).then(function () {
        window.location.reload();
      });
    });
  }

  const group = document.getElementById("tree-group");
  if (group) {
    group.addEventListener("click", function () {
      const picks = selectedModules();
      if (picks.length === 0) {
        return;
      }
      const assigned = nextGroup();
      Promise.all(
        picks.map(function (node) {
          return postBatch(node.data("module_id"), assigned);
        })
      ).then(function () {
        window.location.reload();
      });
    });
  }

  cy.on("dragfree", "node", function (event) {
    const dragged = event.target;
    const pos = dragged.renderedPosition();
    const hit = cy.nodes().filter(function (node) {
      if (node.id() === dragged.id() || !node.data("module_id")) {
        return false;
      }
      const box = node.renderedBoundingBox();
      return pos.x >= box.x1 && pos.x <= box.x2 && pos.y >= box.y1 && pos.y <= box.y2;
    });
    if (hit.length === 0) {
      return;
    }
    const target = hit[0];
    let assigned = target.data("batch_group");
    const writes = [];
    if (assigned == null) {
      assigned = nextGroup();
      writes.push(postBatch(target.data("module_id"), assigned));
      target.data("batch_group", assigned);
    }
    writes.push(postBatch(dragged.data("module_id"), assigned));
    dragged.data("batch_group", assigned);
    Promise.all(writes);
  });

  function post(url, body) {
    return fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/x-www-form-urlencoded" },
      body: body,
    });
  }

  const migrate = document.getElementById("tree-migrate");
  const migrateCount = document.getElementById("tree-migrate-count");
  if (migrate) {
    migrate.addEventListener("click", function (event) {
      const picks = selectedModules().filter(function (node) {
        return !node.data("struck_out");
      });
      if (migrateCount) {
        migrateCount.value = String(picks.length);
      }
      if (picks.length !== 1) {
        /* Let the form submit natively: a fetch would swallow the page that
           says which of the two mistakes this was. */
        return;
      }
      event.preventDefault();
      /* SSE patches the rows. Reloading would wipe a half-typed Alpine field. */
      post("/tree/migrate", "module_id=" + encodeURIComponent(picks[0].data("module_id")));
    });
  }

  const batchRun = document.getElementById("tree-batch");
  const batchGroup = document.getElementById("tree-batch-group");
  if (batchRun && batchGroup) {
    batchRun.addEventListener("click", function () {
      const picks = selectedModules().filter(function (node) {
        return !node.data("struck_out");
      });
      const groups = [];
      picks.forEach(function (node) {
        const group = node.data("batch_group");
        if (group != null && groups.indexOf(group) === -1) {
          groups.push(group);
        }
      });
      batchGroup.value = groups.length === 1 ? String(groups[0]) : "";
    });
  }

  cy.on("select unselect", "node", function () {
    const picks = selectedModules();
    if (picks.length === 1) {
      fillCard(picks[0]);
    } else {
      hideCard();
    }
  });

  if (card && selectedModules().length === 1) {
    fillCard(selectedModules()[0]);
  }

  const cardSkip = document.getElementById("node-card-skip");
  if (cardSkip) {
    cardSkip.addEventListener("click", function () {
      const picks = selectedModules();
      if (picks.length !== 1) {
        return;
      }
      const node = picks[0];
      const wanted = node.data("struck_out") ? "0" : "1";
      applySkip(node.data("module_id"), wanted);
    });
  }
})();
