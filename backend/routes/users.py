from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional
from uuid import uuid4

from fastapi import APIRouter, HTTPException
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, EmailStr, Field

from db.supabase_client import get_supabase_service_client
from services.email_service import EmailServiceError, send_email
from utils.otp_generator import generate_otp, hash_otp, verify_otp

router = APIRouter(prefix="/v1/users", tags=["users"])

OTP_TTL_MINUTES = 10


@dataclass
class OTPRecord:
    email: str
    otp_hash: str
    user_id: str
    expires_at: datetime


class AdminCreateUserRequest(BaseModel):
    email: EmailStr
    full_name: str = Field(min_length=1, max_length=120)


class VerifyOtpSetPasswordRequest(BaseModel):
    token: str
    otp: str = Field(min_length=6, max_length=6)
    new_password: str = Field(min_length=8, max_length=128)


_otp_store: dict[str, OTPRecord] = {}
_otp_lock = asyncio.Lock()


def _otp_email_html(name: str, otp: str) -> str:
    return f"""
    <html>
      <body style=\"font-family: Arial, sans-serif; color: #1f2937;\">
        <h2>Welcome to Amenity Booking</h2>
        <p>Hello {name},</p>
        <p>Your one-time password (OTP) is:</p>
        <p style=\"font-size: 28px; font-weight: 700; letter-spacing: 3px;\">{otp}</p>
        <p>This OTP expires in {OTP_TTL_MINUTES} minutes.</p>
        <p>Use this OTP to log in and set your password.</p>
      </body>
    </html>
    """


@router.post("/admin/create-with-otp")
async def admin_create_user_with_otp(payload: AdminCreateUserRequest):
    """
    Admin creates user, backend generates OTP, and sends OTP via SMTP email.
    """
    client = get_supabase_service_client()

    temporary_password = uuid4().hex + "A1!"

    try:
        response = await run_in_threadpool(
            client.auth.admin.create_user,
            {
                "email": payload.email,
                "password": temporary_password,
                "email_confirm": True,
                "user_metadata": {
                    "full_name": payload.full_name,
                    "role": "resident",
                },
            },
        )
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Unable to create user: {exc}") from exc

    auth_user = getattr(response, "user", None)
    if auth_user is None and isinstance(response, dict):
        auth_user = response.get("user")

    user_id = str(getattr(auth_user, "id", None) or (auth_user or {}).get("id", "")).strip()
    if not user_id:
        raise HTTPException(status_code=500, detail="User created but user id is missing in response.")

    otp = generate_otp()
    otp_token = uuid4().hex
    expires_at = datetime.now(timezone.utc) + timedelta(minutes=OTP_TTL_MINUTES)

    async with _otp_lock:
        _otp_store[otp_token] = OTPRecord(
            email=str(payload.email),
            otp_hash=hash_otp(otp),
            user_id=user_id,
            expires_at=expires_at,
        )

    try:
        await send_email(
            to_email=str(payload.email),
            subject="Your OTP for Amenity Booking",
            html_body=_otp_email_html(payload.full_name, otp),
        )
    except EmailServiceError as exc:
        async with _otp_lock:
            _otp_store.pop(otp_token, None)
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    return {
        "success": True,
        "message": "User created and OTP sent via email.",
        "otp_token": otp_token,
        "expires_at": expires_at.isoformat(),
        "user_id": user_id,
    }


@router.post("/verify-otp-set-password")
async def verify_otp_set_password(payload: VerifyOtpSetPasswordRequest):
    """
    User submits OTP and sets a new password.
    """
    token = payload.token.strip()
    otp = payload.otp.strip()

    async with _otp_lock:
        record: Optional[OTPRecord] = _otp_store.get(token)

    if record is None:
        raise HTTPException(status_code=404, detail="OTP token not found.")

    if datetime.now(timezone.utc) > record.expires_at:
        async with _otp_lock:
            _otp_store.pop(token, None)
        raise HTTPException(status_code=410, detail="OTP expired.")

    if not verify_otp(otp, record.otp_hash):
        raise HTTPException(status_code=401, detail="Invalid OTP.")

    client = get_supabase_service_client()

    try:
        await run_in_threadpool(
            client.auth.admin.update_user_by_id,
            record.user_id,
            {"password": payload.new_password},
        )
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Unable to set new password: {exc}") from exc

    async with _otp_lock:
        _otp_store.pop(token, None)

    return {"success": True, "message": "Password updated successfully."}
