# Dated XER progress updates

Status: implemented locally on 2026-09-17; backend deployment and installation of
the updated mobile build have not been performed. AI completion estimation is
outside this change.

## Workflow

With an approved XER baseline active, open Progress > Schedule > Upload schedule
update. Select an XER, review its reporting date, matching activities, completion
counts, forecast and warnings, then accept. Closing review keeps the upload in
Update history without changing progress. History permits later review and
acceptance of eligible updates. Older/equal reporting dates and updates attached
to another baseline remain read-only.

Progress > Activity defaults to Reported when an accepted update exists. The
Verified switch shows the original approved tour/manual progress. Activity
details display reported status/date beside verified progress and evidence.
Differences are flagged with a reminder to compare observation dates.

## Sources and calculations

- Baseline dates, calendars, relationships, activity mappings and cost weights
  remain unchanged. Updates match on Activity ID, not the P6 internal numeric ID.
- Each update stores its own snapshot. Tour/manual evidence remains attached to
  the original baseline activity, so it is available alongside every update;
  evidence is neither copied nor overwritten.
- P6 `TK_Complete`/`TK_NotStart` yield 100%/0%. In-progress percentage follows
  `CP_Drtn` (original versus remaining duration), `CP_Phys`, or `CP_Units`
  (actual units divided by actual plus remaining units). Status counts follow
  P6 status independently of percentage, including in-progress activities at 0%.
- Overall progress uses original-baseline cost weights, or duration weights
  when cost loading is absent. Activity view uses these same weights.
- Missing/unmatched activities are explicit. New IDs do not expand the baseline
  silently. Missing or uncomputable percentages make overall reported completion
  unavailable instead of substituting verified progress or zero.
- Reporting date uses P6 `last_recalc_date`, with `last_schedule_date` fallback.
  Reported-versus-planned rows use the baseline plan at that reporting date.
- Existing verified analytics, verified S-curves, notifications and verified
  progress reports retain their meaning. The Schedule updates panel shows the
  separately imported client forecast; it does not replace the verified forecast.

## Backend contract

The backend adds owner-scoped `schedule_updates` records and indexes during its
normal startup index initialization. No existing project data migration is needed.
Source XERs are stored below the site's baseline/updates directory.

| Method | Endpoint | Result |
| --- | --- | --- |
| POST | `/projects/{id}/schedule-updates` | Multipart XER import; returns `update` awaiting review |
| GET | `/projects/{id}/schedule-updates` | Metadata/history, active baseline and latest accepted update ID |
| POST | `/projects/{id}/schedule-updates/{update_id}/accept` | Accept with `acknowledge_warnings` |
| GET | Existing schedule analytics/comparison endpoints | Additive `reported_update` and reported fields on matching activity rows |

Existing `actual_percent` remains verified. New row fields include
`reported_percent`, `reported_status`, `reported_as_of`,
`reported_planned_percent`, `progress_difference` and `progress_weight`.
All existing mobile response copies preserve these fields and evidence.

Uploads/acceptance require an authenticated administrator; history is readable
through existing scoped project access. Imports are limited to one project and
10 MB, deduplicated by baseline/hash, and checked for duplicate Activity IDs,
missing reporting dates and project mismatches. Invalid actual dates, renamed
activities, changed relationship counts and filename/data-date differences appear
in review. Warnings require acknowledgement. The latest accepted reporting date
determines displayed progress even when acceptance requests race.

## Verification and rollout

Backend tests exercise parsing, review/acceptance, idempotency, old-date rejection,
baseline changes, evidence preservation, missing IDs, actual-date warnings,
HTTP upload/history, administrator restrictions and both private Fozan files.
Mobile tests exercise Reported/Verified switching, weighted completion, copied
evidence, incomplete coverage, warning acknowledgement, history review, existing
schedule behavior and narrow-width rendering.

Fozan reference results against the supplied original baseline:

| Snapshot | Reporting date | Completed / in progress / not started | Cost-weighted reported completion |
| --- | --- | --- | --- |
| October XER | 2024-10-27 | 255 / 56 / 87 | 68.972% |
| November XER | 2024-11-24 | 280 / 49 / 69 | 79.261% |

November's future actual start on `FAW.CONS.2116` remains visible as a warning;
the import does not silently repair client data. Confirm corrections with the
client. Same-day replacement/corrected updates are currently rejected; the
workflow accepts strictly newer reporting dates.

Deploy the backend first, then build/install the updated mobile app. On a test
project, import October and November in order, accept each, refresh/reopen Activity,
and compare both sources. Verify the original baseline and existing tour/manual
records remain available. Do not import client files into production automatically
as part of deployment.

```text
# Backend repository
python -m pytest tests/test_schedule_updates.py tests/test_schedule_baseline.py tests/test_project_asset_deletion.py -q

# Mobile repository
flutter test --no-pub test/schedule_updates_test.dart test/activity_web_workspace_test.dart test/progress_schedule_models_test.dart test/progress_schedule_baseline_card_test.dart test/progress_arabic_test.dart
```
