#!/usr/bin/env python3
"""TokenHack router — pure-stdlib hybrid retrieval over the indexed codebase.

Invoked by the SKILL.md via:

    !`python3 .claude/skills/tokenhack/router.py "$ARGUMENTS"`

Reads `.claude/skills/tokenhack/index/symbols.json` (produced by indexer.py
in CI), ranks files for the user's query, and prints a staged-context
block: ranked paths with a one-line "why each matches" plus an index
freshness header and any conditional warnings (low-confidence, stale-index).

No third-party dependencies. Pure Python stdlib.
"""

import difflib
import json
import math
import os
import re
import sys
import time
from collections import Counter
from pathlib import Path

# ----------------------------------------------------------------------
# Tunable scoring constants — community-iterable. See README "Scoring".
#
# Every constant below can be overridden per-invocation with an environment
# variable named TOKENHACK_<CONSTANT>, e.g. TOKENHACK_ETA=2.0. That exists so
# `tests/eval.py` can ablate one signal at a time against the gold set — if you
# change a weight, you should be able to show the recall number that justifies
# it — and so a team can tune for their repo without forking the file.
# ----------------------------------------------------------------------

def _tunable(name, default):
    raw = os.environ.get("TOKENHACK_" + name)
    if raw is None:
        return default
    try:
        return type(default)(raw)
    except (TypeError, ValueError):
        return default


ALPHA = _tunable("ALPHA", 1.5)         # filename match weight
BETA = _tunable("BETA", 1.0)          # path-affinity weight
GAMMA = _tunable("GAMMA", 0.5)         # recency weight (file mtime decay)
EPSILON = _tunable("EPSILON", 0.3)       # symbol popularity weight (incoming imports)
ZETA = _tunable("ZETA", 0.5)          # definition bonus (per matched def name)
DELTA_1HOP = _tunable("DELTA_1HOP", 1.0)    # 1-hop import-graph propagation
DELTA_2HOP = _tunable("DELTA_2HOP", 0.3)    # 2-hop import-graph propagation (decayed)
# Prose channel weight (doc comments + markdown). Raised from 1.2 to 2.4 after
# doc-comment extraction shipped: with the channel actually populated it is the
# single most valuable signal in the ranker, because docs are written in the
# words a question uses and identifiers are written in the words a compiler
# needs. Measured on the 72-query gold set: ETA=0 -> hit@5 0.181, ETA=1.2 ->
# 0.208, ETA=2.4 -> 0.264. Flat from 2.4 through 6.0, so 2.4 is the knee.
ETA = _tunable("ETA", 2.4)

BM25_K1 = _tunable("BM25_K1", 1.5)
BM25_B = _tunable("BM25_B", 0.75)

RECENCY_HALFLIFE_DAYS = _tunable("RECENCY_HALFLIFE_DAYS", 30)
HUB_FRACTION = _tunable("HUB_FRACTION", 0.10)              # files imported by >10% of corpus = hubs
LOW_CONFIDENCE_SCORE = _tunable("LOW_CONFIDENCE_SCORE", 0.5)       # top score below this → flag low-confidence
STALE_INDEX_FILE_THRESHOLD = _tunable("STALE_INDEX_FILE_THRESHOLD", 5)   # N+ changed files since index build → warn

# Lexical-vs-prior balance. When the query has a strong term match somewhere
# in the corpus, scale down the *query-independent* priors (recency, popularity)
# so they act as tie-breakers rather than primary signals.
LEX_STRONG_THRESHOLD = _tunable("LEX_STRONG_THRESHOLD", 2.0)       # max(bm25 + η·prose) ≥ this → strong lexical
LEX_STRONG_PRIOR_SCALE = _tunable("LEX_STRONG_PRIOR_SCALE", 0.25)    # multiplier applied to GAMMA, EPSILON when strong

# Noise-hub penalty. A file with popularity > 0 but no query-derived signal
# (no BM25 / prose hit, no filename match, no path affinity, no def bonus) is
# the classic "noise hub" failure: a popular utility unrelated to the query
# bubbling up purely on its import-count prior. Dock it instead of crediting.
NOISE_HUB_PENALTY = _tunable("NOISE_HUB_PENALTY", 0.8)

# Boost for files that match "callers of X" intent (either define X or import
# a file that defines X). Tunable — kept above prior magnitudes so callers-of
# results dominate the ranking when the mode fires.
CALLERS_BOOST = _tunable("CALLERS_BOOST", 3.0)

# Impl-pattern boost — for interface-heavy codebases (Java, JDK, much of
# Spring/netty/JDBC) where the conceptual name lives on an interface and the
# answer to "how does X work" lives in a `Default*` / `Abstract*` / `*Impl`
# sibling. The interface normally wins filename_match because its full stem
# matches the query verbatim (e.g. `ChannelPipeline.java` matches the token
# `channelpipeline`; `DefaultChannelPipeline.java` does not). When the impl's
# *core* name (stem with prefix/suffix stripped) overlaps the query, we credit
# the impl to compensate. An impl-walkthrough intent in the query
# ("how does X work", "walk me through", "implementation") multiplies the
# boost, since the user is explicitly asking for the impl, not the interface.
IMPL_PREFIXES = ("default", "abstract", "base", "internal")
IMPL_SUFFIXES = ("impl", "internal")
IMPL_BOOST = _tunable("IMPL_BOOST", 0.8)                 # weight per overlapping core token
IMPL_INTENT_MULTIPLIER = _tunable("IMPL_INTENT_MULTIPLIER", 1.5)     # extra when query has impl-walkthrough intent
IMPL_INTENT_RE = re.compile(
    r"\b(implementation|implements?|walk\s*(?:me\s*)?through|how\s+does|how\s+is|"
    r"internals?|actually|under\s+the\s+hood|dispatch(?:es|er)?)\b",
    re.I,
)

# Graph-propagation seed gating. A single match on a low-IDF token like
# `get` produces a small but non-trivial BM25 score in every file that
# contains many `get_*` methods. In a 1k+ file corpus, hundreds of files
# can pass any fixed score floor at once, and they collectively radiate
# thousands of graph_prop points into well-connected destinations, burying
# the actual target. We address this two ways:
#   1) MIN_LEX_SEED: a hard floor below which no file seeds at all.
#   2) MAX_LEX_SEEDS: only the top-N files by lex score actually propagate,
#      so common-token explosions ("matched 'get' in 300 files") cannot
#      all participate simultaneously. Files below the cutoff still rank
#      by their own lexical signal — they just don't *radiate* it.
MIN_LEX_SEED = _tunable("MIN_LEX_SEED", 1.0)
MAX_LEX_SEEDS = _tunable("MAX_LEX_SEEDS", 25)

# Filename-gated graph_prop compression. The raw graph_prop bonus accumulates
# into the thousands for popular receivers, which is *useful* when that file
# is also the lexical answer (e.g. `RestTemplate.java` on a query asking
# about RestTemplate — the canonical popular file ought to win) but *harmful*
# when the popular receiver is a generic utility unrelated to the query
# (e.g. `Log.kt` on a Signal adapter question, `EmbeddedChannel.java` on
# a netty pipeline question).
#
# The gate: a file gets *raw* graph_prop credit only if its filename also
# matches the query (>= GRAPH_FILENAME_GATE tokens). Otherwise its
# graph_prop is log-compressed so it acts as a tie-breaker rather than a
# dominant signal. This keeps canonical-popular files at the top of symbol
# lookups while preventing popular utilities from dominating concept queries.
GRAPH_FILENAME_GATE = _tunable("GRAPH_FILENAME_GATE", 2.0)        # filename-match threshold for ungated graph credit
GRAPH_PROP_SCALE = _tunable("GRAPH_PROP_SCALE", 2.0)           # log-compression scale applied below the gate

# Hard ceiling on graph propagation, relative to the file's OWN lexical score.
#
# The filename gate above was meant to keep graph_prop bounded, but it is
# trivially cleared: a natural-language question contains many words, so any
# well-connected file with two of them in its basename gets *raw* credit. On
# netty, "how many bytes does it ask for on each socket read" gave
# SocketReadPendingTest.java a graph_prop of 333 against a lexical score of 22 —
# graph propagation was 94% of the winning score, and the file that actually
# answers the question ranked 1,716th.
#
# Propagation is evidence that a *neighbour* matched, which is weaker than
# matching yourself. It may promote a plausible file; it must not invent one.
# So graph_prop is now always log-compressed AND capped at a multiple of the
# file's own query-derived score.
GRAPH_PROP_CAP_RATIO = _tunable("GRAPH_PROP_CAP_RATIO", 1.0)       # graph_prop <= this * (own lexical score + floor)
GRAPH_PROP_CAP_FLOOR = _tunable("GRAPH_PROP_CAP_FLOOR", 1.0)       # lets a zero-lexical neighbour still surface, barely

# Test / benchmark / fixture demotion.
#
# Tests restate a concept's vocabulary far more densely than the implementation
# does, which is precisely what BM25 rewards — so on netty the top three results
# for a pipeline question were two testsuite files and a microbenchmark. Tests
# are useful context, but they belong in the paired-test slot, not the headline.
# Suppressed unless the question is explicitly about tests.
TEST_PATH_PENALTY = _tunable("TEST_PATH_PENALTY", 0.35)         # multiplier applied to a test file's positive score
TEST_PATH_RE = re.compile(
    r"(^|/)(tests?|testsuite|androidTest|__tests__|__mocks__|spec|specs|"
    r"microbench|benchmarks?|fixtures?|examples?|testFixtures)(/|$)"
    r"|(^|/)src/test/"
    r"|[._-](test|tests|spec|benchmark)\.[A-Za-z0-9]+$"
    r"|(Test|Tests|TestCase|Spec|Benchmark)\.[A-Za-z0-9]+$",
    re.I,
)
TEST_INTENT_RE = re.compile(r"\b(test|tests|testing|spec|specs|benchmark|fixture|mock)\w*\b", re.I)

TOP_K = _tunable("TOP_K", 5)
FUZZY_MAX_PER_TOKEN = _tunable("FUZZY_MAX_PER_TOKEN", 2)

# Structural matches (filename, path) are scaled by token IDF so a hit on a
# distinctive word counts for more than a hit on `read` or `data`. IDF_REFERENCE
# is roughly the IDF of an ordinarily-informative term in a mid-sized corpus;
# dividing by it keeps the weight near 1.0 there, so ALPHA / BETA keep meaning.
IDF_REFERENCE = _tunable("IDF_REFERENCE", 3.0)
IDF_WEIGHT_CAP = _tunable("IDF_WEIGHT_CAP", 2.0)             # a very rare token is worth at most 2 ordinary ones
MIN_QUERY_TOKEN_IDF = _tunable("MIN_QUERY_TOKEN_IDF", 0.35)       # below this a query token is corpus-wide filler
FUZZY_CUTOFF = _tunable("FUZZY_CUTOFF", 0.85)

CODE_STOPWORDS = set("""
the is a an and or but in on at to for of with by from as this that these those
it its be been being are was were has have had do does did how what where when
why who can could would should will shall may might must
function func method class var const let def public private protected static void
return new self super if else then while for foreach try catch finally throw
true false null nil none undefined import export default async await module
""".split())

# ----------------------------------------------------------------------
# Tokenization
# ----------------------------------------------------------------------

_CAMEL_RE = re.compile(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+|\d+")
_WORD_RE = re.compile(r"\w+", re.UNICODE)
_SPLIT_RE = re.compile(r"[_\-/.:\\]+")


def stem_variant(token: str) -> str:
    """Fold a token to a crude singular/base form for structural matching.

    Deliberately minimal — no Porter stemmer, no dictionary. It exists because
    English questions and code identifiers disagree on number and tense far more
    often than on the root: a question says "bytes", "credentials", "retries",
    "uploading" while the code says `ByteBuf`, `Credential`, `retry`, `upload`.
    Those near-misses cost real recall and one string operation fixes most of
    them. Applied only to structural signals (filename / path / span selection),
    never to the BM25 channels, where changing the term distribution would
    invalidate the IDF table computed over the corpus.
    """
    if len(token) < 4:
        return token
    # Plural folding only. A gerund rule was tried and removed: "string" -> "str"
    # and "setting" -> "sett" collide with real identifiers, and the recall it
    # bought did not cover that.
    for suffix, repl in (("ies", "y"), ("es", ""), ("s", "")):
        if token.endswith(suffix) and len(token) - len(suffix) >= 3:
            return token[:len(token) - len(suffix)] + repl
    return token


def split_identifier(token: str):
    """Split CamelCase, snake_case, kebab-case, dot/slash-separated."""
    out = []
    for part in _SPLIT_RE.split(token):
        if not part:
            continue
        for camel in _CAMEL_RE.findall(part):
            if camel:
                out.append(camel.lower())
    return out


def tokenize(text: str):
    """Lowercase tokens with stopword filter and identifier splitting.

    Both the full raw token and its CamelCase / snake_case sub-tokens
    are emitted, so `getUserProfile` matches both the literal identifier
    and the natural-language query "user profile". Case is preserved
    through `split_identifier` so `_CAMEL_RE` can find the boundaries;
    the lowercased form is stored.
    """
    if not text:
        return []
    out = []
    seen = set()
    for raw in _WORD_RE.findall(text):
        low = raw.lower()
        if low in CODE_STOPWORDS or len(low) < 2:
            continue
        if low not in seen:
            seen.add(low)
            out.append(low)
        for sub in split_identifier(raw):
            if sub in CODE_STOPWORDS or len(sub) < 2:
                continue
            if sub not in seen:
                seen.add(sub)
                out.append(sub)
    return out


def fuzzy_expand(tokens, vocabulary):
    """Expand each query token with up to N close matches from the vocabulary.

    Short tokens (<4 chars) are not expanded — fuzz on short tokens is noise.
    """
    if not vocabulary:
        return list(tokens)
    out = list(tokens)
    seen = set(out)
    for t in tokens:
        if len(t) < 4:
            continue
        for m in difflib.get_close_matches(t, vocabulary, n=FUZZY_MAX_PER_TOKEN, cutoff=FUZZY_CUTOFF):
            if m not in seen:
                seen.add(m)
                out.append(m)
    return out


# ----------------------------------------------------------------------
# Query-intent detection — explicit "callers of X" mode
# ----------------------------------------------------------------------

_CALLERS_PATTERNS = [
    re.compile(r"\bcallers?\s+of\s+([A-Za-z_][\w]*)", re.I),
    re.compile(r"\bwho\s+(?:calls|uses|invokes|references?)\s+([A-Za-z_][\w]*)", re.I),
    re.compile(r"\bwhere\s+is\s+([A-Za-z_][\w]*)\s+(?:called|used|invoked|referenced)", re.I),
    re.compile(r"\b(?:find|list|all)\s+(?:usages?|callers?|references?)\s+(?:of\s+)?([A-Za-z_][\w]*)", re.I),
    re.compile(r"\busages?\s+of\s+([A-Za-z_][\w]*)", re.I),
]


def detect_callers_target(query: str):
    """Return the target symbol if the query looks like 'callers of X', else None."""
    for pat in _CALLERS_PATTERNS:
        m = pat.search(query)
        if not m:
            continue
        target = m.group(1)
        if target.lower() in CODE_STOPWORDS or len(target) < 2:
            continue
        return target
    return None


def find_callers_files(target: str, files: dict, reverse_graph: dict):
    """Locate files that define `target` and the set of files that import them.

    Returns (defining_files, importer_files). The defining files are included
    in the importer set so they show up alongside the callers.
    """
    sym_lower = target.lower()
    defining = set()
    for rel, entry in files.items():
        for sym in entry.get("symbols", []):
            if sym.get("kind") == "def" and sym.get("name", "").lower() == sym_lower:
                defining.add(rel)
                break
    importers = set(defining)
    for df in defining:
        importers.update(reverse_graph.get(df, set()))
    return defining, importers


# ----------------------------------------------------------------------
# Index loading
# ----------------------------------------------------------------------

def find_index_path() -> Path:
    """Locate symbols.json by walking up from cwd, then falling back to script-relative."""
    cwd = Path.cwd()
    for parent in [cwd, *cwd.parents]:
        candidate = parent / ".claude" / "skills" / "tokenhack" / "index" / "symbols.json"
        if candidate.exists():
            return candidate
    return Path(__file__).resolve().parent / "index" / "symbols.json"


def load_index():
    path = find_index_path()
    if not path.exists():
        return None, path
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f), path
    except Exception:
        return None, path


# ----------------------------------------------------------------------
# Corpus preparation
# ----------------------------------------------------------------------

def build_corpus(index):
    """Tokenize per-file content into code and prose channels.

    Returns:
      docs            {rel: {"code": [tokens], "prose": [tokens]}}
      idf_code        {token: idf}
      idf_prose       {token: idf}
      avgdl_code      float
      avgdl_prose     float
      vocabulary      list[str]   (for fuzzy expansion — code channel)
      forward_graph   {rel: set[rel]}   (heuristic A→B "A imports B")
      reverse_graph   {rel: set[rel]}   (B→A "B is imported by A")
      max_popularity  int               (max imported_by count, for normalization)
    """
    files = index.get("files", {})
    prose_top = index.get("prose", {})

    docs = {}
    for rel, entry in files.items():
        code = []
        for sym in entry.get("symbols", []):
            code.extend(tokenize(sym.get("name", "")))
            sig = sym.get("signature", "")
            if sig:
                code.extend(tokenize(sig))
        for ref in entry.get("references", []):
            code.extend(tokenize(ref))
        code.extend(tokenize(rel))

        prose = []
        for p in entry.get("prose_paragraphs", []) or []:
            prose.extend(tokenize(p))
        s = entry.get("docstring_summary", "")
        if s:
            prose.extend(tokenize(s))

        docs[rel] = {"code": code, "prose": prose}

    # Stand-alone prose files (README, docs/*.md) get their own doc entries
    for rel, paras in prose_top.items():
        prose_tokens = [t for p in paras for t in tokenize(p)]
        if rel in docs:
            docs[rel]["prose"].extend(prose_tokens)
        else:
            docs[rel] = {"code": tokenize(rel), "prose": prose_tokens}

    # IDF + average doc length per channel
    def channel_idf_avgdl(channel):
        n = len(docs) or 1
        df = Counter()
        total_len = 0
        for d in docs.values():
            unique = set(d[channel])
            for t in unique:
                df[t] += 1
            total_len += len(d[channel])
        idf = {
            t: math.log((n - k + 0.5) / (k + 0.5) + 1.0)
            for t, k in df.items()
        }
        avgdl = (total_len / n) if total_len else 1.0
        return idf, avgdl

    idf_code, avgdl_code = channel_idf_avgdl("code")
    idf_prose, avgdl_prose = channel_idf_avgdl("prose")

    vocabulary = list(idf_code.keys())

    # Build forward (A→B) from reverse (B's imported_by).
    forward_graph = {rel: set() for rel in files}
    reverse_graph = {rel: set(entry.get("imported_by", [])) for rel, entry in files.items()}
    for rel_b, importers in reverse_graph.items():
        for rel_a in importers:
            forward_graph.setdefault(rel_a, set()).add(rel_b)

    max_pop = max((len(v) for v in reverse_graph.values()), default=0)

    return docs, idf_code, idf_prose, avgdl_code, avgdl_prose, vocabulary, forward_graph, reverse_graph, max_pop


# ----------------------------------------------------------------------
# BM25 + heuristic priors
# ----------------------------------------------------------------------

def bm25_score(query_tokens, doc_tokens, idf, avgdl, matched_out=None):
    """Standard BM25 over a token list."""
    if not doc_tokens:
        return 0.0
    tf = Counter(doc_tokens)
    dl = len(doc_tokens)
    score = 0.0
    for q in query_tokens:
        f = tf.get(q, 0)
        if f == 0:
            continue
        i = idf.get(q, 0.0)
        if i <= 0:
            continue
        contrib = i * (f * (BM25_K1 + 1)) / (f + BM25_K1 * (1 - BM25_B + BM25_B * dl / avgdl))
        score += contrib
        if matched_out is not None and contrib > 0:
            matched_out.add(q)
    return score


def _idf_weight(token, idf):
    """Scale a structural (non-BM25) match by how informative the token is.

    Filename and path matches used to count 1.0 per token regardless of what
    the token was, so a question mentioning `read` and `socket` scored the same
    filename hit as one mentioning `idempotency` and `backoff`. In a long
    natural-language question most tokens are near-worthless — on netty, `netty`
    itself has an IDF of 0.01 — and counting them equally is what let generic
    files win. Normalised against IDF_REFERENCE so weights stay near 1.0 for an
    ordinary term and the existing ALPHA / BETA defaults keep their meaning.
    """
    return min(idf.get(token, 0.0) / IDF_REFERENCE, IDF_WEIGHT_CAP)


def filename_match(query_tokens, rel_path, idf=None):
    """IDF-weighted count of query tokens appearing in the file's basename stem.

    The original (mixed-case) stem is fed to `split_identifier` so it can
    find CamelCase boundaries; the lowercased form is added for whole-name
    matches. `idf=None` falls back to unweighted counting (used by the graph
    gate, which wants a raw token count rather than a score).
    """
    stem = os.path.splitext(os.path.basename(rel_path))[0]
    stem_tokens = set(split_identifier(stem))
    stem_tokens.add(stem.lower())
    stem_tokens |= {stem_variant(t) for t in stem_tokens}
    if idf is None:
        return float(sum(1 for q in query_tokens if q in stem_tokens))
    return float(sum(_idf_weight(q, idf) for q in query_tokens
                     if q in stem_tokens or stem_variant(q) in stem_tokens))


def path_affinity(query_tokens, rel_path, idf=None):
    """IDF-weighted count of query tokens appearing in any path segment."""
    parts = []
    for chunk in re.split(r"[\\/]+", os.path.dirname(rel_path)):
        if not chunk:
            continue
        parts.extend(split_identifier(chunk))
        parts.append(chunk.lower())
    if not parts:
        return 0.0
    parts_set = set(parts)
    parts_set |= {stem_variant(t) for t in parts_set}
    if idf is None:
        return float(sum(1 for q in query_tokens if q in parts_set))
    return float(sum(_idf_weight(q, idf) for q in query_tokens
                     if q in parts_set or stem_variant(q) in parts_set))


def is_test_path(rel_path):
    return bool(TEST_PATH_RE.search(rel_path))


def recency_score(mtime, now):
    """Exponential decay on file age in days."""
    if mtime <= 0:
        return 0.0
    age_days = max(0.0, (now - mtime) / 86400.0)
    return math.pow(0.5, age_days / RECENCY_HALFLIFE_DAYS)


def popularity_score(entry, max_pop):
    if max_pop <= 0:
        return 0.0
    return len(entry.get("imported_by", [])) / max_pop


def definition_bonus(query_tokens, entry):
    """+1 per query token that matches a definition name in this file."""
    def_names = set()
    for sym in entry.get("symbols", []):
        if sym.get("kind") == "def":
            def_names.update(tokenize(sym.get("name", "")))
    if not def_names:
        return 0.0
    return float(sum(1 for q in query_tokens if q in def_names))


def impl_pattern_boost(query_tokens, rel_path, has_impl_intent):
    """Boost files whose basename matches a `Default*` / `Abstract*` / `*Impl`
    impl-pattern AND whose core name (after the prefix/suffix is stripped)
    overlaps the query. Compensates for the interface sibling winning the
    plain filename_match because its full stem matches the query term
    verbatim. See IMPL_BOOST docs at the top of the file."""
    stem = os.path.splitext(os.path.basename(rel_path))[0]
    if not stem:
        return 0.0
    low = stem.lower()
    core = None
    # Prefix patterns: DefaultFoo, AbstractFoo, BaseFoo, InternalFoo.
    # The character after the prefix must be uppercase, otherwise we'd
    # spuriously fire on names like `defaults.py` or `abstractor.java`.
    for pref in IMPL_PREFIXES:
        if low.startswith(pref) and len(stem) > len(pref) and stem[len(pref)].isupper():
            core = stem[len(pref):]
            break
    if core is None:
        # Suffix patterns: FooImpl, FooInternal. The character before the
        # suffix must be lowercase or a digit (so the suffix starts a new
        # CamelCase chunk) — guards against false hits like `Simpl`.
        for suf in IMPL_SUFFIXES:
            if low.endswith(suf) and len(stem) > len(suf):
                end = len(stem) - len(suf)
                if stem[end - 1].islower() or stem[end - 1].isdigit():
                    core = stem[:end]
                    break
    if not core:
        return 0.0
    core_tokens = set(split_identifier(core))
    core_tokens.add(core.lower())
    overlap = sum(1 for q in query_tokens if q in core_tokens)
    if overlap == 0:
        return 0.0
    boost = IMPL_BOOST * overlap
    if has_impl_intent:
        boost *= IMPL_INTENT_MULTIPLIER
    return boost


# ----------------------------------------------------------------------
# Import-graph propagation (2-hop, decay, hub suppression)
# ----------------------------------------------------------------------

def identify_hubs(reverse_graph, n_files):
    """Files imported by more than HUB_FRACTION of the corpus."""
    threshold = max(int(HUB_FRACTION * n_files), 5)
    return {rel for rel, importers in reverse_graph.items() if len(importers) >= threshold}


def graph_propagation(seed_scores, forward_graph, reverse_graph, hub_set):
    """Propagate seed scores to neighbors in both directions, 2 hops with decay.

    Hubs are symmetrically suppressed: they neither propagate score outward
    *nor* receive it from non-hub seeds. Without the receive-side filter, a
    file imported by many seeds (e.g. an `axios.js` utility) accumulates
    massive graph-prop bonus from each seed and outranks the actual target,
    even though it has no query-derived signal of its own — the same noise-
    hub failure mode the popularity penalty addresses, just routed through
    the import graph. Legitimate hits on a hub still rank via BM25 /
    filename / def_bonus / callers_boost on the base score.
    """
    bonus = {}

    def neighbors_of(rel):
        return forward_graph.get(rel, set()) | reverse_graph.get(rel, set())

    one_hop = {}
    for src, score in seed_scores.items():
        if score <= 0 or src in hub_set:
            continue
        for nbr in neighbors_of(src):
            if nbr in hub_set:
                continue
            one_hop[nbr] = one_hop.get(nbr, 0.0) + DELTA_1HOP * score

    two_hop = {}
    for src, score in one_hop.items():
        if score <= 0 or src in hub_set:
            continue
        for nbr in neighbors_of(src):
            if nbr in hub_set:
                continue
            two_hop[nbr] = two_hop.get(nbr, 0.0) + DELTA_2HOP * score

    for k, v in one_hop.items():
        bonus[k] = bonus.get(k, 0.0) + v
    for k, v in two_hop.items():
        bonus[k] = bonus.get(k, 0.0) + v
    return bonus


# ----------------------------------------------------------------------
# Test-file pairing
# ----------------------------------------------------------------------

def find_test_partner(rel, all_rels):
    """Heuristically find a paired test file for a source file (or vice versa)."""
    basename = os.path.basename(rel)
    stem, ext = os.path.splitext(basename)
    if not stem:
        return None
    candidates = set()
    low = stem.lower()
    if low.startswith("test_"):
        candidates.add(stem[5:] + ext)
    elif low.endswith("_test"):
        candidates.add(stem[:-5] + ext)
    elif low.endswith("tests"):
        candidates.add(stem[:-5] + ext)
        candidates.add(stem[:-4] + ext)
    elif low.endswith("test"):
        candidates.add(stem[:-4] + ext)
    else:
        # source → test
        candidates.add(f"test_{stem}{ext}")
        candidates.add(f"{stem}_test{ext}")
        candidates.add(f"{stem}Test{ext}")
        candidates.add(f"{stem}Tests{ext}")
        candidates.add(f"Test{stem}{ext}")
    cand_lower = {c.lower() for c in candidates}
    for other in all_rels:
        if other == rel:
            continue
        if os.path.basename(other).lower() in cand_lower:
            return other
    return None


# ----------------------------------------------------------------------
# Why explainer
# ----------------------------------------------------------------------

def build_why(breakdown):
    """One-line human-readable rationale for the score."""
    parts = []
    matched = sorted(breakdown.get("matched_tokens", set()))
    if matched:
        shown = matched[:3]
        more = f" +{len(matched) - 3}" if len(matched) > 3 else ""
        parts.append("matches '" + ", ".join(shown) + "'" + more)

    flags = []
    if breakdown.get("callers_boost", 0) > 0:
        flags.append("callers-of target")
    if breakdown.get("impl_boost", 0) > 0:
        flags.append("impl-pattern match")
    if breakdown["filename"] >= 1.0:
        flags.append("strong filename match")
    if breakdown["path_aff"] >= 1.0:
        flags.append("path affinity")
    if breakdown["def_bonus"] >= 1.0:
        flags.append("definition site")
    if breakdown["popularity"] >= 0.5 and not breakdown.get("noise_hub"):
        flags.append("popular module")
    if breakdown["recency"] >= 0.7:
        flags.append("recently changed")
    if breakdown["graph_prop"] > 0 and not flags:
        flags.append("via import graph")
    if breakdown["prose"] >= 0.5:
        flags.append("strong prose/doc match")

    if flags:
        parts.append("; ".join(flags[:2]))

    return "; ".join(parts) or "lexical match"


# ----------------------------------------------------------------------
# Index freshness / staleness checks
# ----------------------------------------------------------------------

def check_staleness(index, root: Path):
    """Compare index built-at-commit to HEAD; return warning string or None."""
    built_commit = index.get("built_at_commit", "")
    if not built_commit:
        return None
    try:
        import subprocess
        head = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=3,
        )
        if head.returncode != 0:
            return None
        head_sha = head.stdout.strip()
        if head_sha == built_commit:
            return None
        diff = subprocess.run(
            ["git", "-C", str(root), "diff", "--name-only", f"{built_commit}..{head_sha}"],
            capture_output=True, text=True, timeout=5,
        )
        if diff.returncode != 0:
            return None
        changed = [line for line in diff.stdout.splitlines() if line.strip()]
        n = len(changed)
        if n >= STALE_INDEX_FILE_THRESHOLD:
            return f"[index is {n} files behind HEAD — rebuild advised (run indexer.py)]"
    except Exception:
        return None
    return None


# ----------------------------------------------------------------------
# Main scoring + output
# ----------------------------------------------------------------------

def rank(query: str, index: dict):
    """Score every file in the index for the given query.

    Returns (results, breakdowns, meta) where:
      results:    list of (rel, total_score) sorted desc
      breakdowns: {rel: breakdown_dict}
      meta:       {"callers_target", "callers_defining", "strong_lexical", "max_bm"}
    """
    docs, idf_code, idf_prose, avgdl_code, avgdl_prose, vocabulary, fwd, rev, max_pop = build_corpus(index)
    files = index.get("files", {})

    # Fix #4 — "callers of X" mode detection (runs before tokenization so it
    # still fires even when the bare symbol would be filtered as a stopword).
    callers_target = detect_callers_target(query)
    callers_defining: set = set()
    callers_importers: set = set()
    if callers_target:
        callers_defining, callers_importers = find_callers_files(callers_target, files, rev)

    base_query_tokens = tokenize(query)
    if not base_query_tokens:
        meta = {
            "callers_target": callers_target,
            "callers_defining": callers_defining,
            "strong_lexical": False,
            "max_bm": 0.0,
            "query_tokens": [],
        }
        return [], {}, meta
    # Keep the un-fuzzed tokens for symbol-span attribution (a fuzzy near-match
    # is fine for ranking files but too loose for picking which defs to stage).
    query_tokens = fuzzy_expand(base_query_tokens, vocabulary)

    now = time.time()
    hub_set = identify_hubs(rev, len(files))
    has_impl_intent = bool(IMPL_INTENT_RE.search(query))
    wants_tests = bool(TEST_INTENT_RE.search(query))

    # Drop corpus-wide filler from the query. A natural-language question carries
    # words that are technically in the vocabulary but carry no discrimination
    # ("netty" in netty has IDF 0.01, "data" in a data layer, "app" in an app).
    # BM25 already discounts them, but they were still counted at full weight by
    # filename_match and path_affinity. Keep at least one token so a query made
    # entirely of common words still retrieves something.
    informative = [t for t in query_tokens
                   if max(idf_code.get(t, 0.0), idf_prose.get(t, 0.0)) >= MIN_QUERY_TOKEN_IDF]
    if informative:
        query_tokens = informative

    # Pass 1 — collect raw per-file signals so we can derive a corpus-wide
    # lexical-strength measurement before applying prior weights.
    raw = {}
    for rel, channels in docs.items():
        entry = files.get(rel, {})
        matched: set = set()
        bm = bm25_score(query_tokens, channels["code"], idf_code, avgdl_code, matched_out=matched)
        prose_bm = bm25_score(query_tokens, channels["prose"], idf_prose, avgdl_prose, matched_out=matched)
        fn = filename_match(query_tokens, rel, idf_code)
        pa = path_affinity(query_tokens, rel, idf_code)
        rc = recency_score(entry.get("mtime", 0), now)
        pop = popularity_score(entry, max_pop)
        defb = definition_bonus(query_tokens, entry)
        raw[rel] = {
            "bm25": bm, "prose": prose_bm, "filename": fn, "path_aff": pa,
            "recency": rc, "popularity": pop, "def_bonus": defb,
            "matched": matched,
            # Unweighted token count, used only by the graph gate below, which
            # asks "how many query words are in this name" rather than "how good
            # is this name" — a threshold on a weighted score would drift with
            # corpus size.
            "filename_tokens": filename_match(query_tokens, rel),
            "is_test": is_test_path(rel),
        }

    # Fix #2 — strong-lexical detection. When something in the corpus matches
    # the query strongly, we down-weight the priors (recency, popularity) so
    # the lexical signal stays in charge.
    max_bm = 0.0
    for r in raw.values():
        s = r["bm25"] + ETA * r["prose"]
        if s > max_bm:
            max_bm = s
    strong_lexical = max_bm >= LEX_STRONG_THRESHOLD
    gamma_eff = GAMMA * LEX_STRONG_PRIOR_SCALE if strong_lexical else GAMMA
    epsilon_eff = EPSILON * LEX_STRONG_PRIOR_SCALE if strong_lexical else EPSILON

    breakdowns = {}
    base_scores = {}
    # Lexical seeds drive graph propagation independently of priors. Recency
    # and popularity make almost every file in an actively-modified repo a
    # positive seed under base_scores, which inflates graph_prop into the
    # thousands for well-connected hubs and buries the actual targets. By
    # seeding only on query-derived evidence (BM25 / prose / filename /
    # path / def / callers_boost) we keep priors as tie-breakers in the
    # final ranking while preventing them from poisoning propagation.
    lexical_seeds = {}
    lex_by_file = {}
    for rel, r in raw.items():
        impl_b = impl_pattern_boost(query_tokens, rel, has_impl_intent)

        # Any query-derived signal present? (term hit, filename, path, def, impl)
        has_query_signal = (
            bool(r["matched"]) or r["filename"] > 0
            or r["path_aff"] > 0 or r["def_bonus"] > 0
            or impl_b > 0
        )

        # Fix #3 — noise-hub penalty: popularity is only positive when the
        # file actually matched something query-derived; otherwise dock it.
        if r["popularity"] > 0 and not has_query_signal:
            pop_term = -NOISE_HUB_PENALTY * r["popularity"]
            noise_hub = True
        else:
            pop_term = epsilon_eff * r["popularity"]
            noise_hub = False

        total = (
            r["bm25"]
            + ETA * r["prose"]
            + ALPHA * r["filename"]
            + BETA * r["path_aff"]
            + gamma_eff * r["recency"]
            + pop_term
            + ZETA * r["def_bonus"]
            + impl_b
        )

        # Fix #4 — callers-of boost. Defining files get a double boost so
        # they outrank plain importers.
        callers_boost = 0.0
        if callers_target and rel in callers_importers:
            callers_boost = CALLERS_BOOST
            if rel in callers_defining:
                callers_boost += CALLERS_BOOST
            total += callers_boost

        lex = (
            r["bm25"]
            + ETA * r["prose"]
            + ALPHA * r["filename"]
            + BETA * r["path_aff"]
            + ZETA * r["def_bonus"]
            + impl_b
            + callers_boost
        )

        # Demote tests / benchmarks / fixtures unless the question is about them.
        # Applied to positive scores only, so a penalised file can't be pushed
        # below a file with no signal at all.
        is_test = r["is_test"] and not wants_tests
        if is_test:
            if total > 0:
                total *= TEST_PATH_PENALTY
            lex *= TEST_PATH_PENALTY

        if (lex >= MIN_LEX_SEED and has_query_signal) or callers_boost > 0:
            lexical_seeds[rel] = lex
        lex_by_file[rel] = lex

        if total > 0 or r["matched"] or callers_boost > 0 or impl_b > 0:
            breakdowns[rel] = {
                "bm25": r["bm25"], "prose": r["prose"],
                "filename": r["filename"], "path_aff": r["path_aff"],
                "recency": r["recency"], "popularity": r["popularity"],
                "def_bonus": r["def_bonus"], "graph_prop": 0.0,
                "matched_tokens": r["matched"],
                "callers_boost": callers_boost,
                "impl_boost": impl_b,
                "noise_hub": noise_hub,
                "is_test": is_test,
            }
            base_scores[rel] = total

    # Cap to top-N lexical seeds to prevent common-token explosions
    # ("matched 'get' across 300 files") from collectively flooding the
    # import graph with low-quality propagation. Strong matches (multi-
    # token, filename, def, callers_boost) tend to rank above noise.
    if len(lexical_seeds) > MAX_LEX_SEEDS:
        top_seeds = sorted(lexical_seeds.items(), key=lambda kv: kv[1], reverse=True)[:MAX_LEX_SEEDS]
        lexical_seeds = dict(top_seeds)

    # Graph propagation seeded from lexical signals only (see comment above).
    bonus = graph_propagation(lexical_seeds, fwd, rev, hub_set)
    for rel, raw_b in bonus.items():
        # In callers-of mode with a resolved defining file, restrict graph
        # propagation credit to the actual caller set. Otherwise unrelated
        # files (e.g. agent base classes that happen to match `get`) pile
        # up bonus from the import graph and outrank the real target.
        if callers_target and callers_defining and rel not in callers_importers:
            continue
        # Filename-gated compression: if the file *also* matches the query
        # by name (likely the canonical popular file), keep graph_prop raw.
        # Otherwise log-compress so generic utility hubs can't dominate.
        fn_tokens = raw.get(rel, {}).get("filename_tokens", 0.0)
        if raw_b <= 0:
            b = 0.0
        else:
            # Always compress. The old code handed out *raw* graph_prop to any
            # file clearing the filename gate, which let a well-connected test
            # file accumulate 333 points against a 22-point lexical score.
            b = GRAPH_PROP_SCALE * math.log1p(raw_b)
            if fn_tokens >= GRAPH_FILENAME_GATE:
                b *= 2.0   # the canonical-popular case: name also matches
            # Then cap against the file's own query-derived evidence, so
            # propagation can promote a plausible neighbour but never invent one.
            own_lex = max(lex_by_file.get(rel, 0.0), 0.0)
            b = min(b, GRAPH_PROP_CAP_RATIO * own_lex + GRAPH_PROP_CAP_FLOOR)
            if raw.get(rel, {}).get("is_test") and not wants_tests:
                b *= TEST_PATH_PENALTY
        if rel not in breakdowns:
            breakdowns[rel] = {
                "bm25": 0.0, "prose": 0.0, "filename": 0.0, "path_aff": 0.0,
                "recency": 0.0, "popularity": 0.0, "def_bonus": 0.0,
                "graph_prop": b, "matched_tokens": set(),
                "callers_boost": 0.0, "impl_boost": 0.0, "noise_hub": False,
                "is_test": False,
            }
        else:
            breakdowns[rel]["graph_prop"] = b
        base_scores[rel] = base_scores.get(rel, 0.0) + b

    ranked = sorted(base_scores.items(), key=lambda kv: kv[1], reverse=True)
    meta = {
        "callers_target": callers_target,
        "callers_defining": callers_defining,
        "strong_lexical": strong_lexical,
        "max_bm": max_bm,
        "query_tokens": base_query_tokens,
    }
    return ranked, breakdowns, meta


# ----------------------------------------------------------------------
# Symbol-span staging — point Claude at the matched defs, not whole files
# ----------------------------------------------------------------------

MAX_SPANS_PER_FILE = _tunable("MAX_SPANS_PER_FILE", 4)   # cap so a broad query can't list every def in a file

# A matched *class* (or Swift `extension`, or a top-level Kotlin object) has an
# end_line at the end of the class — i.e. essentially the whole file. Staging
# that span tells Claude "read the entire file", which is exactly the cost
# span-staging exists to avoid, and it's usually accompanied by tight method
# spans nested *inside* the range we just asked for.
#
# Adapters flatten every symbol to kind="def" (see ROADMAP "Symbol kind
# granularity"), so the router can't distinguish a class from a method by kind.
# It can distinguish them by *width*: a span covering more than
# WIDE_SPAN_FRACTION of the file is a container, not a target.
#
# Measured over 12 queries on 6 OSS repos (netty, spring-framework,
# Signal-Android, DuckDuckGo iOS, OwnCloud, blinkit): staging every matched
# span read 68% of the whole-file bytes; dropping container spans in favour of
# the tight ones reads 16%.
WIDE_SPAN_FRACTION = _tunable("WIDE_SPAN_FRACTION", 0.5)


def _file_line_extent(entry):
    """Approximate the file's line count as the furthest line any symbol reaches.

    The index doesn't record a line count, but the last definition in a file
    almost always ends near EOF, which is accurate enough to tell a
    whole-file-width span from a method-width one.
    """
    extent = 0
    for sym in entry.get("symbols", []):
        extent = max(extent,
                     int(sym.get("line", 0) or 0),
                     int(sym.get("end_line", 0) or 0))
    return extent


def symbol_spans(entry, query_tokens, max_spans=MAX_SPANS_PER_FILE):
    """Return up to `max_spans` (start, end, signature) tuples for the file's
    definitions whose name overlaps the query — the precise regions to read.

    Ranked by query-token overlap (desc) then source order. Uses the *un-fuzzed*
    query tokens so a fuzzy near-match on a symbol name can't pull in an
    unrelated def. Returns [] when no def name matches (the file was retrieved
    via prose / import-graph / filename) — the caller then leaves the path bare
    and Claude reads it normally. `end` is 0 when the index predates end_line.

    Container-width spans (a matched class enclosing the whole file) are dropped
    when at least one tighter span also matched; see WIDE_SPAN_FRACTION. If
    *every* match is container-width, only the narrowest is kept, so a
    class-only match still stages one span rather than several overlapping
    whole-file ranges.
    """
    if not query_tokens:
        return []
    qset = set(query_tokens)
    hits = []
    for idx, sym in enumerate(entry.get("symbols", [])):
        if sym.get("kind") != "def":
            continue
        name_tokens = set(tokenize(sym.get("name", "")))
        overlap = len(qset & name_tokens)
        if overlap == 0:
            continue
        start = int(sym.get("line", 0) or 0)
        if start <= 0:
            continue
        end = int(sym.get("end_line", 0) or 0)
        hits.append((overlap, idx, start, end, sym.get("signature", "")))
    if not hits:
        return []

    # Prefer tight spans over containers. A span with no end_line (older index)
    # has unknown width and is treated as tight — it renders as "L12+" anyway.
    extent = _file_line_extent(entry)
    if extent > 0:
        def width(h):
            _, _, start, end, _ = h
            return (end - start + 1) if end and end > start else 0

        tight = [h for h in hits if width(h) <= WIDE_SPAN_FRACTION * extent]
        hits = tight if tight else [min(hits, key=width)]

    hits.sort(key=lambda h: (-h[0], h[1]))
    return [(s, e, sig) for (_, _, s, e, sig) in hits[:max_spans]]


def format_span(start, end):
    """Render a read hint: 'L12-48' when the end is known, else 'L12+' (older index)."""
    if end and end > start:
        return f"L{start}-{end}"
    return f"L{start}+"


def format_output(query, index, ranked, breakdowns, meta, index_path):
    files = index.get("files", {})
    all_rels = list(files.keys())
    lines = []

    # Header line — freshness stamp
    built_at = index.get("built_at", "unknown")
    n_files = index.get("n_files_indexed", 0)
    lines.append(f"[tokenhack: index built {built_at}, {n_files} files indexed]")

    # Coverage warning. The indexer records how many files it had to skip and
    # why, but nothing ever read it — so on a repo written in a language with no
    # adapter (TypeScript before this shipped, Go, Rust, Ruby, C#) the router
    # would confidently rank the handful of files it *could* parse and give no
    # hint that it had ignored most of the codebase. Silent partial coverage is
    # worse than no coverage: it looks like an answer.
    skipped_no_adapter = (index.get("skip_reasons") or {}).get("no-adapter", 0)
    total_seen = n_files + skipped_no_adapter
    if skipped_no_adapter and total_seen and skipped_no_adapter / total_seen >= 0.25:
        lines.append(
            f"[coverage warning: {skipped_no_adapter} of {total_seen} source files "
            f"had no language adapter and are NOT in the index — results below cover "
            f"only {100 * n_files // total_seen}% of this repo. Prefer grep for anything "
            f"in an unindexed language]"
        )

    # Staleness warning (conditional)
    root = index_path.parent.parent.parent.parent.parent if index_path else None
    if root and root.exists():
        warn = check_staleness(index, root)
        if warn:
            lines.append(warn)

    # Callers-of mode banner (Fix #4)
    callers_target = (meta or {}).get("callers_target")
    callers_defining = (meta or {}).get("callers_defining") or set()
    if callers_target:
        if callers_defining:
            lines.append(
                f"[callers-of mode: '{callers_target}' "
                f"({len(callers_defining)} defining file(s) found — reverse-import walk applied)]"
            )
        else:
            lines.append(
                f"[callers-of mode: no definition for '{callers_target}' in index "
                "— falling back to lexical retrieval]"
            )

    # Low-confidence detection:
    #   (a) no ranked results, or
    #   (b) top raw score below threshold, or
    #   (c) top result has no query-derived signal at all
    #       (matched_tokens / filename / def_bonus / callers_boost).
    low_conf = False
    if not ranked:
        low_conf = True
    else:
        top_rel, top_score = ranked[0]
        top_b = breakdowns.get(top_rel, {})
        top_has_signal = (
            bool(top_b.get("matched_tokens"))
            or top_b.get("filename", 0) > 0
            or top_b.get("def_bonus", 0) > 0
            or top_b.get("callers_boost", 0) > 0
            or top_b.get("impl_boost", 0) > 0
        )
        if top_score < LOW_CONFIDENCE_SCORE or not top_has_signal:
            low_conf = True

    if low_conf:
        lines.append(
            "[low-confidence retrieval — paths only, no staged rationale. "
            "Consider rephrasing or use grep directly]"
        )

    lines.append("")
    lines.append(f"Staged context for: {query}")
    lines.append("")

    if not ranked:
        lines.append("  (no matches)")
        return "\n".join(lines)

    # Fix #5 — low-confidence: emit bare paths only (no rationales, no test pairs).
    if low_conf:
        emitted = 0
        for rel, score in ranked:
            if emitted >= TOP_K:
                break
            if score <= 0 and not breakdowns.get(rel, {}).get("matched_tokens"):
                continue
            lines.append(f"  - {rel}")
            emitted += 1
        if emitted == 0:
            lines.append("  (no positive-scoring candidates)")
        return "\n".join(lines)

    # Fix #1 — always emit up to TOP_K paths. The previous budget cap
    # was gating on file size even though we only output paths + a one-line
    # rationale, which made the cap kick in arbitrarily on large files.
    chosen = []
    test_pairs = []
    seen = set()
    for rel, score in ranked:
        if len(chosen) >= TOP_K:
            break
        if score <= 0 and not breakdowns.get(rel, {}).get("matched_tokens"):
            break
        if rel in seen:
            continue
        chosen.append((rel, score))
        seen.add(rel)

        # Test partner — add as a paired result but don't count against TOP_K
        partner = find_test_partner(rel, all_rels)
        if partner and partner not in seen:
            test_pairs.append((rel, partner))
            seen.add(partner)

    query_tokens = (meta or {}).get("query_tokens") or []
    for rel, score in chosen:
        why = build_why(breakdowns.get(rel, {}))
        lines.append(f"  - {rel}  ({why})")
        # Stage precise spans for the matched defs so Claude reads ~40 lines
        # instead of the whole file. No matched def → leave the path bare.
        for start, end, sig in symbol_spans(files.get(rel, {}), query_tokens):
            sig_short = " ".join((sig or "").split())
            if len(sig_short) > 90:
                sig_short = sig_short[:90].rstrip() + "…"
            suffix = f"  {sig_short}" if sig_short else ""
            lines.append(f"      ↳ read {format_span(start, end)}{suffix}")

    if test_pairs:
        # Label by what each file actually IS. When a test ranked in the top-K
        # its partner is the *implementation*, and the old wording printed
        # "DefaultChannelPipeline.java (test for DefaultChannelPipelineTest.java)"
        # — naming the file that answers the question as the test of its own test.
        impl_pairs = [(r, p) for r, p in test_pairs if is_test_path(r)]
        tst_pairs = [(r, p) for r, p in test_pairs if not is_test_path(r)]
        if impl_pairs:
            lines.append("")
            lines.append("Implementations of the tests above (usually the real answer):")
            for tst, impl in impl_pairs:
                lines.append(f"  - {impl}  (implementation under test in {tst})")
        if tst_pairs:
            lines.append("")
            lines.append("Paired test files:")
            for src, tst in tst_pairs:
                lines.append(f"  - {tst}  (test for {src})")

    return "\n".join(lines)


# ----------------------------------------------------------------------
# Entrypoint
# ----------------------------------------------------------------------

def main():
    query = " ".join(sys.argv[1:]).strip()
    if not query:
        print("[tokenhack] usage: router.py <question>", file=sys.stderr)
        print("(no query provided — pass a question as arguments)")
        return 1

    index, path = load_index()
    if index is None:
        print(f"[tokenhack: no index found at {path}]")
        print("Run the indexer to seed the index:")
        print("  python3 .claude/skills/tokenhack/indexer.py")
        print("Then re-run /tokenhack <question>.")
        return 0  # not fatal — Claude can still proceed

    ranked, breakdowns, meta = rank(query, index)
    print(format_output(query, index, ranked, breakdowns, meta, path))
    return 0


if __name__ == "__main__":
    sys.exit(main())
