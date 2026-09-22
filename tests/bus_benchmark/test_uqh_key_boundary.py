"""Private keys must remain outside source and installed package boundaries."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from bus_benchmark.errors import ValidationError
from bus_benchmark.uqh import _validate_external_private_key


class PrivateKeyBoundaryTests(unittest.TestCase):
    def test_root_src_archive_and_wheel_exclude_the_whole_owner_directory(self):
        layouts = (
            ("checkout/bus_benchmark/uqh.py", "checkout", "directory"),
            ("checkout/benchmark/src/bus_benchmark/uqh.py", "checkout", "file"),
            ("archive/benchmark/src/bus_benchmark/uqh.py", "archive", None),
            ("site-packages/bus_benchmark/uqh.py", "site-packages", None),
        )
        for module, owner, marker_kind in layouts:
            with self.subTest(module=module), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                boundary = root / owner
                boundary.mkdir()
                if marker_kind == "directory":
                    (boundary / ".git").mkdir()
                    (boundary / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
                elif marker_kind == "file":
                    (boundary / ".git").write_text("gitdir: /unused\n")
                # No actual secret is needed: rejection precedes key parsing.
                inside = boundary / "private.pem"
                outside = root / "outside.pem"
                outside.write_text("synthetic, not a private key")
                outside.chmod(0o600)
                with patch("bus_benchmark.uqh.__file__", str(root / module)), patch(
                    "bus_benchmark.uqh._openssl", return_value=b"synthetic-public-key"
                ) as openssl:
                    with self.assertRaisesRegex(ValidationError, "outside"):
                        _validate_external_private_key(inside, root / "public.pem")
                    openssl.assert_not_called()
                    _validate_external_private_key(outside, root / "public.pem")
                    self.assertEqual(openssl.call_count, 2)
