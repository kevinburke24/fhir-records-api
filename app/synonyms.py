"""Synonym groups bridging patient vocabulary and clinical vocabulary.

Design note: expansion happens at QUERY time, not index time. The index
holds only tokens that literally appear in records; a search for "heart"
expands to every term in its group and unions the results. This keeps the
index smaller and means the synonym map can be edited without a reload.
Terms absent from this map still work as literal keyword search (the
fallback that protects us on unseen data).
"""

SYNONYM_GROUPS: list[set[str]] = [
    {"heart", "cardiac", "cardiology", "cardiovascular", "coronary"},
    {"skin", "derm", "dermatology", "dermatologic"},
    {"brain", "neuro", "neurology", "neurological", "nerve"},
    {"bone", "ortho", "orthopedic", "orthopedics", "joint", "fracture"},
    {"kidney", "renal", "nephrology"},
    {"lung", "pulmonary", "respiratory", "breathing"},
    {"stomach", "gastric", "gastrointestinal", "gi", "digestive"},
    {"sugar", "diabetes", "diabetic", "glucose", "a1c"},
    {"blood pressure", "hypertension", "hypertensive", "bp"},
    {"cancer", "oncology", "tumor", "malignant", "neoplasm"},
    {"allergy", "allergic", "allergies"},
    # medications: brand <-> generic <-> class
    {"proair hfa", "ventolin hfa", "proventil hfa", "albuterol", "bronchodilator"},
    {"zoloft", "sertraline", "selective serotonine reuptake inhibitor", "ssri"},
    {"spiriva", "tiotropium", "inhaler"},
    {"coreg", "coreg cr", "carvedilol", "beta blocker"},
    {"tylenol", "acetaminophen", "paracetamol"},
    {"advil", "motrin", "ibuprofen", "aleve", "aspirin", "naprosyn", "acetylsalicylic acid", "nsaid"},
    {"lipitor", "atorvastatin", "statin"},
    {"coumadin", "warfarin", "anticoagulant", "blood thinner"},
    {"zestril", "prinivil", "lisinopril", "ace inhibitor"},
    {"glucophage", "metformin"},
    {"amoxil", "amoxicillin", "antibiotic"},
    {"lantus", "basaglar", "insuline glargine"},
    {"synthroid", "levoxyl", "levothyroxine", "thyroid hormone"}
]

# term -> the full group it belongs to
_TERM_TO_GROUP: dict[str, set[str]] = {}
for group in SYNONYM_GROUPS:
    for term in group:
        _TERM_TO_GROUP[term] = group


def expand(term: str) -> set[str]:
    """Return all search terms equivalent to `term` (always includes itself)."""
    term = term.lower().strip()
    return _TERM_TO_GROUP.get(term, {term}) | {term}