# Agents Guidance

This file provides guidance to agents when working with code in this repository.

## What this is
`lettucectl` is a Bash CLI that runs several single-user [Lettuce](https://github.com/dmarchevsky/lettuce) stacks on one host, one per person. Each stack is its own Docker Compose project behind its own Cloudflare Tunnel and Cloudflare Access application (Google login). There is no multi-user code: isolation comes from separate stacks, authentication from Cloudflare. README.md covers the design rationale; docs/operations.md and docs/cloudflare-setup.md cover operator workflows.

## Commands
```bash
tests/run.sh                 # all offline tests (docker, curl, dig are stubbed)
tests/run.sh <pattern>       # only tests whose function name contains <pattern>, e.g. tests/run.sh test_add_
shellcheck -x lettucectl lib/*.sh scripts/*.sh tests/run.sh tests/run-broker.sh tests/stubs/*
tests/run-broker.sh          # vault tests (pytest in its image's `test` stage; needs Docker)
tests/run-broker.sh -k vault   # extra args go to pytest
```
Run the first two before changing the CLI, and `tests/run-broker.sh` before changing `broker/`. The CLI has no build step; the broker image is built by `lettucectl up`.

## Layout and flow
- `lettucectl`: argument handling and the `cmd_*` functions (one per subcommand, dispatched from `main`).
- `lib/common.sh`: messages (`info`/`warn`/`die`), env-file helpers, paths, validation, the `compose` wrapper, VAPID key generation, and `check_stack`.
- `lib/cloudflare.sh`: Cloudflare API v4 via `cf_api` (curl + jq) and one `cf_*` helper per resource.
- `compose/hardening.yml`, `compose/multi.yml` and `compose/secrets.yml`: overlays merged on top of `upstream/docker/compose.yml`, in that order, for every stack. `secrets.yml` defines the `secret-broker` service behind the `secrets` profile; relative paths in it resolve against `upstream/docker`.
- `broker/`: the vault (Python, MCP SDK 2.x, Starlette, `cryptography`). `crypto.py` is scrypt + AES-GCM, `vault.py` the encrypted SQLite store on the broker-only volume, `kinds.py` the secret types (public and private fields, which may be shared without asking), `service.py` agent requests and approvals (in memory), `auth.py` the Access JWT and CSRF checks, `app.py` `/mcp` and the `/secrets/` UI. docs/secret-broker.md is the user guide and security model. Internal names (`secret-broker`, `secrets`, `/secrets/`) predate the vault and stay, so routes and volumes don't move.

Untracked instance data (never commit; see `.gitignore`): `config.env` (global settings plus cached Cloudflare ids), `.secrets/cloudflare-api-token`, `upstream/` (Lettuce clone at `LETTUCE_REF`), `stacks/<name>/stack.env` (that stack's secrets and Cloudflare ids) and `stacks/<name>/state/`.

## Key invariants
- **Env files are parsed, never sourced.** `config.env` and `stack.env` are plain `KEY=value` files that Docker Compose also reads. Use `env_get`/`env_set`/`env_unset`/`cfg`; `env_set` rewrites atomically and keeps mode 600. No quotes or inline comments in these files.
- **`stack.env` is the state record for `add` and `remove`.** `cmd_add` creates resources in a fixed order (Access policy -> Access app -> tunnel -> tunnel token -> tunnel ingress -> DNS CNAME) and stores each id as soon as it exists, so a re-run resumes where it stopped. `CF_DNS_RECORD_ID` being set means the stack is complete. `cmd_remove` deletes in reverse and unsets each id only on success, so it can be retried. Preserve this when adding steps; tests assert the exact call order.
- **All docker compose calls go through `compose NAME ...`.** It runs under `env -i` (so the caller's shell can't override `stack.env`), with project `lettuce-<name>`, `--project-directory upstream/docker`, the four compose files in order, and an explicit `--env-file`.
- **`check_stack` gates every `up`.** It refuses: `stack.env` not mode 600, any `DEV_BYPASS_*` (bypasses Access), required keys empty, short `SESSION_SECRET`, `COMPOSE_PROFILES` without `cloudflared` or different from `config.env` `PROFILES` (all stacks share one app-server image), any published port in the rendered config, `DISABLE_AUTOUPDATER != 1` on app-server/channel-gateway (letta-code would reinstall itself over the pinned version), and with the `secrets` profile a `secret-broker` that is missing, not `read_only`, without `cap_drop: [ALL]`, with a host mount, or whose volume another service mounts (`check_broker`). New safety rules belong here.
- **Profiles change everywhere at once.** `cmd_profiles` runs `check_stack --skip-profiles` on every stack before writing, then writes `config.env` `PROFILES` and every `stack.env` `COMPOSE_PROFILES` together and runs `up` on each. Unchanged lists skip all of that. It always removes `stale_services` (containers whose profile is off; Compose leaves them running), comparing what exists rather than what changed, so re-runs repair interrupted ones. Don't tell operators to edit the profile lists by hand.
- **Google redirect URIs are manual.** Google has no API for an OAuth client's redirect URIs, so with the `google` profile `add` and `profiles` print each stack's URI and `remove` reminds the operator to delete it (`google_redirect_uri`, `google_reminder`).
- **The Cloudflare API token never goes in argv.** `cf_api` passes it to curl as a config block on stdin; a test checks this.
- **Access policy and `ALLOWED_USERS` change together** (`cmd_users`); upstream requires them to stay in sync.
- **VAPID keys are generated once per stack** (`ensure_push_keys`) and never rotated: a new keypair invalidates every browser push subscription. The BFF (`PUSH_VAPID_*`) and the vault (`BROKER_VAPID_*`) each have their own pair.
- **Tunnel routes follow the profiles.** `cf_tunnel_configure` puts `^/secrets(/|$)` -> `secret-broker:8000` before the app-server rule when the stack has `secrets`; `add` sets it, and `profiles` re-applies every stack's routes on every run, before `up`.
- **The vault is the approval gate, not Lettuce.** Agents share the BFF's network namespace and can call the vault directly, so approval and every `/secrets/` page require a verified Access JWT (`auth.py`). Agents read names and public fields in `list_secrets`, and get private values only through `request_secret`: after the user approves those fields, or for secrets the user set to share without asking (only kinds with `auto_allowed`). Never add an MCP tool that edits secrets or bypasses approval, never write vault data, keys or the passphrase to disk unencrypted or to logs, and never mount `secret-broker-data` into another service. The data key exists only in memory while unlocked; agent activity must not extend the auto-lock.
- Upstream version moves only via `lettucectl upgrade <tag>`: Lettuce tags pin the letta-code server version the UI was compiled against.
- Use `lettucectl restart`, not a single-service restart: the BFF shares the app-server's network namespace.

## Sensitive and personal data
This repo is public and every operator runs it against their own Cloudflare account, domain and users. Nothing tracked may identify a real deployment.
- Never commit secrets: API tokens, tunnel tokens, `SESSION_SECRET`, VAPID private keys, passwords, or any value copied out of `config.env`, `stack.env` or `.secrets/`. This also covers test fixtures, docs, commit messages and PR descriptions.
- Never commit personal or deployment-specific values: real email addresses, names, domains or hostnames, Cloudflare account/zone/app/tunnel/record ids, IP addresses, or local paths such as `/home/<user>/...`.
- Use the existing placeholders instead: stack names `alfred`/`hal`, emails like `alfred@gmail.com` or `hal@example.com`, domains under `example.com`/`example.org`, and the canned stub ids (`acc1`, `zone1`, `tun1`, ...).
- When pasting command output, logs or rendered compose config into docs, tests or issues, redact real values first.
- Before committing, review the staged diff for these values. If a new kind of instance file appears, add it to `.gitignore` rather than committing it.

## Tests
`tests/run.sh` copies the repo into a temp dir per test and puts `tests/stubs/{docker,curl,dig}` first on `PATH`. Because `lettucectl` runs docker under `env -i`, stubs read their settings from `$T/stub.env` (written with `stub KEY=value`), not the environment.
- curl stub: logs `curl METHOD PATH BODY` to `calls.log`, answers with canned ids (`acc1`, `zone1`, `pol1`, `app1`/`aud1`, `tun1`/`tuntok1`, `rec1`); `STUB_CF_FAIL="METHOD /path"` forces a failure. Unknown routes fail, so add a route when adding an API call.
- docker stub: `compose config --format json` returns `$T/compose-config.json`, which tests overwrite to simulate rendered configs. `compose config --services` lists `app-server`, `bff` and the services of each profile in the `--env-file`'s `COMPOSE_PROFILES`, like upstream. `docker ps -a --filter label=com.docker.compose.project=...` answers from `$T/containers.txt` lines of `project service` (helper: `containers NAME SERVICE...`).
- dig stub: answers from `$T/dns.txt` lines of `name TYPE answer`.
- Helpers: `ctl` (runs lettucectl, sets `$RC`, output in `$OUT`), `ok_rc`/`bad_rc`, `out_has`/`out_lacks`, `log_has`/`log_lacks`, `calls`, `body_of`, `senv`, `eq`, `added` (a fresh `alfred` stack). Each test is a `test_*` function run under `set -e`, so a failing assert ends it.
- With the `secrets` profile, `check` needs a `secret-broker` in `$T/compose-config.json`: call `broker_config` (before `added`) or `with_secrets` (after it).
- Vault tests (`broker/tests`, pytest) never use the network: `FakeSender` (push), `make_jwt` with a test RSA key, `FAST_KDF` (cheap scrypt), a fake `Clock`, and fake values (`TEST_SECRET_DO_NOT_USE_938742`, a card number, a street) that tests assert never reach the vault file on disk, logs, pushes or pages before Show.
