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
    echo "  env     — sync .env.oracle to server and restart container"
    echo "  cookies — sync cookies.txt to server and restart container"
    echo "  update  — git pull on server, rebuild image, recreate container"
    echo "  logs    — tail container logs"
    echo "  restart — restart container"
    echo "  inspect — print the retry queue and channel/video poller state"
}

sync_env() {
    rsync -e "ssh -i $SSH_KEY" .env.oracle "$HOST:$REMOTE_DIR/.env"
}

sync_cookies() {
    rsync -e "ssh -i $SSH_KEY" cookies.txt "$HOST:$REMOTE_DIR/cookies.txt"
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
}

recreate() {
    # Correct ownership before starting: the container runs as uid 1001 (see Dockerfile),
    # while state files from earlier root-running versions are owned by uid 0.
    # session_token.json is mode 0600 and an unreadable one is a fail-closed startup
    # error, so this must happen before the container comes up, not after. Idempotent.
    $SSH "sudo chown -R 1001:1001 \$HOME/knowledger-bot/data"
    $SSH "mkdir -p \$HOME/knowledger-bot/data && cd $REMOTE_DIR && docker rm -f knowledger; docker run -d --name knowledger --restart unless-stopped --network=host --env-file .env -v \$HOME/knowledger-bot/cookies.txt:/app/cookies.txt:ro -v \$HOME/knowledger-bot/data:/app/data --log-opt max-size=10m --log-opt max-file=3 knowledger"
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
