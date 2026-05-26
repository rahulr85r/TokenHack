---
name: Language adapter request
about: Request (or propose) support for a new language
title: "[adapter] "
labels: adapter, enhancement
---

## Language

<!-- e.g. TypeScript, Vue, Ruby, C#, Rust, Go, C/C++ -->

## tree-sitter grammar to use

<!--
Most languages have a `tree-sitter-<language>` PyPI package. Link to it.
If there are multiple competing grammars (this happens for C++, for
example), say which one you're proposing and why.
-->

## Are you offering to write it, or just requesting it?

<!--
Both are valid. If you're writing it, see
.claude/skills/tokenhack/README.md for the adapter shape — Python adapter
is the cleanest reference implementation.
-->

## Representative repo to test against

<!--
Adapter PRs need a real-world test. A public repo of >500 files in the
target language, ideally one you know well so you can sanity-check the
extracted symbols.
-->

## Anything weird about the language?

<!--
Special cases worth flagging:
- TypeScript has decorators, generics, type-only imports
- Vue SFCs combine HTML/CSS/JS in one file
- C++ has the preprocessor, header/impl split, templates
- etc.

Not blockers — just context for whoever writes the adapter.
-->
