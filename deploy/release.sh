#!/bin/sh
# deploy/release.sh - build, deploy, check and roll back watch-web releases. The runbook is RELEASE.md.
#
# Run it from the project directory (where docker-compose.yml and .env are):
#
#   deploy/release.sh build                     build watch-web:<YYYY.MM.DD>-<sha7> from the checked-out commit
#   deploy/release.sh status                    the release in .env, the services, the schema, the history
#   deploy/release.sh deploy <tag>              backup, migrate, switch .env, restart, wait until healthy
#   deploy/release.sh rollback                  back to the release before the current one (no backup, no migration)
#   deploy/release.sh prune [--keep N]          remove old watch-web images (default: keep 3)
#   deploy/release.sh save <tag> <file>         write the release image and imgproxy to <file> (+ <file>.sha256)
#   deploy/release.sh load <file>               check <file>.sha256, then docker load
#
# Options: --yes (no question), --dry-run (every check and the plan, nothing changed), --no-backup (deploy without
# the backup service), --rebuild (build an existing tag again), --keep N (prune).
#
# Exit codes: 0 done (or the dry run passed), 1 usage, 2 a check or a step before the switch failed (the running
# release is untouched), 3 the schema check refused, 4 the operator answered no, 5 switched but not healthy in time.
#
# POSIX sh, no bashisms: it runs on the server's /bin/sh (dash on Ubuntu).

set -eu

HISTORY=deploy/release-history.log
ENV_FILE=.env
POLL_SECONDS=${RELEASE_POLL_SECONDS:-5}
WAIT_SECONDS=${RELEASE_WAIT_SECONDS:-1200}     # the api's start_period (20 min): a long migration at its start
MIN_FREE_MB=${RELEASE_MIN_FREE_MB:-2048}        # free space the backup needs at least
SCHEMA_CHECK="compose run --rm -T --no-deps --entrypoint python migrate -m app.backend.cli.schema_check"

YES=0
DRY_RUN=0
NO_BACKUP=0
REBUILD=0
KEEP=3

# ---------------------------------------------------------------- output

say() { printf '%s\n' "$*"; }

# die <what failed> <what to do> [exit code]: one line on stderr, the form RELEASE.md's failure table lists
die() {
    printf 'release.sh: %s. Fix: %s.\n' "$1" "$2" >&2
    exit "${3:-2}"
}

usage() {
    sed -n '4,16p' "$0" | sed 's/^# \{0,1\}//' >&2
    exit 1
}

now() { date -u +%Y-%m-%dT%H:%M:%SZ; }

operator() { printf '%s' "${SUDO_USER:-${USER:-$(id -un 2>/dev/null || echo unknown)}}"; }

history_line() {
    mkdir -p "$(dirname "$HISTORY")"
    printf '%s %s by=%s\n' "$(now)" "$*" "$(operator)" >> "$HISTORY"
}

# confirm: --yes passes; any answer but y / yes stops with exit 4 and nothing changed
confirm() {
    if [ "$YES" -eq 1 ]; then
        return 0
    fi
    printf 'Proceed? [y/N] '
    answer=""
    read -r answer || answer=""
    answer=$(printf '%s' "$answer" | tr -d '\r')   # a terminal that sends CR LF
    case "$answer" in
        y|Y|yes|YES|Yes) return 0 ;;
    esac
    say "Nothing changed."
    exit 4
}

# dry_run_stop: after the checks and the plan of a dry run
dry_run_stop() {
    if [ "$DRY_RUN" -eq 1 ]; then
        say "Dry run: every check passed, nothing changed."
        exit 0
    fi
}

# ---------------------------------------------------------------- .env

# env_value KEY: the value of the last KEY= line of .env (surrounding quotes removed), empty when missing
env_value() {
    [ -f "$ENV_FILE" ] || return 0
    sed -n "s/^$1=//p" "$ENV_FILE" | tail -n 1 | sed "s/^[\"']//; s/[\"']\$//"
}

current_tag() { env_value WATCH_WEB_TAG; }

# write_env_tag TAG: only the WATCH_WEB_TAG line changes (appended when missing). The old file is kept as .env.bak;
# the new one is written to .env.tmp (same directory, same mode) and renamed over .env, so .env is never half written
write_env_tag() {
    cp -p "$ENV_FILE" "$ENV_FILE.bak"
    cp -p "$ENV_FILE" "$ENV_FILE.tmp"
    awk -v tag="$1" '
        /^WATCH_WEB_TAG=/ { if (!done) { print "WATCH_WEB_TAG=" tag; done = 1 } next }
        { print }
        END { if (!done) print "WATCH_WEB_TAG=" tag }
    ' "$ENV_FILE" > "$ENV_FILE.tmp"
    mv "$ENV_FILE.tmp" "$ENV_FILE"
}

# ---------------------------------------------------------------- checks

check_docker() {
    docker_version=$(docker version --format '{{.Server.Version}}' 2>/dev/null) ||
        die "docker does not answer" "start docker (systemctl start docker) and run release.sh as a user allowed to use it (sudo)"
    compose_version=$(docker compose version --short 2>/dev/null) ||
        die "docker compose v2 is missing" "install the docker compose plugin"
    say "  ok   docker $docker_version, docker compose $compose_version"
}

check_project_dir() {
    [ -f docker-compose.yml ] ||
        die "no docker-compose.yml in $(pwd)" "run release.sh from the project directory (cd /path/to/watch_web)" 1
}

check_compose_config() {
    if ! output=$(docker compose config -q 2>&1); then
        printf '%s\n' "$output" >&2
        die "docker compose config failed" "correct .env (the lines above name the variable), see RELEASE.md, Preconditions"
    fi
    say "  ok   docker compose config"
}

# check_image TAG: the image exists here; prints its revision and creation time
check_image() {
    if ! labels=$(docker image inspect "watch-web:$1" --format '{{index .Config.Labels "org.opencontainers.image.revision"}} {{index .Config.Labels "org.opencontainers.image.created"}}' 2>/dev/null); then
        die "image watch-web:$1 is not on this machine" "build it (deploy/release.sh build) or bring it with deploy/release.sh load <file>; docker image ls watch-web lists what is here"
    fi
    set -- "$1" $labels
    say "  ok   image watch-web:$1 (revision ${2:-unknown}, created ${3:-unknown})"
}

# schema_check TAG ROLE: the schema check of release TAG for ROLE, run from that image. Exit 3 when it refuses.
# An image older than the check (no schema_check module) is compared by its alembic head instead.
schema_check() {
    set +e
    output=$(WATCH_WEB_TAG="$1" docker $SCHEMA_CHECK check --role "$2" 2>&1)
    code=$?
    set -e
    line=$(printf '%s\n' "$output" | grep -E '^(Schema check|SCHEMA CHECK FAILED)' | tail -n 1)
    case "$code" in
        0)
            say "  ok   schema: ${line:-passed}"
            return 0 ;;
        3)
            printf '%s\n' "$line" >&2
            return 3 ;;
    esac
    if printf '%s' "$output" | grep -q "No module named app.backend.cli.schema_check"; then
        legacy_schema_check "$1"
        return $?
    fi
    printf '%s\n' "$output" >&2
    die "the schema check of watch-web:$1 failed (exit $code)" "read the lines above; the database must be reachable with the values in .env"
}

# legacy_schema_check TAG: a release from before the schema check runs only on its own head
legacy_schema_check() {
    target_head=$(WATCH_WEB_TAG="$1" docker compose run --rm -T --no-deps --entrypoint python migrate -c \
        "from alembic.config import Config; from alembic.script import ScriptDirectory; print(ScriptDirectory.from_config(Config('alembic.ini')).get_current_head())" \
        2>/dev/null | tail -n 1)
    database=$(schema_status "$(current_tag)" | sed -n 's/^database_revision=//p')
    if [ -n "$target_head" ] && [ "$target_head" = "$database" ]; then
        say "  ok   schema: watch-web:$1 predates the schema check; its head $target_head is the database revision"
        return 0
    fi
    printf 'SCHEMA CHECK FAILED: watch-web:%s predates the schema check and knows %s; the database is at %s.\n' \
        "$1" "${target_head:-nothing}" "${database:-unknown}" >&2
    return 3
}

# schema_status TAG: the key=value lines of cli/schema_check status, run from that image
schema_status() {
    WATCH_WEB_TAG="$1" docker $SCHEMA_CHECK status 2>/dev/null | grep -E '^[a-z_]+=' || true
}

check_imgproxy() {
    image=$(env_value IMGPROXY_IMAGE)
    expected=$(env_value IMGPROXY_IMAGE_ID)
    [ -n "$image" ] || die "IMGPROXY_IMAGE is not set in .env" "set it as RELEASE.md, Pin imgproxy, says"
    [ -n "$expected" ] || die "IMGPROXY_IMAGE_ID is not set in .env" "set it as RELEASE.md, Pin imgproxy, says"
    actual=$(docker image inspect --format '{{.Id}}' "$image" 2>/dev/null) ||
        die "imgproxy image $image is not on this machine" "load it (deploy/release.sh load <file>) or docker pull $image on a connected machine"
    [ "$actual" = "$expected" ] ||
        die "imgproxy image $image has id $actual, .env says IMGPROXY_IMAGE_ID=$expected" "bring the pinned image (RELEASE.md, Pin imgproxy) or, after checking the new one, set IMGPROXY_IMAGE_ID to its id"
    say "  ok   imgproxy image id matches IMGPROXY_IMAGE_ID"
}

check_env_writable() {
    [ -f "$ENV_FILE" ] || die "no .env in $(pwd)" "copy .env.example to .env and fill it (DEPLOYMENT.md)"
    [ -w "$ENV_FILE" ] && [ -w . ] ||
        die ".env or its directory is not writable by $(operator)" "run release.sh as the owner of the project directory (sudo)"
    say "  ok   .env writable"
}

# check_backup: sets BACKUP (1: back up first); refuses without the service unless --no-backup
check_backup() {
    BACKUP=0
    if docker compose --profile tools config --services 2>/dev/null | grep -qx db-backup; then
        backup_dir=$(env_value BACKUP_DIR)
        free_mb=""
        if [ -n "$backup_dir" ]; then
            free_mb=$(df -Pk "$backup_dir" 2>/dev/null | awk 'NR == 2 { print int($4 / 1024) }')
        fi
        if [ -n "$free_mb" ] && [ "$free_mb" -lt "$MIN_FREE_MB" ]; then
            die "only $free_mb MB free in $backup_dir, the backup needs at least $MIN_FREE_MB MB" "free space (docker builder prune -af; old backups: docker compose run --rm db-backup prune) or set RELEASE_MIN_FREE_MB"
        fi
        say "  ok   backup service configured, ${free_mb:-unknown} MB free in ${backup_dir:-BACKUP_DIR}"
        BACKUP=1
    elif [ "$NO_BACKUP" -eq 1 ]; then
        say "  --   no backup service (--no-backup): the deploy makes no backup"
    else
        die "the backup service db-backup is not configured" "enable docker-compose.backup.yml in COMPOSE_FILE (BACKUPS.md), or deploy with --no-backup after taking a backup yourself"
    fi
}

# ---------------------------------------------------------------- waiting

# wait_healthy TAG: every service running, healthy where it has a healthcheck, watch-web services on TAG
wait_healthy() {
    deadline=$(( $(date +%s) + WAIT_SECONDS ))
    while :; do
        states=$(docker compose ps --format '{{.Service}}|{{.Image}}|{{.State}}|{{.Health}}' 2>/dev/null || true)
        pending=$(printf '%s\n' "$states" | awk -F'|' -v image="watch-web:$1" '
            NF < 3 { next }
            { seen = 1 }
            $3 != "running" || ($4 != "" && $4 != "healthy") { print $1 " " $3 ($4 != "" ? " " $4 : ""); next }
            $2 ~ /^watch-web:/ && $2 != image { print $1 " still on " $2 }
            END { if (!seen) print "no services" }
        ')
        if [ -z "$pending" ]; then
            printf '%s\n' "$states" | awk -F'|' 'NF >= 3 { printf "  %-14s %-40s %s %s\n", $1, $2, $3, $4 }'
            return 0
        fi
        if [ "$(date +%s)" -ge "$deadline" ]; then
            say "Not ready after ${WAIT_SECONDS} s:"
            printf '%s\n' "$pending" | sed 's/^/  /'
            return 1
        fi
        sleep "$POLL_SECONDS"
    done
}

# ---------------------------------------------------------------- commands

cmd_build() {
    git rev-parse HEAD >/dev/null 2>&1 || die "$(pwd) is not a git checkout" "run build in the source checkout on the build machine"
    if [ -n "$(git status --porcelain)" ]; then
        die "the working tree has uncommitted changes" "commit or stash them: a tag names exactly one commit"
    fi
    revision=$(git rev-parse HEAD)
    short=$(printf '%s' "$revision" | cut -c1-7)
    day=$(TZ=UTC git show -s --date=format-local:%Y.%m.%d --format=%cd HEAD)
    created=$(TZ=UTC git show -s --date=format-local:%Y-%m-%dT%H:%M:%SZ --format=%cd HEAD)
    tag="$day-$short"
    if [ "$REBUILD" -eq 0 ] && docker image inspect "watch-web:$tag" >/dev/null 2>&1; then
        say "watch-web:$tag exists already (built from $revision); --rebuild builds it again."
        return 0
    fi
    say "Building watch-web:$tag from $revision (committed $created)"
    if [ "$DRY_RUN" -eq 1 ]; then
        say "Dry run: docker build --build-arg WATCH_WEB_VERSION=$tag ... -t watch-web:$tag ."
        return 0
    fi
    docker build --build-arg "WATCH_WEB_VERSION=$tag" --build-arg "WATCH_WEB_REVISION=$revision" \
        --build-arg "WATCH_WEB_CREATED=$created" -t "watch-web:$tag" . ||
        die "docker build failed" "read the build output above"
    say "Built watch-web:$tag"
    say "Next: deploy/release.sh save $tag watch-web-$tag.tgz (air-gapped server) or deploy/release.sh deploy $tag"
}

cmd_status() {
    check_project_dir
    tag=$(current_tag)
    say "Release in .env: WATCH_WEB_TAG=${tag:-<not set>}"
    say "Services:"
    docker compose ps --all --format '{{.Service}}|{{.Image}}|{{.State}}|{{.Health}}' 2>/dev/null |
        awk -F'|' 'NF >= 3 { printf "  %-14s %-40s %s %s\n", $1, $2, $3, $4 }' || true
    if [ -n "$tag" ]; then
        say "Schema (from watch-web:$tag):"
        schema_status "$tag" | sed 's/^/  /'
    fi
    if [ -n "$(env_value IMGPROXY_IMAGE)" ]; then
        ( check_imgproxy ) || true
    fi
    say "History (last 5):"
    if [ -f "$HISTORY" ]; then
        tail -n 5 "$HISTORY" | sed 's/^/  /'
    else
        say "  (none yet)"
    fi
}

cmd_deploy() {
    tag=$1
    check_project_dir
    old=$(current_tag)
    say "Checks:"
    check_docker
    check_compose_config
    check_image "$tag"
    schema_check "$tag" migrate || exit 3
    check_imgproxy
    check_env_writable
    check_backup
    say "Plan (running release ${old:-none}):"
    if [ "$BACKUP" -eq 1 ]; then
        say "  1. back up the database (label pre-upgrade, kept 90 days), a few minutes"
    else
        say "  1. back up the database: skipped (--no-backup)"
    fi
    say "  2. migrate the database with the new release while the old one keeps serving"
    say "  3. set WATCH_WEB_TAG=$tag in .env (old file kept as .env.bak)"
    say "  4. restart the services on the new release (about 30 s without service)"
    say "  5. wait until every service is healthy (up to $WAIT_SECONDS s while the api finishes its start)"
    dry_run_stop
    confirm

    backup_id=none
    if [ "$BACKUP" -eq 1 ]; then
        say "1. Backing up the database..."
        if ! backup_out=$(docker compose run --rm -T db-backup create --label pre-upgrade); then
            printf '%s\n' "$backup_out" >&2
            history_line "deploy ${old:-none} -> $tag failed step=backup"
            die "the backup failed, nothing was changed" "read the line above (BACKUPS.md lists the codes), correct it and deploy again"
        fi
        backup_id=$(printf '%s\n' "$backup_out" | tail -n 1)
        say "   backup $backup_id"
    fi
    say "2. Migrating the database with watch-web:$tag..."
    if ! WATCH_WEB_TAG="$tag" docker compose run --rm -T migrate; then
        history_line "deploy ${old:-none} -> $tag failed step=migrate backup=$backup_id"
        die "the migration failed; the running release keeps serving" "read the migration log above; the database is at the last revision that completed (deploy/release.sh status), restore $backup_id only if the release cannot run on it (RELEASE.md, Restore the database)"
    fi
    say "3. Setting WATCH_WEB_TAG=$tag in .env"
    write_env_tag "$tag"
    say "4. Restarting the services on watch-web:$tag..."
    if ! docker compose up -d; then
        history_line "deploy ${old:-none} -> $tag failed step=up backup=$backup_id"
        die "docker compose up failed on the new release" "roll back with deploy/release.sh rollback, then read docker compose logs" 5
    fi
    say "5. Waiting until every service is healthy..."
    if ! wait_healthy "$tag"; then
        history_line "deploy ${old:-none} -> $tag failed step=wait backup=$backup_id"
        die "the new release is not healthy after $WAIT_SECONDS s" "roll back with deploy/release.sh rollback, then read docker compose logs api media-worker" 5
    fi
    history_line "deploy ${old:-none} -> $tag ok backup=$backup_id"
    say "Deployed watch-web:$tag (backup $backup_id). Verify: RELEASE.md, Verify."
}

cmd_rollback() {
    check_project_dir
    current=$(current_tag)
    [ -n "$current" ] || die "WATCH_WEB_TAG is not set in .env" "set it to the release that runs (RELEASE.md, Adopt release.sh)"
    [ -f "$HISTORY" ] || die "no $HISTORY: release.sh never deployed here" "deploy the wanted release with deploy/release.sh deploy <tag>"
    found=$(awk -v cur="$current" '$2 == "deploy" && $5 == cur && $6 == "ok" { line = $0 } END { print line }' "$HISTORY")
    [ -n "$found" ] || die "no successful deploy of $current in $HISTORY" "deploy the wanted release with deploy/release.sh deploy <tag>"
    target=$(printf '%s\n' "$found" | awk '{ print $3 }')
    backup_id=$(printf '%s\n' "$found" | sed -n 's/.* backup=\([^ ]*\).*/\1/p')
    [ "$target" != "none" ] ||
        die "$current was deployed over a release release.sh did not know" "follow RELEASE.md, Adopt release.sh on a running server, or restore backup ${backup_id:-of that deploy}"
    say "Checks:"
    check_docker
    check_compose_config
    check_image "$target"
    if ! schema_check "$target" api; then
        die "watch-web:$target does not run on the database's schema (a contract migration came after it): a rollback needs the database restore" "RELEASE.md, Restore the database; the backup of that deploy is ${backup_id:-none}" 3
    fi
    check_env_writable
    say "Plan (running release $current):"
    say "  1. set WATCH_WEB_TAG=$target in .env (old file kept as .env.bak); no backup, no migration"
    say "  2. restart the services on watch-web:$target (about 30 s without service)"
    say "  3. wait until every service is healthy (up to $WAIT_SECONDS s)"
    dry_run_stop
    confirm
    say "1. Setting WATCH_WEB_TAG=$target in .env"
    write_env_tag "$target"
    say "2. Restarting the services on watch-web:$target..."
    if ! docker compose up -d; then
        history_line "rollback $current -> $target failed step=up"
        die "docker compose up failed on $target" "deploy/release.sh deploy $current goes back to the release before the rollback" 5
    fi
    say "3. Waiting until every service is healthy..."
    if ! wait_healthy "$target"; then
        history_line "rollback $current -> $target failed step=wait"
        die "watch-web:$target is not healthy after $WAIT_SECONDS s" "read docker compose logs api media-worker; deploy/release.sh deploy $current goes back" 5
    fi
    history_line "rollback $current -> $target ok"
    say "Rolled back to watch-web:$target."
}

cmd_prune() {
    tags=$(docker image ls watch-web --format '{{.Tag}}' | grep -v '^<none>$' || true)
    keep_list=$(
        printf '%s\n' "$tags" | head -n "$KEEP"
        current_tag
        if [ -f "$HISTORY" ]; then
            tail -n "$KEEP" "$HISTORY" | awk '{ print $3; print $5 }'
        fi
    )
    remove=""
    for tag in $tags; do
        if ! printf '%s\n' "$keep_list" | grep -qx "$tag"; then
            remove="$remove $tag"
        fi
    done
    if [ -z "$remove" ]; then
        say "Nothing to remove (kept: the newest $KEEP, the tag in .env, the last $KEEP history lines)."
        return 0
    fi
    say "Plan: remove these images (kept: the newest $KEEP, the tag in .env, the last $KEEP history lines):"
    for tag in $remove; do
        say "  watch-web:$tag"
    done
    dry_run_stop
    confirm
    for tag in $remove; do
        docker image rm "watch-web:$tag" || say "  could not remove watch-web:$tag (a container uses it?)"
    done
}

cmd_save() {
    tag=$1
    file=$2
    image=${IMGPROXY_IMAGE:-$(env_value IMGPROXY_IMAGE)}
    [ -n "$image" ] || die "IMGPROXY_IMAGE is not set (environment or .env)" "IMGPROXY_IMAGE=ghcr.io/imgproxy/imgproxy:<version> deploy/release.sh save $tag $file"
    docker image inspect "watch-web:$tag" >/dev/null 2>&1 ||
        die "image watch-web:$tag is not on this machine" "build it first (deploy/release.sh build)"
    say "Writing watch-web:$tag and $image to $file..."
    if [ "$DRY_RUN" -eq 1 ]; then
        say "Dry run: nothing written."
        return 0
    fi
    docker save "watch-web:$tag" "$image" | gzip > "$file" ||
        die "docker save failed" "check that $image is pulled (docker pull $image) and that there is room for $file"
    ( cd "$(dirname "$file")" && sha256sum "$(basename "$file")" > "$(basename "$file").sha256" )
    say "Wrote $file and $file.sha256. Copy both to the server, then: deploy/release.sh load $(basename "$file")"
}

cmd_load() {
    file=$1
    [ -f "$file" ] || die "no file $file" "copy the archive and its .sha256 next to each other"
    [ -f "$file.sha256" ] || die "no $file.sha256 next to $file" "copy the .sha256 file written by deploy/release.sh save too"
    ( cd "$(dirname "$file")" && sha256sum -c "$(basename "$file").sha256" >/dev/null 2>&1 ) ||
        die "the checksum of $file does not match $file.sha256" "copy the archive again (it was damaged on the way)"
    say "Checksum ok; loading $file..."
    if [ "$DRY_RUN" -eq 1 ]; then
        say "Dry run: nothing loaded."
        return 0
    fi
    gunzip -c "$file" | docker load || die "docker load failed" "read the lines above; check free space with df -h /var/lib/docker"
    say "Loaded. Image ids: docker image ls watch-web; imgproxy: docker image inspect --format '{{.Id}}' <IMGPROXY_IMAGE>"
}

# ---------------------------------------------------------------- arguments

command=""
args=""
while [ $# -gt 0 ]; do
    case "$1" in
        --yes|-y) YES=1 ;;
        --dry-run) DRY_RUN=1 ;;
        --no-backup) NO_BACKUP=1 ;;
        --rebuild) REBUILD=1 ;;
        --keep)
            [ $# -ge 2 ] || usage
            KEEP=$2
            shift
            case "$KEEP" in ''|*[!0-9]*) usage ;; esac ;;
        -h|--help) usage ;;
        -*) usage ;;
        *)
            if [ -z "$command" ]; then
                command=$1
            else
                args="$args $1"
            fi ;;
    esac
    shift
done

# shellcheck disable=SC2086
set -- $args
case "$command" in
    build) [ $# -eq 0 ] || usage; cmd_build ;;
    status) [ $# -eq 0 ] || usage; cmd_status ;;
    deploy) [ $# -eq 1 ] || usage; cmd_deploy "$1" ;;
    rollback) [ $# -eq 0 ] || usage; cmd_rollback ;;
    prune) [ $# -eq 0 ] || usage; cmd_prune ;;
    save) [ $# -eq 2 ] || usage; cmd_save "$1" "$2" ;;
    load) [ $# -eq 1 ] || usage; cmd_load "$1" ;;
    *) usage ;;
esac
