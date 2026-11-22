"""Envio de alertas: Telegram (principal) y Gmail (respaldo)."""
import html
import os
import smtplib
from email.mime.text import MIMEText

import requests


def clp(x):
    return "$" + f"{x:,.0f}".replace(",", ".") if x is not None else "—"


def fecha(iso):
    meses = "ene feb mar abr may jun jul ago sep oct nov dic".split()
    a, m, d = iso.split("-")
    return f"{int(d)} {meses[int(m) - 1]}"


def texto_alerta(c, usd):
    v, h = c["vuelo"], c["hotel"]
    escalas = "directo" if v["escalas"] == 0 else f"{v['escalas']} escala(s)"
    usd_txt = f" (≈ US{clp(c['total_clp'] / usd)})" if usd else ""
    desc = (f"📉 −{c['descuento_pct']:.0f}% vs precio base pre-Black Friday ({c['base_tipo']})\n"
            if c["descuento_pct"] and c["descuento_pct"] > 0 else "")
    return (
        f"🎯 <b>VUELO + HOTEL AL PRECIO DE UN PAQUETE · {html.escape(c['destino_nombre'])}</b>\n"
        f"📅 {fecha(c['salida'])} → {fecha(c['regreso'])} 2027 ({c['noches']} noches)\n"
        f"💰 <b>{clp(c['total_clp'])} CLP</b> para 2{usd_txt} · {clp(c['total_clp'] / 2)} por persona\n{desc}"
        f"✈️ {html.escape(v['aerolinea'])} {escalas} · {html.escape(v['equipaje'])}\n"
        f"🏨 {html.escape(h['nombre'])} {h['estrellas']}★ · ⭐ {h['nota']}/5 ({h['resenas']} reseñas)\n"
        f"{'✔️ Verificado: ' + html.escape(c.get('verificacion', '')) if c.get('verificado') else '◐ No verificado'}\n"
        f"👉 {c['recomendacion']}: {html.escape(c['razon'])}\n"
        f"🔗 <a href=\"{html.escape(v['link'] or '')}\">Vuelo</a> · "
        f"<a href=\"{html.escape(h['link'] or '')}\">Hotel</a>")


def texto_paquete(p, usd):
    usd_txt = f" (≈ US{clp(p['por_persona_clp'] / usd)})" if usd else ""
    return (
        f"📦 <b>PAQUETE · {html.escape(p['destino_nombre'])}</b>\n"
        f"💰 <b>{'desde ' if p['desde'] else ''}{clp(p['por_persona_clp'])} por persona</b>{usd_txt}"
        f"{' (supuesto: el correo no dice si es por persona)' if p['base_precio'] == 'no indicado' else ''}\n"
        f"{'🌙 ' + str(p['noches']) + ' noches' + chr(10) if p['noches'] else ''}"
        f"🏢 {html.escape(p['agencia'])} · correo del {p['fecha']}\n"
        f"⚠️ Precio publicado por la agencia: revisa fechas, hotel y equipaje antes de comprar\n"
        f"🔗 <a href=\"{html.escape(p['link'] or '')}\">Ver paquete</a>")


def telegram(texto):
    token, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat:
        return "Telegram no configurado"
    r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage", timeout=30, data={
        "chat_id": chat, "text": texto, "parse_mode": "HTML", "disable_web_page_preview": "true"})
    return "ok" if r.ok else f"error Telegram {r.status_code}"


def email(asunto, texto):
    usuario, clave = os.environ.get("GMAIL_USER"), os.environ.get("GMAIL_APP_PASSWORD")
    if not usuario or not clave:
        return "Email no configurado"
    msg = MIMEText(texto.replace("\n", "<br>"), "html", "utf-8")
    msg["Subject"], msg["From"], msg["To"] = asunto, usuario, usuario
    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as s:
            s.login(usuario, clave)
            s.send_message(msg)
        return "ok"
    except Exception as e:
        return f"error email {type(e).__name__}"


def enviar(asunto, texto):
    """Telegram primero; email como respaldo (y si Telegram falla)."""
    t = telegram(texto)
    e = email(asunto, texto)
    return {"telegram": t, "email": e}
