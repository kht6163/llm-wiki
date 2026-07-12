from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .app import create_web_app

__all__ = ["create_web_app"]


def __getattr__(name: str) -> Any:
    if name == "create_web_app":
        from .app import create_web_app

        return create_web_app
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
