# Open Items From Specification Reviews

Consolidated from `review1.md`, `review2.md`, and `review3.md` (2026-06-15 review rounds), after
cross-referencing each finding against the fixes already applied to `spec/00`–`09`. Everything
else raised in those reviews was either resolved in-spec or explicitly declined as out of scope;
only the still-open items were listed below. The review files have been removed now that their
actionable content is captured here.

**Status (2026-08-14): both remaining items are closed.** Nothing from the review rounds is
outstanding.

## 1. Federated-search framing needs a forward-pointer — CLOSED

`spec/04-search-and-retrieval.md` §3 stated "there's only one retrieval path (lexical)... no
fusion of heterogeneous signal types — and so none of the score-comparability problems fusion
exists to solve." §4 then describes a score-normalization/merge step for the dedicated-index case.
Not contradictory (different scopes — single index vs. federated with dedicated indexes), but §3's
blanket framing should point forward to §4's caveat so a reader doesn't think the dedicated-index
case was overlooked.

*Source: review1.md §2 (Clarity/Minor Issues)*

**Resolution**: `04` §3's closing bullet now scopes its claim to a single index instance and points
forward to §4 for the one case where scores from separate instances are reconciled.

## 2. Connector credential/security model unspecified — CLOSED

`spec/09-implementation-notes.md` §4 defines *how* a connector run executes (dedicated polling
worker pool, diff-against-last-sync), but did not cover:

- credential storage/rotation model
- secret scope
- connector permission boundaries
- audit fields for connector actions

`spec/05-admin-backend-and-maintenance.md` §7 only said connector configuration includes
"credentials," without detail.

*Source: review2.md "Missing Details to Add" #3; review1.md §6 (listed there as bundled with the
connector-execution-model gap, which §9.4 has since resolved — this credential/security part was
never addressed)*

**Resolution**: new `09` §13 covers all four — secrets held in an external secrets manager behind
a `credential_ref`, rotation handled there with auth failure disabling rather than retrying, one
least-privileged credential per connector, `connector:<connector_id>` as an `access_policy`
principal capped at `contributor` on one workspace, and audit split across `admin_action_log`
(config) and `ingestion_log` (runs). Touch-points applied to `02` §3 (new `connector` table,
`access_policy` principal forms), `05` §7, and `06` §3.
