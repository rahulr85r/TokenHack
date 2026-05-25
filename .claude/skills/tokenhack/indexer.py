#!/usr/bin/env python3
"""TokenHack indexer.

Walks the repository, parses code files via tree-sitter, and writes:

    .claude/skills/tokenhack/index/symbols.json   # the index proper
    .claude/skills/tokenhack/index/meta.json      # file hashes (incremental)

This script runs in CI on every push to main (see the workflow at
.github/workflows/tokenhack-index.yml). It can also be run locally to
seed the index on first install:

    python3 .claude/skills/tokenhack/indexer.py

Dependencies: tree-sitter + per-language grammars (see requirements.txt).
The indexer degrades gracefully — if a grammar is missing, files in that
language are skipped with a warning, but the rest of the index still builds.
"""

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

THIS_DIR = Path(__file__).resolve().parent
if str(THIS_DIR) not in sys.path:
    sys.path.insert(0, str(THIS_DIR))

from adapters import availability_report, get_adapter, supported_extensions  # noqa: E402

# Directories pruned during the walk. Generated artifacts, dependency
# trees, and tool caches — never the source of useful retrieval signal.
# `.claude` is pruned by default so the skill's own router/adapters don't
# pollute consumer-repo indexes; pass --include-self when indexing the
# TokenHack source repo itself.
STOP_DIRS = {
    "node_modules", "vendor", "dist", "build", "target", "out",
    ".git", ".venv", "venv", "env", ".tox",
    ".pytest_cache", ".mypy_cache", ".ruff_cache",
    "__pycache__", ".next", ".nuxt",
    ".gradle", ".idea", ".vscode",
    "DerivedData", "Pods", "Carthage",
    "coverage", "htmlcov", "site-packages",
    ".claude",
}

INDEX_REL = ".claude/skills/tokenhack/index"
SYMBOLS_FILE = "symbols.json"
META_FILE = "meta.json"
INDEX_VERSION = 1

# Caps to keep the index lean.
MAX_FILE_SIZE = 1024 * 1024  # 1 MB — skip vendored bundles, minified blobs
MAX_PROSE_PARAGRAPHS_PER_FILE = 30
MAX_PROSE_PARAGRAPH_CHARS = 400


def find_repo_root(start: Path) -> Path:
    """Walk up from `start` looking for the repo root.

    Marker priority: `.git` directory, then the TokenHack skill directory
    (so the indexer still works in non-git contexts like tarballs).
    """
    p = start.resolve()
    for _ in range(40):
        if (p / ".git").exists() or (p / ".claude" / "skills" / "tokenhack").is_dir():
            return p
        if p.parent == p:
            break
        p = p.parent
    return start.resolve()


def current_commit(repo_root: Path) -> str:
    try:
        out = subprocess.run(
            ["git", "-C", str(repo_root), "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=5,
        )
        if out.returncode == 0:
            return out.stdout.strip()
    except Exception:
        pass
    return ""


def file_hash(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def walk_code(root: Path, exts, stop_dirs=None):
    """Yield (rel_path, full_path) for code files under `root`."""
    exts_set = {e.lower() for e in exts}
    stop = stop_dirs if stop_dirs is not None else STOP_DIRS
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in stop]
        for fn in filenames:
            ext = os.path.splitext(fn)[1].lower()
            if ext not in exts_set:
                continue
            full = Path(dirpath) / fn
            try:
                rel = str(full.relative_to(root))
            except ValueError:
                continue
            yield rel, full


def walk_prose(root: Path, stop_dirs=None):
    """Yield (rel_path, full_path) for every `.md` file outside stop-dirs.

    Prose is high-signal for "how does X work?" queries — README, SKILL.md,
    CONTRIBUTING, CHANGELOG, ADRs, runbooks, design docs all qualify.
    Generated artifacts and dependency trees are pruned via STOP_DIRS.
    """
    stop = stop_dirs if stop_dirs is not None else STOP_DIRS
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in stop]
        for fn in filenames:
            if not fn.lower().endswith(".md"):
                continue
            full = Path(dirpath) / fn
            yield str(full.relative_to(root)), full


def extract_prose_paragraphs(text: str):
    """Return up to N short paragraphs from markdown text.

    Strips a leading YAML frontmatter block (--- ... ---) before splitting,
    so the frontmatter doesn't pollute the prose corpus on SKILL.md or
    similar files with metadata headers.
    """
    if text.startswith("---\n") or text.startswith("---\r\n"):
        end = text.find("\n---", 4)
        if end > 0:
            text = text[end + 4:].lstrip("\r\n")
    out = []
    for raw in text.split("\n\n"):
        para = " ".join(raw.split()).strip()
        if not para or para.startswith("```"):
            continue
        # Skip standalone H1 headings; keep H2+ since those carry semantic weight
        if para.startswith("# ") and len(para) < 80:
            continue
        if len(para) > MAX_PROSE_PARAGRAPH_CHARS:
            para = para[:MAX_PROSE_PARAGRAPH_CHARS].rstrip() + "…"
        out.append(para)
        if len(out) >= MAX_PROSE_PARAGRAPHS_PER_FILE:
            break
    return out


def load_meta(meta_path: Path) -> dict:
    if not meta_path.exists():
        return {"version": INDEX_VERSION, "file_hashes": {}}
    try:
        with open(meta_path, encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return {"version": INDEX_VERSION, "file_hashes": {}}
        data.setdefault("version", INDEX_VERSION)
        data.setdefault("file_hashes", {})
        return data
    except Exception:
        return {"version": INDEX_VERSION, "file_hashes": {}}


def load_symbols(sym_path: Path):
    if not sym_path.exists():
        return None
    try:
        with open(sym_path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def build_reverse_imports(files_index: dict):
    """Heuristically wire `imported_by` based on import string segments.

    For each import path like 'foo.bar.Baz' or './utils/auth', we split into
    segments and match each segment to an indexed file's basename (stem).
    Approximate but cheap and language-agnostic.
    """
    by_basename = {}
    for rel in files_index:
        stem = os.path.splitext(os.path.basename(rel))[0]
        by_basename.setdefault(stem, []).append(rel)

    # Reset / initialize
    for entry in files_index.values():
        entry["imported_by"] = []

    for src_rel, entry in files_index.items():
        seen = set()
        for imp in entry.get("imports", []):
            normalized = imp.replace("/", ".").replace("\\", ".")
            normalized = normalized.replace("'", "").replace('"', "")
            for seg in normalized.split("."):
                seg = seg.strip()
                if not seg or seg in ("", "."):
                    continue
                for tgt_rel in by_basename.get(seg, ()):
                    if tgt_rel == src_rel:
                        continue
                    if (tgt_rel, src_rel) in seen:
                        continue
                    seen.add((tgt_rel, src_rel))
                    files_index[tgt_rel]["imported_by"].append(src_rel)


def index_repo(root: Path, force: bool = False, verbose: bool = False,
               include_self: bool = False) -> dict:
    started = time.time()
    avail = availability_report()
    if verbose:
        for lang, ok in sorted(avail.items()):
            mark = "OK " if ok else "-- "
            print(f"  [{mark}] adapter: {lang}", file=sys.stderr)

    # `.claude` is pruned by default to keep the skill's own source out of
    # consumer-repo indexes. `--include-self` removes the prune so the
    # TokenHack source repo can index itself in CI.
    stop_dirs = (STOP_DIRS - {".claude"}) if include_self else STOP_DIRS

    exts = supported_extensions()
    out_dir = root / INDEX_REL
    out_dir.mkdir(parents=True, exist_ok=True)
    meta_path = out_dir / META_FILE
    sym_path = out_dir / SYMBOLS_FILE

    prev_hashes = {} if force else load_meta(meta_path).get("file_hashes", {})
    prev_index = None if force else load_symbols(sym_path)
    prev_files = (prev_index or {}).get("files", {})

    files_index: dict = {}
    new_hashes: dict = {}
    n_parsed = 0
    n_reused = 0
    n_skipped = 0
    skip_reasons: dict = {}

    def _skip(reason: str):
        nonlocal n_skipped
        n_skipped += 1
        skip_reasons[reason] = skip_reasons.get(reason, 0) + 1

    for rel, full in walk_code(root, exts, stop_dirs=stop_dirs):
        try:
            size = full.stat().st_size
        except OSError:
            _skip("stat-error")
            continue
        if size > MAX_FILE_SIZE:
            _skip("too-large")
            continue
        try:
            h = file_hash(full)
        except OSError:
            _skip("read-error")
            continue
        new_hashes[rel] = h

        # Incremental reuse of unchanged files
        if not force and prev_hashes.get(rel) == h and rel in prev_files:
            entry = dict(prev_files[rel])
            entry.pop("imported_by", None)  # will be recomputed
            files_index[rel] = entry
            n_reused += 1
            continue

        adapter = get_adapter(rel)
        if adapter is None:
            _skip("no-adapter")
            continue

        try:
            with open(full, "rb") as f:
                source = f.read()
        except OSError:
            _skip("read-error")
            continue

        try:
            result = adapter.extract(source, rel)
        except Exception as exc:
            if verbose:
                print(f"  [warn] adapter failed on {rel}: {exc}", file=sys.stderr)
            _skip("adapter-exception")
            continue

        try:
            mtime = int(full.stat().st_mtime)
        except OSError:
            mtime = 0

        files_index[rel] = {
            "lang": adapter.LANGUAGE_NAME,
            "mtime": mtime,
            "size_chars": size,
            "symbols": [s.to_dict() for s in result.symbols],
            "imports": result.imports,
            "references": result.references,
            "docstring_summary": result.docstring_summary,
            "prose_paragraphs": result.prose_paragraphs,
        }
        n_parsed += 1

    build_reverse_imports(files_index)

    # Prose corpus — README + docs/*.md
    prose_corpus: dict = {}
    for rel, full in walk_prose(root, stop_dirs=stop_dirs):
        try:
            text = full.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        paras = extract_prose_paragraphs(text)
        if paras:
            prose_corpus[rel] = paras

    elapsed = time.time() - started

    output = {
        "version": INDEX_VERSION,
        "built_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "built_at_commit": current_commit(root),
        "n_files_indexed": len(files_index),
        "n_files_parsed": n_parsed,
        "n_files_reused": n_reused,
        "n_files_skipped": n_skipped,
        "skip_reasons": skip_reasons,
        "build_seconds": round(elapsed, 2),
        "adapters_available": avail,
        "files": files_index,
        "prose": prose_corpus,
    }

    sym_path.write_text(
        json.dumps(output, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    meta_path.write_text(
        json.dumps(
            {"version": INDEX_VERSION, "file_hashes": new_hashes},
            indent=2, sort_keys=True,
        ) + "\n",
        encoding="utf-8",
    )

    print(
        f"[tokenhack] indexed {len(files_index)} files "
        f"({n_parsed} parsed, {n_reused} reused, {n_skipped} skipped) "
        f"in {elapsed:.2f}s",
        file=sys.stderr,
    )
    return output


def main():
    p = argparse.ArgumentParser(description="TokenHack indexer")
    p.add_argument("--root", type=Path, default=None,
                   help="Repo root (default: detect from cwd via .git)")
    p.add_argument("--force", action="store_true",
                   help="Force full rebuild (ignore meta.json hashes)")
    p.add_argument("--verbose", action="store_true",
                   help="Print extra diagnostics to stderr")
    p.add_argument("--include-self", action="store_true",
                   help="Include `.claude/` in the index. Used by the TokenHack "
                        "source repo itself; consumer repos should leave this off "
                        "so the skill's own files don't pollute their index.")
    args = p.parse_args()

    start = args.root or Path.cwd()
    root = find_repo_root(start)
    index_repo(root, force=args.force, verbose=args.verbose,
               include_self=args.include_self)


if __name__ == "__main__":
    main()
