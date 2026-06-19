"""
crear_usuarios.py
-----------------
Crea los usuarios iniciales del sistema.
Ejecutar UNA SOLA VEZ después del primer deploy.

Uso:
    .venv\Scripts\python.exe crear_usuarios.py
"""
import os, sys
sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__),
                                "src", "sap_bank", "interfaces", "web"))

from werkzeug.security import generate_password_hash
import db

DB_CFG = {
    "server":   "192.168.250.13",
    "database": "SAPBankAudit",
    "username": "sistemas",
    "password": os.environ.get("DB_PASSWORD", "soporte.."),
}

USUARIOS = [
    # (nombre, email, password, rol, empresa — None=todas/admin)
    ("Rolando Quispe",   "rquispe@comversa.com.bo",        "Rolando123", "admin",    None),
    ("Karen Flores",     "kflores@comversa.com.bo",         "Karen123",   "admin",    None),
    ("Alain Quispe",     "aquispe@bolivianfoods.com.bo",    "Alain123",   "operador", "Bolivian Foods"),
    ("Sissy Fernandez",  "sfernandez@bolivianfoods.com.bo", "Sissy123",   "operador", "Bolivian Foods"),
]

def get_empresa_id(nombre: str) -> int | None:
    if not nombre:
        return None
    empresas = db.get_all_empresas(DB_CFG)
    for e in empresas:
        if e["nombre"] == nombre:
            return e["id"]
    print(f"  ✗ Empresa '{nombre}' no encontrada en la BD.")
    return None

def main():
    print("Inicializando base de datos...")
    db.init_db(DB_CFG)
    print()

    for nombre, email, password, rol, empresa_nombre in USUARIOS:
        empresa_id = get_empresa_id(empresa_nombre) if empresa_nombre else None
        pw_hash    = generate_password_hash(password)
        try:
            uid = db.create_user(DB_CFG,
                nombre=nombre, email=email,
                password_hash=pw_hash, rol=rol,
                empresa_id=empresa_id)
            print(f"  ✓ {nombre} ({rol}) — ID={uid}")
        except Exception as e:
            print(f"  ✗ {nombre}: {e}")

    print()
    print("═" * 50)
    print("  Usuarios creados. Credenciales iniciales:")
    print("═" * 50)
    for nombre, email, password, rol, empresa in USUARIOS:
        print(f"  {nombre:<20} {email:<40} {password}")
    print("═" * 50)
    print("  Recordá cambiar las contraseñas después del primer login.")

if __name__ == "__main__":
    main()