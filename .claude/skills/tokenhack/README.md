# TokenHack — Skill Internals

Contributor-facing docs. For the user-facing pitch and install, see the
[top-level README](../../../README.md).

## Layout

```
.claude/skills/tokenhack/
├── SKILL.md              # slash entry + 3-gate nudge ruleset
├── router.py             # pure-stdlib hybrid retrieval (runs on dev machine)
├── indexer.py            # tree-sitter walker (runs in CI)
├── requirements.txt      # indexer deps (NOT needed on dev machines)
├── README.md             # this file
├── adapters/
│   ├── _base.py          # Symbol, ExtractResult, doc-comment extraction
│   ├── __init__.py       # adapter registry, get_adapter(filepath)
│   ├── python.py         # .py
│   ├── jvm.py            # .java, .kt, .kts  (shared adapter)
│   ├── swift.py          # .swift
│   ├── javascript.py     # .js, .mjs, .cjs, .jsx  (JSX included)
│   └── typescript.py     # .ts, .tsx, .mts, .cts (reuses the JS walk)
└── index/
    ├── symbols.json      # built by CI; committed to the repo
    └── meta.json         # file hashes for incremental rebuilds
```

## Scoring formula

The router combines a BM25 baseline with structural and heuristic priors:

```
tokens = camel_snake_split(query) − code_stopwords − corpus_filler + fuzzy(close_matches)

score(file, query) =
  [ BM25(tokens, symbols ∪ refs ∪ path_tokens)          # lexical
  + η · BM25(tokens, doc_comments ∪ markdown_paragraphs) # prose channel
  + α · Σ idf_weight(t) for t in filename_match(tokens, basename(file))
  + β · Σ idf_weight(t) for t in path_affinity(tokens, dirname(file))
  + γ · recency(mtime(file))                            # exponential decay
  + ε · symbol_popularity(file)                         # incoming imports
  + ζ · definition_bonus(tokens, defs_in_file)
  + impl_pattern_boost(file)
  ] · (test_penalty if file is a test and the query isn't about tests)
  + min( graph_prop(file), own_lexical_score + floor )  # capped, see below
```

**Corpus filler removal.** Query tokens whose IDF falls below
`MIN_QUERY_TOKEN_IDF` are dropped. A natural-language question carries words
that are technically in the vocabulary but carry no discrimination — `netty` in
netty has an IDF of 0.01. BM25 already discounted them; the structural signals
did not.

**IDF-weighted structural matches.** `filename_match` and `path_affinity` used
to count 1.0 per matching token regardless of the token. A question mentioning
`read` and `socket` therefore scored the same filename hit as one mentioning
`idempotency` and `backoff`. Both are now weighted by token IDF, normalised
against `IDF_REFERENCE` so `ALPHA` / `BETA` keep their existing meaning.

**Test/benchmark demotion.** Tests restate a concept's vocabulary far more
densely than the implementation does, which is exactly what BM25 rewards — on
netty, the top three results for a pipeline question were two testsuite files
and a microbenchmark, and the implementation was absent. Test-path files are
multiplied by `TEST_PATH_PENALTY` unless the question is explicitly about
tests, and surface instead in the paired-results section.

Hub suppression: files imported by more than `HUB_FRACTION` of the corpus
do not propagate scores to their neighbours (utility hubs like `utils.py`
would otherwise drag the whole repo in).

**Lexical-vs-prior balance.** When the corpus contains a strong term match
(any file with `bm25 + η·prose ≥ LEX_STRONG_THRESHOLD`), the priors `GAMMA`
(recency) and `EPSILON` (popularity) are scaled down to act as tie-breakers
rather than primary signals. Filename, path-affinity, and definition bonuses
are query-derived and are not scaled.

**Noise-hub penalty.** A file with `popularity > 0` but no query-derived
signal (no BM25 / prose hit, no filename match, no path affinity, no def
bonus) is treated as a *negative* signal — popularity alone is not relevance.
This catches the common failure mode where a popular utility file bubbles up
on import-count prior despite being unrelated to the query.

**Callers-of mode.** When the query matches a pattern like
`callers of X`, `who calls X`, `where is X used`, `find usages of X`, the
router locates files defining `X` and walks the reverse-import graph to
boost their importers. Defining files get a double boost so they outrank
plain importers. Falls back to lexical retrieval if no definition is found.
Graph-propagation credit is *restricted* to the caller set in this mode,
so unrelated files that happen to match common tokens like `get` cannot
accumulate inflated graph_prop and bury the actual target.

**Capped graph propagation.** The filename gate below was meant to bound
`graph_prop`, but a natural-language question contains many words, so any
well-connected file with two of them in its basename cleared it and received
*raw* credit. Measured on netty, `SocketReadPendingTest.java` accumulated a
`graph_prop` of 333 against a lexical score of 22 — propagation was 94% of the
winning score and the file that answered the question ranked 1,716th.
Propagation is evidence that a *neighbour* matched, which is weaker than
matching yourself, so it is now always log-compressed and then capped at
`GRAPH_PROP_CAP_RATIO ×` the file's own query-derived score plus a small floor.
It can promote a plausible file; it can no longer invent one.

**Lexical-only graph propagation.** Recency and popularity are still
summed into the final ranking but are not allowed to *seed* the import
graph. In an actively-modified repo nearly every file has high recency,
which would otherwise make almost every file a propagation seed and
inflate well-connected destinations into thousands of graph_prop points.
A further hard cap (`MAX_LEX_SEEDS`) limits propagation to the top-N
lexical seeds, preventing common-token explosions (e.g. matching `get`
in 300 files at once) from collectively dominating the graph.

## Tunable constants (router.py)

| Constant | Default | What it does |
|---|---|---|
| `ALPHA` | 1.5 | Weight of filename-match hits |
| `BETA` | 1.0 | Weight of path-affinity hits |
| `GAMMA` | 0.5 | Weight of recency decay |
| `EPSILON` | 0.3 | Weight of symbol popularity (incoming imports) |
| `ZETA` | 0.5 | Bonus per matched definition name |
| `DELTA_1HOP` | 1.0 | 1-hop import-graph propagation |
| `DELTA_2HOP` | 0.3 | 2-hop propagation (decayed) |
| `ETA` | 2.4 | Prose channel weight (doc comments + markdown) — the highest-value signal in the ranker; see the measurement note in `router.py` |
| `BM25_K1` | 1.5 | BM25 saturation parameter |
| `BM25_B` | 0.75 | BM25 length-normalization parameter |
| `RECENCY_HALFLIFE_DAYS` | 30 | Files this old contribute half their recency score |
| `HUB_FRACTION` | 0.10 | A file imported by >10% of repo is a "hub" |
| `LOW_CONFIDENCE_SCORE` | 0.5 | Top score below → flag as low-confidence |
| `STALE_INDEX_FILE_THRESHOLD` | 5 | Files changed since index build → warn |
| `LEX_STRONG_THRESHOLD` | 2.0 | `max(bm25 + η·prose)` above this → strong lexical |
| `LEX_STRONG_PRIOR_SCALE` | 0.25 | When strong lexical, multiply GAMMA/EPSILON by this |
| `NOISE_HUB_PENALTY` | 0.8 | Penalty for popularity > 0 with no query-derived signal |
| `CALLERS_BOOST` | 3.0 | Boost for "callers of X" reverse-import-graph hits |
| `MIN_LEX_SEED` | 1.0 | Floor below which a file cannot seed graph propagation |
| `MAX_LEX_SEEDS` | 25 | Hard cap on the number of files allowed to seed graph propagation (top-N by lex score) |
| `GRAPH_FILENAME_GATE` | 2.0 | Filename-match tokens needed for *uncompressed* graph_prop credit |
| `GRAPH_PROP_SCALE` | 2.0 | Log-compression scale applied to graph_prop below the gate |
| `IMPL_BOOST` | 0.8 | Weight per overlapping core token on a `Default*`/`Abstract*`/`*Impl` file |
| `IMPL_INTENT_MULTIPLIER` | 1.5 | Extra multiplier when the query has impl-walkthrough intent |
| `FUZZY_CUTOFF` | 0.85 | `difflib` similarity floor for query-token expansion |
| `FUZZY_MAX_PER_TOKEN` | 2 | Max fuzzy expansions per query token |
| `MAX_SPANS_PER_FILE` | 4 | Cap on staged `↳ read` ranges per result |
| `WIDE_SPAN_FRACTION` | 0.5 | A matched span covering more than this fraction of the file is a container (class / extension), not a target — dropped when a tighter span also matched |
| `TOP_K` | 5 | How many ranked results to emit |

| `TEST_PATH_PENALTY` | 0.35 | Multiplier on a test/benchmark/fixture file when the query isn't about tests |
| `GRAPH_PROP_CAP_RATIO` | 1.0 | graph_prop ceiling as a multiple of the file's own lexical score |
| `GRAPH_PROP_CAP_FLOOR` | 1.0 | Additive floor so a zero-lexical neighbour can still surface |
| `IDF_REFERENCE` | 3.0 | IDF normaliser for structural (filename / path) match weighting |
| `IDF_WEIGHT_CAP` | 2.0 | Max weight a single very-rare token can carry structurally |
| `MIN_QUERY_TOKEN_IDF` | 0.35 | Query tokens below this IDF are corpus-wide filler and get dropped |

Every constant here is overridable at runtime as `TOKENHACK_<NAME>`, so you can
ablate it against the gold set:

```bash
TOKENHACK_ETA=0 python3 tests/eval.py
```

**Open a PR with rationale + measurements if you change any of these.** Since
`tests/` exists there is no excuse for "felt better" — run `python3
tests/eval.py` before and after and put both numbers in the PR body.

## Adding a new language adapter

Each adapter is a small module that wraps a tree-sitter grammar. To add
TypeScript, Vue, Rust, etc.:

1. **Pick the grammar.** `tree-sitter-<language>` packages on PyPI are easiest.
   Add the dependency to `requirements.txt`.
2. **Create `adapters/<language>.py`.** Follow the shape of `python.py`:

   ```python
   from ._base import Symbol, ExtractResult, get_text, first_line

   LANGUAGE_NAME = "typescript"
   FILE_EXTENSIONS = [".ts", ".tsx"]

   try:
       from tree_sitter import Parser, Language
       import tree_sitter_typescript
       _PARSER = Parser(Language(tree_sitter_typescript.language_typescript()))
       AVAILABLE = True
   except Exception:
       _PARSER = None
       AVAILABLE = False

   def extract(source: bytes, filepath: str) -> ExtractResult:
       if not AVAILABLE:
           return ExtractResult()
       # ... walk the AST, populate symbols / imports / references ...
       return ExtractResult(...)
   ```

3. **Register it.** Add the import to `adapters/__init__.py` and include
   it in the `_MODULES` tuple.
4. **Test.** Run `python3 indexer.py --verbose` on a sample repo of the
   new language and check that symbols / imports populate sensibly.

Adapters degrade gracefully: if the tree-sitter grammar fails to load
at import time, `AVAILABLE` becomes `False` and `extract()` returns an
empty result. The indexer continues with other languages.

## What v1 deliberately does NOT do

- TypeScript adapter (first-PR target — extends `javascript.py`)
- Vue, Ruby, C#, Rust, Go, C/C++ adapters
- Embedding-based retrieval (would require a model — breaks the stdlib + zero-install promise)
- Cross-encoder reranking (same reason)
- LLM-as-retriever (same reason)
- Multi-hop graph beyond 2-hop (diminishing returns)
- Git-history co-occurrence
- Acronym expansion (JWT ↔ "json web token")
- Query intent classification
- Result diversification / top-K auto-tuning

See the top-level README's *Contributing* section for which of these
have clear scope and are ready for PRs.
