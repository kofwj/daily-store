/* 复制到剪贴板的共享实现：优先 clipboard API，降级为选区 + execCommand。
 * 支持 textarea/input（取 value）和普通元素（取 innerText）。
 * 提示统一写到页面上 id=copyHint 的元素里。
 * HTTP / 无剪贴板权限时 execCommand 可能返回 false——必须看返回值，
 * 失败就把源文本露出来让人长按，不能提示「已复制」。 */
function setCopyHint(text) {
  const hint = document.getElementById('copyHint');
  if (hint) hint.textContent = text;
}

function revealCopySource(el) {
  if (!el) return;
  el.classList.remove('sr-only');
  el.removeAttribute('hidden');
  el.classList.add('is-reveal');
  try {
    if (typeof el.focus === 'function') el.focus();
    if (typeof el.select === 'function') {
      el.select();
      el.setSelectionRange(0, 99999);
    }
  } catch (e) {}
}

function fallbackCopy(el, doneMsg) {
  if (typeof el.select === 'function') {
    el.focus();
    el.select();
    el.setSelectionRange(0, 99999);
  } else {
    const range = document.createRange();
    range.selectNodeContents(el);
    const sel = window.getSelection();
    sel.removeAllRanges();
    sel.addRange(range);
  }
  var ok = false;
  try {
    ok = !!document.execCommand('copy');
  } catch (e) {
    ok = false;
  }
  if (ok) {
    setCopyHint(doneMsg);
  } else {
    revealCopySource(el);
    setCopyHint('请长按文本框全选复制');
  }
}

function copyText(el, doneMsg) {
  if (!el) return;
  const text = el.value !== undefined ? el.value : (el.innerText || el.textContent || '');
  doneMsg = doneMsg || '已复制，去微信粘贴';
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(text).then(
      () => setCopyHint(doneMsg)
    ).catch(() => fallbackCopy(el, doneMsg));
  } else {
    fallbackCopy(el, doneMsg);
  }
}
