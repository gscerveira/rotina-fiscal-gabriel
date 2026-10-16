import sys
from pathlib import Path

from pydantic import ValidationError

from . import core, db, nfse
from .gateway import Gateway
from .tui import FiscalApp

ROOT = Path(__file__).parent.parent
CONFIG = ROOT / "config.local.toml" if (ROOT / "config.local.toml").exists() else ROOT / "config.toml"
USAGE = """uso: python -m fiscal [--simulate | --homologation]

  (sem opção)      produção: lê e emite NFS-e de verdade
  --homologation   ambiente de testes do governo: emite notas sem efeito fiscal, em banco separado
  --simulate       tudo local: nada sai da máquina
"""


def main() -> None:
    options = set(sys.argv[1:])
    if options - {"--simulate", "--homologation"} or len(options) > 1:
        print(USAGE, file=sys.stderr)
        sys.exit(2)
    try:
        cfg = core.load_config(CONFIG)
    except ValidationError as invalid:
        print(f"{CONFIG.name} inválido:", file=sys.stderr)
        for error in invalid.errors():
            print(f"  {'.'.join(map(str, error['loc'])) or '(arquivo)'}: {core.message(error)}", file=sys.stderr)
        sys.exit(1)

    certificate_path = CONFIG.parent / cfg.certificate_path
    if "--simulate" in options:
        # Simulation has its own database and never touches the network.
        FiscalApp(db.open_db(db.DATA_DIR / "simulation.db"), cfg, simulate=True).run()
    elif "--homologation" in options:
        # The test environment keeps its own database and files, and borrows the real model invoice.
        folder = db.DATA_DIR / "homologation"
        session = db.open_db(folder / "fiscal.db")
        gateway = Gateway(
            session,
            cfg,
            environment=nfse.Environment.RESTRICTED,
            data_dir=folder,
            certificate_path=certificate_path,
            model_source=(db.open_db(db.DATA_DIR / "fiscal.db"), db.DATA_DIR),
        )
        FiscalApp(session, cfg, gateway=gateway, data_dir=folder).run()
    else:
        session = db.open_db(db.DATA_DIR / "fiscal.db")
        gateway = Gateway(
            session,
            cfg,
            environment=nfse.Environment.PRODUCTION,
            data_dir=db.DATA_DIR,
            certificate_path=certificate_path,
        )
        FiscalApp(session, cfg, gateway=gateway).run()


if __name__ == "__main__":
    main()
