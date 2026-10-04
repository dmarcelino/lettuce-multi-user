# Vault

Each stack can have a personal vault of secrets: addresses, company details, notes, loyalty numbers, ID documents, payment cards and logins. Agents can ask to use a secret, for example to fill in a delivery form or a booking, and the person decides. Most secrets are a single private text box: agents read natural language, so an address is simply typed the way you would write it. The vault is encrypted with a passphrase only that person knows. It is opt-in, behind the `secrets` profile.

```
agent ──list_secrets─────────────────────> vault   names, types, public details; never private values
agent ──request_secret(secret, why)───────> vault   ──push──> your phone
you   ──https://<stack>/secrets/ ───────> vault   see what and why, untick fields, Share once / Deny
agent ──get_result──────────────────────> vault   the values you agreed to share, once
```

## For the person using it
1. **Open `https://<your stack>/secrets/`** and sign in as usual.
2. **Create your vault:** choose a passphrase of at least 12 characters. Four or five unrelated words work well. **Nobody can recover it**, not even whoever runs the server. If you forget it, you have to erase the vault and start again.
3. **Add secrets:** go to **Secrets**, then **Add a secret**, and pick a type. Every secret has a name. Fields in *italics* are private; the others agents can read, like the name.

   | Type | Fields | Can be used without asking |
   |---|---|---|
   | Note | *text* | yes, if you choose |
   | Address | *text* (e.g. name, street, postal code, city, country, phone) | yes, if you choose |
   | Company | *text* (e.g. company name, VAT ID, registration number, address) | yes, if you choose |
   | Membership / loyalty | programme, *member number*, *notes* (optional) | yes, if you choose |
   | ID document | document type, *document number*, *notes* (optional) | no, always asks |
   | Payment card | name on card, *card number*, expiry, *security code*, *notes* (optional) | no, always asks |
   | Login | website (optional), username, *password* | no, always asks |

   - **Private fields** stay hidden on screen until you press **Show private fields**, which is recorded in History, and an agent gets them only when you agree.
   - **Name:** give each secret a name you and your agents will recognise, such as "Home address" or "Work Visa".
   - **"What agents may use it for":** an optional note that agents see, for example "online shopping deliveries".
4. **Get notified:** on your phone, add the page to the Home Screen (on iPhone: Share, then Add to Home Screen), open it from there, go to **Notifications**, then **Enable on this device**.
5. **Answer requests:** when an agent asks for something, a notification arrives. It shows the secret, the private fields the agent wants and the reason it gave. Untick anything it doesn't need, then press **Share once** or **Deny**. **Requests** lists what is waiting and what you answered recently; **History** lists everything.
6. **Locking:** the vault locks when the server restarts, after 12 hours without you using the page, or when you press **Lock**. While it is locked, agents get nothing and you get a notification asking you to unlock.

**Connect your agents:** in Lettuce, go to **Settings -> MCP** and add an HTTP server named `vault` with URL `http://secret-broker:8000/mcp`.

## What is protected, and what is not
- **The vault file is encrypted.** Each secret is encrypted with AES-256-GCM using a random data key. That key is stored only wrapped with a key derived from your passphrase (scrypt, 128 MiB). Secret names, fields and the history details are all encrypted. Someone who copies the vault file or a backup sees only secret ids, types and timestamps, and can only try guessing your passphrase, which scrypt makes slow.
- **The key is only in memory:** from when you unlock until you lock, the 12-hour auto-lock, or a restart. Agents asking for secrets do not keep the vault open.
- **Agents never get a private value without your say-so:**
  - Every page, every unlock and every approval requires your Cloudflare Access sign-in, which the vault itself checks. Agents can reach the vault service but cannot sign in as you.
  - Agents cannot add, change or delete secrets.
  - ID documents, payment cards and logins always ask. Notes, addresses, companies and memberships ask unless you switch that secret to "Agents may use it without asking".
  - Public fields (programme, document type, name on card, expiry, website, username) and the name are visible to agents, so they can pick the right secret.
- **Once shared, a value is in the conversation.** Whatever you share goes to the agent. The AI provider sees it, and Lettuce stores the conversation unencrypted on the server. Share only what the task needs.
- **The vault doesn't protect against a determined administrator:** someone with root on the server can read the vault service's memory while it is unlocked, or change its code. What it does stop is someone browsing the server's files or backups.
- **Cloudflare sees your passphrase in transit,** as it sees all traffic to your Lettuce stack.

## For the operator
- **Turn it on:**
  ```bash
  ./lettucectl profiles <current list>,secrets      # e.g. cloudflared,search,secrets
  ```
  This routes `https://<stack>/secrets/` to the vault on every stack, using the same hostname and Access app with no new DNS record, then builds and starts it. Each stack gets its own push keys for the vault (`BROKER_VAPID_*` in `stack.env`). Dropping `secrets` from the list removes the route and the container, and leaves the data where it is.
- **Data:** the vault lives in the Docker volume `lettuce-<name>_secret-broker-data`, not under `stacks/<name>/`, so ordinary stack backups do not include it. To keep it, back up the volume as well (see [operations.md](operations.md#backups)). The backup is useless without the passphrase.
- **Removal:** `lettucectl remove <name>` deletes the volume, so removing a stack deletes its vault.
- **Forgotten passphrase:** there is no recovery. The person can erase the vault in Settings. If they can't unlock to do that, delete the volume (see the troubleshooting table in [operations.md](operations.md#troubleshooting)).
- **Isolation:** the vault container has a read-only root filesystem and no capabilities, runs as a non-root user and has no host mounts. Its volume is mounted by nothing else. `lettucectl check` refuses a stack where any of that is not true.
- **Rules for changing it:** never add a tool that lets agents edit secrets or skip approval, and never let a value or key reach a log or an unencrypted file.

## Limits
- At most 5 requests can wait at once. A request expires after 10 minutes, and shared values wait 30 minutes for the agent to collect them.
- Requests that are waiting are forgotten when the vault restarts. The agent simply asks again.
- iOS delivers notifications only to web apps added to the Home Screen, and only on iOS 16.4 or later.
