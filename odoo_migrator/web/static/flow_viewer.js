/* The flow file, read-only, in the page's one `[data-flow-dialog]`. The eye
   after the Flow field on a migration's card opens it; the text comes from
   `/api/flows/<id>/<version>`. A native <dialog>, like the confirm one: the
   page behind it is inert and Escape closes it. The text is only ever written
   with textContent: a flow is a file somebody imported. */
(function () {
  "use strict";

  const dialog = document.querySelector("[data-flow-dialog]");
  if (!dialog) {
    return;
  }
  const title = dialog.querySelector("[data-flow-title]");
  const hash = dialog.querySelector("[data-flow-hash]");
  const outlineList = dialog.querySelector("[data-flow-outline]");
  const source = dialog.querySelector("[data-flow-source]");
  const failure = dialog.querySelector("[data-flow-error]");
  let lineItems = [];
  let selectedButton = null;
  /* A second press while the first answer is on its way: only the last
     one paints. */
  let asked = 0;

  /* The eye follows the Flow select: an unsaved choice is the one the person
     is about to look at. The option value is `id@version`. */
  document.addEventListener("change", function (event) {
    const select = event.target;
    if (!select.matches || !select.matches("select[name=cartridge_id]")) {
      return;
    }
    const eye = select.closest("label").querySelector("[data-flow-view]");
    const at = select.value.indexOf("@");
    if (!eye || at < 1) {
      return;
    }
    const id = select.value.slice(0, at);
    const version = select.value.slice(at + 1);
    eye.setAttribute(
      "data-flow-url",
      "/api/flows/" + encodeURIComponent(id) + "/" + encodeURIComponent(version)
    );
    eye.setAttribute("data-flow-name", id + " " + version);
  });

  document.addEventListener("click", function (event) {
    const eye = event.target.closest("[data-flow-view]");
    if (eye) {
      matchWidth(eye);
      open(eye.getAttribute("data-flow-url"), eye.getAttribute("data-flow-name") || "");
    }
  });

  /* As wide as the card it opened from. A custom property: the width is the
     card's, which is per-page data. */
  function matchWidth(eye) {
    const card = eye.closest(".row-card");
    if (card) {
      dialog.style.setProperty("--flow-width", card.getBoundingClientRect().width + "px");
    } else {
      dialog.style.removeProperty("--flow-width");
    }
  }

  dialog.querySelector("[data-flow-close]").addEventListener("click", function () {
    dialog.close();
  });
  /* The dialog has no padding of its own, so a press that lands on it and
     not on what it holds is a press on the backdrop. */
  dialog.addEventListener("click", function (event) {
    if (event.target === dialog) {
      dialog.close();
    }
  });

  function fail(text) {
    failure.textContent = text;
    failure.hidden = false;
  }

  function clear() {
    outlineList.replaceChildren();
    source.replaceChildren();
    failure.hidden = true;
    failure.textContent = "";
    hash.textContent = "";
    lineItems = [];
    selectedButton = null;
  }

  function open(url, name) {
    asked += 1;
    const mine = asked;
    clear();
    title.textContent = name;
    dialog.showModal();
    fetch(url)
      .then(function (response) {
        if (!response.ok) {
          throw new Error("the server answered " + response.status);
        }

        return response.json();
      })
      .then(function (body) {
        if (mine === asked) {
          show(body);
        }
      })
      .catch(function (err) {
        if (mine === asked) {
          fail("Could not load the flow file: " + err.message);
        }
      });
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
        const opening = branch.hidden;
        branch.hidden = !opening;
        toggle.setAttribute("aria-expanded", opening ? "true" : "false");
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
    hash.textContent = body.sha256.slice(0, 12);
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
})();
