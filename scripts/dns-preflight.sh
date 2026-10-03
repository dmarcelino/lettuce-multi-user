#!/usr/bin/env bash
# Compares the DNS answers of two servers for a domain, record by record.
#
# Before moving a domain to Cloudflare: compare the current nameserver with the
# Cloudflare nameserver you were assigned, and switch only when nothing differs.
# After the switch: compare the old nameserver with a public resolver.
#
#   scripts/dns-preflight.sh example.com ns1.oldhost.com abc.ns.cloudflare.com
#   scripts/dns-preflight.sh example.com ns1.oldhost.com 1.1.1.1 extra-name other-name
#
# The third argument may be an authoritative nameserver or a public resolver.
# NS and SOA are skipped: they differ by design. TXT strings are joined before
# comparing, because providers split long records (DKIM keys) differently.
# Exit status: 0 when every answer matches, 1 when anything differs.
set -euo pipefail

usage() {
  sed -n '2,13p' "$0" | sed 's/^# \{0,1\}//'
  exit 2
}
(($# >= 3)) || usage
domain=$1 old=$2 new=$3
shift 3

# Common names, including what Google Workspace and Microsoft 365 set up.
names=("" www mail webmail smtp imap pop calendar drive docs sites groups start
  _dmarc google._domainkey default._domainkey selector1._domainkey selector2._domainkey
  autodiscover autoconfig _autodiscover._tcp _caldavs._tcp _carddavs._tcp
  _sip._tls _sipfederationtls._tcp lyncdiscover sip enterpriseenrollment enterpriseregistration
  ftp m blog shop api app dev staging "$@")
types=(A AAAA CNAME MX TXT CAA SRV)

answers() {
  dig +short +time=3 +tries=2 "$2" "$1" "@$3" 2>&1 |
    sed 's/" "//g' | sort
}

diffs=0 compared=0
for n in "${names[@]}"; do
  fqdn=${n:+$n.}$domain
  # A CNAME name holds no other records. Compare only the CNAME itself: a
  # resolver follows the chain to the target's records, a nameserver does not.
  name_types=("${types[@]}")
  if [[ -n $(answers "$fqdn" CNAME "$old") || -n $(answers "$fqdn" CNAME "$new") ]]; then
    name_types=(CNAME)
  fi
  for t in "${name_types[@]}"; do
    a=$(answers "$fqdn" "$t" "$old")
    b=$(answers "$fqdn" "$t" "$new")
    [[ -z $a && -z $b ]] && continue
    compared=$((compared + 1))
    if [[ $a == "$b" ]]; then
      printf 'same  %-6s %s\n' "$t" "$fqdn"
    else
      diffs=$((diffs + 1))
      printf 'DIFF  %-6s %s\n' "$t" "$fqdn"
      printf '        %s: %s\n' "$old" "${a//$'\n'/ | }"
      printf '        %s: %s\n' "$new" "${b//$'\n'/ | }"
    fi
  done
done

printf '\n%d record set(s) compared, %d difference(s).\n' "$compared" "$diffs"
printf 'Only the names listed in this script (plus any you pass) are checked; add the\n'
printf 'rest of your inventory as extra arguments.\n'
((diffs == 0))
