FROM python:3.12-slim

WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TZ=Asia/Shanghai
RUN ln -snf /usr/share/zoneinfo/Asia/Shanghai /etc/localtime && echo Asia/Shanghai > /etc/timezone

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 时区：让 datetime.now() 用北京时间；zoneinfo 也依赖 tzdata
RUN apt-get update && apt-get install -y --no-install-recommends tzdata && \
    rm -rf /var/lib/apt/lists/* && \
    ln -snf /usr/share/zoneinfo/Asia/Shanghai /etc/localtime && echo Asia/Shanghai > /etc/timezone

COPY app ./app
COPY wsgi.py .

EXPOSE 5055
CMD ["gunicorn", "--bind", "0.0.0.0:5055", "--workers", "2", "--threads", "4", "--timeout", "60", "wsgi:app"]
