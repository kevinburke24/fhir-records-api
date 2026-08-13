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
| GET | `/patients/{id}/records?type=&q=&from=&to=&offset=&limit=` | Patient's records; filter by type/date, keyword search with synonym expansion, pagination |
| GET | `/patients/{id}/medications?status=` | Medication list |
| GET | `/records/{type}/{id}` | Single record (keyed by type+id: FHIR ids are only unique per type) |
| POST | `/records` | Add a record; validated, deduplicated (409), indexes updated |
| DELETE | `/patients/{id}/records` | Wipe a patient's data (right to erasure); idempotent |
| GET | `/status` | Load report: counts, skipped lines, unknown types |

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

