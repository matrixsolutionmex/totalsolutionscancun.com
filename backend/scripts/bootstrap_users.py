import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT_DIR))

from app.core.security import hash_password
from app.database.connection import Base, SessionLocal, engine
from app.models.lead import Lead
from app.models.user import User

DEFAULT_PASSWORD = "12345m*"


def upsert_user(db, username, role, full_name):
    user = db.query(User).filter(User.username == username).first()

    if user:
        user.password_hash = hash_password(DEFAULT_PASSWORD)
        user.role = role
        user.full_name = full_name
        user.is_active = True
        return user, False

    user = User(
        username=username,
        password_hash=hash_password(DEFAULT_PASSWORD),
        role=role,
        full_name=full_name,
        is_active=True,
    )
    db.add(user)
    return user, True


def main():
    Base.metadata.create_all(bind=engine)

    with SessionLocal() as db:
        root_user, root_created = upsert_user(db, "root", "ROOT", "Administrador Root")
        manager_user, manager_created = upsert_user(db, "gerente", "GERENTE", "Gerente Principal")
        broker_user, broker_created = upsert_user(db, "broker", "BROKER", "Broker Principal")
        db.commit()
        db.refresh(root_user)
        db.refresh(manager_user)
        db.refresh(broker_user)

    print(f"root: {'criado' if root_created else 'atualizado'}")
    print(f"gerente: {'criado' if manager_created else 'atualizado'}")
    print(f"broker: {'criado' if broker_created else 'atualizado'}")
    print("senha inicial: 12345m*")


if __name__ == "__main__":
    main()
