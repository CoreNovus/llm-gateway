# syntax=docker/dockerfile:1.6
# Build context: the llm-gateway/ project root (one level up from deploy/).
#   docker build -f deploy/Dockerfile -t llm-gateway:dev ..
#
# The image runs the FastAPI gateway only. The vLLM engine lives in its
# own container (vllm/vllm-openai); see deploy/docker-compose.yml for
# the full topology.

FROM python:3.11-slim AS runtime

# curl is required by the docker-compose healthcheck.
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install runtime deps first so Docker caches the layer across code edits.
COPY pyproject.toml poetry.lock README.md ./
COPY llm_gateway ./llm_gateway
RUN pip install --no-cache-dir .

# F9: drop privileges. The gateway only needs to read its own code +
# bind to a port > 1024 + open outbound httpx connections — no root
# capabilities required. Combined with cap_drop / no-new-privileges /
# read_only in docker-compose.yml, an RCE in our code does not yield
# root-in-container.
RUN groupadd --system --gid 1001 llm \
    && useradd  --system --uid 1001 --gid llm --shell /usr/sbin/nologin llm \
    && chown -R llm:llm /app
USER llm:llm

# Container listens on 8000; the docker-compose `ports:` mapping
# constrains host-side reachability to 127.0.0.1.
EXPOSE 8000

CMD ["python", "-m", "llm_gateway"]
