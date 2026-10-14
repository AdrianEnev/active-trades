import os
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from active_trades.env import load_shared_env


class TestSharedEnv(TestCase):
    def test_load_shared_env_finds_parent_env(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            child = root / "active_trades"
            child.mkdir()
            (root / ".env").write_text("LIGHTER_ACCOUNT_INDEX=123\n", encoding="utf-8")

            with patch.dict(os.environ, {}, clear=True):
                with patch("pathlib.Path.cwd", return_value=child):
                    loaded = load_shared_env(anchor_path=child)

                self.assertEqual(loaded, 1)
                self.assertEqual(os.environ["LIGHTER_ACCOUNT_INDEX"], "123")
