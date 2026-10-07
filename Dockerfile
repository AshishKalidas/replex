# syntax=docker/dockerfile:1.7
#
# Stages
#   console  - builds the React console (and runs its vitest suite + type check)
#   dev      - Python + dev extras + source: used by `docker compose run demo`
#   test     - lint, mypy --strict, the full test suite with the 90% coverage gate, the example
#              suite and the secret scan; `docker build --target test .` fails if any gate fails
#   runtime  - slim image with the CLI and the console (default target)
#
# Python version is a build argument so the CI matrix can run in containers:
#   docker build --target test --build-arg PYTHON_VERSION=3.10 .

ARG PYTHON_VERSION=3.12

# ---------------------------------------------------------------- console
FROM node:22-alpine AS console
WORKDIR /ui
COPY console-ui/package.json console-ui/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY console-ui/ ./
RUN npm test && npm run typecheck && REPLEX_CONSOLE_OUT=/ui/dist npm run build

# ---------------------------------------------------------------- python base
FROM python:${PYTHON_VERSION}-slim AS base
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    REPLEX_GIT_SHA=""
WORKDIR /app

# ---------------------------------------------------------------- dev
FROM base AS dev
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
COPY --from=console /ui/dist ./src/replex/console/static
RUN pip install -e ".[dev]"
COPY . .
COPY --from=console /ui/dist ./src/replex/console/static

# ---------------------------------------------------------------- test
FROM dev AS test
RUN ruff check src tests examples \
 && ruff format --check src tests examples \
 && mypy \
 && coverage run -m pytest -q -p no:cacheprovider \
 && coverage report --fail-under=90 \
 && cd examples/support_agent && pytest -q -p no:cacheprovider && cd /app \
 && replex redact --check examples/support_agent/cassettes \
 && python examples/support_agent/demo.py \
 && echo "all gates passed on Python $(python -V)"

# ---------------------------------------------------------------- runtime
FROM base AS runtime
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
COPY --from=console /ui/dist ./src/replex/console/static
RUN pip install ".[console]" \
 && rm -rf src pyproject.toml README.md LICENSE \
 && useradd --create-home --uid 10001 replex
COPY --chown=replex examples/support_agent/cassettes ./examples/support_agent/cassettes
COPY --chown=replex examples/support_agent/replex.policy.yaml ./examples/support_agent/
COPY --chown=replex examples/support_agent/schemas ./examples/support_agent/schemas
USER replex
EXPOSE 8765
HEALTHCHECK --interval=30s --timeout=3s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8765/api/health')" || exit 1
ENTRYPOINT ["replex"]
# Inside a container the console must listen on 0.0.0.0; compose publishes it on the host's
# loopback only (127.0.0.1:8765), so it is still not reachable from the network.
CMD ["console", "examples/support_agent/cassettes", \
     "--candidates", "examples/support_agent/.replex/candidates", \
     "--policy", "examples/support_agent/replex.policy.yaml", \
     "--host", "0.0.0.0", "--no-open"]
