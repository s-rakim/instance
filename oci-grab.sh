#!/usr/bin/env bash
#
# oci-grab.sh -- retry an OCI free-tier instance launch until capacity appears.
#
#   ./oci-grab.sh discover     print the OCIDs you need, then paste them below
#   ./oci-grab.sh run          loop until an instance is created
#   ./oci-grab.sh once         single attempt (useful for debugging your config)
#
# Requires: oci CLI (bash -c "$(curl -L https://raw.githubusercontent.com/oracle/\
# oci-cli/master/scripts/install/install.sh)") and a completed `oci setup config`.

set -uo pipefail

# ----------------------------------------------------------------- config

COMPARTMENT_ID="${COMPARTMENT_ID:-}"      # tenancy OCID is fine
SUBNET_ID="${SUBNET_ID:-}"
IMAGE_ID="${IMAGE_ID:-}"
SSH_KEY="${SSH_KEY:-$HOME/.ssh/id_rsa.pub}"

SHAPE="${SHAPE:-VM.Standard.A1.Flex}"
OCPUS="${OCPUS:-1}"                       # 1 OCPU / 6 GB lands FAR more often than 4/24
MEM_GB="${MEM_GB:-6}"
BOOT_GB="${BOOT_GB:-50}"
DISPLAY_NAME="${DISPLAY_NAME:-free-$(date +%m%d)}"

# ~7 minutes with jitter. This is deliberately slow: Oracle rate-limits below
# ~30s, but the bigger risk is cumulative volume. Accounts have been flagged and
# had service limits zeroed after weeks of polling, at intervals as gentle as
# 10 minutes -- see mohankumarpaluru/oracle-freetier-instance-creation#75.
# Prefer bounded runs (MAX_TRIES) over leaving this going for days.
MIN_SLEEP="${MIN_SLEEP:-390}"
MAX_SLEEP="${MAX_SLEEP:-450}"
MAX_TRIES="${MAX_TRIES:-0}"               # 0 = forever
ON_SUCCESS="${ON_SUCCESS:-}"              # arbitrary shell, run on success
DISCORD_WEBHOOK="${DISCORD_WEBHOOK:-}"    # Discord channel webhook URL

# ------------------------------------------------------------------ utils

json_escape() {                             # make $1 safe inside a JSON string
  local v=$1
  v=${v//\\/\\\\}; v=${v//\"/\\\"}; v=${v//$'\r'/}
  v=${v//$'\n'/\\n}; v=${v//$'\t'/\\t}
  printf '%s' "$v"
}

discord() {                                # $1 = message text
  [[ -n $DISCORD_WEBHOOK ]] || return 0
  if curl -fsS -m 20 -H 'Content-Type: application/json' \
       -d "{\"content\":\"$(json_escape "$1")\"}" "$DISCORD_WEBHOOK" >/dev/null 2>&1; then
    info "discord notified"
  else
    warn "discord notification failed -- check DISCORD_WEBHOOK"
  fi
}

ts()   { date '+%H:%M:%S'; }
info() { printf '\033[34m[%s]\033[0m %s\n'   "$(ts)" "$*"; }
ok()   { printf '\033[32m[%s]\033[0m %s\n'   "$(ts)" "$*"; }
warn() { printf '\033[33m[%s]\033[0m %s\n'   "$(ts)" "$*"; }
die()  { printf '\033[31m[%s]\033[0m %s\n'   "$(ts)" "$*" >&2; exit 1; }

command -v oci >/dev/null || die "oci CLI not found -- install it first"

# --------------------------------------------------------------- discover

cmd_discover() {
  # Cloud Shell exports OCI_TENANCY; deriving it from `compartment list` fails on
  # a tenancy that has no sub-compartments, which is the common fresh-account case.
  local t="${OCI_TENANCY:-}"
  if [[ -z $t ]]; then
    t=$(oci iam compartment list --all --query 'data[0]."compartment-id"' --raw-output 2>/dev/null) \
      || die "oci CLI is not authenticated -- run 'oci setup config'"
  fi
  [[ -n $t && $t != null ]] || die "could not determine your tenancy OCID -- set OCI_TENANCY"
  echo
  echo "COMPARTMENT_ID=$t   # tenancy root; a sub-compartment works too"
  echo
  echo "# --- availability domains (the script rotates all of them) ---"
  oci iam availability-domain list --compartment-id "$t" --query 'data[].name' --raw-output
  echo
  echo "# --- subnets (need one; create a VCN with the wizard if empty) ---"
  oci network subnet list --compartment-id "$t" --all \
    --query 'data[].{name:"display-name",id:id}' --output table 2>/dev/null || echo "  (none -- create a VCN first)"
  echo
  echo "# --- latest Ubuntu 22.04 aarch64 image for $SHAPE ---"
  oci compute image list --compartment-id "$t" --shape "$SHAPE" \
    --operating-system "Canonical Ubuntu" --operating-system-version "22.04" \
    --sort-by TIMECREATED --limit 1 --query 'data[0].{name:"display-name",id:id}' --output table 2>/dev/null
  echo
  echo "Paste COMPARTMENT_ID / SUBNET_ID / IMAGE_ID into the config block above."
}

# ----------------------------------------------------------------- launch

ads=()
load_ads() {
  local raw
  raw=$(oci iam availability-domain list \
        --compartment-id "$COMPARTMENT_ID" --query 'data[].name' --raw-output 2>/dev/null) \
    || die "could not list availability domains -- check COMPARTMENT_ID"

  # --raw-output only unwraps a SCALAR. For a list it still prints JSON:
  #   [
  #     "LpqT:US-CHICAGO-1-AD-1",
  #     "LpqT:US-CHICAGO-1-AD-2"
  #   ]
  # Reading that verbatim sends "[" as an availability domain and the service
  # rejects the whole request with 400 CannotParseRequest. Extract the quoted
  # names instead.
  mapfile -t ads < <(sed -n 's/.*"\([^"]*\)".*/\1/p' <<<"$raw" | tr -d '\r')
  if ((${#ads[@]} == 0)); then          # a CLI that ever returns bare lines
    mapfile -t ads < <(tr -d '[],"\r' <<<"$raw" \
                       | sed '/^[[:space:]]*$/d; s/^[[:space:]]*//; s/[[:space:]]*$//')
  fi

  local ad
  for ad in "${ads[@]}"; do
    [[ $ad == *:* ]] || die "parsed a bogus availability domain: '$ad' -- expected NAME:REGION-AD-n"
  done
  ((${#ads[@]})) || die "could not parse availability domains -- check COMPARTMENT_ID"
  info "availability domains in rotation: ${ads[*]}"
}

launch() {                                  # $1 = availability domain
  # This minimal set is VERIFIED against the live API. Adding --assign-public-ip,
  # --display-name or --boot-volume-size-in-gbs made the service answer 400
  # CannotParseRequest; which one is at fault was never isolated. Each is opt-in
  # below so you can add them back one at a time if you want to find out.
  local args=(
    --availability-domain "$1"
    --compartment-id "$COMPARTMENT_ID"
    --shape "$SHAPE"
    --image-id "$IMAGE_ID"
    --subnet-id "$SUBNET_ID"
    --ssh-authorized-keys-file "$SSH_KEY"
  )
  # non-Flex shapes (e.g. VM.Standard.E2.1.Micro) reject --shape-config
  [[ $SHAPE == *.Flex ]] && args+=(--shape-config "{\"ocpus\":$OCPUS,\"memoryInGBs\":$MEM_GB}")

  # Opt-in extras. A public subnet already assigns a public IP by default, and a
  # boot volume can be expanded after launch, so leaving these off costs little.
  [[ -n ${WITH_DISPLAY_NAME:-} ]] && args+=(--display-name "$DISPLAY_NAME")
  [[ -n ${WITH_PUBLIC_IP:-}    ]] && args+=(--assign-public-ip true)
  [[ -n ${WITH_BOOT_GB:-}      ]] && args+=(--boot-volume-size-in-gbs "$BOOT_GB")

  oci compute instance launch "${args[@]}" 2>&1
}

preflight() {
  # Values pasted into a web form (GitHub secrets, workflow inputs) or copied
  # from Windows can carry a trailing newline, CR or space. The API then answers
  # 400 without naming the offending field, so strip it before we get there.
  COMPARTMENT_ID=$(tr -d '[:space:]' <<<"$COMPARTMENT_ID")
  SUBNET_ID=$(tr -d '[:space:]' <<<"$SUBNET_ID")
  IMAGE_ID=$(tr -d '[:space:]' <<<"$IMAGE_ID")
  SHAPE=$(tr -d '[:space:]' <<<"$SHAPE")
  OCPUS=$(tr -d '[:space:]' <<<"$OCPUS")
  MEM_GB=$(tr -d '[:space:]' <<<"$MEM_GB")
  BOOT_GB=$(tr -d '[:space:]' <<<"$BOOT_GB")
  DISPLAY_NAME=$(tr -d '\r\n' <<<"$DISPLAY_NAME")   # spaces are legal in a name

  [[ $OCPUS =~ ^[0-9]+$ ]]  || die "OCPUS must be a whole number, got '$OCPUS'"
  [[ $MEM_GB =~ ^[0-9]+$ ]] || die "MEM_GB must be a whole number, got '$MEM_GB'"

  [[ -n $COMPARTMENT_ID ]] || die "COMPARTMENT_ID is empty -- run: $0 discover"
  [[ -n $SUBNET_ID      ]] || die "SUBNET_ID is empty -- run: $0 discover"
  [[ -n $IMAGE_ID       ]] || die "IMAGE_ID is empty -- run: $0 discover"
  [[ -r $SSH_KEY        ]] || die "SSH public key not readable: $SSH_KEY"
  grep -q '^ssh-' "$SSH_KEY" || die "$SSH_KEY is not a public key (you want the .pub file)"
}

cmd_run() {
  preflight
  load_ads

  local tries=0 misses=0 backoff=1 i=0 out rc
  info "shape=$SHAPE ${OCPUS}ocpu/${MEM_GB}GB  name=$DISPLAY_NAME"
  info "polling every ${MIN_SLEEP}-${MAX_SLEEP}s. Ctrl-C to stop."

  while :; do
    local ad="${ads[$(( i++ % ${#ads[@]} ))]}"
    tries=$((tries + 1))

    if (( tries == 1 )); then     # square brackets make stray whitespace visible
      info "request parameters:"
      info "  ad      [$ad]"
      info "  subnet  [$SUBNET_ID]"
      info "  image   [$IMAGE_ID]"
      info "  shape   [$SHAPE]  ocpus [$OCPUS]  memory [$MEM_GB]"
      info "  sshkey  [$SSH_KEY] ($(wc -l <"$SSH_KEY" 2>/dev/null) line(s), $(wc -c <"$SSH_KEY" 2>/dev/null) bytes)"
    fi

    out=$(launch "$ad"); rc=$?

    if [[ $rc -eq 0 ]]; then
      ok "INSTANCE CREATED in $ad after $tries attempt(s)"
      printf '\a'
      local iid ip=""
      # tolerate any spacing around the colon; the CLI pretty-prints but a
      # compact body would otherwise slip past and leave the OCID empty
      iid=$(sed -n 's/.*"id"[[:space:]]*:[[:space:]]*"\(ocid1\.instance[^"]*\)".*/\1/p' <<<"$out" | head -1)
      [[ -n $iid ]] || warn "could not parse the instance OCID from the response"

      # the VNIC (and its public IP) is attached a little after the instance
      info "waiting for the public IP to be assigned..."
      local n
      for n in 1 2 3 4 5 6 7 8 9 10; do
        ip=$(oci compute instance list-vnics --instance-id "$iid" \
             --query 'data[0]."public-ip"' --raw-output 2>/dev/null | tr -d '"[:space:]')
        [[ -n $ip && $ip != null ]] && break
        ip=""; sleep 6
      done
      [[ -n $ip ]] || ip="(not assigned yet -- check the console)"

      ok "id:        $iid"
      ok "public ip: $ip"
      ok "ssh:       ssh -i ~/.ssh/oci_arm ubuntu@$ip"

      discord ":white_check_mark: **OCI instance created**
name: ${DISPLAY_NAME}
where: ${ad}
shape: ${SHAPE} ${OCPUS} OCPU / ${MEM_GB} GB
after: ${tries} attempt(s), ${misses} capacity misses
public ip: ${ip}
\`ssh -i ~/.ssh/oci_arm ubuntu@${ip}\`"

      [[ -n $ON_SUCCESS ]] && eval "$ON_SUCCESS"
      return 0
    fi

    if grep -qiE 'out of host capacity|insufficient.*capacity' <<<"$out"; then
      misses=$((misses + 1)); backoff=1
      warn "$ad: out of capacity (miss #$misses) -- normal, continuing"
    elif grep -qiE 'toomanyrequests|429|rate.?limit' <<<"$out"; then
      backoff=$(( backoff * 2 )); (( backoff > 8 )) && backoff=8
      warn "rate limited -- backing off ${backoff}x"
    elif grep -qiE 'limitexceeded|quota|service limit' <<<"$out"; then
      discord ":octagonal_sign: OCI grabber stopped: service limit / quota exceeded after ${tries} attempts."
      die "quota exceeded -- you already hold your Always Free allowance. Terminate an old instance first."
    elif grep -qiE 'notauthorized|notfound|invalidparameter|cannotparserequest' <<<"$out"; then
      printf '%s\n' "$out" >&2
      local why code
      why=$(sed -n 's/.*"message"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' <<<"$out" | head -1)
      code=$(sed -n 's/.*"code"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' <<<"$out" | head -1)
      discord ":warning: OCI grabber stopped after ${tries} attempts.
\`${code:-error}\`: ${why:-request rejected}
Not a capacity problem -- retrying will not fix it."
      die "config error (bad OCID / permissions). Retrying will not help -- re-run: $0 discover"
    else
      warn "unrecognised error:"
      # the "message" field is what actually says what went wrong -- surface it
      grep -m1 '"message"' <<<"$out" || printf '%s\n' "$out" | head -20
    fi

    if (( MAX_TRIES > 0 && tries >= MAX_TRIES )); then
      discord ":hourglass: OCI grabber finished ${tries} attempts (${misses} capacity misses) without landing an instance."
      die "gave up after $tries attempts"
    fi

    local nap=$(( (RANDOM % (MAX_SLEEP - MIN_SLEEP + 1) + MIN_SLEEP) * backoff ))
    info "attempt $tries done ($misses capacity misses). sleeping ${nap}s"
    sleep "$nap"
  done
}

cmd_once() { preflight; load_ads; launch "${ads[0]}"; }

case "${1:-run}" in
  discover) cmd_discover ;;
  run)      cmd_run ;;
  once)     cmd_once ;;
  *)        die "usage: $0 {discover|run|once}" ;;
esac
