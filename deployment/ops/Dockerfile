FROM sha256:539b043a760f92a1685f8f3a8ce5836e5314f7ccb433875b5c19cc940473ce76
COPY wheels/ /opt/wheels/
RUN python -m pip install --no-index --find-links=/opt/wheels adaptive-ai-team==1.1.1 fastapi uvicorn httpx cryptography opentelemetry-sdk opentelemetry-exporter-otlp-proto-http && rm -rf /opt/wheels
COPY ops/ /opt/deployment/
RUN mkdir -p /opt/docker-empty
WORKDIR /opt/deployment
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 AI_TEAM_KEY_FILE=/credentials/keyring.json AI_TEAM_WATCHDOG_DB=/state/watchdog.sqlite
CMD ["python", "deployment.py", "serve"]
