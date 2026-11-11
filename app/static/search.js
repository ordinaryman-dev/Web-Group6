const searchForm = document.querySelector("#room-search");
const message = document.querySelector("#search-message");
const results = document.querySelector("#room-results");

function makeElement(tag, text, className) {
  const element = document.createElement(tag);
  element.textContent = text;
  if (className) element.className = className;
  return element;
}

function renderRooms(rooms) {
  results.replaceChildren();
  if (rooms.length === 0) {
    results.append(makeElement("p", "No room type is available for the whole stay."));
    return;
  }
  for (const room of rooms) {
    const card = makeElement("article", "", "room-card");
    card.append(
      makeElement("h3", room.name),
      makeElement("p", room.description),
      makeElement("p", "Up to " + room.capacity + " guests", "muted"),
      makeElement("p", "¥" + room.total + " stay total", "price")
    );
    const rates = makeElement("ul", "", "rates");
    for (const rate of room.rates) {
      rates.append(makeElement(
        "li", rate.start + " to " + rate.end + " · " + rate.nights +
        " night(s) at ¥" + rate.amount + " per night"
      ));
    }
    card.append(rates);
    results.append(card);
  }
}

searchForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const submit = searchForm.querySelector("button");
  const query = new URLSearchParams(new FormData(searchForm));
  submit.disabled = true;
  results.setAttribute("aria-busy", "true");
  message.textContent = "";
  try {
    const response = await fetch(searchForm.dataset.endpoint + "?" + query);
    const data = await response.json();
    if (!response.ok) throw new Error(data.message || "Search could not be completed.");
    renderRooms(data.room_types);
    document.querySelector("#results-heading").textContent = "Available room types";
    history.replaceState(null, "", searchForm.action + "?" + query);
  } catch (error) {
    results.replaceChildren();
    message.textContent = error.message || "Search could not be completed.";
  } finally {
    submit.disabled = false;
    results.removeAttribute("aria-busy");
  }
});
