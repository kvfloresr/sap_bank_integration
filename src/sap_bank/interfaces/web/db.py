from __future__ import annotations
import logging
from datetime import datetime
from typing import Optional
import pyodbc

logger = logging.getLogger(__name__)

def get_connection(cfg: dict):
    cs = (
        f"DRIVER={{ODBC Driver 17 for SQL Server}};"
        f"SERVER=192.168.250.13;"
        f"DATABASE={cfg['database']};"
        f"UID=sistemas;"
        f"PWD={cfg['password']};"
        f"TrustServerCertificate=yes;"
        f"Encrypt=yes;"
    )
    return pyodbc.connect(cs)


# ── Inicializar tablas ─────────────────────────────────────────
INIT_SQL = """
IF NOT EXISTS (SELECT * FROM sysobjects WHERE name='empresas' AND xtype='U')
CREATE TABLE empresas (
    id          INT IDENTITY(1,1) PRIMARY KEY,
    nombre      NVARCHAR(100) NOT NULL,
    config_file NVARCHAR(200) NOT NULL,
    activa      BIT DEFAULT 1
);

IF NOT EXISTS (SELECT * FROM sysobjects WHERE name='usuarios' AND xtype='U')
CREATE TABLE usuarios (
    id            INT IDENTITY(1,1) PRIMARY KEY,
    nombre        NVARCHAR(100) NOT NULL,
    email         NVARCHAR(150) NOT NULL UNIQUE,
    password_hash NVARCHAR(256) NOT NULL,
    rol           NVARCHAR(20)  NOT NULL DEFAULT 'operador',
    empresa_id    INT NULL REFERENCES empresas(id),
    activo        BIT DEFAULT 1,
    creado_en     DATETIME DEFAULT GETDATE()
);

IF NOT EXISTS (SELECT * FROM sysobjects WHERE name='historial_cargas' AND xtype='U')
CREATE TABLE historial_cargas (
    id             INT IDENTITY(1,1) PRIMARY KEY,
    usuario_id     INT NOT NULL REFERENCES usuarios(id),
    usuario_nombre NVARCHAR(100) NOT NULL,
    empresa_id     INT NOT NULL REFERENCES empresas(id),
    empresa_nombre NVARCHAR(100) NOT NULL,
    archivo        NVARCHAR(300) NOT NULL,
    fecha          DATETIME DEFAULT GETDATE(),
    ip             NVARCHAR(50),
    total_filas    INT DEFAULT 0,
    exitos         INT DEFAULT 0,
    errores        INT DEFAULT 0,
    observados     INT DEFAULT 0,
    omitidos       INT DEFAULT 0,
    resultado      NVARCHAR(20)
);
"""

SEED_SQL = """
IF NOT EXISTS (SELECT 1 FROM empresas WHERE config_file = 'config/config.yaml')
    INSERT INTO empresas (nombre, config_file) VALUES ('Los Tajibos', 'config/config.yaml');

IF NOT EXISTS (SELECT 1 FROM empresas WHERE config_file = 'config/config_bk.yaml')
    INSERT INTO empresas (nombre, config_file) VALUES ('Bolivian Foods', 'config/config_bk.yaml');
"""


def init_db(cfg: dict):
    """Crea las tablas si no existen y siembra empresas base."""
    conn = get_connection(cfg)
    cur = conn.cursor()
    for stmt in INIT_SQL.strip().split(";\n\n"):
        s = stmt.strip()
        if s:
            cur.execute(s)
    for stmt in SEED_SQL.strip().split(";\n\n"):
        s = stmt.strip()
        if s:
            cur.execute(s)
    conn.commit()
    conn.close()
    logger.info("Base de datos inicializada correctamente.")


# ── Empresas ───────────────────────────────────────────────────
def get_all_empresas(cfg: dict) -> list[dict]:
    conn = get_connection(cfg)
    cur = conn.cursor()
    cur.execute("SELECT id, nombre, config_file FROM empresas WHERE activa=1 ORDER BY nombre")
    rows = [{"id": r[0], "nombre": r[1], "config_file": r[2]} for r in cur.fetchall()]
    conn.close()
    return rows


def get_empresa(cfg: dict, empresa_id: int) -> Optional[dict]:
    conn = get_connection(cfg)
    cur = conn.cursor()
    cur.execute("SELECT id, nombre, config_file FROM empresas WHERE id=? AND activa=1", empresa_id)
    r = cur.fetchone()
    conn.close()
    return {"id": r[0], "nombre": r[1], "config_file": r[2]} if r else None


# ── Usuarios ───────────────────────────────────────────────────
def get_user_by_email(cfg: dict, email: str) -> Optional[dict]:
    conn = get_connection(cfg)
    cur = conn.cursor()
    cur.execute("""
        SELECT u.id, u.nombre, u.email, u.password_hash, u.rol,
            u.empresa_id, e.nombre as empresa_nombre, u.activo
        FROM usuarios u
        LEFT JOIN empresas e ON u.empresa_id = e.id
        WHERE u.email = ?
    """, email)
    r = cur.fetchone()
    conn.close()
    if not r:
        return None
    return {
        "id": r[0], "nombre": r[1], "email": r[2],
        "password_hash": r[3], "rol": r[4],
        "empresa_id": r[5], "empresa_nombre": r[6], "activo": r[7],
    }


def get_user_by_id(cfg: dict, user_id: int) -> Optional[dict]:
    conn = get_connection(cfg)
    cur = conn.cursor()
    cur.execute("""
        SELECT u.id, u.nombre, u.email, u.password_hash, u.rol,
            u.empresa_id, e.nombre as empresa_nombre, u.activo
        FROM usuarios u
        LEFT JOIN empresas e ON u.empresa_id = e.id
        WHERE u.id = ?
    """, user_id)
    r = cur.fetchone()
    conn.close()
    if not r:
        return None
    return {
        "id": r[0], "nombre": r[1], "email": r[2],
        "password_hash": r[3], "rol": r[4],
        "empresa_id": r[5], "empresa_nombre": r[6], "activo": r[7],
    }


def get_all_users(cfg: dict) -> list[dict]:
    conn = get_connection(cfg)
    cur = conn.cursor()
    cur.execute("""
        SELECT u.id, u.nombre, u.email, u.rol,
            u.empresa_id, e.nombre as empresa_nombre,
            u.activo, u.creado_en
        FROM usuarios u
        LEFT JOIN empresas e ON u.empresa_id = e.id
        ORDER BY u.creado_en DESC
    """)
    rows = [{
        "id": r[0], "nombre": r[1], "email": r[2], "rol": r[3],
        "empresa_id": r[4], "empresa_nombre": r[5] or "—",
        "activo": r[6], "creado_en": r[7],
    } for r in cur.fetchall()]
    conn.close()
    return rows


def create_user(cfg: dict, nombre: str, email: str, password_hash: str,
                rol: str, empresa_id: Optional[int]) -> int:
    conn = get_connection(cfg)
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO usuarios (nombre, email, password_hash, rol, empresa_id)
        OUTPUT INSERTED.id
        VALUES (?, ?, ?, ?, ?)
    """, nombre, email, password_hash, rol, empresa_id)
    new_id = cur.fetchone()[0]
    conn.commit()
    conn.close()
    return new_id


def update_user(cfg: dict, user_id: int, nombre: str, email: str,
                rol: str, empresa_id: Optional[int],
                activo: bool, password_hash: Optional[str] = None):
    conn = get_connection(cfg)
    cur = conn.cursor()
    if password_hash:
        cur.execute("""
            UPDATE usuarios SET nombre=?, email=?, rol=?, empresa_id=?,
                                activo=?, password_hash=?
            WHERE id=?
        """, nombre, email, rol, empresa_id, activo, password_hash, user_id)
    else:
        cur.execute("""
            UPDATE usuarios SET nombre=?, email=?, rol=?, empresa_id=?, activo=?
            WHERE id=?
        """, nombre, email, rol, empresa_id, activo, user_id)
    conn.commit()
    conn.close()


# ── Historial ──────────────────────────────────────────────────
def registrar_carga(cfg: dict, usuario_id: int, usuario_nombre: str,
                    empresa_id: int, empresa_nombre: str, archivo: str,
                    ip: str, total: int, exitos: int, errores: int,
                    observados: int, omitidos: int, resultado: str):
    conn = get_connection(cfg)
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO historial_cargas
            (usuario_id, usuario_nombre, empresa_id, empresa_nombre,
            archivo, ip, total_filas, exitos, errores, observados, omitidos, resultado)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, usuario_id, usuario_nombre, empresa_id, empresa_nombre,
        archivo, ip, total, exitos, errores, observados, omitidos, resultado)
    conn.commit()
    conn.close()


def get_historial(cfg: dict, empresa_id: Optional[int] = None,
                limit: int = 200) -> list[dict]:
    conn = get_connection(cfg)
    cur = conn.cursor()
    if empresa_id:
        cur.execute("""
            SELECT TOP (?) id, usuario_nombre, empresa_nombre, archivo,
                        fecha, ip, total_filas, exitos, errores,
                        observados, omitidos, resultado
            FROM historial_cargas WHERE empresa_id=?
            ORDER BY fecha DESC
        """, limit, empresa_id)
    else:
        cur.execute("""
            SELECT TOP (?) id, usuario_nombre, empresa_nombre, archivo,
                        fecha, ip, total_filas, exitos, errores,
                        observados, omitidos, resultado
            FROM historial_cargas ORDER BY fecha DESC
        """, limit)
    rows = [{
        "id": r[0], "usuario": r[1], "empresa": r[2], "archivo": r[3],
        "fecha": r[4].strftime("%Y-%m-%d %H:%M") if r[4] else "",
        "ip": r[5], "total": r[6], "exitos": r[7], "errores": r[8],
        "observados": r[9], "omitidos": r[10], "resultado": r[11],
    } for r in cur.fetchall()]
    conn.close()
    return rows