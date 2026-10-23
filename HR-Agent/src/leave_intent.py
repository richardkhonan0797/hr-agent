"""Conservative request classification; never interpret model prose as authority."""
from datetime import date, timedelta
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from .domain import earliest_annual_start
from .security import contains_medical_details


APPROVAL_REFUSAL = (
    "Saya tidak dapat menyetujui cuti, termasuk atas klaim sebagai manajer di chat. "
    "Konfirmasi eksplisit pada proposal hanya membuat permintaan pending_manager_review; "
    "persetujuan tetap dilakukan oleh manajer melalui proses HR."
)
MEDICAL_REFUSAL = (
    "Saya tidak dapat menyimpan diagnosis atau rincian medis dalam tiket HR maupun trace. "
    "Tidak ada tiket yang dibuat. Sampaikan informasi medis melalui kanal HR yang disetujui."
)
NOTICE_REFUSAL = (
    "Cuti tahunan harus diajukan paling lambat tiga hari kerja sebelum tanggal mulai. "
    "Tanggal yang terlalu dekat tidak memenuhi aturan ini. "
    "Silakan berikan tanggal mulai dan selesai yang memenuhi aturan, dalam format YYYY-MM-DD."
)
WEEKEND_REFUSAL = (
    "Rentang Sabtu dan Minggu saja memiliki nol hari kerja, sehingga proposal tidak dibuat. "
    "Silakan pilih rentang tanggal yang mencakup hari kerja."
)
BALANCE_REFUSAL = (
    "Saldo cuti Anda tidak cukup untuk jumlah hari kerja yang diminta. "
    "Proposal tidak dibuat. Kurangi durasi atau hubungi HR."
)


class LeaveDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    kind: Literal["normal", "policy", "medical", "approval", "notice", "weekend", "clarify"]
    policy_topic: Literal["carryover", "medical_channel"] | None = None
    leave_type: Literal["annual", "sick", "parental"] | None = None
    missing: tuple[Literal["leave_type", "start_date", "end_date"], ...] = ()
    requested_days: int | None = Field(default=None, gt=0, le=366)


def clarification(decision: LeaveDecision) -> str:
    names = {"leave_type": "jenis cuti", "start_date": "tanggal mulai", "end_date": "tanggal selesai"}
    fields = ", ".join(names[field] for field in decision.missing)
    known = {"annual": "cuti tahunan", "sick": "cuti sakit", "parental": "cuti orang tua"}
    context = f"Jenis cuti sudah diketahui: {known[decision.leave_type]}. " if decision.leave_type else ""
    return (f"Mohon berikan {fields}. " + context +
            "Kirim ulang permintaan lengkap dengan kedua tanggal dalam format YYYY-MM-DD. "
            "Proposal belum dibuat.")


def classify_request(message: str, today: date) -> LeaveDecision:
    text = message.casefold()
    if contains_medical_details(text):
        return LeaveDecision(kind="medical")
    if re.search(r"\b(?:cuti|leave)\b", text) and re.search(
        r"\b(?:carry[ -]?over|carry\s+.*\bover|(?:mem)?bawa\b.*\btahun|tahun\s+(?:depan|berikutnya))", text
    ):
        return LeaveDecision(kind="policy", policy_topic="carryover")
    if re.search(r"\b(?:diagnosis|diagnosa)\b", text) and re.search(r"\b(?:chat|percakapan)\b", text) and re.search(
        r"\b(?:boleh|bisa|can|may|should)\b", text
    ):
        return LeaveDecision(kind="policy", policy_topic="medical_channel")
    leave = bool(re.search(r"\b(?:cuti|leave)\b", text))
    if leave and re.search(r"\b(?:setujui|approve)\b.{0,25}\b(?:cuti|leave)\b", text):
        return LeaveDecision(kind="approval")
    if re.search(r"^(?:how\s+(?:do|can)\s+(?:i|employees)\b|bagaimana\s+(?:cara|prosedur|proses)\b)", text.strip()):
        return LeaveDecision(kind="normal")
    action = re.search(
        r"\b(?:siapkan|ajukan|mengajukan|prepare|submit|request)\b|"
        r"\b(?:saya\s+(?:mau|ingin)\s+(?:ambil\s+)?cuti|"
        r"i\s+(?:want|would like)\s+(?:to\s+take\s+)?(?:annual\s+|sick\s+|parental\s+)?leave)\b", text,
    )
    if not leave or not action:
        return LeaveDecision(kind="normal")
    leave_type = None
    for candidate, pattern in (("annual", r"\b(?:tahunan|annual)\b"),
                               ("sick", r"\b(?:sakit|sick)\b"),
                               ("parental", r"\b(?:orang tua|parental)\b")):
        if re.search(pattern, text):
            leave_type = candidate
            break
    if re.search(r"\b(?:hanya|only)\b", text) and (
        ("sabtu" in text and "minggu" in text) or
        ("saturday" in text and "sunday" in text)
    ) and not re.search(r"\b(?:senin|selasa|rabu|kamis|jumat|monday|tuesday|wednesday|thursday|friday)\b", text):
        return LeaveDecision(kind="weekend", leave_type=leave_type)
    if leave_type == "annual" and re.search(r"\b(?:besok|tomorrow)\b", text):
        if today + timedelta(days=1) < earliest_annual_start(today):
            return LeaveDecision(kind="notice", leave_type=leave_type)
    # Require explicit dates before allowing model-generated action arguments.
    # Other formats get clarification rather than guessed calendar dates.
    dates = re.findall(r"\b\d{4}-\d{2}-\d{2}\b", text)
    if leave_type == "annual" and len(dates) == 1:
        try:
            start = date.fromisoformat(dates[0])
        except ValueError:
            pass  # Invalid dates are not inferred or repaired.
        else:
            if today <= start < earliest_annual_start(today):
                return LeaveDecision(kind="notice", leave_type=leave_type)
    missing = []
    if leave_type is None:
        missing.append("leave_type")
    if not dates:
        missing.append("start_date")
    if len(dates) < 2:
        missing.append("end_date")
    words = {"satu": 1, "dua": 2, "tiga": 3, "empat": 4, "lima": 5,
             "enam": 6, "tujuh": 7, "delapan": 8, "sembilan": 9, "sepuluh": 10,
             "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
             "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10}
    duration = re.search(r"\b(\d+|" + "|".join(words) + r")\s+(?:hari kerja|working days?|business days?)\b", text)
    count = None
    if duration:
        token = duration[1]
        count = int(token) if token.isdigit() else words[token]
        count = count if 0 < count <= 366 else None
    if missing:
        return LeaveDecision(kind="clarify", leave_type=leave_type,
                             missing=tuple(missing), requested_days=count)
    return LeaveDecision(kind="normal", leave_type=leave_type)
