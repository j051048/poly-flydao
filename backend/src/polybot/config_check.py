from __future__ import annotations

import sys
from collections.abc import Callable
from typing import TextIO

from pydantic import ValidationError

from polybot.config import Settings

EX_CONFIG = 78


def _field_name(location: tuple[object, ...]) -> str:
    return ".".join(str(item) for item in location) or "settings"


def _safe_message(error: dict[str, object]) -> str:
    # Pydantic's structured error message describes the rule that failed. Do
    # not serialize the error itself: that would include its `input` member and
    # could disclose an API key or signer value in deployment logs.
    return " ".join(str(error.get("msg", "invalid configuration")).split())


def check_configuration(
    *,
    settings_factory: Callable[[], Settings] = Settings,
    error_stream: TextIO | None = None,
    success_stream: TextIO | None = None,
) -> int:
    errors = error_stream or sys.stderr
    success = success_stream or sys.stdout
    try:
        settings = settings_factory()
    except ValidationError as exc:
        for error in exc.errors(include_url=False, include_context=False, include_input=False):
            location = tuple(error.get("loc", ()))
            print(
                "POLYBOT_CONFIG_ERROR "
                f"field={_field_name(location)} message={_safe_message(error)}",
                file=errors,
            )
        return EX_CONFIG

    print(
        "POLYBOT_CONFIG_OK "
        f"component={settings.component} mode={settings.mode.value} "
        f"personal_mode={str(settings.personal_mode).lower()}",
        file=success,
    )
    return 0


def main() -> None:
    raise SystemExit(check_configuration())


if __name__ == "__main__":
    main()
