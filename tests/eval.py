#!/usr/bin/env python3
"""TokenHack retrieval evaluation harness.

Scores the router against gold sets in `tests/gold/*.json` — question paired
with the file(s) that actually answer it — and reports recall, hit rate and MRR.

    python3 tests/eval.py                          # score every gold set found
    python3 tests/eval.py --repo netty             # one repo
    python3 tests/eval.py --save-baseline          # record current scores
    python3 tests/eval.py --check                  # fail (exit 1) on regression
    python3 tests/eval.py --markdown               # emit a table for the README
    python3 tests/eval.py --verbose                # per-query detail incl. misses

Gold sets are JSON, not YAML, so the harness stays stdlib-only like the router.

Each gold set names a repo that must exist next to this checkout (or be pointed
at with --repos-dir). Repos that aren't present locally are skipped with a
notice rather than failing the run — CI and contributors won't have all six.

Why these metrics:
  hit@k      did ANY gold file make the top k? This is the one that matters —
             Claude only needs one good entry point.
  recall@k   what fraction of the gold files made the top k?
  MRR        1/rank of the first gold hit; rewards ranking it first, not fifth.

The `low` vocabulary-overlap slice is reported separately on purpose. Those are
the questions whose words do NOT appear in the code, which is exactly where
lexical retrieval is expected to lose to embeddings. Averaging them in hides
the tool's real weakness; splitting them out is the honest presentation.
"""

import argparse
import json
import statistics
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent
GOLD_DIR = HERE / "gold"
ROUTER = REPO_ROOT / ".claude" / "skills" / "tokenhack" / "router.py"
BASELINE = HERE / "baseline.json"

TOP_K = 5
REGRESSION_TOLERANCE = 0.02   # allow 2 points of noise before calling it a regression


# ----------------------------------------------------------------------
# Running the router
# ----------------------------------------------------------------------

BRIDGE = HERE / "bridge_terms.json"


def load_bridge():
    """Pre-generated code-vocabulary guesses, standing in for the model's.

    In real use the agent invoking the skill writes these itself (see SKILL.md
    STEP 1). For evaluation they were generated once, from the questions alone —
    the generating agents were given a plain list of questions with no gold
    files, no `why` field and no access to the repositories, so the terms are a
    guess and not a lookup.
    """
    if not BRIDGE.exists():
        return {}
    return json.loads(BRIDGE.read_text())


def run_router(repo_dir: Path, query: str, terms: str = ""):
    """Return (ranked_paths, raw_stdout). Paths exclude the test-pair section."""
    argv = [sys.executable, str(ROUTER), query]
    if terms:
        argv += ["--terms", terms]
    proc = subprocess.run(
        argv,
        cwd=str(repo_dir), capture_output=True, text=True, timeout=180,
    )
    out = proc.stdout
    paths, in_pairs = [], False
    for line in out.splitlines():
        stripped = line.strip()
        if stripped.startswith("Paired test files:"):
            in_pairs = True
            continue
        if in_pairs or not stripped.startswith("- "):
            continue
        paths.append(stripped[2:].split("  (")[0].strip())
    return paths, out


# ----------------------------------------------------------------------
# Metrics
# ----------------------------------------------------------------------

def score_query(ranked, gold, k=TOP_K):
    top = ranked[:k]
    gold_set = {g.strip() for g in gold}
    found = [g for g in gold_set if g in top]
    rank = None
    for i, p in enumerate(top, start=1):
        if p in gold_set:
            rank = i
            break
    return {
        "hit": 1.0 if found else 0.0,
        "recall": len(found) / len(gold_set) if gold_set else 0.0,
        "rr": (1.0 / rank) if rank else 0.0,
        "rank": rank,
        "top": top,
    }


def aggregate(rows):
    if not rows:
        return {"n": 0, "hit@5": 0.0, "recall@5": 0.0, "mrr": 0.0, "tokens": 0}
    return {
        "n": len(rows),
        "hit@5": statistics.mean(r["hit"] for r in rows),
        "recall@5": statistics.mean(r["recall"] for r in rows),
        "mrr": statistics.mean(r["rr"] for r in rows),
        "tokens": round(statistics.mean(r["tokens"] for r in rows)),
    }


# ----------------------------------------------------------------------
# Reporting
# ----------------------------------------------------------------------

def fmt(agg):
    return (f"n={agg['n']:<3d} hit@5={agg['hit@5']:.2f}  recall@5={agg['recall@5']:.2f}  "
            f"MRR={agg['mrr']:.2f}  staged~{agg['tokens']}tok")


def markdown_table(per_repo, overall, by_overlap):
    lines = ["| Repo | Queries | hit@5 | recall@5 | MRR | Staged tokens |",
             "|---|---:|---:|---:|---:|---:|"]
    for repo in sorted(per_repo):
        a = per_repo[repo]
        lines.append(f"| {repo} | {a['n']} | {a['hit@5']:.2f} | {a['recall@5']:.2f} "
                     f"| {a['mrr']:.2f} | {a['tokens']} |")
    lines.append(f"| **All** | **{overall['n']}** | **{overall['hit@5']:.2f}** "
                 f"| **{overall['recall@5']:.2f}** | **{overall['mrr']:.2f}** "
                 f"| **{overall['tokens']}** |")
    lines.append("")
    lines.append("| Question type | Queries | hit@5 | recall@5 | MRR |")
    lines.append("|---|---:|---:|---:|---:|")
    for label, key in (("Words appear in the code", "high"),
                       ("Vocabulary mismatch", "low")):
        a = by_overlap.get(key)
        if a and a["n"]:
            lines.append(f"| {label} | {a['n']} | {a['hit@5']:.2f} "
                         f"| {a['recall@5']:.2f} | {a['mrr']:.2f} |")
    return "\n".join(lines)


# ----------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description="TokenHack retrieval evaluation")
    ap.add_argument("--repos-dir", type=Path, default=REPO_ROOT.parent,
                    help="Directory containing the evaluated repos (default: alongside this checkout)")
    ap.add_argument("--repo", action="append", default=None,
                    help="Only evaluate this repo (repeatable)")
    ap.add_argument("--save-baseline", action="store_true", help="Write current scores to tests/baseline.json")
    ap.add_argument("--check", action="store_true", help="Exit 1 if scores regressed against the baseline")
    ap.add_argument("--markdown", action="store_true", help="Print a markdown table")
    ap.add_argument("--verbose", action="store_true", help="Per-query detail, including every miss")
    ap.add_argument("--no-bridge", action="store_true",
                    help="Lexical-only: skip the model-supplied code-vocabulary terms")
    ap.add_argument("--json", type=Path, default=None, help="Write full results to this path")
    args = ap.parse_args()

    gold_files = sorted(GOLD_DIR.glob("*.json"))
    if not gold_files:
        print(f"No gold sets in {GOLD_DIR}", file=sys.stderr)
        return 2

    bridge = {} if args.no_bridge else load_bridge()
    per_repo, all_rows, skipped = {}, [], []
    by_overlap = {"high": [], "low": []}
    detail = []

    for gf in gold_files:
        gold = json.loads(gf.read_text(encoding="utf-8"))
        repo = gold["repo"]
        if args.repo and repo not in args.repo:
            continue
        repo_dir = args.repos_dir / repo
        if not (repo_dir / ".claude/skills/tokenhack/index/symbols.json").exists():
            skipped.append(repo)
            continue

        rows = []
        bmap = bridge.get(repo, {})
        for q in gold["queries"]:
            ranked, raw = run_router(repo_dir, q["question"], bmap.get(q["question"], ""))
            s = score_query(ranked, q["gold_files"])
            s["tokens"] = round(len(raw) / 4)
            rows.append(s)
            ov = q.get("vocabulary_overlap", "high")
            by_overlap.setdefault(ov, []).append(s)
            detail.append({
                "repo": repo, "question": q["question"], "gold": q["gold_files"],
                "rank": s["rank"], "hit": s["hit"], "top": s["top"],
                "vocabulary_overlap": ov, "difficulty": q.get("difficulty"),
            })
            if args.verbose:
                mark = f"rank {s['rank']}" if s["rank"] else "MISS"
                print(f"  [{mark:>6}] ({ov:4}) {q['question'][:64]}")
                if not s["rank"]:
                    print(f"            want: {q['gold_files'][0]}")
                    for p in s["top"][:3]:
                        print(f"             got: {p}")

        per_repo[repo] = aggregate(rows)
        all_rows.extend(rows)
        print(f"{repo:<20s} {fmt(per_repo[repo])}")

    if not all_rows:
        print("No repos available to evaluate.", file=sys.stderr)
        return 2

    overall = aggregate(all_rows)
    overlap_agg = {k: aggregate(v) for k, v in by_overlap.items() if v}

    print("-" * 78)
    print(f"{'OVERALL':<20s} {fmt(overall)}")
    for k in ("high", "low"):
        if k in overlap_agg:
            label = "words in code" if k == "high" else "vocab mismatch"
            print(f"{'  ' + label:<20s} {fmt(overlap_agg[k])}")
    if skipped:
        print(f"\nskipped (no local index): {', '.join(skipped)}")

    if args.markdown:
        print("\n" + markdown_table(per_repo, overall, overlap_agg))

    payload = {"overall": overall, "per_repo": per_repo,
               "by_overlap": overlap_agg, "detail": detail}
    if args.json:
        args.json.write_text(json.dumps(payload, indent=2))
    if args.save_baseline:
        BASELINE.write_text(json.dumps({"overall": overall, "per_repo": per_repo,
                                        "by_overlap": overlap_agg}, indent=2) + "\n")
        print(f"\nbaseline written to {BASELINE}")

    if args.check:
        if not BASELINE.exists():
            print("\nno baseline to check against — run --save-baseline first", file=sys.stderr)
            return 2
        base = json.loads(BASELINE.read_text())
        regressions = []
        for metric in ("hit@5", "recall@5", "mrr"):
            was, now = base["overall"][metric], overall[metric]
            if now < was - REGRESSION_TOLERANCE:
                regressions.append(f"{metric}: {was:.3f} -> {now:.3f}")
        if regressions:
            print("\nREGRESSION:", "; ".join(regressions), file=sys.stderr)
            return 1
        print("\nno regression against baseline")
    return 0


if __name__ == "__main__":
    sys.exit(main())
