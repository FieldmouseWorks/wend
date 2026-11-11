"""Build trusted Rust sources outside the data checkout; bound every parser child."""
import functools
import os
from pathlib import Path
import resource
import shutil
import subprocess
import tempfile

MAX_INPUT = 256 * 1024
TIMEOUT = 5


def parser_limits():
    resource.setrlimit(resource.RLIMIT_AS, (256 * 1024 * 1024,) * 2)
    resource.setrlimit(resource.RLIMIT_CPU, (2, 2))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))


@functools.lru_cache(maxsize=1)
def helper():
    # Keep this directory alive as long as the cached executable. No source,
    # manifest, config, toolchain or target artifact comes from the data cwd.
    directory = tempfile.TemporaryDirectory(prefix="wend-shell-lint-")
    root = Path(directory.name)
    source = Path(__file__).resolve().parent / "shell-lint"
    (root / "src").mkdir()
    for name in ("Cargo.toml", "Cargo.lock", "src/main.rs"):
        shutil.copyfile(source / name, root / name)
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("CARGO_", "RUSTC", "RUSTDOC", "RUSTFLAGS", "RUSTUP_TOOLCHAIN"))}
    # CARGO_HOME is the operator's registry cache, never a directory selected by
    # checkout configuration. Cargo's default home is outside the data checkout.
    subprocess.run([shutil.which("cargo") or "cargo", "build", "--quiet", "--locked",
                    "--manifest-path", str(root / "Cargo.toml"),
                    "--target-dir", str(root / "target")], cwd=root, env=env, check=True)
    return directory, root / "target/debug/wend-shell-lint"


def negated_pipeline(source):
    if len(source.encode()) > MAX_INPUT:
        raise ValueError("shell check exceeds 256 KiB")
    directory, binary = helper()
    result = subprocess.run([str(binary)], input=source, capture_output=True, text=True,
                            cwd=directory.name, timeout=TIMEOUT, preexec_fn=parser_limits)
    if result.returncode or result.stdout.strip() not in ("ok", "negated"):
        raise ValueError(result.stderr.strip() or "shell parser failed or exhausted its resource limit")
    return result.stdout.strip() == "negated"
