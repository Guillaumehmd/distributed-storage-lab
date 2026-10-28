#!/usr/bin/env bash
# Fault injection for the Distributed Storage Lab.
#
# Every action is appended with a UTC timestamp to results/raw/events.log, so it
# can be lined up with the per-operation CSV written by `load.py --continuous`.
# Network faults run in a throw-away sidecar container that joins the target's
# network namespace: the SeaweedFS image needs neither tc nor tcpdump.
#
# Usage: scripts/chaos.sh <command> [args]     (or: make chaos ARGS="<command> [args]")
#
#   kill <node>                   SIGKILL: the node crashes without telling anyone
#   stop <node>...                graceful stop (SIGTERM)
#   start <node>...               start stopped nodes
#   rack-down A|B                 graceful stop of both volume servers of a rack
#   rack-up A|B                   start them again
#   wipe <node>                   disk loss: stop, erase /data, start with an empty disk
#   partition <node>              disconnect the node from the cluster network
#   heal <node>                   reconnect it
#   slow <node> [delay] [loss]    netem on the node's outgoing traffic (default 50ms 1%)
#   unslow <node>                 remove netem
#   capture <node> [seconds]      tcpdump of the node's TCP traffic -> results/raw/*.pcap (default 30 s)
#   netem-test                    check that tc netem works on this kernel (T02)
#   status                        containers, network attachment, active netem
#   heal-all                      start, reconnect and un-slow everything
#
# Nodes: master filer s3 vol1 vol2 vol3 vol4 vol5. Racks: A = vol1 vol2, B = vol3 vol4.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
NET="dslab-storage"
TOOLS_IMAGE="${TOOLS_IMAGE:-nicolaka/netshoot:v0.16}"
DEV="${NETEM_DEV:-eth0}"
EVENTS="$ROOT/results/raw/events.log"
ALL_NODES=(master filer s3 vol1 vol2 vol3 vol4 vol5)

die() { echo "chaos: $*" >&2; exit 1; }

log() {
  mkdir -p "$(dirname "$EVENTS")"
  printf '%s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%S.%3NZ)" "$*" | tee -a "$EVENTS"
}

usage() { sed -n '2,/^set -euo/p' "${BASH_SOURCE[0]}" | sed '$d; s/^# \{0,1\}//'; }

check_node() {
  local node="$1" n
  for n in "${ALL_NODES[@]}"; do [[ "$n" == "$node" ]] && break; done
  [[ "$n" == "$node" ]] || die "unknown node '$node' (expected one of: ${ALL_NODES[*]})"
  docker inspect "$node" >/dev/null 2>&1 || die "container '$node' does not exist (make up, or make vol5)"
}

is_running() { [[ "$(docker inspect -f '{{.State.Running}}' "$1" 2>/dev/null)" == "true" ]]; }

is_attached() { docker inspect -f '{{range $k, $v := .NetworkSettings.Networks}}{{$k}} {{end}}' "$1" | grep -qw "$NET"; }

rack_nodes() {
  case "${1^^}" in
    A) echo "vol1 vol2" ;;
    B) echo "vol3 vol4" ;;
    *) die "unknown rack '$1' (A or B)" ;;
  esac
}

# Run a command in the network namespace of a running node.
in_netns() {
  local node="$1"; shift
  is_running "$node" || die "$node is not running"
  docker run --rm --net "container:$node" --cap-add NET_ADMIN --cap-add NET_RAW "$@"
}

cmd_kill() {
  [[ $# -eq 1 ]] || die "usage: kill <node>"
  check_node "$1"
  docker kill "$1" >/dev/null
  log "kill $1 (SIGKILL)"
}

cmd_stop() {
  [[ $# -ge 1 ]] || die "usage: stop <node>..."
  local n; for n in "$@"; do check_node "$n"; done
  docker stop "$@" >/dev/null
  log "stop $*"
}

cmd_start() {
  [[ $# -ge 1 ]] || die "usage: start <node>..."
  local n; for n in "$@"; do check_node "$n"; done
  docker start "$@" >/dev/null
  log "start $*"
}

cmd_rack_down() {
  [[ $# -eq 1 ]] || die "usage: rack-down A|B"
  local nodes; read -r -a nodes <<<"$(rack_nodes "$1")"
  docker stop "${nodes[@]}" >/dev/null
  log "rack-down ${1^^} (stop ${nodes[*]})"
}

cmd_rack_up() {
  [[ $# -eq 1 ]] || die "usage: rack-up A|B"
  local nodes; read -r -a nodes <<<"$(rack_nodes "$1")"
  docker start "${nodes[@]}" >/dev/null
  log "rack-up ${1^^} (start ${nodes[*]})"
}

cmd_wipe() {
  [[ $# -eq 1 ]] || die "usage: wipe <node>"
  local node="$1" image
  check_node "$node"
  [[ "$node" == vol* ]] || die "wipe only applies to volume servers"
  image="$(docker inspect -f '{{.Config.Image}}' "$node")"
  is_running "$node" && docker stop "$node" >/dev/null
  log "wipe $node: stopped"
  if ! docker run --rm --volumes-from "$node" --entrypoint sh "$image" \
    -c 'find /data -mindepth 1 -maxdepth 1 -exec rm -rf {} +'; then
    docker start "$node" >/dev/null
    die "could not erase /data of $node, node restarted untouched"
  fi
  log "wipe $node: /data erased"
  docker start "$node" >/dev/null
  log "wipe $node: started with an empty disk"
}

cmd_partition() {
  [[ $# -eq 1 ]] || die "usage: partition <node>"
  check_node "$1"
  is_attached "$1" || die "$1 is already disconnected"
  docker network disconnect "$NET" "$1"
  log "partition $1 (disconnected from $NET)"
}

cmd_heal() {
  [[ $# -eq 1 ]] || die "usage: heal <node>"
  check_node "$1"
  is_attached "$1" && die "$1 is already connected"
  docker network connect --alias "$1" "$NET" "$1"
  log "heal $1 (reconnected to $NET)"
}

cmd_slow() {
  [[ $# -ge 1 && $# -le 3 ]] || die "usage: slow <node> [delay] [loss]"
  local node="$1" delay="${2:-50ms}" loss="${3:-1%}"
  check_node "$node"
  in_netns "$node" "$TOOLS_IMAGE" tc qdisc replace dev "$DEV" root netem delay "$delay" loss "$loss"
  log "slow $node (netem delay $delay loss $loss on $DEV)"
}

cmd_unslow() {
  [[ $# -eq 1 ]] || die "usage: unslow <node>"
  check_node "$1"
  in_netns "$1" "$TOOLS_IMAGE" sh -c "tc qdisc del dev $DEV root 2>/dev/null || true"
  log "unslow $1"
}

cmd_capture() {
  [[ $# -ge 1 && $# -le 2 ]] || die "usage: capture <node> [seconds]"
  local node="$1" secs="${2:-30}" file
  check_node "$node"
  file="$node-$(date -u +%Y%m%dT%H%M%SZ).pcap"
  mkdir -p "$ROOT/results/raw"
  log "capture $node for ${secs}s -> results/raw/$file"
  in_netns "$node" -v "$ROOT/results/raw:/out" "$TOOLS_IMAGE" sh -c \
    "timeout $secs tcpdump -Z root -i $DEV -s 128 -w /out/$file tcp 2>&1 | tail -n 2; chown $(id -u):$(id -g) /out/$file"
  log "capture $node done"
}

cmd_netem_test() {
  echo "netem test: ping the Docker gateway, add 50 ms of delay with tc netem, ping again"
  local out
  if ! out="$(docker run --rm -i --cap-add NET_ADMIN --cap-add NET_RAW "$TOOLS_IMAGE" sh -s <<'EOF'
avg() { ping -c 4 -q "$1" | awk -F'= ' '/min\/avg/ {split($2, a, "/"); print a[2]}'; }
gw=$(ip route | awk '/^default/ {print $3; exit}')
before=$(avg "$gw")
tc qdisc add dev eth0 root netem delay 50ms || exit 3
after=$(avg "$gw")
echo "$before $after"
EOF
)"; then
    echo "FAIL: tc netem is not available on this kernel."
    echo "WSL2 ships netem as a module: run 'sudo modprobe sch_netem' in the distro, then retry."
    echo "Fallback (roadmap T02): put Toxiproxy in front of the slow node and note it in the README."
    return 1
  fi
  read -r before after <<<"$out"
  echo "average RTT before: ${before} ms, after: ${after} ms"
  if awk -v b="$before" -v a="$after" 'BEGIN { exit !(b < 1 && a > 40 && a < 80) }'; then
    echo "PASS: tc netem works"
  else
    echo "CHECK: unexpected values, expected < 1 ms then about 50 ms"
    return 1
  fi
}

cmd_status() {
  printf '%-8s %-9s %-10s %s\n' NODE STATE NETWORK NETEM
  local n state net qdisc
  for n in "${ALL_NODES[@]}"; do
    docker inspect "$n" >/dev/null 2>&1 || continue
    state="$(docker inspect -f '{{.State.Status}}' "$n")"
    if is_attached "$n"; then net="attached"; else net="CUT"; fi
    qdisc="-"
    if [[ "$n" == vol* ]] && is_running "$n"; then
      qdisc="$(docker run --rm --net "container:$n" "$TOOLS_IMAGE" tc qdisc show dev "$DEV" 2>/dev/null \
        | awk '/netem/ { sub(/.*netem /, ""); print; exit }' || true)"
      is_attached "$n" && qdisc="${qdisc:-none}" || qdisc="${qdisc:-n/a}"
    fi
    printf '%-8s %-9s %-10s %s\n' "$n" "$state" "$net" "$qdisc"
  done
}

cmd_heal_all() {
  local n
  for n in "${ALL_NODES[@]}"; do
    docker inspect "$n" >/dev/null 2>&1 || continue
    is_attached "$n" || { docker network connect --alias "$n" "$NET" "$n"; log "heal-all: reconnect $n"; }
    is_running "$n" || { docker start "$n" >/dev/null; log "heal-all: start $n"; }
    if [[ "$n" == vol* ]]; then
      docker run --rm --net "container:$n" --cap-add NET_ADMIN "$TOOLS_IMAGE" \
        sh -c "tc qdisc del dev $DEV root 2>/dev/null || true"
    fi
  done
  log "heal-all done"
}

main() {
  [[ $# -ge 1 ]] || { usage; exit 1; }
  local cmd="$1"; shift
  case "$cmd" in
    kill | stop | start | rack-down | rack-up | wipe | partition | heal | slow | unslow | capture | netem-test | status | heal-all)
      "cmd_${cmd//-/_}" "$@" ;;
    -h | --help | help) usage ;;
    *) usage; die "unknown command '$cmd'" ;;
  esac
}

main "$@"
