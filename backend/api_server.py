from __future__ import annotations

import hmac
import json
import os
import re
import secrets
from datetime import date, datetime, timezone
from typing import Any, Literal, Optional
from zoneinfo import ZoneInfo

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import or_, select
from sarvamai import SarvamAI
from sarvamai.errors.forbidden_error import ForbiddenError

from booking.availability import overlap_count, split_into_slots
from booking.engine import execute_booking, update_amenity_rules, upsert_amenity
from booking.models import Amenity, AmenityRule, Booking, Building
from booking.schema import (
    AmenityAvailabilityResponse,
    AmenityListItem,
    AmenityListResponse,
    AdminUserCreateRequest,
    AdminUserCreateResponse,
    AdminUserDeleteResponse,
    AdminUserItem,
    AdminUserListResponse,
    AdminUserUpdateRequest,
    AdminUserUpdateResponse,
    AmenityRuleUpdateRequest,
    AmenitySlotAvailability,
    AmenityUpsertRequest,
    BookingIntent,
    BookingStatus,
    UserBookingIntentRequest,
    UserBookingItem,
    UserBookingListResponse,
    UserCancelBookingRequest,
)
from config import get_settings
from db.auth import AuthenticatedSupabaseUser, get_current_supabase_user
from db.session import SessionLocal, validate_db_compatibility
from db.supabase_auth_middleware import SupabaseAuthMiddleware
from db.supabase_client import get_supabase_service_client
from routes.users import router as users_router
from services.email_service import EmailServiceError, send_email

settings = get_settings()

APP_NAME = settings.app_name
APP_VERSION = settings.app_version
MAX_AUDIO_SIZE_BYTES = 25 * 1024 * 1024
DEFAULT_STT_MODEL = "saaras:v3"
DEFAULT_STT_MODE = "transcribe"
DEFAULT_LANGUAGE_CODE = "en-IN"
API_KEY_HEADER = "X-API-Key"
ADMIN_API_KEY_HEADER = "X-Admin-API-Key"


def _model_to_python(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump()
    if isinstance(value, dict):
        return {key: _model_to_python(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_model_to_python(item) for item in value]
    return value


def _extract_response_payload(response: Any) -> Any:
    payload = _model_to_python(response)
    if isinstance(payload, dict) and "data" in payload:
        return payload["data"]
    return payload


def _extract_auth_user_records(response: Any) -> list[dict[str, Any]]:
    payload = _extract_response_payload(response)

    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        if isinstance(payload.get("users"), list):
            return [item for item in payload["users"] if isinstance(item, dict)]
        if isinstance(payload.get("user"), dict):
            return [payload["user"]]
    return []


def _extract_table_rows(response: Any) -> list[dict[str, Any]]:
    payload = _extract_response_payload(response)
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        if isinstance(payload.get("data"), list):
            return [item for item in payload["data"] if isinstance(item, dict)]
        if isinstance(payload.get("data"), dict):
            return [payload["data"]]
    return []


def _generate_temporary_password() -> str:
    return secrets.token_urlsafe(12)


def _load_role_directory() -> dict[str, dict[str, Any]]:
    client = get_supabase_service_client()
    role_directory: dict[str, dict[str, Any]] = {}

    for table_name in ("users", "profiles"):
        try:
            rows = _extract_table_rows(client.table(table_name).select("*").execute())
        except Exception:
            continue

        for row in rows:
            user_id = str(row.get("id", "")).strip()
            if not user_id:
                continue
            role_directory[user_id] = {**role_directory.get(user_id, {}), **row}

    return role_directory


def _persist_role_mapping(user_id: str, role: str) -> None:
    client = get_supabase_service_client()
    payload = {"id": user_id, "role": role}
    errors: list[str] = []

    for table_name in ("users", "profiles"):
        try:
            client.table(table_name).upsert(payload, on_conflict="id").execute()
            return
        except Exception as exc:
            errors.append(f"{table_name}: {exc}")

    try:
        client.auth.admin.delete_user(user_id)
    except Exception:
        pass

    raise HTTPException(
        status_code=500,
        detail=(
            "Auth account was created, but role persistence failed for both users and profiles tables. "
            + " | ".join(errors)
        ),
    )


def _build_admin_user_item(auth_user: dict[str, Any], role_directory: dict[str, dict[str, Any]]) -> AdminUserItem | None:
    auth_user_id = str(auth_user.get("id", "")).strip()
    if not auth_user_id:
        return None

    directory_row = role_directory.get(auth_user_id, {})
    user_metadata = auth_user.get("user_metadata") or {}
    app_metadata = auth_user.get("app_metadata") or {}

    created_at = auth_user.get("created_at")
    email_confirmed_at = auth_user.get("email_confirmed_at")
    last_sign_in_at = auth_user.get("last_sign_in_at")
    status = str(directory_row.get("status") or ("ACTIVE" if email_confirmed_at or last_sign_in_at else "INVITED")).strip().upper()
    if status not in {"ACTIVE", "INVITED", "SUSPENDED", "INACTIVE"}:
        status = "INVITED"

    return AdminUserItem(
        auth_user_id=auth_user_id,
        resident_id=str(user_metadata.get("resident_id") or auth_user_id),
        name=str(user_metadata.get("name") or user_metadata.get("full_name") or directory_row.get("full_name") or "").strip(),
        email=str(auth_user.get("email") or directory_row.get("email") or "").strip(),
        phone=str(user_metadata.get("phone") or directory_row.get("phone") or "").strip(),
        apartment=str(user_metadata.get("apartment") or user_metadata.get("unit") or directory_row.get("apartment") or "").strip(),
        role=str(directory_row.get("role") or user_metadata.get("role") or app_metadata.get("role") or "resident").strip(),
        status=status,
        created_at=created_at,
        email_confirmed_at=email_confirmed_at,
        last_sign_in_at=last_sign_in_at,
    )


def get_client() -> SarvamAI:
    if not settings.sarvam_api_key:
        raise RuntimeError("Missing required environment variable: SARVAM_API_KEY")
    return SarvamAI(api_subscription_key=settings.sarvam_api_key)


def verify_api_key(x_api_key: Optional[str] = Header(default=None, alias=API_KEY_HEADER)):
    if not x_api_key or not hmac.compare_digest(x_api_key, settings.voice_booking_api_key):
        raise HTTPException(status_code=401, detail="Invalid or missing API key.")


def verify_admin_api_key(x_admin_api_key: Optional[str] = Header(default=None, alias=ADMIN_API_KEY_HEADER)):
    if not x_admin_api_key or not hmac.compare_digest(x_admin_api_key, settings.admin_api_key):
        raise HTTPException(status_code=401, detail="Invalid or missing admin API key.")


def extract_json_object(raw_text: str) -> dict:
    try:
        return json.loads(raw_text)
    except json.JSONDecodeError:
        pass

    match = re.search(r"\{[\s\S]*\}", raw_text)
    if not match:
        raise ValueError("Model response did not contain a JSON object.")
    return json.loads(match.group(0))


def _resolve_voice_identity(building_id: Optional[str], user_id: Optional[str]) -> tuple[str, str]:
    resolved_building_id = (building_id or os.getenv("VOICE_BOOKING_DEFAULT_BUILDING_ID", "")).strip()
    resolved_user_id = (user_id or os.getenv("VOICE_BOOKING_DEFAULT_USER_ID", "")).strip()
    if not resolved_building_id or not resolved_user_id:
        raise ValueError(
            "building_id and user_id are required. Provide them in the request or set "
            "VOICE_BOOKING_DEFAULT_BUILDING_ID and VOICE_BOOKING_DEFAULT_USER_ID."
        )
    return resolved_building_id, resolved_user_id


def map_intent(text: str, client: SarvamAI, *, building_id: str, user_id: str) -> dict:
    today = datetime.today().strftime("%Y-%m-%d")
    prompt = f"""
You are an intent extraction engine.

Convert the following sentence into STRICT JSON.
No explanations. No markdown.
Resolve relative dates like today/tomorrow using today's date: {today}.
If a field is missing, return an empty string.
Time must be 24-hour HH:MM format.

Sentence:
{text}

JSON format:
{{
  "intent": "BOOK_AMENITY | CANCEL_BOOKING",
  "building_id": "{building_id}",
  "user_id": "{user_id}",
  "amenity": "string",
  "date": "YYYY-MM-DD",
  "time": "HH:MM"
}}
"""

    response = client.chat.completions(
        temperature=0,
        messages=[{"role": "user", "content": prompt}],
    )
    content = (
        response.choices[0].message.content
        if hasattr(response, "choices")
        else response["choices"][0]["message"]["content"]
    )
    payload = extract_json_object(content)

    raw_intent = str(payload.get("intent", "")).strip().upper()
    normalized_intent = {
        "BOOK": "BOOK_AMENITY",
        "BOOK_AMENITY": "BOOK_AMENITY",
        "CANCEL": "CANCEL_BOOKING",
        "CANCEL_BOOKING": "CANCEL_BOOKING",
    }.get(raw_intent, "")

    unified_payload = {
        "intent": normalized_intent,
        "building_id": payload.get("building_id") or building_id,
        "user_id": payload.get("user_id") or user_id,
        "amenity": payload.get("amenity", ""),
        "date": payload.get("date", ""),
        "time": payload.get("time", ""),
    }
    return UserBookingIntentRequest.model_validate(unified_payload).model_dump(mode="json")


def transcribe_audio_bytes(
    content: bytes,
    filename: str,
    content_type: Optional[str],
    client: SarvamAI,
    language_code: str,
) -> str:
    if not content:
        raise HTTPException(status_code=400, detail="Uploaded file is empty.")
    if len(content) > MAX_AUDIO_SIZE_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"Audio file too large. Max allowed is {MAX_AUDIO_SIZE_BYTES} bytes.",
        )

    file_name = filename or "audio.wav"
    result = client.speech_to_text.transcribe(
        file=(file_name, content, content_type or "application/octet-stream"),
        model=DEFAULT_STT_MODEL,
        mode=DEFAULT_STT_MODE,
        language_code=language_code,
    )
    transcript = result.transcript if hasattr(result, "transcript") else result["transcript"]
    return transcript.strip() if transcript else ""


class HealthResponse(BaseModel):
    status: Literal["ok", "error"]
    service: str
    version: str


class IntentRequest(BaseModel):
    text: str = Field(..., min_length=1, description="Natural language sentence")
    building_id: Optional[str] = None
    user_id: Optional[str] = None


class IntentResponse(BaseModel):
    intent: Literal["BOOK_AMENITY", "CANCEL_BOOKING"]
    building_id: str
    user_id: str
    amenity: str
    date: str
    time: str


class TranscribeResponse(BaseModel):
    transcript: str


class AudioToIntentResponse(BaseModel):
    transcript: str
    intent_json: IntentResponse


app = FastAPI(title=APP_NAME, version=APP_VERSION)
cors_origins = [
    "http://localhost:3000",
    "http://localhost:3001",
    "http://localhost:3002",
]
if settings.cors_allow_origins:
    cors_origins.extend([origin.strip() for origin in settings.cors_allow_origins.split(",") if origin.strip()])
app.add_middleware(
    CORSMiddleware,
    allow_origins=sorted(set(cors_origins)),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.add_middleware(SupabaseAuthMiddleware)
app.include_router(users_router)


def _to_utc(dt: datetime, tz_name: str) -> datetime:
    if dt.tzinfo is not None:
        return dt.astimezone(timezone.utc)
    try:
        zone = ZoneInfo(tz_name)
    except Exception:
        zone = ZoneInfo("UTC")
    return dt.replace(tzinfo=zone).astimezone(timezone.utc)


@app.on_event("startup")
def startup_checks():
    _ = settings.voice_booking_api_key
    _ = settings.admin_api_key
    _ = settings.database_url
    _ = settings.supabase_url
    _ = settings.supabase_anon_key
    _ = settings.supabase_service_role_key
    validate_db_compatibility()


@app.exception_handler(ForbiddenError)
def handle_forbidden_error(_, exc: ForbiddenError):
    return JSONResponse(
        status_code=401,
        content={"detail": "Sarvam authentication failed. Verify SARVAM_API_KEY."},
    )


@app.get("/health/live", response_model=HealthResponse)
def health_live():
    return HealthResponse(status="ok", service=APP_NAME, version=APP_VERSION)


@app.get("/health/ready", response_model=HealthResponse)
def health_ready():
    try:
        validate_db_compatibility()
    except Exception as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return HealthResponse(status="ok", service=APP_NAME, version=APP_VERSION)


@app.post("/v1/transcribe", response_model=TranscribeResponse, dependencies=[Depends(verify_api_key)])
async def transcribe(file: UploadFile = File(...), language_code: str = Form(DEFAULT_LANGUAGE_CODE)):
    try:
        client = get_client()
        content = await file.read()
        transcript = transcribe_audio_bytes(
            content=content,
            filename=file.filename or "audio.wav",
            content_type=file.content_type,
            client=client,
            language_code=language_code,
        )
    except ForbiddenError as exc:
        raise HTTPException(status_code=401, detail="Sarvam authentication failed. Verify SARVAM_API_KEY.") from exc
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Transcription failed: {exc}") from exc
    return TranscribeResponse(transcript=transcript)


@app.post("/v1/intent", response_model=IntentResponse, dependencies=[Depends(verify_api_key)])
def extract_intent(request: IntentRequest):
    try:
        client = get_client()
        building_id, user_id = _resolve_voice_identity(request.building_id, request.user_id)
        intent_data = map_intent(
            request.text,
            client,
            building_id=building_id,
            user_id=user_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except ForbiddenError as exc:
        raise HTTPException(status_code=401, detail="Sarvam authentication failed. Verify SARVAM_API_KEY.") from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Intent extraction failed: {exc}") from exc
    return IntentResponse(**intent_data)


@app.post("/v1/audio-to-intent", response_model=AudioToIntentResponse, dependencies=[Depends(verify_api_key)])
async def audio_to_intent(
    file: UploadFile = File(...),
    language_code: str = Form(DEFAULT_LANGUAGE_CODE),
    building_id: Optional[str] = Form(default=None),
    user_id: Optional[str] = Form(default=None),
):
    try:
        client = get_client()
        resolved_building_id, resolved_user_id = _resolve_voice_identity(building_id, user_id)
        content = await file.read()
        transcript = transcribe_audio_bytes(
            content=content,
            filename=file.filename or "audio.wav",
            content_type=file.content_type,
            client=client,
            language_code=language_code,
        )
        intent_data = map_intent(
            transcript,
            client,
            building_id=resolved_building_id,
            user_id=resolved_user_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except ForbiddenError as exc:
        raise HTTPException(status_code=401, detail="Sarvam authentication failed. Verify SARVAM_API_KEY.") from exc
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Audio-to-intent failed: {exc}") from exc

    return AudioToIntentResponse(transcript=transcript, intent_json=IntentResponse(**intent_data))


@app.post("/v1/bookings/execute", dependencies=[Depends(verify_api_key)])
def execute_booking_route(_request: BookingIntent):
    raise HTTPException(status_code=410, detail="Deprecated. Use POST /v1/bookings")


@app.get("/v1/amenities", response_model=AmenityListResponse, dependencies=[Depends(verify_api_key)])
def list_active_amenities(
    building_id: str,
    _current_user: AuthenticatedSupabaseUser = Depends(get_current_supabase_user),
):
    with SessionLocal() as db:
        stmt = (
            select(Amenity)
            .where(
                Amenity.building_id == building_id,
                Amenity.is_active.is_(True),
            )
            .order_by(Amenity.name.asc())
        )
        amenities = list(db.scalars(stmt))
        return AmenityListResponse(
            amenities=[
                AmenityListItem(
                    amenity_id=amenity.id,
                    building_id=amenity.building_id,
                    name=amenity.name,
                    capacity=amenity.capacity,
                    is_active=amenity.is_active,
                )
                for amenity in amenities
            ]
        )


@app.get(
    "/v1/amenities/{amenity_id}/availability",
    response_model=AmenityAvailabilityResponse,
    dependencies=[Depends(verify_api_key)],
)
def get_amenity_availability(
    amenity_id: str,
    building_id: str,
    day: date,
    _current_user: AuthenticatedSupabaseUser = Depends(get_current_supabase_user),
):
    with SessionLocal() as db:
        amenity = db.get(Amenity, amenity_id)
        if not amenity or not amenity.is_active:
            raise HTTPException(status_code=404, detail="Amenity not found or inactive.")
        if amenity.building_id != building_id:
            raise HTTPException(status_code=400, detail="Amenity and building_id do not match.")

        rule = db.scalar(select(AmenityRule).where(AmenityRule.amenity_id == amenity_id))
        if not rule:
            raise HTTPException(status_code=404, detail="Amenity rules are not configured.")

        building = db.get(Building, amenity.building_id)
        tz_name = building.timezone if building and building.timezone else "UTC"
        local_start = datetime.combine(day, rule.operating_start_time)
        local_end = datetime.combine(day, rule.operating_end_time)
        slots = split_into_slots(
            start_time=local_start,
            end_time=local_end,
            slot_length_minutes=rule.slot_length_minutes,
        )

        available_slots: list[AmenitySlotAvailability] = []
        for slot_start_local, slot_end_local in slots:
            slot_start_utc = _to_utc(slot_start_local, tz_name)
            slot_end_utc = _to_utc(slot_end_local, tz_name)
            active_count = overlap_count(
                db,
                amenity_id=amenity_id,
                start_time=slot_start_utc,
                end_time=slot_end_utc,
            )
            remaining = max(rule.max_capacity - active_count, 0)
            if remaining > 0:
                available_slots.append(
                    AmenitySlotAvailability(
                        slot_start=slot_start_local.time(),
                        slot_end=slot_end_local.time(),
                        remaining_capacity=remaining,
                        max_capacity=rule.max_capacity,
                    )
                )

        return AmenityAvailabilityResponse(
            amenity_id=amenity.id,
            building_id=amenity.building_id,
            date=day,
            slot_length_minutes=rule.slot_length_minutes,
            slots=available_slots,
        )


@app.post("/v1/bookings", dependencies=[Depends(verify_api_key)])
def create_booking(
    request: UserBookingIntentRequest,
    current_user: AuthenticatedSupabaseUser = Depends(get_current_supabase_user),
):
    if request.user_id != current_user.id:
        raise HTTPException(status_code=403, detail="Requested user_id does not match authenticated user.")
    payload = request.model_dump(mode="json")
    payload["user_id"] = current_user.id
    return JSONResponse(content=execute_booking(payload))


@app.post("/v1/bookings/{booking_id}/cancel", dependencies=[Depends(verify_api_key)])
def cancel_booking(
    booking_id: str,
    request: UserCancelBookingRequest,
    current_user: AuthenticatedSupabaseUser = Depends(get_current_supabase_user),
):
    if request.user_id != current_user.id:
        raise HTTPException(status_code=403, detail="Requested user_id does not match authenticated user.")
    with SessionLocal() as db:
        booking = db.get(Booking, booking_id)
        if not booking:
            raise HTTPException(status_code=404, detail="Booking not found.")
        if request.building_id and booking.building_id != request.building_id:
            raise HTTPException(status_code=403, detail="Booking does not belong to this building.")
        if booking.user_id != current_user.id:
            raise HTTPException(status_code=403, detail="Booking does not belong to this user.")

    payload = {
        "intent": "CANCEL_BOOKING",
        "booking_id": booking_id,
        "user_id": current_user.id,
        "building_id": request.building_id,
    }
    return JSONResponse(content=execute_booking(payload))


@app.get("/v1/bookings/my", response_model=UserBookingListResponse, dependencies=[Depends(verify_api_key)])
def my_bookings(
    user_id: Optional[str] = None,
    building_id: Optional[str] = None,
    current_user: AuthenticatedSupabaseUser = Depends(get_current_supabase_user),
):
    if user_id and user_id != current_user.id:
        raise HTTPException(status_code=403, detail="Requested user_id does not match authenticated user.")
    effective_user_id = current_user.id
    now_utc = datetime.now(timezone.utc)
    with SessionLocal() as db:
        stmt = (
            select(Booking, Amenity)
            .join(Amenity, Amenity.id == Booking.amenity_id)
            .where(
                Booking.user_id == effective_user_id,
                Booking.status == BookingStatus.BOOKED.value,
                Booking.start_time >= now_utc,
            )
            .order_by(Booking.start_time.asc())
        )
        if building_id:
            stmt = stmt.where(Booking.building_id == building_id)

        rows = db.execute(stmt).all()
        return UserBookingListResponse(
            bookings=[
                UserBookingItem(
                    booking_id=str(booking.id),
                    building_id=booking.building_id,
                    amenity_id=booking.amenity_id,
                    amenity_name=amenity.name,
                    user_id=booking.user_id,
                    status=booking.status,
                    start_time=booking.start_time,
                    end_time=booking.end_time,
                    created_at=booking.created_at,
                )
                for booking, amenity in rows
            ]
        )


@app.get("/v1/bookings/history", response_model=UserBookingListResponse, dependencies=[Depends(verify_api_key)])
def booking_history(
    user_id: Optional[str] = None,
    building_id: Optional[str] = None,
    current_user: AuthenticatedSupabaseUser = Depends(get_current_supabase_user),
):
    if user_id and user_id != current_user.id:
        raise HTTPException(status_code=403, detail="Requested user_id does not match authenticated user.")
    effective_user_id = current_user.id
    now_utc = datetime.now(timezone.utc)
    with SessionLocal() as db:
        stmt = (
            select(Booking, Amenity)
            .join(Amenity, Amenity.id == Booking.amenity_id)
            .where(
                Booking.user_id == effective_user_id,
                or_(
                    Booking.status == BookingStatus.CANCELLED.value,
                    Booking.end_time < now_utc,
                ),
            )
            .order_by(Booking.start_time.desc())
        )
        if building_id:
            stmt = stmt.where(Booking.building_id == building_id)

        rows = db.execute(stmt).all()
        return UserBookingListResponse(
            bookings=[
                UserBookingItem(
                    booking_id=str(booking.id),
                    building_id=booking.building_id,
                    amenity_id=booking.amenity_id,
                    amenity_name=amenity.name,
                    user_id=booking.user_id,
                    status=booking.status,
                    start_time=booking.start_time,
                    end_time=booking.end_time,
                    created_at=booking.created_at,
                )
                for booking, amenity in rows
            ]
        )


@app.post("/v1/admin/amenities", dependencies=[Depends(verify_admin_api_key)])
def admin_upsert_amenity(request: AmenityUpsertRequest):
    return JSONResponse(content=upsert_amenity(request.model_dump(mode="json")))


@app.post("/v1/admin/rules", dependencies=[Depends(verify_admin_api_key)])
def admin_update_rules(request: AmenityRuleUpdateRequest):
    return JSONResponse(content=update_amenity_rules(request.model_dump(mode="json")))


@app.get("/v1/admin/users", response_model=AdminUserListResponse, dependencies=[Depends(verify_admin_api_key)])
def admin_list_users():
    client = get_supabase_service_client()
    auth_users = _extract_auth_user_records(client.auth.admin.list_users())
    role_directory = _load_role_directory()

    users: list[AdminUserItem] = []
    for auth_user in auth_users:
        user_item = _build_admin_user_item(auth_user, role_directory)
        if user_item is not None:
            users.append(user_item)

    users.sort(
        key=lambda item: item.created_at.isoformat() if item.created_at else "",
        reverse=True,
    )
    return AdminUserListResponse(users=users)


@app.post("/v1/admin/users", response_model=AdminUserCreateResponse, dependencies=[Depends(verify_admin_api_key)])
async def admin_create_user(request: AdminUserCreateRequest):
    client = get_supabase_service_client()
    temporary_password = _generate_temporary_password()

    metadata_payload = {
        "resident_id": request.resident_id,
        "name": request.name,
        "full_name": request.name,
        "phone": request.phone,
        "apartment": request.apartment,
        "role": request.role.value,
    }

    try:
        response = await run_in_threadpool(
            client.auth.admin.create_user,
            {
                "email": request.email,
                "password": temporary_password,
                "email_confirm": True,
                "user_metadata": metadata_payload,
            },
        )
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Unable to create user: {exc}") from exc

    created_users = _extract_auth_user_records(response)
    if not created_users:
        raise HTTPException(status_code=500, detail="User was created, but Supabase response did not include a user record.")

    created_user = created_users[0]
    auth_user_id = str(created_user.get("id", "")).strip()
    if not auth_user_id:
        raise HTTPException(status_code=500, detail="User was created, but Supabase response did not include a valid user id.")

    await run_in_threadpool(_persist_role_mapping, auth_user_id, request.role.value)

    email_body = f"""
    <html>
      <body style=\"font-family: Arial, sans-serif; color: #1f2937;\">
        <h2>Welcome to Amenity Booking</h2>
        <p>Hello {request.name},</p>
        <p>Your login account has been created by the admin.</p>
        <p><strong>Email:</strong> {request.email}</p>
        <p><strong>Temporary Password:</strong> {temporary_password}</p>
        <p>Please log in and change your password immediately.</p>
      </body>
    </html>
    """

    try:
        await send_email(
            to_email=request.email,
            subject="Your Amenity Booking Login Credentials",
            html_body=email_body,
        )
    except EmailServiceError as exc:
        # Avoid orphaned auth users when credential email fails.
        try:
            await run_in_threadpool(client.auth.admin.delete_user, auth_user_id)
        except Exception:
            pass
        raise HTTPException(status_code=502, detail=f"User created but credential email failed: {exc}") from exc

    role_directory = await run_in_threadpool(_load_role_directory)
    user_item = _build_admin_user_item(created_user, role_directory)
    if user_item is None:
        raise HTTPException(status_code=500, detail="Unable to build the created user response.")

    return AdminUserCreateResponse(
        success=True,
        message="User created. Temporary password sent to user email.",
        temporary_password="(sent to user email)",
        user=user_item,
    )




@app.put("/v1/admin/users/{user_id}", response_model=AdminUserUpdateResponse, dependencies=[Depends(verify_admin_api_key)])
def admin_update_user(user_id: str, request: AdminUserUpdateRequest):
    client = get_supabase_service_client()
    user_id = user_id.strip()

    if not user_id:
        raise HTTPException(status_code=400, detail="User ID is required.")

    # Build update payload for Supabase auth metadata
    metadata_updates = {}
    if request.name is not None:
        metadata_updates["name"] = request.name
        metadata_updates["full_name"] = request.name
    if request.phone is not None:
        metadata_updates["phone"] = request.phone
    if request.apartment is not None:
        metadata_updates["apartment"] = request.apartment
    if request.role is not None:
        metadata_updates["role"] = request.role.value

    # Update Supabase auth user metadata
    if metadata_updates:
        try:
            client.auth.admin.update_user_by_id(
                user_id,
                attributes={"user_metadata": metadata_updates}
            )
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"Unable to update user metadata: {exc}") from exc

    # Update email if requested (separate call)
    if request.email is not None:
        try:
            client.auth.admin.update_user_by_id(
                user_id,
                attributes={"email": request.email}
            )
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"Unable to update email: {exc}") from exc

    # Build update payload for database tables
    db_updates = {"id": user_id}
    if request.name is not None:
        db_updates["full_name"] = request.name
    if request.email is not None:
        db_updates["email"] = request.email
    if request.role is not None:
        db_updates["role"] = request.role.value
    if request.status is not None:
        db_updates["status"] = request.status

    # Update database tables (users and profiles)
    if len(db_updates) > 1:  # More than just the ID
        for table_name in ("users", "profiles"):
            try:
                client.table(table_name).upsert(db_updates, on_conflict="id").execute()
            except Exception:
                # If table doesn't exist or update fails, continue
                pass

    # Handle status changes (suspend/reactivate user)
    if request.status is not None:
        try:
            if request.status in ("SUSPENDED", "INACTIVE"):
                # Ban the user (Supabase way to suspend)
                client.auth.admin.update_user_by_id(
                    user_id,
                    attributes={"ban_duration": "876000h"}  # ~100 years
                )
            elif request.status == "ACTIVE":
                # Unban the user
                client.auth.admin.update_user_by_id(
                    user_id,
                    attributes={"ban_duration": "none"}
                )
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"Unable to update user status: {exc}") from exc

    # Fetch updated user data
    try:
        auth_users = _extract_auth_user_records(client.auth.admin.list_users())
        updated_auth_user = next((u for u in auth_users if str(u.get("id", "")).strip() == user_id), None)
        if not updated_auth_user:
            raise HTTPException(status_code=404, detail="User not found after update.")

        role_directory = _load_role_directory()
        user_item = _build_admin_user_item(updated_auth_user, role_directory)
        if user_item is None:
            raise HTTPException(status_code=500, detail="Unable to build updated user response.")

        return AdminUserUpdateResponse(
            success=True,
            message="User profile updated successfully.",
            user=user_item,
        )
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Unable to fetch updated user data: {exc}") from exc


@app.delete("/v1/admin/users/{user_id}", response_model=AdminUserDeleteResponse, dependencies=[Depends(verify_admin_api_key)])
def admin_delete_user(user_id: str):
    user_id = user_id.strip()
    if not user_id:
        raise HTTPException(status_code=400, detail="User ID is required.")

    with SessionLocal() as db:
        now_utc = datetime.now(timezone.utc)
        active_booking_exists = db.execute(
            select(Booking.id).where(
                Booking.user_id == user_id,
                Booking.status == "BOOKED",
                Booking.end_time >= now_utc,
            ).limit(1)
        ).scalar_one_or_none()

    if active_booking_exists is not None:
        raise HTTPException(status_code=409, detail="Cannot delete user with active bookings. Cancel bookings first.")

    client = get_supabase_service_client()

    # Best-effort cleanup in custom tables.
    for table_name in ("users", "profiles"):
        try:
            client.table(table_name).delete().eq("id", user_id).execute()
        except Exception:
            pass
        try:
            client.table(table_name).delete().eq("auth_user_id", user_id).execute()
        except Exception:
            pass

    try:
        client.auth.admin.delete_user(user_id)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Unable to delete auth user: {exc}") from exc

    return AdminUserDeleteResponse(
        success=True,
        message="User deleted successfully.",
        user_id=user_id,
    )
