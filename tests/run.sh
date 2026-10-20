#!/usr/bin/env bash
# Offline tests for lettucectl. docker, curl and dig are replaced by the stubs in
# tests/stubs, so nothing here touches Docker, DNS or Cloudflare.
#   tests/run.sh            run everything
#   tests/run.sh <pattern>  run the tests whose name contains <pattern>
set -uo pipefail
ROOT=$(cd "$(dirname "$0")/.." && pwd)
FILTER=${1:-}
PASS=0 FAILED=()

# ── fixture ──────────────────────────────────────────────────────────────────
# Each test gets a fresh copy of the repo in $T/home and the stubs in $T/bin.
setup() {
  T=$(mktemp -d)
  H=$T/home
  mkdir -p "$H/upstream/docker" "$H/.secrets" "$T/bin"
  cp -r "$ROOT/lettucectl" "$ROOT/lib" "$ROOT/compose" "$ROOT/config.env.example" "$H/"
  cp "$ROOT"/tests/stubs/* "$T/bin/"
  touch "$H/upstream/docker/compose.yml"
  printf 'secret-api-token-123\n' >"$H/.secrets/cloudflare-api-token"
  chmod 600 "$H/.secrets/cloudflare-api-token"
  sed -e 's/^DOMAIN=.*/DOMAIN=example.org/' -e 's/^PROFILES=.*/PROFILES=cloudflared,search/' \
    -e 's/^CF_ACCOUNT_ID=.*/CF_ACCOUNT_ID=acc1/' -e 's/^CF_ZONE_ID=.*/CF_ZONE_ID=zone1/' \
    -e 's/^CF_TEAM_DOMAIN=.*/CF_TEAM_DOMAIN=team/' -e 's/^CF_GOOGLE_IDP_ID=.*/CF_GOOGLE_IDP_ID=idp-g/' \
    -e 's/^PUSH_CONTACT_EMAIL=.*/PUSH_CONTACT_EMAIL=ops@example.org/' \
    "$ROOT/config.env.example" >"$H/config.env"
  chmod 600 "$H/config.env"
  echo '{"services":{"app-server":{"ports":[],"environment":{"DISABLE_AUTOUPDATER":"1"}},"bff":{}}}' >"$T/compose-config.json"
  touch "$T/calls.log" "$T/argv.log" "$T/stdin.log" "$T/stub.env" "$T/containers.txt"
  OUT=$T/out
}

teardown() { rm -rf "$T"; }

# ctl ARGS... -> runs lettucectl with the stubs first on PATH; output in $OUT, status in $RC
ctl() {
  RC=0
  PATH="$T/bin:$PATH" "$H/lettucectl" "$@" >"$OUT" 2>&1 || RC=$?
}

stub() { printf '%s\n' "$*" >>"$T/stub.env"; }
calls() { grep "^curl " "$T/calls.log" | cut -d' ' -f2,3 | sed 's/?.*//'; }
body_of() { grep "^curl $1 $2 " "$T/calls.log" | tail -1 | cut -d' ' -f4-; }
senv() { awk -v k="$2" 'index($0, k "=") == 1 { v = substr($0, length(k) + 2) } END { printf "%s", v }' "$H/stacks/$1/stack.env"; }

fail() {
  printf '    %s\n' "$*"
  printf '    --- output ---\n'
  sed 's/^/    /' "$OUT" 2>/dev/null | tail -15
  return 1
}
ok_rc() { [[ $RC == 0 ]] || fail "expected success, got $RC"; }
bad_rc() { [[ $RC != 0 ]] || fail "expected failure, got success"; }
out_has() { grep -qF -- "$1" "$OUT" || fail "output lacks: $1"; }
out_lacks() { ! grep -qF -- "$1" "$OUT" || fail "output unexpectedly has: $1"; }
log_has() { grep -qF -- "$1" "$T/calls.log" || fail "call log lacks: $1"; }
log_lacks() { ! grep -qF -- "$1" "$T/calls.log" || fail "call log unexpectedly has: $1"; }
eq() { [[ $1 == "$2" ]] || fail "expected [$2], got [$1]"; }

# broker_config [BROKER_JSON] -> the rendered compose config also holds this
# secret-broker (default: the hardened one compose/secrets.yml renders)
broker_config() {
  local broker=${1:-'{"read_only":true,"cap_drop":["ALL"],"volumes":[{"type":"volume","source":"secret-broker-data","target":"/data"}]}'}
  jq -c --argjson b "$broker" '.services["secret-broker"] = $b' "$T/compose-config.json" >"$T/c.json"
  mv "$T/c.json" "$T/compose-config.json"
}

run_test() {
  local name=$1
  [[ -z $FILTER || $name == *"$FILTER"* ]] || return 0
  setup
  local rc
  # A bare statement on purpose: inside `if`, `||` or `&&` bash ignores errexit
  # in the subshell too, and every assert relies on it to stop the test.
  (set -e; "$name")
  rc=$?
  if ((rc == 0)); then
    PASS=$((PASS + 1))
    printf 'ok    %s\n' "$name"
  else
    FAILED+=("$name")
    printf 'FAIL  %s\n' "$name"
  fi
  teardown
}

# ── validation ───────────────────────────────────────────────────────────────
test_add_rejects_bad_names() {
  local n
  for n in Alfred 1abc a_b trail- abcdefghijklmnopqrstu ""; do
    ctl add "$n" a@b.com
    bad_rc
    out_has "invalid stack name"
  done
  log_lacks "curl "
}

test_add_rejects_bad_email() {
  ctl add alfred not-an-email
  bad_rc
  out_has "invalid email address"
  [[ ! -e $H/stacks/alfred ]] || fail "stack dir created for a rejected add"
}

test_add_requires_init() {
  sed -i 's/^CF_ZONE_ID=.*/CF_ZONE_ID=/' "$H/config.env"
  ctl add alfred a@b.com
  bad_rc
  out_has "lettucectl init"
}

# ── init ─────────────────────────────────────────────────────────────────────
make_upstream_repo() {
  local src=$T/src
  git init -q "$src"
  mkdir -p "$src/docker"
  touch "$src/docker/compose.yml"
  git -C "$src" add -A
  git -C "$src" -c user.name=t -c user.email=t@t commit -qm init
  git -C "$src" tag v-test
  rm -rf "$H/upstream"
  sed -i -e "s|^LETTUCE_REPO=.*|LETTUCE_REPO=$src|" -e 's/^LETTUCE_REF=.*/LETTUCE_REF=v-test/' "$H/config.env"
}

test_init_creates_config_from_example() {
  rm "$H/config.env"
  ctl init
  bad_rc
  out_has "edit"
  [[ -f $H/config.env ]] || fail "config.env not created"
  eq "$(stat -c %a "$H/config.env")" 600
}

test_init_clones_and_caches_cloudflare_ids() {
  make_upstream_repo
  sed -i -e 's/^CF_\([A-Z_]*\)=.*/CF_\1=/' "$H/config.env"
  ctl init
  ok_rc
  [[ -f $H/upstream/docker/compose.yml ]] || fail "upstream not cloned"
  eq "$(git -C "$H/upstream" describe --tags)" v-test
  eq "$(grep ^CF_ACCOUNT_ID= "$H/config.env")" CF_ACCOUNT_ID=acc1
  eq "$(grep ^CF_ZONE_ID= "$H/config.env")" CF_ZONE_ID=zone1
  eq "$(grep ^CF_TEAM_DOMAIN= "$H/config.env")" CF_TEAM_DOMAIN=team
  eq "$(grep ^CF_GOOGLE_IDP_ID= "$H/config.env")" CF_GOOGLE_IDP_ID=idp-g
}

test_init_never_puts_the_api_token_in_argv() {
  make_upstream_repo
  ctl init
  ok_rc
  ! grep -q secret-api-token-123 "$T/argv.log" || fail "API token found in curl argv"
  grep -q 'Bearer secret-api-token-123' "$T/stdin.log" || fail "API token not sent via curl config on stdin"
}

test_init_fails_without_google_login_method() {
  make_upstream_repo
  stub STUB_CF_NO_GOOGLE=1
  ctl init
  bad_rc
  out_has "Google"
}

test_init_fails_when_zone_not_found() {
  make_upstream_repo
  stub STUB_CF_NO_ZONE=1
  ctl init
  bad_rc
  out_has "example.org"
}

test_init_rejects_world_readable_token() {
  make_upstream_repo
  chmod 644 "$H/.secrets/cloudflare-api-token"
  ctl init
  bad_rc
  out_has "600"
}

test_init_rejects_old_compose() {
  make_upstream_repo
  stub STUB_COMPOSE_VERSION=2.20.0
  ctl init
  bad_rc
  out_has "2.24"
}

# ── add ──────────────────────────────────────────────────────────────────────
test_add_creates_everything_in_order() {
  ctl add alfred Alfred@Example.com
  ok_rc
  eq "$(calls | tr '\n' ';')" "GET /zones/zone1/dns_records;POST /accounts/acc1/access/policies;POST /accounts/acc1/access/apps;POST /accounts/acc1/cfd_tunnel;GET /accounts/acc1/cfd_tunnel/tun1/token;PUT /accounts/acc1/cfd_tunnel/tun1/configurations;POST /zones/zone1/dns_records;"
  log_has "compose -p lettuce-alfred"
  log_has "up -d --build"
  out_has "https://alfred.example.org"
}

test_add_payloads() {
  ctl add alfred Alfred@Example.com,hal@example.com
  ok_rc
  local b
  b=$(body_of POST /accounts/acc1/access/policies)
  eq "$(jq -c '[.decision, [.include[].email.email]]' <<<"$b")" '["allow",["alfred@example.com","hal@example.com"]]'
  b=$(body_of POST /accounts/acc1/access/apps)
  eq "$(jq -c '[.type, .domain, .destinations[0].uri, .allowed_idps, .auto_redirect_to_identity, .policies]' <<<"$b")" \
    '["self_hosted","alfred.example.org","alfred.example.org",["idp-g"],true,[{"id":"pol1","precedence":1}]]'
  b=$(body_of POST /accounts/acc1/cfd_tunnel)
  eq "$(jq -c '[.name, .config_src]' <<<"$b")" '["lettuce-alfred","cloudflare"]'
  b=$(body_of PUT /accounts/acc1/cfd_tunnel/tun1/configurations)
  eq "$(jq -c '.config.ingress' <<<"$b")" '[{"hostname":"alfred.example.org","service":"http://app-server:8080"},{"service":"http_status:404"}]'
  b=$(body_of POST /zones/zone1/dns_records)
  eq "$(jq -c '[.type, .name, .content, .proxied]' <<<"$b")" '["CNAME","alfred.example.org","tun1.cfargotunnel.com",true]'
}

test_add_writes_private_stack_env() {
  ctl add alfred alfred@example.com
  ok_rc
  eq "$(stat -c %a "$H/stacks/alfred/stack.env")" 600
  eq "$(stat -c %a "$H/stacks/alfred")" 700
  eq "$(senv alfred PUBLIC_ORIGIN)" https://alfred.example.org
  eq "$(senv alfred ALLOWED_USERS)" alfred@example.com
  eq "$(senv alfred COMPOSE_PROFILES)" cloudflared,search
  eq "$(senv alfred LETTA_STATE_DIR)" "$H/stacks/alfred/state"
  eq "$(senv alfred CF_ACCESS_TEAM_DOMAIN)" team
  eq "$(senv alfred CF_ACCESS_AUD)" aud1
  eq "$(senv alfred CLOUDFLARE_TUNNEL_TOKEN)" tuntok1
  eq "$(senv alfred TZ)" UTC
  local s
  s=$(senv alfred SESSION_SECRET)
  ((${#s} == 64)) || fail "SESSION_SECRET length ${#s}"
  s=$(senv alfred PUSH_VAPID_PUBLIC_KEY)
  ((${#s} == 87)) || fail "PUSH_VAPID_PUBLIC_KEY length ${#s}"
  s=$(senv alfred PUSH_VAPID_PRIVATE_KEY)
  ((${#s} == 43)) || fail "PUSH_VAPID_PRIVATE_KEY length ${#s}"
  eq "$(senv alfred PUSH_VAPID_CONTACT_EMAIL)" ops@example.org
  s=$(senv alfred BROKER_VAPID_PUBLIC_KEY)
  ((${#s} == 87)) || fail "BROKER_VAPID_PUBLIC_KEY length ${#s}"
  s=$(senv alfred BROKER_VAPID_PRIVATE_KEY)
  ((${#s} == 43)) || fail "BROKER_VAPID_PRIVATE_KEY length ${#s}"
  [[ $(senv alfred BROKER_VAPID_PRIVATE_KEY) != "$(senv alfred PUSH_VAPID_PRIVATE_KEY)" ]] || fail "broker shares the BFF's VAPID key"
  ! grep -q '^DEV_BYPASS' "$H/stacks/alfred/stack.env" || fail "DEV_BYPASS in stack.env"
}

test_add_secrets_differ_between_stacks() {
  ctl add alfred alfred@example.com
  ctl add hal hal@example.com
  ok_rc
  [[ $(senv alfred SESSION_SECRET) != "$(senv hal SESSION_SECRET)" ]] || fail "same SESSION_SECRET"
  [[ $(senv alfred PUSH_VAPID_PRIVATE_KEY) != "$(senv hal PUSH_VAPID_PRIVATE_KEY)" ]] || fail "same VAPID key"
}

test_add_routes_the_secrets_path_with_the_profile() {
  sed -i 's/^PROFILES=.*/PROFILES=cloudflared,secrets/' "$H/config.env"
  broker_config
  ctl add alfred alfred@example.com
  ok_rc
  eq "$(jq -c '.config.ingress' <<<"$(body_of PUT /accounts/acc1/cfd_tunnel/tun1/configurations)")" \
    '[{"hostname":"alfred.example.org","path":"^/secrets(/|$)","service":"http://secret-broker:8000"},{"hostname":"alfred.example.org","service":"http://app-server:8080"},{"service":"http_status:404"}]'
}

test_add_falls_back_when_name_resolves() {
  echo "alfred.example.org A 1.2.3.4" >"$T/dns.txt"
  ctl add alfred alfred@example.com
  ok_rc
  eq "$(senv alfred PUBLIC_ORIGIN)" https://lettuce-alfred.example.org
}

test_add_falls_back_when_name_is_in_the_zone() {
  stub STUB_CF_TAKEN=alfred.example.org
  ctl add alfred alfred@example.com
  ok_rc
  eq "$(senv alfred PUBLIC_ORIGIN)" https://lettuce-alfred.example.org
}

test_add_fails_when_both_names_taken() {
  printf '%s\n' "mail.example.org CNAME mail.example.net." "lettuce-mail.example.org TXT x" >"$T/dns.txt"
  ctl add mail a@b.com
  bad_rc
  out_has "lettuce-mail.example.org"
  [[ ! -e $H/stacks/mail ]] || fail "stack dir left behind"
}

test_add_warns_about_wildcard_records() {
  stub STUB_DNS_WILDCARD=5.6.7.8
  ctl add alfred alfred@example.com
  ok_rc
  out_has "wildcard"
  eq "$(senv alfred PUBLIC_ORIGIN)" https://alfred.example.org
}

test_add_resumes_after_a_failure() {
  stub 'STUB_CF_FAIL="POST /accounts/acc1/cfd_tunnel"'
  ctl add alfred alfred@example.com
  bad_rc
  out_has "stubbed failure"
  out_has "lettucectl add alfred"
  eq "$(senv alfred CF_ACCESS_APP_ID)" app1
  : >"$T/stub.env"
  : >"$T/calls.log"
  ctl add alfred alfred@example.com
  ok_rc
  eq "$(calls | tr '\n' ';')" "POST /accounts/acc1/cfd_tunnel;GET /accounts/acc1/cfd_tunnel/tun1/token;PUT /accounts/acc1/cfd_tunnel/tun1/configurations;POST /zones/zone1/dns_records;"
}

test_add_prints_the_google_redirect_uri() {
  sed -i 's/^PROFILES=.*/PROFILES=cloudflared,google/' "$H/config.env"
  ctl add alfred alfred@example.com
  ok_rc
  out_has "https://alfred.example.org/api/google/oauth/callback"
}

test_add_skips_the_google_hint_without_the_profile() {
  ctl add alfred alfred@example.com
  ok_rc
  out_lacks "oauth/callback"
}

test_add_refuses_an_existing_stack() {
  ctl add alfred alfred@example.com
  : >"$T/calls.log"
  ctl add alfred alfred@example.com
  bad_rc
  out_has "already exists"
  log_lacks "curl "
}

# ── check / up ───────────────────────────────────────────────────────────────
added() {
  ctl add alfred alfred@example.com
  ok_rc
  : >"$T/calls.log"
}

test_up_refuses_dev_bypass() {
  added
  echo "DEV_BYPASS_EMAIL=x@y.com" >>"$H/stacks/alfred/stack.env"
  ctl up alfred
  bad_rc
  out_has "DEV_BYPASS"
  log_lacks "up -d"
}

test_up_refuses_empty_tunnel_token() {
  added
  sed -i 's/^CLOUDFLARE_TUNNEL_TOKEN=.*/CLOUDFLARE_TUNNEL_TOKEN=/' "$H/stacks/alfred/stack.env"
  ctl up alfred
  bad_rc
  out_has "CLOUDFLARE_TUNNEL_TOKEN"
  log_lacks "up -d"
}

test_up_refuses_profile_drift() {
  added
  sed -i 's/^PROFILES=.*/PROFILES=cloudflared,search,codex/' "$H/config.env"
  ctl up alfred
  bad_rc
  out_has "differs"
  log_lacks "up -d"
}

test_up_refuses_published_ports() {
  added
  echo '{"services":{"app-server":{"ports":[{"target":8080,"published":"8090"}]}}}' >"$T/compose-config.json"
  ctl up alfred
  bad_rc
  out_has "published port"
  log_lacks "up -d"
}

test_up_refuses_auto_update() {
  added
  echo '{"services":{"app-server":{"ports":[],"environment":{}}}}' >"$T/compose-config.json"
  ctl up alfred
  bad_rc
  out_has "DISABLE_AUTOUPDATER"
  log_lacks "up -d"
}

test_up_refuses_auto_update_in_channel_gateway() {
  added
  echo '{"services":{"app-server":{"environment":{"DISABLE_AUTOUPDATER":"1"}},"channel-gateway":{}}}' >"$T/compose-config.json"
  ctl up alfred
  bad_rc
  out_has "channel-gateway"
  log_lacks "up -d"
}

test_up_adds_push_keys_to_existing_stack() {
  added
  sed -i '/^PUSH_VAPID_/d' "$H/stacks/alfred/stack.env"
  ctl up alfred
  ok_rc
  [[ -n $(senv alfred PUSH_VAPID_PUBLIC_KEY) ]] || fail "no PUSH_VAPID_PUBLIC_KEY"
  [[ -n $(senv alfred PUSH_VAPID_PRIVATE_KEY) ]] || fail "no PUSH_VAPID_PRIVATE_KEY"
  eq "$(senv alfred PUSH_VAPID_CONTACT_EMAIL)" ops@example.org
  log_has "up -d"
}

test_up_adds_broker_push_keys_to_existing_stack() {
  added
  local pub
  pub=$(senv alfred PUSH_VAPID_PUBLIC_KEY)
  sed -i '/^BROKER_VAPID_/d' "$H/stacks/alfred/stack.env"
  ctl up alfred
  ok_rc
  [[ -n $(senv alfred BROKER_VAPID_PUBLIC_KEY) ]] || fail "no BROKER_VAPID_PUBLIC_KEY"
  [[ -n $(senv alfred BROKER_VAPID_PRIVATE_KEY) ]] || fail "no BROKER_VAPID_PRIVATE_KEY"
  eq "$(senv alfred PUSH_VAPID_PUBLIC_KEY)" "$pub"
}

test_up_keeps_existing_push_keys() {
  added
  local pub priv
  pub=$(senv alfred PUSH_VAPID_PUBLIC_KEY)
  priv=$(senv alfred BROKER_VAPID_PRIVATE_KEY)
  ctl up alfred
  ok_rc
  eq "$(senv alfred PUSH_VAPID_PUBLIC_KEY)" "$pub"
  eq "$(senv alfred BROKER_VAPID_PRIVATE_KEY)" "$priv"
}

test_up_refuses_missing_push_contact() {
  added
  sed -i 's/^PUSH_CONTACT_EMAIL=.*/PUSH_CONTACT_EMAIL=/' "$H/config.env"
  ctl up alfred
  bad_rc
  out_has "PUSH_CONTACT_EMAIL"
  log_lacks "up -d"
}

test_check_refuses_missing_push_keys() {
  added
  sed -i '/^PUSH_VAPID_PRIVATE_KEY=/d' "$H/stacks/alfred/stack.env"
  ctl check alfred
  bad_rc
  out_has "PUSH_VAPID_PRIVATE_KEY"
}

# with_secrets [BROKER_JSON] -> alfred runs the `secrets` profile and the
# rendered config holds this secret-broker (default: the hardened one)
with_secrets() {
  sed -i 's/^PROFILES=.*/PROFILES=cloudflared,secrets/' "$H/config.env"
  sed -i 's/^COMPOSE_PROFILES=.*/COMPOSE_PROFILES=cloudflared,secrets/' "$H/stacks/alfred/stack.env"
  broker_config "$@"
}

test_check_accepts_the_hardened_broker() {
  added
  with_secrets
  ctl check alfred
  ok_rc
}

test_check_refuses_missing_broker_keys() {
  added
  sed -i '/^BROKER_VAPID_PRIVATE_KEY=/d' "$H/stacks/alfred/stack.env"
  ctl check alfred
  bad_rc
  out_has "BROKER_VAPID_PRIVATE_KEY"
}

test_check_refuses_a_broker_that_is_not_hardened() {
  added
  local b
  for b in \
    '{"cap_drop":["ALL"],"volumes":[]}' \
    '{"read_only":true,"volumes":[]}' \
    '{"read_only":true,"cap_drop":["NET_RAW"],"volumes":[]}' \
    '{"read_only":true,"cap_drop":["ALL"],"volumes":[{"type":"bind","source":"/srv","target":"/data"}]}'; do
    with_secrets "$b"
    ctl check alfred
    bad_rc
    out_has "secret-broker"
  done
}

test_check_refuses_another_service_on_the_broker_volume() {
  added
  with_secrets
  jq -c '.services.bff.volumes = [{"type":"volume","source":"secret-broker-data","target":"/x"}]' "$T/compose-config.json" >"$T/c.json"
  mv "$T/c.json" "$T/compose-config.json"
  ctl check alfred
  bad_rc
  out_has "bff"
}

test_check_refuses_secrets_profile_without_the_broker() {
  added
  with_secrets
  jq -c 'del(.services["secret-broker"])' "$T/compose-config.json" >"$T/c.json"
  mv "$T/c.json" "$T/compose-config.json"
  ctl check alfred
  bad_rc
  out_has "secret-broker"
}

test_up_refuses_readable_stack_env() {
  added
  chmod 644 "$H/stacks/alfred/stack.env"
  ctl up alfred
  bad_rc
  out_has "600"
}

test_compose_ignores_the_callers_environment() {
  added
  PUBLIC_ORIGIN=https://evil.example ctl up alfred
  ok_rc
  log_lacks "PUBLIC_ORIGIN=https://evil.example"
  log_has "--project-directory $H/upstream/docker -f $H/upstream/docker/compose.yml -f $H/compose/hardening.yml -f $H/compose/multi.yml -f $H/compose/secrets.yml --env-file $H/stacks/alfred/stack.env"
}

test_up_all_starts_every_stack() {
  ctl add alfred alfred@example.com
  ctl add hal hal@example.com
  : >"$T/calls.log"
  ctl up all
  ok_rc
  log_has "-p lettuce-alfred"
  log_has "-p lettuce-hal"
}

# ── users ────────────────────────────────────────────────────────────────────
test_users_updates_policy_and_allowlist_together() {
  added
  ctl users alfred alfred@example.com,Partner@Example.com
  ok_rc
  eq "$(jq -c '[.include[].email.email]' <<<"$(body_of PUT /accounts/acc1/access/policies/pol1)")" '["alfred@example.com","partner@example.com"]'
  eq "$(senv alfred ALLOWED_USERS)" alfred@example.com,partner@example.com
  log_has "up -d"
}

# ── remove ───────────────────────────────────────────────────────────────────
test_remove_needs_the_name_typed() {
  added
  ctl remove alfred <<<"nope"
  bad_rc
  log_lacks "curl DELETE"
  log_lacks "down"
}

test_remove_deletes_cloudflare_resources_and_keeps_data() {
  added
  ctl remove alfred <<<"alfred"
  ok_rc
  log_has "down -v"
  eq "$(calls | tr '\n' ';')" "DELETE /zones/zone1/dns_records/rec1;DELETE /accounts/acc1/cfd_tunnel/tun1/connections;DELETE /accounts/acc1/cfd_tunnel/tun1;DELETE /accounts/acc1/access/apps/app1;DELETE /accounts/acc1/access/policies/pol1;"
  [[ -d $H/stacks/alfred/state ]] || fail "state deleted without --purge"
  eq "$(senv alfred CF_TUNNEL_ID)" ""
}

test_remove_purge_deletes_the_stack_directory() {
  added
  ctl remove alfred --purge <<<"alfred"
  ok_rc
  log_has "run --rm --no-deps"
  # `compose run` recreates the project network; a final `down` must remove it again.
  eq "$(grep '^docker compose' "$T/calls.log" | tail -1 | grep -o ' down$')" " down"
  [[ ! -e $H/stacks/alfred ]] || fail "stack directory still exists"
}

test_remove_continues_past_already_deleted_resources() {
  added
  stub 'STUB_CF_FAIL="DELETE /accounts/acc1/cfd_tunnel/tun1"'
  ctl remove alfred <<<"alfred"
  ok_rc
  out_has "stubbed failure"
  log_has "curl DELETE /accounts/acc1/access/apps/app1"
}

test_remove_reminds_to_delete_the_google_redirect_uri() {
  sed -i 's/^PROFILES=.*/PROFILES=cloudflared,google/' "$H/config.env"
  added
  ctl remove alfred --purge <<<"alfred"
  ok_rc
  out_has "delete https://alfred.example.org/api/google/oauth/callback"
}

test_remove_says_the_vault_goes_too() {
  sed -i 's/^PROFILES=.*/PROFILES=cloudflared,secrets/' "$H/config.env"
  broker_config
  added
  ctl remove alfred <<<"alfred"
  ok_rc
  out_has "Its vault"
}

# ── profiles ─────────────────────────────────────────────────────────────────
test_profiles_sets_config_and_every_stack() {
  ctl add alfred alfred@example.com
  ctl add hal hal@example.com
  : >"$T/calls.log"
  ctl profiles "cloudflared, search,google"
  ok_rc
  eq "$(grep ^PROFILES= "$H/config.env")" PROFILES=cloudflared,search,google
  eq "$(senv alfred COMPOSE_PROFILES)" cloudflared,search,google
  eq "$(senv hal COMPOSE_PROFILES)" cloudflared,search,google
  eq "$(stat -c %a "$H/stacks/hal/stack.env")" 600
  log_has "-p lettuce-alfred"
  log_has "-p lettuce-hal"
  log_has "up -d --build"
  log_lacks " rm -sf"
  out_has "https://alfred.example.org/api/google/oauth/callback"
  out_has "https://hal.example.org/api/google/oauth/callback"
}

# containers NAME SERVICE... -> the stub's `docker ps` reports these containers for the stack
containers() {
  local n=$1 s
  shift
  for s in "$@"; do
    printf 'lettuce-%s %s\n' "$n" "$s" >>"$T/containers.txt"
  done
}

test_profiles_removes_services_of_dropped_profiles() {
  added
  containers alfred app-server bff cloudflared searxng ddg-mcp
  ctl profiles cloudflared
  ok_rc
  log_has "up -d --build"
  grep -q '^docker compose -p lettuce-alfred .* rm -sf ddg-mcp searxng$' "$T/calls.log" || fail "dropped services not removed"
  out_lacks "oauth/callback"
}

test_profiles_removes_leftovers_on_a_rerun() {
  added
  containers alfred app-server bff cloudflared searxng ddg-mcp google-mcp
  ctl profiles cloudflared,search
  ok_rc
  grep -q '^docker compose -p lettuce-alfred .* rm -sf google-mcp$' "$T/calls.log" || fail "leftover google-mcp not removed"
}

test_profiles_same_list_restarts_nothing() {
  added
  containers alfred app-server bff cloudflared searxng ddg-mcp
  ctl profiles cloudflared,search
  ok_rc
  out_has "nothing to change"
  log_lacks "up -d"
  log_lacks " rm -sf"
}

test_profiles_prints_redirect_uris_whenever_google_is_on() {
  sed -i 's/^PROFILES=.*/PROFILES=cloudflared,google/' "$H/config.env"
  added
  ctl profiles cloudflared,google
  ok_rc
  out_has "https://alfred.example.org/api/google/oauth/callback"
  log_lacks "up -d"
}

test_profiles_checks_every_stack_before_writing() {
  ctl add alfred alfred@example.com
  ctl add hal hal@example.com
  : >"$T/calls.log"
  sed -i 's/^CLOUDFLARE_TUNNEL_TOKEN=.*/CLOUDFLARE_TUNNEL_TOKEN=/' "$H/stacks/alfred/stack.env"
  ctl profiles cloudflared,search,google
  bad_rc
  out_has "CLOUDFLARE_TUNNEL_TOKEN"
  eq "$(grep ^PROFILES= "$H/config.env")" PROFILES=cloudflared,search
  eq "$(senv hal COMPOSE_PROFILES)" cloudflared,search
  log_lacks "up -d"
}

test_profiles_repairs_drift() {
  added
  sed -i 's/^COMPOSE_PROFILES=.*/COMPOSE_PROFILES=cloudflared/' "$H/stacks/alfred/stack.env"
  ctl profiles cloudflared,search
  ok_rc
  eq "$(senv alfred COMPOSE_PROFILES)" cloudflared,search
  log_has "up -d --build"
}

test_profiles_routes_secrets_for_every_stack() {
  broker_config
  ctl add alfred alfred@example.com
  ctl add hal hal@example.com
  : >"$T/calls.log"
  ctl profiles cloudflared,search,secrets
  ok_rc
  eq "$(grep -c '^curl PUT /accounts/acc1/cfd_tunnel/tun1/configurations ' "$T/calls.log")" 2
  grep '^curl PUT /accounts/acc1/cfd_tunnel/tun1/configurations ' "$T/calls.log" | grep -qF '"hostname":"hal.example.org","path":"^/secrets(/|$)"' ||
    fail "hal has no /secrets route"
  # The route is in place before the stacks start.
  [[ $(grep -n '^curl PUT' "$T/calls.log" | tail -1 | cut -d: -f1) -lt $(grep -n 'up -d --build' "$T/calls.log" | head -1 | cut -d: -f1) ]] ||
    fail "stacks started before their route"
}

test_profiles_dropping_secrets_removes_the_route() {
  sed -i 's/^PROFILES=.*/PROFILES=cloudflared,secrets/' "$H/config.env"
  broker_config
  added
  ctl profiles cloudflared
  ok_rc
  log_has "curl PUT /accounts/acc1/cfd_tunnel/tun1/configurations"
  log_lacks "secret-broker"
}

test_profiles_reports_a_failed_route_update() {
  broker_config
  added
  stub 'STUB_CF_FAIL="PUT /accounts/acc1/cfd_tunnel/tun1/configurations"'
  ctl profiles cloudflared,search,secrets
  bad_rc
  out_has "run the same command again"
}

test_profiles_refuses_a_bad_list() {
  added
  local l
  for l in search "cloudflared,Search" "cloudflared,search,search" "cloudflared,a.b" ","; do
    ctl profiles "$l"
    bad_rc
  done
  eq "$(grep ^PROFILES= "$H/config.env")" PROFILES=cloudflared,search
  eq "$(senv alfred COMPOSE_PROFILES)" cloudflared,search
  log_lacks "up -d"
}

test_profiles_without_arguments_shows_them() {
  added
  ctl profiles
  ok_rc
  out_has "config.env"
  grep -qE '^alfred +cloudflared,search$' "$OUT" || fail "alfred's profiles not shown"
  log_lacks "up -d"
}

test_profiles_view_needs_only_config() {
  added
  sed -i 's/^CF_ZONE_ID=.*/CF_ZONE_ID=/' "$H/config.env"
  ctl profiles
  ok_rc
  out_has "cloudflared,search"
}

# ── list ─────────────────────────────────────────────────────────────────────
test_list_shows_stacks_and_hosts() {
  ctl add alfred alfred@example.com
  ctl add hal hal@example.com
  ctl list
  ok_rc
  out_has "alfred"
  out_has "https://hal.example.org"
}

# ── shell ────────────────────────────────────────────────────────────────────
# agents NAME... -> what `letta agents list` answers: one agent per name, id agent-<n>
agents() {
  local i=0 n items=()
  for n in "$@"; do
    i=$((i + 1))
    items+=("$(jq -nc --arg id "agent-$i" --arg n "$n" '{id: $id, name: $n, system: "long prompt"}')")
  done
  printf '{"items":[%s]}\n' "$(IFS=,; echo "${items[*]}")" >"$T/agents.json"
}

test_shell_without_agent_opens_work() {
  added
  ctl shell alfred </dev/null
  ok_rc
  log_has "-p lettuce-alfred"
  log_has "exec -T -w /work app-server bash"
  log_lacks "letta agents list"
}

test_shell_opens_the_agents_workspace() {
  added
  agents Alfred Lucius
  ctl shell alfred lucius </dev/null
  ok_rc
  out_has "alfred: Lucius (agent-2)"
  log_has "exec -T -w /work/agent-2 -e AGENT_ID=agent-2 -e LETTA_AGENT_ID=agent-2 -e AGENT_NAME=Lucius -e USER_CWD=/work/agent-2 app-server bash -c"
  # shellcheck disable=SC2016 # the literal script lettucectl passes to the container
  log_has 'export MEMORY_DIR=$LETTA_LOCAL_BACKEND_DIR/memfs/$AGENT_ID/memory'
}

test_shell_passes_stdin_to_the_agents_shell() {
  added
  agents Lucius
  ctl shell alfred lucius <<<'ls'
  ok_rc
  eq "$(cat "$T/exec-stdin.log")" ls
}

test_shell_accepts_an_agent_id() {
  added
  agents Alfred Lucius
  ctl shell alfred agent-1 </dev/null
  ok_rc
  log_has "-e AGENT_NAME=Alfred"
}

test_shell_keeps_spaces_in_agent_names() {
  added
  agents "The Finn"
  ctl shell alfred "the finn" </dev/null
  ok_rc
  log_has "-e AGENT_NAME=The Finn -e"
}

test_shell_lists_agents_when_none_match() {
  added
  agents Alfred Lucius
  ctl shell alfred hal </dev/null
  bad_rc
  out_has "alfred has no agent 'hal'; agents: Alfred, Lucius"
  log_lacks "-w /work"
}

test_shell_refuses_ambiguous_agent_names() {
  added
  agents Saburo saburo
  ctl shell alfred Saburo </dev/null
  bad_rc
  out_has "use an id: agent-1 agent-2"
  log_lacks "-w /work"
}

test_shell_fails_when_agents_cannot_be_listed() {
  added
  stub STUB_AGENTS_RC=1
  ctl shell alfred lucius </dev/null
  bad_rc
  out_has "could not list agents"
}

test_shell_rejects_unknown_stacks() {
  ctl shell hal </dev/null
  bad_rc
  out_has "no stack named 'hal'"
}

# ── toolbox ──────────────────────────────────────────────────────────────────
# toolbox_ready -> alfred with agents Alfred and Lucius (agent-2), an app-server
# image and Lucius's workspace; the operator's home is the empty $T/u
toolbox_ready() {
  added
  agents Alfred Lucius
  jq -c '.services["app-server"].image = "lettuce-app-server:test"' "$T/compose-config.json" >"$T/c.json"
  mv "$T/c.json" "$T/compose-config.json"
  mkdir -p "$H/stacks/alfred/state/workspaces/agent-2" "$T/u"
}

# tb ARGS... -> `toolbox alfred ARGS...` with $T/u as home, so nothing of the real
# operator's is read; TB_SOCK and TB_CLAUDE stand in for their variables
tb() {
  HOME=$T/u XDG_CONFIG_HOME="" CLAUDE_CONFIG_DIR="" SSH_AUTH_SOCK=${TB_SOCK:-} \
    CLAUDE_CODE_OAUTH_TOKEN=${TB_CLAUDE:-} ctl toolbox alfred "$@" </dev/null
}

# toolbox_tools -> the operator has ssh, git, gh and Claude Code set up
toolbox_tools() {
  mkdir -p "$T/u/.ssh" "$T/u/.claude/skills"
  touch "$T/u/.ssh/known_hosts" "$T/u/.gitconfig" "$T/u/.claude/CLAUDE.md" "$T/agent.sock"
  TB_SOCK=$T/agent.sock
  stub STUB_GH_TOKEN=gh-tok-1
  stub STUB_SECRET_TOOL=claude-tok-1
}

# toolbox_file LINE... -> the operator's personal toolbox file
toolbox_file() {
  mkdir -p "$T/u/.config/lettucectl"
  printf '%s\n' "$@" >"$T/u/.config/lettucectl/toolbox"
  chmod 600 "$T/u/.config/lettucectl/toolbox"
}

run_env() { cat "$T/run-env.log" 2>/dev/null; }

test_toolbox_opens_the_agents_workspace() {
  toolbox_ready
  tb lucius
  ok_rc
  out_has "alfred: Lucius (agent-2)"
  log_has "--hostname toolbox-alfred --label lettucectl.toolbox=alfred --label lettucectl.agent=agent-2 --security-opt no-new-privileges --memory 8g --pids-limit 4096 --tmpfs /root:mode=700,exec --tmpfs /tmp:exec"
  log_has "-v $H/stacks/alfred/state/workspaces/agent-2:/work/agent-2 -w /work/agent-2"
  log_has "-e AGENT_ID=agent-2 -e LETTA_AGENT_ID=agent-2 -e AGENT_NAME=Lucius -e USER_CWD=/work/agent-2"
  log_has "-e PATH=/opt/claude-code/bin:/usr/local/bin:/usr/bin:/bin"
  log_has " lettuce-app-server:test bash"
}

test_toolbox_clears_the_images_compose_labels() {
  toolbox_ready
  tb lucius
  ok_rc
  # Inherited from the Compose-built image, they would make the toolbox one of the
  # stack's app-server containers to Compose and `stale_services`.
  log_has "docker run --rm --init -i --label com.docker.compose.project= --label com.docker.compose.service= --hostname"
  log_lacks "org.opencontainers.image.title="
}

test_toolbox_runs_a_command() {
  toolbox_ready
  tb lucius claude --resume
  ok_rc
  log_has " lettuce-app-server:test claude --resume"
}

test_toolbox_skips_what_the_host_lacks() {
  toolbox_ready
  tb lucius
  ok_rc
  log_lacks "ssh-agent.sock"
  log_lacks ".gitconfig"
  log_lacks "/root/.claude"
  log_lacks "GH_TOKEN"
  log_lacks "CLAUDE_CODE_OAUTH_TOKEN"
  out_has "toolbox: nothing of yours found"
  out_has "claude setup-token"
}

test_toolbox_brings_in_the_operators_tools() {
  toolbox_ready
  toolbox_tools
  tb lucius
  ok_rc
  out_has "toolbox: ssh-agent, git, gh, claude config, claude token"
  out_lacks "claude setup-token"
  log_has "-v $T/agent.sock:/run/ssh-agent.sock -e SSH_AUTH_SOCK=/run/ssh-agent.sock"
  log_has "-v $T/u/.ssh/known_hosts:/root/.ssh/known_hosts:ro"
  log_has "-v $T/u/.gitconfig:/root/.gitconfig:ro"
  log_has "-e GIT_CONFIG_KEY_1=credential.https://github.com.helper -e GIT_CONFIG_VALUE_1=!gh auth git-credential"
  log_has "-v $T/u/.claude/CLAUDE.md:/root/.claude/CLAUDE.md:ro"
  log_has "-v $T/u/.claude/skills:/root/.claude/skills:ro"
  log_lacks "settings.json"
}

test_toolbox_passes_tokens_by_name_only() {
  toolbox_ready
  toolbox_tools
  tb lucius
  ok_rc
  log_has "-e GH_TOKEN"
  log_has "-e CLAUDE_CODE_OAUTH_TOKEN"
  log_lacks "gh-tok-1"
  log_lacks "claude-tok-1"
  run_env | grep -qx "GH_TOKEN=gh-tok-1" || fail "GH_TOKEN not passed: $(run_env)"
  run_env | grep -qx "CLAUDE_CODE_OAUTH_TOKEN=claude-tok-1" || fail "Claude token not passed: $(run_env)"
}

test_toolbox_prefers_the_callers_claude_token() {
  toolbox_ready
  toolbox_tools
  TB_CLAUDE=env-tok-1 tb lucius
  ok_rc
  run_env | grep -qx "CLAUDE_CODE_OAUTH_TOKEN=env-tok-1" || fail "caller's token not used: $(run_env)"
}

test_toolbox_reads_the_personal_file() {
  toolbox_ready
  mkdir -p "$T/u/shared" "$T/u/.aws" "$T/data"
  touch "$T/u/.aws/config"
  toolbox_file "# extras" "" \
    "mount ~/shared" \
    "mount ~/.aws/config ~/.aws/config" \
    "mount $T/data /data rw" \
    "secret TEST_TOKEN printf tok-123" \
    "env AWS_PROFILE=dev"
  tb lucius
  ok_rc
  out_has "+5 from $T/u/.config/lettucectl/toolbox"
  log_has "-v $T/u/shared:$T/u/shared:ro"
  log_has "-v $T/u/.aws/config:/root/.aws/config:ro"
  log_has "-v $T/data:/data:rw"
  log_has "-e AWS_PROFILE=dev"
  log_has "-e TEST_TOKEN"
  log_lacks "tok-123"
  run_env | grep -qx "TEST_TOKEN=tok-123" || fail "secret not passed: $(run_env)"
}

test_toolbox_file_overrides_built_in_tokens() {
  toolbox_ready
  toolbox_tools
  toolbox_file "secret GH_TOKEN printf file-tok"
  tb lucius
  ok_rc
  eq "$(run_env | grep -c '^GH_TOKEN=')" 1
  run_env | grep -qx "GH_TOKEN=file-tok" || fail "file token not used: $(run_env)"
}

test_toolbox_never_sources_the_personal_file() {
  toolbox_ready
  # shellcheck disable=SC2016 # the literal line a sourced file would run
  toolbox_file 'mount $(touch '"$T"'/pwned)'
  tb lucius
  bad_rc
  [[ ! -e $T/pwned ]] || fail "the personal file was executed"
  log_lacks "docker run"
}

test_toolbox_refuses_unknown_directives() {
  toolbox_ready
  toolbox_file "volume ~/shared"
  tb lucius
  bad_rc
  out_has "unknown directive 'volume'"
  log_lacks "docker run"
}

test_toolbox_refuses_missing_mounts() {
  toolbox_ready
  toolbox_file "mount ~/nope"
  tb lucius
  bad_rc
  out_has "$T/u/nope does not exist"
  log_lacks "docker run"
}

test_toolbox_refuses_a_personal_file_others_can_write() {
  toolbox_ready
  toolbox_file "env A=b"
  chmod 666 "$T/u/.config/lettucectl/toolbox"
  tb lucius
  bad_rc
  out_has "writable by others"
  log_lacks "docker run"
}

test_toolbox_fails_when_a_secret_command_fails() {
  toolbox_ready
  toolbox_file "secret TEST_TOKEN false"
  tb lucius
  bad_rc
  out_has "secret TEST_TOKEN"
  log_lacks "docker run"
}

test_toolbox_needs_the_stack_image() {
  toolbox_ready
  stub STUB_IMAGE_RC=1
  tb lucius
  bad_rc
  out_has "lettucectl up alfred"
  log_lacks "docker run"
}

test_toolbox_needs_the_agents_workspace() {
  toolbox_ready
  rmdir "$H/stacks/alfred/state/workspaces/agent-2"
  tb lucius
  bad_rc
  out_has "no workspace"
  log_lacks "docker run"
}

test_toolbox_lists_agents_when_none_match() {
  toolbox_ready
  tb hal
  bad_rc
  out_has "alfred has no agent 'hal'; agents: Alfred, Lucius"
  log_lacks "docker run"
}

test_toolbox_requires_an_agent() {
  toolbox_ready
  tb
  bad_rc
  out_has "usage: lettucectl toolbox"
}

# ── skills ───────────────────────────────────────────────────────────────────
# skill NAME... -> an agent skill in the repo copy
skill() {
  local s
  for s in "$@"; do
    mkdir -p "$H/agent-skills/$s"
    printf -- '---\nname: %s\ndescription: test\n---\n' "$s" >"$H/agent-skills/$s/SKILL.md"
  done
}
shipped() { tar -tf "$T/exec-stdin.log" | LC_ALL=C sort | paste -sd' '; }

test_skills_installs_every_skill_dir() {
  added
  skill alpha beta
  mkdir -p "$H/agent-skills/notes"
  touch "$H/agent-skills/notes/README.md"
  ctl skills alfred
  ok_rc
  out_has "alfred: 2 skill(s) installed"
  eq "$(shipped)" "alpha/ alpha/SKILL.md beta/ beta/SKILL.md"
  log_has "-p lettuce-alfred"
  log_has "exec -T app-server sh -c"
  log_has "sh /root/.letta/skills alpha beta"
}

test_skills_all_covers_every_stack() {
  ctl add alfred alfred@example.com
  ctl add hal hal@example.com
  skill alpha
  : >"$T/calls.log"
  ctl skills all
  ok_rc
  out_has "alfred: 1 skill(s) installed"
  out_has "hal: 1 skill(s) installed"
  log_has "-p lettuce-hal"
}

test_skills_removes_all_when_the_repo_has_none() {
  added
  ctl skills alfred
  ok_rc
  out_has "alfred: 0 skill(s) installed"
  eq "$(shipped)" ""
}

test_skills_refuses_upstream_names() {
  added
  mkdir -p "$H/upstream/docker/agent-skills/serving-web-apps"
  skill serving-web-apps
  ctl skills alfred
  bad_rc
  out_has "skill 'serving-web-apps' is also shipped by upstream Lettuce"
  log_lacks " exec "
}

test_skills_refuses_bad_names() {
  added
  skill My.Skill
  ctl skills alfred
  bad_rc
  out_has "invalid skill name 'My.Skill'"
  log_lacks " exec "
}

test_skills_fails_when_the_stack_is_down() {
  added
  skill alpha
  stub STUB_EXEC_RC=1
  ctl skills alfred
  bad_rc
  out_has "alfred: could not install skills (is the stack up?)"
}

test_skills_rejects_unknown_stacks() {
  ctl skills hal
  bad_rc
  out_has "no stack named 'hal'"
}

test_skills_ships_the_repo_skills() {
  added
  cp -r "$ROOT/agent-skills" "$H/"
  ctl skills alfred
  ok_rc
  tar -tf "$T/exec-stdin.log" | grep -qx "scheduling-scripts/SKILL.md" || fail "scheduling-scripts not shipped"
  grep -qx "name: scheduling-scripts" "$ROOT/agent-skills/scheduling-scripts/SKILL.md" || fail "SKILL.md name mismatch"
  for f in using-vault-secrets/SKILL.md using-vault-secrets/vault-run; do
    tar -tf "$T/exec-stdin.log" | grep -qx "$f" || fail "$f not shipped"
  done
  grep -qx "name: using-vault-secrets" "$ROOT/agent-skills/using-vault-secrets/SKILL.md" || fail "SKILL.md name mismatch"
}

# The script lettucectl runs in the container, run here on a stand-in skills dir.
test_skills_script_replaces_only_its_own_skills() {
  added
  skill alpha beta
  ctl skills alfred
  ok_rc
  local d=$T/skills
  mkdir -p "$d/serving-web-apps" "$d/old" "$d/alpha"
  touch "$d/serving-web-apps/SKILL.md" "$d/old/SKILL.md" "$d/alpha/stale.txt"
  printf 'old\nalpha\n' >"$d/.lettucectl-skills"
  sh -c "$(cat "$T/exec-script")" sh "$d" alpha beta <"$T/exec-stdin.log"
  eq "$(cd "$d" && find . | LC_ALL=C sort | paste -sd' ')" \
    ". ./.lettucectl-skills ./alpha ./alpha/SKILL.md ./beta ./beta/SKILL.md ./serving-web-apps ./serving-web-apps/SKILL.md"
  eq "$(paste -sd' ' "$d/.lettucectl-skills")" "alpha beta"
}

test_skills_script_keeps_skills_on_a_broken_stream() {
  added
  skill alpha
  ctl skills alfred
  ok_rc
  local d=$T/skills
  mkdir -p "$d/alpha"
  touch "$d/alpha/SKILL.md"
  printf 'alpha\n' >"$d/.lettucectl-skills"
  ! sh -c "$(cat "$T/exec-script")" sh "$d" alpha <<<'not a tar' 2>/dev/null || fail "script accepted a broken stream"
  eq "$(cd "$d" && find . | LC_ALL=C sort | paste -sd' ')" ". ./.lettucectl-skills ./alpha ./alpha/SKILL.md"
}

for t in $(declare -F | awk '$3 ~ /^test_/ { print $3 }'); do
  run_test "$t"
done
printf '\n%d passed, %d failed\n' "$PASS" "${#FAILED[@]}"
((${#FAILED[@]} == 0))
