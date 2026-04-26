from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import json
import logging
import mimetypes
import os
import random
import re
import time
import unicodedata
from dataclasses import dataclass
from html import escape, unescape as html_unescape
from pathlib import Path
from typing import Any, Awaitable, Callable, Iterable

import httpx

try:
    from PIL import Image, ImageDraw, ImageFilter, ImageFont
except Exception:  # Pillow es opcional en import, pero está en requirements.
    Image = None
    ImageDraw = None
    ImageFilter = None
    ImageFont = None

from telegram import (
    BotCommand,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputFile,
    Update,
)
from telegram.constants import ChatAction, ParseMode
from telegram.error import NetworkError, RetryAfter, TelegramError, TimedOut
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)
from telegram.request import HTTPXRequest

import configuracion as cfg

# -----------------------------------------------------------------------------
# Logging
# -----------------------------------------------------------------------------

logging.basicConfig(
    level=getattr(logging, "INFO"),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("tata")
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("telegram").setLevel(logging.WARNING)

OPENAI_BASE = "https://api.openai.com/v1"
TATA: "TataBot | None" = None

# OpenAI keys are ASCII tokens. The TXT may contain comments, labels, BOMs,
# or accidental text after the key. We extract only the real sk-... token so
# non-ASCII text never reaches the HTTP Authorization header.
OPENAI_KEY_RE = re.compile(r"(?<![A-Za-z0-9_-])(sk-[A-Za-z0-9_-]{20,})(?![A-Za-z0-9_-])")
ZERO_WIDTH_CHARS = "\ufeff\u200b\u200c\u200d\u2060"


# -----------------------------------------------------------------------------
# Utilidades generales
# -----------------------------------------------------------------------------


def now_ts() -> float:
    return time.time()


def utc_stamp() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime()) + " UTC"


def safe_json_load(path: Path, default: Any) -> Any:
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        log.warning("No pude leer JSON %s: %s", path, exc)
    return default


def safe_json_save(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def truncate(text: str, limit: int) -> str:
    text = text or ""
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def split_text(text: str, limit: int = 3900) -> list[str]:
    text = text or ""
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    current = ""
    for part in re.split(r"(\n\n+)", text):
        if len(current) + len(part) <= limit:
            current += part
        else:
            if current.strip():
                chunks.append(current.strip())
            while len(part) > limit:
                chunks.append(part[:limit])
                part = part[limit:]
            current = part
    if current.strip():
        chunks.append(current.strip())
    return chunks


def telegram_inline_from_markdownish(src: str, *, _stash: list[str] | None = None) -> str:
    """Convierte markdown común de LLM a HTML compatible con Telegram.

    Telegram soporta HTML y MarkdownV2, pero MarkdownV2 exige escapar muchos
    caracteres especiales. Como las respuestas del modelo suelen venir con
    **negritas**, # títulos y listas con -, aquí las pasamos a HTML seguro.
    """
    src = (src or "").replace("\r\n", "\n").replace("\r", "\n")
    if not src:
        return ""

    stash = _stash if _stash is not None else []

    def store(html: str) -> str:
        token = f"@@TG{len(stash)}@@"
        stash.append(html)
        return token

    def restore(value: str) -> str:
        for i, item in enumerate(stash):
            value = value.replace(f"@@TG{i}@@", item)
        return value

    def repl_link(match: re.Match[str]) -> str:
        label = telegram_inline_from_markdownish(match.group(1), _stash=stash)
        url = escape(match.group(2).strip(), quote=True)
        return store(f'<a href="{url}">{label}</a>')

    src = re.sub(r"\[([^\]\n]+)\]\(((?:https?|tg)://[^\s)]+)\)", repl_link, src)
    src = escape(src)

    patterns = [
        (r"\*\*(.+?)\*\*", r"<b>\1</b>"),
        (r"__(.+?)__", r"<b>\1</b>"),
        (r"~~(.+?)~~", r"<s>\1</s>"),
        (r"\|\|(.+?)\|\|", r"<tg-spoiler>\1</tg-spoiler>"),
        (r"(?<!\*)\*(?!\s)(.+?)(?<!\s)\*(?!\*)", r"<i>\1</i>"),
        (r"(?<![\w_])_(?!\s)(.+?)(?<!\s)_(?![\w_])", r"<i>\1</i>"),
    ]
    for pattern, repl in patterns:
        src = re.sub(pattern, repl, src)

    return restore(src)


_TG_FENCE_RE = re.compile(r"```([A-Za-z0-9_+\-]*)[ \t]*\n(.*?)```", re.S)


def telegram_html_from_ai(text: str) -> str:
    text = (text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not text:
        return ""

    stash: list[str] = []

    def store(html: str) -> str:
        token = f"@@TG{len(stash)}@@"
        stash.append(html)
        return token

    def restore(value: str) -> str:
        for i, item in enumerate(stash):
            value = value.replace(f"@@TG{i}@@", item)
        return value

    def repl_codeblock(match: re.Match[str]) -> str:
        lang = (match.group(1) or "").strip()
        code = (match.group(2) or "").strip("\n")
        safe = escape(code)
        if lang and re.fullmatch(r"[A-Za-z0-9_+\-]{1,30}", lang):
            return store(f'<pre><code class="language-{lang}">{safe}</code></pre>')
        return store(f"<pre>{safe}</pre>")

    text = _TG_FENCE_RE.sub(repl_codeblock, text)
    text = re.sub(r"`([^`\n]+)`", lambda m: store(f"<code>{escape(m.group(1))}</code>"), text)

    out_lines: list[str] = []
    blockquote_lines: list[str] = []

    def flush_blockquote() -> None:
        nonlocal blockquote_lines
        if blockquote_lines:
            out_lines.append(f"<blockquote>{'\n'.join(blockquote_lines)}</blockquote>")
            blockquote_lines = []

    for raw_line in text.split("\n"):
        line = raw_line.rstrip()
        stripped = line.strip()
        if re.match(r"^\s*>", line):
            quote_line = re.sub(r"^\s*>+\s?", "", line)
            blockquote_lines.append(telegram_inline_from_markdownish(quote_line, _stash=stash))
            continue
        flush_blockquote()
        if not stripped:
            out_lines.append("")
            continue
        heading = re.match(r"^\s*#{1,6}\s+(.*)$", line)
        if heading:
            out_lines.append(f"<b>{telegram_inline_from_markdownish(heading.group(1).strip(), _stash=stash)}</b>")
            continue
        bullet = re.match(r"^\s*[-*+]\s+(.*)$", line)
        if bullet:
            out_lines.append(f"• {telegram_inline_from_markdownish(bullet.group(1).strip(), _stash=stash)}")
            continue
        ordered = re.match(r"^\s*(\d{1,3}[.)])\s+(.*)$", line)
        if ordered:
            marker = escape(ordered.group(1))
            body = telegram_inline_from_markdownish(ordered.group(2).strip(), _stash=stash)
            out_lines.append(f"{marker} {body}")
            continue
        out_lines.append(telegram_inline_from_markdownish(stripped, _stash=stash))

    flush_blockquote()
    html = "\n".join(out_lines)
    html = re.sub(r"\n{3,}", "\n\n", html).strip()
    return restore(html)


def telegram_plain_from_ai(text: str) -> str:
    html = telegram_html_from_ai(text)
    plain = re.sub(r"<blockquote(?: expandable)?>", "❝ ", html)
    plain = plain.replace("</blockquote>", "")
    plain = re.sub(r'<a href="[^"]+">(.*?)</a>', r"\1", plain)
    plain = re.sub(r"<tg-time[^>]*>(.*?)</tg-time>", r"\1", plain)
    plain = re.sub(r"<tg-emoji[^>]*>(.*?)</tg-emoji>", r"\1", plain)
    plain = re.sub(r"<code class=\"language-[^\"]+\">", "", plain)
    plain = re.sub(r"</?(?:b|strong|i|em|u|ins|s|strike|del|tg-spoiler|code|pre)>", "", plain)
    plain = re.sub(r"<[^>]+>", "", plain)
    plain = html_unescape(plain)
    plain = re.sub(r"\n{3,}", "\n\n", plain).strip()
    return plain


def key_hash(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:18]


def mask_key(key: str) -> str:
    if len(key) <= 12:
        return "***"
    return f"{key[:8]}…{key[-4:]}"


def redact_secrets(text: str) -> str:
    text = str(text or "")
    text = re.sub(r"sk-[A-Za-z0-9_\-]{10,}", lambda m: m.group(0)[:7] + "…" + m.group(0)[-4:], text)
    text = re.sub(r"bot\d+:[A-Za-z0-9_\-]+", "bot<token>", text)
    return text


def clean_openai_key(raw: str) -> str:
    """Extracts a clean ASCII OpenAI key from a TXT line.

    This makes the bot tolerant of lines like:
    - sk-proj-xxxx # válida
    - OPENAI_API_KEY=sk-proj-xxxx
    - 1) sk-proj-xxxx funciona

    Only the sk-... token is kept; comments or accented text are discarded.
    """
    if not raw:
        return ""
    line = unicodedata.normalize("NFKC", str(raw)).strip()
    for ch in ZERO_WIDTH_CHARS:
        line = line.replace(ch, "")
    if not line or line.startswith("#"):
        return ""
    if "=" in line and line.split("=", 1)[0].strip().upper().endswith("API_KEY"):
        line = line.split("=", 1)[1].strip().strip('"\'')
    match = OPENAI_KEY_RE.search(line)
    if not match:
        return ""
    key = match.group(1).strip()
    try:
        key.encode("ascii")
    except UnicodeEncodeError:
        return ""
    return key


def read_keys(path: Path) -> list[str]:
    if not path.exists():
        return []
    seen: set[str] = set()
    keys: list[str] = []
    for raw in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        key = clean_openai_key(raw)
        if key and key not in seen:
            seen.add(key)
            keys.append(key)
    return keys

def guess_mime(filename: str | None, default: str = "application/octet-stream") -> str:
    if not filename:
        return default
    return mimetypes.guess_type(filename)[0] or default


def data_url(data: bytes, mime: str) -> str:
    return f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"


def _font(size: int, bold: bool = False):
    if ImageFont is None:
        return None
    candidates = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf",
    ]
    for candidate in candidates:
        if os.path.exists(candidate):
            return ImageFont.truetype(candidate, size=size)
    return ImageFont.load_default()


# -----------------------------------------------------------------------------
# Portada y procesamiento de imágenes locales
# -----------------------------------------------------------------------------


def create_start_image(path: Path) -> None:
    """Crea una portada cuadrada 1080x1080 para Telegram, sin estirar imágenes."""
    if Image is None or ImageDraw is None:
        return

    width = height = 1080
    img = Image.new("RGB", (width, height), (255, 247, 251))
    pix = img.load()
    for y in range(height):
        for x in range(width):
            tx = x / max(1, width - 1)
            ty = y / max(1, height - 1)
            r = int(255 - 18 * ty + 4 * tx)
            g = int(247 - 18 * ty - 2 * tx)
            b = int(251 + 4 * tx)
            pix[x, y] = (max(0, min(255, r)), max(0, min(255, g)), max(0, min(255, b)))

    draw = ImageDraw.Draw(img, "RGBA")
    for cx, cy, rad, color in [
        (135, 160, 160, (255, 255, 255, 70)),
        (920, 140, 190, (255, 226, 244, 95)),
        (955, 900, 225, (237, 220, 255, 95)),
        (125, 920, 190, (255, 222, 239, 85)),
        (820, 545, 100, (255, 255, 255, 55)),
    ]:
        draw.ellipse((cx - rad, cy - rad, cx + rad, cy + rad), fill=color)

    if ImageFilter is not None:
        shadow = Image.new("RGBA", (width, height), (0, 0, 0, 0))
        sd = ImageDraw.Draw(shadow, "RGBA")
        sd.rounded_rectangle((93, 113, 987, 1003), radius=78, fill=(84, 40, 88, 26))
        shadow = shadow.filter(ImageFilter.GaussianBlur(22))
        img = Image.alpha_composite(img.convert("RGBA"), shadow)
        draw = ImageDraw.Draw(img, "RGBA")
    else:
        img = img.convert("RGBA")
        draw = ImageDraw.Draw(img, "RGBA")

    draw.rounded_rectangle((82, 88, 998, 978), radius=76, fill=(255, 255, 255, 230), outline=(255, 255, 255, 255), width=3)
    draw.rounded_rectangle((118, 124, 962, 940), radius=58, outline=(249, 188, 213, 140), width=3)

    title_font = _font(86, True)
    hello_font = _font(50, True)
    subtitle_font = _font(30)
    chip_font = _font(26, True)
    small_font = _font(23)

    draw.text((540, 158), "TataBot", font=title_font, fill=(88, 55, 108, 255), anchor="mm")
    draw.text((540, 232), "Hola, mi vida", font=hello_font, fill=(92, 59, 113, 255), anchor="mm")
    draw.text((540, 286), "tu espacio dulce, creativo y seguro", font=subtitle_font, fill=(126, 98, 136, 255), anchor="mm")

    # Avatar vectorial, creado desde cero para que nunca se deforme.
    draw.ellipse((410, 340, 670, 600), fill=(255, 223, 235, 255), outline=(255, 173, 207, 255), width=6)
    draw.pieslice((432, 318, 648, 575), 175, 360, fill=(109, 75, 126, 255))
    draw.ellipse((470, 390, 610, 565), fill=(255, 228, 211, 255))
    draw.pieslice((454, 350, 632, 535), 198, 345, fill=(109, 75, 126, 255))
    draw.ellipse((458, 462, 486, 502), fill=(255, 228, 211, 255))
    draw.ellipse((594, 462, 622, 502), fill=(255, 228, 211, 255))
    draw.arc((501, 454, 531, 482), 0, 180, fill=(72, 50, 85, 255), width=4)
    draw.arc((551, 454, 581, 482), 0, 180, fill=(72, 50, 85, 255), width=4)
    draw.arc((516, 512, 568, 545), 10, 170, fill=(214, 85, 134, 255), width=5)
    draw.ellipse((493, 492, 511, 510), fill=(255, 168, 186, 95))
    draw.ellipse((569, 492, 587, 510), fill=(255, 168, 186, 95))
    for angle in range(0, 360, 72):
        cx = 625 + int(25 * __import__("math").cos(__import__("math").radians(angle)))
        cy = 365 + int(25 * __import__("math").sin(__import__("math").radians(angle)))
        draw.ellipse((cx - 15, cy - 15, cx + 15, cy + 15), fill=(255, 132, 174, 235))
    draw.ellipse((613, 353, 637, 377), fill=(255, 229, 112, 255))

    draw.line((310, 620, 770, 620), fill=(245, 182, 209, 145), width=3)
    draw.ellipse((527, 607, 553, 633), fill=(255, 143, 184, 210))

    chips = ["Chat cálido", "Imágenes", "Búsqueda", "Voz suave", "Memoria", "Archivos"]
    x0, y0 = 210, 650
    cw, ch = 300, 62
    gapx, gapy = 60, 22
    for i, label in enumerate(chips):
        col, row = i % 2, i // 2
        x = x0 + col * (cw + gapx)
        y = y0 + row * (ch + gapy)
        draw.rounded_rectangle((x, y, x + cw, y + ch), radius=31, fill=(255, 240, 247, 255), outline=(248, 190, 214, 255), width=2)
        draw.text((x + cw / 2, y + ch / 2), label, font=chip_font, fill=(100, 70, 116, 255), anchor="mm")

    draw.text((540, 928), "Hecha con cariño para acompañarte bonito", font=small_font, fill=(120, 96, 136, 255), anchor="mm")
    draw.rounded_rectangle((42, 42, 1038, 1038), radius=82, outline=(255, 255, 255, 160), width=4)

    path.parent.mkdir(parents=True, exist_ok=True)
    img.convert("RGB").save(path, format="PNG", optimize=True)


def ensure_start_image() -> None:
    try:
        needs_create = not cfg.START_IMAGE.exists() or cfg.START_IMAGE.stat().st_size > 2_000_000
        if not needs_create and Image is not None:
            try:
                with Image.open(cfg.START_IMAGE) as im:
                    w, h = im.size
                    needs_create = (w != h) or w < 900 or h < 900
            except Exception:
                needs_create = True
        if needs_create:
            create_start_image(cfg.START_IMAGE)
    except Exception as exc:
        log.warning("No pude crear portada de inicio: %s", exc)


def image_to_jpeg(data: bytes, *, max_side: int = 1600, quality: int = 88, target_bytes: int = 3_500_000) -> bytes:
    if Image is None:
        return data
    try:
        with Image.open(io.BytesIO(data)) as im:
            im = im.convert("RGBA")
            bg = Image.new("RGBA", im.size, (255, 255, 255, 255))
            bg.alpha_composite(im)
            im = bg.convert("RGB")
            im.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
            result = data
            q = quality
            while q >= 58:
                out = io.BytesIO()
                im.save(out, format="JPEG", quality=q, optimize=True, progressive=True)
                result = out.getvalue()
                if len(result) <= target_bytes:
                    return result
                q -= 8
            return result
    except Exception:
        return data


def image_to_png_for_edit(data: bytes, *, max_side: int = 1800) -> bytes:
    if Image is None:
        return data
    try:
        with Image.open(io.BytesIO(data)) as im:
            im = im.convert("RGBA")
            im.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
            out = io.BytesIO()
            im.save(out, format="PNG", optimize=True)
            return out.getvalue()
    except Exception:
        return data


# -----------------------------------------------------------------------------
# Estado local: memoria, conversación y file_id de Telegram
# -----------------------------------------------------------------------------


class MemoryStore:
    def __init__(self, path: Path):
        self.path = path
        self._lock = asyncio.Lock()
        self.data: dict[str, list[str]] = safe_json_load(path, {})

    async def remember(self, chat_id: int, text: str) -> None:
        async with self._lock:
            key = str(chat_id)
            items = self.data.setdefault(key, [])
            items.append(truncate(text, 700))
            self.data[key] = items[-30:]
            safe_json_save(self.path, self.data)

    async def get(self, chat_id: int) -> list[str]:
        return list(self.data.get(str(chat_id), []))

    async def clear(self, chat_id: int) -> None:
        async with self._lock:
            self.data.pop(str(chat_id), None)
            safe_json_save(self.path, self.data)

    async def prompt_block(self, chat_id: int) -> str:
        items = await self.get(chat_id)
        if not items:
            return ""
        return "\n\nRecuerdos importantes de este chat:\n" + "\n".join(f"- {x}" for x in items[-20:])


class ConversationStore:
    def __init__(self, path: Path, max_items: int = 18):
        self.path = path
        self.max_items = max_items
        self._lock = asyncio.Lock()
        self.data: dict[str, list[dict[str, Any]]] = safe_json_load(path, {})

    async def add(self, chat_id: int, role: str, text: str) -> None:
        async with self._lock:
            key = str(chat_id)
            items = self.data.setdefault(key, [])
            items.append({"role": role, "text": truncate(text, 1000), "ts": int(now_ts())})
            self.data[key] = items[-self.max_items :]
            safe_json_save(self.path, self.data)

    async def clear(self, chat_id: int) -> None:
        async with self._lock:
            self.data.pop(str(chat_id), None)
            safe_json_save(self.path, self.data)

    async def prompt_block(self, chat_id: int) -> str:
        items = self.data.get(str(chat_id), [])[-10:]
        if not items:
            return ""
        lines = []
        for item in items:
            who = "Usuaria" if item.get("role") == "user" else "Tata"
            lines.append(f"{who}: {item.get('text', '')}")
        return "\n\nContexto reciente de conversación:\n" + "\n".join(lines)


class TelegramFileIdCache:
    def __init__(self, path: Path):
        self.path = path
        self._lock = asyncio.Lock()
        self.data: dict[str, str] = safe_json_load(path, {})

    async def get(self, key: str) -> str | None:
        return self.data.get(key)

    async def set(self, key: str, file_id: str) -> None:
        async with self._lock:
            self.data[key] = file_id
            safe_json_save(self.path, self.data)

    async def delete(self, key: str) -> None:
        async with self._lock:
            self.data.pop(key, None)
            safe_json_save(self.path, self.data)

# -----------------------------------------------------------------------------
# OpenAI: claves, errores y cliente
# -----------------------------------------------------------------------------


@dataclass
class KeyRecord:
    key: str
    label: str
    hid: str


class OpenAIAPIError(Exception):
    def __init__(self, status: int, message: str, *, code: str | None = None, typ: str | None = None):
        super().__init__(message)
        self.status = status
        self.message = message
        self.code = code or ""
        self.typ = typ or ""

    @property
    def invalid_key(self) -> bool:
        msg = self.message.lower()
        return self.status == 401 or self.code in {"invalid_api_key", "account_deactivated"} or "incorrect api key" in msg

    @property
    def quota(self) -> bool:
        msg = self.message.lower()
        return self.status == 429 and ("quota" in msg or self.code in {"insufficient_quota", "billing_hard_limit_reached"})

    @property
    def temporary(self) -> bool:
        msg = self.message.lower()
        return self.status in {408, 409, 425, 429, 500, 502, 503, 504} or "rate limit" in msg or "timeout" in msg

    @property
    def config_like(self) -> bool:
        msg = self.message.lower()
        needles = [
            "unsupported parameter",
            "unknown parameter",
            "invalid value",
            "invalid size",
            "invalid quality",
            "not supported",
            "does not support",
        ]
        return self.status == 400 and any(n in msg for n in needles)

    def friendly(self) -> str:
        base = f"HTTP {self.status}"
        if self.code:
            base += f" · {self.code}"
        return f"{base}: {truncate(redact_secrets(self.message), 220)}"


class OpenAIKeyManager:
    def __init__(self, txt_path: Path, state_path: Path):
        self.txt_path = txt_path
        self.state_path = state_path
        self.keys: list[KeyRecord] = []
        self.state: dict[str, Any] = safe_json_load(
            state_path,
            {"invalid": {}, "quota": {}, "cooldown": {}, "errors": []},
        )
        self._lock = asyncio.Lock()
        self._cursor = 0
        self.reload()

    def reload(self) -> None:
        raw_keys = read_keys(self.txt_path)
        self.keys = [KeyRecord(key=k, label=f"openai-{i+1}", hid=key_hash(k)) for i, k in enumerate(raw_keys)]

    def _entry_active(self, bucket: str, hid: str) -> bool:
        item = self.state.get(bucket, {}).get(hid)
        if not item:
            return False
        if bucket == "invalid":
            return True
        until = float(item.get("until", 0))
        if until > now_ts():
            return True
        self.state.get(bucket, {}).pop(hid, None)
        safe_json_save(self.state_path, self.state)
        return False

    def is_available(self, rec: KeyRecord) -> bool:
        return not (
            self._entry_active("invalid", rec.hid)
            or self._entry_active("quota", rec.hid)
            or self._entry_active("cooldown", rec.hid)
        )

    async def candidates(self, max_keys: int | None) -> list[KeyRecord]:
        async with self._lock:
            available = [rec for rec in self.keys if self.is_available(rec)]
            if not available:
                return []
            if max_keys is None or max_keys <= 0:
                max_keys = len(available)
            max_keys = min(max_keys, len(available))
            result: list[KeyRecord] = []
            for i in range(max_keys):
                idx = (self._cursor + i) % len(available)
                result.append(available[idx])
            self._cursor = (self._cursor + max_keys) % max(1, len(available))
            return result

    def _save(self) -> None:
        safe_json_save(self.state_path, self.state)

    async def mark_invalid(self, rec: KeyRecord, reason: str) -> None:
        async with self._lock:
            self.state.setdefault("invalid", {})[rec.hid] = {
                "label": rec.label,
                "reason": truncate(reason, 500),
                "at": utc_stamp(),
            }
            self.add_error_unlocked(rec, "invalid", reason)
            self._save()

    async def mark_quota(self, rec: KeyRecord, reason: str) -> None:
        async with self._lock:
            until = now_ts() + cfg.KEY_QUOTA_COOLDOWN_HOURS * 3600
            self.state.setdefault("quota", {})[rec.hid] = {
                "label": rec.label,
                "reason": truncate(reason, 500),
                "until": until,
                "at": utc_stamp(),
            }
            self.add_error_unlocked(rec, "quota", reason)
            self._save()

    async def mark_temp(self, rec: KeyRecord, reason: str) -> None:
        async with self._lock:
            until = now_ts() + cfg.KEY_TEMP_COOLDOWN_SECONDS
            self.state.setdefault("cooldown", {})[rec.hid] = {
                "label": rec.label,
                "reason": truncate(reason, 500),
                "until": until,
                "at": utc_stamp(),
            }
            self.add_error_unlocked(rec, "cooldown", reason)
            self._save()

    def add_error_unlocked(self, rec: KeyRecord, category: str, reason: str) -> None:
        errors = self.state.setdefault("errors", [])
        errors.append({
            "at": utc_stamp(),
            "key": rec.label,
            "category": category,
            "reason": truncate(reason, 700),
        })
        self.state["errors"] = errors[-80:]

    async def record_soft_error(self, rec: KeyRecord, category: str, reason: str) -> None:
        async with self._lock:
            self.add_error_unlocked(rec, category, reason)
            self._save()

    async def reset(self) -> None:
        async with self._lock:
            self.state = {"invalid": {}, "quota": {}, "cooldown": {}, "errors": []}
            self._cursor = 0
            self.reload()
            self._save()

    def stats(self) -> dict[str, int]:
        invalid = sum(1 for rec in self.keys if self._entry_active("invalid", rec.hid))
        quota = sum(1 for rec in self.keys if self._entry_active("quota", rec.hid))
        cooldown = sum(1 for rec in self.keys if self._entry_active("cooldown", rec.hid))
        available = sum(1 for rec in self.keys if self.is_available(rec))
        return {
            "total": len(self.keys),
            "available": available,
            "invalid": invalid,
            "quota": quota,
            "cooldown": cooldown,
        }

    def last_errors(self, n: int = 12) -> list[dict[str, Any]]:
        return list(self.state.get("errors", [])[-n:])


@dataclass
class GeneratedImage:
    data: bytes
    model: str
    quality: str
    size: str
    key_label: str


@dataclass
class GeneratedAudio:
    data: bytes
    model: str
    voice: str
    key_label: str


class OpenAIClient:
    def __init__(self, key_manager: OpenAIKeyManager):
        self.keys = key_manager
        timeout = httpx.Timeout(
            connect=cfg.OPENAI_CONNECT_TIMEOUT,
            read=cfg.OPENAI_READ_TIMEOUT,
            write=cfg.OPENAI_WRITE_TIMEOUT,
            pool=cfg.OPENAI_POOL_TIMEOUT,
        )
        self.http = httpx.AsyncClient(timeout=timeout, trust_env=False)

    async def close(self) -> None:
        await self.http.aclose()

    def _headers(self, rec: KeyRecord) -> dict[str, str]:
        # Header values must be ASCII. rec.key is sanitized when loaded, but
        # this check gives a clearer local error if the TXT was edited while running.
        key = clean_openai_key(rec.key)
        if not key or key != rec.key:
            raise UnicodeEncodeError("ascii", rec.key, 0, len(rec.key), "API key contains non-ASCII or extra text")
        return {"Authorization": f"Bearer {key}", "Accept": "application/json"}

    async def _raise_for_openai(self, resp: httpx.Response) -> None:
        if resp.status_code < 400:
            return
        message = resp.text
        code = typ = None
        try:
            data = resp.json()
            err = data.get("error") or data
            message = err.get("message") or message
            code = err.get("code")
            typ = err.get("type")
        except Exception:
            pass
        raise OpenAIAPIError(resp.status_code, str(message), code=str(code or ""), typ=str(typ or ""))

    def _json_body(self, payload: dict[str, Any]) -> bytes:
        # Explicit UTF-8 body. This prevents locale/default-encoding surprises
        # when prompts contain Spanish accents, emojis or ñ.
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")

    async def _post_json(self, rec: KeyRecord, endpoint: str, payload: dict[str, Any]) -> dict[str, Any]:
        resp = await self.http.post(
            OPENAI_BASE + endpoint,
            headers={**self._headers(rec), "Content-Type": "application/json; charset=utf-8"},
            content=self._json_body(payload),
        )
        await self._raise_for_openai(resp)
        return resp.json()

    async def _post_bytes(self, rec: KeyRecord, endpoint: str, payload: dict[str, Any]) -> bytes:
        resp = await self.http.post(
            OPENAI_BASE + endpoint,
            headers={**self._headers(rec), "Content-Type": "application/json; charset=utf-8"},
            content=self._json_body(payload),
        )
        await self._raise_for_openai(resp)
        return resp.content

    async def _post_multipart(
        self,
        rec: KeyRecord,
        endpoint: str,
        *,
        data: dict[str, str],
        files: dict[str, tuple[str, bytes, str]],
        expect_json: bool = True,
    ) -> Any:
        resp = await self.http.post(
            OPENAI_BASE + endpoint,
            headers=self._headers(rec),
            data=data,
            files=files,
        )
        await self._raise_for_openai(resp)
        return resp.json() if expect_json else resp.content

    async def _call_with_keys(
        self,
        operation: Callable[[KeyRecord], Awaitable[Any]],
        *,
        max_keys: int,
        label: str,
        stop_on_config_error: bool = False,
    ) -> tuple[Any, KeyRecord]:
        tried: list[str] = []
        candidates = await self.keys.candidates(max_keys)
        if not candidates:
            raise RuntimeError("No hay claves OpenAI disponibles. Usa /estado o /reset_claves.")
        last_exc: Exception | None = None
        for rec in candidates:
            try:
                result = await operation(rec)
                return result, rec
            except OpenAIAPIError as exc:
                last_exc = exc
                tried.append(f"{rec.label}: {exc.friendly()}")
                if exc.invalid_key:
                    await self.keys.mark_invalid(rec, exc.friendly())
                    continue
                if exc.quota:
                    await self.keys.mark_quota(rec, exc.friendly())
                    continue
                if exc.temporary:
                    await self.keys.mark_temp(rec, exc.friendly())
                    continue
                await self.keys.record_soft_error(rec, label, exc.friendly())
                if stop_on_config_error and exc.config_like:
                    break
                # En errores 400/404 puede ser modelo no disponible para esa key; probamos otra.
                continue
            except UnicodeEncodeError as exc:
                # Most commonly caused by a TXT line where the key has comments or
                # non-ASCII text attached. Mark only this local key as invalid.
                last_exc = exc
                reason = "API key mal formada en el TXT: contiene texto extra o caracteres no ASCII."
                tried.append(f"{rec.label}: {reason}")
                await self.keys.mark_invalid(rec, reason)
                continue
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                last_exc = exc
                tried.append(f"{rec.label}: timeout/red")
                await self.keys.mark_temp(rec, str(exc))
                continue
        detail = " | ".join(tried[-6:])
        raise RuntimeError(f"No pude completar {label} con las claves disponibles. {detail}") from last_exc

    def _extract_text(self, data: dict[str, Any]) -> str:
        if isinstance(data.get("output_text"), str) and data["output_text"].strip():
            return data["output_text"].strip()
        texts: list[str] = []
        for item in data.get("output", []) or []:
            if item.get("type") == "message":
                for content in item.get("content", []) or []:
                    if isinstance(content, dict):
                        if isinstance(content.get("text"), str):
                            texts.append(content["text"])
                        elif isinstance(content.get("output_text"), str):
                            texts.append(content["output_text"])
                        elif content.get("type") == "output_text" and isinstance(content.get("text"), str):
                            texts.append(content["text"])
        return "\n".join(t.strip() for t in texts if t and t.strip()).strip() or "No recibí texto en la respuesta."

    def _base_response_payload(
        self,
        *,
        model: str,
        prompt: str,
        chat_id: int,
        memory_block: str = "",
        context_block: str = "",
        search: bool = False,
        extra_content: list[dict[str, Any]] | None = None,
        web_tool_type: str | None = None,
    ) -> dict[str, Any]:
        instructions = cfg.SYSTEM_PROMPT + memory_block + context_block
        content: list[dict[str, Any]] = [{"type": "input_text", "text": prompt}]
        if extra_content:
            content.extend(extra_content)
        payload: dict[str, Any] = {
            "model": model,
            "instructions": instructions,
            "input": [{"role": "user", "content": content}],
            "max_output_tokens": cfg.MAX_OUTPUT_TOKENS,
        }
        if search and web_tool_type:
            payload["tools"] = [{"type": web_tool_type}]
            payload["tool_choice"] = "auto"
        return payload

    async def response_text(
        self,
        *,
        prompt: str,
        chat_id: int,
        memory_block: str = "",
        context_block: str = "",
        search: bool = False,
        extra_content: list[dict[str, Any]] | None = None,
    ) -> str:
        tool_options = ["web_search", "web_search_preview", None] if search else [None]
        errors: list[str] = []
        for model in cfg.TEXT_MODEL_PRIORITY:
            for tool_type in tool_options:
                async def op(rec: KeyRecord, model=model, tool_type=tool_type):
                    payload = self._base_response_payload(
                        model=model,
                        prompt=prompt,
                        chat_id=chat_id,
                        memory_block=memory_block,
                        context_block=context_block,
                        search=search,
                        extra_content=extra_content,
                        web_tool_type=tool_type,
                    )
                    return await self._post_json(rec, "/responses", payload)

                try:
                    data, rec = await self._call_with_keys(
                        op,
                        max_keys=cfg.MAX_KEYS_PER_TEXT_OPERATION,
                        label=f"texto:{model}",
                    )
                    return self._extract_text(data)
                except Exception as exc:
                    errors.append(f"{model}/{tool_type or 'sin_tool'}: {truncate(str(exc), 250)}")
                    continue
        raise RuntimeError("No pude obtener respuesta de OpenAI. " + " | ".join(errors[-4:]))

    async def analyze_image(self, *, prompt: str, image_data: bytes, chat_id: int, memory_block: str, context_block: str) -> str:
        jpeg = image_to_jpeg(image_data, max_side=1800, quality=90, target_bytes=4_500_000)
        extra = [{"type": "input_image", "image_url": data_url(jpeg, "image/jpeg"), "detail": "high"}]
        return await self.response_text(
            prompt=prompt,
            chat_id=chat_id,
            memory_block=memory_block,
            context_block=context_block,
            extra_content=extra,
        )

    async def analyze_file(self, *, prompt: str, file_data: bytes, filename: str, mime: str, chat_id: int, memory_block: str, context_block: str) -> str:
        if mime.startswith("image/"):
            return await self.analyze_image(
                prompt=prompt,
                image_data=file_data,
                chat_id=chat_id,
                memory_block=memory_block,
                context_block=context_block,
            )
        extra = [{"type": "input_file", "filename": filename, "file_data": data_url(file_data, mime)}]
        return await self.response_text(
            prompt=prompt,
            chat_id=chat_id,
            memory_block=memory_block,
            context_block=context_block,
            extra_content=extra,
        )

    def _image_profiles(self) -> list[dict[str, str]]:
        profiles: list[dict[str, str]] = []
        for model in cfg.IMAGE_MODEL_PRIORITY:
            qualities = cfg.IMAGE_QUALITY_PRIORITY
            if model.endswith("mini"):
                qualities = [q for q in qualities if q != "high"] + ["medium"]
            for quality in qualities:
                for size in cfg.IMAGE_SIZE_PRIORITY:
                    item = {"model": model, "quality": quality, "size": size}
                    if item not in profiles:
                        profiles.append(item)
        return profiles

    def _image_payload_variants(self, profile: dict[str, str], prompt: str) -> list[dict[str, Any]]:
        base = {
            "model": profile["model"],
            "prompt": prompt,
            "n": 1,
            "size": profile["size"],
            "quality": profile["quality"],
            "output_format": cfg.IMAGE_OUTPUT_FORMAT,
            "output_compression": cfg.IMAGE_OUTPUT_COMPRESSION,
        }
        variants = [base]
        no_compression = dict(base)
        no_compression.pop("output_compression", None)
        variants.append(no_compression)
        no_format = dict(no_compression)
        no_format.pop("output_format", None)
        variants.append(no_format)
        bare = {"model": profile["model"], "prompt": prompt, "n": 1, "size": profile["size"]}
        if profile.get("quality"):
            bare["quality"] = profile["quality"]
        variants.append(bare)
        safest = {"model": profile["model"], "prompt": prompt, "n": 1}
        variants.append(safest)
        unique: list[dict[str, Any]] = []
        seen: set[str] = set()
        for v in variants:
            sig = json.dumps(v, sort_keys=True)
            if sig not in seen:
                seen.add(sig)
                unique.append(v)
        return unique

    def _extract_image_bytes(self, data: dict[str, Any]) -> bytes:
        items = data.get("data") or []
        if not items:
            raise RuntimeError("OpenAI no devolvió imagen.")
        item = items[0]
        if item.get("b64_json"):
            return base64.b64decode(item["b64_json"])
        if item.get("url"):
            # Algunos modelos/formatos antiguos pueden devolver URL temporal.
            return b"__URL__" + item["url"].encode("utf-8")
        raise RuntimeError("OpenAI devolvió una imagen sin b64_json ni URL.")

    async def generate_image(self, prompt: str) -> GeneratedImage:
        enhanced_prompt = (
            "Crea una imagen de alta calidad, estética, detallada y profesional. "
            "Cuida composición, iluminación, paleta de color, profundidad, nitidez y detalles finos. "
            "Evita texto ilegible, marcas de agua, deformaciones y manos/anatomía extraña.\n\n"
            f"Solicitud del usuario: {prompt}"
        )
        last_errors: list[str] = []
        for profile in self._image_profiles():
            for payload in self._image_payload_variants(profile, enhanced_prompt):
                candidates = await self.keys.candidates(cfg.MAX_IMAGE_KEYS_PER_PROFILE)
                if not candidates:
                    raise RuntimeError("No hay claves OpenAI disponibles para generar imágenes.")
                profile_config_failed = False
                for rec in candidates:
                    try:
                        data = await self._post_json(rec, "/images/generations", payload)
                        img_data = self._extract_image_bytes(data)
                        if img_data.startswith(b"__URL__"):
                            url = img_data[7:].decode("utf-8")
                            r = await self.http.get(url)
                            r.raise_for_status()
                            img_data = r.content
                        return GeneratedImage(
                            data=img_data,
                            model=profile["model"],
                            quality=profile["quality"],
                            size=profile["size"],
                            key_label=rec.label,
                        )
                    except OpenAIAPIError as exc:
                        last_errors.append(f"{profile['model']}/{profile['quality']}/{profile['size']} {rec.label}: {exc.friendly()}")
                        if exc.invalid_key:
                            await self.keys.mark_invalid(rec, exc.friendly())
                        elif exc.quota:
                            await self.keys.mark_quota(rec, exc.friendly())
                        elif exc.temporary:
                            await self.keys.mark_temp(rec, exc.friendly())
                        else:
                            await self.keys.record_soft_error(rec, "imagen", exc.friendly())
                            if exc.config_like:
                                profile_config_failed = True
                                break
                    except UnicodeEncodeError:
                        reason = "API key mal formada en el TXT: contiene texto extra o caracteres no ASCII."
                        last_errors.append(f"{profile['model']} {rec.label}: {reason}")
                        await self.keys.mark_invalid(rec, reason)
                    except (httpx.TimeoutException, httpx.NetworkError) as exc:
                        last_errors.append(f"{profile['model']} {rec.label}: timeout/red")
                        await self.keys.mark_temp(rec, str(exc))
                if profile_config_failed:
                    break
        raise RuntimeError("No pude generar imagen. " + " | ".join(last_errors[-5:]))

    async def edit_image(self, *, prompt: str, image_data: bytes) -> GeneratedImage:
        png = image_to_png_for_edit(image_data)
        enhanced = (
            "Edita la imagen con resultado profesional, natural y bonito. "
            "Mantén coherencia visual, iluminación y detalles limpios.\n\n"
            f"Edición solicitada: {prompt}"
        )
        last_errors: list[str] = []
        for profile in self._image_profiles():
            candidates = await self.keys.candidates(cfg.MAX_IMAGE_KEYS_PER_PROFILE)
            if not candidates:
                raise RuntimeError("No hay claves OpenAI disponibles para editar imágenes.")
            for rec in candidates:
                try:
                    form = {
                        "model": profile["model"],
                        "prompt": enhanced,
                        "size": profile["size"],
                        "quality": profile["quality"],
                    }
                    files = {"image": ("tata_edit.png", png, "image/png")}
                    data = await self._post_multipart(rec, "/images/edits", data=form, files=files, expect_json=True)
                    img_data = self._extract_image_bytes(data)
                    return GeneratedImage(img_data, profile["model"], profile["quality"], profile["size"], rec.label)
                except OpenAIAPIError as exc:
                    last_errors.append(f"{profile['model']} {rec.label}: {exc.friendly()}")
                    if exc.invalid_key:
                        await self.keys.mark_invalid(rec, exc.friendly())
                    elif exc.quota:
                        await self.keys.mark_quota(rec, exc.friendly())
                    elif exc.temporary:
                        await self.keys.mark_temp(rec, exc.friendly())
                    else:
                        await self.keys.record_soft_error(rec, "editar_imagen", exc.friendly())
                        if exc.config_like:
                            break
                except UnicodeEncodeError:
                    reason = "API key mal formada en el TXT: contiene texto extra o caracteres no ASCII."
                    last_errors.append(f"{profile['model']} {rec.label}: {reason}")
                    await self.keys.mark_invalid(rec, reason)
                except (httpx.TimeoutException, httpx.NetworkError) as exc:
                    last_errors.append(f"{profile['model']} {rec.label}: timeout/red")
                    await self.keys.mark_temp(rec, str(exc))
        raise RuntimeError("No pude editar imagen. " + " | ".join(last_errors[-5:]))

    async def tts(self, text: str) -> GeneratedAudio:
        text = truncate(text.strip(), 3500)
        voices = list(cfg.TTS_FEMALE_VOICES)
        random.shuffle(voices)
        errors: list[str] = []
        for model in cfg.TTS_MODEL_PRIORITY:
            for voice in voices:
                async def op(rec: KeyRecord, model=model, voice=voice):
                    payload: dict[str, Any] = {
                        "model": model,
                        "voice": voice,
                        "input": text,
                        "response_format": "mp3",
                    }
                    if "gpt-4o" in model:
                        payload["instructions"] = (
                            "Habla en español latino con voz femenina, cálida, suave, elegante, "
                            "cercana y profesional. Ritmo tranquilo, sonrisa sutil y tono afectuoso."
                        )
                    return await self._post_bytes(rec, "/audio/speech", payload)

                try:
                    data, rec = await self._call_with_keys(
                        op,
                        max_keys=cfg.MAX_KEYS_PER_AUDIO_OPERATION,
                        label=f"tts:{model}:{voice}",
                        stop_on_config_error=True,
                    )
                    return GeneratedAudio(data=data, model=model, voice=voice, key_label=rec.label)
                except Exception as exc:
                    errors.append(f"{model}/{voice}: {truncate(str(exc), 180)}")
                    continue
        raise RuntimeError("No pude crear voz. " + " | ".join(errors[-5:]))

    async def transcribe(self, audio_data: bytes, filename: str, mime: str) -> str:
        errors: list[str] = []
        for model in cfg.TRANSCRIPTION_MODEL_PRIORITY:
            async def op(rec: KeyRecord, model=model):
                data = {"model": model}
                files = {"file": (filename, audio_data, mime)}
                return await self._post_multipart(rec, "/audio/transcriptions", data=data, files=files, expect_json=True)

            try:
                result, rec = await self._call_with_keys(
                    op,
                    max_keys=cfg.MAX_KEYS_PER_AUDIO_OPERATION,
                    label=f"transcripcion:{model}",
                    stop_on_config_error=True,
                )
                return (result.get("text") or "").strip() or "No logré transcribir el audio."
            except Exception as exc:
                errors.append(f"{model}: {truncate(str(exc), 180)}")
                continue
        raise RuntimeError("No pude transcribir audio. " + " | ".join(errors[-4:]))

# -----------------------------------------------------------------------------
# Envío seguro a Telegram
# -----------------------------------------------------------------------------


async def retry_telegram(operation: Callable[[], Awaitable[Any]], *, attempts: int = 3, label: str = "telegram") -> Any:
    last_exc: Exception | None = None
    for i in range(attempts):
        try:
            return await operation()
        except RetryAfter as exc:
            last_exc = exc
            await asyncio.sleep(float(getattr(exc, "retry_after", 2)) + 0.5)
        except (TimedOut, NetworkError, httpx.TimeoutException) as exc:
            last_exc = exc
            await asyncio.sleep(1.5 + i * 1.5)
        except TelegramError as exc:
            last_exc = exc
            if i >= 1:
                break
            await asyncio.sleep(1)
    raise last_exc or RuntimeError(f"Fallo desconocido enviando {label}")


async def send_long_text(message, text: str, *, parse_mode: str | None = None) -> None:
    for chunk in split_text(text):
        await retry_telegram(
            lambda chunk=chunk: message.reply_text(
                chunk,
                parse_mode=parse_mode,
                disable_web_page_preview=True,
                read_timeout=cfg.TELEGRAM_READ_TIMEOUT,
                write_timeout=cfg.TELEGRAM_WRITE_TIMEOUT,
                connect_timeout=cfg.TELEGRAM_CONNECT_TIMEOUT,
                pool_timeout=cfg.TELEGRAM_POOL_TIMEOUT,
            ),
            label="texto",
        )


async def send_ai_text(message, text: str) -> None:
    """Envía texto del modelo con formato bonito compatible con Telegram.

    Preferimos HTML porque Telegram lo soporta oficialmente y es más estable que
    MarkdownV2 cuando el contenido del modelo trae muchos símbolos especiales.
    Si Telegram rechaza el HTML, hacemos fallback a texto plano limpio.
    """
    raw = (text or "").strip()
    if not raw:
        return
    for chunk in split_text(raw, limit=2800):
        html_chunk = telegram_html_from_ai(chunk)
        try:
            await retry_telegram(
                lambda chunk=html_chunk: message.reply_text(
                    chunk,
                    parse_mode=ParseMode.HTML,
                    disable_web_page_preview=True,
                    read_timeout=cfg.TELEGRAM_READ_TIMEOUT,
                    write_timeout=cfg.TELEGRAM_WRITE_TIMEOUT,
                    connect_timeout=cfg.TELEGRAM_CONNECT_TIMEOUT,
                    pool_timeout=cfg.TELEGRAM_POOL_TIMEOUT,
                ),
                label="texto-ai-html",
            )
        except Exception as exc:
            log.warning("Fallback a texto plano para respuesta de IA: %s", exc)
            plain_chunk = telegram_plain_from_ai(chunk) or chunk
            await retry_telegram(
                lambda chunk=plain_chunk: message.reply_text(
                    chunk,
                    disable_web_page_preview=True,
                    read_timeout=cfg.TELEGRAM_READ_TIMEOUT,
                    write_timeout=cfg.TELEGRAM_WRITE_TIMEOUT,
                    connect_timeout=cfg.TELEGRAM_CONNECT_TIMEOUT,
                    pool_timeout=cfg.TELEGRAM_POOL_TIMEOUT,
                ),
                label="texto-ai-plain",
            )


async def safe_send_photo(message, image_data: bytes, *, caption: str = "", filename: str = "tata.jpg", reply_markup=None):
    preview = image_to_jpeg(image_data, max_side=1600, quality=88, target_bytes=3_500_000)

    async def send_photo_once(data: bytes):
        bio = io.BytesIO(data)
        bio.name = filename
        return await message.reply_photo(
            photo=InputFile(bio, filename=filename),
            caption=caption,
            parse_mode=ParseMode.HTML if caption else None,
            reply_markup=reply_markup,
            read_timeout=cfg.TELEGRAM_READ_TIMEOUT,
            write_timeout=cfg.TELEGRAM_WRITE_TIMEOUT,
            connect_timeout=cfg.TELEGRAM_CONNECT_TIMEOUT,
            pool_timeout=cfg.TELEGRAM_POOL_TIMEOUT,
        )

    try:
        return await retry_telegram(lambda: send_photo_once(preview), attempts=3, label="foto")
    except Exception as exc:
        log.warning("Primer envío de foto falló, intentaré preview más pequeño: %s", exc)

    tiny = image_to_jpeg(image_data, max_side=1024, quality=76, target_bytes=1_500_000)
    try:
        return await retry_telegram(lambda: send_photo_once(tiny), attempts=2, label="foto-mini")
    except Exception as exc:
        log.warning("No pude enviar como foto; usaré documento: %s", exc)

    async def send_document_once():
        bio = io.BytesIO(tiny)
        bio.name = filename
        return await message.reply_document(
            document=InputFile(bio, filename=filename),
            caption=caption,
            parse_mode=ParseMode.HTML if caption else None,
            reply_markup=reply_markup,
            read_timeout=cfg.TELEGRAM_READ_TIMEOUT,
            write_timeout=cfg.TELEGRAM_WRITE_TIMEOUT,
            connect_timeout=cfg.TELEGRAM_CONNECT_TIMEOUT,
            pool_timeout=cfg.TELEGRAM_POOL_TIMEOUT,
        )

    return await retry_telegram(send_document_once, attempts=2, label="documento-imagen")


async def safe_send_document(message, data: bytes, *, filename: str, caption: str = "") -> None:
    async def send_once():
        bio = io.BytesIO(data)
        bio.name = filename
        return await message.reply_document(
            document=InputFile(bio, filename=filename),
            caption=caption,
            parse_mode=ParseMode.HTML if caption else None,
            read_timeout=cfg.TELEGRAM_READ_TIMEOUT,
            write_timeout=cfg.TELEGRAM_WRITE_TIMEOUT,
            connect_timeout=cfg.TELEGRAM_CONNECT_TIMEOUT,
            pool_timeout=cfg.TELEGRAM_POOL_TIMEOUT,
        )

    await retry_telegram(send_once, attempts=2, label="documento")


async def safe_send_audio(message, audio: GeneratedAudio, *, title: str = "Voz de Tata") -> None:
    caption = (
        "🎧 <b>Voz de Tata</b>\n"
        f"Voz generada por IA · timbre: <code>{escape(audio.voice)}</code> · modelo: <code>{escape(audio.model)}</code>"
    )

    async def send_once():
        bio = io.BytesIO(audio.data)
        bio.name = "tata_voz.mp3"
        return await message.reply_audio(
            audio=InputFile(bio, filename="tata_voz.mp3"),
            caption=caption,
            title=title,
            performer="Tata",
            parse_mode=ParseMode.HTML,
            read_timeout=cfg.TELEGRAM_READ_TIMEOUT,
            write_timeout=cfg.TELEGRAM_WRITE_TIMEOUT,
            connect_timeout=cfg.TELEGRAM_CONNECT_TIMEOUT,
            pool_timeout=cfg.TELEGRAM_POOL_TIMEOUT,
        )

    await retry_telegram(send_once, attempts=3, label="audio")


PROCESSING_TEXTS = [
    "🌷 Dame un momentito, mi amor… estoy preparando algo bonito para ti.",
    "✨ Ya estoy trabajando en eso con mucho cariño… no me tardo.",
    "💗 Estoy procesando tu petición despacito y con cuidado.",
    "🫶 Un segundito, estoy acomodando todo para darte una respuesta linda.",
    "🎀 Ya te estoy atendiendo, amor. Estoy creando la mejor respuesta posible.",
]


class ProcessingIndicator:
    def __init__(self, update: Update, context: ContextTypes.DEFAULT_TYPE, *, text: str | None = None, action: str = ChatAction.TYPING):
        self.update = update
        self.context = context
        self.text = text or random.choice(PROCESSING_TEXTS)
        self.action = action
        self.message = None
        self.task: asyncio.Task | None = None
        self._closed = False

    async def __aenter__(self):
        msg = self.update.effective_message
        chat = self.update.effective_chat
        if msg:
            try:
                self.message = await retry_telegram(
                    lambda: msg.reply_text(self.text),
                    attempts=2,
                    label="mensaje-procesando",
                )
            except Exception as exc:
                log.warning("No pude enviar indicador de procesamiento: %s", exc)
        if chat:
            self.task = asyncio.create_task(self._loop(chat.id))
        return self

    async def _loop(self, chat_id: int) -> None:
        while not self._closed:
            try:
                await self.context.bot.send_chat_action(
                    chat_id=chat_id,
                    action=self.action,
                    read_timeout=cfg.TELEGRAM_READ_TIMEOUT,
                    write_timeout=cfg.TELEGRAM_WRITE_TIMEOUT,
                    connect_timeout=cfg.TELEGRAM_CONNECT_TIMEOUT,
                    pool_timeout=cfg.TELEGRAM_POOL_TIMEOUT,
                )
            except Exception:
                pass
            await asyncio.sleep(4)

    async def __aexit__(self, exc_type, exc, tb) -> None:
        self._closed = True
        if self.task:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
            except Exception:
                pass
        if self.message:
            try:
                await self.message.delete(
                    read_timeout=cfg.TELEGRAM_READ_TIMEOUT,
                    write_timeout=cfg.TELEGRAM_WRITE_TIMEOUT,
                    connect_timeout=cfg.TELEGRAM_CONNECT_TIMEOUT,
                    pool_timeout=cfg.TELEGRAM_POOL_TIMEOUT,
                )
            except Exception:
                try:
                    await self.message.edit_text("Listo, amorcito ✨")
                except Exception:
                    pass


# -----------------------------------------------------------------------------
# Núcleo de TataBot
# -----------------------------------------------------------------------------


class TataBot:
    def __init__(self):
        cfg.DATA_DIR.mkdir(parents=True, exist_ok=True)
        ensure_start_image()
        self.key_manager = OpenAIKeyManager(cfg.OPENAI_TXT, cfg.DATA_DIR / "estado_claves.json")
        self.openai = OpenAIClient(self.key_manager)
        self.memory = MemoryStore(cfg.DATA_DIR / "memoria.json")
        self.conversation = ConversationStore(cfg.DATA_DIR / "conversacion.json")
        self.file_ids = TelegramFileIdCache(cfg.DATA_DIR / "telegram_file_ids.json")
        self.semaphore = asyncio.Semaphore(cfg.MAX_CONCURRENCIA)

    async def shutdown(self) -> None:
        await self.openai.close()

    async def prompt_context(self, chat_id: int) -> tuple[str, str]:
        memory_block = await self.memory.prompt_block(chat_id)
        context_block = await self.conversation.prompt_block(chat_id)
        return memory_block, context_block


def get_tata() -> TataBot:
    if TATA is None:
        raise RuntimeError("Tata todavía no está inicializada.")
    return TATA


async def reject_if_not_allowed(update: Update) -> bool:
    if not cfg.USUARIOS_PERMITIDOS:
        return False
    user = update.effective_user
    if user and user.id in set(cfg.USUARIOS_PERMITIDOS):
        return False
    if update.effective_message:
        await update.effective_message.reply_text("🌷 Este bot es privado y solo está disponible para sus personas autorizadas.")
    return True


async def guarded(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    action: Callable[[], Awaitable[Any]],
    *,
    processing_text: str | None = None,
    chat_action: str = ChatAction.TYPING,
) -> Any:
    if await reject_if_not_allowed(update):
        return None
    tata = get_tata()
    async with tata.semaphore:
        try:
            async with ProcessingIndicator(update, context, text=processing_text, action=chat_action):
                return await action()
        except Exception as exc:
            log.exception("Error inesperado")
            msg = update.effective_message
            if msg:
                await send_long_text(
                    msg,
                    "🌧️ Amor, tuve un problema procesando eso.\n\n"
                    f"<b>Detalle técnico:</b> <code>{escape(truncate(redact_secrets(str(exc)), 900))}</code>\n\n"
                    "Puedes intentar de nuevo en unos segundos o revisar <code>/estado</code>.",
                    parse_mode=ParseMode.HTML,
                )
            return None


async def download_telegram_file(context: ContextTypes.DEFAULT_TYPE, file_id: str) -> bytes:
    tg_file = await context.bot.get_file(file_id)
    data = await tg_file.download_as_bytearray(
        read_timeout=cfg.TELEGRAM_READ_TIMEOUT,
        write_timeout=cfg.TELEGRAM_WRITE_TIMEOUT,
        connect_timeout=cfg.TELEGRAM_CONNECT_TIMEOUT,
        pool_timeout=cfg.TELEGRAM_POOL_TIMEOUT,
    )
    return bytes(data)

# -----------------------------------------------------------------------------
# Menús
# -----------------------------------------------------------------------------


def main_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("💬 Hablar con Tata", callback_data="help_chat"), InlineKeyboardButton("🎨 Crear imagen", callback_data="help_image")],
            [InlineKeyboardButton("🎙️ Voz bonita", callback_data="help_voice"), InlineKeyboardButton("📎 Archivos", callback_data="help_files")],
            [InlineKeyboardButton("🧠 Memoria", callback_data="help_memory"), InlineKeyboardButton("🌷 Estado", callback_data="status")],
        ]
    )


START_CAPTION = """
<b>Hola, soy Tata 🌷</b>

Tu compañera IA: dulce, creativa y lista para ayudarte con palabras, imágenes, voz, ideas, emociones y archivos.

<b>Menú rápido</b>
💬 Escríbeme normal y te respondo.
🎨 <code>/imagen</code> una idea bonita
🎙️ <code>/voz</code> texto para escucharlo
🌐 <code>/buscar</code> algo actual
🧠 <code>/recordar</code> algo importante
📎 Envíame fotos, audios, PDFs o documentos.

Estoy hecha para acompañarte con cariño, no para reemplazar ayuda profesional en emergencias.
""".strip()


HELP_TEXT = """
🌷 <b>Guía de TataBot</b>

<b>Hablar conmigo</b>
Solo escribe tu mensaje. Puedo ayudarte a pensar, organizar ideas, estudiar, escribir, crear planes, responder con ternura o acompañarte emocionalmente.

<b>Comandos</b>
<code>/chat</code> mensaje — conversación normal.
<code>/buscar</code> tema — intento usar búsqueda web de OpenAI.
<code>/imagen</code> descripción — genero una imagen priorizando la mejor calidad disponible.
<code>/voz</code> texto — convierto texto en audio con voz suave y femenina.
<code>/recordar</code> dato — guardo algo importante de este chat.
<code>/memoria</code> — veo lo que recuerdo.
<code>/olvidar</code> — borro memoria y contexto de este chat.
<code>/estado</code> — estado de claves y concurrencia.

<b>Fotos y archivos</b>
Envíame una foto para analizarla. Para editar una foto, envíala con caption así:
<code>editar: cambia el fondo a una playa elegante al atardecer</code>

<b>Audio</b>
Puedes enviarme una nota de voz: la transcribo y te respondo con cuidado.
""".strip()


CALLBACK_TEXTS = {
    "help_chat": "💬 <b>Hablar con Tata</b>\n\nEscríbeme normal, como hablarías con alguien de confianza. Puedo responder con cariño, ayudarte a ordenar ideas, escribir mensajes, planear cosas bonitas o acompañarte emocionalmente.",
    "help_image": "🎨 <b>Imágenes</b>\n\nUsa:\n<code>/imagen una habitación elegante con flores rosas y luz cálida</code>\n\nIntento primero los mejores perfiles de imagen y solo bajo de modelo/calidad si una key o cuenta no puede generar.",
    "help_voice": "🎙️ <b>Voz de Tata</b>\n\nUsa:\n<code>/voz hoy quiero recordarte algo bonito...</code>\n\nLa voz es generada por IA y está configurada para sonar suave, femenina, cálida y profesional.",
    "help_files": "📎 <b>Archivos</b>\n\nPuedes enviarme fotos, PDFs, documentos, audios o notas de voz. Los analizo con cuidado y te respondo de forma clara.",
    "help_memory": "🧠 <b>Memoria</b>\n\n<code>/recordar</code> guarda algo importante.\n<code>/memoria</code> muestra recuerdos.\n<code>/olvidar</code> borra memoria y conversación de este chat.",
}


# -----------------------------------------------------------------------------
# Comandos
# -----------------------------------------------------------------------------


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await reject_if_not_allowed(update):
        return
    msg = update.effective_message
    if not msg:
        return
    tata = get_tata()

    # Cachea el file_id por hash de la portada. Si cambias assets/tata_start.png,
    # Telegram recibirá la nueva imagen y no reutilizará una portada vieja/deformada.
    try:
        start_hash = hashlib.sha256(cfg.START_IMAGE.read_bytes()).hexdigest()[:16]
    except Exception:
        start_hash = "default"
    cache_key = f"start_photo:{start_hash}"
    file_id = await tata.file_ids.get(cache_key)
    if file_id:
        try:
            await retry_telegram(
                lambda: msg.reply_photo(
                    photo=file_id,
                    caption=START_CAPTION,
                    parse_mode=ParseMode.HTML,
                    reply_markup=main_keyboard(),
                    read_timeout=cfg.TELEGRAM_READ_TIMEOUT,
                    write_timeout=cfg.TELEGRAM_WRITE_TIMEOUT,
                    connect_timeout=cfg.TELEGRAM_CONNECT_TIMEOUT,
                    pool_timeout=cfg.TELEGRAM_POOL_TIMEOUT,
                ),
                attempts=2,
                label="start-cache",
            )
            return
        except Exception:
            await tata.file_ids.delete(cache_key)

    try:
        data = cfg.START_IMAGE.read_bytes()
        sent = await safe_send_photo(msg, data, caption=START_CAPTION, filename="tata_start.png", reply_markup=main_keyboard())
        if getattr(sent, "photo", None):
            await tata.file_ids.set(cache_key, sent.photo[-1].file_id)
    except Exception as exc:
        log.warning("No pude enviar portada /start: %s", exc)
        await send_long_text(msg, START_CAPTION, parse_mode=ParseMode.HTML)


async def cmd_ayuda(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await reject_if_not_allowed(update):
        return
    if update.effective_message:
        await send_long_text(update.effective_message, HELP_TEXT, parse_mode=ParseMode.HTML)


async def callback_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await reject_if_not_allowed(update):
        return
    query = update.callback_query
    if not query:
        return
    await query.answer()
    data = query.data or ""
    if data == "status":
        await cmd_estado(update, context)
        return
    text = CALLBACK_TEXTS.get(data, HELP_TEXT)
    if query.message:
        await send_long_text(query.message, text, parse_mode=ParseMode.HTML)


async def cmd_chat(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    if not msg:
        return
    text = " ".join(context.args).strip()
    if not text:
        await msg.reply_text("🌷 Escríbeme así: /chat cuéntame algo bonito")
        return
    await process_chat(update, context, text)


async def cmd_buscar(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    if not msg:
        return
    query = " ".join(context.args).strip()
    if not query:
        await msg.reply_text("🌐 Escríbeme así: /buscar noticias actuales sobre inteligencia artificial")
        return

    async def action():
        tata = get_tata()
        chat_id = update.effective_chat.id
        memory_block, context_block = await tata.prompt_context(chat_id)
        prompt = (
            "Busca información actual cuando sea necesario y responde de forma clara, bonita y organizada. "
            "Incluye advertencias si no tienes certeza.\n\n"
            f"Tema: {query}"
        )
        answer = await tata.openai.response_text(
            prompt=prompt,
            chat_id=chat_id,
            memory_block=memory_block,
            context_block=context_block,
            search=True,
        )
        await tata.conversation.add(chat_id, "user", f"/buscar {query}")
        await tata.conversation.add(chat_id, "assistant", answer)
        await send_ai_text(msg, answer)

    await guarded(
        update,
        context,
        action,
        processing_text="🌐 Estoy buscando con cuidadito para darte algo útil y actual, amor…",
        chat_action=ChatAction.TYPING,
    )


async def cmd_imagen(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    if not msg:
        return
    prompt = " ".join(context.args).strip()
    if not prompt:
        await msg.reply_text("🎨 Escríbeme así: /imagen una ilustración elegante de flores rosadas con luz cálida")
        return

    async def action():
        tata = get_tata()
        result = await tata.openai.generate_image(prompt)
        caption = (
            "🎨 <b>Listo, amor. Te hice esta imagen.</b>\n"
            f"Modelo: <code>{escape(result.model)}</code> · calidad: <code>{escape(result.quality)}</code> · tamaño: <code>{escape(result.size)}</code>"
        )
        await safe_send_photo(msg, result.data, caption=caption, filename="tata_imagen.jpg")
        if cfg.ENVIAR_ARCHIVO_ORIGINAL_IMAGEN and len(result.data) <= cfg.MAX_ORIGINAL_IMAGE_DOCUMENT_MB * 1024 * 1024:
            await safe_send_document(
                msg,
                result.data,
                filename="tata_imagen_original.jpg",
                caption="💎 Original de mayor calidad para guardar bonito.",
            )

    await guarded(
        update,
        context,
        action,
        processing_text="🎨 Estoy creando tu imagen con mucho cuidado, amor… primero intento la mejor calidad posible.",
        chat_action=ChatAction.UPLOAD_PHOTO,
    )


async def cmd_voz(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    if not msg:
        return
    text = " ".join(context.args).strip()
    if not text:
        await msg.reply_text("🎙️ Escríbeme así: /voz hoy quiero decirte algo bonito…")
        return

    async def action():
        tata = get_tata()
        audio = await tata.openai.tts(text)
        await safe_send_audio(msg, audio, title="Voz de Tata")

    await guarded(
        update,
        context,
        action,
        processing_text="🎙️ Estoy preparando una voz suave y femenina para ti, mi amor…",
        chat_action=ChatAction.UPLOAD_VOICE,
    )


async def cmd_recordar(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await reject_if_not_allowed(update):
        return
    msg = update.effective_message
    if not msg:
        return
    text = " ".join(context.args).strip()
    if not text:
        await msg.reply_text("🧠 Dime qué quieres que recuerde: /recordar le gustan las flores blancas")
        return
    tata = get_tata()
    await tata.memory.remember(update.effective_chat.id, text)
    await msg.reply_text("🌷 Lo guardé con cariño en mi memoria.")


async def cmd_memoria(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await reject_if_not_allowed(update):
        return
    msg = update.effective_message
    if not msg:
        return
    tata = get_tata()
    items = await tata.memory.get(update.effective_chat.id)
    if not items:
        await msg.reply_text("🧠 Todavía no tengo recuerdos guardados en este chat.")
        return
    text = "🧠 <b>Recuerdos de este chat</b>\n\n" + "\n".join(f"• {escape(x)}" for x in items[-30:])
    await send_long_text(msg, text, parse_mode=ParseMode.HTML)


async def cmd_olvidar(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await reject_if_not_allowed(update):
        return
    msg = update.effective_message
    if not msg:
        return
    tata = get_tata()
    await tata.memory.clear(update.effective_chat.id)
    await tata.conversation.clear(update.effective_chat.id)
    await msg.reply_text("🌧️ Listo, amor. Borré la memoria y el contexto de este chat.")


async def cmd_estado(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await reject_if_not_allowed(update):
        return
    msg = update.effective_message or (update.callback_query.message if update.callback_query else None)
    if not msg:
        return
    tata = get_tata()
    stats = tata.key_manager.stats()
    text = f"""
🌷 <b>Estado de TataBot</b>

<b>OpenAI keys</b>
Total: <code>{stats['total']}</code>
Disponibles: <code>{stats['available']}</code>
Inválidas: <code>{stats['invalid']}</code>
Sin cuota en cooldown: <code>{stats['quota']}</code>
Cooldown temporal: <code>{stats['cooldown']}</code>

<b>Concurrencia</b>
Máximo simultáneo: <code>{cfg.MAX_CONCURRENCIA}</code>

<b>Imagen</b>
Prioridad: <code>{escape(' → '.join(cfg.IMAGE_MODEL_PRIORITY))}</code>

<b>Voz</b>
Voces suaves configuradas: <code>{escape(', '.join(cfg.TTS_FEMALE_VOICES))}</code>
""".strip()
    await send_long_text(msg, text, parse_mode=ParseMode.HTML)


async def cmd_errores(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await reject_if_not_allowed(update):
        return
    msg = update.effective_message
    if not msg:
        return
    tata = get_tata()
    errors = tata.key_manager.last_errors(15)
    if not errors:
        await msg.reply_text("✨ No tengo errores recientes guardados.")
        return
    lines = ["🧾 <b>Últimos errores de OpenAI</b>\n"]
    for err in errors:
        lines.append(
            f"• <code>{escape(err.get('at', ''))}</code> · <b>{escape(err.get('key', ''))}</b> · "
            f"{escape(err.get('category', ''))}\n  <code>{escape(truncate(err.get('reason', ''), 260))}</code>"
        )
    await send_long_text(msg, "\n".join(lines), parse_mode=ParseMode.HTML)


async def cmd_reset_claves(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await reject_if_not_allowed(update):
        return
    msg = update.effective_message
    if not msg:
        return
    tata = get_tata()
    await tata.key_manager.reset()
    await msg.reply_text("🔄 Listo. Reinicié el estado local de claves y cooldowns.")

# -----------------------------------------------------------------------------
# Mensajes normales, fotos, documentos y audio
# -----------------------------------------------------------------------------


async def process_chat(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str) -> None:
    msg = update.effective_message
    chat = update.effective_chat
    if not msg or not chat:
        return

    async def action():
        tata = get_tata()
        memory_block, context_block = await tata.prompt_context(chat.id)
        answer = await tata.openai.response_text(
            prompt=text,
            chat_id=chat.id,
            memory_block=memory_block,
            context_block=context_block,
        )
        await tata.conversation.add(chat.id, "user", text)
        await tata.conversation.add(chat.id, "assistant", answer)
        await send_ai_text(msg, answer)

    await guarded(update, context, action, processing_text=random.choice(PROCESSING_TEXTS), chat_action=ChatAction.TYPING)


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    if not msg or not msg.text:
        return
    await process_chat(update, context, msg.text.strip())


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    chat = update.effective_chat
    if not msg or not chat or not msg.photo:
        return
    caption = (msg.caption or "").strip()
    photo = msg.photo[-1]

    async def action():
        tata = get_tata()
        data = await download_telegram_file(context, photo.file_id)
        if caption.lower().startswith("editar:"):
            prompt = caption.split(":", 1)[1].strip() or "mejora esta imagen con estilo bonito y profesional"
            result = await tata.openai.edit_image(prompt=prompt, image_data=data)
            out_caption = (
                "🎨 <b>Edición lista, amor.</b>\n"
                f"Modelo: <code>{escape(result.model)}</code> · calidad: <code>{escape(result.quality)}</code>"
            )
            await safe_send_photo(msg, result.data, caption=out_caption, filename="tata_edicion.jpg")
            if cfg.ENVIAR_ARCHIVO_ORIGINAL_IMAGEN and len(result.data) <= cfg.MAX_ORIGINAL_IMAGE_DOCUMENT_MB * 1024 * 1024:
                await safe_send_document(msg, result.data, filename="tata_edicion_original.png", caption="💎 Original de la edición.")
            return

        prompt = caption or "Analiza esta imagen con detalle. Describe lo importante y ayuda de forma clara y amable."
        memory_block, context_block = await tata.prompt_context(chat.id)
        answer = await tata.openai.analyze_image(
            prompt=prompt,
            image_data=data,
            chat_id=chat.id,
            memory_block=memory_block,
            context_block=context_block,
        )
        await tata.conversation.add(chat.id, "user", f"[Imagen] {prompt}")
        await tata.conversation.add(chat.id, "assistant", answer)
        await send_ai_text(msg, answer)

    text = "🎨 Estoy mirando tu imagen con cuidado, amor…"
    if caption.lower().startswith("editar:"):
        text = "🎨 Estoy editando la foto con mucho detalle y cariño…"
    await guarded(update, context, action, processing_text=text, chat_action=ChatAction.UPLOAD_PHOTO)


async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    chat = update.effective_chat
    if not msg or not chat or not msg.document:
        return
    doc = msg.document
    filename = doc.file_name or "archivo"
    mime = doc.mime_type or guess_mime(filename)
    size_mb = (doc.file_size or 0) / (1024 * 1024)
    if size_mb > cfg.MAX_TELEGRAM_FILE_MB:
        await msg.reply_text(f"📎 Amor, ese archivo pesa {size_mb:.1f} MB. Mi límite seguro ahora es {cfg.MAX_TELEGRAM_FILE_MB} MB.")
        return

    caption = (msg.caption or "").strip()

    async def action():
        tata = get_tata()
        data = await download_telegram_file(context, doc.file_id)
        prompt = caption or (
            "Analiza este archivo de forma clara y organizada. Resume lo importante, "
            "detecta puntos útiles y explícame lo que debería saber."
        )
        memory_block, context_block = await tata.prompt_context(chat.id)
        answer = await tata.openai.analyze_file(
            prompt=prompt,
            file_data=data,
            filename=filename,
            mime=mime,
            chat_id=chat.id,
            memory_block=memory_block,
            context_block=context_block,
        )
        await tata.conversation.add(chat.id, "user", f"[Archivo {filename}] {prompt}")
        await tata.conversation.add(chat.id, "assistant", answer)
        await send_ai_text(msg, answer)

    await guarded(update, context, action, processing_text="📎 Estoy leyendo tu archivo con cuidadito, amor…", chat_action=ChatAction.UPLOAD_DOCUMENT)


async def handle_audio(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    chat = update.effective_chat
    if not msg or not chat:
        return
    media = msg.voice or msg.audio
    if not media:
        return
    size_mb = (getattr(media, "file_size", 0) or 0) / (1024 * 1024)
    if size_mb > cfg.MAX_TELEGRAM_FILE_MB:
        await msg.reply_text(f"🎙️ Amor, ese audio pesa {size_mb:.1f} MB. Mi límite seguro ahora es {cfg.MAX_TELEGRAM_FILE_MB} MB.")
        return

    async def action():
        tata = get_tata()
        filename = getattr(media, "file_name", None) or "audio.ogg"
        mime = getattr(media, "mime_type", None) or guess_mime(filename, "audio/ogg")
        data = await download_telegram_file(context, media.file_id)
        transcript = await tata.openai.transcribe(data, filename, mime)
        memory_block, context_block = await tata.prompt_context(chat.id)
        answer = await tata.openai.response_text(
            prompt=(
                "La usuaria envió una nota de voz. Primero ten en cuenta esta transcripción, "
                "luego responde de forma cálida y útil.\n\n"
                f"Transcripción: {transcript}"
            ),
            chat_id=chat.id,
            memory_block=memory_block,
            context_block=context_block,
        )
        await tata.conversation.add(chat.id, "user", f"[Audio transcrito] {transcript}")
        await tata.conversation.add(chat.id, "assistant", answer)
        await send_long_text(msg, f"🎙️ <b>Te escuché así:</b>\n{escape(transcript)}", parse_mode=ParseMode.HTML)
        await send_ai_text(msg, answer)

    await guarded(update, context, action, processing_text="🎙️ Estoy escuchando tu audio con atención, amor…", chat_action=ChatAction.TYPING)


# -----------------------------------------------------------------------------
# Inicio de la aplicación
# -----------------------------------------------------------------------------


async def post_init(app: Application) -> None:
    await app.bot.set_my_commands(
        [
            BotCommand("start", "Abrir menú bonito"),
            BotCommand("ayuda", "Ver guía de uso"),
            BotCommand("chat", "Hablar con Tata"),
            BotCommand("buscar", "Buscar algo actual"),
            BotCommand("imagen", "Crear una imagen"),
            BotCommand("voz", "Crear audio con voz de Tata"),
            BotCommand("recordar", "Guardar un recuerdo"),
            BotCommand("memoria", "Ver recuerdos"),
            BotCommand("olvidar", "Borrar memoria del chat"),
            BotCommand("estado", "Ver estado del bot"),
        ]
    )


async def post_shutdown(app: Application) -> None:
    if TATA is not None:
        await TATA.shutdown()


def build_application() -> Application:
    request = HTTPXRequest(
        connect_timeout=cfg.TELEGRAM_CONNECT_TIMEOUT,
        read_timeout=cfg.TELEGRAM_READ_TIMEOUT,
        write_timeout=cfg.TELEGRAM_WRITE_TIMEOUT,
        pool_timeout=cfg.TELEGRAM_POOL_TIMEOUT,
        media_write_timeout=cfg.TELEGRAM_WRITE_TIMEOUT,
        connection_pool_size=max(cfg.TELEGRAM_CONNECTION_POOL_SIZE, cfg.MAX_CONCURRENCIA * 4),
    )
    app = (
        ApplicationBuilder()
        .token(cfg.TELEGRAM_BOT_TOKEN)
        .request(request)
        .concurrent_updates(cfg.MAX_CONCURRENCIA)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )

    app.add_handler(CommandHandler(["start", "menu"], cmd_start))
    app.add_handler(CommandHandler(["ayuda", "help"], cmd_ayuda))
    app.add_handler(CommandHandler("chat", cmd_chat))
    app.add_handler(CommandHandler("buscar", cmd_buscar))
    app.add_handler(CommandHandler("imagen", cmd_imagen))
    app.add_handler(CommandHandler("voz", cmd_voz))
    app.add_handler(CommandHandler("recordar", cmd_recordar))
    app.add_handler(CommandHandler("memoria", cmd_memoria))
    app.add_handler(CommandHandler("olvidar", cmd_olvidar))
    app.add_handler(CommandHandler("estado", cmd_estado))
    app.add_handler(CommandHandler("errores", cmd_errores))
    app.add_handler(CommandHandler("reset_claves", cmd_reset_claves))
    app.add_handler(CallbackQueryHandler(callback_menu))

    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, handle_audio))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    return app


def validate_config() -> None:
    if not cfg.TELEGRAM_BOT_TOKEN or "PEGA_AQUI" in cfg.TELEGRAM_BOT_TOKEN:
        raise RuntimeError("Edita configuracion.py y pon TELEGRAM_BOT_TOKEN.")
    if not cfg.OPENAI_TXT.exists():
        raise RuntimeError(f"No encuentro OPENAI_TXT: {cfg.OPENAI_TXT}")
    keys = read_keys(cfg.OPENAI_TXT)
    if not keys:
        raise RuntimeError(f"El archivo de claves está vacío o no contiene claves sk-: {cfg.OPENAI_TXT}")


def main() -> None:
    global TATA
    validate_config()
    TATA = TataBot()
    stats = TATA.key_manager.stats()
    log.info("Tata está encendida. Claves OpenAI cargadas=%s | disponibles=%s | TXT=%s", stats["total"], stats["available"], cfg.OPENAI_TXT)
    app = build_application()
    app.run_polling(
        allowed_updates=Update.ALL_TYPES,
        close_loop=False,
        drop_pending_updates=True,
    )


if __name__ == "__main__":
    main()
