# TokenHack

### Cut ~80% of the tokens your AI coding agent burns searching your codebase.

Every time you ask an AI agent *"where does X live?"*, it greps, gets hundreds of filenames back, opens the wrong files, and opens more — thousands of tokens gone before it reads a line that matters. TokenHack hands it a ranked shortlist first, from an index built in CI and committed next to your code. Measured across 72 questions on 6 real codebases: **~80% fewer tokens per search**, with **a correct file in the agent's top five 90% of the time** (26% without it).

It costs nothing to run: no embeddings, no vector database, no service, no network call at query time, and nothing for developers to install. The whole retriever is ~1,000 lines of dependency-free Python that lives inside your repo, and the index rebuilds itself in CI on every push. Every number above is reproducible — the question set and scoring harness ship in [`tests/`](tests/).

---

## Install — three steps, five minutes

### 1. Copy the skill into your repo

```bash
git clone --depth 1 https://github.com/rahulr85r/TokenHack /tmp/tokenhack
mkdir -p .claude/skills
cp -R /tmp/tokenhack/.claude/skills/tokenhack .claude/skills/
rm -rf /tmp/tokenhack
```

### 2. Add one line to your `CLAUDE.md`

Create the file at your repo root if it doesn't exist:

```markdown
For codebase-wide questions, use `/tokenhack <question>` first to pre-stage context.
```

### 3. Add the CI workflow

Create `.github/workflows/tokenhack-index.yml`:

```yaml
name: tokenhack-index
on:
  push:
    branches: [main]
    paths-ignore: ['.claude/skills/tokenhack/index/**']
permissions:
  contents: write
jobs:
  index:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: '3.11' }
      - run: pip install -r .claude/skills/tokenhack/requirements.txt
      - run: python3 .claude/skills/tokenhack/indexer.py
      - run: |
          if [[ -n "$(git status --porcelain .claude/skills/tokenhack/index/)" ]]; then
            git config user.name "github-actions[bot]"
            git config user.email "github-actions[bot]@users.noreply.github.com"
            git add .claude/skills/tokenhack/index/
            git commit -m "[tokenhack] update index"
            git push
          fi
```

One-time repo setting: **Settings → Actions → General → Workflow permissions → "Read and write permissions"**.

If your `main` branch blocks direct pushes, use the PR-based workflow in [`INSTALL.md`](INSTALL.md) instead — same job, but the bot opens a pull request you merge.

### Seed the index once

So `/tokenhack` works before CI first runs:

```bash
python3 -m venv .tokenhack-venv && source .tokenhack-venv/bin/activate
pip install -r .claude/skills/tokenhack/requirements.txt
python3 .claude/skills/tokenhack/indexer.py
deactivate && rm -rf .tokenhack-venv
git add .claude/skills/tokenhack/index && git commit -m "Seed TokenHack index"
```

Done. On GitLab, CircleCI or Jenkins? Snippets are in [`INSTALL.md`](INSTALL.md), along with troubleshooting.

---

## Use it

```
/tokenhack how does the payment retry queue handle idempotency conflicts?
```

The agent receives a block like this, then answers from those exact line ranges instead of exploring your tree:

```
[tokenhack: index built 2026-08-08, 1,211 files indexed]

Staged context for: how does the payment retry queue handle idempotency conflicts?

  - core/payments/retry_queue.py  (matches 'retry, queue'; strong filename match)
      ↳ read L84-141   class RetryQueue:
      ↳ read L210-236  def handle_conflict(self, key):
  - core/payments/idempotency.py  (matches 'idempotency'; definition site)
      ↳ read L12-58    class IdempotencyKey:
  ...
```

After the first use in a session, the agent will offer to re-stage on later codebase-wide questions (the rules live in [`SKILL.md`](.claude/skills/tokenhack/SKILL.md); say *"just answer"* to turn that off).

**Not using Claude Code?** The retriever is an ordinary CLI — anything that can run a shell command can drive it:

```bash
python3 .claude/skills/tokenhack/router.py "your question" --terms "identifiers the codebase probably uses"
```

---

## How it works

**The indexer** (runs in CI) walks your code with [tree-sitter](https://tree-sitter.github.io/tree-sitter/) — the same parsing engine behind your editor's syntax highlighting — and writes one line per definition into a JSON index: name, file, start line, end line, signature, and the doc comment above it. Plus a map of which files import which. That's the whole index: a list of what's where, like the back of a textbook.

**The router** (runs at query time, pure stdlib) scores every file against your question: BM25 over two channels (symbols and doc comments), filename and path boosts, definition-site bonuses, import-graph nudges, and test-file demotion. Every scoring constant is overridable via `TOKENHACK_<NAME>` environment variables and measured against the gold set in `tests/` — if you tune one, the harness tells you whether you made retrieval better or worse.

**The agent bridge** is the part that matters most. Lexical matching fails when your words aren't the code's words — you say *"charging twice"*, the code says `IdempotencyKey`. So before searching, the agent writes down 8–12 terms the codebase probably uses and passes them via `--terms`. The agent already knows both languages; asking it costs ~30 tokens. This single step took top-five accuracy from 26% to 90% on our test set. `SKILL.md` makes it automatic.

**Span staging** means results carry `↳ read L120-160` hints, so the agent reads the ~40 relevant lines, not whole files.

---

## Supported languages

Python · TypeScript/TSX · JavaScript/JSX · Java · Kotlin · Swift

Files in other languages are skipped without breaking the build, and the router warns in its header when a large share of your repo isn't covered. Adding a language is one small file — [`typescript.py`](.claude/skills/tokenhack/adapters/typescript.py) is 60 lines. Most-wanted: Go, Rust, C#, Ruby ([ROADMAP](ROADMAP.md)).

---

## The numbers

Correct file in the agent's top five, first try — 72 blind questions, 6 codebases:

| Codebase | Lexical only | With the agent bridge |
|---|---:|---:|
| netty (3,512 files) | 33% | **100%** |
| Signal-Android (5,534) | 0% | **100%** |
| DuckDuckGo iOS (1,211) | 42% | **92%** |
| spring-framework (9,487) | 17% | **75%** |
| **All 72 questions** | **26%** | **90%** |

Token cost: a typical unaided search runs 5,000–12,000 tokens (measured: one `grep` alone returns a median 4,360 tokens of filenames). TokenHack answers in ~980 — **71–82% saved per search** depending on repo size.

Reproduce everything:

```bash
pip install -r .claude/skills/tokenhack/requirements.txt
python3 tests/fetch_repos.py     # clones + indexes the pinned public repos
python3 tests/eval.py
```

The gold set was built blind — by agents that could read the codebases but not run the tool — and 62% of the questions share no words with their answer's filename. Details in [`tests/README.md`](tests/README.md).

---

## Where it breaks

- **Every figure is from the six codebases it was tuned on.** Expect worse on yours; Spring (thousands of near-identical class names) is the weakest at 75%.
- **A miss costs more than not invoking** — you pay for the shortlist and the agent still searches. On small or familiar repos, skip it.
- **Big repos**: queries take ~2.5s at 9,500 files, and the committed index (~4–6 KB/file) meets GitHub's 100 MB blob limit somewhere around 15–20k files.
- **The index tracks `main`** — code on your feature branch that only just landed isn't in it. The router warns when the index is stale.

---

## Contributing

The easiest contribution needs no code: **run it on your repo, and when it puts the wrong file first, open an issue with the question and the file you expected.** That's a gold-set entry, and gold-set entries are the most valuable thing this project can receive.

Code contributions, in rough order of impact:

1. **Language adapters** — Go, Rust, C#, Ruby, Vue. Model on `typescript.py`, ~60 lines each.
2. **Gold questions for new codebases** — grow `tests/gold/`.
3. **Scoring improvements** — any PR that touches a constant must include before/after output from `python3 tests/eval.py`. That harness exists so nobody has to argue from taste, including the maintainer.

See [`CONTRIBUTING.md`](CONTRIBUTING.md) for workflow and [`ROADMAP.md`](ROADMAP.md) for scoped items. Internal architecture: [`.claude/skills/tokenhack/README.md`](.claude/skills/tokenhack/README.md).

---

## Why "TokenHack"

Hacking in the make-do-with-what-you-have sense — and hacking *down* the token bill. Not security tokens.

## License

MIT — see [LICENSE](LICENSE).

Rahul R · [github.com/rahulr85r](https://github.com/rahulr85r)
