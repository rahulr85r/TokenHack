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
│   ├── _base.py          # Symbol, ExtractResult, shared helpers
│   ├── __init__.py       # adapter registry, get_adapter(filepath)
│   ├── python.py         # .py
│   ├── jvm.py            # .java, .kt, .kts  (shared adapter)
│   ├── swift.py          # .swift
│   └── javascript.py     # .js, .mjs, .cjs, .jsx  (JSX included)
└── index/
    ├── symbols.json      # built by CI; committed to the repo
    └── meta.json         # file hashes for incremental rebuilds
```

## Scoring formula

The router combines a BM25 baseline with structural and heuristic priors:

```
tokens = camel_snake_split(query) − code_stopwords + fuzzy(close_matches)

score(file, query) =
    BM25(tokens, symbols ∪ refs ∪ path_tokens)        # lexical
  + η · BM25(tokens, docstrings ∪ markdown_paragraphs) # prose channel
  + α · filename_match(tokens, basename(file))
  + β · path_affinity(tokens, dirname(file))
  + γ · recency(mtime(file))                          # exponential decay
  + ε · symbol_popularity(file)                       # incoming imports
  + ζ · definition_bonus(tokens, defs_in_file)
  + δ₁ · graph_propagation_1hop(file, top_seeds)
  + δ₂ · graph_propagation_2hop(file, top_seeds)      # decayed
```

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
| `ETA` | 1.2 | Prose channel weight |
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
| `TOP_K` | 5 | How many ranked results to emit |
| `STAGED_TOKEN_CAP` | 10000 | Advisory only — router emits TOP_K paths regardless |

Open a PR with rationale + measurements if you change any of these.

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
