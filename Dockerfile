FROM python:3.12-slim

WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TZ=Asia/Shanghai
RUN ln -snf /usr/share/zoneinfo/Asia/Shanghai /etc/localtime && echo Asia/Shanghai > /etc/timezone

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 时区：让 datetime.now() 用北京时间；zoneinfo 也依赖 tzdata。
# gosu：entrypoint 以 root 校正数据卷权限后降权到 app。
RUN apt-get update && apt-get install -y --no-install-recommends tzdata gosu && \
    rm -rf /var/lib/apt/lists/* && \
    ln -snf /usr/share/zoneinfo/Asia/Shanghai /etc/localtime && echo Asia/Shanghai > /etc/timezone && \
    groupadd --system app && useradd --system --gid app --uid 1000 --home /app --no-create-home app

COPY --chown=app:app app ./app
COPY --chown=app:app wsgi.py docker-entrypoint.sh VERSION ./
RUN chmod 755 /app/docker-entrypoint.sh && mkdir -p /app/data && chown app:app /app/data

EXPOSE 5055
ENTRYPOINT ["./docker-entrypoint.sh"]
CMD ["gunicorn", "--bind", "0.0.0.0:5055", "--workers", "4", "--threads", "4", "--timeout", "60", "wsgi:app"]
