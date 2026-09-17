# Approved schedule progress

Accepting a dated XER makes its matched activity percentages approved client schedule
observations. Existing accepted updates are included automatically; no re-upload or
migration is needed. The original baseline, relationships, zones and evidence records
remain intact. Unaccepted uploads never contribute to progress.

## One progress value

`actual_percent` in the comparison, summary and actual S-curve is resolved from
accepted client schedules and approved tour/manual observations. Original baseline
cost weights (duration fallback) remain the aggregation weights. A source filter in
the mobile Activity view filters activities by their current approved source; it does
not switch between competing project totals. The default is all sources.

- Activity matching uses external Activity IDs, not P6 internal numeric IDs.
- Newer approved observations can advance progress. Older observations uploaded
  later enrich history without replacing a newer approved observation.
- A reduction or differing percentage on the same project-local observation date
  is held for review. The previous approved value remains current.
- Explicit review can approve a correction or reject it. A later observation still
  takes precedence over a correction to an older date.
- Missing or uncomputable XER percentages do not erase earlier known progress.
  They appear as observations requiring review. Unknown activities retain the
  existing zero contribution in the project aggregate and are identified by
  `progress_known: false`, `unknown_progress_count`, and the source filter.
- Photo evidence is not required for approved client schedule progress. Source,
  observation date, acceptance/reviewer and prior evidence remain in history.
- The client forecast remains available in the XER card. The app forecast continues
  to use baseline relationships and the unified approved progress, evaluated at the
  comparison date. Tour coverage/AI confidence remain separate metrics.

## API and storage

Existing import/history/accept routes and administrator permissions remain unchanged:
`POST/GET /projects/{id}/schedule-updates` and
`POST /projects/{id}/schedule-updates/{update_id}/accept`.
Uploads retain the one-project/10 MB restriction, hash deduplication, reporting-date
validation and warning acknowledgement. Acceptance still requires a strictly newer
reporting date than the latest accepted file.

Comparison rows add `progress_source` (`client_schedule`, `tour`, `manual`),
`progress_as_of`, `progress_known`, and `progress_conflict`. The summary adds
`progress_basis: approved`, `progress_as_of`, and `unknown_progress_count`.
The previous `reported_*` fields remain for backwards compatibility and source detail.

Schedule observations are derived from existing snapshot rows; no fake tours or
copied evidence records are created. Their history IDs are
`scheduleupdate:{update_id}:{activity_id}`. Administrators review them through the
existing `PATCH /schedule-evidence/{evidence_id}` route. Decisions append audited
`progress_reviews` entries to the owner-scoped update without modifying source XER
percentages. To change a supplied XER percentage, record a separate manual observation.
For a tour/manual conflict the review payload includes `resolve_progress_conflict: true`;
ordinary manual entry defaults to false and cannot silently approve a regression.
Separate manual entries on the same day retain separate history records.

## Validation and deployment

Tests cover chronology, equal-date conflicts, reductions, explicit corrections,
rejection, future dates, timezone conversion, unknown values, owner access, history
preservation and equality of activity/summary/curve percentages. Mobile checks cover
source filtering, history, baseline/update cards, narrow layouts and reports.

Fozan reference values using the original baseline weights:
October data date 2024-10-27: 255 complete, 56 in progress, 87 not started, 68.972%.
November data date 2024-11-24: 280 complete, 49 in progress, 69 not started, 79.261%.
Newer approved site observations can make the current combined value differ from
these historical snapshots.

Deploy only the Main API, preserving its environment, mounts and network. Retain a
stopped rollback container. No client files are imported or accepted during deployment.
The mobile source change requires an updated installed app to remove the old switch.
