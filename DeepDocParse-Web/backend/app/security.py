"""密码、JWT、API key 的原语。

三条口径：
- 用户密码：bcrypt（慢哈希，抗爆破）
- API key：sha256（每个对外请求都要验一次，慢哈希会压垮代理路径；
  key 是 32 字节随机串，不存在弱口令）
- 会话：JWT（HS256），Web 前端用
"""
import base64
import hashlib
import secrets
from datetime import UTC, datetime, timedelta

import bcrypt
from jose import JWTError, jwt

from app.config import settings

ALGORITHM = "HS256"
KEY_PREFIX = "sk-"


def _password_bytes(password: str) -> bytes:
    """bcrypt 只吃前 72 字节（超长会被静默截断，新版本直接报错）。
    先 sha256 再 base64，长度固定 44 字节且无 NUL —— 这是官方推荐的规避方式。"""
    return base64.b64encode(hashlib.sha256(password.encode()).digest())


def hash_password(password: str) -> str:
    # 成本因子可配只为了让单测跑得动（见 settings.bcrypt_rounds），生产用默认 12
    return bcrypt.hashpw(_password_bytes(password),
                         bcrypt.gensalt(rounds=settings.bcrypt_rounds)).decode()


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return bcrypt.checkpw(_password_bytes(password), password_hash.encode())
    except ValueError:      # 库里是损坏/非 bcrypt 的哈希
        return False


def create_access_token(user_id: str) -> str:
    expire = datetime.now(UTC) + timedelta(minutes=settings.jwt_ttl_minutes)
    return jwt.encode({"sub": user_id, "exp": expire}, settings.jwt_secret, algorithm=ALGORITHM)


def decode_access_token(token: str) -> str | None:
    """返回 user_id；无效/过期返回 None。"""
    try:
        payload = jwt.decode(token, settings.jwt_secret, algorithms=[ALGORITHM])
    except JWTError:
        return None
    return payload.get("sub")


def generate_api_key() -> tuple[str, str, str]:
    """返回 (明文 key, 展示用前缀, sha256 哈希)。明文只在创建时返回一次。"""
    plain = KEY_PREFIX + secrets.token_urlsafe(32)
    return plain, plain[:11], hash_api_key(plain)


def hash_api_key(plain: str) -> str:
    return hashlib.sha256(plain.encode()).hexdigest()


def constant_time_equals(a: str, b: str) -> bool:
    return secrets.compare_digest(a, b)
