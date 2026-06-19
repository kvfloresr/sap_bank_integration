"""
crear_admin.py
--------------
Crea el primer usuario administrador en la base de datos.
Ejecutar UNA SOLA VEZ después del primer deploy.

Uso:
    python crear_admin.py
    python crear_admin.py --email admin@empresa.com --nombre "Juan Pérez" --password MiClave123
"""
import argparse, os, sys
sys.path.insert(0, os.path.dirname(__file__))

from werkzeug.security import generate_password_hash
import db

DB_CFG = {
    "server":   os.environ.get("DB_SERVER",   "192.168.250.13"),
    "database": os.environ.get("DB_NAME",     "SAPBankAudit"),
    "username": os.environ.get("DB_USER",     "sa"),
    "password": os.environ.get("DB_PASSWORD", ""),
}

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--email",    default="admin@empresa.com")
    parser.add_argument("--nombre",   default="Administrador")
    parser.add_argument("--password", default="Admin123!")
    args = parser.parse_args()

    print("Inicializando base de datos...")
    db.init_db(DB_CFG)

    print(f"Creando usuario admin: {args.email}")
    pw_hash = generate_password_hash(args.password)
    try:
        uid = db.create_user(DB_CFG,
            nombre=args.nombre, email=args.email,
            password_hash=pw_hash, rol="admin", empresa_id=None)
        print(f"✓ Usuario creado con ID={uid}")
        print(f"  Email:    {args.email}")
        print(f"  Password: {args.password}")
        print(f"\n  Cambiá la contraseña después del primer login.")
    except Exception as e:
        print(f"✗ Error: {e}")

if __name__ == "__main__":
    main()