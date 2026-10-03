# shellcheck shell=bash
# Cloudflare API v4 helpers (curl + jq). Sourced by lettucectl after common.sh.
#
# The API token never reaches argv: curl reads it from a config block on stdin,
# so it does not show up in `ps` or shell history. Request bodies carry no
# secrets and go in argv.
#
# Token permissions (docs/cloudflare-setup.md): Zone Read + DNS Write on the
# zone; account Cloudflare Tunnel Write, Access: Apps and Policies Write, and
# Access: Organizations, Identity Providers, and Groups Read.

CF_API=https://api.cloudflare.com/client/v4

# cf_api METHOD PATH [JSON_BODY] -> prints .result; returns 1 (with the API's
# errors on stderr) when the call is not successful.
cf_api() {
  local method=$1 path=$2 body=${3:-} token resp errors
  token=$(<"$(token_file)")
  token=${token//[$'\t\r\n ']/}
  local args=(-sS -X "$method" --config - -H 'Content-Type: application/json')
  [[ -n $body ]] && args+=(--data-binary "$body")
  if ! resp=$(printf 'header = "Authorization: Bearer %s"\n' "$token" | curl "${args[@]}" "$CF_API$path"); then
    printf 'Cloudflare API %s %s: request failed\n' "$method" "${path%%\?*}" >&2
    return 1
  fi
  if [[ $(jq -r '.success' <<<"$resp" 2>/dev/null) != true ]]; then
    errors=$(jq -r '[.errors[]? | "\(.code): \(.message)"] | join("; ")' <<<"$resp" 2>/dev/null) || errors=$resp
    printf 'Cloudflare API %s %s: %s\n' "$method" "${path%%\?*}" "${errors:-unknown error}" >&2
    return 1
  fi
  jq -c '.result' <<<"$resp"
}

# cf_zone DOMAIN -> "zone_id account_id", empty when the zone is not visible
cf_zone() {
  local r
  r=$(cf_api GET "/zones?name=$1") || return 1
  jq -r 'if length == 0 then empty else "\(.[0].id) \(.[0].account.id)" end' <<<"$r"
}

# cf_team ACCOUNT -> the <team> of <team>.cloudflareaccess.com
cf_team() {
  local r
  r=$(cf_api GET "/accounts/$1/access/organizations") || return 1
  jq -r '.auth_domain // empty | sub("\\.cloudflareaccess\\.com$"; "")' <<<"$r"
}

# cf_google_idp ACCOUNT -> id of the Google login method (Google Workspace as a fallback)
cf_google_idp() {
  local r
  r=$(cf_api GET "/accounts/$1/access/identity_providers") || return 1
  jq -r '(map(select(.type == "google")) + map(select(.type == "google-apps")))[0].id // empty' <<<"$r"
}

# cf_dns_name_taken ZONE HOST -> success when any record with that exact name exists
cf_dns_name_taken() {
  local r
  r=$(cf_api GET "/zones/$1/dns_records?name.exact=$2") || return 2
  [[ $(jq 'length' <<<"$r") != 0 ]]
}

cf_policy_body() {
  jq -cn --arg name "$1" --arg emails "$2" \
    '{name: $name, decision: "allow", include: ($emails | split(",") | map({email: {email: .}}))}'
}

# cf_policy_create ACCOUNT NAME EMAILS -> policy id
cf_policy_create() {
  local r
  r=$(cf_api POST "/accounts/$1/access/policies" "$(cf_policy_body "$2" "$3")") || return 1
  jq -r '.id' <<<"$r"
}

# cf_policy_update ACCOUNT POLICY_ID NAME EMAILS
cf_policy_update() {
  cf_api PUT "/accounts/$1/access/policies/$2" "$(cf_policy_body "$3" "$4")" >/dev/null
}

# cf_app_create ACCOUNT NAME HOST IDP SESSION_DURATION POLICY_ID -> "app_id aud"
cf_app_create() {
  local body r
  body=$(jq -cn --arg name "$2" --arg host "$3" --arg idp "$4" --arg dur "$5" --arg pol "$6" '{
    type: "self_hosted", name: $name, domain: $host,
    destinations: [{type: "public", uri: $host}],
    allowed_idps: [$idp], auto_redirect_to_identity: true,
    session_duration: $dur, app_launcher_visible: false,
    policies: [{id: $pol, precedence: 1}]}')
  r=$(cf_api POST "/accounts/$1/access/apps" "$body") || return 1
  jq -r '"\(.id) \(.aud)"' <<<"$r"
}

# cf_tunnel_create ACCOUNT NAME -> tunnel id (remotely managed: ingress lives in Cloudflare)
cf_tunnel_create() {
  local r
  r=$(cf_api POST "/accounts/$1/cfd_tunnel" "$(jq -cn --arg n "$2" '{name: $n, config_src: "cloudflare"}')") || return 1
  jq -r '.id' <<<"$r"
}

# cf_tunnel_token ACCOUNT TUNNEL_ID -> the token cloudflared runs with
cf_tunnel_token() {
  local r
  r=$(cf_api GET "/accounts/$1/cfd_tunnel/$2/token") || return 1
  jq -r '.' <<<"$r"
}

# cf_tunnel_configure ACCOUNT TUNNEL_ID HOST -> HOST to the stack's app-server, 404 for anything else
cf_tunnel_configure() {
  cf_api PUT "/accounts/$1/cfd_tunnel/$2/configurations" "$(jq -cn --arg h "$3" '{config: {ingress: [
    {hostname: $h, service: "http://app-server:8080"}, {service: "http_status:404"}]}}')" >/dev/null
}

# cf_dns_cname_create ZONE HOST TUNNEL_ID COMMENT -> record id
cf_dns_cname_create() {
  local r
  r=$(cf_api POST "/zones/$1/dns_records" "$(jq -cn --arg h "$2" --arg t "$3.cfargotunnel.com" --arg c "$4" \
    '{type: "CNAME", name: $h, content: $t, proxied: true, ttl: 1, comment: $c}')") || return 1
  jq -r '.id' <<<"$r"
}
