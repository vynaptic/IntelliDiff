# DeltaSleuth: Node builds the UI and uv the Python environment; the runtime stage holds neither and runs
# `ds serve` as uid 10001. Base images are pinned by digest (docs/mvp/RELEASE.md §5): change one on
# purpose, with tools/release.py's INDEX and BASES. Releases: tools/release.py. From source (unsigned):
#   docker build -f deploy/Dockerfile -t deltasleuth:source .

# The python:3.13-slim index; a release passes its platform's manifest digest instead (release.BASES).
ARG PYTHON=python:3.13-slim@sha256:7c61056e61ac89e852de05f3dc6fa51a6dd2181797bceed46aa725dd7cb2cd3b

FROM --platform=$BUILDPLATFORM node:24-alpine@sha256:ebfe2f90462722a7a4de65e91990e97fe0d401c70e0e762c5b53302f905ec1c1 AS web
WORKDIR /src/web
COPY web/package.json web/package-lock.json ./
RUN npm ci
COPY web/ ./
RUN npm run build

FROM ${PYTHON} AS venv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
COPY deploy/uv-requirements.txt /tmp/
RUN pip install --no-cache-dir --root-user-action=ignore --require-hashes -r /tmp/uv-requirements.txt
WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
# Dependencies first, so this layer survives code changes. --no-cache: the image keeps no download cache.
RUN uv sync --frozen --no-dev --no-install-project --no-cache
COPY deltasleuth/ deltasleuth/
RUN uv sync --frozen --no-dev --no-cache

FROM ${PYTHON}
ENV PYTHONUNBUFFERED=1 PATH=/app/.venv/bin:$PATH DS_CONFIG=/etc/deltasleuth/deltasleuth.yaml
RUN python -m pip uninstall --yes --root-user-action=ignore pip \
 && groupadd --system --gid 10001 deltasleuth \
 && useradd --system --uid 10001 --gid 10001 --home-dir /app --shell /usr/sbin/nologin deltasleuth \
 && install -d -o deltasleuth -m 0700 /var/lib/deltasleuth/evidence /var/lib/deltasleuth/workspace \
      /var/lib/deltasleuth/p0 /var/lib/deltasleuth/files /var/lib/deltasleuth/spill
WORKDIR /app
COPY --from=venv /app ./
COPY --from=web /src/web/dist web/dist/
# What web/dist bundles, for the SBOM (RELEASE.md §7); outside web/dist, so never served.
COPY web/package-lock.json web/
USER 10001:10001
EXPOSE 8000
# Direct, as the app calls: a proxy that Docker injects into the environment must not see this check.
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
  CMD ["python", "-c", "import urllib.request as u; u.build_opener(u.ProxyHandler({})).open('http://127.0.0.1:8000/v1/health', timeout=4)"]
CMD ["ds", "serve", "--host", "0.0.0.0", "--port", "8000"]
# Last, so that a new version rebuilds no layer: tools/release.py sets them, a source build keeps these.
ARG VERSION=source REVISION=unknown CREATED=unknown VENDOR=unknown
LABEL org.opencontainers.image.title=DeltaSleuth org.opencontainers.image.version=$VERSION \
      org.opencontainers.image.revision=$REVISION org.opencontainers.image.created=$CREATED \
      org.opencontainers.image.vendor=$VENDOR org.opencontainers.image.licenses=LicenseRef-Proprietary
