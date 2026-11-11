# Shell pipeline lint

`tools/check.py` uses `tools/shell_lint.py` to copy only the trusted manifest,
lockfile and Rust source into a fresh temporary directory outside the data
checkout. Cargo runs there with a separate target directory, an explicit
workspace root, and compiler/Cargo environment overrides removed. Checkout
Cargo configuration, toolchain files, workspaces and prebuilt targets are never
used. Rust 1.88+ and Cargo are required. The operator's normal Cargo home remains
the dependency source cache; it must not contain untrusted configuration.

The helper parses syntax; it never executes the supplied shell. Output is `ok`
or `negated`; parse, build and resource failures fail the check. Inputs are
limited to 256 KiB. Each parser child has a five-second wall timeout, two CPU
seconds and 256 MiB of address space; core dumps are disabled. Bash/dash syntax
checks use the same limits.

brush-parser owns shell grammar, including substitution boundaries, pipeline
scope, compound commands, quoting and here-documents. Its source is pinned to
upstream revision `751dae803c4a493fc2fb1b74cefd94ebcd4b7de0`, which includes the
empty-heredoc tokenizer fix discussed in upstream issue #1692 and the tokenizer
based substitution-boundary parser. The walker carries the surrounding quoting
context through nested parameter operands. Literal heredocs remain data;
expanding heredocs use heredoc expansion rules, where quote characters are data.

`python3 tools/test_check.py` runs both-table syntax regressions and the build
isolation/resource tests. The latter can also run alone with
`python3 tools/test_shell_lint.py`.
