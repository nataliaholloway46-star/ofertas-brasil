"""Lee en tu Gmail SOLO los correos de ofertas de agencias (Falabella, Despegar, ...)
y extrae paquetes a los destinos configurados, con precio real publicado por la agencia.

Privacidad (el repositorio es publico): no se guarda asunto, nombre ni texto del correo;
solo agencia, fecha, destino, precio, noches y el enlace SIN parametros de seguimiento.
"""
import datetime as dt
import email
import html
import imaplib
import os
import re
import unicodedata
from email.header import decode_header
from html.parser import HTMLParser
from urllib.parse import urlsplit, urlunsplit

PRECIO = re.compile(r"(?:\$|CLP)\s?(\d{1,3}(?:[.\s]\d{3})+)")
NOCHES = re.compile(r"(\d{1,2})\s*(?:noches|nights|noites)")


def _norm(t):
    t = unicodedata.normalize("NFKD", t.lower())
    return "".join(c for c in t if not unicodedata.combining(c))


class _Texto(HTMLParser):
    """Convierte HTML a texto y guarda los enlaces con su posicion en el texto."""

    def __init__(self):
        super().__init__()
        self.partes, self.links, self.largo = [], [], 0

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            href = dict(attrs).get("href") or ""
            if href.startswith("http"):
                self.links.append((self.largo, href))
        if tag in ("br", "p", "div", "td", "tr", "li", "h1", "h2", "h3"):
            self._add(" \n ")

    def handle_data(self, data):
        self._add(data)

    def _add(self, s):
        self.partes.append(s)
        self.largo += len(s)


def _limpiar_link(url):
    p = urlsplit(url)
    return urlunsplit((p.scheme, p.netloc, p.path, "", ""))  # sin ?utm=... ni tokens


def _cuerpo(msg):
    htm, txt = None, None
    for parte in msg.walk():
        tipo = parte.get_content_type()
        if tipo in ("text/html", "text/plain") and not parte.get_filename():
            datos = parte.get_payload(decode=True) or b""
            s = datos.decode(parte.get_content_charset() or "utf-8", errors="replace")
            if tipo == "text/html" and htm is None:
                htm = s
            elif tipo == "text/plain" and txt is None:
                txt = s
    p = _Texto()
    p.feed(htm or html.escape(txt or ""))
    return html.unescape("".join(p.partes)), p.links


def _agencia(remitente, agencias):
    r = remitente.lower()
    for clave, nombre in agencias.items():
        if clave in r:
            return nombre
    return None


def extraer(texto, links, destinos, agencia, fecha, msgid):
    """Busca 'destino ... $precio' en el texto. Devuelve lista de ofertas."""
    norm = _norm(texto)
    correo_de_paquetes = "paquete" in norm
    ofertas, vistos = [], set()
    for d in destinos:
        for palabra in d["palabras"]:
            for m in re.finditer(re.escape(_norm(palabra)), norm):
                ventana = texto[m.start(): m.start() + 300]
                vn = _norm(ventana)
                pm = PRECIO.search(ventana)
                if not pm:
                    continue
                precio = float(re.sub(r"[.\s]", "", pm.group(1)))
                if precio < 50000:  # tasas, cuotas u otros montos chicos
                    continue
                clave = (d["id"], precio)
                if clave in vistos:
                    continue
                vistos.add(clave)
                if "por persona" in vn or "p/p" in vn or "c/u" in vn or "pp" in vn.split():
                    base = "por persona"
                elif "2 personas" in vn or "dos personas" in vn or "total" in vn:
                    base = "total"
                else:
                    base = "no indicado"
                nm = NOCHES.search(vn)
                # enlace mas cercano a la mencion del destino
                link = min(links, key=lambda l: abs(l[0] - m.start()))[1] if links else None
                ofertas.append(dict(
                    msgid=f"{msgid}|{d['id']}|{int(precio)}", fecha=fecha, agencia=agencia, destino=d["id"],
                    precio_clp=precio, base_precio=base, noches=int(nm.group(1)) if nm else None,
                    es_paquete=int(any(k in vn for k in ("paquete", "vuelo + hotel", "vuelo y hotel",
                                                         "aereo + hotel", "aereo y hotel", "hotel"))
                                   or (correo_de_paquetes and "solo ida" not in vn)),
                    desde=int("desde" in vn), link=_limpiar_link(link) if link else None))
    return ofertas


def leer_ofertas(cfg, dias=3):
    """Devuelve (ofertas, error). Sin credenciales -> ([], 'no configurado')."""
    usuario, clave = os.environ.get("GMAIL_USER"), os.environ.get("GMAIL_APP_PASSWORD")
    if not usuario or not clave:
        return [], "Gmail no configurado"
    cc = cfg["correos"]
    destinos = [dict(id=x["id"], palabras=x.get("palabras_correo") or [x["nombre"]])
                for x in cfg["destinos"] if x.get("activo", True)]
    desde = (dt.date.today() - dt.timedelta(days=dias)).strftime("%d-%b-%Y")
    ofertas = []
    try:
        im = imaplib.IMAP4_SSL("imap.gmail.com")
        im.login(usuario, clave)
        im.select("INBOX", readonly=True)  # incluye la pestana Promociones
        for clave_ag in cc["agencias"]:
            ok, ids = im.search(None, "SINCE", desde, "FROM", f'"{clave_ag}"')
            for num in (ids[0].split() if ok == "OK" else []):
                ok, datos = im.fetch(num, "(RFC822)")
                if ok != "OK":
                    continue
                msg = email.message_from_bytes(datos[0][1])
                remitente = str(email.header.make_header(decode_header(msg.get("From", ""))))
                agencia = _agencia(remitente, cc["agencias"])
                if not agencia:
                    continue
                fecha = email.utils.parsedate_to_datetime(msg["Date"]).date().isoformat()
                texto, links = _cuerpo(msg)
                ofertas += extraer(texto, links, destinos, agencia, fecha,
                                   msg.get("Message-ID", num.decode()).strip("<>")[-40:])
        im.logout()
        return ofertas, None
    except Exception as e:
        return ofertas, f"Gmail: {type(e).__name__}"
