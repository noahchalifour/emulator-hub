# syntax=docker/dockerfile:1.7
# Build context: the repo root (it needs the generated gRPC stubs).
FROM python:3.12-slim
ARG FAKE_MODE=ok
ARG BOOT_DELAY_S=4
RUN pip install --no-cache-dir "grpcio==1.84.0" "protobuf==7.36.2" "pillow==12.3.0"
COPY src/emulator_hub/_grpc /app/emulator_hub/_grpc
RUN touch /app/emulator_hub/__init__.py
COPY e2e/fake-emulator/server.py /app/server.py
ENV PYTHONPATH=/app PYTHONUNBUFFERED=1 FAKE_MODE=${FAKE_MODE} BOOT_DELAY_S=${BOOT_DELAY_S}
RUN useradd --uid 10010 --create-home emu && mkdir -p /avd && chown emu:emu /avd
USER 10010
EXPOSE 5557 8554
ENTRYPOINT ["python", "/app/server.py"]
