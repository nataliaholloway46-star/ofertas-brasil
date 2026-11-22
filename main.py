"""Monitor de ofertas Brasil 2027.

Uso:
  python main.py              ejecucion normal (decide sola si busca)
  python main.py --forzar     busca aunque ya haya buscado hoy
  python main.py --seco       NO gasta busquedas: solo recalcula panel con datos guardados
  python main.py --sin-avisos no envia Telegram/email
  python main.py --max N      limita las busquedas de esta corrida a N
"""
import argparse
import datetime as dt
import json
import logging
import os
import pathlib
import random
from zoneinfo import ZoneInfo

import yaml

from viajes import analisis, avisos, correo, db, fuentes

RAIZ = pathlib.Path(__file__).resolve().parent
CHILE = ZoneInfo("America/Santiago")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("viajes")


def cargar_env():
    env = RAIZ / ".env"
    if env.exists():
        for linea in env.read_text(encoding="utf-8").splitlines():
            if "=" in linea and not linea.strip().startswith("#"):
                k, v = linea.split("=", 1)
                if v.strip():
                    os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def todos_los_combos(cfg):
    v = cfg["viaje"]
    ini, fin = dt.date.fromisoformat(v["fecha_desde"]), dt.date.fromisoformat(v["fecha_hasta"])
    combos, d = [], ini
    while d <= fin:
        for n in range(v["noches_min"], v["noches_max"] + 1):
            r = d + dt.timedelta(days=n)
            if r <= fin:
                combos.append((d.isoformat(), r.isoformat()))
        d += dt.timedelta(days=1)
    return combos


def planificar_vuelos(con, cfg, n, hoy):
    """Elige que vuelos buscar: seguimiento fijo + exploracion rotativa."""
    activos = [x for x in cfg["destinos"] if x.get("activo", True)]
    buscados_hoy = {(r[0], r[1], r[2]) for r in con.execute(
        "SELECT destino, salida, regreso FROM vuelos WHERE substr(ts,1,10)=?", (hoy,))}
    plan = []
    for dest, fijos in (cfg.get("seguimiento_fijo") or {}).items():
        if any(x["id"] == dest for x in activos):
            plan += [(dest, s, r) for s, r in fijos if (dest, s, r) not in buscados_hoy]
    todos = todos_los_combos(cfg)
    prio1 = [x["id"] for x in activos if x.get("prioridad", 2) == 1]
    prio2 = sorted([x["id"] for x in activos if x.get("prioridad", 2) != 1], key=lambda d: (
        con.execute("SELECT MAX(ts) FROM vuelos WHERE destino=?", (d,)).fetchone()[0] or ""))
    turno = 0
    while len(plan) < n and (prio1 or prio2) and turno < 50:
        dest = (prio1[turno % len(prio1)] if prio1 and (turno % 2 == 0 or not prio2)
                else prio2.pop(0) if prio2 else prio1[0])
        turno += 1
        hechos = {(r[0], r[1]) for r in con.execute(
            "SELECT DISTINCT salida, regreso FROM vuelos WHERE destino=?", (dest,))}
        libres = [c for c in todos if c not in hechos and (dest, *c) not in plan]
        if libres:
            plan.append((dest, *random.choice(libres)))
    return plan[:n]


def planificar_hoteles(con, cfg, n, ahora):
    lim = (ahora - dt.timedelta(days=cfg["busquedas"]["dias_reuso_hotel"])).isoformat()
    filas = con.execute(
        "SELECT destino, salida, regreso, MIN(precio_clp) p FROM vuelos WHERE ts>=? "
        "GROUP BY destino, salida, regreso ORDER BY p", (lim,)).fetchall()
    prio = {x["id"]: x.get("prioridad", 2) for x in cfg["destinos"]}
    filas = sorted(filas, key=lambda r: (prio.get(r["destino"], 9), r["p"]))
    plan = []
    for r in filas:
        tiene = con.execute("SELECT 1 FROM hoteles WHERE destino=? AND salida=? AND regreso=? AND ts>=?",
                            (r["destino"], r["salida"], r["regreso"], lim)).fetchone()
        if not tiene:
            plan.append((r["destino"], r["salida"], r["regreso"]))
        if len(plan) >= n:
            break
    return plan


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--forzar", action="store_true")
    ap.add_argument("--seco", action="store_true")
    ap.add_argument("--sin-avisos", action="store_true")
    ap.add_argument("--max", type=int)
    ap.add_argument("--reporte", action="store_true", help="enviar resumen ahora (no gasta busquedas)")
    a = ap.parse_args()
    if a.reporte:
        a.max = 0

    cargar_env()
    cfg = yaml.safe_load((RAIZ / "config.yaml").read_text(encoding="utf-8"))
    con = db.conectar()
    ahora = dt.datetime.now(dt.timezone.utc)
    cl = ahora.astimezone(CHILE)
    hoy = cl.date().isoformat()
    b = cfg["busquedas"]
    intensivo = b["intensivo_desde"] <= hoy <= b["intensivo_hasta"]
    destinos = {x["id"]: x for x in cfg["destinos"]}
    estado = {"errores": [], "busquedas_usadas": 0, "modo": "intensivo" if intensivo else "normal"}

    # ------------------------------------------------ cuanto podemos gastar
    usadas_hoy = con.execute("SELECT COUNT(*) FROM busquedas WHERE ts>=?",
                             (dt.datetime.combine(cl.date(), dt.time(), CHILE)
                              .astimezone(dt.timezone.utc).isoformat(),)).fetchone()[0]
    ya_busco_hoy = con.execute("SELECT 1 FROM eventos WHERE tipo='busqueda' AND fecha=?", (hoy,)).fetchone()
    restantes_cuenta = None
    serp = fuentes.Serp(con, 0)
    if not a.seco:
        try:
            cuenta = serp.cuenta()
            restantes_cuenta = cuenta.get("total_searches_left", cuenta.get("plan_searches_left"))
        except Exception as e:
            estado["errores"].append(f"SerpApi cuenta: {type(e).__name__}")
    tope_dia = b["max_por_dia_intensivo"] if intensivo else b["max_por_dia_normal"]
    permitidas = max(0, tope_dia - usadas_hoy)
    if intensivo:
        permitidas = min(permitidas, 3)
    if restantes_cuenta is not None:
        permitidas = min(permitidas, max(0, restantes_cuenta - b["reserva_minima"]))
    if a.max is not None:
        permitidas = min(permitidas, a.max)
    if a.seco or (ya_busco_hoy and not intensivo and not a.forzar):
        permitidas = 0
    serp.permitidas = permitidas
    log.info("Modo %s | usadas hoy %s | cuenta %s | permitidas ahora %s",
             estado["modo"], usadas_hoy, restantes_cuenta, permitidas)

    # ------------------------------------------------ busquedas
    if permitidas:
        n_hot = 0 if permitidas < 2 else (2 if permitidas >= 5 else 1)
        for dest, sal, reg in planificar_vuelos(con, cfg, permitidas - n_hot, hoy):
            d = destinos[dest]
            try:
                filas, hist = fuentes.buscar_vuelos(serp, cfg["viaje"]["origen"], dest, d["aeropuertos"],
                                                    sal, reg, cfg["viaje"]["adultos"])
                for f in filas:
                    con.execute(f"INSERT INTO vuelos({','.join(f)}) VALUES({','.join('?' * len(f))})",
                                list(f.values()))
                con.executemany("INSERT OR IGNORE INTO historial_google VALUES(?,?,?,?,?)",
                                [(dest, sal, reg, fe, p) for fe, p in hist])
                con.commit()
                log.info("Vuelos %s %s→%s: %d opciones", dest, sal, reg, len(filas))
                if not filas:
                    estado["errores"].append(f"Vuelos {dest} {sal}: sin resultados")
            except fuentes.SinPresupuesto:
                break
            except Exception as e:
                estado["errores"].append(f"Vuelos {dest} {sal}: {e}"[:200])
        for dest, sal, reg in planificar_hoteles(con, cfg, permitidas - serp.usadas, ahora):
            d = destinos[dest]
            n_prev = con.execute("SELECT COUNT(DISTINCT ts) FROM hoteles WHERE destino=?", (dest,)).fetchone()[0]
            zona = d["zonas"][n_prev % len(d["zonas"])]
            try:
                filas = fuentes.buscar_hoteles(serp, dest, zona, sal, reg, cfg["viaje"]["adultos"],
                                               cfg["calidad_minima"]["hotel_estrellas_min"],
                                               mas_baratos=(n_prev // len(d["zonas"])) % 2 == 0)
                for f in filas:
                    con.execute(f"INSERT INTO hoteles({','.join(f)}) VALUES({','.join('?' * len(f))})",
                                list(f.values()))
                con.commit()
                log.info("Hoteles %s (%s) %s→%s: %d", dest, zona, sal, reg, len(filas))
            except fuentes.SinPresupuesto:
                break
            except Exception as e:
                estado["errores"].append(f"Hoteles {dest} {zona}: {e}"[:200])
        con.execute("INSERT OR IGNORE INTO eventos VALUES('busqueda', ?)", (hoy,))
        con.commit()
    estado["busquedas_usadas"] = serp.usadas

    # ------------------------------------------------ analisis
    cands = analisis.candidatos(con, cfg, ahora)
    tope = cfg["viaje"].get("presupuesto_max_clp")
    if tope:
        cands = [c for c in cands if c["total_clp"] <= tope]
    for c in cands:
        c["explicacion"] = analisis.explicar(c)
        c["verificado"] = False
    usd, usd_fecha, usd_fuente = fuentes.dolar_clp()

    # ------------------------------------------------ paquetes REALES desde correos (opcion A)
    cc = cfg.get("correos") or {}
    if not a.seco:
        ofertas, err = correo.leer_ofertas(cfg, dias=3)
        estado["gmail"] = err or f"ok ({len(ofertas)} ofertas leídas)"
        if err and err != "Gmail no configurado":
            estado["errores"].append(err)
        for o in ofertas:
            con.execute(f"INSERT OR IGNORE INTO paquetes_correo({','.join(o)}) VALUES({','.join('?' * len(o))})",
                        list(o.values()))
        con.commit()
    vig = (cl.date() - dt.timedelta(days=cc.get("dias_vigencia", 7))).isoformat()
    paquetes = []
    for r in con.execute("SELECT * FROM paquetes_correo WHERE fecha>=? AND es_paquete=1", (vig,)):
        p = dict(r)
        p["destino_nombre"] = destinos.get(p["destino"], {}).get("nombre", p["destino"])
        # Si el correo no dice "por persona" ni "total", se asume por persona (costumbre en Chile)
        p["por_persona_clp"] = p["precio_clp"] / cfg["viaje"]["adultos"] if p["base_precio"] == "total" \
            else p["precio_clp"]
        paquetes.append(p)
    paquetes.sort(key=lambda p: p["por_persona_clp"])
    # Un vuelo+hotel separado (opcion B) solo "iguala" si cuesta lo mismo o menos que el
    # paquete real mas barato del MISMO destino. Sin paquete real -> tope del objetivo.
    obj = cfg.get("objetivo") or {}
    ref_dest = {}
    for p in paquetes:
        ref_dest.setdefault(p["destino"], p["por_persona_clp"])
    ref_pp = paquetes[0]["por_persona_clp"] if paquetes else obj.get("por_persona_max")
    for c in cands:
        ref_c = ref_dest.get(c["destino"], obj.get("por_persona_max"))
        c["referencia_pp"] = ref_c
        c["iguala_paquete"] = bool(ref_c) and c["por_persona_clp"] <= ref_c

    envios = []
    if not a.seco and not a.sin_avisos:
        # Paquetes reales de correo dentro del precio objetivo (sin repetir)
        tope_paq = cc.get("alerta_por_persona_max", obj.get("por_persona_max"))
        for p in [p for p in paquetes if tope_paq and p["por_persona_clp"] <= tope_paq][:3]:
            clave = "correo|" + p["msgid"]
            if con.execute("SELECT 1 FROM alertas WHERE clave=?", (clave,)).fetchone():
                continue
            envios.append(avisos.enviar(f"Paquete {p['destino_nombre']}", avisos.texto_paquete(p, usd)))
            con.execute("INSERT OR REPLACE INTO alertas VALUES(?,?,?)", (clave, ahora.isoformat(), p["precio_clp"]))
            con.commit()
        # Solo vuelo + hotel que iguale al mejor paquete y cumpla calidad (nunca solo vuelos)
        for c in [c for c in cands if c["calidad_ok"] and c["iguala_paquete"]][:2]:
            clave = f"{c['destino']}|{c['salida']}|{c['regreso']}|{c['hotel']['nombre']}"
            prev = con.execute("SELECT precio_clp FROM alertas WHERE clave=?", (clave,)).fetchone()
            extra = cfg["alertas"]["rebaja_extra_para_repetir_pct"] / 100
            if prev and c["total_clp"] > prev[0] * (1 - extra):
                continue
            verif = fuentes.Serp(con, 3 if (restantes_cuenta or 0) > 3 else 0)
            try:
                v, h = c["vuelo"], c["hotel"]
                fv, tv = fuentes.verificar_vuelo(verif, cfg["viaje"]["origen"], v["llegada"], v["salida"],
                                                 v["regreso"], cfg["viaje"]["adultos"],
                                                 v["departure_token"], v["precio_clp"])
                fh, th = fuentes.verificar_hotel(verif, h["zona"], h["salida"], h["regreso"],
                                                 cfg["viaje"]["adultos"], h["property_token"],
                                                 h["precio_total_clp"])
                c["verificado"] = len(fv) >= 1 and len(fh) >= 2
                c["verificacion"] = f"vuelo: {tv}; hotel: {th}"
            except Exception as e:
                c["verificacion"] = f"no se pudo verificar ({str(e)[:80]})"
            estado["busquedas_usadas"] += verif.usadas
            if c["verificado"]:
                envios.append(avisos.enviar(f"Oferta {c['destino_nombre']}", avisos.texto_alerta(c, usd)))
                con.execute("INSERT OR REPLACE INTO alertas VALUES(?,?,?)",
                            (clave, ahora.isoformat(), c["total_clp"]))
                con.commit()

        # resumen diario
        hecho = con.execute("SELECT 1 FROM eventos WHERE tipo='resumen' AND fecha=?", (hoy,)).fetchone()
        if (cl.hour >= cfg["alertas"]["hora_resumen_chile"] and not hecho) or a.reporte:
            lineas = [f"☀️ <b>Resumen {cl:%d-%m}</b> · {estado['busquedas_usadas']} búsquedas · "
                      f"{len(estado['errores'])} errores"]
            if paquetes:
                p = paquetes[0]
                lineas.append(f"📦 Mejor paquete real (correo {p['agencia']}, {p['fecha']}): "
                              f"{p['destino_nombre']} {avisos.clp(p['por_persona_clp'])} por persona"
                              f"{' · ' + str(p['noches']) + ' noches' if p['noches'] else ''}"
                              f"{' (precio desde)' if p['desde'] else ''}")
            else:
                lineas.append("📦 Sin paquetes nuevos en tus correos de ofertas.")
            buenos = [c for c in cands if c["calidad_ok"]]
            if buenos:
                c = min(buenos, key=lambda x: x["por_persona_clp"])
                dif = c["por_persona_clp"] - (c["referencia_pp"] or 0)
                estado_txt = "✅ iguala o mejora al paquete" if c["iguala_paquete"] else \
                    f"❌ {avisos.clp(dif)} por persona sobre el paquete"
                lineas.append(f"🏨✈️ Mejor vuelo + hotel separado: {avisos.clp(c['por_persona_clp'])} por persona · "
                              f"{c['noches']} noches · {c['vuelo']['aerolinea']} + {c['hotel']['nombre']} "
                              f"{c['hotel']['estrellas']}★ → {estado_txt}")
            else:
                lineas.append("Aún sin combinaciones vuelo + hotel que cumplan calidad.")
            lineas.append("👉 Recomendación: paquete, salvo que el separado lo iguale.")
            envios.append(avisos.enviar("Resumen viaje Brasil", "\n".join(lineas)))
            con.execute("INSERT OR IGNORE INTO eventos VALUES('resumen', ?)", (hoy,))
            con.commit()
    estado["envios"] = envios

    # ------------------------------------------------ datos del panel
    series = {}
    for r in con.execute("SELECT destino, substr(ts,1,10) f, MIN(precio_clp) p FROM vuelos "
                         "GROUP BY destino, f ORDER BY f"):
        series.setdefault(r["destino"], []).append([r["f"], r["p"]])
    seguimiento = {}
    for dest, fijos in (cfg.get("seguimiento_fijo") or {}).items():
        for s, r in fijos:
            h = analisis.historial_vuelo(con, dest, s, r)
            if h:
                seguimiento[f"{dest} {s}→{r}"] = list(h.items())
    datos = {
        "paquetes_correo": paquetes,
        "dias_vigencia_correo": cc.get("dias_vigencia", 7),
        "objetivo": cfg.get("objetivo"),
        "generado": ahora.isoformat(timespec="seconds"), "generado_chile": cl.strftime("%d-%m-%Y %H:%M"),
        "estado": estado, "usd_clp": usd, "usd_fecha": usd_fecha, "usd_fuente": usd_fuente,
        "busquedas_restantes_cuenta": restantes_cuenta,
        "inicio_black_friday": b["inicio_black_friday"],
        "destinos": [{"id": x["id"], "nombre": x["nombre"], "pais": x["pais"]}
                     for x in cfg["destinos"] if x.get("activo", True)],
        "candidatos": cands[:60], "series_destino": series, "series_seguimiento": seguimiento,
        "total_vuelos_guardados": con.execute("SELECT COUNT(*) FROM vuelos").fetchone()[0],
        "total_hoteles_guardados": con.execute("SELECT COUNT(*) FROM hoteles").fetchone()[0],
    }
    salida = RAIZ / "docs" / "data.json"
    salida.parent.mkdir(exist_ok=True)
    salida.write_text(json.dumps(datos, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    log.info("Panel actualizado: %d candidatos, %d errores", len(cands), len(estado["errores"]))
    for e in estado["errores"]:
        log.warning(e)


if __name__ == "__main__":
    main()
