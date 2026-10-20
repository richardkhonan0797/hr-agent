"""HTTP helpers for the UI: readiness GETs may retry; application calls never do."""
import time
from typing import Callable

import httpx


class DemoAPIError(Exception):
    pass


def wait_until_ready(base_url: str, *, timeout: float = 120,
                     send: Callable = httpx.request, clock: Callable = time.monotonic,
                     sleep: Callable = time.sleep) -> bool:
    deadline = clock() + timeout
    while clock() < deadline:
        try:
            response = send("GET", f"{base_url.rstrip('/')}/readyz",
                            timeout=min(5.0, max(0.01, deadline - clock())))
            body = response.json()
            if response.status_code == 200 and isinstance(body, dict) and (
                body.get("status") in {"ready", "degraded"}
            ):
                return True
        except (httpx.HTTPError, ValueError):
            pass
        remaining = deadline - clock()
        if remaining > 0:
            sleep(min(2.0, remaining))
    return False


def api_request(method: str, base_url: str, path: str, token: str,
                *, send: Callable = httpx.request, **kwargs):
    try:
        response = send(method, f"{base_url.rstrip('/')}{path}",
                        headers={"Authorization": f"Bearer {token}"}, timeout=40, **kwargs)
    except httpx.HTTPError:
        raise DemoAPIError(
            "Koneksi terputus atau waktu tunggu habis. Hasil permintaan belum diketahui. "
            "Periksa daftar permintaan sebelum mencoba lagi; konfirmasi ulang memakai kunci yang sama."
        ) from None
    if response.status_code == 429:
        retry = response.headers.get("Retry-After", "")
        wait = f" Tunggu {retry} detik." if retry.isdigit() else " Coba lagi nanti."
        raise DemoAPIError("Batas percakapan demo bersama tercapai." + wait)
    try:
        body = response.json()
    except ValueError:
        raise DemoAPIError("Server belum siap atau mengirim respons tidak valid. "
                           "Bangunkan backend lalu periksa status permintaan.") from None
    if response.is_error:
        # Display API problem details only, never an HTML proxy response.
        detail = body.get("detail") if isinstance(body, dict) else None
        if not isinstance(detail, str):
            detail = "Layanan belum tersedia. Periksa status sebelum mencoba lagi."
        raise DemoAPIError(f"API {response.status_code}: {detail}")
    if not isinstance(body, (dict, list)):
        raise DemoAPIError("Server mengirim respons tidak valid.")
    return body
