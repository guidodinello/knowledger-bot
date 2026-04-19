#!/usr/bin/env bash
set -euo pipefail

[[ -f deploy.env ]] && source deploy.env

HOST="${DEPLOY_HOST:?set DEPLOY_HOST in deploy.env}"
SSH_KEY="${DEPLOY_SSH_KEY:-$HOME/.ssh/oracle}"
REMOTE_DIR="~/knowledger-bot"
SSH="ssh -i $SSH_KEY $HOST"

usage() {
    echo "Usage: $0 <command>"
    echo "  env     — sync .env.oracle to server and restart container"
    echo "  update  — git pull on server, rebuild image, recreate container"
    echo "  logs    — tail container logs"
    echo "  restart — restart container"
}

sync_env() {
    rsync -e "ssh -i $SSH_KEY" .env.oracle "$HOST:$REMOTE_DIR/.env"
}

recreate() {
    $SSH "cd $REMOTE_DIR && docker rm -f knowledger; docker run -d --name knowledger --restart unless-stopped --env-file .env --log-opt max-size=10m --log-opt max-file=3 knowledger"
}

case "${1:-}" in
    env)
        echo "Syncing .env.oracle..."
        sync_env
        echo "Recreating container..."
        recreate
        $SSH "docker logs --tail 20 knowledger"
        ;;
    update)
        echo "Pulling latest code and rebuilding..."
        $SSH "cd $REMOTE_DIR && git fetch origin && git reset --hard origin/main && docker build -t knowledger ."
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
    *)
        usage
        exit 1
        ;;
esac
