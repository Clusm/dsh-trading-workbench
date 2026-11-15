"""统一错误信封（docs/01 §8.1）。"""

from __future__ import annotations

from typing import Any


class ServiceError(Exception):
    """带 HTTP 状态与错误码的业务异常；由 app 层统一转成错误信封。"""

    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        *,
        retryable: bool = False,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.retryable = retryable
        self.details = details or {}

    def as_error(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "retryable": self.retryable,
            "details": self.details,
        }
