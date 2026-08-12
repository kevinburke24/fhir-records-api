"""In-memory record store.

Two index structures:

    RecordKey = (ResourceType, FHIR ID) because FHIR IDs are only unique per resource type

    patient_index: dict[patient_id -> set[RecordKey]] (analog: compound index)
    term_idx: dict[term -> set[RecordKey]] (analog: inverted index)
    record_tokens: dict[RecordKey -> set[term]]
    
    records: dict[RecordKey -> record] (analog: primary key)

"""

import json
import re
import sys
from collections import defaultdict
from datetime import datetime, timezone

from .synonyms import expand

RecordKey = tuple[str, str]

_TOKEN_RE = re.compile(r"[a-z0-9]+")

# Fields whose string values are patient-meaningful text worth indexing.
_TEXT_KEYS = {"text", "display", "name", "description", "title"}

# Fields that reference the record's patient, by FHIR convention.
_PATIENT_REF_KEYS = ("subject", "patient")

# Where a clinically relevant date may live, tried in order.
_DATE_KEYS = (
    "effectiveDateTime", "onsetDateTime", "authoredOn", "recordedDate",
    "issued", "date", "performedDateTime",
)


def normalize_ref(ref: str) -> str | None:
    """'Patient/abc' -> 'abc';  'urn:uuid:abc' -> 'abc'.

    Synthetic FHIR (e.g. Synthea) commonly uses urn:uuid references, real
    exports use Type/id — silently supporting only one orphans half the
    world's data.
    """
    if not isinstance(ref, str) or not ref:
        return None
    if ref.startswith("urn:uuid:"):
        return ref[len("urn:uuid:"):]
    if "/" in ref:
        return ref.rsplit("/", 1)[1]
    return ref


def extract_patient_id(resource: dict) -> str | None:
    rtype = resource.get("resourceType")
    if rtype == "Patient":
        return resource.get("id")
    for key in _PATIENT_REF_KEYS:
        field = resource.get(key)
        if isinstance(field, dict):
            pid = normalize_ref(field.get("reference", ""))
            if pid:
                return pid
    return None


def extract_event_date(resource: dict) -> str | None:
    for key in _DATE_KEYS:
        v = resource.get(key)
        if isinstance(v, str) and v:
            return v
    period = resource.get("period")
    if isinstance(period, dict) and isinstance(period.get("start"), str):
        return period["start"]
    return None

def harvest_tokens(node) -> set[str]:
    """Recursively collect lowercase word tokens from human-readable fields."""
    tokens: set[str] = set()

    def walk(n):
        if isinstance(n, dict):
            for k, v in n.items():
                if k in _TEXT_KEYS and isinstance(v, str):
                    tokens.update(_TOKEN_RE.findall(v.lower()))
                else:
                    walk(v)
        elif isinstance(n, list):
            for item in n:
                walk(item)

    walk(node)
    return tokens


class Store:
    def __init__(self) -> None:
        self.records: dict[RecordKey, dict] = {}
        self.patient_index: dict[str, set[RecordKey]] = defaultdict(set)
        self.term_index: dict[str, set[RecordKey]] = defaultdict(set)
        self.record_tokens: dict[RecordKey, set[str]] = defaultdict(set)
        self.load_report: dict = {}

    # ---------------------------------------------------------------- load
    def load_file(self, path: str) -> None:
        loaded = duplicates = malformed = 0
        orphaned: list[str] = []
        unknown_note = defaultdict(int)  # resourceType -> count, informational

        try:
            f = open(path, encoding="utf-8")
        except OSError as e:
            # Fail loudly: an empty API serving 200s is worse than no API.
            print(f"FATAL: cannot open data file {path!r}: {e}", file=sys.stderr)
            raise SystemExit(1)

        with f:
            for lineno, line in enumerate(f, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    resource = json.loads(line)
                    if not isinstance(resource, dict):
                        raise ValueError("not a JSON object")
                except (json.JSONDecodeError, ValueError):
                    malformed += 1
                    continue

                outcome = self.add(resource)
                if outcome == "duplicate":
                    duplicates += 1
                elif outcome == "invalid":
                    malformed += 1
                else:
                    loaded += 1
                    unknown_note[resource["resourceType"]] += 1
                    if extract_patient_id(resource) is None and \
                            resource["resourceType"] != "Patient":
                        orphaned.append(f"{resource['resourceType']}/{resource['id']}")

        self.load_report = {
            "loaded": loaded,
            "skipped_malformed": malformed,
            "skipped_duplicates": duplicates,
            "resource_type_counts": dict(unknown_note),
            "records_without_patient": len(orphaned),
            "records_without_patient_sample": orphaned[:10],
            "patients": len(self.patient_index),
            "loaded_at": datetime.now(timezone.utc).isoformat(),
        }

    # ------------------------------------------------------------ mutation
    def add(self, resource: dict) -> str:
        """Insert one resource, updating every index. Returns
        'added' | 'duplicate' | 'invalid'."""
        rtype, rid = resource.get("resourceType"), resource.get("id")
        if not isinstance(rtype, str) or not isinstance(rid, str) or not rtype or not rid:
            return "invalid"

        key: RecordKey = (rtype, rid)
        if key in self.records:
            return "duplicate"

        self.records[key] = resource

        pid = extract_patient_id(resource)
        if pid:
            self.patient_index[pid].add(key)

        tokens = harvest_tokens(resource)
        self.record_tokens[key] = tokens
        for token in tokens:
            self.term_index[token].add(key)

        return "added"

    def remove(self, key : RecordKey) -> bool:
        """Remove a single resource from records. Returns False if record
        doesn't exist (Idempotent)"""
        resource = self.records.pop(key,None)
        if resource is None:
            return False

        for token in self.record_tokens.pop(key,set()):
            keys = self.term_index.get(token)
            if keys is not None:
                keys.discard(key)
                if len(keys) == 0:
                    del self.term_index[token]

        pid = extract_patient_id(resource)
        if pid is not None:
            pkeys = self.patient_index.get(pid)
            if pkeys is not None:
                pkeys.remove(key)
                if not pkeys:
                    del self.patient_index[pid]

        return True


    def wipe_patient(self, patient_id: str) -> int:
        """Remove all of a patient's records from every index. Idempotent."""
        keys = self.patient_index.pop(patient_id, set()).copy()
        for key in keys:
            self.remove(key)
        return len(keys)

    # --------------------------------------------------------------- reads
    def patient_records(
        self,
        patient_id: str,
        rtypes: set[str] | None = None,
        q: str | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
        limit: int | None = None,
        offset: int | None = None,
    ) -> list[dict]:
        keys = self.patient_index.get(patient_id, set())
        
        if q:
            # Expand the whole query as a phrase first (so multi-word synonym
            # entries like "blood pressure" -> hypertension work), then within
            # each candidate term: AND across its words (each word also
            # expanded), OR across candidate terms.
            matched: set[RecordKey] = set()
            for term in expand(q):
                words = _TOKEN_RE.findall(term)
                term_hits: set[RecordKey] | None = None
                for word in words:
                    word_hits: set[RecordKey] = set()
                    for syn in expand(word):
                        for syn_word in _TOKEN_RE.findall(syn):
                            word_hits |= self.term_index.get(syn_word, set())
                    term_hits = word_hits if term_hits is None else (term_hits & word_hits)
                matched |= term_hits or set()
            # Intersecting with the patient's own keys is the search-side
            # privacy boundary: the term index is global.
            keys = keys & matched

        results = [self.records[k] for k in keys]

        if rtypes:
            results = [r for r in results if r.get("resourceType") in rtypes]
        if date_from or date_to:
            def in_range(r: dict) -> bool:
                d = extract_event_date(r)
                if d is None:
                    return False
                return (not date_from or d >= date_from) and (not date_to or d <= date_to)
            results = [r for r in results if in_range(r)]

        # newest first; undated records sort last
        results.sort(key=lambda r: extract_event_date(r) or "", reverse=True)
        offset = offset or 0
        if limit is None:
            return results[offset:]
        return results[offset: offset + limit]

    def patient_medications(self, patient_id: str, status: str | None = None) -> list[dict]:
        meds = self.patient_records(patient_id, {"MedicationRequest"})
        if status:
            meds = [self.flatten_medication(m) for m in meds if m.get("status") == status]
        return meds

    def flatten_medication(self, r: dict) -> dict:
        med = r.get("medicationCodeableConcept", {})
        name = med.get("text") or next(
            (c.get("display") for c in med.get("coding", []) if c.get("display")), None
        ) or r.get("medicationReference", {}).get("display")
        dosage = next(
            (d.get("text") for d in r.get("dosageInstruction", []) if d.get("text")), None
        )
        return {
            "name": name or "Unknown medication",
            "status": r.get("status"),
            "dosage": dosage,
            "date": extract_event_date(r),
            "resourceType": r.get("resourceType"),
            "id": r.get("id"),
        }

store = Store()