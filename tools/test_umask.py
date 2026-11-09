"""wend's root_umask() by behaviour: 022 inside, the caller's mask after, also when the walk raises (SPEC "How a hop
runs"). It loads and runs the `wend` it sits next to, so the gate's schema job (which judges a PR with the base
branch's tools and never runs the PR's code) doesn't run it: `wend check` and the client job do.
Also checks the Python sandbox's private work directory and its rejection of unsafe existing paths.
python3 tools/test_umask.py"""
import importlib.machinery
import importlib.util
import os
import sys
import errno
import hashlib
import stat
import tempfile
import unittest
from unittest.mock import patch

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
# These checks run without root: only the ownership field is substituted for the happy path.
import sandbox


class WorkDirTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="wend-work-test-")
        self.addCleanup(self.temp.cleanup)
        self.work = os.path.join(self.temp.name, "work")
        self.enterContext(patch.object(sandbox, "WORK", self.work))
        real_lstat = os.lstat

        def root_stat(path):
            fields = list(real_lstat(path))
            fields[4] = 0
            return os.stat_result(fields)

        self.enterContext(patch.object(sandbox.os, "lstat", side_effect=root_stat))

    def test_private_creation_under_replay_umask(self):
        with wend.root_umask():
            sandbox.work_dir()
        self.assertEqual(stat.S_IMODE(os.stat(self.work).st_mode), 0o700)

    def test_creation_under_restrictive_umask(self):
        saved = os.umask(0o777)
        try:
            sandbox.work_dir()
        finally:
            os.umask(saved)
        self.assertEqual(stat.S_IMODE(os.stat(self.work).st_mode), 0o700)

    def test_safe_existing_directory_is_unchanged(self):
        os.mkdir(self.work, 0o755)
        sandbox.work_dir()
        self.assertEqual(stat.S_IMODE(os.stat(self.work).st_mode), 0o755)

    def test_writable_directories_are_rejected_without_chmod(self):
        os.mkdir(self.work)
        for mode in (0o770, 0o707, 0o777):
            os.chmod(self.work, mode)
            with self.assertRaises(RuntimeError):
                sandbox.work_dir()
            self.assertEqual(stat.S_IMODE(os.stat(self.work).st_mode), mode)

    def test_symlink_is_rejected(self):
        os.symlink(self.temp.name, self.work)
        with self.assertRaises(RuntimeError):
            sandbox.work_dir()

    def test_file_is_rejected(self):
        with open(self.work, "w"):
            pass
        with self.assertRaises(RuntimeError):
            sandbox.work_dir()

    def test_other_owner_is_rejected(self):
        os.mkdir(self.work)
        fields = list(os.stat(self.work))
        fields[4] = 12345
        with patch.object(sandbox.os, "lstat", return_value=os.stat_result(fields)):
            with self.assertRaises(RuntimeError):
                sandbox.work_dir()

    def test_acls_and_unknown_errors_are_rejected(self):
        os.mkdir(self.work)
        for acl in ("system.posix_acl_access", "system.posix_acl_default"):
            def getxattr(path, name, **kwargs):
                if name == acl:
                    return b"acl"
                raise OSError(errno.ENODATA, "no ACL")
            with patch.object(sandbox.os, "getxattr", side_effect=getxattr):
                with self.assertRaises(RuntimeError):
                    sandbox.work_dir()
        with patch.object(sandbox.os, "getxattr", side_effect=OSError(errno.EACCES, "denied")):
            with self.assertRaises(RuntimeError):
                sandbox.work_dir()

    def test_filesystem_without_acls_is_allowed(self):
        with patch.object(sandbox.os, "getxattr", side_effect=OSError(errno.EOPNOTSUPP, "unsupported")):
            sandbox.work_dir()

    def test_cached_box_and_mirror_validate_before_use(self):
        key = hashlib.sha256(b"box" + b"adapter" + sandbox.BUILD_VERSION).hexdigest()[:12]
        root = os.path.join(self.work, "boxes", f"debian_test-{key}")
        os.makedirs(root)
        with open(os.path.join(root, ".wend-ready"), "w"):
            pass
        os.chmod(self.work, 0o777)
        for action in (lambda: sandbox.build_box("debian/test", {}, {}, b"box", b"adapter"),
                       lambda: sandbox.ensure_mirror({})):
            with self.assertRaises(RuntimeError):
                action()


result = unittest.TextTestRunner().run(unittest.defaultTestLoader.loadTestsFromTestCase(WorkDirTests))
sys.exit(0 if ok and result.wasSuccessful() else 1)
