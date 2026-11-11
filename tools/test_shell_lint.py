"""Isolation and resource regressions; run with python3 tools/test_shell_lint.py."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import shell_lint


class ShellLintTests(unittest.TestCase):
    def test_build_ignores_checkout_configuration_and_artifacts(self):
        with tempfile.TemporaryDirectory(prefix="wend-hostile-checkout-") as directory:
            root = Path(directory)
            (root / ".cargo").mkdir()
            marker = root / "executed"
            wrapper = root / "wrapper"
            wrapper.write_text(f"#!/bin/sh\ntouch '{marker}'\nexit 99\n")
            wrapper.chmod(0o755)
            config = f'[build]\nrustc-wrapper = "{wrapper}"\ntarget-dir = "{root / "target"}"\n'
            (root / "Cargo.toml").write_text('[workspace]\nmembers = ["missing"]\n')
            (root / "rust-toolchain.toml").write_text('[toolchain]\nchannel = "nonexistent"\n')
            binary = root / "target/debug/wend-shell-lint"
            binary.parent.mkdir(parents=True)
            binary.write_text(wrapper.read_text())
            binary.chmod(0o755)
            before = Path.cwd()
            try:
                os.chdir(root)
                for name in ("config", "config.toml"):
                    with self.subTest(name=name):
                        config_path = root / ".cargo" / name
                        config_path.write_text(config + '\n[patch.crates-io]\nserde_json = { path = "missing" }\n')
                        if shell_lint.helper.cache_info().currsize:
                            shell_lint.helper()[0].cleanup()
                        shell_lint.helper.cache_clear()
                        with patch.dict(os.environ, {"RUSTC_WRAPPER": str(wrapper),
                                                    "RUSTC": str(wrapper),
                                                    "RUSTUP_TOOLCHAIN": "nonexistent",
                                                    "CARGO_TARGET_DIR": str(root / "target")}):
                            self.assertTrue(shell_lint.negated_pipeline("! missing | cat"))
                        self.assertFalse(marker.exists())
                        config_path.unlink()
            finally:
                os.chdir(before)

    def test_upstream_hanging_heredocs_are_bounded(self):
        # Each parse is a separate child with wall, CPU and address-space limits.
        for source in ("<<'' ", '<<""\t', "<<'';#", "<<\\\n'' ", "$(<<  ",
                       "$(<<\t t ", "<<\\\n$(\t\t", "<<<<<''\t", '<<"" `` ',
                       "cat <<''\n\n", "rc=$(cat <<OUTER\n$(cat <<INNER\nx\nINNER\n)\nOUTER\n)"):
            with self.subTest(source=source):
                try:
                    shell_lint.negated_pipeline(source)
                except (ValueError, subprocess.TimeoutExpired):
                    pass  # Invalid input or a killed parser must fail closed.

    def test_unsupported_parser_syntax_fails_closed(self):
        # brush currently cannot parse an unparenthesized case pattern in a
        # substitution. It must refuse it rather than silently miss its body.
        with self.assertRaises(ValueError):
            shell_lint.negated_pipeline('rc="$(case x in x) ! missing | cat;; esac)"')

    def test_resource_failure_is_an_error(self):
        import check
        with tempfile.TemporaryDirectory() as directory:
            binary = Path(directory) / "parser"
            for code in ("while True: pass", "x = bytearray(1024**3)",
                         "import time; time.sleep(30)"):
                with self.subTest(code=code):
                    binary.write_text(f"#!{sys.executable}\n{code}\n")
                    binary.chmod(0o755)
                    with patch.object(shell_lint, "helper", return_value=(type("Directory", (), {"name": directory})(), binary)):
                        for table in ("roles", "live"):
                            doc = {"roles": {"probe": "true"}} if table == "roles" else {"vm": {"live": {"probe": "true"}}}
                            errors = check.box_shell_errors(doc)
                            self.assertTrue(any("shell pipeline lint failed" in e for e in errors), errors)
        with self.assertRaises(ValueError):
            shell_lint.negated_pipeline("x" * (shell_lint.MAX_INPUT + 1))


if __name__ == "__main__":
    unittest.main()
