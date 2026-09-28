/* Timestamps, in the reader's zone.

   Everything is stored and sent as UTC with its offset, because an instant
   without a zone is a guess. Only the browser knows where the reader is, so
   the formatting happens here and nowhere on the server. */
(function () {
  function pad(value) {
    return String(value).padStart(2, "0");
  }

  function parse(iso) {
    if (!iso) {
      return null;
    }
    var when = new Date(iso);

    return isNaN(when.getTime()) ? null : when;
  }

  /* Date and time to the minute. What a card is stamped with: it is read to
     tell one run from another, not to measure anything. */
  function localStamp(iso) {
    var when = parse(iso);
    if (!when) {
      return "";
    }

    return (
      when.getFullYear() +
      "-" +
      pad(when.getMonth() + 1) +
      "-" +
      pad(when.getDate()) +
      " " +
      pad(when.getHours()) +
      ":" +
      pad(when.getMinutes())
    );
  }

  /* Time of day, to the second. A log line is read against the line above
     it, and the date is the same for all of them. */
  function localClock(iso) {
    var when = parse(iso);
    if (!when) {
      return "";
    }

    return (
      pad(when.getHours()) + ":" + pad(when.getMinutes()) + ":" + pad(when.getSeconds())
    );
  }

  /* A server-rendered `<time datetime="...">` carries the instant and its
     own text is the fallback. Hydrating is not optional decoration: without
     it the page shows an ISO string with an offset the reader has to do in
     their head. */
  function hydrate(root) {
    var scope = root || document;
    scope.querySelectorAll("time[data-stamp]").forEach(function (node) {
      var text = localStamp(node.getAttribute("datetime"));
      if (text) {
        node.textContent = text;
      }
    });
  }

  window.localStamp = localStamp;
  window.localClock = localClock;
  window.hydrateStamps = hydrate;
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", function () {
      hydrate();
    });
  } else {
    hydrate();
  }
})();
