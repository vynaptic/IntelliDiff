#!/bin/sh
# Run from the checkout root. "trial" sets up the loopback installation; other arguments go to Compose.
set -eu
fail() { printf 'error: %s\n' "$1" >&2; exit 2; }
[ -f deploy/docker-compose.mirror.yml ] || fail 'run this command from the IntelliDiff checkout'
[ -r .env ] || fail 'copy deploy/env.mirror.example to .env and fill in its five mirror settings'
[ "$#" -gt 0 ] || fail 'use sh deploy/mirror.sh trial, or give a docker compose command'

# Compose gives the shell precedence over .env. Use the sources from the validated .env file.
unset MIRROR_DOCKER MIRROR_PYPI MIRROR_NPM MIRROR_DEBIAN MIRROR_DEBIAN_SECURITY MIRROR_POSTGRES_TAG
unset MIRROR_CA_FILE MIRROR_NETRC_FILE MIRROR_NPMRC_FILE MIRROR_APT_AUTH_FILE
unset COMPOSE_FILE COMPOSE_PROJECT_NAME COMPOSE_ENV_FILES COMPOSE_DISABLE_ENV_FILE COMPOSE_PATH_SEPARATOR
# Compose accepts several dotenv spellings. Read only unambiguous, single-line assignments.
awk '
    /^[[:space:]]*(#|$)/ {next}
    !/^[A-Za-z_][A-Za-z0-9_]*=/ {exit 1}
    {key = substr($0, 1, index($0, "=") - 1); if (seen[key]++) exit 1}
    key ~ /^COMPOSE_(ENV_FILES|DISABLE_ENV_FILE|PATH_SEPARATOR)$/ {exit 1}
' .env || fail 'use one plain NAME=value line per setting in .env and keep its Compose file selection'
files=$(sed -n 's/^COMPOSE_FILE=//p' .env)
case $files in
    deploy/docker-compose.yml:deploy/docker-compose.site.yml:deploy/docker-compose.source.yml:deploy/docker-compose.mirror.yml) ;;
    *) fail '.env must select the Compose files listed in deploy/env.mirror.example' ;;
esac
registry=$(sed -n 's/^MIRROR_DOCKER=//p' .env)
# An unqualified repository (such as "mirror/library") would resolve against Docker Hub.
case $registry in ''|*[!a-z0-9./:_-]*|*//*|*/|/*) fail 'MIRROR_DOCKER needs a literal lowercase registry host and optional repository path' ;; esac
host=${registry%%/*}
case $host in docker.io|*.docker.io|docker.io:*|*.docker.io:*) fail 'MIRROR_DOCKER must name a mirror registry' ;; esac
case $host in *.*|*:*|localhost) ;; *) fail 'qualify the MIRROR_DOCKER registry with a domain or an explicit port' ;; esac
# Build arguments are visible in build records. Refuse credential-bearing URLs before calling Docker.
for key in MIRROR_PYPI MIRROR_NPM MIRROR_DEBIAN MIRROR_DEBIAN_SECURITY; do
    url=$(sed -n "s/^$key=//p" .env)
    case $url in
        https://?*|http://127.0.0.1:*|http://localhost:*|http://\[::1\]:*) ;;
        *) fail "$key needs a literal HTTPS mirror URL in .env" ;;
    esac
    case $url in *'@'*|*'?'*|*'#'*|*'$'*|*'\'*|*[[:space:]]*) fail "$key must be credential-free, without a query or fragment" ;; esac
done

# The site file is created by the trial, so validate the required mirror arguments without it first.
docker compose --env-file .env -f deploy/docker-compose.yml -f deploy/docker-compose.source.yml \
    -f deploy/docker-compose.mirror.yml config --quiet
if [ "$1" = trial ]; then
    shift
    exec sh deploy/up.sh "$@"
fi
exec docker compose "$@"
