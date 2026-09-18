FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    BGT_DATA_DIR=/data

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

RUN mkdir -p /data
VOLUME ["/data"]

EXPOSE 7106

# 容器内直接 uvicorn 跑 0.0.0.0:7106
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "7106"]
