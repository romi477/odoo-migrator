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

  document.addEventListener("submit", function (event) {
    const form = event.target;
    if (!form.hasAttribute || !form.hasAttribute("data-confirm-text")) {
      return;
    }
    event.preventDefault();
    pending = form;
    title.textContent = form.getAttribute("data-confirm-title") || "Are you sure?";
    text.textContent = form.getAttribute("data-confirm-text");
    go.textContent = form.getAttribute("data-confirm-go") || "Confirm";
    dialog.showModal();
    cancel.focus();
  });

  cancel.addEventListener("click", function () {
    pending = null;
    dialog.close();
  });

  go.addEventListener("click", function () {
    const form = pending;
    pending = null;
    dialog.close();
    if (form) {
      /* `submit()` does not fire the submit event again, so the form is not
         asked about twice. */
      form.submit();
    }
  });

  /* Escape and a click on the backdrop close the dialog and confirm nothing. */
  dialog.addEventListener("close", function () {
    pending = null;
  });
  dialog.addEventListener("click", function (event) {
    if (event.target === dialog) {
      dialog.close();
    }
  });
})();
