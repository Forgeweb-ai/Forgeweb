"""
forge_server/api/auth.py
=========================
JWT-based auth: register, login, /me.

Dev mode (DEV_MODE=true):
  All requests bypass JWT and are served as the built-in dev user.
  The dev user is auto-created on startup (see app.py lifespan).
  NEVER enable in production.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt
from passlib.context import CryptContext
from pydantic import BaseModel, EmailStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from forge_server.config import get_settings
from forge_server.db.database import get_db
from forge_server.db.models import User, UserSettings
from forge_server.api.constants import SETTINGS_DEFAULTS

log      = logging.getLogger("forge.auth")
settings = get_settings()
router   = APIRouter(prefix="/api/auth", tags=["auth"])
_pwd     = CryptContext(schemes=["bcrypt"], deprecated="auto")
_bearer  = HTTPBearer(auto_error=False)


# ── Pydantic schemas ──────────────────────────────────────────────────────────

class RegisterIn(BaseModel):
    email:    EmailStr
    username: str
    password: str


class LoginIn(BaseModel):
    email:    EmailStr
    password: str


class TokenOut(BaseModel):
    access_token:         str
    token_type:           str = "bearer"
    user_id:              str
    username:             str
    # FE uses these to route after auth: unverified → /auth/verify-email,
    # verified-but-incomplete → first unfinished onboarding step.
    email_verified:       bool
    onboarding_completed: bool


class UserOut(BaseModel):
    id:                   str
    email:                str
    username:             str
    created_at:           datetime
    email_verified:       bool
    onboarding_completed: bool
    full_name:            str | None = None
    role:                 str | None = None
    company_size:         str | None = None
    theme_pref:           str | None = None


class OnboardingIn(BaseModel):
    full_name:    str
    role:         str
    company_size: str
    theme_pref:   str | None = None


class ProfilePatchIn(BaseModel):
    """Partial profile update. Every field optional — only patches what's sent."""
    full_name:    str | None = None
    theme_pref:   str | None = None


# ── JWT helpers ───────────────────────────────────────────────────────────────

def _create_token(user_id: str) -> str:
    expire  = datetime.utcnow() + timedelta(minutes=settings.jwt_expire_minutes)
    payload = {"sub": user_id, "exp": expire}
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def _decode_token(token: str) -> str:
    """Returns user_id or raises HTTPException 401."""
    try:
        payload = jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])
        user_id: str | None = payload.get("sub")
        if not user_id:
            raise ValueError("missing sub")
        return user_id
    except JWTError as exc:
        raise HTTPException(status_code=401, detail=f"Invalid token: {exc}")


# ── Dependency ────────────────────────────────────────────────────────────────

async def current_user(
    creds: HTTPAuthorizationCredentials | None = Depends(_bearer),
    db:    AsyncSession                        = Depends(get_db),
) -> User:
    """Resolve the current user from the Bearer JWT. Raises 401 on failure."""

    if not creds:
        raise HTTPException(status_code=401, detail="Not authenticated")
    user_id = _decode_token(creds.credentials)
    result  = await db.execute(select(User).where(User.id == user_id))
    user    = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=401, detail="User not found")
    return user


async def current_user_optional(
    creds: HTTPAuthorizationCredentials | None = Depends(_bearer),
    db:    AsyncSession                        = Depends(get_db),
) -> User | None:
    """
    Resolve the current user if a valid Bearer JWT is present, else None.
    Use for endpoints that have a fallback path for unauthenticated callers
    (e.g. runtime-error ingestion from the in-iframe bridge, which can't
    carry the user's JWT). The endpoint itself must enforce whatever
    additional check (Origin header, IP allowlist, etc.) gates the
    unauthenticated path.
    """
    if not creds:
        return None
    try:
        user_id = _decode_token(creds.credentials)
    except HTTPException:
        return None
    result = await db.execute(select(User).where(User.id == user_id))
    return result.scalar_one_or_none()


async def current_user_qs(
    token: str | None = Query(None, alias="token"),
    creds: HTTPAuthorizationCredentials | None = Depends(_bearer),
    db:    AsyncSession = Depends(get_db),
) -> User:
    """
    Auth dependency that accepts token via either:
      - Authorization: Bearer <token>   (normal requests)
      - ?token=<token>                  (EventSource — can't set headers)
    Raises 401 if neither is valid.
    """

    raw = (creds.credentials if creds else None) or token
    if not raw:
        raise HTTPException(status_code=401, detail="Not authenticated")
    user_id = _decode_token(raw)
    result  = await db.execute(select(User).where(User.id == user_id))
    user    = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=401, detail="User not found")
    return user


# ── Routes ────────────────────────────────────────────────────────────────────

def _token_out(user: User) -> TokenOut:
    return TokenOut(
        access_token         = _create_token(user.id),
        user_id              = user.id,
        username             = user.username,
        email_verified       = bool(user.email_verified),
        onboarding_completed = bool(user.onboarding_completed),
    )


def _user_out(user: User) -> UserOut:
    return UserOut(
        id                   = user.id,
        email                = user.email,
        username             = user.username,
        created_at           = user.created_at,
        email_verified       = bool(user.email_verified),
        onboarding_completed = bool(user.onboarding_completed),
        full_name            = user.full_name,
        role                 = user.role,
        company_size         = user.company_size,
        theme_pref           = user.theme_pref,
    )


@router.post("/register", response_model=TokenOut, status_code=201)
async def register(body: RegisterIn, db: AsyncSession = Depends(get_db)):
    # Check duplicate email
    existing = await db.execute(select(User).where(User.email == body.email))
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=409, detail="Email already registered")

    user = User(
        email                = body.email,
        username             = body.username,
        hashed_password      = _pwd.hash(body.password),
        # Email verification is disabled for now — new accounts are verified
        # by default so users skip the /auth/verify-email screen. To re-enable
        # the flow later, flip this back to False (FE routing + the
        # verify-email page are still in place).
        email_verified       = True,
        onboarding_completed = False,
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)

    log.info("Registered new user %s (%s)", user.username, user.id)
    return _token_out(user)


@router.post("/login", response_model=TokenOut)
async def login(body: LoginIn, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(User).where(User.email == body.email))
    user   = result.scalar_one_or_none()

    if not user or not _pwd.verify(body.password, user.hashed_password):
        raise HTTPException(status_code=401, detail="Invalid credentials")

    return _token_out(user)


@router.get("/me", response_model=UserOut)
async def me(user: User = Depends(current_user)):
    return _user_out(user)


@router.patch("/me", response_model=UserOut)
async def update_me(
    body: ProfilePatchIn,
    user: User         = Depends(current_user),
    db:   AsyncSession = Depends(get_db),
):
    """
    Partial profile update — currently full_name and theme_pref.

    Why so narrow: email/username/password rotation are gated by a separate
    re-auth flow (not built yet); role and company_size are onboarding-only.
    Adding a generic "update anything" surface here would invite mistakes.
    Idempotent: omitting a field leaves it untouched.
    """
    touched = False

    if body.full_name is not None:
        name = body.full_name.strip()
        if not name:
            raise HTTPException(status_code=422, detail="full_name cannot be empty")
        if len(name) > 255:
            raise HTTPException(status_code=422, detail="full_name too long (max 255)")
        if name != user.full_name:
            user.full_name = name
            touched = True

    if body.theme_pref is not None:
        theme = body.theme_pref.strip().lower()
        if theme not in _ALLOWED_THEMES:
            raise HTTPException(status_code=422, detail=f"theme_pref must be one of {sorted(_ALLOWED_THEMES)}")
        if theme != user.theme_pref:
            user.theme_pref = theme
            touched = True

    if touched:
        await db.commit()
        await db.refresh(user)
    return _user_out(user)


# ── Email verification ────────────────────────────────────────────────────────
# Email sending is not wired up yet. The signup screen takes the user to
# /auth/verify-email, where clicking "I've verified" calls this endpoint and
# the flag flips to true. When real email is added later the FE contract
# doesn't change — only this endpoint's body will (it'll accept a token).

@router.post("/verify-email", response_model=UserOut)
async def verify_email(
    user: User         = Depends(current_user),
    db:   AsyncSession = Depends(get_db),
):
    """Mark the calling user's email as verified (no token required yet)."""
    if not user.email_verified:
        user.email_verified = True
        await db.commit()
        await db.refresh(user)
        log.info("User %s marked email verified", user.id)
    return _user_out(user)


# ── Onboarding ────────────────────────────────────────────────────────────────

_ALLOWED_ROLES = {
    "founder", "product", "designer", "engineer",
    "consultant", "marketing-sales", "operations", "other",
}
_ALLOWED_COMPANY_SIZES = {"solo", "2-20", "21-200", "200+"}
_ALLOWED_THEMES        = {"light", "dark"}


@router.post("/onboarding", response_model=UserOut)
async def complete_onboarding(
    body: OnboardingIn,
    user: User         = Depends(current_user),
    db:   AsyncSession = Depends(get_db),
):
    """
    Save the onboarding answers and flip onboarding_completed=True.
    Caller must already be verified (FE enforces this via routing, but we
    double-check here so the endpoint can't be skipped past).
    """
    if not user.email_verified:
        raise HTTPException(status_code=403, detail="Email must be verified before onboarding")

    full_name = body.full_name.strip()
    if not full_name:
        raise HTTPException(status_code=422, detail="full_name is required")

    role = body.role.strip().lower()
    if role not in _ALLOWED_ROLES:
        raise HTTPException(status_code=422, detail=f"role must be one of {sorted(_ALLOWED_ROLES)}")

    company_size = body.company_size.strip().lower()
    if company_size not in _ALLOWED_COMPANY_SIZES:
        raise HTTPException(status_code=422, detail=f"company_size must be one of {sorted(_ALLOWED_COMPANY_SIZES)}")

    theme_pref = (body.theme_pref or "light").strip().lower()
    if theme_pref not in _ALLOWED_THEMES:
        raise HTTPException(status_code=422, detail=f"theme_pref must be one of {sorted(_ALLOWED_THEMES)}")

    user.full_name            = full_name
    user.role                 = role
    user.company_size         = company_size
    user.theme_pref           = theme_pref
    user.onboarding_completed = True

    # Seed default user_settings (design_model = deepseek free) if not yet created.
    existing = await db.execute(
        select(UserSettings).where(UserSettings.user_id == user.id)
    )
    if existing.scalar_one_or_none() is None:
        import json as _json
        db.add(UserSettings(user_id=user.id, settings_json=_json.dumps(SETTINGS_DEFAULTS)))

    await db.commit()
    await db.refresh(user)

    log.info("User %s completed onboarding (%s / %s / %s)", user.id, role, company_size, theme_pref)
    return _user_out(user)
