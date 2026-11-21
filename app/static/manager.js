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

const roomEditor = document.querySelector("[data-room-editor]");
if (roomEditor) {
  const roomTypes = JSON.parse(roomEditor.querySelector("[data-room-preview-types]").textContent);
  const typeSelect = roomEditor.querySelector('[name="room_type_id"]');
  const image = roomEditor.querySelector("[data-room-preview-image]");
  const updateRoomPhoto = () => {
    const roomType = roomTypes.find((item) => String(item.id) === typeSelect.value);
    if (!roomType) return;
    image.src = roomType.image;
    image.alt = roomType.name;
    roomEditor.querySelector("[data-room-preview-name]").textContent = roomType.name;
    roomEditor.querySelector("[data-room-preview-description]").textContent = roomType.description;
    roomEditor.querySelector("[data-room-preview-capacity]").textContent =
      `Up to ${roomType.capacity} guest${roomType.capacity === 1 ? "" : "s"}`;
  };
  typeSelect.addEventListener("change", updateRoomPhoto);
  updateRoomPhoto();
}
