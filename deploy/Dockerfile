# DeltaSleuth: Node builds the UI and uv the Python environment; the runtime stage holds neither and runs
# `ds serve` as uid 10001. Base images are pinned by digest: change one on purpose, with tools/release.py's
# INDEX and BASES. Releases: tools/release.py. From source (unsigned):
#   docker build -f deploy/Dockerfile -t deltasleuth:source .

# The python:3.13-slim index; a release passes its platform's manifest digest instead (release.BASES).
ARG PYTHON=python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90

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
# LICENSE: the installed package's dist-info holds it, so the image carries its licence text.
COPY pyproject.toml uv.lock README.md LICENSE ./
# Dependencies first, so this layer survives code changes. --no-cache: the image keeps no download cache.
RUN uv sync --frozen --no-dev --no-install-project --no-cache
COPY deltasleuth/ deltasleuth/
RUN uv sync --frozen --no-dev --no-cache

FROM ${PYTHON}
ENV PYTHONUNBUFFERED=1 PATH=/app/.venv/bin:$PATH DS_CONFIG=/etc/deltasleuth/deltasleuth.yaml
# The pinned base predates the trixie security update to zlib.
RUN apt-get update \
 && apt-get install -y --no-install-recommends --only-upgrade 'zlib1g=1:1.3.dfsg+really1.3.1-1+deb13u1' \
 && rm -rf /var/lib/apt/lists/* \
 && python -m pip uninstall --yes --root-user-action=ignore pip \
 && groupadd --system --gid 10001 deltasleuth \
 && useradd --system --uid 10001 --gid 10001 --home-dir /app --shell /usr/sbin/nologin deltasleuth \
 && install -d -o deltasleuth -m 0700 /var/lib/deltasleuth/evidence /var/lib/deltasleuth/workspace \
      /var/lib/deltasleuth/p0 /var/lib/deltasleuth/files /var/lib/deltasleuth/spill
WORKDIR /app
COPY --from=venv /app ./
COPY --from=web /src/web/dist web/dist/
# What web/dist bundles, for the SBOM; outside web/dist, so never served.
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
      org.opencontainers.image.vendor=$VENDOR org.opencontainers.image.licenses=Apache-2.0
