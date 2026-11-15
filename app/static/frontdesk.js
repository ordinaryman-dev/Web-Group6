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
    row.addEventListener("click", function (event) {
      if (event.target.closest("a, button, form, input, select, textarea")) {
        return;
      }
      window.location.href = row.dataset.href;
    });
  });

  var drawer = document.querySelector(".drawer[data-close-url]");
  if (drawer) {
    document.addEventListener("keydown", function (event) {
      if (event.key === "Escape") {
        window.location.href = drawer.dataset.closeUrl;
      }
    });
  }

  function placeTip(card) {
    var tip = card.querySelector(".card-tip");
    if (!tip) {
      return;
    }
    tip.style.visibility = "hidden";
    tip.style.opacity = "0";
    tip.style.left = "0px";
    tip.style.top = "0px";
    var rect = card.getBoundingClientRect();
    var width = tip.offsetWidth || 218;
    var height = tip.offsetHeight || 90;
    var margin = 8;
    var left = Math.min(Math.max(margin, rect.left), window.innerWidth - width - margin);
    var top = rect.top - height - 6;
    if (top < margin) {
      top = rect.bottom + 6;
    }
    tip.style.left = left + "px";
    tip.style.top = top + "px";
    tip.style.visibility = "visible";
    tip.style.opacity = "1";
  }

  function hideTip(card) {
    var tip = card.querySelector(".card-tip");
    if (tip) {
      tip.style.visibility = "hidden";
      tip.style.opacity = "0";
    }
  }

  document.querySelectorAll(".room-card").forEach(function (card) {
    card.addEventListener("mouseenter", function () { placeTip(card); });
    card.addEventListener("mouseleave", function () { hideTip(card); });
    card.addEventListener("focus", function () { placeTip(card); });
    card.addEventListener("blur", function () { hideTip(card); });
  });
  window.addEventListener("scroll", function () {
    document.querySelectorAll(".room-card").forEach(hideTip);
  }, true);

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
})();
