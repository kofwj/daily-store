"""装配层：Flask app 配置、before_request 钩子、上下文、路由模块注册。

路由按模块拆分：
- helpers     共享函数/装饰器（无路由）
- views_auth  login/logout/me/home/health/admin
- views_daily today / deal / deal_delete / deal_records
- views_report report / report.csv / report/delete / report/cell
- views_admin board / bulletin / bulletin.csv / incentive / edits
- views_settings settings

用 register_*(app) 而非 Blueprint：路由名保持原样（today/deal/report…），
模板里的 url_for('today')、request.endpoint=='today' 都不用改。
"""

from __future__ import annotations

import os
import secrets
import sys
from datetime import datetime, timedelta

from flask import Flask, session
from werkzeug.middleware.proxy_fix import ProxyFix

from . import db
from .errors import register_errors
from .helpers import csrf_protect, load_user, pin_change_required
from .views_admin import register_admin
from .views_advance import register_advance
from .views_auth import register_auth
from .views_daily import register_daily
from .views_report import register_report
from .views_settings import register_settings

INSECURE_SECRETS = frozenset({"", "store-daily-dev-change-me", "replace-with-random-secret"})


def create_app(*, testing: bool = False) -> Flask:
    """Create the application without ever supplying a production secret fallback.

    Tests may opt in with ``testing=True``; deployments must provide a private,
    non-placeholder STORE_DAILY_SECRET before this function is called.
    """
    app = Flask(__name__)
    secret = (os.environ.get("STORE_DAILY_SECRET") or "").strip()
    if not testing and secret in INSECURE_SECRETS:
        raise RuntimeError("必须设置随机的 STORE_DAILY_SECRET；拒绝使用缺失、示例或默认密钥")
    if testing and secret in INSECURE_SECRETS:
        secret = secrets.token_urlsafe(32)
    app.config["SECRET_KEY"] = secret
    app.config["TESTING"] = testing
    app.config["TEMPLATES_AUTO_RELOAD"] = True
    # 公网 / Cloudflare 后必须开 Secure，否则口令 Cookie 会明文落到 HTTP
    secure = os.environ.get("STORE_DAILY_SECURE", "0") == "1"
    app.config["SESSION_COOKIE_HTTPONLY"] = True
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
    app.config["SESSION_COOKIE_SECURE"] = secure
    app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(hours=24)
    app.config["SESSION_REFRESH_EACH_REQUEST"] = True
    app.config["PREFERRED_URL_SCHEME"] = "https" if secure else "http"
    app.config["MAX_CONTENT_LENGTH"] = 32 * 1024 * 1024
    # 只有反向代理网络已被部署方明确设为可信时才接受 X-Forwarded-*。
    # Caddy / Cloudflare Tunnel 可设置 STORE_DAILY_TRUST_PROXY=1。
    if os.environ.get("STORE_DAILY_TRUST_PROXY", "0") == "1":
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
    db.init_db()

    app.before_request(load_user)
    app.before_request(csrf_protect)
    app.before_request(pin_change_required)

    @app.context_processor
    def inject_now():
        # 首次访问就为会话生成 CSRF token，供表单使用
        if "_csrf_token" not in session:
            session["_csrf_token"] = secrets.token_hex(16)
        return {
            "today_iso": db.today_local().isoformat(),
            "now": datetime.now(db.TZ),
            "csrf_token": session.get("_csrf_token", ""),
        }

    register_auth(app)
    register_daily(app)
    register_advance(app)
    register_report(app)
    register_admin(app)
    register_settings(app)
    register_errors(app)
    return app


# Flask/WSGI imports this symbol, while pytest imports modules before fixtures run.
# The test-only branch still creates a fresh random key and is never a deployment fallback.
app = create_app(testing="pytest" in sys.modules)
