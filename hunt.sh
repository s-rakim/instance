#!/usr/bin/env bash
#
# hunt.sh -- try to launch the instance every 8 minutes, forever, until one lands.
#
#   cp hunt.env.example hunt.env      # fill in the OCIDs (./oci-grab.sh discover)
#   ./hunt.sh                         # foreground; Ctrl-C to stop
#
# Leave it running under oci-hunt.service (see that file) so it survives logout
# and reboots. It stops by itself when:
#   - an instance is created (exit 0, notifies you, writes .hunt-landed)
#   - the config is wrong or the quota is used up (exit 2; retrying won't help)
# Anything else, like the network dropping or OCI answering 500, is waited out.

set -uo pipefail

HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
ENV_FILE="${HUNT_ENV:-$HERE/hunt.env}"
LANDED="$HERE/.hunt-landed"

if [[ -f $ENV_FILE ]]; then
  set -a
  # shellcheck disable=SC1090
  source <(tr -d '\r' <"$ENV_FILE")    # a Windows-edited file would put CR in every OCID
  set +a
fi

# 130 attempts/day. The launch call adds ~27s on top of the sleep, so a 638s
# sleep gives a 665s cycle and 86400/665 = 130.
INTERVAL="${INTERVAL:-638}"
[[ $INTERVAL =~ ^[0-9]+$ ]] && (( INTERVAL >= 60 )) \
  || { echo "INTERVAL must be a whole number of seconds, 60 or more (got '$INTERVAL')" >&2; exit 2; }

# oci-grab.sh does the actual work; pin it to a fixed interval with no end
export MIN_SLEEP="$INTERVAL" MAX_SLEEP="$INTERVAL" MAX_TRIES=0

ts() { date '+%Y-%m-%d %H:%M:%S'; }

if [[ -f $LANDED ]]; then
  echo "[$(ts)] an instance already landed: $(cat "$LANDED")"
  echo "[$(ts)] nothing to do. Delete $LANDED to hunt again."
  exit 0
fi

trap 'echo "[$(ts)] stopped"; exit 130' INT TERM

echo "[$(ts)] hunting: one attempt every $((INTERVAL / 60))m$((INTERVAL % 60))s until an instance lands"

while :; do
  "$HERE/oci-grab.sh" run
  rc=$?
  case $rc in
    0)
      echo "landed $(ts)" >"$LANDED"
      echo "[$(ts)] done. The hunt is over."
      exit 0
      ;;
    2)
      echo "[$(ts)] stopped: fix the error above, then start the hunt again" >&2
      exit 2
      ;;
    *)
      # exit 1 only happens before the first launch (e.g. listing availability
      # domains failed), so restarting can never create a second instance
      echo "[$(ts)] oci-grab.sh exited with $rc; retrying in ${INTERVAL}s" >&2
      sleep "$INTERVAL" &
      wait $!
      ;;
  esac
done
