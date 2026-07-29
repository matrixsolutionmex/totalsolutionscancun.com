import os
from datetime import datetime

from fastapi import APIRouter, Depends, Header, HTTPException
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from app.database.connection import SessionLocal
from app.models.import_job import ImportJob
from app.schemas.import_schema import ImportJobResponse, MatrixLeadImportRequest
from app.services.import_service import import_lead_records

router = APIRouter(prefix="/imports", tags=["imports"])


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def require_matrix_token(authorization: str | None = Header(default=None)):
    expected_token = os.getenv("MATRIX_IMPORT_TOKEN")
    if not expected_token:
        raise HTTPException(status_code=503, detail="Token de importacao nao configurado")

    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Authorization Bearer obrigatorio")

    token = authorization.removeprefix("Bearer ").strip()
    if token != expected_token:
        raise HTTPException(status_code=403, detail="Token de importacao invalido")


def serialize_job(job: ImportJob):
    return ImportJobResponse.model_validate(job)


@router.post("/matrix/leads", response_model=ImportJobResponse)
def import_matrix_leads(
    payload: MatrixLeadImportRequest,
    db: Session = Depends(get_db),
    _: None = Depends(require_matrix_token),
):
    existing_job = (
        db.query(ImportJob)
        .filter(ImportJob.source == payload.source, ImportJob.batch_id == payload.batch_id)
        .first()
    )
    if existing_job:
        raise HTTPException(status_code=409, detail="batch_id ja processado para esta origem")

    job = ImportJob(
        source=payload.source,
        batch_id=payload.batch_id,
        status="RECEIVED",
        total_received=len(payload.records),
        created_at=payload.sent_at or datetime.utcnow(),
    )
    db.add(job)
    db.commit()
    db.refresh(job)

    try:
        job.status = "PROCESSING"
        job.started_at = datetime.utcnow()
        db.commit()

        stats = import_lead_records(
            db,
            [record.model_dump() for record in payload.records],
        )

        job.status = "DONE"
        job.inserted = stats.inserted
        job.skipped_duplicates = stats.skipped_duplicates
        job.invalid = stats.invalid
        job.finished_at = datetime.utcnow()
        db.commit()
        db.refresh(job)
        return serialize_job(job)
    except Exception as exc:
        db.rollback()
        failed_job = db.query(ImportJob).filter(ImportJob.id == job.id).first()
        if failed_job:
            failed_job.status = "FAILED"
            failed_job.error_message = str(exc)
            failed_job.finished_at = datetime.utcnow()
            db.commit()
            db.refresh(failed_job)
            return JSONResponse(
                status_code=500,
                content=serialize_job(failed_job).model_dump(mode="json"),
            )
        raise


@router.get("/jobs", response_model=list[ImportJobResponse])
def list_import_jobs(
    db: Session = Depends(get_db),
    _: None = Depends(require_matrix_token),
):
    jobs = db.query(ImportJob).order_by(ImportJob.created_at.desc()).limit(100).all()
    return [serialize_job(job) for job in jobs]


@router.get("/jobs/{job_id}", response_model=ImportJobResponse)
def get_import_job(
    job_id: int,
    db: Session = Depends(get_db),
    _: None = Depends(require_matrix_token),
):
    job = db.query(ImportJob).filter(ImportJob.id == job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job de importacao nao encontrado")

    return serialize_job(job)
