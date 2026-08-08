# TokenHack evaluation harness

Until this existed, TokenHack had ~20 hand-set scoring constants and no way to
tell whether changing one helped. This is that way.

```bash
python3 tests/eval.py                 # score every gold set you have repos for
python3 tests/eval.py --verbose       # per-query, including every miss and what won instead
python3 tests/eval.py --markdown      # table for the README
python3 tests/eval.py --save-baseline # record current scores
python3 tests/eval.py --check         # exit 1 if scores regressed
```

## What it measures

| Metric | Meaning |
|---|---|
| `hit@5` | Did **any** gold file make the top 5? This is the one that matters — Claude only needs one correct entry point. |
| `recall@5` | What fraction of the gold files made the top 5? |
| `MRR` | 1/rank of the first gold hit. Rewards ranking it *first*, not fifth. |

Results are also split by `vocabulary_overlap`:

- **`high`** — the question's words literally appear in the answer's filename or
  symbols. Lexical retrieval's home ground.
- **`low`** — the question uses different words than the code does ("what stops
  double-charging" vs `IdempotencyKey`). This is the case lexical retrieval is
  *expected* to lose, and it is reported separately rather than averaged in,
  because averaging hides exactly the weakness a reader needs to know about.

## The gold sets

`tests/gold/<repo>.json` — 72 questions across 6 open-source repos
(spring-framework, netty, Signal-Android, DuckDuckGo iOS, ownCloud Android, and
a small full-stack JS app), each paired with the file(s) that actually answer it.

Two properties make the numbers worth trusting:

1. **Built blind.** The gold sets were produced by agents that explored each
   repository with nothing but glob/grep/read and were explicitly forbidden from
   running the router or reading its index. The questions were written first and
   the answers found second, so the set is not shaped around what TokenHack
   happens to be good at.
2. **Verified.** Every gold path is checked to exist, and each carries a `why`
   naming the class/function and line region that answers the question.

62% of the questions are deliberately `vocabulary_overlap: low`. That makes the
headline number look worse than a friendlier set would, which is the point — a
gold set you tuned against is a gold set that tells you nothing.

## Adding to a gold set

```json
{
  "question": "What stops the camera-roll backup from re-sending the same photos every run?",
  "gold_files": ["app/src/main/java/.../AutomaticUploadsWorker.kt"],
  "why": "getFilesReadyToUpload (239-265) filters to lastSyncTimestamp..<currentTimestamp; updateTimestamp (217-237) persists the window end.",
  "difficulty": "medium",
  "vocabulary_overlap": "low"
}
```

Write the question the way you'd actually type it. If you find yourself
choosing words because they appear in the filename, you're writing a benchmark
that flatters the tool instead of measuring it.

## Ablating a constant

Every tunable in `router.py` reads an optional `TOKENHACK_<NAME>` environment
override, so you can measure a weight instead of arguing about it:

```bash
TOKENHACK_ETA=0 python3 tests/eval.py            # prose channel off
TOKENHACK_TEST_PATH_PENALTY=1.0 python3 tests/eval.py   # test demotion off
TOKENHACK_DELTA_1HOP=0 TOKENHACK_DELTA_2HOP=0 python3 tests/eval.py
```

**If you change a scoring constant, put the before/after numbers in the PR.**
That's the whole reason this directory exists.

## Reproducing the numbers

```bash
pip install -r .claude/skills/tokenhack/requirements.txt
python3 tests/fetch_repos.py     # clones + indexes the pinned public repos
python3 tests/eval.py
```

`tests/repos.json` pins each public repo to the commit its gold set was written
against, so the scores are reproducible rather than something you have to take
on trust. Two of the six evaluated repos aren't in the manifest — the
DuckDuckGo iOS checkout was a source snapshot rather than a clone, and the
JS app is private — but their gold sets ship anyway; point `--repos-dir` at your
own copies, and the harness skips whatever it can't find.
