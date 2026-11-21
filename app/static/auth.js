/* 登录背景交互：按钮或方向键手动切图，没有自动轮播；不处理身份认证。 */
/* Auth background gallery: manual previous/next only, no autoplay. */
(function () {
  "use strict";

  const slides = Array.prototype.slice.call(document.querySelectorAll(".auth-slide"));
  if (slides.length < 2) {
    return;
  }

  let index = 0;
  const prev = document.querySelector(".auth-prev");
  const next = document.querySelector(".auth-next");

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
