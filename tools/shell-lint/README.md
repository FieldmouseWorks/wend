# Shell pipeline lint

`tools/check.py` builds this helper with Cargo (Rust 1.88+) using the locked
dependencies, then sends each role or VM live check on stdin. It parses syntax;
it never executes the supplied shell. Output is `ok` or `negated`; parse and
build errors fail the check rather than silently bypassing the lint.

brush-parser owns shell grammar, including pipeline scope, compound commands,
quoting and here-documents. The walker inspects pipeline nodes and recursively
parses executable word expansions. Literal heredocs are never parsed as code;
expanding heredocs use heredoc expansion rules, where quote characters are data.

Regression cases run through both box tables in `python3 tools/test_check.py`.
