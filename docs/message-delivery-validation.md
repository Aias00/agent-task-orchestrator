# Delivery validation boundaries

The published baseline includes durable mailbox, registered identity, turn
correlation and coordinator/worker fixture tests. The complete offline suite
runs with temporary ATO/native state and fake/local protocol peers. It does not
send user notifications, reuse an operator's conversation or invoke a provider.

Message queued, recipient received, execution resumed, native turn complete
and overall workflow accepted are distinct states. Independent host checks,
real approvals and current owner/generation proof remain necessary. EOF or
an ACP end-turn alone does not prove a valid coding result.

Historical host evidence is kept privately. This source export documents its
origin and portable adaptations; it does not reissue or rewrite historical
test, rollout or approval records. See [publication](publication-20261120.md)
for current source-distribution checks and [development status](development-status.md)
for work that remains outside the stable baseline.
