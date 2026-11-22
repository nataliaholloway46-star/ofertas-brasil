"""Base de datos SQLite con el historial de precios (datos/historial.db)."""
import sqlite3
import pathlib

RUTA = pathlib.Path(__file__).resolve().parent.parent / "datos" / "historial.db"

ESQUEMA = """
CREATE TABLE IF NOT EXISTS busquedas(
  id INTEGER PRIMARY KEY, ts TEXT, motor TEXT, detalle TEXT, ok INTEGER, error TEXT);
CREATE TABLE IF NOT EXISTS vuelos(
  id INTEGER PRIMARY KEY, ts TEXT, destino TEXT, salida TEXT, regreso TEXT,
  llegada TEXT, precio_clp REAL, aerolinea TEXT, escalas INTEGER, duracion_min INTEGER,
  escala_max_min INTEGER, escala_nocturna INTEGER, equipaje TEXT, hora_salida TEXT,
  retraso_frecuente INTEGER, nivel_precio TEXT, tipico_min REAL, tipico_max REAL,
  link TEXT, fuente TEXT, departure_token TEXT);
CREATE TABLE IF NOT EXISTS historial_google(
  destino TEXT, salida TEXT, regreso TEXT, fecha TEXT, precio_clp REAL,
  PRIMARY KEY(destino, salida, regreso, fecha));
CREATE TABLE IF NOT EXISTS hoteles(
  id INTEGER PRIMARY KEY, ts TEXT, destino TEXT, salida TEXT, regreso TEXT, zona TEXT,
  nombre TEXT, estrellas INTEGER, nota REAL, resenas INTEGER, precio_total_clp REAL,
  desayuno TEXT, cancelacion TEXT, link TEXT, fuente TEXT, property_token TEXT);
CREATE TABLE IF NOT EXISTS paquetes_correo(
  msgid TEXT PRIMARY KEY, fecha TEXT, agencia TEXT, destino TEXT, precio_clp REAL,
  base_precio TEXT, noches INTEGER, es_paquete INTEGER, desde INTEGER, link TEXT);
CREATE TABLE IF NOT EXISTS alertas(clave TEXT PRIMARY KEY, ts TEXT, precio_clp REAL);
CREATE TABLE IF NOT EXISTS eventos(tipo TEXT, fecha TEXT, PRIMARY KEY(tipo, fecha));
"""


def conectar():
    RUTA.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(RUTA)
    con.row_factory = sqlite3.Row
    con.executescript(ESQUEMA)
    return con
