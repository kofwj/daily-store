"""集中错误处理：好看的简体中文错误页 + 异常落日志。

没有自定义 500 处理器时，Flask 会把未捕获异常抛给 gunicorn，
用户看到的是光秃秃的 "Internal Server Error"，排查全靠 docker logs。
这里补上：
- 500：记录完整 traceback（带请求上下文），渲染统一错误页
- 400/403/404/405：个性化的错误页（继承 base.html 外壳）
注：TESTING 模式下 Flask 会继续往外抛异常（不改默认行为），测试不受影响。
"""

from __future__ import annotations

import logging
import time

from flask import current_app, render_template, request

_COPY = {
    400: ("请求有误", "你这次请求好像有些问题，麻烦回到上一步再试。"),
    413: ("请求太大", "这次提交的内容超出限制，缩小后再试。"),
    403: ("没有权限", "这个操作需要管理员权限，你可能没有登录或被改了权限。"),
    404: ("页面不存在", "你要找的页面已经搬家了，从导航里重新进吧。"),
    405: ("方法不支持", "这是个只读页面，不能这样提交。"),
    500: ("服务器开小差了", "出了点内部错误，数据不会丢。请稍等几秒刷新重试，或联系管理员。"),
    503: ("服务暂时不可用", "服务器正在忙，稍等片刻刷新试试。"),
}


def _render(code: int) -> str:
    title, message = _COPY.get(code, ("出错了", "出了点问题，请回到上一步。"))
    return render_template(
        "errors/error.html", code=code, title=title, message=message
    )


def register_errors(app) -> None:
    @app.errorhandler(500)
    def handle_500(exc):
        # 关键：自定义处理器接管后 Flask 不再自动打日志，必须自己记。
        started = getattr(request, "_start_time", None)
        elapsed = ""
        if started is not None:
            elapsed = f" ({time.monotonic() - started:.2f}s)"
        current_app.logger.error(
            "未捕获异常 %s %s%s (%s): %s",
            request.method,
            request.url,
            elapsed,
            request.remote_addr,
            request.path,
            exc_info=exc,
        )
        return _render(500), 500

    @app.errorhandler(404)
    def handle_404(_e):
        return _render(404), 404

    @app.errorhandler(403)
    def handle_403(_e):
        return _render(403), 403

    @app.errorhandler(400)
    def handle_400(_e):
        return _render(400), 400

    @app.errorhandler(405)
    def handle_405(_e):
        return _render(405), 405

    @app.errorhandler(413)
    def handle_413(_e):
        return _render(413), 413

    # 兜底：给应用日志配一个稳定的 stderr handler，方便 gunicorn/docker logs 里看到
    logger = logging.getLogger("app")
    logger.setLevel(logging.INFO)
    if not any(
        isinstance(h, logging.StreamHandler) and getattr(h, "level", 0) <= logging.INFO
        for h in logger.handlers
    ):
        handler = logging.StreamHandler()
        handler.setLevel(logging.INFO)
        handler.setFormatter(
            logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
        )
        logger.addHandler(handler)
