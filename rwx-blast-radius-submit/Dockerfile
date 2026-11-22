FROM python:3.11-slim

WORKDIR /app
COPY scheduler.py selector.py /app/
COPY artifacts/maps.pkl /app/artifacts/maps.pkl

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONHASHSEED=0 \
    DATA_DIR=/data \
    MAPS_PATH=/app/artifacts/maps.pkl \
    SAFETY_LAMBDA=1.5 \
    P_HOT=0.05

USER nobody
ENTRYPOINT ["python3", "/app/selector.py"]
