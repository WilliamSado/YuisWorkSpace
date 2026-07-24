FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
COPY config.example.toml ./config.toml
ENV CI_CONFIG=/app/config.toml CI_DEVICES_CONFIG=/devices/devices.toml CI_DATA_DIR=/data
VOLUME ["/data"]
EXPOSE 8080
CMD ["python", "-m", "app"]
