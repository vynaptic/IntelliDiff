#!/bin/sh
# deploy/up.sh: from a fresh clone to a running one-machine trial of DeltaSleuth, with the mock services
# and a first API token saved to a file. Run it from the checkout's folder:
#
#   sh deploy/up.sh [--project NAME] [--port N] [--subnet A.B.C.0/24]
#
# It creates what is missing and changes, overwrites and deletes no file: the settings (deploy/config),
# the site file and .env, four secrets (deploy/secrets), an unsigned image built from this checkout, the
# database, the containers and the token (.deltasleuth/tokens/first-run, never printed).
# It runs no "docker pull". The build and the first start fetch what this machine does not hold: the
# images python:3.13-slim and node:24-alpine (pinned by digest in deploy/Dockerfile) and
# postgres:16-alpine, and the Python and npm packages. Without that network access, load or mirror them
# first.
# The options are for a second installation on this machine: its own project name, port and subnet. For
# a first one whose port 8000 is taken, --port N alone is enough.
# This trial, and an office deployment set up by hand: docs/deployment.md.

# No secret and no token reaches the terminal, a variable, a command line or the environment: each goes
# from the command that makes it straight into its file.

set -eu
LC_ALL=C
export LC_ALL

OPTIONS='(--project NAME, --port N, --subnet A.B.C.0/24)'
UNCHANGED='this script changes no existing file (edit it yourself, or give the option that matches it)'
AGAIN='remove it yourself, then run this script again'
MOVE='choose another with --port N (for a first installation on this machine that option alone is enough)'
SETTINGS=deploy/config/deltasleuth.yaml
SITE=deploy/docker-compose.site.yml
TOKEN=.deltasleuth/tokens/first-run
SUBNET=172.30.0.0/24 # the base file's
nl='
'

die() {
    printf 'error: %s\n' "$1" >&2
    exit 2
}

# Whether $1 is what Compose takes as a project and Docker as an image name.
named() {
    case $1 in '' | *[!a-z0-9_-]*) return 1 ;; esac
    printf '%s\n' "$1" | grep -Eqx '[a-z0-9]+((_|__|-+)[a-z0-9]+)*'
}

# .env's one line that holds $1, which must start "$1="; nothing without one. Comment lines are skipped.
dotenv() {
    held=$(grep -v '^#' .env | grep -e "$1" || true)
    case $held in
        '') ;;
        *"$nl"*) return 1 ;;
        "$1="*) printf '%s\n' "$held" ;;
        *) return 1 ;;
    esac
}

# Whether Compose's rendering $1 holds the whole line $2, leading blanks apart.
holds() {
    printf '%s\n' "$1" | sed 's/^[[:blank:]]*//' | grep -Fxq -e "$2"
}

# Whether the Compose version $1 is older than 2.24.4, or is no version at all.
outdated() {
    major=${1#v}
    case $major in [0-9]*.[0-9]*.[0-9]*) ;; *) return 0 ;; esac
    rest=${major#*.}
    patch=${rest#*.}
    major=${major%%.*} minor=${rest%%.*} patch=${patch%%[!0-9]*}
    case $major.$minor.$patch in *[!0-9.]* | .* | *..* | *.) return 0 ;; esac
    [ "$major" -lt 2 ] || { [ "$major" -eq 2 ] && [ "$minor" -lt 24 ]; } ||
        { [ "$major" -eq 2 ] && [ "$minor" -eq 24 ] && [ "$patch" -lt 4 ]; }
}

kept() {
    printf 'kept     %s\n' "$1"
}

# The file in progress, $new, is complete: it becomes $1.
made() {
    mv "$new" "$1"
    new=
    printf 'created  %s\n' "$1"
}

# -- options ---------------------------------------------------------------------------------------

for arg; do
    case $arg in
        -h | --help)
            sed -n '2,/^$/{/^$/d;s/^# \{0,1\}//;p;}' "$0"
            exit 0
            ;;
    esac
done
project='' port='' subnet=''
while [ $# -gt 0 ]; do
    case $1 in
        --project | --port | --subnet)
            [ -n "${2:-}" ] || die "$1 needs a value $OPTIONS"
            case $1 in
                --project) project=$2 ;;
                --port) port=$2 ;;
                *) subnet=$2 ;;
            esac
            shift 2
            ;;
        *) die "unknown option $1 $OPTIONS" ;;
    esac
done
[ -z "$project" ] || named "$project" ||
    die '--project: a project name is lower-case letters and digits, joined by -, _ or __ (for example ds2)'
case $port in
    '') ;;
    *[!0-9]* | 0* | ??????*) die '--port: a number from 1024 to 65535' ;;
    *) if [ "$port" -lt 1024 ] || [ "$port" -gt 65535 ]; then die '--port: a number from 1024 to 65535'; fi ;;
esac
case $subnet in
    '') ;;
    *[!0-9./]*) die '--subnet: a.b.c.0/24, for example 172.31.7.0/24' ;;
    *)
        printf '%s\n' "$subnet" | grep -Eqx '((25[0-5]|2[0-4][0-9]|1[0-9][0-9]|[1-9]?[0-9])\.){3}0/24' ||
            die '--subnet: a.b.c.0/24, for example 172.31.7.0/24'
        ;;
esac

# -- the folder, the shell and Docker --------------------------------------------------------------

[ -f deploy/docker-compose.yml ] ||
    die 'deploy/docker-compose.yml is not in this folder: cd to the DeltaSleuth checkout, then sh deploy/up.sh'
[ -z "${COMPOSE_FILE+set}${COMPOSE_PROJECT_NAME+set}" ] ||
    die 'unset COMPOSE_FILE and COMPOSE_PROJECT_NAME in this shell: they override .env'
for tool in docker openssl; do
    command -v "$tool" > /dev/null 2>&1 ||
        die "$tool is not installed"
done
compose=$(docker compose version --short 2> /dev/null) || die 'docker compose (version 2) is not installed'
docker info > /dev/null 2>&1 || die 'Docker is not running, or this account may not use it'

# -- what an earlier run left, and links to nothing ------------------------------------------------

# A link to a file that is not there exists too: refused here, since "-e" below does not see it.
for file in .env "$SITE" "$SETTINGS" deploy/config/targets.yaml deploy/config/llm_profiles.yaml \
    deploy/secrets/db_password deploy/secrets/db_dsn deploy/secrets/kek deploy/secrets/tokeniser "$TOKEN"; do
    if [ -e "$file.new" ] || [ -L "$file.new" ]; then die "$file.new is left by an interrupted run: $AGAIN"; fi
    [ ! -L "$file" ] || [ -e "$file" ] ||
        die "$file is a link to a file that is not there: put that file back, or remove the link yourself; this script changes no existing file"
done
for file in deploy/secrets/db_password deploy/secrets/db_dsn deploy/secrets/kek deploy/secrets/tokeniser \
    "$TOKEN"; do
    [ ! -e "$file" ] || [ -s "$file" ] || die "$file is empty: $AGAIN"
done

# -- the project: .env's, else --project -----------------------------------------------------------

if [ -e .env ]; then
    files=$(dotenv COMPOSE_FILE) || files=
    files=${files#COMPOSE_FILE=}
    case $files in *[!A-Za-z0-9._/:-]*) files= ;; esac
    case :$files: in
        *:deploy/docker-compose.source.yml:*) ;;
        *) die '.env has no line COMPOSE_FILE=… that names deploy/docker-compose.source.yml (once, unquoted): this script runs an unsigned build of this checkout for a trial; a loaded release is run as docs/deployment.md says' ;;
    esac
    name=$(dotenv COMPOSE_PROJECT_NAME) || name='?'
    name=${name:-COMPOSE_PROJECT_NAME=deltasleuth}
    name=${name#COMPOSE_PROJECT_NAME=}
    named "$name" ||
        die ".env's COMPOSE_PROJECT_NAME (read unquoted and once): a project name is lower-case letters and digits, joined by -, _ or __ (for example ds2)"
    [ -z "$project" ] || [ "$project" = "$name" ] ||
        die ".env makes this folder project \"$name\", not \"$project\": this script changes no existing file (leave --project out, or edit .env yourself)"
    project=$name
else
    release=$(sed -n 's/^  image: \(deltasleuth:[^[:blank:]]*\).*/\1/p' deploy/docker-compose.yml)
    if [ -n "$release" ] && docker image inspect "$release" > /dev/null 2>&1; then
        die "the release image $release is loaded on this machine and this folder has no .env: this script runs an unsigned build of this checkout for a trial; a release is run as docs/deployment.md says (for a trial all the same: cp deploy/env.source.example .env and, for another project than deltasleuth, add a line COMPOSE_PROJECT_NAME=<name> to it; then run this script again)"
    fi
    project=${project:-deltasleuth}
fi

# -- the address: the settings', else --port -------------------------------------------------------

if [ -e "$SETTINGS" ]; then
    office() {
        die "$SETTINGS is not a token sign-in trial at a loopback http address ($1): this script sets up a trial only; docs/deployment.md is the office deployment"
    }
    grep -q '^auth:' "$SETTINGS" || office 'it has no auth block'
    if grep -q '^  oidc:' "$SETTINGS"; then office 'auth.oidc is on'; fi
    [ "$(grep -c '^  public_url: ' "$SETTINGS")" = 1 ] || office 'it has no single line "  public_url: <address>"'
    address=$(sed -n 's/^  public_url: \([^[:blank:]]*\).*/\1/p' "$SETTINGS")
    printf '%s\n' "$address" | grep -Eqx 'http://(localhost|127\.0\.0\.1|\[::1\]):[0-9]+' ||
        office "public_url is $address"
    [ -z "$port" ] || [ "$port" = "${address##*:}" ] ||
        die "$SETTINGS exists and does not say port $port in public_url: $UNCHANGED"
    port=${address##*:}
else
    port=${port:-8000}
    address=http://localhost:$port
fi
if [ -e "$SITE" ]; then
    rendered=$(docker compose -f deploy/docker-compose.yml -f "$SITE" config) ||
        die "Compose does not accept .env and $SITE (the lines above say why)"
    holds "$rendered" "published: \"$port\"" || die "$SITE exists and does not say port $port: $UNCHANGED"
    [ -z "$subnet" ] || holds "$rendered" "- subnet: $subnet" ||
        die "$SITE exists and does not say subnet $subnet: $UNCHANGED"
elif [ "$port" != 8000 ] || [ "${subnet:-$SUBNET}" != "$SUBNET" ]; then
    if outdated "$compose"; then
        die "Docker Compose $compose is older than 2.24.4, which a site file for another port or subnet needs (!override): update Compose, or keep port 8000 and subnet $SUBNET"
    fi
fi
subnet=${subnet:-$SUBNET}

# -- the keys, and what Docker already holds -------------------------------------------------------

# The folder's mode is all that keeps its files private: the containers' users must read them (444).
if [ -d deploy/secrets ]; then
    case $(ls -ldL deploy/secrets) in
        d???--[-S]--[-T]*) ;; # a setgid or sticky folder (S, T) gives nobody else a permission
        *) die 'deploy/secrets is open to other accounts: chmod 700 it yourself, then run this script again' ;;
    esac
fi
secrets=
for name in db_password db_dsn kek tokeniser; do
    [ ! -e "deploy/secrets/$name" ] || secrets="$secrets $name"
done
case $secrets in
    '' | ' db_password db_dsn kek tokeniser') ;;
    ' db_password kek tokeniser') # step 2 makes db_dsn, and writes the password into it as it stands
        if tr -d '\n' < deploy/secrets/db_password | grep -q '[^A-Za-z0-9._~-]'; then
            die 'deploy/secrets/db_password holds characters an address cannot carry as written: write deploy/secrets/db_dsn yourself (postgresql://deltasleuth:<the password, percent-encoded>@db:5432/deltasleuth), then run this script again'
        fi
        ;;
    *) die 'deploy/secrets holds some of db_password, db_dsn, kek and tokeniser but not all: put the missing ones back; new ones cannot open what the old ones protected' ;;
esac
here=$(cd ./deploy && pwd -P)
started=$(docker ps -a --filter "label=com.docker.compose.project=$project" \
    --format 'x{{.Label "com.docker.compose.project.working_dir"}}') # x: one without the label is a line too
if [ -n "$started" ]; then
    while IFS= read -r dir; do
        dir=${dir#x}
        if [ -z "$dir" ] || [ "$(cd "$dir" 2> /dev/null && pwd -P)" != "$here" ]; then
            die "Compose project \"$project\" on this machine was started from ${dir:-a folder its container does not name}: this script takes over no other installation. For a second one here: sh deploy/up.sh --project <name> --port <port> --subnet <a.b.c.0/24>"
        fi
    done << EOF
$started
EOF
fi
[ -n "$secrets" ] || [ -z "$(docker volume ls -q --filter "label=com.docker.compose.project=$project")" ] ||
    die "Docker volumes of project \"$project\" exist, but deploy/secrets holds no db_password, kek and tokeniser: this folder does not hold the keys of that data, which DeltaSleuth cannot open without them. Use another project name, or put the secret files back. If the data is no longer wanted, docker volume ls --filter label=com.docker.compose.project=$project lists its volumes, for you to remove with docker volume rm; this script deletes nothing"
if [ ! -e "$SETTINGS" ] && [ ! -e "$SITE" ]; then
    running=$(docker ps --format '{{.Names}} {{.Ports}}') # --filter publish= asks for a container's own port
    names=$(printf '%s\n' "$running" | grep -e ":$port->" || true)
    [ -z "$names" ] || die "port $port of this machine is published by the container ${names%% *}: $MOVE"
    if command -v curl > /dev/null 2>&1; then
        for host in 127.0.0.1 '[::1]'; do # localhost is either; Docker will hold the first only
            answer=0
            curl -q -g -s -o /dev/null --noproxy '*' --max-time 2 "http://$host:$port/" || answer=$?
            [ "$answer" = 7 ] || die "port $port of this machine answers already: $MOVE"
        done
    fi
    for id in $(docker network ls -q); do
        network=$(docker network inspect --format '{{.Name}} {{range .IPAM.Config}}{{.Subnet}} {{end}}' "$id") ||
            continue
        [ "${network%% *}" != "${project}_default" ] || continue
        case " ${network#* }" in
            *" $subnet "*) die "the subnet $subnet is the Docker network ${network%% *}'s: choose another with --subnet a.b.c.0/24" ;;
        esac
    done
fi

# -- from here on files are made: each as <file>.new, then moved -----------------------------------

new=
trap '[ -z "$new" ] || rm -f "$new"' EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 131' QUIT
trap 'exit 141' PIPE
trap 'exit 143' TERM

printf '== 1/6 Settings, site file and .env\n'
for name in deltasleuth targets llm_profiles; do
    file=deploy/config/$name.yaml
    if [ -e "$file" ]; then
        kept "$file"
        continue
    fi
    new=$file.new
    if [ "$name" = deltasleuth ] && [ "$port" != 8000 ]; then
        sed "s|^  public_url: http://localhost:8000 |  public_url: http://localhost:$port |" \
            deploy/config/deltasleuth.example.yaml > "$new"
        [ "$(grep -c "^  public_url: http://localhost:$port " "$new")" = 1 ] ||
            die 'deploy/config/deltasleuth.example.yaml does not hold the line "  public_url: http://localhost:8000 " once: this script writes the port nowhere else'
    else
        cp "deploy/config/$name.example.yaml" "$new"
    fi
    chmod 644 "$new" # the app reads it as another user
    made "$file"
done
if [ -e "$SITE" ]; then
    kept "$SITE"
else
    new=$SITE.new
    if [ "$port" = 8000 ] && [ "$subnet" = "$SUBNET" ]; then
        cp deploy/docker-compose.site.example.yml "$new"
    else
        {
            cat << 'EOF'
# This installation's changes to docker-compose.yml, written by deploy/up.sh for its own port and subnet;
# deploy/docker-compose.site.example.yml shows what else goes here.
services:
  app:
    secrets: []
EOF
            [ "$port" = 8000 ] || printf '    ports: !override ["127.0.0.1:%s:8000"]\n' "$port"
            [ "$subnet" = "$SUBNET" ] ||
                printf 'networks: {default: {ipam: {config: !override [{subnet: %s, gateway: %s}]}}}\n' \
                    "$subnet" "${subnet%.0/24}.1"
        } > "$new"
    fi
    made "$SITE"
fi
if [ -e .env ]; then
    kept .env
else
    new=.env.new
    cp deploy/env.source.example "$new"
    [ "$project" = deltasleuth ] || printf 'COMPOSE_PROJECT_NAME=%s\n' "$project" >> "$new"
    made .env
fi
rendered=$(docker compose --profile mocks config) ||
    die "Compose does not accept .env and $SITE (the lines above say why)"
holds "$rendered" "name: $project" || die ".env does not make this folder project \"$project\""
holds "$rendered" "image: $project:source" ||
    die "Compose would not run the image $project:source: this script runs a build of this checkout only (see COMPOSE_FILE in .env)"
holds "$rendered" "published: \"$port\"" ||
    die "$SITE does not publish port $port, which $SETTINGS names in public_url"

printf '== 2/6 Secrets\n'
[ -d deploy/secrets ] || (umask 077 && mkdir deploy/secrets)
for name in db_password db_dsn kek tokeniser; do
    file=deploy/secrets/$name
    if [ -e "$file" ]; then
        kept "$file"
        continue
    fi
    new=$file.new
    (
        umask 077
        case $name in
            db_password) openssl rand -hex 24 ;;
            db_dsn)
                printf 'postgresql://deltasleuth:'
                tr -d '\n' < deploy/secrets/db_password
                printf '@db:5432/deltasleuth'
                ;;
            *) openssl rand -hex 32 ;;
        esac > "$new"
    )
    chmod 444 "$new" # the containers' users read it; the folder keeps it private
    made "$file"
done

# $1 failed, and Docker or ds has said why.
failed() {
    die "$1 failed (the lines above say why): correct it and run this script again; it keeps what exists"
}

printf '== 3/6 Build the image %s:source\n' "$project"
docker compose build || failed 'the build'

printf '== 4/6 Workspace and database\n'
docker compose run --rm app ds init || failed 'ds init'
docker compose run --rm app ds migrate || failed 'ds migrate'

# The app is not healthy: its last error line, if it logged one, then the refusal.
unhealthy() {
    docker compose logs --no-log-prefix --tail 40 app 2> /dev/null | grep '^error:' | tail -n 1 >&2
    die 'the app did not become healthy; the line above, if any, is its last error (all of it: docker compose logs --tail 40 app)'
}

printf '== 5/6 Start\n'
docker compose --profile mocks up -d || failed 'docker compose up'
# The service's own container, one id: "docker compose ps -a" lists the containers of "run" as well.
app=$(docker ps -aq --filter "label=com.docker.compose.project=$project" \
    --filter label=com.docker.compose.service=app --filter label=com.docker.compose.oneoff=False) || app=
app=${app%%"$nl"*}
[ -n "$app" ] || unhealthy
first=
reads=0
while :; do
    state=$(docker inspect --format '{{.State.Health.Status}} {{.RestartCount}}' "$app" 2> /dev/null) ||
        state='none 0'
    [ "${state% *}" != healthy ] || break
    first=${first:-${state#* }}
    if [ "${state#* }" -gt "$first" ] || [ "$reads" -ge 90 ]; then # a restart loop, or three minutes
        unhealthy
    fi
    reads=$((reads + 1))
    sleep 2
done

printf '== 6/6 First token\n'
if [ -e "$TOKEN" ]; then
    kept "$TOKEN"
    id=$(cut -d_ -f2 < "$TOKEN") # the T- name every list shows: no secret
    # ds opens here what a start opens: a mistake the running app has not read yet ends the run.
    tokens=$(docker compose run --rm -T app ds token list) || failed 'ds token list'
    printf '%s\n' "$tokens" | grep -e "^T-$id " | grep -q -e '  active  ' ||
        printf 'warning: %s\n' "the token in $TOKEN (T-$id) is not an active token of this database (revoked, expired, or another database's) and will be refused: remove the file and run this script again for a new one" >&2
else
    [ -d "${TOKEN%/*}" ] || (umask 077 && mkdir -p "${TOKEN%/*}")
    new=$TOKEN.new
    (
        umask 077
        docker compose run --rm -T -e DS_OPERATOR="$(id -un)" app \
            ds token create --name first-run --owner "$(id -un)" --role spec_author > "$new"
    ) || die 'the token was not created (the lines above say why)'
    if [ "$(grep -c '' "$new")" != 1 ] || [ "$(grep -c '^dst_' "$new")" != 1 ]; then
        die 'the token was not created (the lines above say why)'
    fi
    made "$TOKEN"
fi

# -- the closing block: it names the clipboard tools and runs none ----------------------------------

missing=
if [ "$(uname -s)" = Darwin ]; then
    copy=pbcopy empty='pbcopy < /dev/null'
elif [ -n "${WAYLAND_DISPLAY:-}" ]; then
    copy=wl-copy empty='wl-copy --clear'
else
    copy='xclip -selection clipboard' empty='xclip -selection clipboard < /dev/null'
fi
[ "$copy" = pbcopy ] || command -v "${copy%% *}" > /dev/null 2>&1 ||
    missing=' (install xclip or wl-clipboard to copy it; the file holds the token)'
cat << EOF

DeltaSleuth is running: Compose project "$project", an unsigned build of this checkout, for a trial.

  Open            $address   in a browser on this machine, typed exactly so
  Copy the token  tr -d '\n' < $TOKEN | $copy$missing
  Sign in         paste it into "API token", press "Sign in", then empty the clipboard: $empty$missing
  You act as      the token "first-run" (role spec_author; it expires at most 90 days after it was made): the audit log names the token, not you
  ds commands     docker compose run --rm -e DS_OPERATOR="\$(id -un)" app ds --help
  Stop, start     docker compose stop        docker compose --profile mocks up -d
  Read more       docs/deployment.md (this trial, and the office deployment)
EOF
