"""wend's root_umask() by behaviour: 022 inside, the caller's mask after, also when the walk raises (SPEC "How a hop
runs"). It loads and runs the `wend` it sits next to, so the gate's schema job (which judges a PR with the base
branch's tools and never runs the PR's code) doesn't run it: `wend check` and the client job do.
python3 tools/test_umask.py"""
import importlib.machinery
import importlib.util
import os
import sys

sys.dont_write_bytecode = True
loader = importlib.machinery.SourceFileLoader(
    "wend_entry", os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "wend"))
wend = importlib.util.module_from_spec(importlib.util.spec_from_loader(loader.name, loader))
loader.exec_module(wend)
saved, seen = os.umask(0o077), []
try:
    with wend.root_umask():
        seen.append(os.umask(0o022))
    seen.append(os.umask(0o077))
    try:
        with wend.root_umask():
            seen.append(os.umask(0o022))
            raise KeyboardInterrupt
    except KeyboardInterrupt:
        pass
    seen.append(os.umask(0o077))
finally:
    os.umask(saved)
ok = seen == [0o022, 0o077, 0o022, 0o077]
print(("passed  " if ok else f"WRONG   {[oct(m) for m in seen]} ") +
      "root_umask() is 022 inside, the caller's mask after (also on an exception)")
sys.exit(0 if ok else 1)
