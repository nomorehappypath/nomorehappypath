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

## What this is not

No production app credential, no GitHub token, and no AI triage/repair run
here — this is Task 1 of 3 ("Receive and show") of the Dedicated Support
NMHP build. If this Support instance is down, the paired app is unaffected:
it keeps working and keeps its own local `ERROR_SINK_FILE` log.
