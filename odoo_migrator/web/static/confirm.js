/* Ask before a form does something that should not happen by accident.

   A form opts in with `data-confirm-title` and `data-confirm-text`. Pressing
   its button opens the page's one `[data-confirm-dialog]` with those words,
   and the form is sent only when the person confirms. Cancel is the button
   that has focus, so Enter on a dialog nobody read does nothing. */
(function () {
  const dialog = document.querySelector("[data-confirm-dialog]");
  if (!dialog) {
    return;
  }
  const title = dialog.querySelector("[data-confirm-title]");
  const text = dialog.querySelector("[data-confirm-text]");
  const go = dialog.querySelector("[data-confirm-go]");
  const cancel = dialog.querySelector("[data-confirm-cancel]");
  let pending = null;
  let pressed = null;
  /* A control that is not a form (the log's Clear) asks through here. */
  let then = null;

  function ask(words) {
    title.textContent = words.title || "Are you sure?";
    text.textContent = words.text || "";
    go.textContent = words.go || "Confirm";
    dialog.showModal();
    cancel.focus();
  }

  window.askToConfirm = function (words, onYes) {
    pending = null;
    pressed = null;
    then = onYes;
    ask(words);
  };

  document.addEventListener("submit", function (event) {
    const form = event.target;
    if (!form.hasAttribute || !form.hasAttribute("data-confirm-text")) {
      return;
    }
    /* The confirmed send, coming back through here once. */
    if (form.dataset.confirmed === "yes") {
      delete form.dataset.confirmed;
      return;
    }
    event.preventDefault();
    pending = form;
    /* The button that was pressed carries a name and value of its own (a
       bin says which key), and `submit()` would send the form without it. */
    pressed = event.submitter || null;
    then = null;
    ask({
      title: form.getAttribute("data-confirm-title"),
      text: form.getAttribute("data-confirm-text"),
      go: form.getAttribute("data-confirm-go"),
    });
  });

  cancel.addEventListener("click", function () {
    pending = null;
    pressed = null;
    then = null;
    dialog.close();
  });

  go.addEventListener("click", function () {
    const form = pending;
    const button = pressed;
    const onYes = then;
    pending = null;
    pressed = null;
    then = null;
    dialog.close();
    if (onYes) {
      onYes();
    }
    if (form) {
      form.dataset.confirmed = "yes";
      if (typeof form.requestSubmit === "function") {
        form.requestSubmit(button && button.form === form ? button : undefined);
      } else {
        form.submit();
      }
    }
  });

  /* Escape and a click on the backdrop close the dialog and confirm nothing. */
  dialog.addEventListener("close", function () {
    pending = null;
    pressed = null;
    then = null;
  });
  dialog.addEventListener("click", function (event) {
    if (event.target === dialog) {
      dialog.close();
    }
  });
})();
