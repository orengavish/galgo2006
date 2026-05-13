"""
PostToolUse hook — runs --self-test on any trader/*.py file just written/edited.
Receives JSON on stdin: {"tool_name": "Write"|"Edit", "tool_input": {"file_path": "..."}}
Outputs JSON: {"systemMessage": "..."} so Claude sees the result inline.
"""
import json
import subprocess
import sys
from pathlib import Path


def main():
    try:
        payload = json.load(sys.stdin)
    except Exception:
        sys.exit(0)

    file_path = (
        payload.get("tool_input", {}).get("file_path")
        or payload.get("tool_response", {}).get("filePath")
    )
    if not file_path:
        sys.exit(0)

    p = Path(file_path)

    if p.suffix != ".py":
        sys.exit(0)

    # Only fire for files inside the trader/ directory
    try:
        p.relative_to(Path(file_path).parent.parent / "trader")
    except ValueError:
        # Not under trader/ — check by name component
        if "trader" not in p.parts:
            sys.exit(0)

    result = subprocess.run(
        [sys.executable, str(p), "--self-test"],
        capture_output=True, text=True, timeout=30
    )

    output = (result.stdout + result.stderr).strip()
    if result.returncode == 0:
        msg = f"self-test {p.name}: {output or 'PASSED'}"
    else:
        msg = f"self-test {p.name} FAILED:\n{output}"

    print(json.dumps({"systemMessage": msg}))


if __name__ == "__main__":
    main()
