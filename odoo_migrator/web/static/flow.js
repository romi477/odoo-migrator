/* The Flow canvas. Positions are computed here; the API does not store them. */
(function () {
  var canvas = document.getElementById("flow-canvas");
  if (!canvas || typeof cytoscape === "undefined") {
    return;
  }
  var migration = canvas.getAttribute("data-migration");
  var embedded = document.getElementById("flow-graph");
  if (migration) {
    fetch("/api/flow/" + migration + "/graph")
      .then(function (response) {
        return response.json();
      })
      .then(draw);
    return;
  }
  if (embedded) {
    draw(JSON.parse(embedded.textContent));
  }

  var trace = document.getElementById("flow-trace");
  var body = document.getElementById("flow-body");
  var job = trace && trace.getAttribute("data-job");
  if (trace && job) {
    fetch("/api/flow/" + job + "/trace")
      .then(function (response) {
        return response.json();
      })
      .then(function (rows) {
        rows.forEach(function (row) {
          var item = document.createElement("li");
          var button = document.createElement("button");
          button.type = "button";
          button.textContent = row.side + " " + row.tool + " " + row.preview;
          button.addEventListener("click", function () {
            fetch("/api/flow/trace/" + row.id + "/body")
              .then(function (response) {
                return response.text();
              })
              .then(function (text) {
                body.hidden = false;
                body.textContent = text;
              });
          });
          item.appendChild(button);
          trace.appendChild(item);
        });
      });
  }

  function draw(graph) {
      var elements = [];
      var mainIndex = 0;
      var failureIndex = 0;
      graph.nodes.forEach(function (node) {
        var failure = node.lane === "failure";
        var index = failure ? failureIndex : mainIndex;
        if (failure) {
          failureIndex += 1;
        } else {
          mainIndex += 1;
        }
        elements.push({
          data: {
            id: node.id,
            label: node.id,
            kind: node.kind,
            lane: node.lane,
            gated_on: node.gated_on || "",
          },
          position: { x: failure ? 140 : 460, y: 36 + index * 64 },
        });
      });
      graph.edges.forEach(function (edge, index) {
        elements.push({
          data: {
            id: "e" + index,
            source: edge.from,
            target: edge.to,
            kind: edge.kind,
          },
        });
      });
      cytoscape({
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
              "background-color": "#c4552a",
              color: "#1c140f",
              "font-size": "11px",
              width: "label",
              height: 28,
              padding: "8px",
              shape: "round-rectangle",
            },
          },
          {
            selector: "node[lane = 'failure']",
            style: { "background-color": "#e4d7cc" },
          },
          {
            selector: "edge",
            style: {
              width: 1.5,
              "line-color": "#8a7568",
              "target-arrow-color": "#8a7568",
              "target-arrow-shape": "triangle",
              "curve-style": "bezier",
            },
          },
          {
            selector: "edge[kind = 'retry']",
            style: { "line-style": "dashed", "line-color": "#c4552a" },
          },
        ],
      });
  }
})();
