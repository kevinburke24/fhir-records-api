# Patient Records API

An API that makes a JSONL file of FHIR resources useful and queryable —
designed around the patient consuming their own consolidated health history.

## Run

```bash
# with the included sample data
make docker                      # or: docker compose up --build

# with your own JSONL
docker build -t records-api .
docker run -p 8000:8000 \
  -v $(pwd)/your-data.jsonl:/data/records.jsonl:ro \
  -e DATA_FILE=/data/records.jsonl \
  records-api

# then
open http://localhost:8000/docs      # interactive API docs
curl http://localhost:8000/status    # load report
```

Local dev without Docker: `pip install -r requirements-dev.txt && make run`.
Tests: `make test`.

**Note:** place the provided sample file at `data/records.jsonl` before
building, or mount any file via the `-v`/`DATA_FILE` pattern above.

## Endpoints

| Method | Path | Purpose |
|---|---|---|
| GET | `/status` | Load report: counts, skipped lines, unknown types |
| GET | `/patients/{id}/records?type=&q=&from=&to=&offset=&limit=` | Patient's records; filter by type/date, keyword search with synonym expansion, pagination |
| GET | `/records/{type}/{id}` | Single record (keyed by type+id: FHIR ids are only unique per type) |
| GET | `/patients/{id}/medications?status=` | Medication list |
| POST | `/records` | Add a record; validated, deduplicated (409), indexes updated |
| DELETE | `/patients/{id}/records` | Wipe a patient's data (right to erasure); idempotent |
| DELETE | `/records/{type}/{id}` | Delete a patient's record; idempotent |

## API examples

All examples assume the server is running on `localhost:8000`. Patient and record
ids are from the included sample data. Pipe to `jq` for readable output if you have it.

### Load report — what parsed, and what didn't

```bash
curl -s localhost:8000/status
```

Reports records loaded, malformed lines skipped, resource types found (including
`ClinicalNote`, which isn't a standard FHIR type), and records with no resolvable
patient.

### Browse a patient's records

```bash
# Newest first, paginated (default limit 50)
curl -s "localhost:8000/patients/patrick-ball/records?limit=5"

# Filter by resource type
curl -s "localhost:8000/patients/patrick-ball/records?type=Condition"

# Filter by date range
curl -s "localhost:8000/patients/patrick-ball/records?from=2020-01-01&to=2022-12-31"
```

### Search a patient's history

Query terms expand through a synonym map, so patients can search using their own vocabulary. Unmapped terms fall back to literal keyword matching.

```bash
# "heart" matches records that say "coronary"
curl -s "localhost:8000/patients/patrick-ball/records?q=heart"

# Search combined with the other filters
curl -s "localhost:8000/patients/patrick-ball/records?q=heart&type=Condition"
```

### Fetch a single record

Records are addressed by `{resourceType}/{id}`, as suggested in FHIR docs, which specified that IDs are only unique within a resource type.

```bash
curl -s localhost:8000/records/Condition/cond-pb-001

# Real id, wrong type → 404
curl -s localhost:8000/records/Observation/cond-pb-001
```

### Medications

Purpose-built endpoints return flattened, purpose-specific shapes as opposed to the generic
records endpoint, which returns canonical FHIR.

```bash
curl -s localhost:8000/patients/patrick-ball/medications
curl -s "localhost:8000/patients/patrick-ball/medications?status=active"

# Same data, unflattened, via the generic endpoint
curl -s "localhost:8000/patients/patrick-ball/records?type=MedicationRequest"
```

### Add a record

Records are validated and deduplicated; all indexes update immediately.
Note that added records live in memory only and don't survive a restart.

```bash
curl -s -X POST localhost:8000/records \
  -H "Content-Type: application/json" \
  -d '{
    "resourceType": "Condition",
    "id": "cond-demo-001",
    "subject": {"reference": "Patient/patrick-ball"},
    "code": {"text": "Seasonal allergic rhinitis"},
    "onsetDateTime": "2024-04-12"
  }'

# Immediately searchable
curl -s "localhost:8000/patients/patrick-ball/records?q=allergy"

# Duplicate → 409
curl -s -X POST localhost:8000/records \
  -H "Content-Type: application/json" \
  -d '{"resourceType": "Condition", "id": "cond-demo-001"}'

# Missing required fields → 422
curl -s -X POST localhost:8000/records \
  -H "Content-Type: application/json" \
  -d '{"id": "no-type-here"}'
```

### Erase a patient's data

Idempotent: erasing an already-erased patient returns `removed: 0` rather than
404. 

```bash

# Erase a single record
curl -s -X DELETE localhost:8000/records/Condition/cond-demo-001

# Wipe out a patient's data
curl -s -X DELETE localhost:8000/patients/patrick-ball/records
```

## Design notes

**Who consumes this:** a patient.
That drove the endpoint shapes — records/medications scoped to one patient,
keyword search in patient vocabulary, and right-to-erasure as a priortiy.
Secondary users like biopharma and research companies are scoped out on purpose
because they require different data models and endpoints: data would need to be
accessed across all patients. That also requires a completely different approach
to data privacy.


**In-memory over a database — deliberately.** The prompt scopes out
ingestion and the dataset fits in RAM, so records load at startup into three
structures that mirror exactly what a database would build:

- `patient_index` (patient → record keys) — the compound index
- `records` keyed by `(resourceType, id)` — the primary key / unique constraint
- `term_index` (token → record keys) — the inverted / multikey index

Migration to e.g. MongoDB is therefore mechanical: each structure maps
one-to-one onto a collection index. Known trade-offs are space limitation,
lack of support for multiple writers, and data volatility
(currently, POSTed records don't survive restarts — a known trade-off).

**Search (the "additional capability"):** patients don't know FHIR resource
types or clinical coding, and at thousands of records browsing fails.
`?q=` searches all text in the patient's records, expanding query terms
through a synonym map (heart→cardiac, lipitor→atorvastatin) at query time.
Unmapped terms fall back to literal keyword match, so unseen vocabulary
degrades gracefully instead of returning nothing. Search results are
intersected with the patient's own record set — the term index is global,
so this intersection is the privacy boundary.

**Response shape policy** General queries for records are returned in 
canonical FHIR format, while more purpose-build endpoints are designed
to handle queries for specific records (i.e. /medications) and return 
a flattend shape

**Messy data:** malformed lines are skipped and counted, never fatal.
Unknown resource types are stored and served — the system indexes by
convention (patient reference, dates, text), not by a type whitelist.
Both `Patient/id` and `urn:uuid:id` reference styles resolve. Records
with no resolvable patient are counted and sampled in `/status` rather
than silently dropped or crashed on. `/status` makes all of this visible.

**Auth (out of scope):** with authentication, the patient identity would
come from the session token, not the URL path so only logged-in patients
can have access to their data and delete it. Wipe would additionally
get a soft-delete grace window and an audit event that records the deletion 
(without the confidential content).

