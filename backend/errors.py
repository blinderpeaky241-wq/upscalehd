class AppError(Exception):
    """An error with a stable machine code and a user-readable message."""

    def __init__(self, code, message, status=400):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
