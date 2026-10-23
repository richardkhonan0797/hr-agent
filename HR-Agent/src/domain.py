"""Deterministic leave-domain rules; this module must never call the model."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from .contracts import LeaveBalance, PrepareLeaveArgs


class DomainError(ValueError):
    pass


class LeaveOverlapError(DomainError):
    """The authenticated employee already has leave on these dates."""


class InsufficientNoticeError(DomainError):
    pass


class ZeroWorkingDaysError(DomainError):
    pass


class InsufficientBalanceError(DomainError):
    pass


class MedicalDetailsError(DomainError):
    pass


JAKARTA = timezone(timedelta(hours=7))


def jakarta_today() -> date:
    return datetime.now(JAKARTA).date()


def earliest_annual_start(today: date) -> date:
    current = today
    remaining = 3
    while remaining:
        current += timedelta(days=1)
        if current.weekday() < 5:
            remaining -= 1
    return current


def business_days(start: date, end: date) -> int:
    # TODO(M2): Validate ordering, exclude weekends, and reject zero business-day ranges.
    
    if end < start:
        raise DomainError("Tanggal selesai tidak boleh sebelum tanggal mulai")

    # Both endpoints count. Weekday numbers 0–4 represent Monday–Friday.
    # Stop before advancing past end, including when end is date.max.
    current = start
    days = 0
    while True:
        if current.weekday() < 5:
            days += 1
        if current == end:
            break
        current += timedelta(days=1)
    if days == 0:
        raise ZeroWorkingDaysError("Rentang tanggal harus mencakup hari kerja")
    return days


def validate_leave_request(
    args: PrepareLeaveArgs,
    balances: list[LeaveBalance],
    overlaps: bool,
    today: date,
) -> tuple[int, list[str]]:
    # TODO(M2): Apply past-date, range, overlap, leave-type, and balance rules.
    
    if args.start_date < today:
        raise DomainError("Tanggal mulai tidak boleh di masa lalu")
    days = business_days(args.start_date, args.end_date)
    if overlaps:
        raise LeaveOverlapError("Tanggal cuti bertumpang tindih dengan permintaan aktif")

    matching_balance = None
    for balance in balances:
        if balance.leave_type == args.leave_type:
            matching_balance = balance
            break
    if matching_balance is None:
        raise DomainError("Saldo untuk tipe cuti ini tidak tersedia")
    if matching_balance.available_days < days:
        raise InsufficientBalanceError("Saldo cuti tidak cukup")
    if args.leave_type == "annual" and args.start_date < earliest_annual_start(today):
        raise InsufficientNoticeError("Cuti tahunan memerlukan pemberitahuan tiga hari kerja")

    return days, [
        "Tanggal dan hari kerja valid",
        "Tidak bertumpang tindih dengan permintaan aktif",
        "Saldo cuti mencukupi",
    ]


def estimate_cost(
    input_tokens: int,
    output_tokens: int,
    input_rate: float | None,
    output_rate: float | None,
) -> float | None:
    if input_rate is None or output_rate is None:
        return None
    input_cost = input_tokens * input_rate / 1_000_000
    output_cost = output_tokens * output_rate / 1_000_000
    return input_cost + output_cost

