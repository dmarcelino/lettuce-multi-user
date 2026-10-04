# shellcheck shell=bash
# Shared helpers for lettucectl: messages, env files, config, validation, compose.
# Sourced by lettucectl; expects LETTUCE_HOME to be set.

info() { printf '%s\n' "$*" >&2; }
warn() { printf 'warning: %s\n' "$*" >&2; }
die() {
  printf 'error: %s\n' "$*" >&2
  exit 1
}

# ── env files ────────────────────────────────────────────────────────────────
# config.env and stack.env are plain KEY=value files, read by Docker Compose as
# well, so they are parsed here instead of being sourced: nothing in them runs.

# env_get FILE KEY -> prints the value (last occurrence wins); empty if absent.
env_get() {
  [[ -f $1 ]] || return 0
  awk -v k="$2" 'index($0, k "=") == 1 { v = substr($0, length(k) + 2) } END { printf "%s", v }' "$1"
}

# env_has FILE KEY -> success when KEY is present, even with an empty value.
env_has() {
  [[ -f $1 ]] && grep -q "^$2=" "$1"
}

# env_set FILE KEY VALUE -> replaces or appends KEY, keeping the file at 0600.
env_set() {
  local file=$1 key=$2 value=$3 tmp
  [[ $value != *$'\n'* ]] || die "refusing to write a multi-line value for $key"
  tmp=$(mktemp "$file.XXXXXX")
  if [[ -f $file ]]; then
    grep -v "^$key=" "$file" >"$tmp" || true
  fi
  printf '%s=%s\n' "$key" "$value" >>"$tmp"
  chmod 600 "$tmp"
  mv "$tmp" "$file"
}

# env_unset FILE KEY
env_unset() {
  local file=$1 key=$2 tmp
  [[ -f $file ]] || return 0
  tmp=$(mktemp "$file.XXXXXX")
  grep -v "^$key=" "$file" >"$tmp" || true
  chmod 600 "$tmp"
  mv "$tmp" "$file"
}

file_mode() { stat -c '%a' "$1"; }

# ── paths and config ─────────────────────────────────────────────────────────
config_file() { printf '%s/config.env' "$LETTUCE_HOME"; }
upstream_dir() { printf '%s/upstream' "$LETTUCE_HOME"; }
token_file() { printf '%s/.secrets/cloudflare-api-token' "$LETTUCE_HOME"; }
stack_dir() { printf '%s/stacks/%s' "$LETTUCE_HOME" "$1"; }
stack_env() { printf '%s/stacks/%s/stack.env' "$LETTUCE_HOME" "$1"; }

# cfg KEY -> value from config.env
cfg() { env_get "$(config_file)" "$1"; }

require_config() {
  local f
  f=$(config_file)
  [[ -f $f ]] || die "no config.env; run: lettucectl init"
  [[ $(file_mode "$f") == 600 ]] || die "config.env must be mode 600 (chmod 600 $f)"
  [[ -n $(cfg DOMAIN) && $(cfg DOMAIN) != example.com ]] || die "set DOMAIN in config.env"
  has_profile "$(cfg PROFILES)" cloudflared || die "PROFILES in config.env must contain cloudflared"
}

require_initialised() {
  require_config
  local k
  for k in CF_ACCOUNT_ID CF_ZONE_ID CF_TEAM_DOMAIN CF_GOOGLE_IDP_ID; do
    [[ -n $(cfg "$k") ]] || die "$k is empty; run: lettucectl init"
  done
  [[ -f $(upstream_dir)/docker/compose.yml ]] || die "upstream/ is missing; run: lettucectl init"
}

# has_profile "a,b,c" b -> exact comma-delimited token match, like upstream's BFF.
has_profile() {
  local IFS=, p
  for p in $1; do
    [[ $p == "$2" ]] && return 0
  done
  return 1
}

# ── validation ───────────────────────────────────────────────────────────────
validate_name() {
  [[ $1 =~ ^[a-z][a-z0-9-]{0,19}$ && $1 != *- ]] ||
    die "invalid stack name '$1': lowercase letters, digits and '-', start with a letter, max 20 chars"
}

# normalise_emails "A@x.com, b@y.org" -> "a@x.com,b@y.org" (dies on a bad address)
normalise_emails() {
  local IFS=, e out=()
  for e in $1; do
    e=$(printf '%s' "$e" | tr -d '[:space:]' | tr '[:upper:]' '[:lower:]')
    [[ -z $e ]] && continue
    [[ $e =~ ^[a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,}$ ]] || die "invalid email address '$e'"
    out+=("$e")
  done
  ((${#out[@]})) || die "at least one email address is required"
  printf '%s' "${out[*]}"
}

list_stacks() {
  local d
  for d in "$LETTUCE_HOME"/stacks/*/stack.env; do
    [[ -f $d ]] || continue
    d=${d%/stack.env}
    printf '%s\n' "${d##*/}"
  done
}

require_stack() {
  [[ -f $(stack_env "$1") ]] || die "no stack named '$1' (see: lettucectl list)"
}

# expand_targets NAME|all -> stack names, one per line
expand_targets() {
  if [[ $1 == all ]]; then
    list_stacks
  else
    validate_name "$1"
    require_stack "$1"
    printf '%s\n' "$1"
  fi
}

# ── compose ──────────────────────────────────────────────────────────────────
# compose NAME ARGS... -> docker compose for one stack. `env -i` keeps variables
# exported in the caller's shell from overriding stack.env, and the explicit
# --env-file stops Compose from reading any upstream/docker/.env.
compose() {
  local name=$1 up
  shift
  up=$(upstream_dir)
  env -i PATH="$PATH" HOME="$HOME" ${DOCKER_HOST:+DOCKER_HOST="$DOCKER_HOST"} \
    docker compose -p "lettuce-$name" \
    --project-directory "$up/docker" \
    -f "$up/docker/compose.yml" \
    -f "$LETTUCE_HOME/compose/hardening.yml" \
    -f "$LETTUCE_HOME/compose/multi.yml" \
    --env-file "$(stack_env "$name")" \
    "$@"
}

# check_stack NAME -> dies with the first reason the stack must not start.
check_stack() {
  local name=$1 f k profiles rendered published updating
  f=$(stack_env "$name")
  [[ -f $f ]] || die "$name: stack.env missing"
  [[ $(file_mode "$f") == 600 ]] || die "$name: stack.env must be mode 600"
  if grep -q '^DEV_BYPASS_' "$f"; then
    die "$name: DEV_BYPASS_* is set; it would bypass Cloudflare Access entirely"
  fi
  for k in SESSION_SECRET LETTA_STATE_DIR PUBLIC_ORIGIN ALLOWED_USERS COMPOSE_PROFILES \
    CF_ACCESS_TEAM_DOMAIN CF_ACCESS_AUD CLOUDFLARE_TUNNEL_TOKEN; do
    [[ -n $(env_get "$f" "$k") ]] || die "$name: $k is empty"
  done
  k=$(env_get "$f" SESSION_SECRET)
  ((${#k} >= 32)) || die "$name: SESSION_SECRET is shorter than 32 characters"
  profiles=$(env_get "$f" COMPOSE_PROFILES)
  has_profile "$profiles" cloudflared || die "$name: COMPOSE_PROFILES must contain cloudflared"
  [[ $profiles == "$(cfg PROFILES)" ]] ||
    die "$name: COMPOSE_PROFILES ($profiles) differs from config.env PROFILES ($(cfg PROFILES)); all stacks share one image"
  rendered=$(compose "$name" config --format json) || die "$name: docker compose config failed"
  published=$(jq '[.services[] | (.ports // []) | length] | add // 0' <<<"$rendered")
  [[ $published == 0 ]] || die "$name: $published published port(s) in the rendered config; nothing may be published"
  updating=$(jq -r '[.services | to_entries[]
    | select(.key == "app-server" or .key == "channel-gateway")
    | select(.value.environment.DISABLE_AUTOUPDATER != "1") | .key] | join(", ")' <<<"$rendered")
  [[ -z $updating ]] ||
    die "$name: DISABLE_AUTOUPDATER is not 1 for $updating; letta-code would reinstall itself over the pinned version (compose/hardening.yml)"
}
