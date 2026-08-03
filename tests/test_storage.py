from __future__ import annotations

import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from astrbot_plugin_raise.core import GrowthState, save_state


class GrowthStateStorageTests(unittest.TestCase):
    def test_concurrent_atomic_saves_leave_valid_state(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "growth.json"

            def write(value: int) -> None:
                save_state(
                    path,
                    GrowthState(
                        scope_id="group",
                        bot_id="bot",
                        logical_group_id="group",
                        intimacy=value,
                    ),
                )

            with ThreadPoolExecutor(max_workers=8) as executor:
                list(executor.map(write, range(30)))

            payload = json.loads(path.read_text(encoding="utf-8"))
            self.assertIn(payload["intimacy"], range(30))
            self.assertEqual(list(path.parent.glob("*.tmp")), [])


if __name__ == "__main__":
    unittest.main()
