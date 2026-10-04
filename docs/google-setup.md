# Google for agents (optional)

The `google` profile gives each person's agents Gmail, Calendar, Tasks and Contacts through upstream's `google-mcp` sidecar. It is separate from the Google *login* in [cloudflare-setup.md](cloudflare-setup.md) step 4: that client only proves who someone is, while this one lets an agent read their mail.

You need:
- one Google OAuth client for the whole installation, with one redirect URI per stack;
- each person to connect their own Google account in their own stack.

Menu labels were checked against Google's docs in October 2026. Labels marked *(may differ)* could not be confirmed.

**Contents**
1. Turn the profile on
2. Google Cloud: enable the APIs
3. Google Cloud: check the consent screen
4. Google Cloud: create the OAuth client
5. Each person: connect in Lettuce

---

## 1. Turn the profile on
```bash
./lettucectl profiles                                  # shows the current list
./lettucectl profiles cloudflared,search,google        # your list plus google
```
`profiles` writes the list to `config.env` and every `stack.env`, rebuilds and restarts every stack, and prints each stack's redirect URI. Copy those URIs for step 4. Later, `add` prints the URI of each new stack and `remove` reminds you to delete it.

## 2. Google Cloud: enable the APIs
Use the project from cloudflare-setup.md step 4.1. Its consent screen is already set up, and every client in a project shares it.

1. Open the **navigation menu** (☰) -> **APIs & Services** -> **Library**.
2. Search for each API below, open it and click **Enable**. Enable only those you will allow:
   - **Gmail API**
   - **Google Calendar API**
   - **Google Tasks API**
   - **People API** (Contacts)

## 3. Google Cloud: check the consent screen
Go to **Google Auth Platform** -> **Audience**.

- **User type: External** is needed when anyone connects a personal `@gmail.com` account. **Internal** only works for accounts of the organisation that owns the project.
- **Publishing status: In production.** If you followed cloudflare-setup.md, it already is.
  - In **Testing**, Google ends every authorization 7 days after consent, refresh token included. Each person would have to reconnect weekly.
  - In production but unverified, Gmail and Drive scopes are "sensitive" or "restricted". Google shows an **unverified app** warning on the consent screen, and at most 100 accounts can connect. People continue past it with **Advanced** -> **Go to \<app name\> (unsafe)** *(may differ)*. Removing the warning takes Google verification, which for Gmail scopes includes a paid security assessment.
  - Publishing changes nothing for the Cloudflare login. That client asks only for the basic profile scopes, which need no verification.
- The consent screen shows the **App name** from **Branding**. People will see it when they connect, so pick a name they recognise.

Lettuce asks Google for exactly the scopes of the levels each person picks, and the consent screen lists them. This setup needs nothing under **Data Access** *(may differ)*.

## 4. Google Cloud: create the OAuth client
Create a new client; don't add these URIs to the Cloudflare login client:
- **Revoking is grant-wide.** Removing the app from a Google account drops everything it was granted. With one shared client, cutting an agent's Gmail access could also affect that person's sign-in.
- **The secret is kept in every stack.** A client of its own can be rotated without touching Cloudflare.

1. Go to **Google Auth Platform** -> **Clients** -> **Create client**.
2. **Application type:** **Web application**. **Name:** `lettuce-google`.
3. If the form asks whether the client will be used by an AI-powered agent, answer **yes**: it will. What this changes was not documented when this guide was written.
4. Under **Authorized redirect URIs**, click **Add URI** once per stack and enter the URIs from step 1, for example:
   - `https://alfred.example.com/api/google/oauth/callback`
   - `https://hal.example.com/api/google/oauth/callback`

   No JavaScript origins are needed.
5. Click **Create** and copy the **Client ID** and **Client secret**. Google shows the secret **only once**.

Changes on Google's side can take from 5 minutes to a few hours to apply.

## 5. Each person: connect in Lettuce
In their own stack, each person:
1. Opens **Settings** -> **Google**.
2. Enters the **OAuth client ID** and **OAuth client secret**. The form also shows the stack's redirect URI: it must match the one on the client exactly.
3. Picks a level per service, then clicks **Save** and **Connect**, and consents with the Google account this stack should use. Gmail goes `readonly` -> `organize` -> `drafts` -> `send` -> `full`; start low.
4. Clicks **Check with Google** to confirm the token works.

Whoever clicks **Connect** decides which Google account that stack reads. Mind what levels combine to: an agent that can create calendar events can invite any address, which sends mail even with Gmail read-only, and email the agent reads can carry instructions aimed at it.

The client ID, secret and token are stored in the stack's `google-policy` and `google-creds` Docker volumes, which only the BFF and the sidecar mount. Agents can't read them. Never put them in `stack.env`, `.secrets/` or the MCP settings.

## Troubleshooting
| Symptom | Look at |
|---|---|
| Google says `redirect_uri_mismatch` | The stack's URI is missing from the client, or differs from what Settings -> Google shows. Add it, then wait a few minutes. |
| "Google hasn't verified this app" | Expected for an unverified app in production; continue via **Advanced**. |
| Access stops working after about a week | The consent screen is in **Testing**. Publish it (step 3), then reconnect in Settings -> Google. |
| An agent's Google call fails saying an API is disabled or has not been used | That API isn't enabled in the project (step 2). |

To turn Google off, drop it from the list with `./lettucectl profiles`. That removes the `google-mcp` container from every stack, and you can delete the redirect URIs and the client. `lettucectl remove` deletes a stack's volumes, its Google token included; a backup of `stacks/` doesn't include them, so after a restore on another host each person connects again.
