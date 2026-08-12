from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Query

from .config import DATA_FILE
from .models import FHIRResource
from .store import store


@asynccontextmanager
async def lifespan(app: FastAPI):
    store.load_file(DATA_FILE)
    yield


app = FastAPI(
    title="Patient Records API",
    description=(
        "Makes a JSONL file of FHIR resources useful and queryable for the "
        "patient consuming their own health history. Data is held in indexed "
        "in-memory structures; see /status for the load report."
    ),
    lifespan=lifespan,
)


@app.get("/status")
def status() -> dict:
    """Load report: how much data loaded, what was skipped, and why."""
    return store.load_report

@app.get("/patients/{patient_id}/records")
def get_patient_records(
    patient_id: str,
    type: str | None = Query(None, description="Filter by FHIR resourceType"),
    q: str | None = Query(None, description="Keyword search with synonym expansion, e.g. 'heart'"),
    date_from: str | None = Query(None, alias="from", description="ISO date lower bound"),
    date_to: str | None = Query(None, alias="to", description="ISO date upper bound"),
    limit: int = Query(50, ge=1, le=50),
    offset: int = Query(0, ge=0),
):
    # NOTE (auth): with authentication, patient_id would come from the token,
    # not the path — the path param exists because auth is out of scope.
    records = store.patient_records(patient_id, rtypes=type, q=q,
                                    date_from=date_from, date_to=date_to,
                                    limit=limit, offset=offset)
    if not records and patient_id not in store.patient_index:
        raise HTTPException(status_code=404, detail=f"Unknown patient {patient_id!r}")
    return {"patient_id": patient_id, "count": len(records), "limit": limit, "offset": offset, "records": records}


@app.get("/patients/{patient_id}/medications")
def get_patient_medications(
    patient_id: str,
    status: str | None = Query(None, description="e.g. 'active', 'stopped'"),
):
    if patient_id not in store.patient_index:
        raise HTTPException(status_code=404, detail=f"Unknown patient {patient_id!r}")
    meds = store.patient_medications(patient_id, status=status)
    return {"patient_id": patient_id, "count": len(meds), "medications": meds}


@app.get("/records/{resource_type}/{record_id}")
def get_record(resource_type: str, record_id: str):
    # Keyed by (type, id) because FHIR ids are only unique per resource type.
    record = store.records.get((resource_type, record_id))
    if record is None:
        raise HTTPException(status_code=404,
                            detail=f"No {resource_type} with id {record_id!r}")
    return record

@app.post("/records", status_code=201)
def add_record(resource: FHIRResource):
    outcome = store.add(resource.model_dump())
    if outcome == "duplicate":
        # 409 over silent-200: callers should know their write was a no-op.
        raise HTTPException(
            status_code=409,
            detail=f"{resource.resourceType}/{resource.id} already exists",
        )
    return {"status": "created", "key": [resource.resourceType, resource.id]}


@app.delete("/patients/{patient_id}/records")
def wipe_patient_records(patient_id: str):
    """Patient right-to-erasure. Idempotent: wiping an unknown/already-wiped
    patient succeeds with removed=0 rather than 404 — deletion cares about
    the end state, not whether there was something to delete.

    Production notes (out of scope, ready to discuss): auth-derived patient
    identity, soft-delete grace window before hard purge, audit event
    recording the deletion without the clinical content.
    """
    removed = store.wipe_patient(patient_id)
    return {"patient_id": patient_id, "removed": removed}
   
@app.delete("/records/{resource_type}/{record_id}")
def delete_record(resource_type: str, record_id: str):
    if not store.remove((resource_type, record_id)):
        raise HTTPException(status_code=404, detail=f"No resource {resource_type} with id {record_id}.")
    return {"status" : "deleted", "key" : [resource_type, record_id]}
