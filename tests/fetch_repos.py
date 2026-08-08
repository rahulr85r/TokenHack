#!/usr/bin/env python3
"""Clone and index the repositories the gold sets are written against.

    python3 tests/fetch_repos.py                 # clone + index everything in repos.json
    python3 tests/fetch_repos.py --dest ~/eval   # somewhere other than alongside this checkout
    python3 tests/fetch_repos.py --index-only    # repos already cloned, just (re)build indexes

Each repo is pinned to the commit its gold set was authored against, so the
evaluation numbers are reproducible rather than a claim you have to take on
trust. Clones are shallow but must fetch the pinned commit, so this pulls a bit
more than `--depth 1` of HEAD.

Needs the indexer's tree-sitter dependencies:

    pip install -r .claude/skills/tokenhack/requirements.txt
"""

import argparse
import json
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
TOKENHACK = HERE.parent
INDEXER = TOKENHACK / ".claude" / "skills" / "tokenhack" / "indexer.py"
MANIFEST = HERE / "repos.json"


def sh(cmd, cwd=None, check=True):
    print("  $", " ".join(str(c) for c in cmd))
    p = subprocess.run(cmd, cwd=cwd and str(cwd), text=True)
    if check and p.returncode != 0:
        raise SystemExit(f"command failed: {' '.join(str(c) for c in cmd)}")
    return p.returncode


def clone(repo, dest: Path):
    target = dest / repo["name"]
    if target.exists():
        print(f"{repo['name']}: already present, skipping clone")
        return target
    print(f"{repo['name']}: cloning {repo['url']}")
    target.mkdir(parents=True, exist_ok=True)
    sh(["git", "init", "-q"], cwd=target)
    sh(["git", "remote", "add", "origin", repo["url"]], cwd=target)
    # Fetch just enough history to reach the pinned commit.
    if sh(["git", "fetch", "--depth", "1", "origin", repo["commit"]],
          cwd=target, check=False) != 0:
        print(f"  pinned commit not fetchable shallowly; falling back to full fetch")
        sh(["git", "fetch", "origin"], cwd=target)
    sh(["git", "checkout", "-q", "FETCH_HEAD"], cwd=target, check=False) or \
        sh(["git", "checkout", "-q", repo["commit"]], cwd=target, check=False)
    return target


def index(target: Path):
    print(f"{target.name}: indexing")
    sh([sys.executable, str(INDEXER), "--root", str(target), "--force"], check=False)


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--dest", type=Path, default=TOKENHACK.parent,
                    help="Where to place the clones (default: alongside this checkout)")
    ap.add_argument("--index-only", action="store_true", help="Skip cloning")
    ap.add_argument("--repo", action="append", help="Only this repo (repeatable)")
    args = ap.parse_args()

    manifest = json.loads(MANIFEST.read_text())
    for repo in manifest["repos"]:
        if args.repo and repo["name"] not in args.repo:
            continue
        target = args.dest / repo["name"]
        if not args.index_only:
            target = clone(repo, args.dest)
        if not target.exists():
            print(f"{repo['name']}: not present, skipping")
            continue
        index(target)

    print("\nDone. Now run:  python3 tests/eval.py"
          + ("" if args.dest == TOKENHACK.parent else f" --repos-dir {args.dest}"))


if __name__ == "__main__":
    main()
