/* Put text on the clipboard and say so on the button that asked.

   The button gets `is-copied` or `is-failed` for a moment; the colours are
   the stylesheet's. Shared by the run log and the result summary. */
(function () {
  /* Copying is why the log exists: a failed run gets pasted into a chat or
     an issue. `navigator.clipboard` is the good path, and it is refused
     often enough — a permissions policy, an embedded view, a browser that
     wants a stronger user gesture — that a button relying on it alone is a
     button that sometimes silently does nothing. The textarea fallback
     needs no permission. */
  function copyText(text, button) {
    function done(ok) {
      button.classList.add(ok ? "is-copied" : "is-failed");
      setTimeout(function () {
        button.classList.remove("is-copied");
        button.classList.remove("is-failed");
      }, 1200);
    }
    function viaTextarea() {
      var pad = document.createElement("textarea");
      pad.value = text;
      pad.setAttribute("readonly", "");
      pad.style.position = "fixed";
      pad.style.opacity = "0";
      document.body.appendChild(pad);
      pad.select();
      var ok = false;
      try {
        ok = document.execCommand("copy");
      } catch (err) {
        ok = false;
      }
      document.body.removeChild(pad);
      done(ok);
    }
    if (!navigator.clipboard) {
      viaTextarea();
      return;
    }
    navigator.clipboard.writeText(text).then(function () {
      done(true);
    }, viaTextarea);
  }

  window.copyText = copyText;
})();
