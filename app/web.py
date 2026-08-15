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
from datetime import datetime

from flask import Flask, session
from werkzeug.middleware.proxy_fix import ProxyFix

from . import db
from .errors import register_errors
from .helpers import csrf_protect, load_user
from .views_admin import register_admin
from .views_auth import register_auth
from .views_daily import register_daily
from .views_report import register_report
from .views_settings import register_settings

SECRET_FALLBACK = "store-daily-dev-change-me"


def create_app() -> Flask:
    app = Flask(__name__)
    secret = os.environ.get("STORE_DAILY_SECRET", SECRET_FALLBACK)
    app.config["SECRET_KEY"] = secret
    app.config["TEMPLATES_AUTO_RELOAD"] = True
    # 公网 / Cloudflare 后必须开 Secure，否则口令 Cookie 会明文落到 HTTP
    secure = os.environ.get("STORE_DAILY_SECURE", "0") == "1"
    app.config["SESSION_COOKIE_HTTPONLY"] = True
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
    app.config["SESSION_COOKIE_SECURE"] = secure
    app.config["PREFERRED_URL_SCHEME"] = "https" if secure else "http"
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
    if secret == SECRET_FALLBACK and secure:
        raise RuntimeError("生产环境必须设置 STORE_DAILY_SECRET，不要用默认值")
    db.init_db()

    app.before_request(load_user)
    app.before_request(csrf_protect)

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
    register_report(app)
    register_admin(app)
    register_settings(app)
    register_errors(app)
    return app


app = create_app()
