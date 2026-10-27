from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import get_settings
from app.db import mongo, neo4j_db
from app.db.postgres import SessionLocal, init_postgres
from app.routers import auth, documents, schemes, system, users
from app.services.catalog import seed_catalog

settings = get_settings()

app = FastAPI(title="PAANGBA API", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(system.router)
app.include_router(auth.router)
app.include_router(users.router)
app.include_router(documents.router)
app.include_router(schemes.router)


@app.on_event("startup")
def on_startup() -> None:
    init_postgres()
    mongo.init_mongo()
    neo4j_db.init_neo4j()
    db = SessionLocal()
    try:
        seed_catalog(db)
    finally:
        db.close()
