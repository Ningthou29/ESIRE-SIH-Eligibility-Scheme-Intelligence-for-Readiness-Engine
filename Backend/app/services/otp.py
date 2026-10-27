from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import OtpChallenge
from app.security import hash_value, verify_value

settings = get_settings()


class OtpProvider:
    """Development OTP abstraction. Swap send() for SMS later."""

    def send(self, phone: str, code: str) -> None:
        print(f"[PAANGBA OTP] {phone} -> {code}")


otp_provider = OtpProvider()


def issue_otp(db: Session, phone: str, purpose: str = "login") -> str:
    code = settings.otp_static_code if settings.environment != "production" else __import__("random").randint(100000, 999999)
    if settings.environment == "production":
        code = f"{__import__('random').randint(100000, 999999):06d}"
    else:
        code = settings.otp_static_code
    db.query(OtpChallenge).filter(OtpChallenge.phone == phone, OtpChallenge.consumed.is_(False)).update({"consumed": True})
    challenge = OtpChallenge(
        phone=phone,
        code_hash=hash_value(code),
        purpose=purpose,
        expires_at=datetime.utcnow() + timedelta(seconds=settings.otp_ttl_seconds),
    )
    db.add(challenge)
    db.commit()
    otp_provider.send(phone, code)
    return code


def verify_otp(db: Session, phone: str, code: str, purpose: str = "login") -> bool:
    challenge = (
        db.query(OtpChallenge)
        .filter(
            OtpChallenge.phone == phone,
            OtpChallenge.purpose == purpose,
            OtpChallenge.consumed.is_(False),
        )
        .order_by(OtpChallenge.id.desc())
        .first()
    )
    if not challenge or challenge.expires_at < datetime.utcnow():
        return False
    if not verify_value(code, challenge.code_hash):
        return False
    challenge.consumed = True
    db.commit()
    return True
