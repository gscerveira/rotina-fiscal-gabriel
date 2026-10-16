# rotina-fiscal

Terminal app that runs the monthly tax routine of a Simples Nacional company exporting
services: confirm revenue from the issued NFS-e, file PGDAS-D, generate the DAS, send eSocial
S-1200 / S-1210 / S-1299, check DCTFWeb and generate the DARF. Every irreversible step shows a
full summary and waits for an explicit confirmation.

Code is English; everything the user reads on screen is Portuguese. See `PRODUCT.md` for the
product context.

## Status

- **Connected (read-only):** issued NFS-e are pulled from the national data environment (ADN)
  with the e-CNPJ certificate; their XML and PDF are filed under `data/YYYY/MM/`.
- **Connected (issuing):** a new NFS-e is built from the last issued one, signed, checked against
  the official schema and sent to SEFIN after an on-screen review. Verified against a stand-in
  server only: **no real send, test or production, has been made yet**.
- **Not connected yet:** PGDAS-D/DAS, eSocial, DCTFWeb/DARF. Those steps can only be confirmed in
  **simulation**.

The certificate password is read from the system keyring (service `rotina-fiscal`, entry
`certificate`); it is never written to a file.

## Run

```sh
uv sync
uv run python -m fiscal --simulate       # everything local, nothing leaves the machine
uv run python -m fiscal --homologation   # government test environment: invoices with no fiscal effect
uv run python -m fiscal                  # production: reads and issues real NFS-e
```

## Layout

| Path | Role |
|---|---|
| `config.toml` | Tax profile and pro-labore validity ranges, with placeholder values. No secrets. Copy it to `config.local.toml` (gitignored) and fill in the real profile. |
| `fiscal/core.py` | Pure rules: strict pydantic models, steps, payroll, checks. |
| `fiscal/db.py` | SQLAlchemy tables, pydantic read models, audit trail, document registry. |
| `fiscal/certificate.py` | Loads the e-CNPJ A1 and builds the mutual-TLS context. |
| `fiscal/nfse.py` | NFS-e Nacional: ADN sync of issued invoices, SEFIN issuing client. |
| `fiscal/dps.py` | Builds a DPS from the last issued NFS-e, signs it, validates it against the XSD. |
| `fiscal/gateway.py` | Orders an irreversible send: validate, file, record intent, send once, file the answer. |
| `fiscal/schemas/` | Official NFS-e XSDs (one documented local patch). |
| `fiscal/migrations/` | Alembic revisions. Applied automatically on startup. |
| `fiscal/tui.py` | Textual interface. |
| `data/` | Local database and documents. Git-ignored. |

## Development

Four gates, also enforced by the pre-commit hook (`uv run pre-commit install`):

```sh
uv run ruff check . && uv run ruff format --check . && uv run ty check && uv run pytest
```

Changing a table requires a migration, and a test fails if one is missing:

```sh
uv run alembic revision --autogenerate -m "describe the change"
```

## Git flow

- `main` always passes the four gates. Nothing is committed to it directly.
- One short-lived branch per change: `feat/…`, `fix/…`, `chore/…`, `docs/…`.
- Conventional Commits, in English, one logical change per commit.
- Merge into `main` with `--no-ff` once the branch is reviewed.

## Guardrails

- Models are frozen and strict: a float never becomes money, and coercion happens only at the
  boundaries (config file, database rows, keyboard input).
- Amounts are `Decimal`, never rounded when typed by the user.
- A step is recorded as `SENDING` before anything is sent, so an interrupted send is visible on
  resume instead of being silently repeated.
- Certificates, `data/` and anything resembling a key are git-ignored.
