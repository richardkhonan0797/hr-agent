"""Consistent error responses without request bodies or internal exception details."""
from http import HTTPStatus
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError, ResponseValidationError
from fastapi.responses import JSONResponse
from psycopg import InterfaceError, OperationalError
from psycopg_pool import PoolTimeout
from starlette.exceptions import HTTPException

from .contracts import ProblemDetail
from .repository import ConflictError


DETAILS = {
    401: "Bearer token tidak ada atau tidak valid.",
    403: "Anda tidak memiliki izin untuk mengakses sumber daya ini.",
    404: "Sumber daya tidak ditemukan.",
    405: "Metode HTTP tidak didukung untuk endpoint ini.",
    409: "Proposal sudah dikonfirmasi, kedaluwarsa, atau idempotency key sudah digunakan.",
    422: "Data permintaan tidak sesuai kontrak API.",
    429: "Batas percakapan demo bersama tercapai. Coba lagi setelah waktu tunggu.",
    500: "Terjadi kesalahan internal saat memproses permintaan.",
    503: "Layanan atau dependency yang diperlukan belum siap.",
}


def problem_responses(*statuses: int) -> dict[int, dict[str, Any]]:
    return {
        status: {"description": HTTPStatus(status).phrase, "content": {
            "application/problem+json": {"schema": ProblemDetail.model_json_schema()},
        }}
        for status in statuses
    }


def problem_response(status: int, detail: str | None = None) -> JSONResponse:
    problem = ProblemDetail(title=HTTPStatus(status).phrase, status=status,
                            detail=detail or DETAILS.get(status, "Permintaan tidak dapat diproses."))
    headers = {"WWW-Authenticate": "Bearer"} if status == 401 else None
    return JSONResponse(status_code=status, content=problem.model_dump(mode="json"),
                        media_type="application/problem+json", headers=headers)


def register_error_handlers(app: FastAPI) -> None:
    async def http_error(request: Request, error: HTTPException) -> JSONResponse:
        # Even an exception raised by a dependency may contain sensitive detail.
        response = problem_response(error.status_code)
        if error.status_code == 429 and error.headers:
            retry = error.headers.get("Retry-After", "")
            if retry.isdigit():
                response.headers["Retry-After"] = retry
        return response

    async def invalid_request(request: Request, error: RequestValidationError) -> JSONResponse:
        # Pydantic error details can contain the original prompt or bearer token.
        return problem_response(422)

    async def conflict(request: Request, error: ConflictError) -> JSONResponse:
        return problem_response(409)

    async def unavailable(request: Request, error: Exception) -> JSONResponse:
        return problem_response(503)

    async def internal_error(request: Request, error: Exception) -> JSONResponse:
        return problem_response(500)

    app.add_exception_handler(HTTPException, http_error)
    app.add_exception_handler(RequestValidationError, invalid_request)
    app.add_exception_handler(ResponseValidationError, internal_error)
    app.add_exception_handler(ConflictError, conflict)
    for error_type in (OperationalError, InterfaceError, PoolTimeout):
        app.add_exception_handler(error_type, unavailable)
    app.add_exception_handler(Exception, internal_error)
