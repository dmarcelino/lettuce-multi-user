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
  touch "$T/calls.log" "$T/argv.log" "$T/stdin.log" "$T/stub.env"
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
log_has() { grep -qF -- "$1" "$T/calls.log" || fail "call log lacks: $1"; }
log_lacks() { ! grep -qF -- "$1" "$T/calls.log" || fail "call log unexpectedly has: $1"; }
eq() { [[ $1 == "$2" ]] || fail "expected [$2], got [$1]"; }

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
  ! grep -q '^DEV_BYPASS' "$H/stacks/alfred/stack.env" || fail "DEV_BYPASS in stack.env"
}

test_add_secrets_differ_between_stacks() {
  ctl add alfred alfred@example.com
  ctl add hal hal@example.com
  ok_rc
  [[ $(senv alfred SESSION_SECRET) != "$(senv hal SESSION_SECRET)" ]] || fail "same SESSION_SECRET"
  [[ $(senv alfred PUSH_VAPID_PRIVATE_KEY) != "$(senv hal PUSH_VAPID_PRIVATE_KEY)" ]] || fail "same VAPID key"
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

test_up_keeps_existing_push_keys() {
  added
  local pub priv
  pub=$(senv alfred PUSH_VAPID_PUBLIC_KEY)
  priv=$(senv alfred PUSH_VAPID_PRIVATE_KEY)
  ctl up alfred
  ok_rc
  eq "$(senv alfred PUSH_VAPID_PUBLIC_KEY)" "$pub"
  eq "$(senv alfred PUSH_VAPID_PRIVATE_KEY)" "$priv"
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
  log_has "--project-directory $H/upstream/docker -f $H/upstream/docker/compose.yml -f $H/compose/hardening.yml -f $H/compose/multi.yml --env-file $H/stacks/alfred/stack.env"
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

# ── list ─────────────────────────────────────────────────────────────────────
test_list_shows_stacks_and_hosts() {
  ctl add alfred alfred@example.com
  ctl add hal hal@example.com
  ctl list
  ok_rc
  out_has "alfred"
  out_has "https://hal.example.org"
}

for t in $(declare -F | awk '$3 ~ /^test_/ { print $3 }'); do
  run_test "$t"
done
printf '\n%d passed, %d failed\n' "$PASS" "${#FAILED[@]}"
((${#FAILED[@]} == 0))
