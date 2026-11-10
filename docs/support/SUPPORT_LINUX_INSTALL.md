# Installing a Support instance on Linux

One working setup, documented and checked by `scripts/check_support_install.sh`.
This is a dedicated, second Linux server — not the app's own server.

## 1. Install NMHP normally

```bash
git clone https://github.com/nomorehappypath/nomorehappypath.git
cd nomorehappypath && bash install.sh
```

Choose "Run once in the foreground" when prompted, or Ctrl-C the auto-start
service afterwards — Support mode uses its own systemd unit (below), not the
one `install.sh` offers for the normal manager.

## 2. Run it in Support mode

```bash
python3 -E harness/project_manager.py --support \
  --home "$HOME/.harness-home-support" \
  --host 0.0.0.0 --port 8742 --support-admin-port 8741
```

- `--host 0.0.0.0 --port 8742` is the **public** receiver: intake and
  `/healthz` only. Put it behind a reverse proxy with a real TLS certificate
  (step 4); never expose it directly without one.
- `--support-admin-port 8741` is the **owner-only** case-card page, always
  bound to `127.0.0.1`. Reach it only through an SSH tunnel:
  `ssh -L 8741:127.0.0.1:8741 you@support-server`, then open
  `http://127.0.0.1:8741/` on your own computer.

## 3. systemd unit (keeps it running)

Copy `docs/support/nmhp-support.service` to
`~/.config/systemd/user/nmhp-support.service`, edit `WorkingDirectory` to
your clone's path, then:

```bash
systemctl --user daemon-reload
systemctl --user enable --now nmhp-support.service
loginctl enable-linger "$USER"   # keeps it running after you log out
```

## 4. Reverse proxy (TLS for the public receiver only)

Copy `docs/support/nmhp-support-nginx.conf` into your proxy's config,
pointing `server_name` at your Support server's real domain name, and use
your own certificate (certbot or your provider's). It forwards only to the
public receiver port (8742); the admin port (8741) is never proxied — it
stays loopback-only.

## 5. Check it

```bash
bash scripts/check_support_install.sh
```

Starts Support mode briefly on a scratch port, confirms `/healthz` answers
and the admin page is loopback-only, confirms the systemd unit template and
the nginx template are both present and well-formed, then stops it. It is a
functional check, not an installer: it does not create the systemd unit or
edit your proxy for you.

## 6. Owner key and the agents' sign-ins

The Support page is read-only until you sign in. The very first visit to `/support`
asks you to choose your own owner key (at least 12 characters) instead of showing you
one: type it into the "Choose your owner key" form and press Choose. Support never
generates, prints or logs this key anywhere — only its one-way hash is stored, and only
you know the key itself, so keep it somewhere safe. Every visit after that asks you to
sign in with it, and settings, pairing and Deploy all need that sign-in. Lost it? Delete
`support_owner_key.sha256` in the Support home folder and restart Support; the next
visit to `/support` asks you to choose a new one.

Repairs are made by Support's own Claude agent and checked by a Codex reviewer, both
running on this computer with their own sign-ins (never your app's credentials):

```bash
claude setup-token                              # in your own terminal, once
python3 -m harness.claude_auth store-token      # hidden prompt; saved owner-only, survives reboots
codex login                                     # the independent reviewer's own sign-in
```

Without them a case still reaches Replicated, and its card says in plain words which
sign-in is missing instead of pretending a repair was tried.

## 7. Settings (repository, GitHub credential, notice email, spending ceiling)

Sign in on the case-card page (`/support`) with the owner key, then save under **Support settings**: the
app's GitHub repository, a GitHub credential scoped to that one repository
(push `support/` branches, open and merge pull requests), your notice email,
the mail server, and a positive spending ceiling. Without a positive
ceiling, Support stays receive-and-show only — it collects and shows cases
but never checks out the app, replicates, or repairs anything. See the
README's **Support** chapter for the full Received → Replicated → Fixed →
Ready to deploy → Deployed flow and what Support never does.

## What this is not

If this Support instance is down, the paired app is unaffected: it keeps
working and keeps its own local `ERROR_SINK_FILE` log. Support never logs
into the app's own server, never holds trading, payment or production
database credentials, and never deploys by itself — pressing **Deploy**
only merges the reviewed pull request; the app's existing GitHub deployment
does the rest.
