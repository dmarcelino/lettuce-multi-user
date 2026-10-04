# lettuce-multi-user

Run several [Lettuce](https://github.com/dmarchevsky/lettuce) stacks on one host, one per person. Each stack sits behind its own Cloudflare Tunnel and Cloudflare Access application, and people sign in with Google.

Each person gets:
- a private Letta agent server with its own memory, conversations, workspaces and secrets;
- the Lettuce web UI (a PWA) at `https://<name>.<your-domain>`.

Nobody can reach another person's stack. **Cloudflare authenticates the people; Docker separates the assistants.** Upstream Lettuce needs no multi-user code for this.

```
              Internet
                 │  https://alfred.example.com      https://hal.example.com
                 ▼
   ┌───────────────────────── Cloudflare ─────────────────────────┐
   │  Access app "lettuce-alfred"      Access app "lettuce-hal"   │
   │  allow: alfred@…  (Google login)  allow: hal@…               │
   │  Tunnel "lettuce-alfred"          Tunnel "lettuce-hal"       │
   └──────────┬──────────────────────────────────┬────────────────┘
              │ outbound-only connections        │
   ┌──────────┴────────── your host ─────────────┴────────────────┐
   │ compose project lettuce-alfred    compose project lettuce-hal│
   │  cloudflared → app-server:8080    cloudflared → app-server   │
   │  app-server + bff (+ sidecars)    app-server + bff           │
   │  stacks/alfred/state/             stacks/hal/state/          │
   │                  no ports published on the host              │
   └──────────────────────────────────────────────────────────────┘
```

## Why this shape
- **No multi-user code.** Lettuce is single-user: agents in one stack can read each other's memory and tokens. Giving everyone a separate stack isolates them using what Docker already provides.
- **No open ports.** `cloudflared` connects outbound, so the host can run behind NAT, and its firewall can stay closed except for SSH. `compose/multi.yml` removes every published port.
- **Defence in depth.** Each stack is checked three times:
  - The Access policy admits only that stack's people.
  - The Lettuce BFF verifies the Access JWT against that application's own AUD tag, so a token minted for another stack is rejected.
  - `ALLOWED_USERS` must also match. `lettucectl users` updates the policy and `ALLOWED_USERS` together, because upstream says they must be kept in sync.
- **One command per person.** `lettucectl add <name> <email>` creates the Access policy and application, the tunnel and its routing, and the DNS record through the Cloudflare API, then starts the stack.

## Requirements
- Linux host with Docker Engine and Docker Compose >= 2.24, plus `git`, `curl`, `jq`, `openssl` and `dig` (`dnsutils` / `bind-utils`).
- A domain whose DNS is on Cloudflare. The free plan is enough.
- Cloudflare Zero Trust. The free plan covers up to 50 users.
- A Google Cloud OAuth client for the Google login method (free).
- RAM for every stack you run. Each app-server is capped at 8 GiB, most other services at 1 GiB and cloudflared at 256 MiB (`compose/hardening.yml`). These are caps, not reservations; measure idle use with `docker stats`.

## Quickstart
1. Do the one-time Cloudflare and Google setup: **[docs/cloudflare-setup.md](docs/cloudflare-setup.md)**. Allow about an hour, most of it waiting for DNS.
2. Configure and start:
   ```bash
   git clone <this repo> ~/lettuce && cd ~/lettuce
   ./lettucectl init                 # creates config.env from the example; edit it
   $EDITOR config.env                # DOMAIN, PROFILES, TZ, PUSH_CONTACT_EMAIL
   ./lettucectl init                 # clones Lettuce, checks the token, caches Cloudflare ids
   ./lettucectl add alfred alfred@gmail.com
   ./lettucectl add hal hal@example.com
   ```
3. Each person opens their URL, signs in with Google, and connects a model provider in Lettuce under **Settings -> Providers & models**. One API key per person keeps spending separate.

Day-to-day commands, upgrades, backups and troubleshooting are in **[docs/operations.md](docs/operations.md)**.

## What lives where
| Path | Tracked | What |
|---|---|---|
| `lettucectl`, `lib/` | yes | the CLI |
| `compose/hardening.yml` | yes | `no-new-privileges`, memory/pid caps for every service, letta-code self-update off |
| `compose/multi.yml` | yes | drops all published ports, so stacks never collide |
| `config.env.example` | yes | template for `config.env` |
| `scripts/dns-preflight.sh` | yes | compares two nameservers before and after moving a domain |
| `tests/` | yes | offline tests (`tests/run.sh`); docker, curl and dig are stubbed |
| `config.env` | **no** | your settings plus cached Cloudflare ids (mode 600) |
| `.secrets/cloudflare-api-token` | **no** | Cloudflare API token (mode 600) |
| `upstream/` | **no** | Lettuce, cloned at `LETTUCE_REF` |
| `stacks/<name>/stack.env` | **no** | that stack's secrets and Cloudflare ids (mode 600) |
| `stacks/<name>/state/` | **no** | that stack's data: `letta-home/`, `letta-data/`, `workspaces/` |

The compose files of each stack are applied in this order:
1. `upstream/docker/compose.yml`
2. `compose/hardening.yml`
3. `compose/multi.yml`

Each stack runs as Compose project `lettuce-<name>` with `--env-file stacks/<name>/stack.env`.

## Limits and caveats
- **Version pinning.** Lettuce is compiled against one exact letta-code release and pins the server image to it. Move between upstream tags only with `lettucectl upgrade <tag>`.
- **Shared app-server image.** All stacks share one image, whose contents depend on `PROFILES`. That is why profiles are global, and why `check` refuses a stack whose profiles differ.
- **Agent web apps.** Upstream's agent-served apps on ports 3000-3099 are not exposed: they are unauthenticated, and one host cannot publish the same range twice.
- **Data on disk.** A stack's data is only as private as the host. Whoever has root on it can read every stack.

## Licence
MIT, for the contents of this repository only. See [LICENSE](LICENSE).
