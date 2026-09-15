# Shared notifications for mobile and Next.js web

Updated: 2026-09-15. Status: Main API deployed; live push acceptance blocked by missing Firebase credentials.

## Contract additions

All endpoints require the existing authenticated Main API session.

| Endpoint | Behavior |
| --- | --- |
| `GET /notifications?include_archived=true` | Includes active and archived records for the recipient. Default excludes archived records. |
| `GET /notifications/unread-count` | Counts unread, non-archived recipient records, regardless of business status. |
| `POST /notifications/{id}/unread` | Clears read state and read timestamp after recipient ownership verification. |
| `POST /notifications/{id}/archive` | Sets `is_archived`, preserving invite/business status and read state. |
| `POST /notifications/{id}/unarchive` | Clears archive state; accepted/rejected invitations keep their decision. |
| `POST /notifications/mark-all-read` | Marks the current recipient's active unread records as read. |
| `POST /notifications/unregister-device` | Deactivates the exact FCM token for the authenticated user and app, with no upsert. Other accounts/devices are unaffected. |

Device-unregister JSON: `{"fcm_token":"...","app":"main"}`.
The existing register endpoint is unchanged. Serialization now includes
`is_archived`. Push data preserves available tour, comment, inspection, activity,
panorama and node IDs so the mobile tap opens the same destination as the inbox.

The current Next.js client already expects the unread/archive/restore/mark-all
endpoints. No web source changes are required for these additions.

## Validation

`python -m unittest discover -s tests -p test_notification_parity.py -v`
passes 8 tests. They execute the affected functions with mocked database/push
dependencies and cover ownership, count filters, state changes, archive status
preservation and evidence payload fields. Modified Python files also compile.
These checks do not verify a deployed MongoDB/Firebase connection.

## Rollout and acceptance

Deploy backend first, then release the mobile update. With the same account open
on web and mobile, test read/unread, archive/restore, mark-all-read, badge count
and invitation acceptance/rejection. Use a second account to verify foreign
notification IDs and device tokens cannot be modified.

Verify FCM and signed APNs delivery, exact tour/comment and Progress destinations,
permission denial, logout, account switching and offline cleanup. Device deletion
and server deactivation are independent attempts; if both fail offline, remote
delivery cleanup requires restored connectivity. Mobile foreground display is
gated immediately.

Budget/Materials/Activity event producers and production verification of the
existing event generators remain follow-up work. Endpoint and routing support
alone do not generate those events.

## VPS rollout — 2026-09-15

- Source commit `f8316df` was pushed to both `origin/main` and `client/main`.
- Main API deployed as `conscout-backend-api:f8316df`; the prior container is
  retained stopped for rollback. Existing environment, mounts, network and
  restart configuration were preserved.
- All 32 focused notification, safety and workforce tests passed locally and
  inside the production image with mocked data and no production DB access.
- Public Main/Lite health, Main database ping, Main-to-AI health, and all five
  new notification action routes passed read-only checks.
- Applied the project upload nginx configuration (256 MB); nginx validation
  and reload succeeded.
- Existing VPS Dockerfile.ai customization and deployment-script permissions
  were preserved. AI and Lite containers did not require replacement for this
  Main API change.

### Remaining push activation blocker

The running Main API has the Firebase Admin SDK, but its configured
`FIREBASE_CREDENTIALS_FILE=/secrets/firebase-adminsdk.json` does not exist inside
its container. Firebase initialization fails. No matching credential was found
in the checked backend, storage or standard secret directories on the VPS.

Provision the correct Firebase Admin service-account credential securely outside
Git and the Docker build context, bind-mount it read-only at the configured
container path, and recheck Firebase initialization. Then verify the iOS APNs
key/provisioning and test authorized delivery on Android and a signed iPhone
build (foreground, background, cold launch, denied permission and logout).
Registered device records alone do not prove delivery. No test push was sent
as part of this deployment.
