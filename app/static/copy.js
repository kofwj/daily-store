/* 复制到剪贴板的共享实现：优先 clipboard API，降级为选区 + execCommand。
 * 支持 textarea/input（取 value）和普通元素（取 innerText）。
 * 提示统一写到页面上 id=copyHint 的元素里。 */
function setCopyHint(text) {
  const hint = document.getElementById('copyHint');
  if (hint) hint.textContent = text;
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
  try {
    document.execCommand('copy');
    setCopyHint(doneMsg);
  } catch (e) {
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
