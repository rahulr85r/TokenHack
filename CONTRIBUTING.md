# Contributing to TokenHack

Thank you for considering a contribution. TokenHack is small on purpose, so the bar for additions is "does this help the load-bearing case — making AI coding assistants cheaper to use on large codebases?" — but inside that lane, contributions of every size are welcome.

This document explains how to send your change in.

---

## The short version

1. **Fork** this repo to your own GitHub account.
2. Clone your fork locally, create a branch (`git checkout -b your-change-name`).
3. Make your change, run the indexer + router locally to sanity-check.
4. Push to your fork and **open a pull request** against `rahulr85r/TokenHack:main`.
5. Rahul reviews. You may be asked for changes. Once approved, your PR is merged.

Direct pushes to `main` are blocked for everyone except the repo owner — please don't try to work around this.

---

## Setting up your fork

From your project directory:

```bash
# 1. Click "Fork" on https://github.com/rahulr85r/TokenHack — you now have your own copy.

# 2. Clone YOUR fork (replace YOUR-USERNAME):
git clone https://github.com/YOUR-USERNAME/TokenHack.git
cd TokenHack

# 3. Add the original as "upstream" so you can sync future changes:
git remote add upstream https://github.com/rahulr85r/TokenHack.git
git fetch upstream
```

When you start a new contribution, sync your `main` with upstream first:

```bash
git checkout main
git fetch upstream
git rebase upstream/main
git push origin main
```

---

## Making a change

```bash
# 1. Branch off main with a descriptive name:
git checkout -b add-typescript-adapter

# 2. Make your change. Test it locally — see "Testing your change" below.

# 3. Commit. Use a clear subject and explain the WHY in the body if the change is non-trivial:
git add <files>
git commit -m "Add TypeScript adapter

Extends adapters/javascript.py to handle .ts and .tsx files. The
tree-sitter-typescript grammar shares ~95% of node types with the
JavaScript grammar so most of the existing extraction logic carries
over unchanged."

# 4. Push to your fork:
git push origin add-typescript-adapter

# 5. Open a PR on github.com from your branch -> rahulr85r/TokenHack:main.
#    GitHub will show a "Compare & pull request" button.
```

### Commit style

Short subject (≤ 70 chars), imperative mood ("Add X", not "Added X" or "Adding X"). If the change is more than a couple of lines, add a body explaining **why** the change is needed and what trade-offs you considered. The git history is the durable record of design decisions, so please err on the side of explaining yourself.

No Claude/Copilot/AI attribution lines in commits, please — TokenHack credits humans only. If you used an AI assistant to draft the change, that's fine; just don't co-author the commit with it.

---

## Testing your change

There's no formal test suite yet (one is on the roadmap — see [Contributing opportunities](#contributing-opportunities)). For now, please test by hand:

**1. Index a small repo of your choice with the modified code.**

```bash
cd /path/to/some/repo
python3 -m venv .tokenhack-venv
source .tokenhack-venv/bin/activate
pip install -r /path/to/TokenHack/.claude/skills/tokenhack/requirements.txt
python3 /path/to/TokenHack/.claude/skills/tokenhack/indexer.py --verbose
```

The indexer should complete without errors and write `.claude/skills/tokenhack/index/symbols.json`.

**2. Run the router with a representative query.**

```bash
python3 /path/to/TokenHack/.claude/skills/tokenhack/router.py "<question relevant to that repo>"
```

The router should return a ranked list of files with reasonable rationales. Ideally, eyeball that the top results are actually the right files.

**3. For adapter contributions**, also confirm:
- Symbols are extracted for representative files (run with `--verbose` to see counts).
- Imports are picked up and the reverse-import graph is non-empty.
- Failure modes (malformed source, unsupported syntax) are skipped gracefully, not fatal.

**4. If your change affects ranking behaviour**, please include in the PR description:
- A before/after for at least one real query, ideally on a public repo of >500 files.
- What you tuned and why.

This is the closest thing TokenHack has to "tests" right now — concrete evidence that the change improves something specific without regressing something else.

---

## Contributing opportunities

The canonical, in-depth list is in [`ROADMAP.md`](ROADMAP.md) — near-term well-scoped items, medium-term design-discussion items, long-term speculative ideas, and what's explicitly out of scope.

The internal [`.claude/skills/tokenhack/README.md`](.claude/skills/tokenhack/README.md) has architecture details. The top-level [README's Contributing section](README.md#contributing) lists a shorter quick-pick subset. The highest-impact items right now:

**Sharply scoped, ~few hours of work each:**

- **TypeScript adapter** (`adapters/typescript.py`) — extends the existing JS adapter to `.ts` / `.tsx`. Highest-leverage contribution since most modern frontends are TS.
- **Vue.js adapter** — `.vue` SFCs with embedded `<script>` blocks.
- **Ruby / C# / Rust / Go adapters** — straightforward tree-sitter wrappers following the shape of `adapters/python.py`.

**Larger, please open an issue first to scope:**

- Multi-hop import graph beyond 2-hop (with safeguards against blow-up).
- Git-history co-occurrence as a relevance signal.
- Acronym expansion (`JWT` ↔ "json web token").
- A proper test suite for the router (some kind of golden-set regression).

**Out of scope** (intentionally — these would break TokenHack's load-bearing constraint of "no model, no install, no SaaS"):

- Embeddings, semantic search, neural rerankers.
- Anything requiring a model artifact to ship with the skill.
- Anything requiring a network call at query time.

If you have an idea that fits the constraints, open an issue — happy to discuss before you write code.

---

## Pull request checklist

Before opening your PR, please confirm:

- [ ] Your branch is up to date with `upstream/main`.
- [ ] You ran the indexer + router locally on at least one test repo.
- [ ] Commits have descriptive messages explaining the *why*.
- [ ] No AI attribution lines (`Co-Authored-By: Claude` etc.) in commits.
- [ ] If you changed ranking behaviour, the PR description includes a before/after on a real query.
- [ ] If you added a new language adapter, you've tested it on a real repo of that language.

The PR template (which GitHub will populate automatically) walks you through this.

---

## Review process

Rahul reads every PR personally. Expect:

- A first response within a few days.
- Inline comments asking for clarification or change.
- Sometimes a "love the idea, let me think about scope" comment for larger changes.
- Once approved: squash-merge into `main`.

If you don't hear back after a week, feel free to ping the PR — it has likely just slid down the queue.

---

## Code of Conduct

This project follows the [Contributor Covenant](CODE_OF_CONDUCT.md). By participating, you agree to abide by its terms. Behaviour that violates the Code of Conduct should be reported to the contact email in that document.

---

## License

By contributing, you agree that your contributions will be licensed under the same [MIT License](LICENSE) that covers the project. You retain copyright on your contributions; the MIT license covers everyone else's right to use them.
