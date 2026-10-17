/* The flow file, read-only. Two things live here: the eye on a migration's
   card, which opens the viewer in a popup, and the viewer page itself. The
   text is only ever written with textContent: a flow is a file somebody
   imported. */
(function () {
  "use strict";

  const WINDOW = "popup,width=1000,height=760";

  /* The eye follows the Flow select: an unsaved choice is the one the person
     is about to look at. The option value is `id@version`. */
  function viewAddress(value) {
    const at = value.indexOf("@");
    if (at < 1) {
      return null;
    }

    return (
      "/flows/" +
      encodeURIComponent(value.slice(0, at)) +
      "/" +
      encodeURIComponent(value.slice(at + 1))
    );
  }

  document.addEventListener("click", function (event) {
    const eye = event.target.closest("a[data-flow-view]");
    if (!eye) {
      return;
    }
    const popup = window.open(eye.getAttribute("href"), "flow-viewer", WINDOW);
    /* A blocked popup comes back null; the link is a plain one and opens
       a tab on its own. */
    if (!popup) {
      return;
    }
    event.preventDefault();
    popup.focus();
  });

  document.addEventListener("change", function (event) {
    const select = event.target;
    if (!select.matches || !select.matches("select[name=cartridge_id]")) {
      return;
    }
    const eye = select.closest("label").querySelector("a[data-flow-view]");
    const address = viewAddress(select.value);
    if (eye && address) {
      eye.setAttribute("href", address);
    }
  });

  /* ---- the viewer page ---- */

  const root = document.querySelector("[data-flow-viewer]");
  if (!root) {
    return;
  }
  const outlineList = root.querySelector("[data-flow-outline]");
  const source = root.querySelector("[data-flow-source]");
  const failure = root.querySelector("[data-flow-error]");
  let lineItems = [];
  let selectedButton = null;

  function fail(text) {
    failure.textContent = text;
    failure.hidden = false;
  }

  function paintLines(first, last) {
    lineItems.forEach(function (item, index) {
      const number = index + 1;
      item.classList.toggle("is-selected", number >= first && number <= last);
    });
    if (lineItems[first - 1]) {
      lineItems[first - 1].scrollIntoView({ block: "start" });
    }
  }

  function select(button, node) {
    if (selectedButton) {
      selectedButton.removeAttribute("aria-current");
    }
    selectedButton = button;
    button.setAttribute("aria-current", "true");
    paintLines(node.lines[0], node.lines[1]);
  }

  function outlineItem(node, depth) {
    const item = document.createElement("li");
    item.className = "flow-outline-item";
    item.setAttribute("role", "none");
    const row = document.createElement("div");
    row.className = "flow-outline-row";
    /* A custom property: how far in this row sits is per-row data. */
    row.style.setProperty("--depth", String(depth));
    let branch = null;
    if (node.children.length) {
      const toggle = document.createElement("button");
      toggle.type = "button";
      toggle.className = "flow-outline-toggle";
      toggle.setAttribute("aria-label", "Show or hide " + node.label);
      toggle.setAttribute("aria-expanded", depth === 0 ? "true" : "false");
      toggle.textContent = "▸";
      row.appendChild(toggle);
      branch = document.createElement("ul");
      branch.className = "flow-outline-list";
      branch.hidden = depth !== 0;
      toggle.addEventListener("click", function () {
        const open = branch.hidden;
        branch.hidden = !open;
        toggle.setAttribute("aria-expanded", open ? "true" : "false");
      });
    } else {
      const gap = document.createElement("span");
      gap.className = "flow-outline-toggle is-leaf";
      gap.setAttribute("aria-hidden", "true");
      row.appendChild(gap);
    }
    const label = document.createElement("button");
    label.type = "button";
    label.className = "flow-outline-label";
    const name = document.createElement("span");
    name.className = "flow-outline-name mono";
    name.textContent = node.label;
    label.appendChild(name);
    if (node.preview) {
      const preview = document.createElement("span");
      preview.className = "flow-outline-preview";
      preview.textContent = node.preview;
      label.appendChild(preview);
    }
    label.addEventListener("click", function () {
      select(label, node);
    });
    row.appendChild(label);
    item.appendChild(row);
    if (branch) {
      node.children.forEach(function (child) {
        branch.appendChild(outlineItem(child, depth + 1));
      });
      item.appendChild(branch);
    }

    return item;
  }

  function show(body) {
    document.title = body.cartridge_id + " " + body.version + " — flow file";
    const hash = root.querySelector("[data-flow-hash]");
    if (hash) {
      hash.textContent = body.sha256.slice(0, 12);
    }
    lineItems = body.text.split("\n").map(function (text, index) {
      const item = document.createElement("li");
      item.className = "flow-line";
      const number = document.createElement("span");
      number.className = "flow-line-no";
      number.textContent = String(index + 1);
      const code = document.createElement("span");
      code.className = "flow-line-text";
      code.textContent = text;
      item.appendChild(number);
      item.appendChild(code);
      source.appendChild(item);

      return item;
    });
    body.outline.forEach(function (node) {
      outlineList.appendChild(outlineItem(node, 0));
    });
    if (!body.outline.length) {
      fail("This file is not valid YAML, so there is no outline. The text is shown as it is.");
    }
  }

  fetch(root.getAttribute("data-flow-url"))
    .then(function (response) {
      if (!response.ok) {
        throw new Error("the server answered " + response.status);
      }

      return response.json();
    })
    .then(show)
    .catch(function (err) {
      fail("Could not load the flow file: " + err.message);
    });
})();
