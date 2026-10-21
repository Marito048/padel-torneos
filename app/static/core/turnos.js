(function () {
  "use strict";

  var panel = document.querySelector("[data-turno-panel]");
  if (!panel) {
    return;
  }

  var details = Array.prototype.slice.call(panel.querySelectorAll("[data-turno-detail]"));
  var empty = panel.querySelector("[data-turno-empty]");
  var openers = Array.prototype.slice.call(document.querySelectorAll("[data-turno-open]"));
  var mobileQuery = window.matchMedia("(max-width: 900px)");
  var lastOpener = null;

  function setBodyLock(isOpen) {
    document.body.classList.toggle("turno-detail-open", Boolean(isOpen && mobileQuery.matches));
  }

  function updateUrl(detailId) {
    var url = new URL(window.location.href);
    if (detailId) {
      url.searchParams.set("turno", detailId.replace("turno-detalle-", ""));
    } else {
      url.searchParams.delete("turno");
    }
    url.hash = "";
    window.history.replaceState(null, "", url.pathname + url.search);
  }

  function setOpenerState(detailId) {
    openers.forEach(function (opener) {
      var isActive = Boolean(detailId) && opener.dataset.turnoOpen === detailId;
      opener.setAttribute("aria-expanded", isActive ? "true" : "false");
      var card = opener.closest(".turno-card");
      if (card) {
        card.classList.toggle("turno-card--selected", isActive);
      }
    });
  }

  function visibleCloseButton() {
    var detail = panel.querySelector("[data-turno-detail]:not([hidden])");
    return detail ? detail.querySelector("[data-turno-close]") : null;
  }

  function showDetail(detailId, options) {
    options = options || {};
    var found = null;
    details.forEach(function (detail) {
      var isActive = detail.id === detailId;
      detail.hidden = !isActive;
      if (isActive) {
        found = detail;
      }
    });
    if (!found) {
      return;
    }
    if (empty) {
      empty.hidden = true;
    }
    setOpenerState(detailId);
    panel.classList.add("is-open");
    setBodyLock(true);
    if (!options.keepUrl) {
      updateUrl(detailId);
    }
    if (mobileQuery.matches) {
      panel.scrollTop = 0;
      var closeButton = visibleCloseButton();
      if (closeButton && !options.noFocus) {
        closeButton.focus({ preventScroll: true });
      }
    }
    if (!lastOpener) {
      lastOpener = openers.find(function (opener) {
        return opener.dataset.turnoOpen === detailId;
      }) || null;
    }
  }

  function closeDetail() {
    details.forEach(function (detail) {
      detail.hidden = true;
    });
    if (empty) {
      empty.hidden = false;
    }
    setOpenerState("");
    panel.classList.remove("is-open");
    setBodyLock(false);
    updateUrl(null);
    if (lastOpener) {
      lastOpener.focus({ preventScroll: true });
    }
  }

  openers.forEach(function (opener) {
    opener.addEventListener("click", function (event) {
      if (
        event.defaultPrevented ||
        event.button !== 0 ||
        event.metaKey ||
        event.ctrlKey ||
        event.shiftKey ||
        event.altKey
      ) {
        return;
      }
      event.preventDefault();
      lastOpener = opener;
      showDetail(opener.dataset.turnoOpen);
    });
  });

  panel.addEventListener("click", function (event) {
    var closeButton = event.target.closest("[data-turno-close]");
    if (closeButton) {
      closeDetail();
    }
  });

  document.addEventListener("keydown", function (event) {
    if (event.key === "Escape" && panel.classList.contains("is-open") && mobileQuery.matches) {
      closeDetail();
    }
  });

  var initial = panel.dataset.selectedTurno;
  if (initial) {
    showDetail("turno-detalle-" + initial, { keepUrl: true, noFocus: true });
  }

  mobileQuery.addEventListener("change", function () {
    setBodyLock(panel.classList.contains("is-open"));
  });
})();
