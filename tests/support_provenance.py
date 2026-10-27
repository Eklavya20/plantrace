"""Declared physical evidence TEST DOUBLES for positive policy-contract tests.

These records are not real surveys and are never counted as real validation or
inserted by application extraction. Negative tests use unverified references.
"""
from hashlib import sha256
from plantrace.metrology_models import ReferenceEstablishment
from plantrace.calibration import put_reference


def physical_record(index):
    return ReferenceEstablishment(kind="independently_verified_physical",independence_group=f"test-double-independent-acquisition-{index}",
        evidence_record=f"test-double-record-{index}",evidence_sha256=sha256(f"test-double artifact {index}".encode()).hexdigest(),
        established_by="Unit-test actor (not a real survey)",established_at="2026-10-27",
        unit_basis="explicit_physical_reference",unit_statement="Declared explicit units, test-double contract only")


def with_physical_declarations(book):
    for index,reference in enumerate(list(book.references)):
        book=put_reference(book,reference.model_copy(update={"establishment":physical_record(index)}))
    return book
