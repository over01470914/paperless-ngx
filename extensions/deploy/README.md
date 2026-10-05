# Paperless Radar deployment — revision 1

Baseline: upstream Paperless-ngx v3.2.1. Upstream `main` and `dev` remain unmodified; custom deployment and companion module live on `naya/article-intelligence`. Runtime data and secrets must never enter this public fork.

## Start

```sh
export PAPERLESS_SECRETS_FILE="$HOME/.config/paperless-ngx/secrets.env"
export PAPERLESS_RUNTIME_DIR="$HOME/Library/Application Support/paperless-radar"
docker compose -f extensions/deploy/compose.yml config --quiet
docker compose -f extensions/deploy/compose.yml up -d
```

Use a mode-0600 external env file containing POSTGRES_DB, POSTGRES_USER, POSTGRES_PASSWORD, PAPERLESS_DBPASS (same database password), PAPERLESS_SECRET_KEY and initial PAPERLESS_ADMIN_USER/PAPERLESS_ADMIN_PASSWORD. Generate random secrets; do not use examples as credentials. Admin startup variables do not reset an existing account password.

Native service: port 4386. Database and broker have no published host ports. HTTP is for trusted LAN/Tailscale only; no public forwarding or Funnel. Authentication remains required. Prefer VPN transport; plain LAN HTTP is not encrypted. Files are stored unencrypted on the local disk. The extension must not bypass native permissions. External AI is not enabled.

Use `docker compose -f extensions/deploy/compose.yml ps` and GET `/accounts/login/` as startup checks; authenticate and list actual documents before claiming functional readiness. Restart policies are `unless-stopped`; Docker Desktop itself must be running. Do not claim reboot acceptance without an actual reboot test.

## Backup and rollback

Create a new external dated backup folder and use the native exporter:

```sh
docker exec paperless-radar-webserver-1 python manage.py document_exporter /usr/src/paperless/export/backup-YYYYMMDD --no-progress-bar
```

Export includes sensitive metadata/accounts as well as source documents. Protect the export directory, copy it to a separate encrypted backup destination and separately secure runtime config/secret files. An on-disk export on the same Mac is a recovery snapshot, NOT an off-device backup. Validate manifest, originals and document counts. Restore should be tested into a separate throwaway instance with `document_importer`, never over the active instance.

Stop only this project's containers:

```sh
docker compose -f extensions/deploy/compose.yml stop
```

Do not use `down -v` unless deleting all archived documents is explicitly approved. The original WeChat article library and its scheduled maintenance remain untouched.

## Upstream upgrades

```sh
git fetch upstream --tags
git switch main  # create a tracking branch if absent
git merge --ff-only upstream/main
git push origin main
```

Keep custom work on its own branch; rebase/merge a chosen stable upstream release only after an external backup, compatibility checks, focused migration/proxy tests and review. Pin the Docker image version to that release; a fork does not automatically alter an official container image. The companion uses native API v10 with its own v0.1.0 contract, so no custom Paperless core build is required. Do not automatically deploy upstream `dev` or turn on unattended upgrades.
