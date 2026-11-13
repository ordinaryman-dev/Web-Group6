/* Auth background gallery: manual previous/next only, no autoplay. */
(function () {
  "use strict";

  var slides = Array.prototype.slice.call(document.querySelectorAll(".auth-slide"));
  if (slides.length < 2) {
    return;
  }

  var index = 0;
  var prev = document.querySelector(".auth-prev");
  var next = document.querySelector(".auth-next");

  function render() {
    slides.forEach(function (slide, position) {
      slide.classList.toggle("is-active", position === index);
      slide.classList.toggle("is-before", position < index);
      slide.classList.toggle("is-after", position > index);
    });
  }

  function go(step) {
    index = (index + step + slides.length) % slides.length;
    render();
  }

  if (prev) {
    prev.addEventListener("click", function () { go(-1); });
  }
  if (next) {
    next.addEventListener("click", function () { go(1); });
  }

  document.addEventListener("keydown", function (event) {
    if (event.key === "ArrowLeft") {
      go(-1);
    } else if (event.key === "ArrowRight") {
      go(1);
    }
  });

  render();
})();
