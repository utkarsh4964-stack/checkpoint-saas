from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field, field_validator

from backend.auth import get_current_user
from backend.config import settings
from backend.db import repositories as repo
from backend.models.schemas import User
from backend.ratelimit import rate_limiter
from backend.security import create_access_token, hash_password, verify_password

router = APIRouter(prefix="/auth", tags=["auth"])


def _valid_email(v: str) -> str:
    v = v.strip().lower()
    if "@" not in v or " " in v or v.startswith("@") or v.endswith("@") or len(v) > 254:
        raise ValueError("Enter a valid email address.")
    return v


class RegisterRequest(BaseModel):
    email: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=8, max_length=128)

    @field_validator("email")
    @classmethod
    def _check_email(cls, v: str) -> str:
        return _valid_email(v)


class LoginRequest(BaseModel):
    email: str
    password: str

    @field_validator("email")
    @classmethod
    def _check_email(cls, v: str) -> str:
        return _valid_email(v)


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user_id: str
    email: str


def _client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


@router.post("/register", response_model=TokenResponse, status_code=status.HTTP_201_CREATED)
def register(req: RegisterRequest, request: Request) -> TokenResponse:
    ip = _client_ip(request)
    if not rate_limiter.check(
        f"register:{ip}", settings.REGISTER_RATE_LIMIT_MAX, settings.REGISTER_RATE_LIMIT_WINDOW_SEC
    ):
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS, "Too many registration attempts. Please try again later."
        )

    if repo.get_user_by_email(req.email):
        raise HTTPException(status.HTTP_409_CONFLICT, "An account with this email already exists.")

    user = User(email=req.email, password_hash=hash_password(req.password))
    repo.create_user(user)
    token = create_access_token(user.id, user.email)
    return TokenResponse(access_token=token, user_id=user.id, email=user.email)


@router.post("/login", response_model=TokenResponse)
def login(req: LoginRequest, request: Request) -> TokenResponse:
    ip = _client_ip(request)
    if not rate_limiter.check(f"login-ip:{ip}", settings.LOGIN_RATE_LIMIT_MAX, settings.LOGIN_RATE_LIMIT_WINDOW_SEC):
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "Too many login attempts from this network. Please wait a few minutes and try again.",
        )
    if not rate_limiter.check(
        f"login-email:{req.email}", settings.LOGIN_RATE_LIMIT_MAX, settings.LOGIN_RATE_LIMIT_WINDOW_SEC
    ):
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "Too many failed attempts for this account. Please wait a few minutes and try again.",
        )

    user = repo.get_user_by_email(req.email)
    if not user or not verify_password(req.password, user.password_hash):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Incorrect email or password.")
    if not user.is_active:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "This account has been disabled.")

    token = create_access_token(user.id, user.email)
    return TokenResponse(access_token=token, user_id=user.id, email=user.email)


@router.get("/me")
def me(user: User = Depends(get_current_user)):
    return {"id": user.id, "email": user.email, "created_at": user.created_at}
