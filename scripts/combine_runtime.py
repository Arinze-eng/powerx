#!/usr/bin/env python3
"""Combine runtime_a.py and runtime_b.py into runtime.py and clean up."""
from pathlib import Path

dir_path = Path(__file__).parent.parent / "nanobot" / "channels" / "telegram"
part_a = (dir_path / "runtime_a.py").read_text()
part_b = (dir_path / "runtime_b.py").read_text()
(dir_path / "runtime.py").write_text(part_a + part_b)
(dir_path / "runtime_a.py").unlink()
(dir_path / "runtime_b.py").unlink()
print(f"runtime.py created ({len(part_a) + len(part_b)} chars)")
