# Open Items From Specification Reviews

Consolidated from `review1.md`, `review2.md`, and `review3.md` (2026-06-15 review rounds), after
cross-referencing each finding against the fixes already applied to `spec/00`–`09`. Everything
else raised in those reviews was either resolved in-spec or explicitly declined as out of scope;
only the still-open items are listed below. The review files have been removed now that their
actionable content is captured here.

## 1. Federated-search framing needs a forward-pointer

`spec/04-search-and-retrieval.md` §3 states "there's only one retrieval path (lexical)... no
fusion of heterogeneous signal types — and so none of the score-comparability problems fusion
exists to solve." §4 then describes a score-normalization/merge step for the dedicated-index case.
Not contradictory (different scopes — single index vs. federated with dedicated indexes), but §3's
blanket framing should point forward to §4's caveat so a reader doesn't think the dedicated-index
case was overlooked.

*Source: review1.md §2 (Clarity/Minor Issues)*

## 2. Connector credential/security model unspecified

`spec/09-implementation-notes.md` §4 defines *how* a connector run executes (dedicated polling
worker pool, diff-against-last-sync), but does not cover:

- credential storage/rotation model
- secret scope
- connector permission boundaries
- audit fields for connector actions

`spec/05-admin-backend-and-maintenance.md` §7 only says connector configuration includes
"credentials," without detail.

*Source: review2.md "Missing Details to Add" #3; review1.md §6 (listed there as bundled with the
connector-execution-model gap, which §9.4 has since resolved — this credential/security part was
never addressed)*
