"""Busca el grupo de Telegram donde esta el bot, le envia una prueba y
guarda su ID en .env como TELEGRAM_CHAT_ID (el token nunca se muestra)."""
import pathlib
import re

import requests

import main

main.cargar_env()
import os

token = os.environ["TELEGRAM_BOT_TOKEN"]
base = f"https://api.telegram.org/bot{token}"
grupos = {}
for u in requests.get(f"{base}/getUpdates", timeout=30).json().get("result", []):
    for k in ("message", "my_chat_member", "edited_message"):
        chat = (u.get(k) or {}).get("chat")
        if chat and chat.get("type") in ("group", "supergroup"):
            grupos[chat["id"]] = chat.get("title")
if not grupos:
    print("No encuentro el grupo. En el grupo escribe /hola@TU_BOT y repite.")
    raise SystemExit
for gid, titulo in grupos.items():
    print(f"Grupo encontrado: {titulo}  (ID {gid})")
gid = list(grupos)[-1]
r = requests.post(f"{base}/sendMessage", timeout=30, data={
    "chat_id": gid, "text": "✅ Listo: las alertas de ofertas de viaje llegarán a este grupo."})
print("Mensaje de prueba al grupo:", "ok" if r.ok else r.text[:200])
if r.ok:
    env = pathlib.Path(".env")
    txt = re.sub(r"(?m)^TELEGRAM_CHAT_ID=.*$", f"TELEGRAM_CHAT_ID={gid}", env.read_text(encoding="utf-8"))
    env.write_text(txt, encoding="utf-8")
    print(f"Guardado en .env. Valor para el secreto de GitHub TELEGRAM_CHAT_ID: {gid}")
