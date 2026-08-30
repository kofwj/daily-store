import os

if os.environ.get("STORE_DAILY_TESTING", "0") == "1":
    raise RuntimeError("STORE_DAILY_TESTING=1 禁止用于生产进程（gunicorn/wsgi）")

from app.web import app

if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5055, debug=False)
