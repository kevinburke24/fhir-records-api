import json

import pytest
from fastapi.testclient import TestClient

from app.store import Store, store
from app import main

SAMPLE = [
    {"resourceType": "Patient", "id": "p1", "name": [{"family": "Osei", "given": ["Amara"]}]},
    {"resourceType": "Condition", "id": "c1",
     "subject": {"reference": "Patient/p1"},
     "code": {"text": "Coronary artery disease"},
     "onsetDateTime": "2021-03-01"},
    {"resourceType": "Observation", "id": "o1",
     "subject": {"reference": "urn:uuid:p1"},           # urn-style ref
     "code": {"text": "Blood glucose"},
     "effectiveDateTime": "2023-06-15"},
    {"resourceType": "MedicationRequest", "id": "m1",
     "subject": {"reference": "Patient/p1"},
     "medicationCodeableConcept": {"text": "Atorvastatin 20mg"},
     "status": "active", "authoredOn": "2022-01-10"},
    {"resourceType": "Patient", "id": "p2"},
    {"resourceType": "Condition", "id": "c2",
     "subject": {"reference": "Patient/p2"},
     "code": {"text": "Eczema"}},
    {"resourceType": "MysteryType", "id": "x1",        # unknown resource type
     "subject": {"reference": "Patient/p1"},
     "code": {"text": "Something unexpected"}},
]


@pytest.fixture()
def client(tmp_path, monkeypatch):
    data = tmp_path / "records.jsonl"
    lines = [json.dumps(r) for r in SAMPLE]
    lines.insert(3, "{this is not json")                # malformed line
    lines.append(json.dumps(SAMPLE[1]))                 # duplicate
    data.write_text("\n".join(lines))

    fresh = Store()
    fresh.load_file(str(data))
    monkeypatch.setattr(main, "store", fresh)
    import app.store as store_module
    monkeypatch.setattr(store_module, "store", fresh)
    # rebind the store referenced inside route closures
    monkeypatch.setattr("app.main.store", fresh)
    return TestClient(main.app, raise_server_exceptions=True), fresh


def test_load_report_counts(client):
    c, s = client
    r = s.load_report
    assert r["loaded"] == len(SAMPLE)
    assert r["skipped_malformed"] == 1
    assert r["skipped_duplicates"] == 1
    assert "MysteryType" in r["resource_type_counts"]


def test_urn_uuid_reference_resolves(client):
    c, s = client
    keys = s.patient_index["p1"]
    assert ("Observation", "o1") in keys


def test_patient_records_scoped_and_sorted(client):
    c, _ = client
    body = c.get("/patients/p1/records").json()
    ids = [r["id"] for r in body["records"]]
    assert "c2" not in ids                              # other patient's data
    dated = [r for r in body["records"] if r["id"] in ("o1", "m1", "c1")]
    assert [r["id"] for r in dated] == ["o1", "m1", "c1"]  # newest first


def test_records_pagination_limit(client):
    c, _ = client
    body = c.get("/patients/p1/records", params={"limit": 2}).json()
    assert body["limit"] == 2
    assert body["offset"] == 0
    assert body["count"] == 2
    assert len(body["records"]) == 2


def test_records_pagination_offset_continues_sequence(client):
    c, _ = client
    full = c.get("/patients/p1/records", params={"limit": 50}).json()["records"]
    assert len(full) >= 3                                # need at least 3 to page through

    # walk the list one record at a time via offset and confirm it matches
    # the unpaginated order exactly, with no gaps or duplicates
    paged = []
    for i in range(len(full)):
        page = c.get("/patients/p1/records", params={"limit": 1, "offset": i}).json()["records"]
        paged.extend(page)
    assert [r["id"] for r in paged] == [r["id"] for r in full]


def test_records_pagination_offset_past_end_is_empty(client):
    c, _ = client
    body = c.get("/patients/p1/records", params={"offset": 1000}).json()
    assert body["count"] == 0
    assert body["records"] == []


def test_records_pagination_defaults(client):
    c, _ = client
    body = c.get("/patients/p1/records").json()
    assert body["limit"] == 50
    assert body["offset"] == 0


def test_records_pagination_limit_bounds_rejected(client):
    c, _ = client
    assert c.get("/patients/p1/records", params={"limit": 0}).status_code == 422
    assert c.get("/patients/p1/records", params={"limit": 51}).status_code == 422
    assert c.get("/patients/p1/records", params={"offset": -1}).status_code == 422


def test_search_synonym_expansion(client):
    c, _ = client
    # 'heart' -> {cardiac, cardiology, cardiovascular, coronary} matches
    # a Condition whose text is 'Coronary artery disease'
    body = c.get("/patients/p1/records", params={"q": "heart"}).json()
    assert any(r["id"] == "c1" for r in body["records"])
    # literal fallback: unmapped terms still match their own token
    body2 = c.get("/patients/p1/records", params={"q": "coronary"}).json()
    assert any(r["id"] == "c1" for r in body2["records"])


def test_multiword_synonym_phrase(client):
    c, _ = client
    # add a hypertension condition, then find it via the phrase "blood pressure"
    c.post("/records", json={"resourceType": "Condition", "id": "c-bp",
                             "subject": {"reference": "Patient/p1"},
                             "code": {"text": "Essential hypertension"}})
    body = c.get("/patients/p1/records", params={"q": "blood pressure"}).json()
    assert any(r["id"] == "c-bp" for r in body["records"])


def test_search_brand_name_finds_generic(client):
    c, _ = client
    body = c.get("/patients/p1/records", params={"q": "lipitor"}).json()
    assert any(r["id"] == "m1" for r in body["records"])


def test_search_does_not_leak_across_patients(client):
    c, _ = client
    body = c.get("/patients/p2/records", params={"q": "glucose"}).json()
    assert body["count"] == 0

def test_medications_endpoint(client):
    c, _ = client
    body = c.get("/patients/p1/medications", params={"status": "active"}).json()
    assert [m["id"] for m in body["medications"]] == ["m1"]

def test_delete_endpoint(client):
    c, _ = client
    assert c.delete("/records/Condition/c1").status_code in (200, 204)
    assert c.get("/records/Condition/c1").status_code == 404
    assert c.delete("/records/Condition/c1").status_code == 404  # already gone

def test_post_validates_and_dedups(client):
    c, _ = client
    assert c.post("/records", json={"id": "z9"}).status_code == 422
    ok = c.post("/records", json={"resourceType": "Condition", "id": "c9",
                                  "subject": {"reference": "Patient/p1"},
                                  "code": {"text": "Migraine"}})
    assert ok.status_code == 201
    assert c.post("/records", json={"resourceType": "Condition", "id": "c9"}).status_code == 409
    # index updated: new record findable by search immediately
    body = c.get("/patients/p1/records", params={"q": "migraine"}).json()
    assert any(r["id"] == "c9" for r in body["records"])


def test_wipe_is_complete_and_idempotent(client):
    c, s = client
    n = c.delete("/patients/p1/records").json()["removed"]
    assert n >= 4
    assert c.delete("/patients/p1/records").json()["removed"] == 0
    assert c.get("/patients/p1/records").status_code == 404
    # term index holds no dangling keys for p1's records
    for keys in s.term_index.values():
        assert all(k in s.records for k in keys)


def test_unknown_type_still_served(client):
    c, _ = client
    assert c.get("/records/MysteryType/x1").status_code == 200

def test_remove_single_record(client):
    c, s = client
    key = ("Condition", "c1")
    assert s.remove(key) is True

    # 1. gone from the table
    assert key not in s.records
    # 2. gone from the patient's secondary index
    assert key not in s.patient_index.get("p1", set())
    # 3. gone from the reverse map itself
    assert key not in s.record_tokens
    # 4. no dangling keys anywhere in the term index
    for token, keys in s.term_index.items():
        assert key not in keys, f"dangling key under token {token!r}"


def test_remove_cleans_empty_token_entries(client):
    c, s = client
    # 'coronary' appears only in c1 in the fixture, so removing c1
    # should delete the token entry entirely, not leave an empty set
    assert "coronary" in s.term_index
    s.remove(("Condition", "c1"))
    assert "coronary" not in s.term_index


def test_remove_is_idempotent(client):
    c, s = client
    assert s.remove(("Condition", "c1")) is True
    assert s.remove(("Condition", "c1")) is False      # second time: no-op
    assert s.remove(("Nope", "zzz")) is False          # never existed


def test_removed_record_not_searchable(client):
    c, s = client
    # findable before
    body = c.get("/patients/p1/records", params={"q": "heart"}).json()
    assert any(r["id"] == "c1" for r in body["records"])
    s.remove(("Condition", "c1"))
    # invisible after — and no 500 from a dangling key lookup
    body = c.get("/patients/p1/records", params={"q": "heart"}).json()
    assert not any(r["id"] == "c1" for r in body["records"])


def test_remove_does_not_touch_other_records(client):
    c, s = client
    s.remove(("Condition", "c1"))
    # sibling record sharing tokens like 'disease'/'blood' is unaffected
    assert ("Observation", "o1") in s.records
    body = c.get("/patients/p1/records", params={"q": "glucose"}).json()
    assert any(r["id"] == "o1" for r in body["records"])


def test_wipe_still_complete_after_refactor(client):
    c, s = client
    # if you rewrote wipe_patient as a loop over remove(), this guards it
    c.delete("/patients/p1/records")
    assert "p1" not in s.patient_index
    for keys in s.term_index.values():
        assert all(k in s.records for k in keys)
