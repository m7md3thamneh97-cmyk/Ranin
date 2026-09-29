FROM python:3.13-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 RANEEN_DATA_DIR=/data PORT=8080
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt \
    && groupadd --gid 10001 raneen \
    && useradd --uid 10001 --gid 10001 --no-create-home raneen
COPY studio ./studio
COPY serve.py docker-entrypoint.py ./
EXPOSE 8080
# Entry point initializes root-owned Railway volume, then drops to uid/gid 10001.
CMD ["python", "docker-entrypoint.py"]
