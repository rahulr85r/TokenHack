# Installing TokenHack

The short version is in the [README](README.md#install--three-steps-five-minutes): copy one directory, add one line to `CLAUDE.md`, add one CI workflow, seed the index once. This page covers everything past that — protected branches, other CI systems, verification, troubleshooting, updating, and using it outside Claude Code.

---

## GitHub Actions

### Variant A — direct push (simplest)

Works when your `main` branch allows pushes from Actions. This is the workflow shown in the README:

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

One-time setting: **Settings → Actions → General → Workflow permissions → "Read and write permissions."**

The `paths-ignore` line stops the bot's own index commit from triggering another rebuild, so there is no loop.

### Variant B — pull request (for protected branches)

If `main` requires pull requests, replace the last step so the bot pushes to a branch and opens a PR you merge (or auto-merge):

```yaml
      - name: Open or update index PR
        env:
          GH_TOKEN: ${{ github.token }}
        run: |
          if [[ -z "$(git status --porcelain .claude/skills/tokenhack/index/)" ]]; then
            echo "No index changes."; exit 0
          fi
          git config user.name "github-actions[bot]"
          git config user.email "github-actions[bot]@users.noreply.github.com"
          git checkout -B tokenhack/index-update
          git add .claude/skills/tokenhack/index/
          git commit -m "[tokenhack] update index"
          git push --force origin tokenhack/index-update
          if [[ -z "$(gh pr list --head tokenhack/index-update --state open --json number --jq '.[0].number')" ]]; then
            gh pr create --base main --head tokenhack/index-update \
              --title "[tokenhack] update index" \
              --body "Automated index rebuild. Mechanical change - safe to merge."
          fi
```

Add `pull-requests: write` under `permissions:` for this variant. Index PRs are mechanical; enabling auto-merge on them keeps the index fresh without anyone thinking about it.

> Note: this repo's own workflow additionally passes `--include-self` to the indexer so TokenHack can index its own source under `.claude/`. **Leave that flag off in your repo** — you don't want the skill's internals polluting your search results.

---

## GitLab CI

```yaml
tokenhack-index:
  stage: build
  image: python:3.11
  rules:
    - if: '$CI_COMMIT_BRANCH == "main" && $CI_COMMIT_TITLE !~ /^\[tokenhack\]/'
  script:
    - pip install -r .claude/skills/tokenhack/requirements.txt
    - python3 .claude/skills/tokenhack/indexer.py
    - |
      if [ -n "$(git status --porcelain .claude/skills/tokenhack/index/)" ]; then
        git config user.email "tokenhack-ci@invalid"
        git config user.name "tokenhack-ci"
        git add .claude/skills/tokenhack/index/
        git commit -m "[tokenhack] update index"
        git push "https://gitlab-ci-token:${CI_PUSH_TOKEN}@${CI_SERVER_HOST}/${CI_PROJECT_PATH}.git" "HEAD:${CI_COMMIT_REF_NAME}"
      fi
```

Requires a `CI_PUSH_TOKEN` CI/CD variable (a Project Access Token with `write_repository`). The commit-title rule is the loop guard: the bot's own `[tokenhack]` commits don't retrigger the job.

## CircleCI

```yaml
jobs:
  tokenhack-index:
    docker:
      - image: cimg/python:3.11
    steps:
      - checkout
      - run: pip install -r .claude/skills/tokenhack/requirements.txt
      - run: python3 .claude/skills/tokenhack/indexer.py
      - run: |
          if [ -n "$(git status --porcelain .claude/skills/tokenhack/index/)" ] \
             && [[ "$(git log -1 --pretty=%s)" != "[tokenhack]"* ]]; then
            git config user.email "tokenhack-ci@invalid"
            git config user.name "tokenhack-ci"
            git add .claude/skills/tokenhack/index/
            git commit -m "[tokenhack] update index"
            git push origin "$CIRCLE_BRANCH"
          fi
```

Requires a deploy key or token with push access.

## Jenkins

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
      when {
        branch 'main'
        not { changelog '^\\[tokenhack\\].*' }
      }
      steps {
        sh '''
          if [ -n "$(git status --porcelain .claude/skills/tokenhack/index/)" ]; then
            git config user.email "tokenhack-ci@invalid"
            git config user.name "tokenhack-ci"
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

Requires a Jenkins credential with git push access.

---

## Verify it works

From your repo root, no Claude required:

```bash
python3 .claude/skills/tokenhack/router.py "where is the retry logic"
```

You should see a header line (`[tokenhack: index built …, N files indexed]`) followed by up to five ranked paths with `↳ read L<start>-<end>` hints. If retrieval looks weak on a plain-English question, add the vocabulary the codebase probably uses — this is what the agent does automatically:

```bash
python3 .claude/skills/tokenhack/router.py "where is the retry logic" --terms "retry backoff attempt maxRetries scheduler"
```

---

## Troubleshooting

**`[tokenhack: no index found at …]`** — the index hasn't been seeded. Run the seed block from the README, or wait for the first CI run and pull.

**`[coverage warning: N of M source files had no language adapter]`** — a large share of your repo is in a language TokenHack doesn't parse yet (Go, Rust, C#, Ruby…). Retrieval will only cover the indexed part; prefer plain grep for the rest, or contribute an adapter (they're ~60 lines — see [ROADMAP](ROADMAP.md)).

**`[index is N files behind HEAD — rebuild advised]`** — the index is stale relative to your checkout. Normal on a feature branch (the index tracks `main`); it means very recent files may be missing from results.

**Queries feel slow** — the router re-derives its term index from `symbols.json` on every call: roughly 0.6s at 1,200 files, 1.5s at 5,500, 2.5s at 9,500. Comfortable to a few thousand files; a precomputed postings list is on the roadmap for very large repos.

**Index size** — expect ~4–6 KB per indexed file (7 MB at 1,200 files, 40 MB at 9,500). GitHub warns at 50 MB per blob and hard-blocks at 100 MB, so the practical ceiling is roughly 15–20k files today.

**Adapter build errors during `pip install`** — the tree-sitter grammar wheels cover common platforms; on unusual ones you may need a C compiler (`build-essential` / Xcode CLT). This only ever affects CI or the one-time seed, never developer machines.

---

## Updating TokenHack

The skill is vendored — update by re-copying:

```bash
git clone --depth 1 https://github.com/rahulr85r/TokenHack /tmp/tokenhack
rm -rf .claude/skills/tokenhack/index.bak && mv .claude/skills/tokenhack/index /tmp/index.keep
rm -rf .claude/skills/tokenhack
cp -R /tmp/tokenhack/.claude/skills/tokenhack .claude/skills/
mv /tmp/index.keep .claude/skills/tokenhack/index
rm -rf /tmp/tokenhack
```

(Keeping your `index/` avoids an immediate full rebuild; the next CI run refreshes it incrementally either way.)

## Uninstalling

```bash
rm -rf .claude/skills/tokenhack .github/workflows/tokenhack-index.yml
```

…and remove the one line from `CLAUDE.md`. Nothing else was ever installed anywhere.

---

## Using it outside Claude Code

`router.py` is a plain stdlib CLI, so any agent framework that can execute a shell command can use it: call it with the user's question plus a `--terms` list of likely code vocabulary, and feed the staged block back to the model as context. The output format is stable, line-oriented, and small (~500–700 tokens). The Claude Code integration in [`SKILL.md`](.claude/skills/tokenhack/SKILL.md) is exactly this, written as instructions.
