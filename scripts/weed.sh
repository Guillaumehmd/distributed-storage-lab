#!/usr/bin/env bash
# Run `weed shell` commands inside the master container.
#
#   scripts/weed.sh                           interactive shell
#   scripts/weed.sh volume.list               one command
#   scripts/weed.sh "lock; <command>; unlock" several commands, separated by ';'
#   scripts/weed.sh "help <command>"          options of a command
#
# Commands that change the cluster need `lock` first and `unlock` at the end, and
# several of them only print a plan unless asked to apply it: read their help.
set -euo pipefail

CONTAINER="${WEED_SHELL_CONTAINER:-master}"
ARGS=(weed shell -master=master:9333 -filer=filer:8888)

if [[ $# -eq 0 ]]; then
  exec docker exec -it "$CONTAINER" "${ARGS[@]}"
fi

printf '%s\n' "$*" | tr ';' '\n' | sed 's/^ *//' | docker exec -i "$CONTAINER" "${ARGS[@]}"
