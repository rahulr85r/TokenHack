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
# ----------------------------------------------------------------------

ALPHA = 1.5         # filename match weight
BETA = 1.0          # path-affinity weight
GAMMA = 0.5         # recency weight (file mtime decay)
EPSILON = 0.3       # symbol popularity weight (incoming imports)
ZETA = 0.5          # definition bonus (per matched def name)
DELTA_1HOP = 1.0    # 1-hop import-graph propagation
DELTA_2HOP = 0.3    # 2-hop import-graph propagation (decayed)
ETA = 1.2           # prose channel weight (docstrings + README/docs)

BM25_K1 = 1.5
BM25_B = 0.75

RECENCY_HALFLIFE_DAYS = 30
HUB_FRACTION = 0.10              # files imported by >10% of corpus = hubs
LOW_CONFIDENCE_SCORE = 0.5       # top score below this → flag low-confidence
STALE_INDEX_FILE_THRESHOLD = 5   # N+ changed files since index build → warn

TOP_K = 5
STAGED_TOKEN_CAP = 2000          # cap on staged context (rough chars/4)
FUZZY_MAX_PER_TOKEN = 2
FUZZY_CUTOFF = 0.85

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
    and the natural-language query "user profile".
    """
    if not text:
        return []
    out = []
    seen = set()
    for raw in _WORD_RE.findall(text.lower()):
        if raw in CODE_STOPWORDS or len(raw) < 2:
            continue
        if raw not in seen:
            seen.add(raw)
            out.append(raw)
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


def filename_match(query_tokens, rel_path):
    """Count query tokens that appear in the file's basename stem."""
    stem = os.path.splitext(os.path.basename(rel_path))[0].lower()
    stem_tokens = set(split_identifier(stem))
    stem_tokens.add(stem)
    return float(sum(1 for q in query_tokens if q in stem_tokens))


def path_affinity(query_tokens, rel_path):
    """Count query tokens that appear in any path-segment of the file."""
    parts = []
    for chunk in re.split(r"[\\/]+", os.path.dirname(rel_path)):
        if not chunk:
            continue
        parts.extend(split_identifier(chunk))
        parts.append(chunk.lower())
    if not parts:
        return 0.0
    parts_set = set(parts)
    return float(sum(1 for q in query_tokens if q in parts_set))


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


# ----------------------------------------------------------------------
# Import-graph propagation (2-hop, decay, hub suppression)
# ----------------------------------------------------------------------

def identify_hubs(reverse_graph, n_files):
    """Files imported by more than HUB_FRACTION of the corpus."""
    threshold = max(int(HUB_FRACTION * n_files), 5)
    return {rel for rel, importers in reverse_graph.items() if len(importers) >= threshold}


def graph_propagation(seed_scores, forward_graph, reverse_graph, hub_set):
    """Propagate seed scores to neighbors in both directions, 2 hops with decay.

    Hubs are not traversed *through* — they can still receive propagation
    (so they show up if directly relevant), but they don't drag their entire
    incoming/outgoing neighborhood along.
    """
    bonus = {}

    def neighbors_of(rel):
        return forward_graph.get(rel, set()) | reverse_graph.get(rel, set())

    one_hop = {}
    for src, score in seed_scores.items():
        if score <= 0:
            continue
        if src in hub_set:
            continue
        for nbr in neighbors_of(src):
            one_hop[nbr] = one_hop.get(nbr, 0.0) + DELTA_1HOP * score

    two_hop = {}
    for src, score in one_hop.items():
        if score <= 0 or src in hub_set:
            continue
        for nbr in neighbors_of(src):
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
    if breakdown["filename"] >= 1.0:
        flags.append("strong filename match")
    if breakdown["path_aff"] >= 1.0:
        flags.append("path affinity")
    if breakdown["def_bonus"] >= 1.0:
        flags.append("definition site")
    if breakdown["popularity"] >= 0.5:
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

def estimate_tokens(entry):
    """Rough chars/4 token estimate for token-budget capping."""
    return max(1, int(entry.get("size_chars", 0) / 4))


def rank(query: str, index: dict):
    """Score every file in the index for the given query.

    Returns (results, top_breakdowns) where:
      results: list of (rel, total_score) sorted desc
      top_breakdowns: {rel: breakdown_dict}
    """
    docs, idf_code, idf_prose, avgdl_code, avgdl_prose, vocabulary, fwd, rev, max_pop = build_corpus(index)
    files = index.get("files", {})

    query_tokens = tokenize(query)
    if not query_tokens:
        return [], {}
    query_tokens = fuzzy_expand(query_tokens, vocabulary)

    now = time.time()
    hub_set = identify_hubs(rev, len(files))

    breakdowns = {}
    base_scores = {}
    for rel, channels in docs.items():
        entry = files.get(rel, {})
        matched = set()
        bm = bm25_score(query_tokens, channels["code"], idf_code, avgdl_code, matched_out=matched)
        prose_bm = bm25_score(query_tokens, channels["prose"], idf_prose, avgdl_prose, matched_out=matched)
        fn = filename_match(query_tokens, rel)
        pa = path_affinity(query_tokens, rel)
        rc = recency_score(entry.get("mtime", 0), now)
        pop = popularity_score(entry, max_pop)
        defb = definition_bonus(query_tokens, entry)

        total = (
            bm
            + ETA * prose_bm
            + ALPHA * fn
            + BETA * pa
            + GAMMA * rc
            + EPSILON * pop
            + ZETA * defb
        )
        if total > 0 or matched:
            breakdowns[rel] = {
                "bm25": bm,
                "prose": prose_bm,
                "filename": fn,
                "path_aff": pa,
                "recency": rc,
                "popularity": pop,
                "def_bonus": defb,
                "graph_prop": 0.0,
                "matched_tokens": matched,
            }
            base_scores[rel] = total

    # Graph propagation from non-zero base hits
    bonus = graph_propagation(base_scores, fwd, rev, hub_set)
    for rel, b in bonus.items():
        if rel not in breakdowns:
            breakdowns[rel] = {
                "bm25": 0.0, "prose": 0.0, "filename": 0.0, "path_aff": 0.0,
                "recency": 0.0, "popularity": 0.0, "def_bonus": 0.0,
                "graph_prop": b, "matched_tokens": set(),
            }
        else:
            breakdowns[rel]["graph_prop"] = b
        base_scores[rel] = base_scores.get(rel, 0.0) + b

    ranked = sorted(base_scores.items(), key=lambda kv: kv[1], reverse=True)
    return ranked, breakdowns


def format_output(query, index, ranked, breakdowns, index_path):
    files = index.get("files", {})
    all_rels = list(files.keys())
    lines = []

    # Header line — freshness stamp
    built_at = index.get("built_at", "unknown")
    n_files = index.get("n_files_indexed", 0)
    lines.append(f"[tokenhack: index built {built_at}, {n_files} files indexed]")

    # Staleness warning (conditional)
    root = index_path.parent.parent.parent.parent.parent if index_path else None
    if root and root.exists():
        warn = check_staleness(index, root)
        if warn:
            lines.append(warn)

    # Low-confidence flag — fires when:
    #   (a) no ranked results at all, or
    #   (b) top raw score below threshold, or
    #   (c) top result has zero matched query tokens (pure popularity/recency
    #       floor with no real term hit — the failure mode actually worth flagging).
    top_matched = bool(breakdowns.get(ranked[0][0], {}).get("matched_tokens")) if ranked else False
    if not ranked or ranked[0][1] < LOW_CONFIDENCE_SCORE or not top_matched:
        lines.append("[low-confidence retrieval — consider rephrasing or use grep directly]")

    lines.append("")
    lines.append(f"Staged context for: {query}")
    lines.append("")

    if not ranked:
        lines.append("  (no matches)")
        return "\n".join(lines)

    # Pick top-K within token budget, plus test-file pairs
    chosen = []
    test_pairs = []
    used_tokens = 0
    seen = set()
    for rel, score in ranked:
        if len(chosen) >= TOP_K:
            break
        if score <= 0 and not breakdowns.get(rel, {}).get("matched_tokens"):
            break
        if rel in seen:
            continue
        entry = files.get(rel, {})
        cost = estimate_tokens(entry)
        if used_tokens + cost > STAGED_TOKEN_CAP and chosen:
            break
        chosen.append((rel, score))
        seen.add(rel)
        used_tokens += cost

        # Test partner — add as a paired result but don't count against TOP_K
        partner = find_test_partner(rel, all_rels)
        if partner and partner not in seen:
            test_pairs.append((rel, partner))
            seen.add(partner)

    for rel, score in chosen:
        why = build_why(breakdowns.get(rel, {}))
        lines.append(f"  - {rel}  ({why})")

    if test_pairs:
        lines.append("")
        lines.append("Paired test files:")
        for src, tst in test_pairs:
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

    ranked, breakdowns = rank(query, index)
    print(format_output(query, index, ranked, breakdowns, path))
    return 0


if __name__ == "__main__":
    sys.exit(main())
