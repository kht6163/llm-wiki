"""Command-line entrypoint. Filled in incrementally; `main` is the console script."""
from __future__ import annotations


def main(argv: list[str] | None = None) -> int:  # pragma: no cover - thin wrapper
    from llm_wiki._cli_impl import run

    return run(argv)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
