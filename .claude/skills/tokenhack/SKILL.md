---
name: tokenhack
description: Pre-stage relevant codebase context to save tokens on large-repo questions. Use before asking questions that would otherwise require Claude to grep across many files.
argument-hint: "<your question>"
disable-model-invocation: true
when_to_use: When you are about to ask Claude a question that would require exploring multiple unfamiliar files in a large codebase — e.g., "how does X work?", "where is Y handled?", "find all callers of Z". TokenHack pre-stages the relevant files locally via a stdlib router over a CI-built index, so Claude reads ~5 targeted files instead of grepping the tree.
---

# TokenHack — Staged Context

The user invoked `/tokenhack` with this question:

> **$ARGUMENTS**

Pre-staged context from the local symbol index (zero LLM calls — pure local retrieval):

!`python3 .claude/skills/tokenhack/router.py "$ARGUMENTS"`

---

## How to use the staged context above

You now have a pre-ranked list of files most likely relevant to the user's question. The ranking combines BM25 over symbols + docstrings, filename / path-affinity / recency signals, and 2-hop import-graph proximity.

- **Read the staged spans, not whole files.** Each result may list one or more `↳ read L<start>-<end>  <signature>` hints — the exact definitions that matched the query. Read those line ranges (via the Read tool's `offset`/`limit`) rather than the entire file; that is the point of TokenHack. Widen to the full file only when the span doesn't answer the question (you need the imports, the class header, or a caller).
- **Start with the top result.** Only widen your reading if you cannot answer from it.
- A result with **no** `↳ read` hints was retrieved by prose / filename / import-graph rather than a specific symbol — read that file normally (or its docstring region).
- If the `[low-confidence retrieval …]` flag is shown above, the index found no strong match — consider using grep directly or asking the user to rephrase.
- If the `[index is N files behind HEAD …]` warning is shown, very recent changes may not be reflected; mention this if you suspect their question hits new code.
- Paired test files are listed separately and are an additional context source, not a primary one.

Now answer the user's question using the staged files.

---

## For the rest of this session — nudge ruleset

The user has opted into TokenHack. Help them keep using it when it would save tokens.

**Before answering each new user turn**, silently classify the question. Suggest re-running `/tokenhack` **only when ALL THREE gates pass**:

**Gate 1 — Scope signal present.** The new question contains at least one of:

  (a) "where", "which files", or "how does … work" *without* a specific file or symbol named,
  (b) cross-cutting verbs: *find all*, *rename across*, *audit*, *every place that*, *callers of*,
  (c) architectural framing: *trace from … to …*, *end-to-end*, *data flow*, *how does X work in this repo*,
  (d) existence / duplication checks: *do we already have*, *is there an existing*,
  (e) a domain concept without a symbol: *rate limiting*, *auth flow*, *caching layer*, *retry logic*.

**Gate 2 — No local anchor.** The new question does NOT:

  (a) name a file or symbol already in this conversation,
  (b) refer to the immediately previous assistant turn via pronouns (*this*, *that*, *it*, *the function above*),
  (c) ask a generic language/algorithm question unrelated to the repo,
  (d) request a trivial edit to displayed code (rename, comment, reformat).

**Gate 3 — Worth the round-trip.** The question has substantive content (more than ~5 tokens of intent) AND would plausibly require reading 3+ unseen files to answer correctly.

**When in doubt, do NOT nudge.** False positives are strictly worse than missed nudges. The user can always invoke `/tokenhack` manually.

### Nudge phrasing

When all three gates pass, respond first with this short line **before** answering anything else:

> This looks codebase-wide — want me to re-stage via `/tokenhack`? (Say "just answer" to skip and turn off nudging for this session.)

Then wait for the user's response.

### Escape hatch

If the user says any of: *"just answer"*, *"stop nudging"*, *"no thanks"*, *"skip"*, *"don't nudge"*, *"silence"*, or anything semantically equivalent — **stop nudging for the rest of this session.** Answer their questions directly without the suggestion line. Do not re-introduce nudging in this session.
