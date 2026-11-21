#!/bin/sh
# Build from the configured PEP 503 index with the hashes from the repository.
set -eu

: "${MIRROR_PYPI:?set MIRROR_PYPI to the Python package mirror}"
python - <<'PY'
import os
from urllib.parse import urlsplit

url = os.environ["MIRROR_PYPI"]
try:
    index = urlsplit(url)
    valid = (
        bool(index.hostname) and index.username is None and index.password is None
        and not index.query and not index.fragment and not any(c.isspace() for c in url)
        and (index.scheme == "https" or
             (index.scheme == "http" and index.hostname in {"localhost", "127.0.0.1", "::1"}))
    )
    index.port  # reject malformed ports before an installer prints its URL
except ValueError:
    valid = False
if not valid:
    raise SystemExit("MIRROR_PYPI needs an HTTPS index URL without credentials, query or fragment")
PY

here=$(dirname "$0")
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
if [ -s /run/secrets/mirror_ca ]; then
    cat /etc/ssl/certs/ca-certificates.crt /run/secrets/mirror_ca > "$work/ca.pem"
fi

# The installer gets one index. Host or image pip/uv settings cannot add another.
installer() {
    if [ -s "$work/ca.pem" ]; then
        set -- env SSL_CERT_FILE="$work/ca.pem" PIP_CERT="$work/ca.pem" "$@"
    fi
    env -i HOME="$HOME" PATH="$PATH" \
        HTTP_PROXY="${HTTP_PROXY:-${http_proxy:-}}" HTTPS_PROXY="${HTTPS_PROXY:-${https_proxy:-}}" \
        NO_PROXY="${NO_PROXY:-${no_proxy:-}}" PIP_CONFIG_FILE=/dev/null PIP_DISABLE_PIP_VERSION_CHECK=1 \
        UV_NO_CONFIG=1 UV_NO_CACHE=1 UV_PYTHON_DOWNLOADS=never UV_LINK_MODE=copy UV_COMPILE_BYTECODE=1 "$@"
}

case "${1:-}" in
    bootstrap)
        installer python -m pip install --index-url "$MIRROR_PYPI" --no-cache-dir --no-input \
            --root-user-action=ignore --require-hashes --only-binary=:all: -r "$here/uv-requirements.txt"
        ;;
    dependencies)
        # Export names, markers and hashes offline so the public artifact URLs in the lock are never fetched.
        installer uv export --offline --frozen --no-dev --no-emit-project --no-header \
            --format requirements.txt > "$work/requirements.txt"
        if ! awk '/^[[:space:]]*(#|$)/ || /^    --hash=sha256:/ {next}
            /^[[:alnum:]][[:alnum:]_.-]*==[[:alnum:]_+.!-]+([[:space:]]|$)/ {next}
            {exit 1}' "$work/requirements.txt"; then
            echo 'mirror build requires pinned index packages in the exported lock' >&2
            exit 2
        fi
        installer uv venv --python "$(command -v python)" .venv
        installer uv pip sync --python .venv/bin/python --default-index "$MIRROR_PYPI" \
            --only-binary=:all: --require-hashes "$work/requirements.txt"
        ;;
    project)
        installer uv venv --python "$(command -v python)" "$work/backend"
        installer uv pip sync --python "$work/backend/bin/python" --default-index "$MIRROR_PYPI" \
            --only-binary=:all: --require-hashes "$here/mirror-build-requirements.txt"
        # The UI lives under /app/web next to the source the normal editable install imports.
        mkdir "$work/dist"
        installer "$work/backend/bin/python" -I - "$work/dist" <<'PYTHON'
import importlib
import sys
import tomllib

with open("pyproject.toml", "rb") as source:
    backend = tomllib.load(source)["build-system"]["build-backend"]
importlib.import_module(backend).build_editable(sys.argv[1])
PYTHON
        installer uv pip install --offline --no-deps --python .venv/bin/python "$work/dist/"*.whl
        ;;
    *)
        echo 'mirror-python.sh needs bootstrap, dependencies or project' >&2
        exit 2
        ;;
esac
