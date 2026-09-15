# Shared notifications for mobile and Next.js web

Updated: 2026-09-10. Status: implemented locally, not deployed.

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
