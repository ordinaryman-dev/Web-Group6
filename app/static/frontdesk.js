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
})();
