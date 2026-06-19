"""
app.py — Flask multiempresa con login, roles y auditoría SQL Server
Roles: admin (todo), operador (solo su empresa)
"""
from __future__ import annotations
import logging, os, sys, tempfile, uuid
from functools import wraps

import yaml
from flask import (Flask, jsonify, redirect, render_template,
                request, send_file, session, url_for, flash)
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename

ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "../../../.."))
WEB_DIR  = os.path.dirname(__file__)
sys.path.insert(0, ROOT_DIR)
sys.path.insert(0, WEB_DIR)

from src.sap_bank.application.processor import parse_row, process_rows
from src.sap_bank.domain.payment_builder import build_payload
from src.sap_bank.infrastructure.excel_reader import read_payments
from src.sap_bank.infrastructure.report_writer import write_report
from src.sap_bank.infrastructure.sap_client import SapClient
import db

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("web")

# ── App ────────────────────────────────────────────────────────
app = Flask(__name__)
app.secret_key = os.environ.get("SAP_WEB_SECRET", "cambiar-en-produccion")
app.config["MAX_CONTENT_LENGTH"] = 20 * 1024 * 1024

UPLOAD_DIR = os.path.join(tempfile.gettempdir(), "sap_bank_uploads")
os.makedirs(UPLOAD_DIR, exist_ok=True)

# ── Config SQL Server (desde variables de entorno) ─────────────
def get_db_cfg() -> dict:
    return {
        "server":   os.environ.get("DB_SERVER",   "192.168.250.13"),
        "database": os.environ.get("DB_NAME",     "SAPBankAudit"),
        "username": os.environ.get("DB_USER",     "sistemas"),
        "password": os.environ.get("DB_PASSWORD", "soporte.."),
    }

# Inicializar BD al arrancar
with app.app_context():
    try:
        db.init_db(get_db_cfg())
        log.info("Base de datos lista.")
    except Exception as e:
        log.error("Error conectando a SQL Server: %s", e)


# ── Helpers de sesión ──────────────────────────────────────────
def get_current_user() -> dict | None:
    uid = session.get("user_id")
    if not uid:
        return None
    try:
        return db.get_user_by_id(get_db_cfg(), uid)
    except Exception:
        # Fallback a datos en sesión si BD no responde
        return {
            "id":             session.get("user_id"),
            "nombre":         session.get("user_nombre", ""),
            "rol":            session.get("user_rol", "operador"),
            "empresa_id":     session.get("empresa_id"),
            "empresa_nombre": session.get("empresa_nombre", ""),
            "email":          "",
            "activo":         True,
        }

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("user_id"):
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return decorated

def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        # Usar rol de sesión directamente — más rápido y confiable
        if session.get("user_rol") != "admin":
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return decorated

def load_config(config_file: str) -> dict:
    # Si es ruta relativa, resolverla desde la raiz del proyecto
    if not os.path.isabs(config_file):
        # Normalizar separadores para Windows y Linux
        config_file = os.path.normpath(os.path.join(ROOT_DIR, config_file))
    with open(config_file, encoding="utf-8") as fh:
        return yaml.safe_load(fh)


# ── Login / Logout ─────────────────────────────────────────────
@app.route("/login", methods=["GET", "POST"])
def login():
    if session.get("user_id"):
        return redirect(url_for("index"))
    error = None
    if request.method == "POST":
        email    = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        user = db.get_user_by_email(get_db_cfg(), email)
        if not user or not user["activo"]:
            error = "Usuario no encontrado o inactivo."
        elif not check_password_hash(user["password_hash"], password):
            error = "Contraseña incorrecta."
        else:
            session.clear()
            session["user_id"]     = user["id"]
            session["user_nombre"] = user["nombre"]
            session["user_rol"]    = user["rol"]
            log.info("Login: %s (%s)", user["email"], user["rol"])
            return redirect(url_for("index"))
    return render_template("login.html", error=error)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# ── Index principal ────────────────────────────────────────────
@app.route("/")
@login_required
def index():
    user = get_current_user()
    db_cfg = get_db_cfg()
    try:
        if user["rol"] == "admin":
            empresas = db.get_all_empresas(db_cfg)
        else:
            if not user["empresa_id"]:
                return render_template("error.html",
                    msg="Tu usuario no tiene empresa asignada. Contactá al administrador.")
            emp = db.get_empresa(db_cfg, user["empresa_id"])
            empresas = [emp] if emp else []
    except Exception as e:
        log.error("Error cargando empresas: %s", e)
        empresas = []
    return render_template("index.html", empresas=empresas, user=user)


# ── Admin: gestión de usuarios ─────────────────────────────────
@app.route("/admin/usuarios")
@login_required
@admin_required
def admin_usuarios():
    db_cfg = get_db_cfg()
    usuarios  = db.get_all_users(db_cfg)
    empresas  = db.get_all_empresas(db_cfg)
    user      = get_current_user()
    return render_template("admin_usuarios.html",
                        usuarios=usuarios, empresas=empresas, user=user)


@app.route("/admin/usuarios/nuevo", methods=["POST"])
@login_required
@admin_required
def admin_crear_usuario():
    db_cfg     = get_db_cfg()
    nombre     = request.form.get("nombre", "").strip()
    email      = request.form.get("email", "").strip().lower()
    password   = request.form.get("password", "")
    rol        = request.form.get("rol", "operador")
    empresa_id = request.form.get("empresa_id") or None
    if empresa_id:
        empresa_id = int(empresa_id)
    if not nombre or not email or not password:
        flash("Todos los campos son obligatorios.", "error")
        return redirect(url_for("admin_usuarios"))
    pw_hash = generate_password_hash(password)
    try:
        db.create_user(db_cfg, nombre, email, pw_hash, rol, empresa_id)
        flash(f"Usuario {nombre} creado correctamente.", "ok")
    except Exception as e:
        flash(f"Error al crear usuario: {e}", "error")
    return redirect(url_for("admin_usuarios"))


@app.route("/admin/usuarios/<int:uid>/editar", methods=["POST"])
@login_required
@admin_required
def admin_editar_usuario(uid: int):
    db_cfg     = get_db_cfg()
    nombre     = request.form.get("nombre", "").strip()
    email      = request.form.get("email", "").strip().lower()
    rol        = request.form.get("rol", "operador")
    empresa_id = request.form.get("empresa_id") or None
    if empresa_id:
        empresa_id = int(empresa_id)
    activo     = request.form.get("activo") == "1"
    password   = request.form.get("password", "").strip()
    pw_hash    = generate_password_hash(password) if password else None
    try:
        db.update_user(db_cfg, uid, nombre, email, rol, empresa_id, activo, pw_hash)
        flash(f"Usuario actualizado correctamente.", "ok")
    except Exception as e:
        flash(f"Error al actualizar usuario: {e}", "error")
    return redirect(url_for("admin_usuarios"))


# ── Admin: historial ───────────────────────────────────────────
@app.route("/admin/historial")
@login_required
@admin_required
def admin_historial():
    db_cfg   = get_db_cfg()
    empresa_id = request.args.get("empresa_id", type=int)
    historial  = db.get_historial(db_cfg, empresa_id=empresa_id)
    empresas   = db.get_all_empresas(db_cfg)
    user       = get_current_user()
    return render_template("admin_historial.html",
                        historial=historial, empresas=empresas,
                        empresa_id_sel=empresa_id, user=user)


# ── API: preview ───────────────────────────────────────────────
@app.route("/api/preview", methods=["POST"])
@login_required
def preview():
    user = get_current_user()
    db_cfg = get_db_cfg()

    if "file" not in request.files:
        return jsonify({"error": "No se recibió ningún archivo."}), 400

    file       = request.files["file"]
    empresa_id = request.form.get("empresa_id", type=int)

    if not file.filename or not file.filename.lower().endswith(".xlsx"):
        return jsonify({"error": "El archivo debe ser .xlsx"}), 400

    # Verificar que el operador solo acceda a su empresa
    if user["rol"] != "admin":
        if not empresa_id or empresa_id != user["empresa_id"]:
            return jsonify({"error": "No tenés acceso a esa empresa."}), 403

    empresa = db.get_empresa(db_cfg, empresa_id)
    if not empresa:
        return jsonify({"error": "Empresa no válida."}), 400

    file_id    = uuid.uuid4().hex
    safe_name  = secure_filename(file.filename)
    saved_path = os.path.join(UPLOAD_DIR, f"{file_id}__{safe_name}")
    file.save(saved_path)

    try:
        raw_rows = read_payments(saved_path)
        cfg      = load_config(empresa["config_file"])
    except Exception as exc:
        os.remove(saved_path)
        return jsonify({"error": f"No se pudo leer el archivo: {exc}"}), 400

    if not raw_rows:
        os.remove(saved_path)
        return jsonify({"error": "El archivo no tiene filas válidas."}), 400

    accounts_cfg = cfg["accounts"]
    preview_rows = []
    for idx, raw in enumerate(raw_rows, start=2):
        try:
            row = parse_row(raw, idx)
            build_payload(row, accounts_cfg)
            preview_rows.append({
                "linea": idx, "fecha": row.fecha,
                "tipo": row.tipo_pago.value, "monto": row.monto,
                "descripcion": row.descripcion,
                "cuenta_banco": row.cuenta_banco or "",
                "cuenta_destino": row.cuenta_destino,
                "centro_costo": row.centro_costo or "",
                "valido": True, "error": "",
            })
        except ValueError as exc:
            preview_rows.append({
                "linea": idx, "fecha": raw.get("fecha", ""),
                "tipo": raw.get("tipo_pago", ""), "monto": raw.get("monto", ""),
                "descripcion": raw.get("descripcion", ""),
                "cuenta_banco": raw.get("cuenta_banco", ""),
                "cuenta_destino": raw.get("cuenta_destino", ""),
                "centro_costo": raw.get("centro_costo", ""),
                "valido": False, "error": str(exc),
            })

    session["file_id"]    = file_id
    session["file_path"]  = saved_path
    session["file_name"]  = file.filename
    session["empresa_id"] = empresa_id

    validas     = sum(1 for r in preview_rows if r["valido"])
    total_monto = sum(r["monto"] for r in preview_rows
                    if r["valido"] and isinstance(r["monto"], (int, float)))

    return jsonify({
        "file_name":   file.filename,
        "empresa":     empresa["nombre"],
        "empresa_id":  empresa["id"],
        "rows":        preview_rows,
        "total":       len(preview_rows),
        "validas":     validas,
        "invalidas":   len(preview_rows) - validas,
        "total_monto": total_monto,
    })


# ── API: process ───────────────────────────────────────────────
@app.route("/api/process", methods=["POST"])
@login_required
def process():
    user       = get_current_user()
    db_cfg     = get_db_cfg()
    saved_path = session.get("file_path")
    file_name  = session.get("file_name", "archivo.xlsx")
    empresa_id = session.get("empresa_id")

    if not saved_path or not os.path.exists(saved_path):
        return jsonify({"error": "No hay archivo en sesión. Subí el archivo de nuevo."}), 400

    # Verificar acceso
    if user["rol"] != "admin" and empresa_id != user["empresa_id"]:
        return jsonify({"error": "No tenés acceso a esa empresa."}), 403

    empresa = db.get_empresa(db_cfg, empresa_id)
    if not empresa:
        return jsonify({"error": "Empresa no válida."}), 400

    cfg          = load_config(empresa["config_file"])
    accounts_cfg = cfg["accounts"]
    idem_cfg     = cfg.get("idempotency", {})
    max_workers  = int(cfg.get("concurrency", {}).get("max_workers", 6))
    reports_dir  = cfg["paths"]["reports"]

    client = SapClient(cfg["sap"], cfg.get("retry", {}))
    try:
        client.ensure_session()
    except Exception as exc:
        return jsonify({"error": f"No se pudo conectar a SAP: {exc}"}), 502

    try:
        raw_rows = read_payments(saved_path)
        summary  = process_rows(
            raw_rows=raw_rows, source_name=file_name,
            client=client, accounts_cfg=accounts_cfg,
            max_workers=max_workers, idem_cfg=idem_cfg,
        )
        os.makedirs(reports_dir, exist_ok=True)
        report_path = write_report(summary, reports_dir)
    except Exception as exc:
        log.exception("Error durante el procesamiento.")
        return jsonify({"error": f"Error procesando: {exc}"}), 500
    finally:
        try: client.logout()
        except Exception: pass

    # Registrar en historial
    ip = request.headers.get("X-Forwarded-For", request.remote_addr)
    try:
        db.registrar_carga(
            db_cfg,
            usuario_id=user["id"], usuario_nombre=user["nombre"],
            empresa_id=empresa["id"], empresa_nombre=empresa["nombre"],
            archivo=file_name, ip=ip,
            total=summary.total, exitos=summary.exitos,
            errores=summary.errores, observados=summary.observados,
            omitidos=summary.omitidas,
            resultado="OK" if summary.all_success else "CON INCIDENCIAS",
        )
    except Exception as e:
        log.warning("No se pudo registrar en historial: %s", e)

    session["report_path"] = report_path

    return jsonify({
        "empresa":    empresa["nombre"],
        "resultado":  "OK" if summary.all_success else "CON INCIDENCIAS",
        "total":      summary.total,
        "exitos":     summary.exitos,
        "errores":    summary.errores,
        "observados": summary.observados,
        "omitidas":   summary.omitidas,
        "resultados": [{
            "linea":     r.line_num, "estado": r.status.value,
            "doc_entry": r.doc_entry, "doc_num": r.doc_num,
            "cuenta":    r.cuenta_destino, "error": r.error,
        } for r in summary.results],
    })


@app.route("/api/report")
@login_required
def download_report():
    report_path = session.get("report_path")
    if not report_path or not os.path.exists(report_path):
        return jsonify({"error": "No hay reporte disponible."}), 404
    return send_file(report_path, as_attachment=True,
                    download_name=os.path.basename(report_path))


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)