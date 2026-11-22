"""Puntaje precio-calidad, linea base, deteccion de rebajas falsas y recomendacion."""
import datetime as dt
import math
import statistics as st


def _clip(x, a=0, b=100):
    return max(a, min(b, x))


# ----------------------------------------------------------- calidad
def puntaje_vuelo(f, cfg):
    cm = cfg["calidad_minima"]
    s = 100 - {0: 0, 1: 20}.get(f["escalas"], 60)
    s -= max(0, (f["duracion_min"] or 0) / 60 - 4) * 4
    if (f["escala_max_min"] or 0) > cm["escala_max_horas"] * 60:
        s -= 25
    if f["escala_nocturna"]:
        s -= 15
    if f["retraso_frecuente"]:
        s -= 10
    if "mano con costo" in f["equipaje"]:
        s -= 10
    if "bodega incluida" in f["equipaje"]:
        s += 5
    aer = cfg["aerolineas"]
    primera = (f["aerolinea"] or "").split(" + ")[0]
    rep = aer.get(primera, aer["default"])
    return round(0.7 * _clip(s) + 0.3 * rep, 1)


def puntaje_hotel(h):
    s = _clip(((h["nota"] or 0) - 3.8) / 1.2, 0, 1) * 60
    s += {5: 20, 4: 12}.get(h["estrellas"] or 0, 0)
    s += min(1, math.log10(max(h["resenas"] or 1, 1)) / 3.5) * 10
    s += 10 if h["desayuno"] == "sí" else 0
    return round(_clip(s), 1)


def calidad_ok(f, h, cfg):
    cm = cfg["calidad_minima"]
    motivos = []
    if f["escalas"] > cm["escalas_max"]:
        motivos.append(f"{f['escalas']} escalas")
    if (f["escala_max_min"] or 0) > cm["escala_max_horas"] * 60:
        motivos.append("escala de más de %dh" % cm["escala_max_horas"])
    if (h["nota"] or 0) < cm["hotel_nota_min_5"]:
        motivos.append(f"nota hotel {h['nota']}")
    if (h["estrellas"] or 0) < cm["hotel_estrellas_min"]:
        motivos.append(f"hotel {h['estrellas']}★")
    if (h["resenas"] or 0) < cm["hotel_resenas_min"]:
        motivos.append(f"pocas reseñas ({h['resenas']})")
    return not motivos, motivos


# ----------------------------------------------------------- historial
def historial_vuelo(con, d, sal, reg, escalas_max=1):
    """Precio minimo diario del vuelo (con escalas permitidas): propio + historial Google Flights."""
    dias = {}
    for r in con.execute("SELECT substr(ts,1,10) f, MIN(precio_clp) p FROM vuelos WHERE destino=? "
                         "AND salida=? AND regreso=? AND escalas<=? GROUP BY f", (d, sal, reg, escalas_max)):
        dias[r["f"]] = r["p"]
    for r in con.execute("SELECT fecha, precio_clp FROM historial_google "
                         "WHERE destino=? AND salida=? AND regreso=?", (d, sal, reg)):
        dias.setdefault(r["fecha"], r["precio_clp"])
    return dict(sorted(dias.items()))


def historial_hotel(con, d, sal, reg, nombre):
    return {r["f"]: r["p"] for r in con.execute(
        "SELECT substr(ts,1,10) f, MIN(precio_total_clp) p FROM hoteles WHERE destino=? AND salida=? "
        "AND regreso=? AND nombre=? GROUP BY f", (d, sal, reg, nombre))}


def linea_base(hist, inicio_bf):
    pre = [p for f, p in hist.items() if f < inicio_bf]
    return st.median(pre) if pre else None


def posible_inflado(hist, inicio_bf):
    pre = [(f, p) for f, p in hist.items() if f < inicio_bf]
    if len(pre) < 8:
        return False
    ultimos = st.median([p for _, p in pre[-5:]])
    antes = st.median([p for _, p in pre[:-5]])
    return ultimos > antes * 1.10


# ----------------------------------------------------------- ranking
def candidatos(con, cfg, ahora):
    cm = cfg["calidad_minima"]
    bf = cfg["busquedas"]["inicio_black_friday"]
    hoy = ahora.date().isoformat()
    lim = (ahora - dt.timedelta(days=cfg["busquedas"]["dias_reuso_hotel"])).isoformat()
    destinos = {x["id"]: x for x in cfg["destinos"]}
    salida = []

    combos = con.execute("SELECT destino, salida, regreso, MAX(ts) ts FROM vuelos WHERE ts>=? "
                         "GROUP BY destino, salida, regreso", (lim,)).fetchall()
    for c in combos:
        if c["destino"] not in destinos or not destinos[c["destino"]].get("activo", True):
            continue
        vuelos = [dict(r) for r in con.execute("SELECT * FROM vuelos WHERE destino=? AND salida=? "
                  "AND regreso=? AND ts=?", (c["destino"], c["salida"], c["regreso"], c["ts"]))]
        buenos = [v for v in vuelos if v["escalas"] <= cm["escalas_max"]] or vuelos
        pref = cfg.get("aerolineas_preferidas") or []
        modo = cfg.get("modo_aerolinea", "no")
        if pref and modo != "no":
            preferidos = [v for v in buenos if any(p in (v["aerolinea"] or "") for p in pref)]
            if preferidos:
                buenos = preferidos
            elif modo == "solo":
                continue
        for v in buenos:
            v["puntaje"] = puntaje_vuelo(v, cfg)
        # mejor vuelo = mejor equilibrio precio/calidad entre opciones de la misma busqueda
        pmin = min(v["precio_clp"] for v in buenos)
        f = max(buenos, key=lambda v: v["puntaje"] - 100 * (v["precio_clp"] / pmin - 1))
        hoteles = [dict(r) for r in con.execute(
            "SELECT * FROM hoteles WHERE destino=? AND salida=? AND regreso=? AND ts>=? "
            "AND ts=(SELECT MAX(ts) FROM hoteles h2 WHERE h2.destino=hoteles.destino AND "
            "h2.salida=hoteles.salida AND h2.regreso=hoteles.regreso AND h2.zona=hoteles.zona)",
            (c["destino"], c["salida"], c["regreso"], lim))]
        hist_v = historial_vuelo(con, c["destino"], c["salida"], c["regreso"], cm["escalas_max"])
        for h in hoteles:
            h["puntaje"] = puntaje_hotel(h)
            ok, motivos = calidad_ok(f, h, cfg)
            total = f["precio_clp"] + h["precio_total_clp"]
            base_v = linea_base(hist_v, bf)
            base_h = linea_base(historial_hotel(con, c["destino"], c["salida"], c["regreso"], h["nombre"]), bf)
            base_total = base_v + base_h if base_v and base_h else None
            if base_total:
                desc, base_tipo = (base_total - total) / base_total * 100, "total"
            elif base_v:
                desc, base_tipo = (base_v - f["precio_clp"]) / base_v * 100, "solo vuelo"
            else:
                desc, base_tipo = None, None
            minimo_hist = min(hist_v.values()) if hist_v else None
            salida.append(dict(
                destino=c["destino"], destino_nombre=destinos[c["destino"]]["nombre"],
                pais=destinos[c["destino"]]["pais"], nota_destino=destinos[c["destino"]].get("nota"),
                salida=c["salida"], regreso=c["regreso"],
                noches=(dt.date.fromisoformat(c["regreso"]) - dt.date.fromisoformat(c["salida"])).days,
                vuelo=f, hotel=h, total_clp=total, base_total_clp=base_total, base_vuelo_clp=base_v,
                base_tipo=base_tipo, descuento_pct=None if desc is None else round(desc, 1),
                es_minimo_historico=bool(minimo_hist and f["precio_clp"] <= minimo_hist * 1.02),
                minimo_historico_vuelo=minimo_hist,
                inflado=posible_inflado(hist_v, bf), calidad_ok=ok, motivos_calidad=motivos,
                antes_bf=hoy < bf))
    _puntuar(salida, cfg)
    return sorted(salida, key=lambda x: -x["puntaje"])


def _puntuar(cands, cfg):
    if not cands:
        return
    w = cfg["pesos"]
    pmin = min(c["total_clp"] for c in cands)
    pmax = max(c["total_clp"] for c in cands)
    for c in cands:
        p_precio = 100 * (pmax - c["total_clp"]) / (pmax - pmin) if pmax > pmin else 50
        p_oferta = _clip((c["descuento_pct"] or 0) * 100 / 30)
        c["puntaje"] = round(w["precio"] * p_precio + w["vuelo"] * c["vuelo"]["puntaje"]
                             + w["hotel"] * c["hotel"]["puntaje"] + w["oferta_real"] * p_oferta, 1)
        if not c["calidad_ok"]:
            c["puntaje"] = round(c["puntaje"] * 0.6, 1)
        c["recomendacion"], c["razon"] = recomendar(c, cfg)


def recomendar(c, cfg):
    umbral = cfg["alertas"]["descuento_min_pct"]
    d = c["descuento_pct"]
    if not c["calidad_ok"]:
        return "ESPERAR", "No cumple calidad mínima: " + ", ".join(c["motivos_calidad"])
    if c["inflado"]:
        return "ESPERAR", "Precio subió antes de Black Friday: posible rebaja inflada"
    if d is not None and d >= umbral and c["es_minimo_historico"]:
        return "COMPRAR", f"{d:.0f}% bajo la línea base ({c['base_tipo']}) y mínimo histórico registrado"
    if c["vuelo"]["nivel_precio"] == "low" and c["es_minimo_historico"]:
        return "COMPRAR", "Google Flights lo marca 'bajo' y es el mínimo histórico registrado"
    if c["antes_bf"]:
        return "ESPERAR", "Registrando línea base antes de Black Friday"
    if d is not None and d < 2:
        return "ESPERAR", "Sin rebaja real vs precio base pre-Black Friday"
    if not c["es_minimo_historico"] and c["minimo_historico_vuelo"]:
        extra = (c["vuelo"]["precio_clp"] / c["minimo_historico_vuelo"] - 1) * 100
        return "ESPERAR", f"Vuelo {extra:.0f}% sobre su mínimo histórico"
    return "ESPERAR", "Descuento bajo el umbral de %d%%" % umbral


def explicar(c):
    v, h = c["vuelo"], c["hotel"]
    tipo = "directo" if v["escalas"] == 0 else f"{v['escalas']} escala(s)"
    partes = [f"Vuelo {tipo} ({v['puntaje']:.0f}/100)", f"hotel {h['nota']}/5 en {h['zona']} ({h['puntaje']:.0f}/100)"]
    if c["descuento_pct"] is not None:
        partes.append(f"{c['descuento_pct']:+.0f}% vs base".replace("+", "−", 1) if c["descuento_pct"] > 0
                      else f"{-c['descuento_pct']:.0f}% sobre base")
    return ", ".join(partes) + "."
