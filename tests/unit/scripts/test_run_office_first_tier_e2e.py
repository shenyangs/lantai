from __future__ import annotations

import importlib.util
import json
import sys
import zipfile
from collections import Counter
from pathlib import Path

import pytest

import lantai.app.officecli_safe_bridge as bridge


SCRIPT_PATH = (
    Path(__file__).resolve().parents[3]
    / "scripts/run_office_first_tier_e2e.py"
)


class ResidentOffice:
    """Model file-bound residents while using the real transactional bridge."""

    def __init__(self, module, output_dir: Path):
        self.module = module
        self.output_dir = output_dir
        self.residents: set[Path] = set()
        self.close_attempts: list[Path] = []
        self.close_counts: Counter[Path] = Counter()
        self.transactions = []
        self.deleted_while_live: list[Path] = []
        self.failure = None

    def run(self, command, **kwargs):
        verb, path = command[1], Path(command[2])
        if verb == "close":
            self.close_attempts.append(path)
            self.close_counts[path] += 1
        else:
            # Even reads and validation can auto-start a native resident.
            self.residents.add(path)
        if self.failure is not None:
            self.failure(verb, path, command)
        if verb == "close":
            self.residents.discard(path)
            return {"success": True}
        if verb == "create":
            self.write(path, {"commands": []})
        elif verb == "batch":
            payload = self.read(path)
            payload["commands"].extend(
                json.loads(command[command.index("--commands") + 1])
            )
            self.write(path, payload)
        elif verb == "get":
            payload = self.read(path)
            if command[3] == "/Sheet1/C1":
                payload.update(formula="B1*6", computedValue="42")
            return payload
        return {"success": True}

    @staticmethod
    def write(path: Path, payload):
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("fixture.json", json.dumps(payload))

    @staticmethod
    def read(path: Path):
        with zipfile.ZipFile(path) as archive:
            return json.loads(archive.read("fixture.json"))

    def create(self):
        return self.module._create_openxml_evidence(
            officecli=Path("/isolated-test/officecli"),
            output_dir=self.output_dir,
            case_id="resident-regression",
        )


@pytest.fixture
def resident_office(monkeypatch, tmp_path):
    spec = importlib.util.spec_from_file_location(
        "office_resident_gate", SCRIPT_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    monkeypatch.setattr(sys, "path", sys.path.copy())
    monkeypatch.setenv("LANTAI_WORKING_DIR", str(tmp_path / "working"))
    monkeypatch.setattr(bridge, "WORKING_DIR", tmp_path / "working")
    office = ResidentOffice(module, tmp_path)
    prepare = bridge.prepare_officecli_mcp_call
    cleanup = bridge.cleanup_officecli_transaction

    def capture_transaction(arguments):
        transaction = prepare(arguments)
        office.transactions.append(transaction)
        return transaction

    def capture_cleanup(transaction):
        if transaction.working_path in office.residents:
            office.deleted_while_live.append(transaction.working_path)
        cleanup(transaction)

    monkeypatch.setattr(
        bridge, "prepare_officecli_mcp_call", capture_transaction
    )
    monkeypatch.setattr(
        bridge, "cleanup_officecli_transaction", capture_cleanup
    )
    monkeypatch.setattr(module, "_run_json", office.run)
    try:
        yield office
    finally:
        # Failed-close cases intentionally retain their isolated temporary file.
        # Dispose only this test double's state and this test's transactions.
        office.residents.clear()
        for transaction in office.transactions:
            cleanup(transaction)


def test_success_leaves_no_file_residents_or_live_temporary_copies(
    resident_office,
):
    files, evidence = resident_office.create()

    assert len(files) == 3
    assert len(evidence["receipts"]) == 6
    assert all(row["receipt"]["atomic"] for row in evidence["receipts"])
    assert resident_office.residents == set()
    assert resident_office.deleted_while_live == []
    assert all(
        not t.temporary_dir.exists() for t in resident_office.transactions
    )


@pytest.mark.parametrize("verb", ["create", "batch", "validate"])
def test_transaction_failure_closes_before_deleting_and_never_commits(
    resident_office, verb
):
    def fail(actual_verb, path, command):
        if actual_verb == verb:
            raise resident_office.module.EvidenceError("transaction failure")

    resident_office.failure = fail
    with pytest.raises(
        resident_office.module.EvidenceError, match="transaction failure"
    ):
        resident_office.create()

    assert resident_office.residents == set()
    assert resident_office.deleted_while_live == []
    assert all(
        not t.temporary_dir.exists() for t in resident_office.transactions
    )
    failed = resident_office.transactions[-1]
    if failed.existed:
        assert resident_office.read(failed.target_path) == {"commands": []}
    else:
        assert not failed.target_path.exists()


def test_commit_failure_still_closes_validation_resident(
    resident_office, monkeypatch
):
    def fail(transaction):
        raise bridge.OfficeCliSafetyError("baseline changed")

    monkeypatch.setattr(bridge, "commit_officecli_transaction", fail)
    with pytest.raises(bridge.OfficeCliSafetyError, match="baseline changed"):
        resident_office.create()

    assert resident_office.residents == set()
    assert resident_office.deleted_while_live == []
    assert not resident_office.transactions[0].target_path.exists()


@pytest.mark.parametrize(
    ("verb", "suffix", "selector"),
    [
        ("get", ".docx", "/body"),
        ("get", ".xlsx", "/Sheet1/C1"),
        ("get", ".pptx", "/slide[1]"),
        ("validate", ".docx", None),
        ("validate", ".xlsx", None),
        ("validate", ".pptx", None),
    ],
)
def test_readback_or_validation_failure_closes_every_opened_document(
    resident_office, verb, suffix, selector
):
    def fail(actual_verb, path, command):
        if (
            actual_verb == verb
            and path.parent == resident_office.output_dir
            and path.suffix == suffix
            and (selector is None or command[3] == selector)
        ):
            raise resident_office.module.EvidenceError("readback failure")

    resident_office.failure = fail
    with pytest.raises(
        resident_office.module.EvidenceError, match="readback failure"
    ):
        resident_office.create()

    assert resident_office.residents == set()
    assert resident_office.deleted_while_live == []


def test_failed_final_close_is_reported_and_other_documents_are_closed(
    resident_office,
):
    failed_path = resident_office.output_dir / "office-first-tier.docx"

    def fail(verb, path, command):
        if verb == "close" and path == failed_path:
            raise resident_office.module.EvidenceError("close failed")

    resident_office.failure = fail
    with pytest.raises(
        resident_office.module.EvidenceError, match="close failed"
    ):
        resident_office.create()

    assert resident_office.residents == {failed_path}
    for suffix in (".docx", ".xlsx", ".pptx"):
        assert resident_office.output_dir / ("office-first-tier" + suffix) in (
            resident_office.close_attempts
        )


def test_failed_temporary_close_retains_file_and_prevents_success(
    resident_office,
):
    def fail(verb, path, command):
        if verb == "close" and resident_office.close_counts[path] == 2:
            raise resident_office.module.EvidenceError("close failed")

    resident_office.failure = fail
    with pytest.raises(
        resident_office.module.EvidenceError, match="close failed"
    ):
        resident_office.create()

    assert len(resident_office.transactions) == 1
    failed = resident_office.transactions[0]
    assert failed.working_path in resident_office.residents
    assert failed.working_path.exists()
    assert not failed.target_path.exists()
    assert resident_office.deleted_while_live == []
