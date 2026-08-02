#!/usr/bin/env bash
set -euo pipefail

# shellcheck source=/dev/null
[[ -f deploy.env ]] && source deploy.env

HOST="${DEPLOY_HOST:?set DEPLOY_HOST in deploy.env}"
SSH_KEY="${DEPLOY_SSH_KEY:-$HOME/.ssh/oracle}"
REMOTE_DIR=knowledger-bot
SSH="ssh -i $SSH_KEY $HOST"

usage() {
    echo "Usage: $0 <command>"
    echo "  env      — sync .env.oracle to server and restart container"
    echo "  cookies  — sync cookies.txt to server and restart container"
    echo "  channels — seed server's data/channels.json (refuses if it already exists) and restart container"
    echo "  update   — git pull on server, rebuild image, recreate container"
    echo "  logs     — tail container logs"
    echo "  restart  — restart container"
    echo "  inspect  — print the retry queue, channel/video poller state, and watch list"
}

sync_env() {
    rsync -e "ssh -i $SSH_KEY" .env.oracle "$HOST:$REMOTE_DIR/.env"
}

sync_cookies() {
    rsync -e "ssh -i $SSH_KEY" cookies.txt "$HOST:$REMOTE_DIR/cookies.txt"
}

# Seeds the host watch list. Deliberately *not* a general sync: unlike cookies.txt (mounted
# :ro and never rewritten), data/channels.json is runtime-mutated state — /subscribe appends
# to it and the poller backfills channel_id — so the host copy is the authority. Overwriting
# it from a stale local file would silently drop live subscriptions and resolved ids.
# Read the host copy with `./deploy.sh inspect`.
sync_channels() {
    if [[ ! -f channels.json ]]; then
        echo "error: no local channels.json — start from the schema:" >&2
        echo "         cp channels.example.json channels.json" >&2
        exit 1
    fi
    # Before the existence test and the rsync, and before `recreate`'s own mkdir: on a host
    # that has never been provisioned there is no data/ for either to work in.
    $SSH "mkdir -p $REMOTE_DIR/data"
    if $SSH "test -e $REMOTE_DIR/data/channels.json"; then
        echo "error: $HOST:$REMOTE_DIR/data/channels.json already exists." >&2
        echo "       It is the authority — /subscribe and the channel_id backfill write it." >&2
        echo "       Read it with './deploy.sh inspect'; remove it on the host to re-seed." >&2
        exit 1
    fi
    rsync -e "ssh -i $SSH_KEY" channels.json "$HOST:$REMOTE_DIR/data/channels.json"
}

_print_remote_json() {
    local label="$1" remote_path="$2" content
    echo "--- $label ---"
    content="$($SSH "cat $remote_path 2>/dev/null" || true)"
    if [[ -z "$content" ]]; then
        echo "(empty — no file)"
    else
        echo "$content" | python3 -m json.tool
    fi
}

inspect() {
    _print_remote_json "petition_queue.json (retry/upload queue)" "$REMOTE_DIR/data/petition_queue.json"
    echo
    _print_remote_json "poller_state.json (seen + pending videos)" "$REMOTE_DIR/data/poller_state.json"
    echo
    _print_remote_json "channels.json (watch list)" "$REMOTE_DIR/data/channels.json"
}

recreate() {
    # Order matters here, in both directions.
    #
    # The old container is removed *first* because it runs as root (any pre-non-root
    # image): its atomic-replace writes create fresh uid-0 state files, so chowning while
    # it is still live re-roots whatever it touches — the exact race that putting the
    # chown in the deploy is meant to avoid.
    #
    # mkdir precedes chown because chown exits 1 on a missing path, and `set -euo
    # pipefail` (line 2) would abort the whole deploy on a host that has never been
    # provisioned — the case mkdir -p is here for.
    $SSH "docker rm -f knowledger 2>/dev/null || true"
    $SSH "mkdir -p \$HOME/knowledger-bot/data && sudo chown -R 1001:1001 \$HOME/knowledger-bot/data"
    $SSH "cd $REMOTE_DIR && docker run -d --name knowledger --restart unless-stopped --network=host --env-file .env -v \$HOME/knowledger-bot/cookies.txt:/app/cookies.txt:ro -v \$HOME/knowledger-bot/data:/app/data --log-opt max-size=10m --log-opt max-file=3 knowledger"
    echo "Waiting for bot to start..."
    $SSH "docker logs -f knowledger 2>&1 | grep -m1 'Application started'"
    echo "Bot is up."
}

case "${1:-}" in
    env)
        echo "Syncing .env.oracle..."
        sync_env
        echo "Recreating container..."
        recreate
        $SSH "docker logs --tail 20 knowledger"
        ;;
    cookies)
        echo "Syncing cookies.txt..."
        sync_cookies
        echo "Recreating container..."
        recreate
        $SSH "docker logs --tail 20 knowledger"
        ;;
    channels)
        echo "Seeding data/channels.json..."
        sync_channels
        echo "Recreating container..."
        recreate
        $SSH "docker logs --tail 20 knowledger"
        ;;
    update)
        echo "Pulling latest code and rebuilding..."
        $SSH "cd $REMOTE_DIR && git fetch origin && git reset --hard origin/main && docker build --build-arg GIT_SHA=\$(git rev-parse --short HEAD) --build-arg GIT_COMMIT_DATE=\$(git log -1 --format=%cI) -t knowledger ."
        echo "Recreating container..."
        recreate
        $SSH "docker logs --tail 20 knowledger"
        ;;
    logs)
        $SSH "docker logs -f knowledger"
        ;;
    restart)
        $SSH "docker rm -f knowledger"
        recreate
        $SSH "docker logs --tail 20 knowledger"
        ;;
    inspect)
        inspect
        ;;
    *)
        usage
        exit 1
        ;;
esac
