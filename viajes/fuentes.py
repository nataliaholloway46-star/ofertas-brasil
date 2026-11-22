"""Fuentes de datos: SerpApi (Google Flights / Google Hotels) y tipo de cambio.
Nunca se inventan datos: si algo falta queda como None / "no informado"."""
import datetime as dt
import json
import os
import requests

SERP = "https://serpapi.com/search.json"


class SinPresupuesto(Exception):
    pass


class Serp:
    """Cliente SerpApi que cuenta y limita las busquedas gastadas."""

    def __init__(self, con, permitidas):
        self.key = os.environ.get("SERPAPI_KEY", "")
        self.con = con
        self.permitidas = permitidas
        self.usadas = 0

    def cuenta(self):
        r = requests.get("https://serpapi.com/account.json", params={"api_key": self.key}, timeout=30)
        r.raise_for_status()
        return r.json()  # no gasta busquedas

    def buscar(self, params):
        if self.usadas >= self.permitidas:
            raise SinPresupuesto()
        self.usadas += 1
        ts = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
        detalle = json.dumps({k: v for k, v in params.items() if "token" not in k}, ensure_ascii=False)
        try:
            r = requests.get(SERP, params={**params, "api_key": self.key}, timeout=90)
            datos = r.json()
            if r.status_code != 200 or "error" in datos:
                raise RuntimeError(datos.get("error", f"HTTP {r.status_code}"))
            self.con.execute("INSERT INTO busquedas(ts,motor,detalle,ok) VALUES(?,?,?,1)",
                             (ts, params["engine"], detalle))
            self.con.commit()
            return datos
        except Exception as e:
            msg = str(e).replace(self.key, "***") if self.key else str(e)
            self.con.execute("INSERT INTO busquedas(ts,motor,detalle,ok,error) VALUES(?,?,?,0,?)",
                             (ts, params["engine"], detalle, msg[:300]))
            self.con.commit()
            raise RuntimeError(msg) from None


# ---------------------------------------------------------------- vuelos
def _equipaje(opcion):
    textos = [t.lower() for t in opcion.get("extensions", []) or []]
    for s in opcion.get("flights", []):
        textos += [t.lower() for t in s.get("extensions", []) or []]
    partes = []
    if any("carry-on" in t and ("fee" in t or "not included" in t) for t in textos):
        partes.append("mano con costo")
    elif any("carry-on" in t for t in textos):
        partes.append("mano incluida")
    if any("checked bag" in t and ("fee" in t or "not included" in t) for t in textos):
        partes.append("bodega con costo")
    elif any("checked bag" in t for t in textos):
        partes.append("bodega incluida")
    return " · ".join(partes) if partes else "no informado"


def buscar_vuelos(serp, origen, destino, llegada, salida, regreso, adultos):
    d = serp.buscar({
        "engine": "google_flights", "departure_id": origen, "arrival_id": llegada,
        "outbound_date": salida, "return_date": regreso, "type": "1",
        "adults": adultos, "currency": "CLP", "hl": "en", "gl": "cl", "deep_search": "true",
    })
    ins = d.get("price_insights", {}) or {}
    tip = ins.get("typical_price_range") or [None, None]
    link = d.get("search_metadata", {}).get("google_flights_url")
    ts = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    filas = []
    for op in (d.get("best_flights") or []) + (d.get("other_flights") or []):
        if not op.get("price"):
            continue  # sin precio = no se guarda
        segs = op.get("flights", [])
        lay = op.get("layovers", []) or []
        filas.append(dict(
            ts=ts, destino=destino, salida=salida, regreso=regreso, llegada=llegada,
            precio_clp=float(op["price"]),
            aerolinea=" + ".join(dict.fromkeys(s.get("airline", "?") for s in segs)),
            escalas=len(lay), duracion_min=op.get("total_duration"),
            escala_max_min=max([l.get("duration", 0) for l in lay], default=0),
            escala_nocturna=int(any(l.get("overnight") for l in lay)),
            equipaje=_equipaje(op),
            hora_salida=(segs[0].get("departure_airport", {}).get("time", "") or "")[-5:] if segs else "",
            retraso_frecuente=int(any(s.get("often_delayed_by_over_30_min") for s in segs)),
            nivel_precio=ins.get("price_level"), tipico_min=tip[0], tipico_max=tip[1],
            link=link, fuente="Google Flights (SerpApi)", departure_token=op.get("departure_token")))
    historial = [(dt.datetime.fromtimestamp(t, dt.timezone.utc).date().isoformat(), float(p))
                 for t, p in (ins.get("price_history") or []) if p]
    return filas, historial


def verificar_vuelo(serp, origen, llegada, salida, regreso, adultos, departure_token, precio):
    """Busca quien vende el vuelo y a que precio (gasta 2 busquedas)."""
    base = {"engine": "google_flights", "departure_id": origen, "arrival_id": llegada,
            "outbound_date": salida, "return_date": regreso, "type": "1", "adults": adultos,
            "currency": "CLP", "hl": "en", "gl": "cl"}
    vuelta = serp.buscar({**base, "departure_token": departure_token})
    ops = (vuelta.get("best_flights") or []) + (vuelta.get("other_flights") or [])
    if not ops or not ops[0].get("booking_token"):
        return [], "sin opciones de compra"
    reserva = serp.buscar({**base, "booking_token": ops[0]["booking_token"]})
    vendedores = []
    for b in reserva.get("booking_options", []) or []:
        t = b.get("together") or {}
        if t.get("price"):
            vendedores.append({"vendedor": t.get("book_with"), "precio_clp": float(t["price"])})
    cercanos = [v for v in vendedores if abs(v["precio_clp"] - precio) / precio <= 0.05]
    return cercanos, f"{len(cercanos)} vendedores confirman precio (±5%)"


# ---------------------------------------------------------------- hoteles
def buscar_hoteles(serp, destino, zona, salida, regreso, adultos, estrellas_min):
    clases = ",".join(str(c) for c in range(estrellas_min, 6))
    d = serp.buscar({
        "engine": "google_hotels", "q": f"hotels in {zona}, Brazil",
        "check_in_date": salida, "check_out_date": regreso, "adults": adultos,
        "currency": "CLP", "hl": "en", "gl": "cl", "hotel_class": clases, "rating": "8",
    })
    ts = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    link_busqueda = d.get("search_metadata", {}).get("google_hotels_url")
    filas = []
    for p in d.get("properties", []) or []:
        total = (p.get("total_rate") or {}).get("extracted_lowest")
        if not total or p.get("type") == "vacation rental":
            continue
        amen = " ".join(p.get("amenities", []) or []).lower()
        filas.append(dict(
            ts=ts, destino=destino, salida=salida, regreso=regreso, zona=zona,
            nombre=p.get("name"), estrellas=p.get("extracted_hotel_class"),
            nota=p.get("overall_rating"), resenas=p.get("reviews"),
            precio_total_clp=float(total),
            desayuno="sí" if "breakfast" in amen else "no informado",
            cancelacion="no informado",
            link=p.get("link") or link_busqueda, fuente="Google Hotels (SerpApi)",
            property_token=p.get("property_token")))
    return filas


def verificar_hotel(serp, zona, salida, regreso, adultos, token, precio):
    """Compara el precio del hotel entre agencias (gasta 1 busqueda)."""
    d = serp.buscar({"engine": "google_hotels", "q": f"hotels in {zona}, Brazil",
                     "property_token": token, "check_in_date": salida, "check_out_date": regreso,
                     "adults": adultos, "currency": "CLP", "hl": "en", "gl": "cl"})
    fuentes = {}
    for p in (d.get("featured_prices") or []) + (d.get("prices") or []):
        v = (p.get("total_rate") or {}).get("extracted_lowest")
        if v and p.get("source"):
            fuentes[p["source"]] = float(v)
    cercanos = [s for s, v in fuentes.items() if abs(v - precio) / precio <= 0.05]
    return cercanos, f"{len(cercanos)} de {len(fuentes)} agencias con precio ±5%"


# ---------------------------------------------------------------- moneda
def dolar_clp():
    """Dolar observado (Banco Central) via mindicador.cl. None si falla."""
    try:
        r = requests.get("https://mindicador.cl/api/dolar", timeout=20)
        s = r.json()["serie"][0]
        return float(s["valor"]), s["fecha"][:10], "mindicador.cl (dólar observado)"
    except Exception:
        return None, None, "no disponible"
