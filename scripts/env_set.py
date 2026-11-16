"""Set keys in .env in place, keeping everything else (comments, order, other secrets). Standard library only.

    python3 scripts/env_set.py LLM_PROVIDER=anthropic LLM_MODEL=claude-sonnet-5-5
    python3 scripts/env_set.py --comment-out VLLM_EXTRA_ARGS VLLM_MODEL_PATH
    python3 scripts/env_set.py --get LLM_PROVIDER

A key that exists is replaced where it is; a new key is added at the end. Values are never printed.
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

KEY = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=")


def set_values(text: str, values: dict[str, str], comment_out: tuple[str, ...] = ()) -> str:
    lines = text.splitlines()
    done: set[str] = set()
    out = []
    for line in lines:
        m = KEY.match(line)
        key = m.group(1) if m else None
        if key in values:
            if key not in done:
                out.append(f"{key}={values[key]}")
                done.add(key)
            continue  # a duplicate of a key being set: drop it, the first one wins anyway
        if key in comment_out:
            out.append(f"# {line}")
            continue
        out.append(line)
    new = [f"{k}={v}" for k, v in values.items() if k not in done]
    if new:
        out += ["", "# Added by scripts/env_set.py", *new]
    return "\n".join(out) + "\n"


def get_value(text: str, key: str) -> str | None:
    for line in text.splitlines():
        m = KEY.match(line)
        if m and m.group(1) == key:
            return line.split("=", 1)[1]
    return None


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("pairs", nargs="*", help="KEY=value")
    ap.add_argument("--comment-out", nargs="*", default=[], metavar="KEY")
    ap.add_argument("--get", metavar="KEY", help="exit 0 if KEY has a non-empty value (prints nothing)")
    ap.add_argument("--path", default=".env")
    args = ap.parse_args(argv)
    path = Path(args.path)
    text = path.read_text() if path.exists() else ""
    if args.get:
        sys.exit(0 if get_value(text, args.get) else 1)
    values = {}
    for pair in args.pairs:
        key, sep, value = pair.partition("=")
        if not sep or not KEY.match(f"{key}="):
            ap.error(f"not KEY=value: {key}")
        values[key] = value
    path.write_text(set_values(text, values, tuple(args.comment_out)))
    path.chmod(0o600)
    print(f"{path}: set {', '.join(values) or 'nothing'}"
          + (f"; commented out {', '.join(args.comment_out)}" if args.comment_out else ""))


if __name__ == "__main__":
    main()
