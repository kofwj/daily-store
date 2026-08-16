/* 门店选择：对齐 GitHub Primer SelectPanel
 * 关闭时只显示触发按钮；打开后面板挂到 body，顶部过滤 + 分组列表。
 */
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

  /* 部分匹配：整串包含，或按空格拆词每段都命中 */
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

  function filterPanel(panel, raw) {
    var shown = 0;
    $all(".sp-group", panel).forEach(function (group) {
      var g = 0;
      var city = group.getAttribute("data-city") || "";
      $all(".sp-opt", group).forEach(function (opt) {
        var text =
          (opt.getAttribute("data-text") || "") + " " + city + " " + (opt.textContent || "");
        var hit = matches(text, raw);
        opt.hidden = !hit;
        if (hit) g += 1;
      });
      group.hidden = g === 0;
      shown += g;
    });
    $all(".sp-opt-all", panel).forEach(function (opt) {
      var hit = matches(opt.getAttribute("data-text") || opt.textContent || "", raw);
      opt.hidden = !hit;
      if (hit) shown += 1;
    });
    var empty = $(".sp-empty", panel);
    if (empty) empty.hidden = shown !== 0;
    var list = $(".sp-list", panel);
    if (list) list.hidden = shown === 0;
    setActive(panel, 0);
  }

  function placePanel(trigger, panel) {
    var rect = trigger.getBoundingClientRect();
    var gap = 6;
    var vw = window.innerWidth;
    var vh = window.innerHeight;
    var width = Math.min(vw <= 800 ? vw - 16 : 280, vw - 16);
    var left = Math.min(Math.max(8, rect.left), vw - width - 8);
    panel.style.width = width + "px";
    panel.style.left = left + "px";
    panel.style.right = "auto";
    panel.style.bottom = "auto";
    panel.style.maxHeight = "";
    panel.hidden = false;
    /* 先放到下方测高度 */
    panel.style.top = Math.round(rect.bottom + gap) + "px";
    var ph = panel.getBoundingClientRect().height || 300;
    var below = vh - rect.bottom - 12;
    var above = rect.top - 12;
    if (below < Math.min(ph, 220) && above > below) {
      var maxH = Math.max(200, above);
      panel.style.maxHeight = maxH + "px";
      ph = Math.min(ph, maxH);
      panel.style.top = Math.round(Math.max(8, rect.top - gap - ph)) + "px";
    } else {
      panel.style.maxHeight = Math.max(200, below) + "px";
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
      /* 还回原位 */
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
    /* 挂到 body，避免被卡片 overflow 裁切 */
    document.body.appendChild(panel);
    document.body.classList.add("sp-open");
    panel.classList.add("is-open");
    if (q) {
      q.value = "";
      filterPanel(panel, "");
    } else {
      filterPanel(panel, "");
    }
    placePanel(trigger, panel);
    if (q) {
      /* 下一帧再 focus，避免移动端键盘顶掉定位 */
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
    /* 面板可能已挂到 body，也要更新里面的选中态 */
    if (openPick && openPick.panel) {
      $all(".sp-opt", openPick.panel).forEach(function (el) {
        el.classList.toggle("is-selected", el === opt);
      });
    }
    var autosubmit = pick.getAttribute("data-autosubmit") !== "0";
    close();
    if (autosubmit) {
      var form = pick.closest("form");
      if (form) form.submit();
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
