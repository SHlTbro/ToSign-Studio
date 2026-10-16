"""Font-preset domain service (patch-C section 4, CC-11).

Manifest-driven: ``fonts/presets/manifest.json`` is the single source of
truth for the seeded preset library; no DB table exists (system assets are
read-only and do not count against the per-user 5GB quota - orchestrator
ruling, D-T65-4).

Download tickets are HMAC-signed bearer tokens so ``@font-face`` can fetch
without custom headers (patch-C section 4 "signed URL, TTL >= 10min";
TTL is 900s for clock-skew margin). Contract error mapping (D-T65-2):
malformed/tampered/expired tokens -> 400 INVALID_REQUEST; unknown preset ->
404 PRESET_NOT_FOUND; manifest-listed but unreadable file ->
500 PRESET_DOWNLOAD_FAILED (retryable).

Signing key (T75, D-T65-6): ``YY_FONT_SIGNING_SECRET`` resolves exactly once
per process in :func:`ensure_signing_secret` (the single getenv). When unset,
a fresh per-process random key is generated and a WARNING is logged - the
public default is gone, so tickets forged with it are rejected.
"""

import base64
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path, PureWindowsPath

from sqlalchemy.orm import Session

from app.core.audit import audit
from app.core.errors import ApiError
from app.domains.styles.models import StyleProfile

TTL_SECONDS = 900  # contract floor is 600s; 900s leaves clock-skew margin

# Legal source enum for preset-backed profiles (P10: "统一合法source枚举及旧
# 数据映射"). Rows are always written as M0_SYSTEM; SYSTEM_FONT is the
# pre-patch-C spelling that historical deployments may still carry on the same
# font_url, so compat reads accept both and old rows degrade to "already
# activated" instead of duplicating on re-activation.
_PRESET_SOURCE_TYPES = ("M0_SYSTEM", "SYSTEM_FONT")

logger = logging.getLogger("yy.font_presets")
_process_secret: str | None = None


def ensure_signing_secret() -> str:
    """Single resolution point for the ticket-signing key. Env wins (tickets
    verifiable across processes/restarts); otherwise a per-process random key
    is generated and the deployment is warned (D-T65-6)."""
    global _process_secret
    if _process_secret is None:
        env = os.environ.get("YY_FONT_SIGNING_SECRET")
        if env:
            _process_secret = env
        else:
            _process_secret = secrets.token_hex(32)
            logger.warning(
                "生产部署必须设置 YY_FONT_SIGNING_SECRET：本次启动已生成进程随机密钥，"
                "票据跨进程/重启不可验"
            )
    return _process_secret


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64url_decode(text: str) -> bytes:
    padding = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + padding)


def presets_root() -> Path:
    """Resolve the preset root per request so probes can override per run."""
    env = os.environ.get("YY_FONT_PRESETS_ROOT")
    if env:
        return Path(env)
    # app/domains/font_presets/service.py -> repo root is three levels up.
    return Path(__file__).resolve().parents[3] / "fonts" / "presets"


def _secret() -> str:
    return ensure_signing_secret()


def _font_path(file_name: str) -> Path:
    """Only catalog-root descendants may become downloadable font assets."""
    if (not file_name or "\x00" in file_name or "\\" in file_name
            or Path(file_name).is_absolute() or PureWindowsPath(file_name).drive
            or ".." in Path(file_name).parts):
        raise ValueError("invalid preset file path")
    root = presets_root().resolve()
    path = (root / file_name).resolve()
    if not path.is_relative_to(root) or path == root:
        raise ValueError("preset file escapes catalog root")
    return path


def _read_catalog() -> tuple[dict, str]:
    """Optional licensed presets are separate from Base and the ML addon.

    A missing or broken optional catalog must not make profile onboarding
    unavailable. Never invent preset entries from system/addon fallback fonts.
    """
    path = presets_root() / "manifest.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or not isinstance(data.get("presets"), list):
            raise ValueError("invalid preset catalog")
        entries = {}
        for entry in data["presets"]:
            if not isinstance(entry, dict):
                raise ValueError("invalid preset entry")
            preset_id, file_name = entry.get("preset_id"), entry.get("file_name")
            if (not isinstance(preset_id, str) or not preset_id.strip()
                    or preset_id in entries or not isinstance(file_name, str)
                    or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", preset_id)):
                raise ValueError("invalid preset identity")
            _font_path(file_name)
            for field in ("name", "description", "version"):
                if entry.get(field) is not None and not isinstance(entry[field], str):
                    raise ValueError("invalid preset metadata")
            size = entry.get("file_size_bytes")
            if size is not None and (not isinstance(size, int) or isinstance(size, bool) or size < 0):
                raise ValueError("invalid preset size")
            entries[preset_id] = entry
        return entries, "ready"
    except FileNotFoundError:
        return {}, "not_installed"
    except (OSError, ValueError, TypeError, RuntimeError):
        # Only local logs expose the implementation path; client guidance is
        # stable and does not imply that repeating a request repairs assets.
        logger.warning("Optional preset catalog is invalid or unreadable")
        return {}, "invalid"


def load_manifest() -> dict:
    """Actions require a real readable catalog; listing may degrade safely."""
    manifest, status = _read_catalog()
    if status != "ready":
        raise ApiError(
            503, "PRESET_CATALOG_UNAVAILABLE", "Preset catalog unavailable",
            "预设字体资源未安装或不可用，请安装或修复已授权的预设字体资源。系统字体功能与手写档案仍可使用。",
            False,
        )
    return manifest


def available_font_paths() -> dict[str, Path]:
    """Share validated optional catalog paths with the ordinary renderer."""
    manifest, _ = _read_catalog()
    paths = {}
    for preset_id, entry in manifest.items():
        try:
            path = _font_path(entry["file_name"])
            if path.is_file():
                paths[preset_id] = path
        except (OSError, ValueError, RuntimeError):
            continue
    return paths


def _make_token(preset_id: str, exp: float) -> str:
    payload = json.dumps(
        {"preset_id": preset_id, "exp": exp}, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    sig = hmac.new(_secret().encode("utf-8"), payload, hashlib.sha256).digest()
    return _b64url(payload) + "." + _b64url(sig)


def _parse_token(token: str) -> str:
    """Verify signature and expiry; return the preset_id or raise 400."""
    parts = token.split(".")
    if len(parts) != 2:
        raise ApiError(400, "INVALID_REQUEST", "malformed download token")
    try:
        payload = _b64url_decode(parts[0])
        sig = _b64url_decode(parts[1])
    except (ValueError, TypeError) as exc:
        raise ApiError(400, "INVALID_REQUEST", "malformed download token") from exc
    expected = hmac.new(_secret().encode("utf-8"), payload, hashlib.sha256).digest()
    if not hmac.compare_digest(sig, expected):
        raise ApiError(400, "INVALID_REQUEST", "download token signature mismatch")
    try:
        data = json.loads(payload)
        preset_id = data["preset_id"]
        exp = float(data["exp"])
    except (ValueError, KeyError, TypeError) as exc:
        raise ApiError(400, "INVALID_REQUEST", "malformed download token") from exc
    if datetime.now(timezone.utc).timestamp() >= exp:
        raise ApiError(400, "INVALID_REQUEST", "download token expired")
    return preset_id


def _list_presets(session: Session, user_id: int, manifest: dict) -> list:
    """Contract v0.2 line 63 six-field shape; ``font_url`` points at the
    ticket-issuing endpoint (D-T65-1: the list never embeds a signed URL).
    Activation state is caller-scoped through style_profiles."""
    font_urls = {f"/api/v1/style-presets/{preset_id}/download" for preset_id in manifest}
    activated = {
        row.font_url: row.style_id
        for row in session.query(StyleProfile)
        .filter(
            StyleProfile.user_id == user_id,
            StyleProfile.source_type.in_(_PRESET_SOURCE_TYPES),
            StyleProfile.font_url.in_(font_urls),
        )
        .all()
    }
    available = available_font_paths()
    items = []
    for preset_id, entry in manifest.items():
        font_url = f"/api/v1/style-presets/{preset_id}/download"
        items.append(
            {
                "preset_id": preset_id,
                "name": entry.get("name"),
                "description": entry.get("description"),
                "font_url": font_url,
                "file_size_bytes": entry.get("file_size_bytes"),
                "version": entry.get("version"),
                "activated_style_id": activated.get(font_url),
                "available": preset_id in available,
            }
        )
    return items


def list_catalog(session: Session, user_id: int) -> dict:
    manifest, status = _read_catalog()
    system_font = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts" / "simkai.ttf"
    return {
        "items": _list_presets(session, user_id, manifest),
        "catalog": {
            "status": status,
            "message": {
                "ready": "",
                "not_installed": "此安装未包含可选预设字体资源，可继续创建手写档案或使用系统字体。",
                "invalid": "可选预设字体资源不可用，请修复已授权的字体资源；手写档案和系统字体不受影响。",
            }[status],
        },
        "fallback": {
            "source_type": "M0_SYSTEM",
            "font_name": "simkai",
            "available": system_font.is_file(),
            "downloadable": False,
        },
    }


def list_presets(session: Session, user_id: int) -> list:
    """Keep the existing service-level list shape for older callers."""
    return list_catalog(session, user_id)["items"]


def activate_preset(session: Session, user_id: int, preset_id: str) -> StyleProfile:
    """Persist a manifest-backed system font in the caller's style library.

    Official M0 font presets are already trusted system assets, so activation
    creates an approved, enabled style profile that the existing render path
    can consume. The profile stores only the stable manifest URL; font bytes
    and licensing metadata remain owned by the manifest.
    """
    manifest = load_manifest()
    entry = manifest.get(preset_id)
    if entry is None:
        raise ApiError(404, "PRESET_NOT_FOUND", "Preset not found")
    if not _font_path(entry["file_name"]).is_file():
        raise ApiError(503, "PRESET_FONT_UNAVAILABLE", "Preset font unavailable",
                       "预设字体文件未安装或不可用，请修复资源后再启用。", False)

    font_url = f"/api/v1/style-presets/{preset_id}/download"
    existing = (
        session.query(StyleProfile)
        .filter(
            StyleProfile.user_id == user_id,
            StyleProfile.source_type.in_(_PRESET_SOURCE_TYPES),
            StyleProfile.font_url == font_url,
        )
        .first()
    )
    if existing is not None:
        return existing

    row = StyleProfile(
        user_id=user_id,
        name=entry.get("name") or preset_id,
        source_type="M0_SYSTEM",
        status="approved",
        quality_status=None,
        coverage=None,
        model_version=entry.get("version"),
        receipt_id=None,
        font_url=font_url,
        enabled=1,
        reject_reason=None,
        created_at=datetime.now(timezone.utc).isoformat(),
    )
    session.add(row)
    session.flush()
    audit(
        session,
        action="style_preset_activate",
        actor=str(user_id),
        object_ref=f"style_profiles/{row.style_id}",
        result="ok",
    )
    return row


def issue_download(preset_id: str) -> dict:
    """Issue a signed download ticket; 404 for unknown presets (D-T65-2)."""
    manifest = load_manifest()
    if preset_id not in manifest:
        raise ApiError(404, "PRESET_NOT_FOUND", "Preset not found")
    exp = datetime.now(timezone.utc).timestamp() + TTL_SECONDS
    expires_at = datetime.fromtimestamp(exp, timezone.utc).isoformat()
    return {
        "url": f"/api/v1/font-downloads/{_make_token(preset_id, exp)}",
        "expires_at": expires_at,
        "ttl_seconds": TTL_SECONDS,
    }


def resolve_download(token: str):
    """Verify the ticket and return the font file bytes; unknown preset inside
    a validly-signed ticket answers 404 (D-T65-2), an unreadable file answers
    500 PRESET_DOWNLOAD_FAILED (patch-C line 70)."""
    preset_id = _parse_token(token)
    manifest = load_manifest()
    entry = manifest.get(preset_id)
    if entry is None:
        raise ApiError(404, "PRESET_NOT_FOUND", "Preset not found")
    try:
        path = _font_path(entry["file_name"])
        return path.read_bytes()
    except (OSError, ValueError, RuntimeError) as exc:
        raise ApiError(
            500, "PRESET_DOWNLOAD_FAILED", "Font file unreadable",
            "预设字体文件不可用，请修复已授权的字体资源。", True
        ) from exc
