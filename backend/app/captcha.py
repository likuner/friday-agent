import base64
import hmac
import io
import random
import secrets
import string
import time
from hashlib import sha256

from PIL import Image, ImageDraw, ImageFont

from .config import settings

_CAPTCHA_TTL = 600
_captchas: dict[str, tuple[str, int, str]] = {}


def create_captcha() -> tuple[str, str]:
    answer = "".join(random.choices(string.ascii_uppercase + string.digits, k=4)).lower()
    issued = int(time.time())
    nonce = secrets.token_urlsafe(18)
    signature = hmac.new(settings.jwt_secret.encode(), nonce.encode(), sha256).hexdigest()
    _captchas[nonce] = (answer, issued, signature)
    image = Image.new("RGB", (120, 42), "#f1f4ff")
    draw = ImageDraw.Draw(image)
    font = ImageFont.load_default(size=28)
    text = answer.upper()
    left, top, right, bottom = draw.textbbox((0, 0), text, font=font)
    draw.text(((120 - (right - left)) / 2 - left, (42 - (bottom - top)) / 2 - top), text, fill="#4057c9", font=font)
    for _ in range(3):
        draw.line(
            (random.randrange(120), random.randrange(42), random.randrange(120), random.randrange(42)),
            fill="#b9c5ef",
            width=1,
        )
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    encoded = base64.b64encode(buffer.getvalue()).decode()
    return f"data:image/png;base64,{encoded}", f"{nonce}.{signature}"


def verify_captcha(value: str, captcha_token: str) -> bool:
    try:
        nonce, signature = captcha_token.split(".", 1)
        answer, issued, expected_signature = _captchas.pop(nonce)
        valid_signature = hmac.compare_digest(signature, expected_signature)
        valid_time = time.time() - issued < _CAPTCHA_TTL
        return valid_signature and valid_time and hmac.compare_digest(value.strip().lower(), answer)
    except (KeyError, ValueError, TypeError):
        return False
