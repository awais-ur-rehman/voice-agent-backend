class AppError(Exception):
    def __init__(
        self, code: str, status: int = 400, message: str | None = None, details: dict | None = None
    ):
        self.code, self.status = code, status
        self.message = message or code.replace("_", " ")
        self.details = details

    def body(self) -> dict:
        error: dict[str, object] = {"code": self.code, "message": self.message}
        if self.details is not None:
            error["details"] = self.details
        return {"error": error}
