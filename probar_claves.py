"""Prueba las claves de .env sin mostrarlas en pantalla.
- SerpApi: consulta la cuenta (no gasta busquedas).
- Telegram: obtiene tu chat_id, lo guarda en .env y te envia un mensaje de prueba.
"""
import json
import pathlib
import urllib.parse
import urllib.request

ENV = pathlib.Path(__file__).with_name(".env")


def leer_env():
    datos = {}
    for linea in ENV.read_text(encoding="utf-8").splitlines():
        if "=" in linea and not linea.strip().startswith("#"):
            k, v = linea.split("=", 1)
            datos[k.strip()] = v.strip().strip('"').strip("'")
    return datos


def get_json(url, data=None):
    req = urllib.request.Request(url, data=data)
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.load(r)


env = leer_env()

# SerpApi
key = env.get("SERPAPI_KEY", "")
if not key:
    print("SerpApi: FALTA la clave en .env")
else:
    try:
        cuenta = get_json(f"https://serpapi.com/account.json?api_key={key}")
        print(f"SerpApi OK - plan: {cuenta.get('plan_name')}, "
              f"busquedas restantes este mes: {cuenta.get('total_searches_left')}")
    except Exception as e:
        print(f"SerpApi ERROR: {type(e).__name__} (revisa que la clave este bien copiada)")

# Telegram
token = env.get("TELEGRAM_BOT_TOKEN", "")
if not token:
    print("Telegram: FALTA el token en .env")
else:
    base = f"https://api.telegram.org/bot{token}"
    try:
        upd = get_json(f"{base}/getUpdates")
        chats = [u["message"]["chat"]["id"] for u in upd.get("result", []) if "message" in u]
        if not chats:
            print("Telegram: token OK, pero no encuentro tu mensaje. "
                  "Abre tu bot, toca Iniciar y escribele 'hola'. Luego repite.")
        else:
            chat_id = str(chats[-1])
            if "TELEGRAM_CHAT_ID" not in env:
                with ENV.open("a", encoding="utf-8") as f:
                    f.write(f"TELEGRAM_CHAT_ID={chat_id}\n")
            msg = urllib.parse.urlencode({
                "chat_id": chat_id,
                "text": "✅ Prueba OK: tu bot de alertas de viaje funciona.",
            }).encode()
            get_json(f"{base}/sendMessage", data=msg)
            print("Telegram OK - mensaje de prueba enviado, chat_id guardado en .env")
    except Exception as e:
        detalle = str(getattr(e, "reason", e)).replace(token, "***")
        print(f"Telegram ERROR: {type(e).__name__}: {detalle}")
