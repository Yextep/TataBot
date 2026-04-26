# Bot Asistente TataBot 💜 

TataBot es una asistente de Telegram basada solo en OpenAI, pensada para ser cálida, bonita, ordenada y práctica. Esta V2 corrige el problema de codificación `ascii codec can't encode character` y endurece el manejo del TXT de claves.

# Estructura del codigo:

<img align="right" height="400" width="400" alt="GIF" src="https://github.com/Yextep/Learning-Bot/assets/114537444/f4696460-3cbf-4c36-921d-a42d815c469d"/>

```text

tatabot_final_v2/
├─ configuracion.py      # Aquí pones token, ruta del TXT y preferencias                                                ├─ tata_bot.py           # Bot completo
├─ requirements.txt      # Dependencias
├─ apis-openai.txt       # Tus claves válidas, una por línea
├─ assets/
│  └─ tata_start.png    # Portada cuadrada de /start
└─ data/                 # Memoria, conversación y estado de claves
```
                                                                      
## Uso rápido

```bash
cd tatabot_final_v2
pip install -r requirements.txt
nano configuracion.py
nano apis-openai.txt
python3 tata_bot.py
```

En `configuracion.py` cambia:                                          
```python
TELEGRAM_BOT_TOKEN = "TU_TOKEN_DE_BOTFATHER"
OPENAI_TXT = CARPETA_PROYECTO / "apis-openai.txt"
```

En `apis-openai.txt` pega tus claves válidas de OpenAI. Esta versión extrae solo el token `sk-...`, así que tolera comentarios o etiquetas, por ejemplo:

```text
sk-proj-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
OPENAI_API_KEY=sk-proj-yyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyy
sk-proj-zzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzz # válida
```

Aun así, lo más limpio es dejar una clave por línea, sin texto adicional.

## Comandos

```text
/start       Menú bonito con imagen y botones
/ayuda       Guía de uso
/chat        Conversación normal
/buscar      Búsqueda web con OpenAI cuando esté disponible
/imagen      Genera imágenes priorizando mejor calidad/modelo
/voz         Convierte texto en audio con voz suave/femenina
/recordar    Guarda un recuerdo del chat
/memoria     Muestra recuerdos
/olvidar     Borra memoria y contexto del chat
/estado      Estado de claves, concurrencia y modelos
/errores     Últimos errores técnicos de OpenAI
/reset_claves Reinicia cooldowns locales de claves
```

También acepta mensajes normales, fotos, PDFs, documentos, audios y notas de voz.

## Edición de imágenes

Envía una foto con caption:

```text
editar: cambia el fondo a una playa elegante al atardecer
```

## Mejoras incluidas en esta V2

- Corrige el error `ascii codec can't encode character` al enviar texto con tildes, ñ o emojis.
- El cuerpo JSON hacia OpenAI se envía explícitamente como UTF-8.
- El TXT de claves ahora se limpia: solo se usa el token `sk-...`, no la línea completa.
- Si una key trae caracteres raros o texto pegado, se marca localmente como mal formada en vez de tumbar el bot.
- `/start` cachea la portada por hash; si cambias `assets/tata_start.png`, Telegram recibirá la nueva imagen.
- Respuestas de audio transcrito ya no mezclan HTML con texto generado por IA, evitando errores de parseo en Telegram.
- `/start` usa una portada cuadrada 1080x1080 para no verse deformada.
- El envío de imágenes comprime una vista previa para Telegram y, si está activado, manda el original como documento.
- Si Telegram falla al subir una foto por timeout, Tata intenta una imagen más ligera y luego documento.
- Mensaje temporal de “procesando” con tono cariñoso; se elimina automáticamente cuando llega la respuesta.
- Concurrencia limitada a 5 solicitudes pesadas simultáneas.
- Rotación de claves OpenAI con cooldown para claves inválidas, sin cuota o con errores temporales.
- Imágenes con fallback por modelo/calidad/tamaño/key: intenta primero `gpt-image-1.5` y baja si no se puede.
- Voz configurada con timbres suaves y `instructions` para pedir tono femenino/cálido cuando el modelo lo permite.

## Notas importantes

- El bot guarda estado local en `data/`.
- Si cambias tus claves o quieres olvidar cooldowns, usa `/reset_claves`.
- Si tu TXT tenía comentarios con tildes como `válida`, esta versión ya no intentará enviar esa palabra dentro del header de autorización.
