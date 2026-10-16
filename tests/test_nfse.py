from datetime import date
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from fiscal import core, db, nfse
from tests import nfse_samples as samples

CFG = core.load_config(Path(__file__).parent.parent / "config.toml")
TODAY = date(2026, 10, 7)
PDF = b"%PDF-1.7 fake"


def served(*batches: bytes, pdf_status: int = 200) -> tuple[nfse.AdnClient, list[str]]:
    """An ADN that answers the given batches in order, then "nothing new". Returns the client and its call log."""
    queue, calls = list(batches), []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if "/danfse/" in request.url.path:
            return httpx.Response(pdf_status, content=PDF if pdf_status == 200 else b"")
        body = queue.pop(0) if queue else samples.batch()
        return httpx.Response(200 if b"DOCUMENTOS_LOCALIZADOS" in body else 404, content=body)

    return nfse.AdnClient(httpx.Client(transport=httpx.MockTransport(handler))), calls


def test_parses_an_issued_nfse():
    parsed = nfse.parse_nfse(samples.nfse_xml(1))
    assert (parsed.number, parsed.issuer, parsed.amount, parsed.period) == (
        "1",
        samples.OWN_CNPJ,
        Decimal("13731.88"),
        "2026-09",
    )
    assert parsed.key == samples.key(1)


def test_broken_or_incomplete_xml_blocks():
    with pytest.raises(core.BlockedError):
        nfse.parse_nfse(b"<not-xml")
    with pytest.raises(core.BlockedError):
        nfse.parse_nfse(samples.nfse_xml(1).replace(b"<vServ>13731.88</vServ>", b""))


def test_sync_files_only_our_invoices_and_their_documents(tmp_path: Path):
    s = db.open_db()
    ours, theirs = samples.nfse_xml(1), samples.nfse_xml(130396, issuer=samples.OTHER_CNPJ, amount="137.00")
    client, calls = served(
        samples.batch(
            (1, "NFSE", samples.key(130396, samples.OTHER_CNPJ), theirs),
            (2, "NFSE", samples.key(1), ours),
        )
    )
    report = nfse.sync(s, client, CFG, tmp_path, TODAY)

    assert (report.issued, report.ignored, report.pdfs_missing) == (1, 1, 0)
    (invoice,) = db.invoices(s, "2026-09")
    assert (invoice.number, invoice.amount, invoice.status) == ("1", Decimal("13731.88"), "ISSUED")
    assert (tmp_path / "2026/09/NFS-e-1.xml").read_bytes() == ours
    assert (tmp_path / "2026/09/NFS-e-1.pdf").read_bytes() == PDF
    assert [d.kind for d in db.documents(s, "2026-09")] == ["NFS-e PDF", "NFS-e XML"]
    assert db.get_state(s, nfse.LAST_NSU) == 2
    assert calls[0] == "/contribuintes/DFe/0"

    # A second run asks only for what is new and changes nothing.
    again = nfse.sync(s, client, CFG, tmp_path, TODAY)
    assert (again.issued, again.ignored) == (0, 0)
    assert calls[-1] == "/contribuintes/DFe/2"
    assert len(db.invoices(s, "2026-09")) == 1


def test_a_cancellation_removes_the_invoice_from_revenue(tmp_path: Path):
    s = db.open_db()
    client, _ = served(
        samples.batch(
            (1, "NFSE", samples.key(1), samples.nfse_xml(1)),
            (2, "NFSE", samples.key(2), samples.nfse_xml(2, amount="100.00")),
        ),
        samples.batch((3, "EVENTO", samples.key(2), samples.event_xml(samples.key(2)))),
    )
    report = nfse.sync(s, client, CFG, tmp_path, TODAY)
    assert (report.issued, report.events) == (2, 1)
    by_number = {i.number: i.status for i in db.invoices(s, "2026-09")}
    assert by_number == {"1": "ISSUED", "2": "CANCELLED"}
    assert nfse.countable(db.invoices(s, "2026-09")) == Decimal("13731.88")


def test_an_unknown_event_or_a_different_tax_profile_asks_for_review(tmp_path: Path):
    s = db.open_db()
    client, _ = served(
        samples.batch(
            (1, "NFSE", samples.key(1), samples.nfse_xml(1)),
            (2, "NFSE", samples.key(2), samples.nfse_xml(2, iss_taxation="1")),
            (3, "EVENTO", samples.key(1), samples.event_xml(samples.key(1), "e202201")),
        )
    )
    nfse.sync(s, client, CFG, tmp_path, TODAY)
    assert {i.number: i.status for i in db.invoices(s, "2026-09")} == {"1": "REVIEW", "2": "REVIEW"}


def test_a_failed_pdf_download_is_counted_and_retried(tmp_path: Path):
    s = db.open_db()
    client, _ = served(samples.batch((1, "NFSE", samples.key(1), samples.nfse_xml(1))), pdf_status=503)
    assert nfse.sync(s, client, CFG, tmp_path, TODAY).pdfs_missing == 1
    assert not (tmp_path / "2026/09/NFS-e-1.pdf").exists()
    assert db.invoices(s, "2026-09")  # the invoice itself is safely filed

    retry, _ = served()  # a later run, in the month after the invoice, still fetches it
    assert nfse.sync(s, retry, CFG, tmp_path, TODAY).pdfs_missing == 0
    assert (tmp_path / "2026/09/NFS-e-1.pdf").exists()


def test_wrong_environment_and_rejections_block(tmp_path: Path):
    s = db.open_db()
    wrong, _ = served(samples.batch((1, "NFSE", samples.key(1), samples.nfse_xml(1)), environment="HOMOLOGACAO"))
    with pytest.raises(core.BlockedError) as blocked:
        nfse.sync(s, wrong, CFG, tmp_path, TODAY)
    assert blocked.value.field == "ambiente do ADN"

    down = nfse.AdnClient(httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(502))))
    with pytest.raises(core.BlockedError) as blocked:
        nfse.sync(s, down, CFG, tmp_path, TODAY)
    assert blocked.value.found == "HTTP 502"
    assert db.invoices(s, "2026-09") == []


def test_a_filed_document_is_never_overwritten_with_other_content(tmp_path: Path):
    s = db.open_db()
    db.save_document(s, tmp_path, "2026/09/x.xml", b"one", period="2026-09", kind="k", origin="test")
    db.save_document(s, tmp_path, "2026/09/x.xml", b"one", period="2026-09", kind="k", origin="test")
    with pytest.raises(core.BlockedError):
        db.save_document(s, tmp_path, "2026/09/x.xml", b"two", period="2026-09", kind="k", origin="test")
    assert (tmp_path / "2026/09/x.xml").read_bytes() == b"one"


def test_an_invoice_issued_locally_is_not_filed_twice_when_the_adn_distributes_it(tmp_path: Path):
    s = db.open_db()
    client, _ = served(samples.batch((1, "NFSE", samples.key(1), samples.nfse_xml(1))))
    nfse.sync(s, client, CFG, tmp_path, TODAY)
    db.set_state(s, nfse.LAST_NSU, 0)  # as if the ADN only now distributed it, serialized differently
    again, _ = served(samples.batch((1, "NFSE", samples.key(1), samples.nfse_xml(1) + b"\n")))
    report = nfse.sync(s, again, CFG, tmp_path, TODAY)
    assert (report.issued, report.ignored) == (0, 1)
    assert (tmp_path / "2026/09/NFS-e-1.xml").read_bytes() == samples.nfse_xml(1)
    assert db.get_state(s, nfse.LAST_NSU) == 1
