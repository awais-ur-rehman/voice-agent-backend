class AppError(Exception):
    def __init__(
        self,
        code: str,
        status: int = 400,
        message: str | None = None,
        details: dict | None = None,
        *,
        retry_after: int | None = None,
    ):
        self.code, self.status = code, status
        self.message = message or code.replace("_", " ")
        self.details = details
        if retry_after is not None and (
            type(retry_after) is not int or not 1 <= retry_after <= 3600
        ):
            raise ValueError("Invalid retry interval")
        self.retry_after = retry_after

    def headers(self) -> dict[str, str]:
        return {"Retry-After": str(self.retry_after)} if self.retry_after is not None else {}

    def body(self) -> dict:
        error: dict[str, object] = {"code": self.code, "message": self.message}
        if self.details is not None:
            error["details"] = self.details
        return {"error": error}
