"""One error shape for every failure:
{"error": {"code": "...", "message": "...", "trace_id": "...", "details": [...]}}"""
from fastapi import Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.exam import DomainError

_CODES = {400: "bad_request", 401: "unauthorized", 403: "forbidden", 404: "not_found",
          405: "method_not_allowed", 409: "conflict", 422: "validation_error",
          429: "rate_limited", 503: "unavailable"}

DOMAIN_TO_HTTP = {
    "NOT_FOUND": (404, "not_found"),
    "PERMISSION_DENIED": (403, "forbidden"),
    "INVALID_ARGUMENT": (422, "invalid_request"),
    "CONFLICT": (409, "conflict"),
}


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str, details=None, headers=None):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message
        self.details, self.headers = details, headers


def from_domain(e: DomainError) -> ApiError:
    status, code = DOMAIN_TO_HTTP[e.code]
    return ApiError(status, code, e.message)


def error_body(request: Request, code: str, message: str, details=None) -> dict:
    err = {"code": code, "message": message, "trace_id": getattr(request.state, "trace_id", None)}
    if details:
        err["details"] = details
    return {"error": err}


def install(app):
    @app.exception_handler(ApiError)
    async def _api_error(request: Request, exc: ApiError):
        return JSONResponse(status_code=exc.status, headers=exc.headers,
                            content=error_body(request, exc.code, exc.message, exc.details))

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(request: Request, exc: StarletteHTTPException):
        return JSONResponse(status_code=exc.status_code, headers=getattr(exc, "headers", None),
                            content=error_body(request, _CODES.get(exc.status_code, "error"), str(exc.detail)))

    @app.exception_handler(RequestValidationError)
    async def _validation_error(request: Request, exc: RequestValidationError):
        details = [{"field": ".".join(str(p) for p in e["loc"][1:]) or str(e["loc"][0]), "message": e["msg"]}
                   for e in exc.errors()]
        first = details[0] if details else {"field": "?", "message": "invalid"}
        return JSONResponse(status_code=422, content=error_body(
            request, "validation_error", f"Invalid request: {first['field']}: {first['message']}", details))
