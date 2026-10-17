/* The flow file, in the page's one `[data-flow-dialog]`. The eye after the
   Flow field on a migration's card opens it; the text comes from
   `/api/flows/<id>/<version>`. A native <dialog>, like the confirm one: the
   page behind it is inert and Escape closes it. It opens read-only. Fork
   copies the text into a textarea, and Save fork posts that text to
   `/api/flows/fork`, which stores it as a new flow or refuses; the flow it
   was copied from is never written. The text is only ever written with
   textContent or a value: a flow is a file somebody imported. */
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
  const mode = dialog.querySelector("[data-flow-mode]");
  const forkButton = dialog.querySelector("[data-flow-fork]");
  const bar = dialog.querySelector("[data-flow-bar]");
  const idField = dialog.querySelector("[data-flow-id]");
  const versionField = dialog.querySelector("[data-flow-version]");
  const saveButton = dialog.querySelector("[data-flow-save]");
  const cancelButton = dialog.querySelector("[data-flow-cancel]");
  const editor = dialog.querySelector("[data-flow-edit]");
  let loaded = null;
  let editing = false;
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

  /* Typed-in text is not thrown away by a stray Escape or click. */
  function dirty() {
    return editing && loaded !== null && editor.value !== loaded.forkText;
  }

  function leave() {
    if (dirty() && !window.confirm("Discard the changes to this fork?")) {

      return false;
    }
    setEditing(false);

    return true;
  }

  dialog.querySelector("[data-flow-close]").addEventListener("click", function () {
    if (leave()) {
      dialog.close();
    }
  });
  /* Escape asks a dirty fork before it closes. */
  dialog.addEventListener("cancel", function (event) {
    if (!leave()) {
      event.preventDefault();
    }
  });
  /* The dialog has no padding of its own, so a press that lands on it and
     not on what it holds is a press on the backdrop. A fork being typed is
     not closed by one. */
  dialog.addEventListener("click", function (event) {
    if (event.target === dialog && !editing) {
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
    loaded = null;
    forkButton.hidden = true;
    setEditing(false);
  }

  /* One top-level key of the file, read and written as a line, so the rest
     of the text (comments included) is left exactly as it was. Written
     quoted: a bare 1.0 is a number to YAML, and a version is a string. */
  function keyLine(key) {

    return new RegExp("^" + key + ":[^\\n]*", "m");
  }

  function readKey(text, key) {
    const found = new RegExp("^" + key + ":[ \\t]*[\"']?([^\"'#\\s]*)", "m").exec(text);

    return found ? found[1] : "";
  }

  function writeKey(text, key, value) {

    return text.replace(keyLine(key), function () {

      return key + ': "' + value + '"';
    });
  }

  function setEditing(on) {
    editing = on;
    editor.hidden = !on;
    source.hidden = on;
    bar.hidden = !on;
    forkButton.hidden = on || loaded === null;
    mode.textContent = on ? "editing a fork" : "read-only";
    failure.hidden = true;
  }

  forkButton.addEventListener("click", function () {
    if (loaded === null) {
      return;
    }
    loaded.forkText = writeKey(loaded.text, "version", loaded.next);
    editor.value = loaded.forkText;
    idField.value = readKey(editor.value, "cartridge");
    versionField.value = readKey(editor.value, "version");
    setEditing(true);
    /* Focus puts the caret at the end, and the pane would scroll there. */
    editor.focus();
    editor.setSelectionRange(0, 0);
    editor.scrollTop = 0;
  });

  /* The two fields and the first lines of the text say the same thing, so
     each follows the other. The server reads the text alone. */
  idField.addEventListener("input", function () {
    editor.value = writeKey(editor.value, "cartridge", idField.value.trim());
  });
  versionField.addEventListener("input", function () {
    editor.value = writeKey(editor.value, "version", versionField.value.trim());
  });
  editor.addEventListener("input", function () {
    idField.value = readKey(editor.value, "cartridge");
    versionField.value = readKey(editor.value, "version");
  });

  cancelButton.addEventListener("click", leave);

  saveButton.addEventListener("click", function () {
    saveButton.disabled = true;
    failure.hidden = true;
    fetch("/api/flows/fork", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text: editor.value }),
    })
      .then(function (response) {
        /* A server that fails answers in plain text, and the parser's
           complaint about its first character says nothing of that. */
        return response.text().then(function (raw) {
          let body = {};
          try {
            body = JSON.parse(raw);
          } catch (err) {
            body = { error: "The server answered " + response.status + " and not with a flow reply." };
          }

          return { ok: response.ok, body: body };
        });
      })
      .then(function (answer) {
        if (answer.ok) {
          /* The new flow is in the Flow select once the page is read again;
             it is not picked for the card. */
          window.location.reload();

          return;
        }
        let text = answer.body.error || "The fork was not saved.";
        if (answer.body.next_version) {
          text += " The next free version is " + answer.body.next_version + ".";
        }
        fail(text);
      })
      .catch(function (err) {
        fail("Could not save the fork: " + err.message);
      })
      .finally(function () {
        saveButton.disabled = false;
      });
  });

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
    if (editing) {
      selectInEditor(node.lines[0], node.lines[1]);
    } else {
      paintLines(node.lines[0], node.lines[1]);
    }
  }

  /* The outline is of the file as it was forked: once the text has been
     typed into, its line numbers no longer say where anything is. */
  function selectInEditor(first, last) {
    if (dirty()) {

      return;
    }
    const lines = editor.value.split("\n");
    const start = lines.slice(0, first - 1).join("\n").length + (first > 1 ? 1 : 0);
    const end = lines.slice(0, last).join("\n").length;
    editor.focus();
    editor.setSelectionRange(start, end);
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
    loaded = { text: body.text, next: body.next_version, forkText: "" };
    forkButton.hidden = false;
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
