FROM python:3.12-slim

ENV TZ=Asia/Seoul \
    PYTHONUNBUFFERED=1 \
    G2B_DATA_DIR=/app/data

WORKDIR /app

RUN pip install --no-cache-dir requests openpyxl

COPY bid_history/ bid_history/
COPY main.py web.py collector.py index.html ./

RUN mkdir -p /app/data

EXPOSE 8931
CMD ["python", "web.py"]
