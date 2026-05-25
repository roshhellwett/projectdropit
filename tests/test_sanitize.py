"""Unit tests for projectdropit.transfer.sanitize_filename.

Covers the cross-OS path-traversal and reserved-name protections.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from projectdropit.transfer import sanitize_filename


CASES = [
    # (input, expected)
    ("photo.jpg", "photo.jpg"),
    ("/etc/passwd", "passwd"),
    ("..\\..\\windows\\system32\\evil.exe", "evil.exe"),
    ("C:\\Users\\victim\\Desktop\\bad.txt", "bad.txt"),
    ("../../etc/shadow", "shadow"),
    ("", "file.bin"),
    (".", "file.bin"),
    ("..", "file.bin"),
    (".bashrc", "bashrc"),                     # leading dot stripped
    ("a/b/c/.hidden.cfg", "hidden.cfg"),
    ("file\x00.png", "file.png"),              # null byte gone
    ("note\twith\tcontrol\x01chars.txt", "notewithcontrolchars.txt"),
    ("CON", "_CON"),                           # Windows reserved
    ("aux.txt", "_aux.txt"),                   # Windows reserved with ext
    ("ok.txt", "ok.txt"),
    ('weird:<name>"?.txt', "weird__name___.txt"),
]


def main() -> int:
    failures = 0
    for raw, expected in CASES:
        got = sanitize_filename(raw)
        if got != expected:
            print(f"FAIL  sanitize_filename({raw!r}) = {got!r}  expected {expected!r}")
            failures += 1
    long_input = "x" * 500 + ".bin"
    got = sanitize_filename(long_input)
    if len(got) > 200:
        print(f"FAIL  length cap not enforced: {len(got)}")
        failures += 1
    if failures:
        print(f"{failures} failure(s)")
        return 1
    print(f"OK  {len(CASES) + 1} sanitize cases passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
