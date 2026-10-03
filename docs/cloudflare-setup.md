# One-time Cloudflare and Google setup

You do this once per installation. Afterwards each person is one `lettucectl add`.

Placeholders used below:
- `example.com`: your domain.
- `<team>`: the Zero Trust team name you choose in step 3.

Menu labels were checked against the Cloudflare and Google docs in October 2026. Labels marked *(may differ)* could not be confirmed. Dashboards change, so if a label is not where this guide says, use the dashboard's search box: both Cloudflare's and Google's find pages by old and new names.

**Contents**
1. Create the Cloudflare account
2. Move the domain's DNS to Cloudflare (the only step that can break something)
3. Enable Zero Trust
4. Add Google as a login method
5. Create the API token for lettucectl
6. Initialise and add people

---

## 1. Create the Cloudflare account
1. Go to https://dash.cloudflare.com/sign-up and sign up with an address you will keep. Verify it from the email Cloudflare sends.
2. Turn on two-factor authentication:
   1. Click the **profile icon** (top right), then **My Profile**.
   2. Open **Authentication**.
   3. Under **Two-Factor Authentication**, set up an authenticator app (or a security key).
   4. Store the backup codes somewhere safe.

   This account will control your domain's DNS and who can reach every stack.

---

## 2. Move the domain's DNS to Cloudflare
Cloudflare Tunnel hostnames need a zone whose DNS Cloudflare serves. On the free plan that means **changing the domain's nameservers** at your registrar. The domain stays registered where it is; only the DNS service moves.

If the domain already serves a website or email, one forgotten record breaks it: mail gets lost or rejected. The process below avoids that: during the switch, the old and new providers answer identically. A domain with no records yet can skip 2.1, 2.4 and 2.6.

### 2.1 Inventory every record at your current DNS host
Do this **before** touching Cloudflare.

1. Find your current nameservers: `dig +short NS example.com`. They tell you who serves the DNS today: usually your registrar, sometimes a separate DNS host.
2. Sign in there and open the domain's DNS records page (often called "DNS records", "DNS management" or "Zone editor").
3. Save the whole table: use an export or zone-file download if there is one, otherwise screenshot it or copy it into a text file.

Some providers stop showing this page once the nameservers point elsewhere, so save it now.

Check the list for:
- **Mail:** `MX` records, the SPF `TXT` (`v=spf1 ...`), DKIM records (`<selector>._domainkey`, TXT or CNAME), and `_dmarc`.
- **Service records:** CNAMEs and SRV records that point at the services you use (website, mail, calendar, chat).
- **Verification records with random names or values:** TXT records like `<provider>-verification=...` and CNAMEs with random-looking names. Nothing can guess these names, so they exist only in this list.

### 2.2 Check DNSSEC
Run:
```bash
dig +short DS example.com @1.1.1.1
```
- **No output:** DNSSEC is off. Continue.
- **Any output:** turn DNSSEC off at the registrar, wait 24-48 h, and run the check again. A leftover DS record makes the whole domain stop resolving after the switch.

### 2.3 Add the domain to Cloudflare
1. In the Cloudflare dashboard, click **Domains** in the left sidebar.
   - Choose **Connect** (an existing domain; it stays at your registrar).
   - **Transfer** would move the registration to Cloudflare Registrar, and **Buy** registers a new domain.
   - Cloudflare's docs call this button "Onboard a domain"; older screens said "Add a site" or "Add a domain".
2. Type the bare domain, `example.com` (no `www`, no `https://`).
3. For how to add DNS records, choose **Quick scan for DNS records** *(may differ)*, then **Continue**.
4. Choose the **Free** plan, then **Continue**.
5. Cloudflare shows the records it found. **Don't continue yet.** Do 2.4 first, on this screen or later on the domain's **DNS -> Records** page.

### 2.4 Make the Cloudflare zone identical to your inventory
Work through your inventory line by line.

- **Missing record:** click **Add record**, fill in **Type**, **Name** (`@` for the domain itself, otherwise only the left part, e.g. `mail`), **Content** / **Target** and, for MX, **Priority**, then **Save**.
- **Wrong record:** click **Edit** on its row, fix it, then **Save**.
- **Extra record** (found by the scan but not in your inventory): click **Edit** -> **Delete**.
- **Proxy status:** A, AAAA and CNAME rows have a **Proxy status** toggle. Set **every** one to **DNS only** (grey cloud). Proxied (orange cloud) changes how the name behaves and often breaks records that point at third-party services. MX and TXT records have no toggle; they are always DNS only.
- **Long TXT records (DKIM):** open **Edit** and compare the **Content** with your inventory character by character. Scans often truncate or miss them.
- **TTL:** leave it on **Auto**.

**Do not** enable anything email-related. In particular:
- Never click **Onboard Domain** under **Compute -> Email Service -> Email Routing** (zone-level **Email -> Email Routing** on older screens). Email Routing replaces your MX records.
- Ignore any "fix your SPF/DMARC" prompts during the move.
- Leave DNSSEC off; you can enable it later in **DNS -> Settings**.

Change one thing at a time.

### 2.5 Note the assigned nameservers
1. Finish the onboarding screens (**Continue**). Cloudflare shows **two nameservers** assigned to your account, of the form `<name>.ns.cloudflare.com`.
2. They also stay visible on the domain's **Overview** page.
3. Don't change anything at the registrar yet.

### 2.6 Compare before switching
From the directory you cloned this repo into, compare your current nameserver with one of the Cloudflare ones. Add every random-named record from your inventory as an extra name:
```bash
scripts/dns-preflight.sh example.com <current-ns> <cloudflare-ns> <extra names...>
```
`<current-ns>` is one of the nameservers from 2.1 and `<cloudflare-ns>` one of the two from 2.5.

Fix differences in Cloudflare (step 2.4) and repeat until the last line says `0 difference(s)`.

### 2.7 Switch the nameservers at the registrar
1. Sign in at your registrar (where the domain is registered) and open the domain's settings.
2. Find the nameserver setting, usually called "Nameservers" or "DNS servers". If it offers a choice, pick custom nameservers.
3. Replace all existing nameservers with the two Cloudflare ones and leave no others.
4. Save.

Don't use a "registered nameservers", "child nameservers" or "glue records" page: that is for running your own nameserver hosts.

A domain lock does not stop this; it only blocks transfers.

**Rollback:** set the nameservers back to the old ones. Don't delete anything at the old provider until Cloudflare has been active for a few days.

Do the switch at a quiet time, not right before you need email.

### 2.8 Wait for "Active", then verify
1. Back in Cloudflare, check the domain's **Overview** page. Cloudflare re-checks automatically. You can trigger a check there, and it emails you when the status changes from **Pending Nameserver Update** to **Active**. That takes minutes to hours; delegation can take up to 48 h. Both providers answer identically in the meantime, so nothing breaks.
2. Once it is Active, run:
   ```bash
   scripts/dns-preflight.sh example.com <old-ns> 1.1.1.1 <extra names...>
   scripts/dns-preflight.sh example.com <old-ns> 8.8.8.8 <extra names...>
   ```
3. If the domain has email: send a message to an address on the domain from an outside account, and reply to it. If your mail provider's admin page shows the domain's DKIM or authentication status, check that it is still verified.

---

## 3. Enable Zero Trust
1. In the Cloudflare dashboard's left sidebar, click **Zero Trust**. This opens the Zero Trust dashboard (also at https://one.dash.cloudflare.com).
2. On the onboarding screen, enter a **team name**. It becomes `<team>.cloudflareaccess.com`: the address of the sign-in page people see, and part of the Google redirect URI in step 4. Choose something neutral you won't want to change.
3. Choose the **Zero Trust Free** plan and enter payment details. Cloudflare requires them even for the $0 plan; you are not charged. The plan covers up to 50 users.
4. Finish onboarding. Cloudflare's own one-time-PIN login is added as a default login method. That's fine: lettucectl restricts each application to Google.

You can view the team name later under **Settings** -> **Team name and domain**. Older screens put it under **Custom pages**. Renaming the team later means updating the Google redirect URIs too.

---

## 4. Add Google as a login method
You need a Google OAuth client, then you register it in Cloudflare. Have `<team>` from step 3 at hand.

### 4.1 Google Cloud: create a project
1. Go to https://console.cloud.google.com and sign in with a Google account you will keep.
2. Click the **project picker** in the top bar, then **New project**.
3. **Project name:** `cloudflare-access`.
4. **Organization / Parent resource:** leave the default ("No organization" for a personal account).
5. Click **Create**, wait for the notification, then make sure the new project is selected in the project picker.

### 4.2 Google Cloud: consent screen
1. Open the **navigation menu** (☰, top left) and select **Google Auth Platform** -> **Branding**. Older screens: **APIs & Services** -> **OAuth consent screen**.
2. Click **Get started**, then work through the wizard:
   1. **App information:** **App name** `Cloudflare Access` (shown on Google's sign-in screen), and your address as **User support email**. Click **Next**.
   2. **Audience:**
      - **External** lets anyone with a Google account reach the sign-in screen; Cloudflare Access still decides who gets in. Choose this.
      - **Internal** is offered only when the project belongs to an organisation, and admits only that organisation's accounts.

      Click **Next**.
   3. **Contact information:** your address. Click **Next**.
   4. **Finish:** tick the agreement to the Google API Services User Data Policy, then **Continue** -> **Create**.
3. No scopes need adding. The defaults (`openid`, email, profile) are all Cloudflare uses, so skip **Data Access**.
4. External only: go to **Google Auth Platform** -> **Audience** and click **Publish app**. The status becomes **In production**.
   - Apps with only these basic scopes need no Google verification.
   - Don't upload a logo: that triggers brand verification.

### 4.3 Google Cloud: create the OAuth client
1. Go to **Google Auth Platform** -> **Clients** -> **Create client**. Older screens: **APIs & Services** -> **Credentials** -> **Create credentials** -> **OAuth client ID**.
2. **Application type:** **Web application**. **Name:** `cloudflare-access`.
3. Under **Authorized JavaScript origins**, click **Add URI** and enter:
   `https://<team>.cloudflareaccess.com`
4. Under **Authorized redirect URIs**, click **Add URI** and enter:
   `https://<team>.cloudflareaccess.com/cdn-cgi/access/callback`
5. Click **Create**.
6. **Copy both the Client ID and the Client secret now** (or click **Download JSON**). Google shows the secret **only once**; afterwards only its last 4 characters are visible. If you lose it, open the client and use **Add secret**.

Changes on Google's side can take from 5 minutes to a few hours to apply.

### 4.4 Cloudflare: register Google
1. In Zero Trust, go to **Integrations** -> **Identity providers**. Before Nov 2025 this was **Settings -> Authentication -> Login methods**.
2. Under **Your identity providers**, click **Add new identity provider**, then **Google**.
3. **Name:** `Google`. **App ID:** the Google Client ID. **Client Secret:** the Google Client secret.
4. Optionally turn on **Proof of Key Exchange (PKCE)**.
5. Click **Save**.
6. Back in **Integrations** -> **Identity providers**, click **Test** next to Google. A window should sign you in with Google and show your email and a success message. If it fails, re-check the two URIs in 4.3 (`<team>` spelled exactly), or wait a few minutes for Google to apply them.

---

## 5. Create the API token for lettucectl
1. In the Cloudflare dashboard, click the **profile icon** -> **My Profile** -> **API Tokens**.
2. Click **Create Token**. At the bottom, under **Create Custom Token**, click **Get started**.
3. **Token name:** `lettucectl`.
4. **Permissions:** each row has three dropdowns (scope, item, level); **+ Add more** adds a row. Create these five:

   | 1st dropdown | 2nd dropdown | 3rd dropdown |
   |---|---|---|
   | Account | Cloudflare Tunnel | Edit |
   | Account | Access: Apps and Policies | Edit |
   | Account | Access: Organizations, Identity Providers, and Groups | Read |
   | Zone | Zone | Read |
   | Zone | DNS | Edit |

   - The dashboard says **Edit**; Cloudflare's API docs call the same level "Write".
   - If **Cloudflare Tunnel** is missing, use **Cloudflare One Connector: cloudflared**.
5. **Account Resources:** **Include** -> your account.
6. **Zone Resources:** **Include** -> **Specific zone** -> `example.com`.
7. Optional:
   - **Client IP Address Filtering:** restrict to your host's public IP. The token is useless elsewhere, but it breaks if that IP changes.
   - **TTL:** an end date, if you want the token to expire.
8. Click **Continue to summary**, check the list, then click **Create Token**.
9. **The token is shown only once.** Without pasting it into any chat or file, store it on the host from the directory you cloned this repo into. The command doesn't echo it, and the token never reaches your shell history:
   ```bash
   mkdir -p .secrets && chmod 700 .secrets
   (umask 077 && read -rs -p 'Cloudflare API token: ' T && printf '%s\n' "$T" > .secrets/cloudflare-api-token)
   ```
   Then close the Cloudflare page.

This is a *user* token. An account-owned token (**Manage Account -> Account API Tokens**) with the same permissions also works with lettucectl.

---

## 6. Initialise and add people
```bash
./lettucectl init        # first run creates config.env: set DOMAIN, PROFILES, TZ; then run init again
./lettucectl add alfred alfred@gmail.com
```
`add` uses `<name>.example.com`. When that name already exists in DNS, it uses `lettuce-<name>.example.com` instead (`HOSTNAME_FALLBACK`). Stack names are a single DNS label on purpose: Cloudflare's free certificate covers `*.example.com` but not `a.b.example.com`.

Check it works:
- `curl -sI https://alfred.example.com` answers `302` with a `location:` on `https://<team>.cloudflareaccess.com/...`.
- In a private browser window you get Google sign-in, then Lettuce.
- Signing in with an account that is not on that stack's list ends on Cloudflare's "access denied" page.
