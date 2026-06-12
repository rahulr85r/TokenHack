# TokenHack Roadmap

This is the canonical list of things we'd like to improve in TokenHack, organized by scope and readiness. The shorter "good first issue" lists in [`README.md`](README.md#where-to-start) and [`CONTRIBUTING.md`](CONTRIBUTING.md#contributing-opportunities) are quick pointers into this document.

For how to claim and contribute an item, see [`CONTRIBUTING.md`](CONTRIBUTING.md). For internal architecture, see [`.claude/skills/tokenhack/README.md`](.claude/skills/tokenhack/README.md).

---

## Philosophy

These constraints are load-bearing — proposals that violate them are out of scope no matter how compelling:

- **Pure-stdlib router.** The query path runs on stock Python with no third-party packages. Indexer uses tree-sitter (one-time per repo, not per query); the router does not.
- **No model artifacts shipped with the skill.** No embeddings, no weights, no trained reranker.
- **No network at query time.** Retrieval is local-only.
- **Token cost is the load-bearing metric.** Every improvement should reduce the number of tokens Claude spends to answer a coding question, not increase precision-at-1 for its own sake.

Anything that satisfies these is fair game.

---

## Status snapshot (May 2026)

- **4 language adapters**: Java/Kotlin/Scala (JVM), Python, JavaScript, Swift
- **Indexer**: 0% error rate across ~22k files tested (DDG, Spring, netty, Signal-Android, OwnCloud, blinkit)
- **Router scoring**: 6 named fixes accumulated, most recent being the impl-pattern boost + filename-gated graph_prop ([#2](https://github.com/rahulr85r/TokenHack/pull/2))
- **Validation**: Ad-hoc — no formal regression test suite yet (see Near-term item below)

---

## Near-term — concrete, scoped, ~few hours each

These are well-defined contributions that don't need a design discussion. Pick one, open a PR.

### Language adapters

- **TypeScript adapter** (`adapters/typescript.py`). Extends the existing JS adapter to `.ts` / `.tsx`. The grammars share ~95% of node types. *Highest-impact contribution — most modern frontends are TS.*
- **Vue.js adapter** (`adapters/vue.py`). `tree-sitter-vue` for `.vue` SFCs; reuse JS/TS adapter for `<script>` blocks.
- **Ruby adapter** (`adapters/ruby.py`). `tree-sitter-ruby`. Straightforward — model after `adapters/python.py`.
- **Go adapter** (`adapters/go.py`). `tree-sitter-go`. Modern infrastructure code.
- **Rust adapter** (`adapters/rust.py`). `tree-sitter-rust`. Growing ecosystem.
- **C# adapter** (`adapters/csharp.py`). `tree-sitter-c-sharp`. Heavy enterprise use.

### Prose / documentation

- **AsciiDoc prose support.** `indexer.py:walk_prose` and `extract_prose_paragraphs` only index `.md` today. Spring's design docs are `.adoc`, AsciiDoctor is widely used in Java/Ruby projects, and the prose corpus on Spring is anemic (8 docs / 100 paragraphs vs the real corpus). Add `.adoc` handling — same paragraph extraction with AsciiDoc-aware syntax stripping.
- **reStructuredText support.** `.rst` for Python projects (Sphinx, ReadTheDocs).
- **Plain-text manifest files.** `CHANGELOG.txt`, `RELEASE_NOTES`, `HACKING` — projects that don't use Markdown still ship semantic prose.

### Symbol extraction

- **Symbol kind granularity.** Adapters currently flatten every symbol to `kind: "def"`. Distinguish `class` / `interface` / `struct` / `enum` / `function` / `method` / `field` / `constant` / `typealias`. Enables (a) downstream filtering (`--kind=class`), (b) better ranking signals (a class definition is usually worth more than a constant), and (c) richer output in the staged context.

### Router / output

- **Interface ↔ impl paired surfacing.** Today the router pairs source ↔ test (`find_test_partner` in `router.py`). Add an analogous mechanism for interface ↔ `Default*` / `Abstract*` / `*Impl` siblings: when the interface is in top-K, automatically surface the impl as a pair (and vice versa). Sister feature to the impl-pattern boost merged in [#2](https://github.com/rahulr85r/TokenHack/pull/2).
- **Better symbol-lookup detection.** Phrases like *"where is X defined"* / *"find X"* should trigger a mode that biases toward definition-site weighting and demotes test files. Today these queries route through the same scoring path as impl walkthroughs; a small intent classifier (regex-based, same shape as `IMPL_INTENT_RE`) would help.
- **Query-time filters.** `--no-tests`, `--lang=swift`, `--exclude=examples/`, `--only=src/`. Useful when the user knows which slice of the corpus to search.

### Symbol-span staging follow-ups

Symbol-span staging shipped (the router now emits `↳ read L<start>-<end>` hints for query-matched defs so Claude reads ~40-line spans instead of whole files — see *Recently shipped*). Two follow-ups build directly on it:

- **Symbol cards — answer definitional queries with zero file reads (Tier 2).** The index stores per-symbol `signature`, `line`, and `end_line`, but the per-symbol *docstring first line* is not captured (`docstring_summary` is per-file). Capture a per-symbol `doc` in the adapters (the def node's docstring/leading-comment first line) and have the router emit a compact card per matched symbol — `signature — doc  (path:L<start>-<end>)`. For "what's the signature of X" / "what does Y do" the staged block then answers with **no** file read at all. Cost: one short string per symbol in the index; eliminates the read on a whole class of lookups. Touches `adapters/*` (extract per-symbol doc → `Symbol.doc`) and `router.py:format_output` (render the card). *Good follow-on for whoever did, or wants to learn, the span-staging path.*
- **File-outline staging for architectural queries (Tier 3).** For impl-walkthrough / "how does X work" intent (`IMPL_INTENT_RE` already detects it), emit the matched file's ordered signature skeleton — the signatures the index already stores — beneath the result, so Claude navigates straight to the relevant body and reads only that one span. **Pure `router.py` change, no index growth.** Pairs naturally with the *Better symbol-lookup detection* item above (both are small intent-gated output tweaks).

### Configuration

- **Per-repo config file** (`.tokenhack.toml` at repo root). Override scoring constants, add custom stopwords, define exclude patterns, register codebase-specific synonyms. Today all config is in the source.

### Testing infrastructure

- **Ranker regression test suite.** This is the highest-leverage item under "Larger" in the README, and it's actually been prototyped — see the comparison harness used to validate [#2](https://github.com/rahulr85r/TokenHack/pull/2). Formalize it: a `tests/regression/` directory with one YAML file per repo, each containing a gold-set of queries and expected files. CI runs the router against fixtures and fails on rank regressions. Solves the "no formal tests" gap that the CONTRIBUTING guide currently apologizes for.

---

## Medium-term — design discussion first

Open an issue before writing code. Each of these has multiple reasonable implementations and we want to align on one.

### Retrieval signal extensions

- **Synonym / concept mapping.** User-extensible YAML mapping domain terms across naming conventions: `ViewController` ↔ `Activity` ↔ `Fragment`, `BeanFactory` ↔ `Container`, `Repository` ↔ `Store` ↔ `DAO`. Each query token expands with its synonyms. Open question: ship a default mapping with the skill, or require users to opt in?
- **Acronym expansion.** `JWT` ↔ "json web token", `CRUD` ↔ "create read update delete", `DI` ↔ "dependency injection". Adjacent to synonyms but bounded and reusable.
- **Git-history co-occurrence as a signal.** Files frequently committed together are likely structurally related even when imports don't capture the relationship. Cheap to compute, useful for "what else changes when I touch X" queries. Privacy / repo-size considerations.
- **Multi-hop import graph beyond 2-hop.** Currently capped at 2 hops with decay. Going to 3+ amplifies noise dramatically; need a principled signal cutoff (PageRank-style? Personalized PageRank seeded on lexical matches?).

### Output / UX

- **Configurable result count.** `TOP_K` is hardcoded to 5. Trivial to make a flag, but the downstream skill body assumes 5 — needs coordinated change.
- **Result diversification.** When top 5 results are all near-duplicates (same module, same purpose), the user gains nothing from the bottom 4. Optional MMR-style reranking.

### Architecture

- **MCP server mode.** Expose the router as an MCP (Model Context Protocol) server so non-Claude-Code tools — IDE plugins, other agents, scripts — can query the index without bash. Doesn't break the "no network" rule (local IPC only).
- **Multi-root / monorepo support.** Index multiple repository roots into one federated corpus, each result tagged with its origin. Useful for organizations with split-out shared libraries.
- **Diff-aware retrieval.** `--diff=BRANCH` flag biases toward files changed in a branch — useful for PR review and "what's the blast radius of this change" queries.

---

## Long-term / speculative

Bigger ideas with real uncertainty about cost/benefit. Not currently planned; included so contributors can see the wider thinking.

- **Query intent classification beyond regexes.** Today we detect "callers of X" and impl-walkthrough intent with regex patterns. A more principled tiny classifier (rule tree or small NN trained offline, not shipped at query time) could distinguish: symbol lookup, impl walkthrough, callers, "how does X relate to Y", "what owns X". Risk: complexity creep.
- **Cross-language symbol joins.** A Kotlin caller of a Java class isn't currently linked through the import graph because they go through different adapters. Cross-adapter symbol normalization would help mixed JVM codebases.
- **Index compression / streaming.** `symbols.json` is 36 MB on Spring. Gzip-on-disk would cut load time; streaming JSON parse would let the router start ranking before the full file loads. Only worth doing if profiling shows load time matters.
- **Live re-indexing on file save** (LSP-adjacent). Today the index is built once in CI and reused; incremental rebuilds happen via the hash cache. Editor integration that re-indexes on save would be useful but introduces install / daemon concerns.

---

## Explicitly out of scope

Listed so contributors don't waste time proposing them. These break the load-bearing constraints stated above.

- **Embeddings, semantic search, neural rerankers.** Require model artifacts shipped with the skill or a network call.
- **LLM-as-retriever.** Same constraint.
- **Real-time / web-based retrieval.** No network at query time.
- **Multi-user / hosted / SaaS mode.** TokenHack is a per-repo skill, not a service.
- **Replacing tree-sitter with a custom parser.** Tree-sitter handles edge cases TokenHack will never re-implement well.

If you have an idea for a richer retrieval signal that fits the constraints, [open an issue](https://github.com/rahulr85r/TokenHack/issues) — we'd rather discuss before you write code than reject a finished PR.

---

## How items move

1. **Idea** → opened as a GitHub issue tagged `roadmap`.
2. **Scoped** → maintainer comments confirm scope and acceptance criteria. Promoted from speculative → medium-term, or medium-term → near-term.
3. **Claimed** → contributor comments "I'd like to take this." Maintainer assigns.
4. **In flight** → contributor opens a draft PR linked to the issue.
5. **Merged** → item moves to the change log; new items take its slot in the roadmap.

If you want to propose a brand-new item not on this list, open an issue first. Brand-new PRs without a corresponding issue may get pushed back into the issue queue before review.

---

## Recently shipped

- **2026-06** — Symbol-span staging (Tier 1). Indexer captures per-symbol `end_line`; the router stages precise `↳ read L<start>-<end>` hints for the query-matched defs in each result, so Claude reads the relevant ~40-line spans instead of whole files. Tiny index growth (one int per symbol), large query-token reduction on big repos. Degrades gracefully on an older (`end_line`-less) index. Tier 2/3 follow-ups under *Near-term → Symbol-span staging follow-ups*.
- **2026-05** — [#2](https://github.com/rahulr85r/TokenHack/pull/2) Impl-pattern boost + filename-gated graph_prop compression. Fixes interface-heavy codebases (netty, Spring impl walkthroughs) without regressing canonical-popular lookups (Spring RestTemplate).
- **2026-05** — [#1](https://github.com/rahulr85r/TokenHack/pull/1) Switch CI index updates to PR-based flow.
- *(earlier history — see commit log)*
