"""llm-wiki: an Obsidian-like markdown knowledge base with a web viewer and an
HTTP MCP server for LLMs."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("llm-wiki")
except PackageNotFoundError:  # source tree without installed metadata
    __version__ = "0.0.0"
