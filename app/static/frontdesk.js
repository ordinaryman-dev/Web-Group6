/* Front desk interactions: live clock, confirmation, submit guard, row links. */
(function () {
  "use strict";

  var clock = document.getElementById("desk-clock");
  if (clock) {
    var epoch = Number(clock.dataset.epoch || 0);
    var offset = epoch - Date.now();
    var format = new Intl.DateTimeFormat("en-GB", {
      timeZone: "Asia/Shanghai",
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
      hour12: false
    });
    var tick = function () {
      clock.textContent = format.format(new Date(Date.now() + offset));
    };
    tick();
    window.setInterval(tick, 1000);
  }

  var markRequestStale = function (card) {
    if (!card || card.classList.contains("is-stale")) {
      return;
    }
    card.classList.add("is-stale");
    card.querySelectorAll("button, input[type='submit']").forEach(function (control) {
      control.disabled = true;
    });
    if (!card.querySelector(".stale-note")) {
      var note = document.createElement("p");
      note.className = "stale-note";
      note.textContent = "Handled in another tab. Refresh to see the result.";
      card.appendChild(note);
    }
  };

  var requestFeedShell = document.querySelector("[data-notify-feed]");
  document.addEventListener("submit", function (event) {
    var form = event.target;
    if (!(form instanceof HTMLFormElement) || !form.dataset.requestCheck ||
        form.dataset.requestChecked === "1") {
      return;
    }
    if (!requestFeedShell) {
      return;
    }
    event.preventDefault();
    var feed = requestFeedShell.dataset.notifyFeed;
    var separator = feed.indexOf("?") === -1 ? "?" : "&";
    window
      .fetch(feed + separator + "t=" + Date.now(), {
        headers: { Accept: "application/json" },
        credentials: "same-origin"
      })
      .then(function (response) {
        return response.ok ? response.json() : null;
      })
      .then(function (data) {
        if (!data) {
          form.dataset.requestChecked = "1";
          form.requestSubmit();
          return;
        }
        var stillPending = (data.pending || []).some(function (row) {
          return String(row.id) === form.dataset.requestCheck;
        });
        if (stillPending) {
          form.dataset.requestChecked = "1";
          form.requestSubmit();
        } else {
          markRequestStale(form.closest("[data-request-id]"));
        }
      })
      .catch(function () {
        // The server still enforces the request state if this check fails.
        form.dataset.requestChecked = "1";
        form.requestSubmit();
      });
  }, true);

  document.addEventListener("submit", function (event) {
    var form = event.target;
    if (!(form instanceof HTMLFormElement)) {
      return;
    }
    var submitter = event.submitter;
    var message = submitter && submitter.dataset ? submitter.dataset.confirm : "";
    if (message && !window.confirm(message)) {
      event.preventDefault();
      return;
    }
    if (form.hasAttribute("data-guard")) {
      form.classList.add("is-loading");
      var buttons = form.querySelectorAll("button[type='submit'], input[type='submit']");
      window.setTimeout(function () {
        buttons.forEach(function (button) {
          button.disabled = true;
        });
      }, 0);
    }
  });

  document.querySelectorAll("tr[data-href]").forEach(function (row) {
    row.setAttribute("tabindex", "0");
    row.setAttribute("role", "link");
    row.addEventListener("click", function (event) {
      if (event.target.closest("a, button, form, input, select, textarea")) {
        return;
      }
      window.location.href = row.dataset.href;
    });
    row.addEventListener("keydown", function (event) {
      if (event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        window.location.href = row.dataset.href;
      }
    });
  });

  var drawer = document.querySelector(".drawer[data-close-url]");
  if (drawer) {
    var drawerClose = drawer.querySelector(".drawer-close");
    if (drawerClose) {
      drawerClose.focus();
    }
    document.addEventListener("keydown", function (event) {
      if (event.key === "Escape") {
        window.location.href = drawer.dataset.closeUrl;
      }
    });
  }

  document.addEventListener("click", function (event) {
    document.querySelectorAll("details.bell[open]").forEach(function (bell) {
      if (!bell.contains(event.target)) {
        bell.open = false;
      }
    });
  });

  // Live bell: poll the notification feed so a new guest request shows up without
  // a page reload. Everything falls back to the server-rendered markup when the
  // fetch fails, so the desk still works offline or with JS blocked.
  var shell = document.querySelector("[data-notify-feed]");
  if (shell) {
    var feed = shell.dataset.notifyFeed;
    var version = shell.dataset.notifyVersion || "";
    var every = Number(shell.dataset.notifyInterval || 30000);

    var flash = function (element) {
      element.classList.remove("is-new");
      void element.offsetWidth;
      element.classList.add("is-new");
      window.setTimeout(function () {
        element.classList.remove("is-new");
      }, 4000);
    };

    var paint = function (data, arriving) {
      document.querySelectorAll("[data-notify-total]").forEach(function (element) {
        element.textContent = data.total;
        element.hidden = data.total === 0;
        if (arriving) {
          flash(element);
        }
      });
      document.querySelectorAll("[data-notify-badge]").forEach(function (element) {
        element.textContent = data.total;
        element.hidden = data.total === 0;
      });
      document.querySelectorAll("[data-notify-count]").forEach(function (element) {
        var key = element.dataset.notifyCount;
        if (Object.prototype.hasOwnProperty.call(data.counts, key)) {
          element.textContent = data.counts[key];
        }
      });
      // A task card whose request is no longer pending was handled in another tab.
      var waiting = {};
      (data.pending || []).forEach(function (row) {
        waiting[String(row.id)] = true;
      });
      document.querySelectorAll("[data-request-id]").forEach(function (card) {
        if (waiting[card.dataset.requestId] || card.classList.contains("is-stale")) {
          return;
        }
        markRequestStale(card);
      });

      var list = document.querySelector("[data-notify-items]");
      if (list) {
        list.innerHTML = "";
        if (!data.items.length) {
          var empty = document.createElement("p");
          empty.className = "bell-empty";
          empty.textContent = "All clear. Nothing needs attention.";
          list.appendChild(empty);
        }
        data.items.forEach(function (item) {
          var link = document.createElement("a");
          link.className = "bell-row tone-" + item.tone;
          link.href = item.href;
          var title = document.createElement("b");
          title.textContent = item.title;
          var note = document.createElement("small");
          note.textContent = item.text;
          link.appendChild(title);
          link.appendChild(note);
          list.appendChild(link);
        });
      }
    };

    var poll = function () {
      if (document.hidden) {
        return;
      }
      window
        .fetch(feed, { headers: { Accept: "application/json" }, credentials: "same-origin" })
        .then(function (response) {
          return response.ok ? response.json() : null;
        })
        .then(function (data) {
          if (!data || data.version === version) {
            return;
          }
          var arriving = data.counts.requests > 0;
          version = data.version;
          paint(data, arriving);
        })
        .catch(function () {
          /* keep the server-rendered state */
        });
    };

    window.setInterval(poll, every);
    document.addEventListener("visibilitychange", function () {
      if (!document.hidden) {
        poll();
      }
    });
  }

  // Check a maintenance block for overlapping bookings before it is submitted.
  var blockForm = document.querySelector("form[data-block-preview]");
  if (blockForm) {
    var blockOut = blockForm.querySelector("[data-block-preview-out]");
    var blockRoom = blockForm.dataset.blockRoom;
    var blockStart = blockForm.querySelector('input[name="block-start_date"]');
    var blockEnd = blockForm.querySelector('input[name="block-end_date"]');
    var blockTimer = null;

    var clearBlockPreview = function (message) {
      blockOut.textContent = message;
      blockOut.classList.remove("bad");
    };

    var previewBlock = function () {
      if (!blockStart.value || !blockEnd.value) {
        clearBlockPreview("Choose a date range to check for bookings.");
        return;
      }
      var query = new URLSearchParams({
        room_id: blockRoom,
        start_date: blockStart.value,
        end_date: blockEnd.value
      });
      window
        .fetch(blockForm.dataset.blockPreview + "?" + query.toString(), {
          headers: { Accept: "application/json" },
          credentials: "same-origin"
        })
        .then(function (response) {
          return response.ok ? response.json() : null;
        })
        .then(function (data) {
          if (!data) {
            clearBlockPreview("Choose a valid date range.");
            blockOut.classList.add("bad");
            return;
          }
          if (!data.count) {
            clearBlockPreview("No bookings fall inside this block.");
            return;
          }
          var references = data.bookings.map(function (booking) {
            return booking.reference;
          });
          clearBlockPreview(
            data.count + " booking" + (data.count === 1 ? "" : "s") +
            " conflict: " + references.join(", ") + ". They stay booked and will need a move."
          );
          blockOut.classList.add("bad");
        })
        .catch(function () {
          clearBlockPreview("Could not check conflicts. The server checks again on save.");
        });
    };

    var scheduleBlockPreview = function () {
      window.clearTimeout(blockTimer);
      blockTimer = window.setTimeout(previewBlock, 150);
    };
    [blockStart, blockEnd].forEach(function (input) {
      if (input) {
        input.addEventListener("change", scheduleBlockPreview);
        input.addEventListener("input", scheduleBlockPreview);
      }
    });
  }
})();
