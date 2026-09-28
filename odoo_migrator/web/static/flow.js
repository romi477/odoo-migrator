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
  var ACCENT = "#c4552a";
  var QUIET = "#e4d7cc";
  var LINE = "#8a7568";
  var INK = "#1c140f";

  if (migration) {
    fetch("/api/flow/" + migration + "/graph")
      .then(function (response) {
        return response.json();
      })
      .then(draw);
  } else if (embedded) {
    draw(JSON.parse(embedded.textContent));
  }

  /* A gated step says so on the node. Without the flag, `probe` reads as part
     of every run, and `patch` reads as something that happens before
     `rewrite` rather than instead of it. */
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
          style: { "border-width": 3, "border-color": "#7ec8e3" },
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
      cy.$("node").removeClass("is-selected");
      event.target.addClass("is-selected");
      inspect(event.target.data());
    });
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
