class HubError(Exception):
    def __init__(self, code: str, message: str, http_status: int = 503, **details):
        super().__init__(message)
        self.code = code
        self.http_status = http_status
        self.details = details

    def as_dict(self):
        return {"error": {"code": self.code, "message": str(self), **self.details}}


class DeviceError(HubError):
    """Проверенный ответ оборудования с ненулевым status."""
