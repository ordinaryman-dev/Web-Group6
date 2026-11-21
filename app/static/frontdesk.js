/* 员工交互：各初始化函数分别处理时钟、提交保护、抽屉、通知和维修预检；页面不直接改数据库。 */
/* Front desk interactions: live clock, confirmation, submit guard, row links. */
(function () {
  "use strict";

  // 以服务器时间校准显示，再按北京时间刷新，避免客户端时钟偏差影响阅读。
  function initClock() {
    const clock = document.getElementById("desk-clock");
    if (clock) {
      const epoch = Number(clock.dataset.epoch || 0);
      const offset = epoch - Date.now();
      const format = new Intl.DateTimeFormat("en-GB", {
        timeZone: "Asia/Shanghai",
        hour: "2-digit",
        minute: "2-digit",
        second: "2-digit",
        hour12: false
      });
      const tick = function () {
        clock.textContent = format.format(new Date(Date.now() + offset));
      };
      tick();
      window.setInterval(tick, 1000);
    }
  }

  // 已由其他窗口处理的申请停止提交，提示刷新查看结果。
  const markRequestStale = function (card) {
    if (!card || card.classList.contains("is-stale")) {
      return;
    }
    card.classList.add("is-stale");
    card.querySelectorAll("button, input[type='submit']").forEach(function (control) {
      control.disabled = true;
    });
    if (!card.querySelector(".stale-note")) {
      const note = document.createElement("p");
      note.className = "stale-note";
      note.textContent = "Handled in another tab. Refresh to see the result.";
      card.appendChild(note);
    }
  };

  // 提交审批前查询最新申请状态；这是提前提示，数据库中的加锁复查才是最终保障。
  function initRequestCheck() {
    const requestFeedShell = document.querySelector("[data-notify-feed]");
    document.addEventListener("submit", function (event) {
      const form = event.target;
      if (!(form instanceof HTMLFormElement) || !form.dataset.requestCheck ||
          form.dataset.requestChecked === "1") {
        return;
      }
      if (!requestFeedShell) {
        return;
      }
      event.preventDefault();
      const feed = requestFeedShell.dataset.notifyFeed;
      const separator = feed.indexOf("?") === -1 ? "?" : "&";
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
          const stillPending = (data.pending || []).some(function (row) {
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
  }

  // 确认敏感操作并暂时禁用提交按钮，降低连续点击造成的重复请求。
  function initSubmitGuard() {
    document.addEventListener("submit", function (event) {
      const form = event.target;
      if (!(form instanceof HTMLFormElement)) {
        return;
      }
      const submitter = event.submitter;
      const message = submitter && submitter.dataset ? submitter.dataset.confirm : "";
      if (message && !window.confirm(message)) {
        event.preventDefault();
        return;
      }
      if (form.hasAttribute("data-guard")) {
        form.classList.add("is-loading");
        const buttons = form.querySelectorAll("button[type='submit'], input[type='submit']");
        window.setTimeout(function () {
          buttons.forEach(function (button) {
            button.disabled = true;
          });
        }, 0);
      }
    });
  }

  // 表格行支持鼠标和键盘跳转，但不拦截行内链接、按钮或表单。
  function initRowNavigation() {
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
  }

  // 打开抽屉后聚焦关闭入口，并允许用 Escape 返回原工作台。
  function initDrawer() {
    const drawer = document.querySelector(".drawer[data-close-url]");
    if (drawer) {
      const drawerClose = drawer.querySelector(".drawer-close");
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
  }

  // 定时读取通知；窗口不可见时暂停，网络失败时保留服务器首次渲染的内容。
  function initNotifications() {
    // Live bell: poll the notification feed so a new guest request shows up without
    // a page reload. Everything falls back to the server-rendered markup when the
    // fetch fails, so the desk still works offline or with JS blocked.
    const shell = document.querySelector("[data-notify-feed]");
    if (shell) {
      const feed = shell.dataset.notifyFeed;
      let version = shell.dataset.notifyVersion || "";
      const every = Number(shell.dataset.notifyInterval || 30000);

      const flash = function (element) {
        element.classList.remove("is-new");
        void element.offsetWidth;
        element.classList.add("is-new");
        window.setTimeout(function () {
          element.classList.remove("is-new");
        }, 4000);
      };

      const paint = function (data, arriving) {
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
          const key = element.dataset.notifyCount;
          if (Object.prototype.hasOwnProperty.call(data.counts, key)) {
            element.textContent = data.counts[key];
          }
        });
        // A task card whose request is no longer pending was handled in another tab.
        const waiting = {};
        (data.pending || []).forEach(function (row) {
          waiting[String(row.id)] = true;
        });
        document.querySelectorAll("[data-request-id]").forEach(function (card) {
          if (waiting[card.dataset.requestId] || card.classList.contains("is-stale")) {
            return;
          }
          markRequestStale(card);
        });

        const list = document.querySelector("[data-notify-items]");
        if (list) {
          list.innerHTML = "";
          if (!data.items.length) {
            const empty = document.createElement("p");
            empty.className = "bell-empty";
            empty.textContent = "All clear. Nothing needs attention.";
            list.appendChild(empty);
          }
          data.items.forEach(function (item) {
            const link = document.createElement("a");
            link.className = "bell-row tone-" + item.tone;
            link.href = item.href;
            const title = document.createElement("b");
            title.textContent = item.title;
            const note = document.createElement("small");
            note.textContent = item.text;
            link.appendChild(title);
            link.appendChild(note);
            list.appendChild(link);
          });
        }
      };

      const poll = function () {
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
            const arriving = data.counts.requests > 0;
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
  }

  // 默认日期也立即检查冲突；最终保存仍由后端重新检查，预检结果不能当作锁房。
  function initMaintenancePreview() {
    // Check a maintenance block for overlapping bookings before it is submitted.
    const blockForm = document.querySelector("form[data-block-preview]");
    if (blockForm) {
      const blockOut = blockForm.querySelector("[data-block-preview-out]");
      const blockRoom = blockForm.dataset.blockRoom;
      const blockStart = blockForm.querySelector('input[name="block-start_date"]');
      const blockEnd = blockForm.querySelector('input[name="block-end_date"]');
      let blockTimer = null;

      const clearBlockPreview = function (message) {
        blockOut.textContent = message;
        blockOut.classList.remove("bad");
      };

      const previewBlock = function () {
        if (!blockStart.value || !blockEnd.value) {
          clearBlockPreview("Choose a date range to check for bookings.");
          return;
        }
        const query = new URLSearchParams({
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
            const references = data.bookings.map(function (booking) {
              return booking.reference;
            });
            clearBlockPreview(
              data.count + " booking" + (data.count === 1 ? "" : "s") +
              " conflict: " + references.join(", ") +
              ". Bookings stay unchanged. Review each conflict; in-house guests need manager coordination."
            );
            blockOut.classList.add("bad");
          })
          .catch(function () {
            clearBlockPreview("Could not check conflicts. The server checks again on save.");
          });
      };

      const scheduleBlockPreview = function () {
        window.clearTimeout(blockTimer);
        blockTimer = window.setTimeout(previewBlock, 150);
      };
      [blockStart, blockEnd].forEach(function (input) {
        if (input) {
          input.addEventListener("change", scheduleBlockPreview);
          input.addEventListener("input", scheduleBlockPreview);
        }
      });
      previewBlock();
    }
  }

  initClock();
  initRequestCheck();
  initSubmitGuard();
  initRowNavigation();
  initDrawer();
  initNotifications();
  initMaintenancePreview();
})();
