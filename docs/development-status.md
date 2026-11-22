# Stable baseline and development snapshots

The default branch contains the exported deployed runtime and the offline
source-distribution tests. The source export does not activate it on a machine.

Unmerged local code is preserved on the `work/local-candidate-snapshots-20261120`
branch, under `snapshots/`. Each snapshot has a public status/provenance file;
none is represented as an accepted upgrade of the stable baseline.

| Snapshot | Qualification boundary at export |
|---|---|
| `tracking-rectification` | Source/fixture candidate qualified; strict real-record historical linkage gate not satisfied |
| `execution-efficiency` | Source remediation/requalification in progress; actual native benchmark remains incomplete; failed claim retained privately |
| `agy-acpx-integration` | Software candidate qualified; real coding demo failed/unknown; additional native trial and actual additive delivery remain pending |
| `legacy-managed-mailbox` | Original dirty maintenance checkout preserved as an unmerged development snapshot, not selected as the deployed baseline |

Snapshots contain reusable source, tests and design documents. Task state,
operator evidence, credentials, native transcripts, qualification archives and
private provenance ledgers are excluded. Their historical packaging tools can
require those explicit private inputs and must fail when they are absent;
publishing a snapshot does not satisfy its original overall acceptance.

Active local writers and all original task records are preserved. Stable
checks apply to the default branch; snapshot checks have separate status and
must not silently become release claims. Later integration should use reviewed
commits from a development branch and a new source-matched verification.

The [W3 source audit](w3-candidate-snapshots-audit.md) compares the four archived
trees at `7ec8221` with `main@0f45125` and records the reviewed dispositions
(`tracking-rectification`: `INCREMENTAL_PORT`, `execution-efficiency`:
`REDESIGN_FROM_MAIN`, `agy-acpx-integration`: `REDESIGN_FROM_MAIN`,
`legacy-managed-mailbox`: `ARCHIVE_ONLY`). It does not change the export-time
qualification facts above, approve integration, reissue historical acceptance,
or authorize a native/model trial.
