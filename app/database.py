"""数据库连接与会话管理。"""
from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.orm import declarative_base, sessionmaker

from .config import DATABASE_URL

# SQLite 需要关闭同线程检查以配合 FastAPI 的依赖注入
connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}
engine = create_engine(DATABASE_URL, connect_args=connect_args)

if DATABASE_URL.startswith("sqlite"):
    # SQLite 默认不强制外键约束，必须显式打开：
    # 否则删除被换电记录引用的站点/车辆会留下孤儿数据，而不是给出约束异常。
    @event.listens_for(engine, "connect")
    def _enable_sqlite_fk(dbapi_connection, _connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()


SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

Base = declarative_base()


def ensure_schema() -> None:
    """建表并为旧版数据库补齐新增列（轻量迁移，幂等）。"""
    Base.metadata.create_all(bind=engine)
    inspector = inspect(engine)
    new_columns = {
        "stations": {
            "is_retired": "BOOLEAN NOT NULL DEFAULT 0",
            "retired_at": "DATETIME",
            "name_history": "JSON NOT NULL DEFAULT '[]'",
        },
        "vehicles": {
            "is_retired": "BOOLEAN NOT NULL DEFAULT 0",
            "retired_at": "DATETIME",
            "name_history": "JSON NOT NULL DEFAULT '[]'",
        },
    }
    with engine.begin() as conn:
        for table, columns in new_columns.items():
            if table not in inspector.get_table_names():
                continue
            existing = {col["name"] for col in inspector.get_columns(table)}
            for column, ddl in columns.items():
                if column not in existing:
                    conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}"))


def get_db():
    """FastAPI 依赖：提供一个数据库会话，请求结束后关闭。"""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
