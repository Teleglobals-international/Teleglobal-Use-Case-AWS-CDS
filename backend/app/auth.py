"""
Authentication backed by Amazon Cognito (with graceful in-memory fallback).

Flows exposed:
  register(email, password, profile)       -> SignUp (Cognito emails a code)
  confirm(email, code)                      -> ConfirmSignUp
  login(email, password)                    -> InitiateAuth (USER_PASSWORD_AUTH)
  forgot_password(email)                    -> ForgotPassword (Cognito emails a code)
  reset_password(email, code, new_password) -> ConfirmForgotPassword
  get_profile(email)                        -> AdminGetUser attributes -> profile dict
  set_kyc_status(email, status)             -> AdminUpdateUserAttributes

Investor profile fields map to Cognito attributes:
  name                 -> standard "name"
  language             -> "custom:language"
  risk_tolerance       -> "custom:risk_tolerance"
  investment_goals     -> "custom:investment_goals" (comma-joined)
  kyc_status           -> "custom:kyc_status"

When Cognito is not configured (COGNITO_USER_POOL_ID/CLIENT_ID unset), callers
should fall back to the in-memory Memory store. `cognito_enabled()` reports this.
"""
from __future__ import annotations

import logging
from typing import Any

from botocore.exceptions import ClientError

from .aws_clients import cognito_idp
from .config import get_settings

logger = logging.getLogger("mf_assistant.auth")


class AuthError(Exception):
    """User-facing authentication error (safe to show in the UI)."""

    def __init__(self, message: str, code: str = "auth_error") -> None:
        super().__init__(message)
        self.message = message
        self.code = code


def cognito_enabled() -> bool:
    return get_settings().cognito_enabled


# --------------------------------------------------------------------------- #
# Attribute <-> profile mapping
# --------------------------------------------------------------------------- #
def _profile_to_attributes(email: str, profile: dict[str, Any]) -> list[dict[str, str]]:
    goals = profile.get("investment_goals") or []
    if isinstance(goals, list):
        goals = ",".join(goals)
    attrs = {
        "email": email,
        "name": profile.get("name", ""),
        "custom:language": profile.get("language", "English"),
        "custom:risk_tolerance": profile.get("risk_tolerance", "Moderate"),
        "custom:investment_goals": goals or "",
        "custom:kyc_status": profile.get("kyc_status", "Pending"),
    }
    return [{"Name": k, "Value": str(v)} for k, v in attrs.items() if v != ""]


def _attributes_to_profile(email: str, attrs: dict[str, str]) -> dict[str, Any]:
    goals = attrs.get("custom:investment_goals", "")
    return {
        "user_id": email,
        "name": attrs.get("name", email.split("@")[0]),
        "channel": "web",
        "language": attrs.get("custom:language", "English"),
        "risk_tolerance": attrs.get("custom:risk_tolerance", "Moderate"),
        "investment_goals": [g for g in goals.split(",") if g.strip()],
        "kyc_status": attrs.get("custom:kyc_status", "Pending"),
        "holdings": [],
    }


def _friendly(err: ClientError) -> AuthError:
    code = err.response.get("Error", {}).get("Code", "")
    msg = err.response.get("Error", {}).get("Message", "Authentication failed.")
    mapping = {
        "UsernameExistsException": "An account with this email already exists.",
        "UserNotFoundException": "No account found for that email.",
        "NotAuthorizedException": "Incorrect email or password.",
        "UserNotConfirmedException": "Please confirm your email first (check the code we sent).",
        "CodeMismatchException": "That verification code is incorrect.",
        "ExpiredCodeException": "That code has expired. Please request a new one.",
        "InvalidPasswordException": "Password must be 8+ chars with upper, lower and a number.",
        "InvalidParameterException": msg,
        "LimitExceededException": "Too many attempts. Please try again shortly.",
    }
    return AuthError(mapping.get(code, msg), code or "auth_error")


# --------------------------------------------------------------------------- #
# Flows
# --------------------------------------------------------------------------- #
def register(email: str, password: str, profile: dict[str, Any]) -> dict[str, Any]:
    s = get_settings()
    try:
        resp = cognito_idp().sign_up(
            ClientId=s.COGNITO_CLIENT_ID,
            Username=email,
            Password=password,
            UserAttributes=_profile_to_attributes(email, profile),
        )
        return {"status": "confirmation_required", "confirmed": resp.get("UserConfirmed", False)}
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code", "")
        # If the email exists but was never confirmed, resend the code instead
        # of hard-failing — lets the user pick up where they left off.
        if code == "UsernameExistsException":
            try:
                u = cognito_idp().admin_get_user(UserPoolId=s.COGNITO_USER_POOL_ID, Username=email)
                if u.get("UserStatus") == "UNCONFIRMED":
                    cognito_idp().resend_confirmation_code(ClientId=s.COGNITO_CLIENT_ID, Username=email)
                    return {"status": "confirmation_required", "confirmed": False, "resent": True}
            except ClientError:
                pass
        raise _friendly(e)


def confirm(email: str, code: str) -> dict[str, Any]:
    s = get_settings()
    try:
        cognito_idp().confirm_sign_up(ClientId=s.COGNITO_CLIENT_ID, Username=email, ConfirmationCode=code)
        return {"status": "confirmed"}
    except ClientError as e:
        raise _friendly(e)


def resend_code(email: str) -> dict[str, Any]:
    s = get_settings()
    try:
        cognito_idp().resend_confirmation_code(ClientId=s.COGNITO_CLIENT_ID, Username=email)
        return {"status": "code_sent"}
    except ClientError as e:
        raise _friendly(e)


def login(email: str, password: str) -> dict[str, Any]:
    s = get_settings()
    try:
        cognito_idp().initiate_auth(
            ClientId=s.COGNITO_CLIENT_ID,
            AuthFlow="USER_PASSWORD_AUTH",
            AuthParameters={"USERNAME": email, "PASSWORD": password},
        )
        return {"status": "ok", "profile": get_profile(email)}
    except ClientError as e:
        raise _friendly(e)


def forgot_password(email: str) -> dict[str, Any]:
    s = get_settings()
    try:
        cognito_idp().forgot_password(ClientId=s.COGNITO_CLIENT_ID, Username=email)
        return {"status": "code_sent"}
    except ClientError as e:
        raise _friendly(e)


def reset_password(email: str, code: str, new_password: str) -> dict[str, Any]:
    s = get_settings()
    try:
        cognito_idp().confirm_forgot_password(
            ClientId=s.COGNITO_CLIENT_ID,
            Username=email,
            ConfirmationCode=code,
            Password=new_password,
        )
        return {"status": "password_reset"}
    except ClientError as e:
        raise _friendly(e)


def get_profile(email: str) -> dict[str, Any]:
    s = get_settings()
    try:
        u = cognito_idp().admin_get_user(UserPoolId=s.COGNITO_USER_POOL_ID, Username=email)
        attrs = {a["Name"]: a["Value"] for a in u.get("UserAttributes", [])}
        return _attributes_to_profile(email, attrs)
    except ClientError as e:
        raise _friendly(e)


def user_exists(email: str) -> bool:
    s = get_settings()
    try:
        cognito_idp().admin_get_user(UserPoolId=s.COGNITO_USER_POOL_ID, Username=email)
        return True
    except ClientError:
        return False


def set_kyc_status(email: str, status: str) -> None:
    s = get_settings()
    cognito_idp().admin_update_user_attributes(
        UserPoolId=s.COGNITO_USER_POOL_ID,
        Username=email,
        UserAttributes=[{"Name": "custom:kyc_status", "Value": status}],
    )
