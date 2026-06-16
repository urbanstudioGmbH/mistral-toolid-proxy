# mistral_toolid_proxy — minimal container image.
#
# Build:  docker build -t mistral-toolid-proxy .
# Run:    docker run --rm -p 8081:8081 \
#             -e UPSTREAM=http://host.docker.internal:8001 \
#             mistral-toolid-proxy
#
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY mistral_toolid_proxy.py .

# Run unprivileged.
RUN useradd --system --no-create-home --uid 10001 proxy
USER proxy

# Runtime config — override at `docker run` time (-e / --env-file).
# UPSTREAM must point at wherever your Mistral endpoint actually listens.
# From a container the host is reachable as host.docker.internal (Docker
# Desktop); on plain Linux add `--add-host=host.docker.internal:host-gateway`,
# or just set UPSTREAM to the real address.
ENV HOST=0.0.0.0 \
    PORT=8081 \
    UPSTREAM=http://host.docker.internal:8001

EXPOSE 8081

CMD ["python", "mistral_toolid_proxy.py"]
