/* The Flow canvas. Positions are computed here; the API does not store them. */
(function () {
  var canvas = document.getElementById("flow-canvas");
  if (!canvas || typeof cytoscape === "undefined") {
    return;
  }
  var migration = canvas.getAttribute("data-migration");
  var embedded = document.getElementById("flow-graph");
  var cyGraph = null;
  var COLUMN = { main: 200, failure: 560 };

  /* The stylesheet's colours for the drawing, resolved to what Cytoscape
     can paint. They were constants here and in the stylesheet both. */
  function token(name, fallback) {
    var value = getComputedStyle(canvas).getPropertyValue(name).trim();

    return value || fallback;
  }

  var ACCENT = token("--flow-agent", "#c4552a");
  var QUIET = token("--flow-function", "#e4d7cc");
  var LINE = token("--flow-line", "#8a7568");
  var INK = token("--flow-ink", "#1c140f");
  var SELECTED = token("--flow-selected", "#7ec8e3");

  /* A drawing that could not be had says so, in the canvas it would fill. */
  function unavailable(text) {
    var note = document.createElement("p");
    note.className = "flow-unavailable field-hint";
    note.textContent = text;
    canvas.replaceChildren(note);
  }

  if (migration) {
    fetch("/api/flow/" + encodeURIComponent(migration) + "/graph")
      .then(function (response) {
        if (!response.ok) {
          throw new Error(String(response.status));
        }
        return response.json();
      })
      .then(draw)
      .catch(function () {
        unavailable("The flow could not be drawn: the app did not hand over its graph. Reload the page.");
      });
  } else if (embedded) {
    draw(JSON.parse(embedded.textContent));
  }

  /* Cytoscape does not paint oklch(), which is what the theme computes to.
     A one-pixel canvas turns that into rgb the edge labels can sit on. */
  function panelColor() {
    var probe = document.createElement("canvas");
    probe.width = probe.height = 1;
    var context = probe.getContext("2d");
    context.fillStyle = getComputedStyle(canvas).backgroundColor;
    context.fillRect(0, 0, 1, 1);
    var pixel = context.getImageData(0, 0, 1, 1).data;

    return "rgb(" + pixel[0] + "," + pixel[1] + "," + pixel[2] + ")";
  }

  /* A gated step says so on the node. Without the flag, `probe` reads as part
     of every run, and `patch` reads as something that happens before
     `rewrite` rather than instead of it. */
  function label(node) {
    if (node.gated_on) {
      return node.label + "\nif " + node.gated_on;
    }

    return node.label;
  }

  function draw(graph) {
    var elements = [];
    var row = { main: 0, failure: 0 };
    graph.nodes.forEach(function (node) {
      var data = {
        id: node.id,
        label: label(node),
        kind: node.kind,
        lane: node.lane,
        gated: node.gated_on ? "yes" : "no",
        hint: node.hint || "",
        actor: node.actor || "",
        sessions: node.sessions || [],
        tools: node.tools || [],
        context: node.context || [],
        raises: node.raises || [],
        prompt: node.prompt || "",
        gatedOn: node.gated_on || "",
      };
      if (node.kind === "lane") {
        elements.push({ data: data });
        return;
      }
      data.parent = node.parent;
      var index = row[node.lane] || 0;
      row[node.lane] = index + 1;
      elements.push({
        data: data,
        position: { x: COLUMN[node.lane] || COLUMN.main, y: 60 + index * 68 },
      });
    });
    graph.edges.forEach(function (edge, index) {
      elements.push({
        data: {
          id: "e" + index,
          source: edge.from,
          target: edge.to,
          kind: edge.kind,
          label: edge.label || "",
        },
      });
    });
    var cy = cytoscape({
      container: canvas,
      elements: elements,
      layout: { name: "preset" },
      style: [
        {
          selector: "node",
          style: {
            label: "data(label)",
            "text-valign": "center",
            "text-halign": "center",
            "text-wrap": "wrap",
            "background-color": QUIET,
            "border-width": 1,
            "border-color": LINE,
            color: INK,
            "font-size": "11px",
            width: "label",
            height: "label",
            padding: "8px",
            shape: "round-rectangle",
          },
        },
        {
          selector: "node[kind = 'agent']",
          style: { "background-color": ACCENT, "border-color": ACCENT },
        },
        {
          /* The one node that stops the run instead of passing it on. */
          selector: "node[kind = 'ask_human']",
          style: {
            shape: "cut-rectangle",
            "border-width": 2,
            "border-color": ACCENT,
          },
        },
        {
          selector: "node[gated = 'yes']",
          style: { "border-style": "dashed", "border-width": 2 },
        },
        {
          selector: "node.is-selected",
          style: { "border-width": 3, "border-color": SELECTED },
        },
        {
          selector: "node[kind = 'lane']",
          style: {
            shape: "round-rectangle",
            "background-color": QUIET,
            "background-opacity": 0.15,
            "border-color": LINE,
            "border-style": "dotted",
            color: QUIET,
            "text-valign": "top",
            "text-halign": "center",
            "text-margin-y": 6,
            "font-size": "11px",
            "text-transform": "uppercase",
            padding: "28px",
          },
        },
        {
          selector: "edge",
          style: {
            width: 1.5,
            "line-color": LINE,
            "target-arrow-color": LINE,
            "target-arrow-shape": "triangle",
            "curve-style": "bezier",
            "font-size": "11px",
            color: QUIET,
            "text-background-color": panelColor(),
            "text-background-opacity": 1,
            "text-background-padding": "4px",
            "text-background-shape": "roundrectangle",
          },
        },
        {
          selector: "edge[kind = 'failure']",
          style: { label: "data(label)", "line-style": "dashed" },
        },
        {
          selector: "edge[kind = 'retry']",
          style: {
            label: "data(label)",
            "line-style": "dashed",
            "line-color": ACCENT,
            "target-arrow-color": ACCENT,
            color: ACCENT,
            "font-size": "11px",
          },
        },
      ],
    });
    cyGraph = cy;
    cy.on("tap", "node[kind != 'lane']", function (event) {
      select(event.target);
    });
    /* A ribbon block on Runner links here as `/flow#understand`. The ids
       match because both come from the same place: `step.id or step.kind`.
       Arriving at a drawing of eighteen boxes with no idea which one was
       meant is the reason the link exists. */
    fromHash();
    if (!window.__flowHashBound) {
      window.__flowHashBound = true;
      window.addEventListener("hashchange", fromHash);
    }
    var close = document.getElementById("flow-inspect-close");
    if (close && !close.dataset.bound) {
      close.dataset.bound = "yes";
      close.addEventListener("click", function () {
        if (cyGraph) {
          cyGraph.$("node").removeClass("is-selected");
        }
        var board = document.querySelector(".flow-board");
        var aside = document.getElementById("flow-inspect");
        if (board) {
          board.classList.remove("is-inspecting");
        }
        if (aside) {
          aside.hidden = true;
        }
        if (cyGraph) {
          requestAnimationFrame(function () {
            cyGraph.resize();
          });
        }
      });
    }
  }

  function select(node) {
    if (!cyGraph) {
      return;
    }
    cyGraph.$("node").removeClass("is-selected");
    node.addClass("is-selected");
    inspect(node.data());
  }

  function fromHash() {
    var wanted = decodeURIComponent((window.location.hash || "").slice(1));
    if (!wanted || !cyGraph) {
      return;
    }
    var node = cyGraph.getElementById(wanted);
    if (!node || node.empty() || node.data("kind") === "lane") {
      return;
    }
    select(node);
    /* `resize` runs on the next frame, after the inspector has taken its
       width. Centring before that centres on the old viewport. */
    requestAnimationFrame(function () {
      cyGraph.center(node);
    });
  }

  function listText(values) {
    return values && values.length ? values.join(", ") : "none";
  }

  function inspect(data) {
    var aside = document.getElementById("flow-inspect");
    var board = document.querySelector(".flow-board");
    if (!aside) {
      return;
    }
    aside.hidden = false;
    if (board) {
      board.classList.add("is-inspecting");
    }
    if (cyGraph) {
      requestAnimationFrame(function () {
        cyGraph.resize();
      });
    }
    document.getElementById("flow-inspect-title").textContent = data.id;
    document.getElementById("flow-inspect-actor").textContent =
      data.actor === "agent"
        ? "Agent. A model runs this turn."
        : "Function. The app runs this. No model.";
    var when = document.getElementById("flow-inspect-when");
    if (data.gatedOn) {
      when.hidden = false;
      when.textContent = "Runs only if " + data.gatedOn + " was raised.";
    } else {
      when.hidden = true;
      when.textContent = "";
    }
    document.getElementById("flow-inspect-summary").textContent = data.hint || "";
    document.getElementById("flow-inspect-sessions").textContent = listText(data.sessions);
    var tools = document.getElementById("flow-inspect-tools");
    tools.replaceChildren();
    var names = data.tools || [];
    if (!names.length) {
      var none = document.createElement("li");
      none.textContent = "No tools. This step does not call the model.";
      tools.appendChild(none);
    } else {
      names.forEach(function (name) {
        var item = document.createElement("li");
        item.className = "mono";
        item.textContent = name;
        tools.appendChild(item);
      });
    }
    document.getElementById("flow-inspect-context").textContent = listText(data.context);
    document.getElementById("flow-inspect-raises").textContent = listText(data.raises);
    var prompt = document.getElementById("flow-inspect-prompt");
    if (data.prompt) {
      prompt.textContent = data.prompt;
    } else {
      prompt.textContent = "No prompt. The step is a function in the app.";
    }
  }
})();
