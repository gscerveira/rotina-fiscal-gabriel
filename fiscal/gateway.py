"""The one object the interface talks to when something has to cross the network.

It owns the certificate, the HTTP connection and the order of operations around an irreversible
send: validate, file what will be sent, record the intent, send once, file the answer.
"""

from datetime import date, datetime, timedelta
from pathlib import Path

import httpx
from sqlalchemy.orm import Session

from . import certificate, db, dps, nfse
from .core import BlockedError, Client, Config, InvoiceDraft, Model

WHERE = "Emissão de NFS-e"
PENDING_DPS = "pending_dps_number"  # a DPS was sent and its answer never arrived
LAST_DPS = "last_dps_number"
CLOCK_MARGIN = timedelta(minutes=2)  # the server refuses an issue time ahead of its own clock


class Preview(Model):
    """Everything the user must read before confirming an issue."""

    environment: nfse.Environment
    number: int
    template: dps.Template


class Gateway:
    def __init__(
        self,
        session: Session,
        cfg: Config,
        *,
        environment: nfse.Environment,
        data_dir: Path,
        certificate_path: Path,
        model_source: tuple[Session, Path] | None = None,
        loaded: certificate.Certificate | None = None,
        http: httpx.Client | None = None,
    ) -> None:
        self.session, self.cfg, self.environment, self.data_dir = session, cfg, environment, data_dir
        self.certificate_path = certificate_path
        # Where the model invoice lives. The test environment has no invoices of its own,
        # so it borrows the real one (read-only).
        self.model_session, self.model_dir = model_source or (session, data_dir)
        self._certificate, self._http = loaded, http

    # --- connection, opened on first use so a certificate problem surfaces inside the TUI ---

    def _loaded(self) -> certificate.Certificate:
        if self._certificate is None:
            self._certificate = certificate.load(
                self.certificate_path, certificate.stored_password(), self.cfg.cnpj, date.today()
            )
        return self._certificate

    def _connection(self) -> httpx.Client:
        if self._http is None:
            self._http = nfse.connect(self._loaded())
        return self._http

    # --- reading ---

    def sync(self) -> nfse.SyncReport:
        client = nfse.AdnClient(self._connection(), self.environment)
        return nfse.sync(self.session, client, self.cfg, self.data_dir, date.today())

    # --- issuing ---

    def _model(self) -> bytes:
        latest = db.latest_invoice(self.model_session)
        if latest is None:
            raise BlockedError("nota modelo", "uma NFS-e já emitida e sincronizada", "nenhuma", WHERE)
        path = self.model_dir / f"{latest.period[:4]}/{latest.period[5:]}/NFS-e-{latest.number}.xml"
        if not path.is_file():
            raise BlockedError("XML da nota modelo", str(path), "não encontrado", WHERE)
        return path.read_bytes()

    def _next_number(self, template: dps.Template) -> int:
        pending = db.get_state(self.session, PENDING_DPS)
        if pending:
            identifier = dps.dps_id(template.city_code, self.cfg.cnpj, template.series, pending)
            if nfse.SefinClient(self._connection(), self.environment).was_issued(identifier):
                db.set_state(self.session, LAST_DPS, pending)
                db.set_state(self.session, PENDING_DPS, 0)
                raise BlockedError(
                    f"DPS {pending}",
                    "sem resposta pendente",
                    "já virou NFS-e: sincronize as notas antes de emitir",
                    WHERE,
                )
            db.set_state(self.session, PENDING_DPS, 0)  # it never arrived: the number is free again
        return max(db.get_state(self.session, LAST_DPS), template.last_dps_number) + 1

    def clients(self) -> list[db.StoredClient]:
        """Registered clients. The first time, the client of the model invoice is registered."""
        registered = db.clients(self.session)
        if not registered and db.latest_invoice(self.model_session) is not None:
            db.save_client(self.session, dps.client_from_model(self._model(), self.cfg))
            registered = db.clients(self.session)
        return registered

    def preview(self) -> Preview:
        template = dps.template(self._model(), self.cfg)
        return Preview(environment=self.environment, number=self._next_number(template), template=template)

    def issue(self, draft: InvoiceDraft, number: int, client: Client) -> db.Invoice:
        """Issue one NFS-e. Sent exactly once; whatever happens, what was sent stays on file."""
        s, production = self.session, self.environment is nfse.Environment.PRODUCTION
        now = datetime.now().astimezone() - CLOCK_MARGIN
        try:
            content = dps.DpsDraft(
                production=production,
                number=number,
                issued_at=now,
                service_date=draft.date,
                description=draft.description,
                amount=draft.amount,
                foreign_amount=draft.foreign_amount,
            )
        except ValueError as invalid:  # pydantic.ValidationError is a ValueError
            raise BlockedError("dados da nota", "válidos", str(invalid).splitlines()[-1].strip(), WHERE) from invalid
        signed = dps.sign(dps.build(self._model(), content, client, self.cfg), self._loaded())
        dps.validate(signed)

        period = f"{draft.date.year:04d}-{draft.date.month:02d}"
        folder = f"{period[:4]}/{period[5:]}"
        db.save_document(
            s,
            self.data_dir,
            f"{folder}/DPS-{number}-{now:%Y%m%d%H%M%S}.xml",
            signed,
            period=period,
            kind="DPS enviada",
            origin="local",
            amount=draft.amount,
        )
        db.set_state(s, PENDING_DPS, number)
        try:
            issued = nfse.SefinClient(self._connection(), self.environment).issue(signed)
        except BlockedError:
            db.set_state(s, PENDING_DPS, 0)  # an explicit refusal consumes no number
            raise
        except httpx.HTTPError as lost:
            # The number stays pending: the next attempt first asks the government what happened.
            raise BlockedError(
                "envio da DPS", "resposta da SEFIN", f"sem resposta ({type(lost).__name__}); nada será reenviado", WHERE
            ) from lost

        parsed = nfse.parse_nfse(issued.xml)
        invoice = db.Invoice(
            key=parsed.key,
            period=parsed.period,
            number=parsed.number,
            date=parsed.service_date,
            client=parsed.client,
            amount=parsed.amount,
            description=parsed.description,
            status="ISSUED",
        )
        db.save_invoice(s, invoice)
        db.save_document(
            s,
            self.data_dir,
            f"{folder}/NFS-e-{parsed.number}.xml",
            issued.xml,
            period=parsed.period,
            kind="NFS-e XML",
            origin="SEFIN",
            amount=parsed.amount,
        )
        db.set_state(s, LAST_DPS, number)
        db.set_state(s, PENDING_DPS, 0)
        return invoice
