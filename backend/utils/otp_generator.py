from __future__ import annotations

import hashlib
import hmac
import os
import secrets


def generate_otp() -> str:
    """Generate a cryptographically secure 6-digit OTP."""
    return f"{secrets.randbelow(1_000_000):06d}"


def _get_secret() -> str:
    return os.getenv("OTP_SECRET", os.getenv("ADMIN_API_KEY", "dev-otp-secret"))


def hash_otp(otp: str) -> str:
    """Hash an OTP using SHA-256 with an application secret."""
    secret = _get_secret().encode("utf-8")
    return hashlib.sha256(secret + otp.encode("utf-8")).hexdigest()


def verify_otp(plain_otp: str, otp_hash: str) -> bool:
    """Constant-time OTP hash verification."""
    return hmac.compare_digest(hash_otp(plain_otp), otp_hash)
