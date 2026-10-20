/* A menu is a `<details data-menu>`: it opens on its button, and closes on a
   click anywhere else or on Escape, the way a menu does. A details alone
   stays open until its own summary is pressed again. */
(function () {
  function closeAll(except) {
    document.querySelectorAll("details[data-menu][open]").forEach(function (menu) {
      if (menu !== except) {
        menu.removeAttribute("open");
      }
    });
  }

  document.addEventListener("click", function (event) {
    closeAll(event.target.closest("details[data-menu]"));
  });

  document.addEventListener("keydown", function (event) {
    if (event.key !== "Escape") {
      return;
    }
    const open = document.querySelector("details[data-menu][open]");
    if (open) {
      open.removeAttribute("open");
      const summary = open.querySelector("summary");
      if (summary) {
        summary.focus();
      }
    }
  });
})();
