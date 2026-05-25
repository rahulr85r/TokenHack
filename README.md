# TokenHack

**Stop your AI coding assistant from burning tokens grepping its way around your codebase.**

TokenHack ships as a [Claude Code](https://docs.claude.com/en/docs/claude-code/overview) skill that lives inside your repo. When you ask a codebase-wide question, it pre-stages the most relevant files using a local, pure-Python hybrid retrieval engine — so Claude reads ~5 targeted files instead of exploring your tree.

Zero per-developer install. Zero external tooling. No embeddings model, no API calls, no SaaS. The index is built by CI and committed alongside your code; the retrieval is plain Python stdlib running on the developer's own machine.

---

## Who this is for

- Engineers working on **existing, extensive codebases** (large monorepos, mature services, legacy systems).
- Engineers using **token-intensive AI coding tools** (Claude Code, Cursor, etc.) who keep hitting usage limits.
- Engineers in **organizations that block external tooling**, can't deploy a SaaS retrieval service, and can't ship a model file to every laptop.

If your team is small, your repo fits comfortably in Claude's context window, or you're greenfield — you probably don't need this yet.

---

## How it works

Three pieces, all checked into your repo:

1. **Indexer** (`indexer.py`). Runs in CI on every push. Walks the repo with tree-sitter, extracts symbols + imports + docstrings, builds a compact JSON index. Incremental (file-hash keyed) so big repos rebuild quickly. Commits the updated index back.
2. **Router** (`router.py`). Pure Python stdlib. Runs locally on the developer's machine when `/tokenhack` is invoked. Tokenizes the question, ranks files via BM25 + 2-hop import graph + heuristic priors (filename, path-affinity, recency, symbol popularity, definition-vs-reference). Returns top-K paths with a one-line "why each matches".
3. **Claude Code skill** (`SKILL.md`). The `/tokenhack` slash command. Inlines the router's output into Claude's context, then teaches Claude a [three-gate ruleset](#the-three-gate-nudge-ruleset) for suggesting `/tokenhack` again on future codebase-wide questions in the same session.

Zero passive token cost — the skill is **invisible to Claude** until the developer explicitly types `/tokenhack`, thanks to `disable-model-invocation: true` in the frontmatter.

---

## Install — 3 steps

### Step 1. Copy the skill directory into your repo

```bash
# from your project's repo root:
git clone https://github.com/rahulr85r/TokenHack.git /tmp/tokenhack
cp -R /tmp/tokenhack/.claude/skills/tokenhack ./.claude/skills/
```

Or just download the [latest release](https://github.com/rahulr85r/TokenHack/releases) and copy the `.claude/skills/tokenhack/` directory.

### Step 2. Add one line to your project's `CLAUDE.md`

Open (or create) `CLAUDE.md` at your repo root and add this single line:

```markdown
For codebase-wide questions, use `/tokenhack <question>` first to pre-stage context.
```

That's ~20 tokens loaded once per Claude Code session — Claude itself will then suggest `/tokenhack` to your developers when their question looks codebase-wide.

### Step 3. Wire up CI to keep the index fresh

#### GitHub Actions (recommended path)

Copy the workflow from this repo into yours:

```bash
mkdir -p .github/workflows
cp /tmp/tokenhack/.github/workflows/tokenhack-index.yml .github/workflows/
```

**One-time repo setting:** GitHub → Settings → Actions → General → **Workflow permissions** → set to *"Read and write permissions"*. This lets the workflow commit the rebuilt index back to your repo.

**First-time seed.** Run the indexer locally once to seed an initial index so `/tokenhack` works before CI catches up:

```bash
python3 -m venv .tokenhack-venv
source .tokenhack-venv/bin/activate
pip install -r .claude/skills/tokenhack/requirements.txt
python3 .claude/skills/tokenhack/indexer.py
deactivate
git add .claude/skills/tokenhack/index/
git commit -m "Seed TokenHack index"
git push
```

After this initial seed, CI takes over and rebuilds the index automatically on every push.

**How the workflow protects against recursive triggers.** The provided workflow uses `paths-ignore: ['.claude/skills/tokenhack/index/**']`, so the bot's own index commit doesn't trigger another rebuild.

#### GitLab CI

```yaml
# .gitlab-ci.yml
tokenhack-index:
  stage: build
  only:
    refs: [main]
    changes_not:
      - .claude/skills/tokenhack/index/**
  image: python:3.11
  script:
    - pip install -r .claude/skills/tokenhack/requirements.txt
    - python3 .claude/skills/tokenhack/indexer.py
    - |
      if [ -n "$(git status --porcelain .claude/skills/tokenhack/index/)" ]; then
        git config user.email "ci@example.com"
        git config user.name "GitLab CI"
        git add .claude/skills/tokenhack/index/
        git commit -m "[tokenhack] update index"
        git push https://gitlab-ci-token:${CI_PUSH_TOKEN}@$CI_SERVER_HOST/$CI_PROJECT_PATH.git HEAD:$CI_COMMIT_REF_NAME
      fi
```

(Requires a `CI_PUSH_TOKEN` CI/CD variable with write access — GitLab uses Personal Access Tokens or Project Access Tokens.)

#### CircleCI

```yaml
# .circleci/config.yml — relevant job
jobs:
  tokenhack-index:
    docker:
      - image: cimg/python:3.11
    steps:
      - checkout
      - run:
          name: Rebuild index
          command: |
            pip install -r .claude/skills/tokenhack/requirements.txt
            python3 .claude/skills/tokenhack/indexer.py
      - run:
          name: Commit if changed
          command: |
            if [ -n "$(git status --porcelain .claude/skills/tokenhack/index/)" ]; then
              git config user.email "ci@example.com"
              git config user.name "CircleCI"
              git add .claude/skills/tokenhack/index/
              git commit -m "[tokenhack] update index"
              git push origin $CIRCLE_BRANCH
            fi
```

(Requires a deploy key or token with push access on the repo.)

#### Jenkins

```groovy
pipeline {
  agent { docker { image 'python:3.11' } }
  stages {
    stage('Rebuild index') {
      steps {
        sh 'pip install -r .claude/skills/tokenhack/requirements.txt'
        sh 'python3 .claude/skills/tokenhack/indexer.py'
      }
    }
    stage('Commit') {
      when { branch 'main' }
      steps {
        sh '''
          if [ -n "$(git status --porcelain .claude/skills/tokenhack/index/)" ]; then
            git config user.email "ci@example.com"
            git config user.name "Jenkins"
            git add .claude/skills/tokenhack/index/
            git commit -m "[tokenhack] update index"
            git push origin HEAD:main
          fi
        '''
      }
    }
  }
}
```

(Requires a Jenkins credential with git push access.)

---

## Usage

```
/tokenhack Why is the payment retry queue stalling on idempotency conflicts?
```

Claude will receive a pre-staged list of the most-relevant files (with one-line rationales), an index-freshness stamp, and conditional warnings if the index looks stale or retrieval was low-confidence. Then Claude answers your question using those files — not by grepping your tree.

For the rest of that Claude Code session, when Claude detects that a *new* question is also codebase-wide, it will suggest:

> *This looks codebase-wide — want me to re-stage via `/tokenhack`? (Say "just answer" to skip and turn off nudging for this session.)*

Say *"just answer"* (or any equivalent) once and Claude stops suggesting for the rest of the session.

---

## How retrieval actually works

The router combines several signals — none of them embeddings or LLM-based:

```
tokens = camel_snake_split(query) − stopwords + fuzzy(close_matches)

score(file, query) =
    BM25(tokens, symbols ∪ refs ∪ path_tokens)        # lexical
  + η · BM25(tokens, docstrings ∪ markdown_paragraphs) # prose channel
  + α · filename_match(tokens, basename(file))         # heuristic priors
  + β · path_affinity(tokens, dirname(file))
  + γ · recency(mtime(file))
  + ε · symbol_popularity(file)
  + ζ · definition_bonus(tokens, defs)
  + δ₁ · 1-hop import-graph propagation                # structural
  + δ₂ · 2-hop propagation (decayed, hubs suppressed)
```

CamelCase + snake_case identifier splitting means `getUserProfile` matches the natural-language query "user profile". Stopword filtering keeps BM25 weight off generic terms. `difflib`-based fuzzy expansion catches typos.

The full set of tunable constants is documented in [`.claude/skills/tokenhack/README.md`](.claude/skills/tokenhack/README.md). Open a PR if your team finds better defaults.

---

## Token math

On every `/tokenhack` invocation, the router emits roughly:

- **Header line** — index freshness + file count (~5 tokens, always present)
- **Top-K results** — paths + one-line "why" (~3-5 tokens × 5 = ~20 tokens)
- **Conditional warnings** — low-confidence / stale-index (~10 tokens each, only when triggered)
- **Test pair section** — when applicable (~5 tokens per pair)

**Worst case: ~45 tokens. Average: ~25 tokens.** Versus a typical codebase-wide question where Claude grep-explores 5-15 files at hundreds-to-thousands of tokens each, the net savings are usually 10-50× on the question itself.

---

## The three-gate nudge ruleset

After `/tokenhack` is invoked once in a session, Claude is instructed to suggest re-invoking it only when **all three** of the following gates pass:

1. **Scope signal present.** The question contains cross-cutting language (*find all*, *where*, *trace*), architectural framing, existence checks, or a domain concept with no specific symbol named.
2. **No local anchor.** The question doesn't name a file/symbol already in the conversation, doesn't pronoun-refer to the previous turn, isn't a generic language question, and isn't a trivial edit.
3. **Worth the round-trip.** The question is substantive *and* would require Claude to read ~3+ unseen files.

When in doubt, Claude is told to NOT nudge — false positives are strictly worse than missed nudges. The user can always invoke `/tokenhack` manually.

The exact ruleset is in [`.claude/skills/tokenhack/SKILL.md`](.claude/skills/tokenhack/SKILL.md). It's natural-language instructions to Claude, not regex — community-tunable.

---

## Honest limitations

- **Coreference depth-2+.** "And the same for the Stripe one" referring to a refactor two turns back will leak through Gate 2 — the rule only catches immediate-previous-turn pronouns.
- **Project jargon.** "Fix the SSO bug" looks scoped to one symbol but may span 20 files. Users must invoke `/tokenhack` manually for these.
- **Stack-trace debugging.** Looks local (file:line given) but root cause may be elsewhere. By design we don't nudge here — let Claude grep from the trace, which it does well.
- **Multi-language repos.** Lexical heuristics are language-agnostic but can't disambiguate "the controller" → Rails vs Spring vs NestJS.
- **Auto-invocation is probabilistic.** Claude follows the skill body's nudge rules most of the time, not deterministically. The escape hatch exists precisely so misfires don't accumulate.
- **First-time discovery.** Developers don't know `/tokenhack` exists until they read the README, see it in the `/` slash menu, or are nudged by Claude (after at least one prior invocation in the session, or via the `CLAUDE.md` hint). Org onboarding helps.

---

## Contributing

TokenHack is intentionally small, single-purpose, and stdlib-only on the dev side. PRs welcome — see [`.claude/skills/tokenhack/README.md`](.claude/skills/tokenhack/README.md) for adapter internals.

**Sharply scoped first-PR opportunities** (each is a few hours of work and has a clear, testable surface):

- **TypeScript adapter.** Extend `adapters/javascript.py` to handle `.ts` and `.tsx`. The grammars share ~95% of node types. *(Highest-impact contribution — the majority of modern React / Vue codebases.)*
- **Vue.js adapter.** Add `adapters/vue.py` using `tree-sitter-vue` to handle `.vue` SFCs. Reuse the JS/TS adapter internally for `<script>` blocks.
- **Ruby adapter.** `tree-sitter-ruby`. Straightforward.
- **C# adapter.** `tree-sitter-c-sharp`. Heavy enterprise use.
- **Rust adapter.** `tree-sitter-rust`. CNCF / infra workloads.
- **Go adapter.** `tree-sitter-go`. Modern infra.

**Larger, scope-discussed-first contributions:**

- Multi-hop import graph beyond 2-hop (with safeguards against blow-up).
- Git-history co-occurrence as a relevance signal.
- Acronym expansion (JWT ↔ "json web token").
- Query intent classification (explanation vs modification vs search).
- Result diversification / top-K auto-tuning.

Open an issue first for the larger items — we can scope together before code.

**What stays out** (intentionally, to preserve the load-bearing constraints):

- Embeddings, cross-encoder rerankers, LLM-as-retriever — any of these would require a model artifact, a runtime install, or a network call. All three break "pure-stdlib router, zero per-dev install, no external tooling."

If you have an idea for a richer retrieval signal that fits these constraints, open an issue — happy to discuss.

---

## Why it's called TokenHack

Two senses:

1. **Hacking** in the *make-do-with-what-you-have* sense — using only stdlib + tree-sitter + good lexical retrieval to dramatically cut the tokens an AI assistant spends exploring a codebase.
2. **Hacking down** the token cost itself.

Not about security tokens.

---

## License

MIT — see [LICENSE](LICENSE).

## Author

Rahul R · [github.com/rahulr85r](https://github.com/rahulr85r)
