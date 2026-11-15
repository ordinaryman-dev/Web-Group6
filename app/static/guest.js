(function () {
  function openDialog(id) {
    const dialog = document.getElementById(id);
    if (dialog && typeof dialog.showModal === "function") dialog.showModal();
  }

  const filterLinks = Array.from(document.querySelectorAll("[data-bed-filter]"));
  const roomCards = Array.from(document.querySelectorAll(".room-card[data-bed-type]"));
  const filterEmpty = document.querySelector(".filter-empty");
  const searchForm = document.querySelector(".search-form");

  function applyBedFilter(value) {
    let visible = 0;
    roomCards.forEach(function (card) {
      const show = value === "all" || card.dataset.bedType === value;
      card.hidden = !show;
      if (show) visible += 1;
    });
    filterLinks.forEach(function (link) {
      link.classList.toggle("active", link.dataset.bedFilter === value);
    });
    if (filterEmpty) filterEmpty.hidden = visible > 0;
    if (searchForm) {
      let input = searchForm.querySelector('input[name="bed_type"]');
      if (value === "all") {
        if (input) input.remove();
      } else {
        if (!input) {
          input = document.createElement("input");
          input.type = "hidden";
          input.name = "bed_type";
          searchForm.appendChild(input);
        }
        input.value = value;
      }
    }
  }

  filterLinks.forEach(function (link) {
    link.addEventListener("click", function (event) {
      event.preventDefault();
      const value = link.dataset.bedFilter;
      applyBedFilter(value);
      const url = new URL(link.href, window.location.href);
      window.history.replaceState({}, "", url.pathname + url.search);
    });
  });
  applyBedFilter(new URLSearchParams(window.location.search).get("bed_type") || "all");

  document.querySelectorAll("[data-open]").forEach(function (button) {
    button.addEventListener("click", function () {
      openDialog(button.dataset.open);
    });
  });

  document.querySelectorAll("[data-close]").forEach(function (button) {
    button.addEventListener("click", function () {
      const dialog = button.closest("dialog");
      if (dialog) dialog.close();
    });
  });

  document.querySelectorAll("dialog.modal").forEach(function (dialog) {
    dialog.addEventListener("click", function (event) {
      if (event.target === dialog) dialog.close();
    });
  });

  // A change request that changes nothing is refused by the server. Say it in the
  // dialog and disable the button, so the guest sees it before submitting.
  function guardRequest(formId, submitId, warningSelector, isUnchanged) {
    const form = document.getElementById(formId);
    const warning = document.querySelector(warningSelector);
    const submit = document.getElementById(submitId);
    if (!form || !warning || !submit) return;
    const check = function () {
      const blocked = isUnchanged(form);
      warning.hidden = !blocked;
      submit.disabled = blocked;
      submit.classList.toggle("is-disabled", blocked);
    };
    form.addEventListener("input", check);
    form.addEventListener("change", check);
    check();
  }

  guardRequest("dates-form", "dates-submit", "[data-dates-warning]", function (form) {
    const checkIn = form.querySelector("#dates-check-in");
    const checkOut = form.querySelector("#dates-check-out");
    if (!checkIn || !checkOut) return false;
    return checkIn.value === form.dataset.currentCheckIn &&
      checkOut.value === form.dataset.currentCheckOut;
  });

  guardRequest("extension-form", "extension-submit", "[data-extension-warning]", function (form) {
    const checkOut = form.querySelector("#extension-check-out");
    if (!checkOut) return false;
    return !checkOut.value || checkOut.value <= form.dataset.currentCheckOut;
  });

  document.querySelectorAll("textarea[data-max-codepoints], textarea[data-max_codepoints]").forEach(function (area) {
    const max = parseInt(area.dataset.maxCodepoints || area.dataset.max_codepoints || "100", 10);
    let counter = area.parentElement.querySelector(".char-count");
    if (!counter) {
      counter = document.createElement("small");
      counter.className = "char-count";
      area.parentElement.appendChild(counter);
    }
    function update() {
      const count = Array.from(area.value).length;
      counter.textContent = count + " / " + max;
      counter.classList.toggle("over", count > max);
      area.classList.toggle("over", count > max);
    }
    area.addEventListener("input", update);
    update();
  });

  document.querySelectorAll("form").forEach(function (form) {
    form.addEventListener("submit", function (event) {
      const areas = form.querySelectorAll("textarea[data-max-codepoints], textarea[data-max_codepoints]");
      for (const area of areas) {
        const max = parseInt(area.dataset.maxCodepoints || area.dataset.max_codepoints || "100", 10);
        if (Array.from(area.value).length > max) {
          event.preventDefault();
          area.focus();
          return;
        }
      }
      const button = form.querySelector('button[type="submit"], input[type="submit"]');
      if (button) {
        button.disabled = true;
        button.classList.add("is-loading");
        if (button.tagName === "BUTTON") {
          button.dataset.label = button.textContent;
          button.textContent = "Please wait…";
        }
      }
    });
  });

  const search = document.querySelector(".search-form");
  if (search && search.dataset.afterCutoff === "1") {
    const checkIn = search.querySelector("#check_in");
    if (checkIn) checkIn.min = search.dataset.tomorrow;
  }
})();
