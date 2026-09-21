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
  const elements = [];
  modules.forEach(function (row) {
    elements.push({
      data: {
        id: row.technical_name,
        module_id: row.id,
        label: row.technical_name,
        struck_out: row.struck_out,
        absent: row.absent_from_odoo,
        batch_group: row.batch_group,
        status: row.status,
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

  const styles = getComputedStyle(document.documentElement);
  const text = styles.getPropertyValue("--text").trim();
  const muted = styles.getPropertyValue("--muted").trim();
  const border = styles.getPropertyValue("--border").trim();
  const panel = styles.getPropertyValue("--panel-raised").trim();
  const cyan = styles.getPropertyValue("--cyan").trim();
  const amber = styles.getPropertyValue("--amber").trim();

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
          "background-color": panel,
          "border-color": border,
          "border-width": 1,
          "font-family": "ui-monospace, SFMono-Regular, Menlo, monospace",
          "font-size": 11,
          "text-valign": "center",
          "text-halign": "center",
          width: "label",
          height: "label",
          padding: "8px",
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
        selector: "node[batch_group]",
        style: { "border-color": amber },
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

  /* The lamp sends the module id here. Opening the right migration and
     then leaving the human to find one stalled node in a hundred is not
     opening the module. */
  const focus = new URLSearchParams(window.location.search).get("focus");
  if (focus) {
    const stalled = cy.nodes().filter(function (node) {
      return String(node.data("module_id")) === focus;
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

  function post(url, body) {
    return fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/x-www-form-urlencoded" },
      body: body,
    });
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
          return post(
            "/tree/modules/" + node.data("module_id") + "/strike",
            "struck=" + wanted
          );
        })
      ).then(function () {
        window.location.reload();
      });
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
})();
