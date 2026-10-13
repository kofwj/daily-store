/* 把页面上一块 DOM 截成 PNG，供「复制图片 / 下载图片」用（通报页、触客通报页共用）。
 *
 * 与 app/templates/bulletin.html 里那套逻辑同源：微信/手机内置浏览器几乎不支持
 * 写图片进剪贴板，download 也常被拦，所以那两个环境一律只生成预览，让人长按保存。
 *
 * 用法：页面在加载本脚本前设好 window.__shotSetup：
 *   sourceId   要截图的元素 id（必填）
 *   fileName   下载的文件名（默认 shot.png）
 *   hintId     提示文字写进的元素 id（默认 copyHint）
 *   buttonIds  期间要禁用的按钮 id 数组
 *   previewId  预览图 <img> 的 id（默认 shotPreview）
 *   previewCardId 预览卡片容器 id（默认 shotPreviewCard），会被取消 hidden 并滚进视野
 *   plainCells 可选 {form, text}：截图前把 form 选择器命中的元素藏起来、text 命中的露出来
 *              （把可编辑的 <input> 换成纯数字，拍完自动还原）
 *   fitContent 可选 true：容器比内容窄时（手机上贴宽表），截图前按内容撑宽容器、拍完还原，
 *              免得 html2canvas 只画容器那么大、把表右边裁掉
 * 然后调用 shotCopyImage() / shotDownloadImage()。
 */
(function () {
  function cfg() { return window.__shotSetup || {}; }

  function hint(text) {
    var el = document.getElementById(cfg().hintId || 'copyHint');
    if (el) el.textContent = text;
  }

  function isWeChat() { return /MicroMessenger/i.test(navigator.userAgent || ''); }
  function isMobile() { return /Android|iPhone|iPad|iPod|Mobile/i.test(navigator.userAgent || ''); }

  function setBusy(busy) {
    (cfg().buttonIds || []).forEach(function (id) {
      var btn = document.getElementById(id);
      if (btn) btn.disabled = !!busy;
    });
  }

  function showPreview(url) {
    var card = document.getElementById(cfg().previewCardId || 'shotPreviewCard');
    var img = document.getElementById(cfg().previewId || 'shotPreview');
    if (img) img.src = url;
    if (!card) return;
    card.hidden = false;
    // 手机端看不到页面底部时会以为没反应，滚到预览
    try { card.scrollIntoView({ behavior: 'smooth', block: 'start' }); }
    catch (e) { card.scrollIntoView(true); }
  }

  function renderShot() {
    var el = document.getElementById(cfg().sourceId || '');
    if (!el) return Promise.reject(new Error('找不到要截图的内容'));
    if (typeof html2canvas !== 'function') return Promise.reject(new Error('截图组件未加载'));
    // 手机内存紧，缩放别太高
    var scale = Math.min(isMobile() ? 1.5 : 2, window.devicePixelRatio || 2);
    var undoFit = cfg().fitContent ? fitContent(el) : null;
    function putBack() { if (undoFit) undoFit(); }
    var box = el.getBoundingClientRect();
    return html2canvas(el, {
      backgroundColor: '#ffffff',
      scale: scale,
      useCORS: true,
      logging: false,
      // 避免把页面滚动条裁进画布
      scrollX: 0,
      scrollY: -window.scrollY,
      windowWidth: Math.max(document.documentElement.clientWidth, Math.ceil(box.width)),
    }).then(function (canvas) {
      putBack();
      return canvas;
    }, function (err) {
      putBack();
      throw err;
    });
  }

  // 手机屏比宽表窄：容器 overflow 是 visible，但 html2canvas 只画容器那么大，图右边会被裁。
  // 页面开 fitContent 时按内容撑宽容器、量好再拍，返回还原函数；放得下就不动（通报页没开这个开关）。
  function fitContent(el) {
    var prevWidth = el.style.width;
    var prevMax = el.style.maxWidth;
    if (el.scrollWidth <= el.getBoundingClientRect().width + 1) return null;
    el.style.maxWidth = 'none';
    var w = Math.ceil(el.scrollWidth);
    el.style.width = w + 'px';
    var w2 = Math.ceil(el.scrollWidth);
    if (w2 > w) el.style.width = w2 + 'px';
    return function () {
      el.style.width = prevWidth;
      el.style.maxWidth = prevMax;
    };
  }

  function canvasToBlob(canvas) {
    return new Promise(function (resolve, reject) {
      if (!canvas || !canvas.toBlob) {
        try {
          var data = canvas.toDataURL('image/png');
          var bin = atob(data.split(',')[1] || '');
          var arr = new Uint8Array(bin.length);
          for (var i = 0; i < bin.length; i++) arr[i] = bin.charCodeAt(i);
          resolve(new Blob([arr], { type: 'image/png' }));
        } catch (e) { reject(e); }
        return;
      }
      canvas.toBlob(function (blob) {
        blob ? resolve(blob) : reject(new Error('截图失败'));
      }, 'image/png');
    });
  }

  function downloadBlob(blob) {
    var url = URL.createObjectURL(blob);
    var a = document.createElement('a');
    a.href = url;
    a.download = cfg().fileName || 'shot.png';
    a.rel = 'noopener';
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(function () { URL.revokeObjectURL(url); }, 8000);
  }

  // 可编辑格子（<input>）截图前换成旁边的纯文字：html2canvas 对 <input> 支持不稳，
  // 也不想把输入框外观截进图里。页面用 plainCells: {form, text} 两个选择器开启。
  function swapPlainCells(on) {
    var s = cfg().plainCells || {};
    if (!s.form || !s.text) return;
    document.querySelectorAll(s.form).forEach(function (f) {
      f.style.display = on ? 'none' : '';
    });
    document.querySelectorAll(s.text).forEach(function (t) {
      t.style.display = on ? '' : 'none';
    });
  }

  function makeShot() {
    hint('正在生成图片…');
    swapPlainCells(true);
    function putBack() { swapPlainCells(false); }
    return renderShot().then(function (canvas) {
      return canvasToBlob(canvas).then(function (blob) {
        // 预览用 data URL：部分环境里 blob: 地址会失效导致预览 0×0 不显示
        showPreview(canvas.toDataURL('image/png'));
        return blob;
      });
    }).then(
      function (blob) { putBack(); return blob; },
      function (err) { putBack(); throw err; }
    );
  }

  function fail(prefix, err) {
    hint(prefix + ((err && err.message) ? err.message : '再试'));
  }

  window.shotCopyImage = function () {
    if (window.__shotBusy) return;
    window.__shotBusy = true;
    setBusy(true);
    makeShot().then(function (blob) {
      // 微信/手机内置浏览器几乎不支持写图片到剪贴板
      if (isWeChat() || isMobile()) {
        hint('图片已生成，长按下方预览转发');
        return;
      }
      if (navigator.clipboard && window.ClipboardItem) {
        return navigator.clipboard.write([new ClipboardItem({ 'image/png': blob })]).then(
          function () { hint('图片已复制，去微信粘贴'); },
          function () {
            downloadBlob(blob);
            hint('当前浏览器不支持复制图片，已改为下载');
          }
        );
      }
      downloadBlob(blob);
      hint('当前浏览器不支持复制图片，已改为下载');
    }).catch(function (err) {
      fail('生成失败：', err);
    }).then(function () {
      window.__shotBusy = false;
      setBusy(false);
    });
  };

  window.shotDownloadImage = function () {
    if (window.__shotBusy) return;
    window.__shotBusy = true;
    setBusy(true);
    makeShot().then(function (blob) {
      if (isWeChat()) {
        // 微信里 download 常被拦，预览长按更稳
        hint('微信里请长按下方预览图保存或转发');
        return;
      }
      downloadBlob(blob);
      hint('已下载');
    }).catch(function (err) {
      fail('下载失败：', err);
    }).then(function () {
      window.__shotBusy = false;
      setBusy(false);
    });
  };
})();
