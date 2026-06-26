# End-to-End Consistency Pass (2026-06-15)

Full re-read of `spec/00`–`08` (9 docs) after the review1/review2 fix rounds and the new `08`
appendix. Checked: ~80 cross-references (all resolve to real sections), the 9 enum-style fields,
footer navigation chain, the `00` §4 Document Map, and `08`'s spec-reference mappings against
`00`–`07`. Found and fixed:

| Finding | Resolution |
|---|---|
| `raw_source.status` enum (02 §3: `active\|superseded\|archived`) missing `rejected`, despite 03 §1's table and §4's "marked `rejected`" using it | **✅ Resolved.** Enum now `active\|superseded\|archived\|rejected`. |
| 03 §6 step 7 said "Set `raw_source.status` and pipeline state to `ingested`" — `ingested` isn't a `raw_source.status` value (per 03 §1's own table, an ingested source has `status=active`) | **✅ Resolved.** Reworded: pipeline state becomes `ingested`; `raw_source.status` remains `active`. |
| `00` §6 glossary "Review Item" entry omitted the `classification` kind (added via review2 #1) | **✅ Resolved.** Added "a low-confidence classification" to the enumeration. |

**Declined / not new scope:**
- A few `[07]` refs (05 §4, 05 §8, 06 §3, 06 §5) point at the whole doc rather than a specific 07
  subsection — not incorrect, just less precise than they could be.
- 05 §3's `[02] §8` ref for the `pending/stale → indexing → indexed` transition could arguably be
  `§7` (where that state diagram lives) — both sections are relevant, not a contradiction.
- `page_version.trigger=prune` (01 §5) isn't explicitly invoked in 05 §4's archive description —
  plausible level-of-detail choice, not a contradiction.

Everything else — Mermaid diagrams, requirements traceability (00 §7), erDiagrams vs. table field
lists, and narrative coherence across the cumulative edits — holds up.
