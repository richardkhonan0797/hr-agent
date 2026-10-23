"""Identity hashing and redaction for safe telemetry."""
from __future__ import annotations
import hashlib
import re

from .domain import MedicalDetailsError


# Deliberately conservative, finite recognition, not a general medical classifier.
# Keep this check at storage boundaries as well as the chat entry point.
MEDICAL_DISCLOSURE = re.compile(
    r"\b(?:diagnosis|diagnosa)\b\s*(?::|=|saya\b|aku\b|is\b)|"
    r"\b(?:diagnosed\s+with|didiagnosis|my\s+diagnosis)\b|"
    r"\b(?:rincian\s+medis|medical\s+details?)\s*[:=]|"
    r"\b(?:diabetes|hiv|kanker|cancer|tuberkulosis|tuberculosis)\b",
    re.IGNORECASE,
)


def contains_medical_details(value: str | None) -> bool:
    return bool(value and MEDICAL_DISCLOSURE.search(value))


def reject_medical_details(value: str | None) -> None:
    if contains_medical_details(value):
        raise MedicalDetailsError("Medical details require the approved HR channel")


def token_hash(token: str) -> str:
    # TODO(M2): Return a deterministic SHA-256 hex digest; never store the raw token.
    return hashlib.sha256(token.encode()).hexdigest()


def stable_safety_id(employee_id: str) -> str:
    # TODO(M2): Return a stable, non-identifying hash suitable for provider safety_identifier.
    return hashlib.sha256(employee_id.encode("utf-8")).hexdigest()


def redact(value: str) -> str:
    # TODO(M2): Redact email, Indonesian phone numbers, token-like values, and medical detail.
    
    # This is a fallback for recognizable sensitive patterns, not a general
    # detector of medical prose. The agent stores fixed error categories rather
    # than raw exception messages; trace fields exclude prompts and tool payloads.
    # Remove labelled medical detail before smaller patterns, so a diagnosis is
    # not retained merely because an email/phone inside it was already masked.
    value = re.sub(
        r"\b(?:diagnosis|diagnosa|medical(?:\s+details?)?|rincian\s+medis|penyakit)\s*[:=]\s*[^\n]*",
        "[MEDICAL REDACTED]",
        value,
        flags=re.IGNORECASE,
    )
    if contains_medical_details(value):
        return "[MEDICAL REDACTED]"
    value = re.sub(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", "[EMAIL REDACTED]", value)
    # Allow spacing after the country code, but do not join separate lines.
    value = re.sub(
        r"(?<!\w)(?:\+?62[ \t]*|0)8[\d\s().-]{7,}\d",
        "[PHONE REDACTED]",
        value,
    )
    value = re.sub(
        r"\b(?:token|sk|api[_-]?key|bearer)[\s:=_-]+[A-Za-z0-9_.-]+",
        "[TOKEN REDACTED]",
        value,
        flags=re.IGNORECASE,
    )
    # JWT-shaped tokens may appear without a label such as "Bearer".
    value = re.sub(
        r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b",
        "[TOKEN REDACTED]",
        value,
    )
    return value

