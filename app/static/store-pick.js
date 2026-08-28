/* 门店选择：关闭只显示触发器；打开后搜索 + 按地市/经理分组。 */
(function () {
  "use strict";

  var openPick = null;
  var activeIdx = -1;

  function $(sel, root) {
    return (root || document).querySelector(sel);
  }
  function $all(sel, root) {
    return Array.prototype.slice.call((root || document).querySelectorAll(sel));
  }

  function norm(s) {
    return String(s || "")
      .toLowerCase()
      .replace(/\s+/g, "")
      .replace(/[市县区]/g, "");
  }

  function matches(hay, raw) {
    var q = String(raw || "").trim().toLowerCase();
    if (!q) return true;
    var h = norm(hay);
    if (h.indexOf(norm(q)) !== -1) return true;
    var parts = q.split(/\s+/).filter(Boolean);
    if (parts.length < 2) return h.indexOf(norm(parts[0] || q)) !== -1;
    return parts.every(function (p) {
      return h.indexOf(norm(p)) !== -1;
    });
  }

  function groupBy(panel) {
    var on = panel.querySelector(".sp-gb.is-on");
    return on ? String(on.getAttribute("data-group") || "city") : "city";
  }

  function groupKey(opt, mode) {
    if (mode === "manager") {
      return (opt.getAttribute("data-manager") || "").trim() || "未设经理";
    }
    return (opt.getAttribute("data-city") || "").trim() || "未分地市";
  }

  function visibleOpts(panel) {
    return $all(".sp-opt", panel).filter(function (el) {
      return !el.hidden && el.offsetParent !== null;
    });
  }

  function setActive(panel, idx) {
    var opts = visibleOpts(panel);
    opts.forEach(function (el) {
      el.classList.remove("is-active");
    });
    if (!opts.length) {
      activeIdx = -1;
      return;
    }
    activeIdx = ((idx % opts.length) + opts.length) % opts.length;
    var el = opts[activeIdx];
    el.classList.add("is-active");
    el.scrollIntoView({ block: "nearest" });
  }

  function paintMeta(opt, mode) {
    var meta = $(".sp-meta", opt);
    if (!meta) return;
    if (mode === "manager") {
      meta.textContent = meta.getAttribute("data-alt-city") || "";
    } else {
      meta.textContent = meta.getAttribute("data-alt-mgr") || "";
    }
  }

  function filterPanel(panel, raw) {
    var mode = groupBy(panel);
    var list = $(".sp-list", panel);
    if (!list) return;
    $all(".sp-city", list).forEach(function (el) {
      el.parentNode.removeChild(el);
    });

    var shown = [];
    $all(".sp-opt", list).forEach(function (opt) {
      var text = (opt.getAttribute("data-text") || "") + " " + (opt.textContent || "");
      var hit = matches(text, raw);
      opt.hidden = !hit;
      if (hit) {
        paintMeta(opt, mode);
        shown.push(opt);
      }
    });

    var lastKey = null;
    shown.forEach(function (opt) {
      if (opt.classList.contains("sp-opt-all")) {
        lastKey = null;
        return;
      }
      var key = groupKey(opt, mode);
      if (key !== lastKey) {
        var head = document.createElement("div");
        head.className = "sp-city";
        head.setAttribute("role", "presentation");
        head.textContent = key;
        list.insertBefore(head, opt);
        lastKey = key;
      }
    });

    var empty = $(".sp-empty", panel);
    if (empty) empty.hidden = shown.length !== 0;
    list.hidden = shown.length === 0;
    setActive(panel, 0);
  }

  function placePanel(trigger, panel) {
    var rect = trigger.getBoundingClientRect();
    var gap = 6;
    var vw = window.innerWidth;
    var vh = window.innerHeight;
    var phone = vw <= 800;
    var CAP = phone ? 360 : 420;
    var width = Math.min(phone ? vw - 16 : 320, vw - 16);
    var left = Math.min(Math.max(8, rect.left), vw - width - 8);
    var below = vh - rect.bottom - 12;
    var above = rect.top - 12;
    var openUp = below < 220 && above > below;
    var room = openUp ? above : below;
    var maxH = Math.min(CAP, Math.max(200, room));
    panel.style.width = width + "px";
    panel.style.left = left + "px";
    panel.style.right = "auto";
    panel.style.bottom = "auto";
    panel.style.maxHeight = maxH + "px";
    panel.hidden = false;
    if (openUp) {
      panel.style.top = Math.round(Math.max(8, rect.top - gap - maxH)) + "px";
    } else {
      panel.style.top = Math.round(rect.bottom + gap) + "px";
    }
  }

  function close() {
    if (!openPick) return;
    var pick = openPick.pick;
    var panel = openPick.panel;
    var trigger = openPick.trigger;
    var home = openPick.home;
    pick.classList.remove("is-open");
    if (trigger) trigger.setAttribute("aria-expanded", "false");
    if (panel) {
      panel.hidden = true;
      panel.classList.remove("is-open");
      if (home && panel.parentNode !== home) home.appendChild(panel);
    }
    document.body.classList.remove("sp-open");
    openPick = null;
    activeIdx = -1;
  }

  function open(pick) {
    close();
    var trigger = $(".sp-trigger", pick);
    var panel = $(".sp-panel", pick);
    var q = $(".sp-q", pick);
    if (!trigger || !panel) return;
    openPick = { pick: pick, panel: panel, trigger: trigger, home: panel.parentNode };
    pick.classList.add("is-open");
    trigger.setAttribute("aria-expanded", "true");
    document.body.appendChild(panel);
    document.body.classList.add("sp-open");
    panel.classList.add("is-open");
    if (q) q.value = "";
    $all(".sp-gb", panel).forEach(function (btn, i) {
      var on = i === 0;
      btn.classList.toggle("is-on", on);
      btn.setAttribute("aria-selected", on ? "true" : "false");
    });
    filterPanel(panel, "");
    placePanel(trigger, panel);
    if (q) {
      requestAnimationFrame(function () {
        q.focus();
        placePanel(trigger, panel);
      });
    }
  }

  function selectOpt(pick, opt) {
    if (!opt || opt.hidden) return;
    var hidden = $('input[type="hidden"]', pick);
    var label = $(".sp-label", pick);
    if (hidden) hidden.value = opt.getAttribute("data-value") || "";
    var nameEl = $(".sp-name", opt);
    if (label) label.textContent = (nameEl ? nameEl.textContent : opt.textContent).trim();
    $all(".sp-opt", pick).forEach(function (el) {
      el.classList.toggle("is-selected", el === opt);
    });
    if (openPick && openPick.panel) {
      $all(".sp-opt", openPick.panel).forEach(function (el) {
        el.classList.toggle("is-selected", el === opt);
      });
    }
    var autosubmit = pick.getAttribute("data-autosubmit") !== "0";
    close();
    if (autosubmit) {
      var form = pick.closest("form");
      if (form) {
        if (form.requestSubmit) form.requestSubmit();
        else {
          var token = form.querySelector('[name="_csrf_token"]');
          if (!token) {
            token = document.createElement("input");
            token.type = "hidden";
            token.name = "_csrf_token";
            token.value = window.__csrfToken || "";
            form.appendChild(token);
          }
          form.submit();
        }
      }
    }
  }

  document.addEventListener("click", function (e) {
    var trigger = e.target.closest(".sp-trigger");
    if (trigger) {
      var pick = trigger.closest(".store-pick");
      if (!pick) return;
      e.preventDefault();
      if (openPick && openPick.pick === pick) close();
      else open(pick);
      return;
    }
    var gb = e.target.closest(".sp-gb");
    if (gb && openPick && openPick.panel.contains(gb)) {
      e.preventDefault();
      $all(".sp-gb", openPick.panel).forEach(function (el) {
        var on = el === gb;
        el.classList.toggle("is-on", on);
        el.setAttribute("aria-selected", on ? "true" : "false");
      });
      var q = $(".sp-q", openPick.panel);
      filterPanel(openPick.panel, q ? q.value : "");
      placePanel(openPick.trigger, openPick.panel);
      return;
    }
    var opt = e.target.closest(".sp-opt");
    if (opt && openPick) {
      e.preventDefault();
      selectOpt(openPick.pick, opt);
      return;
    }
    if (openPick) {
      if (e.target.closest(".sp-panel")) return;
      close();
    }
  });

  document.addEventListener("input", function (e) {
    if (!e.target.classList.contains("sp-q") || !openPick) return;
    filterPanel(openPick.panel, e.target.value);
    placePanel(openPick.trigger, openPick.panel);
  });

  document.addEventListener("keydown", function (e) {
    if (!openPick) return;
    var panel = openPick.panel;
    var opts = visibleOpts(panel);
    if (e.key === "Escape") {
      e.preventDefault();
      close();
      return;
    }
    if (e.key === "ArrowDown") {
      e.preventDefault();
      setActive(panel, activeIdx < 0 ? 0 : activeIdx + 1);
      return;
    }
    if (e.key === "ArrowUp") {
      e.preventDefault();
      setActive(panel, activeIdx < 0 ? opts.length - 1 : activeIdx - 1);
      return;
    }
    if (e.key === "Enter") {
      e.preventDefault();
      if (activeIdx >= 0 && opts[activeIdx]) selectOpt(openPick.pick, opts[activeIdx]);
      else if (opts[0]) selectOpt(openPick.pick, opts[0]);
      return;
    }
    if (e.key === "Tab") {
      close();
    }
  });

  window.addEventListener(
    "resize",
    function () {
      if (openPick) placePanel(openPick.trigger, openPick.panel);
    },
    { passive: true }
  );
  window.addEventListener(
    "scroll",
    function () {
      if (openPick) placePanel(openPick.trigger, openPick.panel);
    },
    true
  );
})();
