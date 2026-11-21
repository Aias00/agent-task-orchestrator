# Compact origin host handoff

This fixes the execution conversation surface, separately from user hub notification preferences.
The controller used to enqueue the full source, contract instructions and pending request as a native user turn. These host messages also inherited the generic `followup` type, whose completion exception was intended for final user notifications. Old host handoffs could consequently pass the completed-workflow dispatch check.

Controller-created host handoffs now retain their full original work material in a private immutable 0600 file. The native wake-up retains the original correlation marker plus a short `ato-read-handoff ID` read instruction. Reading material grants no authority, marks no approval/acceptance, and changes no task state. The exact recipient, contract, generation, live needs_host request and STOP/closure are checked again when reading. Stale reads return only a compact end_silently result. The read helper must be installed before activating this controller.

New envelopes have the host_handoff phase. At both dispatch guards, old legacy followup envelopes are recognized by their canonical host-handoff ID and must still match the live request. Completed/stopped/changed requests cannot be queued. Completion after native queue submission cannot be recalled by a dispatch guard; the receiving read fence handles late compact wake-ups without running obsolete work.

Normal origin handoffs still wake the origin once. Human questions, answers, final completion once, unattended failure routing, STOP, attempts, contracts, business services and global ctask bindings are unchanged. Existing delivered history and unknown receipts are not deleted or replayed. Deployment only switches the controller executable and installs an additive reader; all state stores are retained.

Qualification: test_compact_handoff covers compact rendering/full material integrity, origin and filesystem checks, old and new envelope rejection, completion at the dispatch barrier, valid once-only routing and a silent stale-reader CLI. All inherited tests remain unmodified.
