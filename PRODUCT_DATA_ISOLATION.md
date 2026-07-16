# Main/Web and Lite product isolation

The system is split into two independent product boundaries:

| Product | Clients | `APP_SURFACE` | MongoDB database | Storage root |
| --- | --- | --- | --- | --- |
| Main | Web and Main mobile | `main` | `construction_ai` during the first cutover | `/app/data-main` |
| Lite | Lite mobile only | `lite` | `construction_ai_lite` | `/app/data-lite` |

Deploy the same backend source twice using separate environment files. The
server chooses its product from `APP_SURFACE`; the `app` property submitted by
a client is validated and cannot choose a database.

## Deployment

1. Keep the existing `construction_ai` database as the Main/Web database for
   the first cutover. Create `construction_ai_lite` for Lite, and create two
   least-privilege MongoDB users. Each backend user must have access only to
   its own database.
2. Deploy the Main backend with `.env.main.example` values, using the current
   web/API hostname.
3. Deploy a second Lite backend with `.env.lite.example` values, exposed at
   `https://lite-api.conscout.com` or another HTTPS endpoint.
4. Point the Lite Flutter build to that Lite endpoint:

   ```powershell
   flutter build appbundle --dart-define=API_BASE_URL=https://lite-api.conscout.com --dart-define=LITE_WEB_BASE_URL=https://lite.conscout.com --dart-define=INVITE_BASE_URL=https://lite.conscout.com
   ```

5. Keep the Web Cloudflare `/api/*` proxy pointed only at the Main backend.
6. Confirm `GET /health` returns `{"status":"ok","product":"main"}` for
   Main and `{"status":"ok","product":"lite"}` for Lite before releasing.

## Data migration

Do not move only `users`. A user account and all of its records must be moved
together: `sites`, `tours`, `work_schedules`, `inspections`, `notifications`,
`notification_devices`, subscription records, and that user's uploaded file
folder under `DATA_DIR`.

Back up the existing database and files before migration. The old shared
database becomes the Main/Web source of truth during the first cutover, so
copy—not move—the selected Lite records into `construction_ai_lite` until
verification is complete. It does not reliably identify which projects are
Lite because historical accounts were assigned both applications. Use an
approved owner/project list to select Lite records. Also migrate every
stakeholder who needs access to a migrated Lite project, or remove that
stakeholder email from the project.

## Safety rules

- Do not run `ENABLE_LEGACY_BOOTSTRAP_MIGRATION` on either new deployment.
- Lite refuses to start if `DB_NAME=construction_ai`, preventing an accidental
  connection to the old shared database.
- Lite mobile defaults to `https://lite-api.conscout.com`; it no longer
  defaults to the Main/Web API.
