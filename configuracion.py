"""
Configuración principal de TataBot.

Edita este archivo para poner tu token, la ruta de tu TXT de OpenAI y tus preferencias.
No necesitas .env, Docker ni scripts extra.
"""
from pathlib import Path

CARPETA_PROYECTO = Path(__file__).resolve().parent

# -----------------------------------------------------------------------------
# 1) Datos que debes configurar
# -----------------------------------------------------------------------------

# Token del bot creado con BotFather.
TELEGRAM_BOT_TOKEN = "PEGA_AQUI_TU_TOKEN_NUEVO_DE_BOTFATHER"

# TXT con tus API keys válidas de OpenAI, una por línea.
# Puedes dejarlo junto al bot o poner una ruta absoluta.
OPENAI_TXT = CARPETA_PROYECTO / "apis-openai.txt"
# Ejemplo Android/Termux:
# OPENAI_TXT = Path(r"/storage/emulated/0/Download/apis-openai.txt")
# Ejemplo Windows:
# OPENAI_TXT = Path(r"C:\\Users\\TuUsuario\\Desktop\\apis-openai.txt")

# Opcional: limita el bot solo a ciertos IDs de Telegram.
# Déjalo vacío para que cualquiera con acceso al bot pueda usarlo.
USUARIOS_PERMITIDOS = []
# USUARIOS_PERMITIDOS = [123456789, 987654321]

# -----------------------------------------------------------------------------
# 2) Identidad de Tata
# -----------------------------------------------------------------------------

BOT_NAME = "Tata"
BOT_DISPLAY_NAME = "TataBot"

SYSTEM_PROMPT = f"""
Eres {BOT_NAME}, una asistente virtual femenina, dulce, elegante, cálida y muy detallista.
Tu personalidad está pensada para una mujer especial: quieres que se sienta cuidada,
escuchada, comprendida y acompañada.

Tu estilo:
- Hablas en español natural, bonito y humano.
- Eres organizada, clara, estética y cariñosa.
- Usas emojis con moderación y buen gusto.
- Tienes vibra de psicóloga amable: escuchas, validas emociones, ayudas a ordenar ideas
  y propones pasos concretos.
- No afirmas ser terapeuta licenciada, médica ni sustituto de ayuda profesional.
- No diagnosticas enfermedades ni das instrucciones peligrosas.
- En crisis, urgencias, autolesión o violencia, recomiendas buscar ayuda profesional o servicios de emergencia.
- En tareas creativas, sorprendes con detalles, opciones elegantes y sensibilidad.
- En imágenes, mejoras el prompt con composición, iluminación, estilo, colores y detalles femeninos/profesionales.

Formato:
- Responde bonito, pero sin exagerar.
- Cuando sea útil, usa secciones breves.
- Formatea pensando en Telegram: mejor texto limpio, títulos cortos y viñetas claras.
- Evita markdown complejo o símbolos decorativos innecesarios si pueden ensuciar el mensaje.
- Para temas emocionales, prioriza contención, calma y pasos pequeños.
""".strip()

# -----------------------------------------------------------------------------
# 3) Modelos y prioridad
# -----------------------------------------------------------------------------

# Tata intenta primero los mejores modelos configurados y baja si una cuenta/key no tiene acceso.
TEXT_MODEL_PRIORITY = [
    "gpt-5.4-mini",
    "gpt-5.4-nano",
    "gpt-5-mini",
    "gpt-4.1-mini",
    "gpt-4o-mini",
]
MAX_OUTPUT_TOKENS = 2400

# Generación de imágenes: prioridad de máxima calidad a menor costo/compatibilidad.
# El código prueba modelo/calidad/tamaño/key y va bajando solo si falla.
IMAGE_MODEL_PRIORITY = [
    "gpt-image-1.5",
    "gpt-image-1",
    "gpt-image-1-mini",
]
IMAGE_QUALITY_PRIORITY = ["high", "medium", "low"]
IMAGE_SIZE_PRIORITY = ["1536x1024", "1024x1536", "1024x1024"]
IMAGE_OUTPUT_FORMAT = "jpeg"
IMAGE_OUTPUT_COMPRESSION = 92
MAX_IMAGE_KEYS_PER_PROFILE = 7

# Voces para Tata. OpenAI no etiqueta oficialmente género por voz; estas están elegidas
# por timbre suave/cálido y además se usa instructions para pedir voz femenina cuando se puede.
TTS_MODEL_PRIORITY = ["gpt-4o-mini-tts", "tts-1-hd", "tts-1"]
TTS_FEMALE_VOICES = ["coral", "nova", "shimmer", "sage", "ballad", "marin"]
TRANSCRIPTION_MODEL_PRIORITY = ["gpt-4o-mini-transcribe", "whisper-1"]

# -----------------------------------------------------------------------------
# 4) Rendimiento, timeouts y Telegram
# -----------------------------------------------------------------------------

MAX_CONCURRENCIA = 5
MAX_TELEGRAM_FILE_MB = 19

# Timeouts amplios para Termux/conexiones móviles. El error que mostraste era WriteTimeout
# de Telegram al subir foto, por eso se aumenta media_write_timeout/write_timeout.
TELEGRAM_CONNECT_TIMEOUT = 30
TELEGRAM_READ_TIMEOUT = 180
TELEGRAM_WRITE_TIMEOUT = 240
TELEGRAM_POOL_TIMEOUT = 60
TELEGRAM_CONNECTION_POOL_SIZE = 24

OPENAI_CONNECT_TIMEOUT = 30
OPENAI_READ_TIMEOUT = 240
OPENAI_WRITE_TIMEOUT = 240
OPENAI_POOL_TIMEOUT = 60

# Preview: imagen comprimida para verse rápido en Telegram.
# Original: archivo adicional de mayor calidad, si no pesa demasiado.
ENVIAR_ARCHIVO_ORIGINAL_IMAGEN = True
MAX_ORIGINAL_IMAGE_DOCUMENT_MB = 12

# -----------------------------------------------------------------------------
# 5) Rotación de claves
# -----------------------------------------------------------------------------

MAX_KEYS_PER_TEXT_OPERATION = 7
MAX_KEYS_PER_AUDIO_OPERATION = 7
KEY_TEMP_COOLDOWN_SECONDS = 8 * 60
KEY_QUOTA_COOLDOWN_HOURS = 24

# -----------------------------------------------------------------------------
# 6) Carpetas internas
# -----------------------------------------------------------------------------

DATA_DIR = CARPETA_PROYECTO / "data"
ASSETS_DIR = CARPETA_PROYECTO / "assets"
START_IMAGE = ASSETS_DIR / "tata_start.png"
