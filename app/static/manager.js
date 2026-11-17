const zoom = document.querySelector("[data-plan-zoom]");
if (zoom) {
  const map = document.getElementById(zoom.getAttribute("aria-controls"));
  const revealMatch = () => {
    const match = map.querySelector(".is-match");
    if (match) {
      const room = match.getBoundingClientRect();
      const frame = map.getBoundingClientRect();
      map.scrollLeft += room.left - frame.left + (room.width - frame.width) / 2;
    }
  };
  zoom.hidden = false;
  zoom.addEventListener("click", () => {
    const enlarged = map.classList.toggle("is-zoomed");
    zoom.setAttribute("aria-pressed", String(enlarged));
    zoom.textContent = enlarged ? "Zoom out" : "Zoom in";
    revealMatch();
  });
  revealMatch();
}

const reportFilters = document.querySelector("[data-report-filters]");
if (reportFilters) {
  const period = reportFilters.elements.period;
  const fields = reportFilters.querySelectorAll("[data-report-date]");
  const updateDateFields = () => {
    const mode = period.value === "custom" ? "custom" : "preset";
    fields.forEach((label) => {
      const input = label.querySelector("input");
      label.hidden = label.dataset.reportDate !== mode;
      input.disabled = label.hidden;
      input.required = !label.hidden && mode === "custom";
    });
  };
  period.addEventListener("change", updateDateFields);
  updateDateFields();
}
