"""Terminal interface: the open month on top, the closed period below as a calendar of due dates."""

from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import cast

import httpx
from pydantic import ValidationError
from rich.console import Group, RenderableType
from rich.table import Table
from rich.text import Text
from sqlalchemy.orm import Session
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, VerticalScroll
from textual.screen import Screen
from textual.widgets import Button, DataTable, Input, MaskedInput, Select, Static

from . import core, db, nfse
from .core import Status
from .gateway import Gateway

# Palette: dark blue ink, paper, and a single amber reserved for the next decision.
GROUND, FIELD, CURSOR, RULE = "#0f1420", "#171d2c", "#1d2538", "#2a3142"
PAPER, WAITING, AMBER, SEAL, ERROR = "#e8e2d4", "#8c93a6", "#f0b03f", "#86c49a", "#ee6f5e"

INVOICE_STATUS = {"ISSUED": ("EMITIDA", SEAL), "CANCELLED": ("CANCELADA", ERROR), "REVIEW": ("CONFERIR", AMBER)}

CSS = f"""
Screen {{ background: {GROUND}; color: {PAPER}; }}
#page {{ max-width: 84; padding: 1 3 0 3; scrollbar-size-vertical: 1;
         scrollbar-background: {GROUND}; scrollbar-color: {RULE}; }}
#closed, #actions {{ margin-top: 2; }}
#notice, #body, #error, #question {{ margin-top: 1; }}
#keys {{ dock: bottom; height: 3; padding: 1 3; color: {WAITING}; }}
DataTable {{ height: auto; background: {GROUND}; margin-top: 1; }}
DataTable:focus, Input:focus, Button:focus {{ background-tint: transparent; }}
DataTable:focus > .datatable--header {{ background-tint: transparent; }}
DataTable:focus > .datatable--cursor {{ background: {CURSOR}; text-style: none; }}
DataTable > .datatable--header {{ background: {GROUND}; color: {WAITING}; text-style: none; }}
DataTable > .datatable--cursor {{ background: {CURSOR}; }}
DataTable > .datatable--hover {{ background: {FIELD}; }}
Input {{ border: none; height: 1; padding: 0 1; background: {FIELD}; color: {PAPER}; width: 44; }}
Input:focus {{ border: none; background: {CURSOR}; }}
Input:disabled {{ color: {WAITING}; }}
Input > .input--placeholder {{ color: {WAITING}; }}
Input > .input--cursor {{ background: {AMBER}; color: {GROUND}; }}
Select {{ width: 44; }}
Select > SelectCurrent {{ background: {FIELD}; color: {PAPER}; padding: 0 1; }}
Select:focus > SelectCurrent {{ border: none; background: {CURSOR}; background-tint: transparent; }}
Select > SelectOverlay {{ background: {FIELD}; color: {PAPER}; border: none; }}
Select > SelectOverlay > .option-list--option-highlighted {{ background: {AMBER}; color: {GROUND}; }}
.label {{ width: 28; color: {WAITING}; }}
.row {{ height: 1; margin-top: 1; }}
#actions {{ height: 1; }}
Button {{ border: none; height: 1; min-width: 0; padding: 0 2; margin-right: 2;
          background: {FIELD}; color: {PAPER}; text-style: none; }}
Button:hover {{ background: {CURSOR}; }}
Button:focus {{ background: {AMBER}; color: {GROUND}; text-style: bold; }}
Button:disabled {{ background: {GROUND}; color: {WAITING}; }}
.hidden {{ display: none; }}
"""

MONTHS = [
    "JANEIRO",
    "FEVEREIRO",
    "MARÇO",
    "ABRIL",
    "MAIO",
    "JUNHO",
    "JULHO",
    "AGOSTO",
    "SETEMBRO",
    "OUTUBRO",
    "NOVEMBRO",
    "DEZEMBRO",
]


def month_title(period: str, state: str) -> Text:
    title = Text(f"{MONTHS[int(period[5:]) - 1]} {period[:4]}", style="bold")
    return title.append(f" · {state}", style=f"not bold {WAITING}")


def sides(left: RenderableType, right: RenderableType) -> Table:
    grid = Table.grid(expand=True)
    grid.add_column()
    grid.add_column(justify="right")
    grid.add_row(left, right)
    return grid


def pairs(lines: list[tuple[str, str]]) -> Table:
    grid = Table.grid(padding=(0, 3))
    grid.add_column(style=WAITING)
    grid.add_column(style=PAPER)
    for name, value in lines:
        grid.add_row(name, Text(value))
    return grid


def key_hints(*items: tuple[str, str]) -> Text:
    text = Text()
    for key, action in items:
        text.append(key, style=f"bold {PAPER}")
        text.append(f" {action}  ", style=WAITING)
    return text


def blocked_panel(error: core.BlockedError) -> Group:
    return Group(
        Text("ERRO DE VALIDAÇÃO", style=f"bold {ERROR}"),
        pairs(
            [
                ("Campo", error.field),
                ("Valor esperado", error.expected),
                ("Valor encontrado", error.found),
                ("Tela atual", error.where),
            ]
        ),
    )


def _yes_no(value: bool) -> str:
    return "SIM" if value else "NÃO"


def summary(p: core.Payload) -> list[tuple[str, str]]:
    """The (label, value) lines the user reads before confirming a step."""
    lines = [("Competência", core.label(p.period))]
    match p:
        case core.RevenueConfirmation():
            lines += [("Faturamento", core.brl(p.revenue))]
        case core.PgdasDeclaration():
            lines += [
                ("Regime", p.accounting_basis),
                ("Mercado interno", core.brl(p.domestic_revenue)),
                ("Mercado externo", core.brl(p.foreign_revenue)),
                ("Atividade", p.activity),
                ("Classificação", p.classification),
                ("Receita da atividade", core.brl(p.activity_revenue)),
                ("ISS fixo", core.brl(p.fixed_iss)),
                ("CSLL · INSS/CPP · IRPJ suspensos", "em branco"),
            ]
        case core.DasRequest():
            lines += [("Valor", "o calculado pelo PGDAS-D")]
        case core.S1200():
            lines += [("Categoria", p.worker_category), ("Lotação tributária", p.tax_unit)]
            for r in p.rubrics:
                factor = "vazio" if r.factor is None else core.brl(r.factor)
                lines += [
                    (
                        f"Rubrica {r.code} · tabela {r.table}",
                        f"quantidade {core.brl(r.quantity)} · fator {factor} · valor {core.brl(r.amount)}",
                    )
                ]
            lines += [("Apuração de IR", " · ".join(sorted({r.income_tax_mode for r in p.rubrics})))]
        case core.S1210():
            lines += [
                ("Pró-labore bruto", core.brl(p.gross)),
                ("INSS", core.brl(p.inss)),
                ("Valor líquido", core.brl(p.net)),
                ("Data do pagamento", f"{p.paid_on:%d/%m/%Y}"),
                ("Demonstrativo", p.payslip_id),
            ]
        case core.S1299():
            lines += [
                ("Remuneração no período", _yes_no(p.has_remuneration)),
                ("Pagamento no período", _yes_no(p.has_payment)),
                ("Comercialização de produção", _yes_no(p.has_rural_sales)),
                ("Trabalhador avulso", _yes_no(p.has_dock_workers)),
                ("Desoneração", _yes_no(p.has_payroll_relief)),
                ("Transmissão imediata da DCTFWeb", _yes_no(p.immediate_dctfweb)),
            ]
        case core.DctfwebCheck():
            lines += [
                ("INSS esperado (conferência)", core.brl(p.expected_inss)),
                ("Valor oficial", "o apresentado pela DCTFWeb"),
            ]
    return lines


def ruler(period: str, today: date, width: int) -> Group:
    """The month the obligations fall due in (the one after the period): days, today, deadlines."""
    last = core.due_date(period, 31).day
    in_month = core.period_of(today) == core.shift(period, 1)

    def col(day: int) -> int:
        return round((day - 1) / (last - 1) * (width - 2))

    cells = [("─", RULE)] * width
    marks = [(1, WAITING), (15, PAPER), (20, PAPER), (last, WAITING)]
    if in_month and all(abs(col(today.day) - col(day)) > 3 for day, _ in marks):
        marks.append((today.day, AMBER))
    for day, color in marks:
        for i, char in enumerate(f"{day:02d}"):
            cells[col(day) + i] = (char, color)
    days = Text()
    for char, color in cells:
        days.append(char, style=color)

    today_line = Text(" " * col(today.day) + "▲ hoje", style=AMBER) if in_month else Text("")
    deadlines = Text(" " * col(15) + "│ eSocial", style=WAITING)
    deadlines.append(" " * (col(20) - col(15) - 9) + "│ DAS · DARF")
    return Group(days, today_line, deadlines)


class MoneyInput(Input):
    """Digits only, no separators to type: 920000 shows as 9.200,00."""

    def on_input_changed(self) -> None:
        digits = "".join(char for char in self.value if char.isdigit()).lstrip("0")
        shown = core.brl(Decimal(digits) / 100) if digits else ""
        if shown != self.value:
            self.value = shown
        self.cursor_position = len(self.value)


def date_input(id: str, classes: str = "") -> MaskedInput:
    """Digits only: 07102026 shows as 07/10/2026."""
    return MaskedInput(template="99/99/9999", id=id, classes=classes)


class FiscalScreen(Screen[None]):
    @property
    def fiscal(self) -> FiscalApp:
        return cast("FiscalApp", self.app)

    def static(self, selector: str) -> Static:
        return self.query_one(selector, Static)


class Home(FiscalScreen):
    BINDINGS = [
        Binding("n", "new_invoice", "nova nota"),
        Binding("s", "sync", "sincronizar notas"),
        Binding("d", "documents", "documentos"),
        Binding("a", "audit", "auditoria"),
        Binding("left", "shift_period(-1)", "período anterior", priority=True),
        Binding("right", "shift_period(1)", "período seguinte", priority=True),
        Binding("q", "app.quit", "sair"),
    ]
    lit: core.Step | None = None  # the single step waiting for a decision
    sync_note: RenderableType = ""  # outcome of the last invoice sync

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="page", can_focus=False):
            yield Static(id="title")
            yield Static(id="open")
            yield Static(id="closed")
            yield DataTable(id="obligations", cursor_type="row", cursor_foreground_priority="renderable")
            yield Static(id="notice")
        yield Static(id="keys")

    def on_mount(self) -> None:
        table = self.query_one(DataTable)
        table.add_column(" ", width=1)
        table.add_column("VENCE", width=5)
        table.add_column("OBRIGAÇÃO", width=14)
        table.add_column(Text("VALOR", justify="right"), width=11)
        table.add_column("SITUAÇÃO", width=30)
        self.sync_invoices()
        self.refresh_all()

    def on_screen_resume(self) -> None:
        self.refresh_all()

    def refresh_all(self) -> None:
        app = self.fiscal
        self.render_title(app)
        self.render_open_month(app)
        self.render_closed_period(app)
        self.render_keys()

    def render_title(self, app: FiscalApp) -> None:
        mode = Text("")
        if app.simulate:
            mode = Text(" SIMULAÇÃO · nada é enviado ", style=f"reverse {WAITING}")
        elif app.gateway is not None and app.gateway.environment is nfse.Environment.RESTRICTED:
            mode = Text(" HOMOLOGAÇÃO · sem efeito fiscal ", style=f"reverse {WAITING}")
        title = Text("ROTINA FISCAL  ", style="bold").append_text(mode)
        self.static("#title").update(sides(title, Text(f"{app.today:%d/%m/%Y}", style=WAITING)))

    def render_open_month(self, app: FiscalApp) -> None:
        """Invoices issued this month; independent from the closing below."""
        period = core.period_of(app.today)
        issued = db.invoices(app.db, period)
        body: RenderableType
        if issued:
            body = Table.grid(padding=(0, 2))
            for _ in range(3):
                body.add_column()
            body.add_column(justify="right")
            body.add_column()
            for inv in issued:
                label, color = INVOICE_STATUS[inv.status]
                body.add_row(
                    Text(f"{inv.date:%d/%m}", style=WAITING),
                    f"NFS-e {inv.number}",
                    Text(inv.client[:24]),
                    core.brl(inv.amount),
                    Text(label, style=color),
                )
            total = Text(core.brl(nfse.countable(issued)), style="bold")
            body.add_row("", "", Text("total", style=WAITING), total, "")
        else:
            body = Text("Nenhuma nota neste mês. A tecla n emite a primeira.", style=WAITING)
        self.static("#open").update(Group(Text(""), month_title(period, "em aberto"), Text(""), body))

    def render_closed_period(self, app: FiscalApp) -> None:
        """The closed period as a calendar of due dates, with exactly one row lit."""
        period, today = app.period, app.today
        records = db.steps(app.db, period)
        status = {step: r.status for step, r in records.items()}
        self.lit = core.next_step(status)
        revenue = db.inputs(app.db, period).revenue
        issued = db.invoices(app.db, period)
        pay, blocked = None, None
        try:
            pay = core.payroll(app.cfg, period)
        except core.BlockedError as error:
            blocked = error

        open_steps = [s for s in core.STEPS if s.due_day and status.get(s.id) != Status.DONE]
        if not open_steps:
            countdown = Text("período processado", style=SEAL)
        else:
            day = min(s.due_day for s in open_steps if s.due_day)
            left = (core.due_date(period, day) - today).days
            target = "o eSocial" if day == 15 else "DAS e DARF"
            if left < 0:
                countdown = Text(f"{target} venceu há {-left} dias", style=ERROR)
            else:
                countdown = Text(f"{left} dias para {target}", style=AMBER if left <= 5 else WAITING)
        heading = sides(month_title(period, "encerrado"), countdown)
        self.static("#closed").update(
            Group(heading, Text(""), ruler(period, today, self.query_one(DataTable).size.width or 78))
        )

        amounts: dict[str, Decimal | None] = {
            "invoices": revenue or nfse.countable(issued) or None,
            "pgdas": revenue,
            "s1200": pay.gross if pay else None,
            "s1210": pay.net if pay else None,
            "darf": pay.inss if pay else None,
        }
        table = self.query_one(DataTable)
        table.clear()
        for index, step in enumerate(core.STEPS):
            state, lit = status.get(step.id, Status.PENDING), step is self.lit
            if state == Status.DONE:
                situation = Text(f"{step.stamp} {records[step.id].updated_at:%d/%m}", style=SEAL)
            elif state == Status.BLOCKED:
                situation = Text("BLOQUEADA", style=f"bold {ERROR}")
            elif state == Status.SENDING:
                situation = Text("ENVIO INTERROMPIDO", style=f"bold {ERROR}")
            elif lit:
                situation = Text("AGUARDA CONFIRMAÇÃO", style=f"bold {AMBER}")
            else:
                situation = Text("·", style=WAITING)
            color = PAPER if lit or state == Status.DONE else WAITING
            amount = amounts.get(step.id)
            # The DARF amount is only the expected one until DCTFWeb shows the official value.
            amount_cell = Text(
                core.brl(amount) if amount else "", style=WAITING if step.id == "darf" else color, justify="right"
            )
            valid = sum(inv.status != "CANCELLED" for inv in issued)
            name = f"Notas ({valid})" if step.id == "invoices" else step.name
            due = f"{core.due_date(period, step.due_day):%d/%m}" if step.due_day else "—"
            table.add_row(
                Text("▸" if lit else "", style=AMBER),
                Text(due, style=WAITING),
                Text(name, style=color),
                amount_cell,
                situation,
                key=step.id,
            )
            if lit:
                table.move_cursor(row=index)

        self.static("#notice").update(blocked_panel(blocked) if blocked else self.sync_note)

    def sync_invoices(self) -> None:
        """Pull issued invoices from the government. Blocks the UI for a second or two."""
        # ponytail: synchronous on the UI thread; move to a worker if the ADN gets slow.
        app = self.fiscal
        if app.gateway is None:
            return
        try:
            report = app.gateway.sync()
        except core.BlockedError as blocked:
            self.sync_note = blocked_panel(blocked)
        except httpx.HTTPError as offline:
            self.sync_note = Text(
                f"Sem resposta do Emissor Nacional ({type(offline).__name__}). s tenta de novo.", style=ERROR
            )
        else:
            parts = [f"{report.issued} nota(s) nova(s)"] if report.issued else ["Notas em dia com o Emissor Nacional"]
            if report.events:
                parts.append(f"{report.events} evento(s) aplicado(s)")
            if report.pdfs_missing:
                parts.append(f"{report.pdfs_missing} PDF(s) indisponível(is), tento de novo na próxima")
            self.sync_note = Text(" · ".join(parts) + ".", style=WAITING)

    def action_sync(self) -> None:
        self.sync_invoices()
        self.refresh_all()

    def action_documents(self) -> None:
        self.app.push_screen(Documents())

    def render_keys(self) -> None:
        table = self.query_one(DataTable)
        step = core.STEPS[table.cursor_row] if table.row_count else core.STEPS[0]
        verb = "revisar" if step is self.lit else "ver"
        self.static("#keys").update(
            key_hints(
                ("enter", f"{verb} {step.name}"),
                ("n", "nota"),
                ("s", "atualizar"),
                ("←→", "mês"),
                ("d", "docs"),
                ("a", "auditoria"),
                ("q", "sair"),
            )
        )

    def on_data_table_row_highlighted(self) -> None:
        self.render_keys()

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        if event.row_key.value is not None:
            self.app.push_screen(Review(core.STEP_BY_ID[event.row_key.value]))

    def action_shift_period(self, months: int) -> None:
        app = self.fiscal
        target = core.shift(app.period, months)
        if target < core.period_of(app.today):  # only periods that have already closed
            app.period = target
            self.refresh_all()

    def action_new_invoice(self) -> None:
        self.app.push_screen(NewInvoice())

    def action_audit(self) -> None:
        self.app.push_screen(AuditLog())


class Review(FiscalScreen):
    """A step's full summary and the only door to confirming it."""

    BINDINGS = [Binding("escape", "app.pop_screen", "voltar")]
    CONFIRM_LABEL = {
        "invoices": "Confirmar faturamento",
        "das": "Confirmar e gerar guia",
        "darf": "Confirmar e gerar guia",
    }

    def __init__(self, step: core.Step) -> None:
        super().__init__()
        self.step = step
        self.payload: core.Payload | None = None

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="page", can_focus=False):
            yield Static(id="heading")
            yield Static(id="question", classes="hidden")
            yield MoneyInput(id="money", classes="hidden")
            yield date_input("day", "hidden")
            yield Static(id="body")
            yield Static(id="error")
            with Horizontal(id="actions"):
                yield Button("Voltar", id="back")
                yield Button("Confirmar", id="confirm", disabled=True)
        yield Static(key_hints(("tab", "escolher"), ("enter", "acionar"), ("esc", "voltar")), id="keys")

    def on_mount(self) -> None:
        app, step = self.fiscal, self.step
        period = app.period
        due = f" · vence {core.due_date(period, step.due_day):%d/%m/%Y}" if step.due_day else ""
        heading = Text(step.name, style="bold")
        self.static("#heading").update(
            heading.append(f" · {step.target} · {core.label(period)}{due}", style=f"not bold {WAITING}")
        )
        confirm = self.query_one("#confirm", Button)
        confirm.label = self.CONFIRM_LABEL.get(step.id, "Confirmar e transmitir")

        records = db.steps(app.db, period)
        status = {name: r.status for name, r in records.items()}
        state = status.get(step.id, Status.PENDING)
        body, error = self.static("#body"), self.static("#error")

        if state == Status.DONE:
            record = records[step.id]
            seal = Text(f"{step.stamp} em {record.updated_at:%d/%m/%Y %H:%M}", style=f"bold {SEAL}")
            if record.receipt:
                seal.append(f" · recibo {record.receipt}", style=f"not bold {WAITING}")
            sent = pairs(summary(record.payload)) if record.payload else Text("")
            body.update(Group(seal, Text(""), sent))
            confirm.add_class("hidden")
        elif state == Status.SENDING:
            error.update(
                Text(
                    "O envio foi interrompido antes do recibo. Consulte o órgão antes de reenviar: "
                    "a etapa pode já ter sido recebida.",
                    style=ERROR,
                )
            )
            confirm.add_class("hidden")
        elif not core.is_unlocked(step, status):
            dependency = core.STEP_BY_ID[step.depends_on or step.id].name
            body.update(Text(f"Depende de {dependency}, que ainda não foi concluída.", style=WAITING))
            confirm.add_class("hidden")
        elif step.id == "invoices":
            valid = [inv for inv in db.invoices(app.db, period) if inv.status != "CANCELLED"]
            if valid:
                total = core.brl(nfse.countable(valid))
                doubt = [inv.number for inv in valid if inv.status == "REVIEW"]
                warning = (
                    f" Confira no portal antes: nota(s) {', '.join(doubt)} com evento ou tributação diferente."
                    if doubt
                    else ""
                )
                self.ask(
                    f"Faturamento identificado em {len(valid)} nota(s): R$ {total}. Confirmar?{warning}",
                    "#money",
                    total,
                )
            else:
                self.ask(f"Qual foi o faturamento da competência {core.label(period)}?", "#money")
        elif step.id == "s1210":
            self.ask("Em qual data o pró-labore foi efetivamente pago?", "#day")
        else:
            self.prepare()

    def ask(self, question: str, field: str, value: str = "") -> None:
        self.static("#question").update(Text(question))
        self.static("#question").remove_class("hidden")
        answer = self.query_one(field, Input)
        if value:
            answer.value = value
        answer.remove_class("hidden")
        answer.focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        try:
            if self.step.id == "invoices":
                self.prepare(revenue=core.BRL_AMOUNT.validate_python(event.value))
            else:
                self.prepare(paid_on=core.BR_DATE.validate_python(event.value))
        except ValidationError as invalid:
            problem = core.message(invalid.errors()[0])
            self.static("#error").update(Text(f"Não entendi “{event.value}”: {problem}.", style=ERROR))
            self.static("#body").update("")
            self.query_one("#confirm", Button).disabled = True

    def prepare(self, revenue: Decimal | None = None, paid_on: date | None = None) -> None:
        """Build and check the payload; only a clean one enables the confirm button."""
        app, step = self.fiscal, self.step
        body, error, confirm = self.static("#body"), self.static("#error"), self.query_one("#confirm", Button)
        revenue = revenue or db.inputs(app.db, app.period).revenue
        try:
            payload = core.build(step, app.cfg, app.period, revenue, paid_on)
            core.check(payload, app.cfg, app.period, app.today)
        except core.BlockedError as blocked:
            self.payload = None
            body.update(blocked_panel(blocked))
            error.update("")
            confirm.disabled = True
            return
        self.payload = payload
        body.update(pairs(summary(payload)))
        if step.transmits and not app.simulate:
            error.update(
                Text(f"O canal {step.target} ainda não está conectado nesta versão. Nada será enviado.", style=WAITING)
            )
            confirm.disabled = True
        else:
            error.update(Text("Validações conferidas.", style=SEAL))
            confirm.disabled = False
            confirm.focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        app, step, payload = self.fiscal, self.step, self.payload
        if event.button.id == "confirm" and payload is not None:
            match payload:
                case core.RevenueConfirmation():
                    db.save_input(app.db, app.period, "revenue", str(payload.revenue))
                case core.S1210():
                    db.save_input(app.db, app.period, "paid_on", payload.paid_on.isoformat())
                case _:
                    pass
            if step.transmits:
                # The intent is recorded before sending: a crash here stays visible on resume.
                db.mark(app.db, app.period, step.id, Status.SENDING, payload)
                db.mark(app.db, app.period, step.id, Status.DONE, receipt=f"SIMULATED-{datetime.now():%Y%m%d%H%M%S}")
            else:
                db.mark(app.db, app.period, step.id, Status.DONE, payload)
        app.pop_screen()


NEW_CLIENT = -1  # the "register a new client" entry of the client list


class NewInvoice(FiscalScreen):
    """Issue an NFS-e, independent from the closing. Value, date and description are always typed anew."""

    BINDINGS = [Binding("escape", "app.pop_screen", "voltar")]
    LABELS = {
        "amount": "Valor do serviço (R$)",
        "foreign_amount": "Valor na moeda do cliente",
        "date": "Data de competência",
        "description": "Descrição do serviço",
    }
    draft: core.InvoiceDraft | None = None  # set once the form has been reviewed
    number: int = 0  # DPS number reserved by the review
    known: int = 0  # how many clients the list showed last time

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="page", can_focus=False):
            title = Text("Nova NFS-e", style="bold")
            yield Static(title.append(" · Emissor Nacional", style=f"not bold {WAITING}"))
            with Horizontal(classes="row"):
                yield Static("Tomador", classes="label")
                yield Select[int]([], prompt="escolha o tomador", compact=True, id="client")
            with Horizontal(classes="row"):
                yield Static(self.LABELS["amount"], classes="label")
                yield MoneyInput(id="amount", placeholder="só os números")
            with Horizontal(classes="row"):
                yield Static(self.LABELS["foreign_amount"], classes="label", id="foreign_label")
                yield MoneyInput(id="foreign_amount", placeholder="só os números")
            with Horizontal(classes="row"):
                yield Static(self.LABELS["date"], classes="label")
                yield date_input("date")
            with Horizontal(classes="row"):
                yield Static(self.LABELS["description"], classes="label")
                yield Input(id="description", placeholder="texto que sai na nota")
            yield Static(id="body")
            yield Static(id="error")
            with Horizontal(id="actions"):
                yield Button("Voltar", id="back")
                yield Button("Revisar", id="review")
                yield Button("Corrigir", id="edit", classes="hidden")
                yield Button("Confirmar e emitir", id="issue", classes="hidden")
        yield Static(key_hints(("tab", "próximo campo"), ("enter", "acionar"), ("esc", "voltar")), id="keys")

    def clients(self) -> list[db.StoredClient]:
        app = self.fiscal
        try:
            return app.gateway.clients() if app.gateway else db.clients(app.db)
        except core.BlockedError:  # no model invoice yet: the list is simply empty
            return db.clients(app.db)

    def load_clients(self) -> None:
        """Fill the client list. A client registered a moment ago, or the only one there is, comes selected."""
        clients, select = self.clients(), self.query_one("#client", Select)
        newest = max(clients, key=lambda c: c.id).id if len(clients) > self.known and self.known else None
        chosen = newest or (clients[0].id if len(clients) == 1 else None)
        self.known = len(clients)
        options = [(f"{c.name[:30]} ({c.country})", c.id) for c in clients]
        select.set_options([*options, ("Cadastrar novo tomador…", NEW_CLIENT)])
        if chosen is not None:
            select.value = chosen

    def chosen_client(self) -> db.StoredClient | None:
        value = self.query_one("#client", Select).value
        return next((c for c in self.clients() if c.id == value), None)

    def on_mount(self) -> None:
        self.load_clients()

    def on_screen_resume(self) -> None:
        self.load_clients()  # back from registering a client

    def on_select_changed(self, event: Select.Changed) -> None:
        if event.value == NEW_CLIENT:
            event.select.clear()
            self.app.push_screen(NewClient())
            return
        client = self.chosen_client()
        currency = core.CURRENCIES[client.currency] if client else "moeda do cliente"
        self.static("#foreign_label").update(f"Valor em {currency}")
        # The contract price is only a starting value: it stays editable and is shown again in the review.
        self.query_one("#foreign_amount", Input).value = (
            core.brl(client.contract_amount) if client and client.contract_amount else ""
        )

    def set_editing(self, editing: bool) -> None:
        for field in (*self.query(Input), *self.query(Select)):
            field.disabled = not editing
        for button in self.query(Button):
            button.set_class(editing != (button.id in ("back", "review")), "hidden")
        if editing:
            self.draft = None
            self.static("#body").update("")
            self.static("#error").update("")

    def on_input_submitted(self) -> None:
        self.focus_next()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        match event.button.id:
            case "back":
                self.app.pop_screen()
            case "review":
                self.review()
            case "edit":
                self.set_editing(True)
                self.query_one("#amount", Input).focus()
            case "issue" if self.draft is not None:
                self.issue(self.draft)
            case _:
                pass

    def review(self) -> None:
        app, client = self.fiscal, self.chosen_client()
        typed = {field.id: field.value for field in self.query(Input)}
        problems = [] if client else ["Tomador: escolha um da lista ou cadastre um novo."]
        draft = None
        try:
            draft = core.InvoiceDraft.model_validate(typed, context={"today": app.today})
        except ValidationError as invalid:
            problems += [f"{self.LABELS[str(e['loc'][0])]}: {core.message(e)}." for e in invalid.errors()]
        if problems or draft is None or client is None:
            self.static("#error").update(Text("\n".join(problems), style=ERROR))
            return
        lines = [
            ("Tomador", f"{client.name} ({client.country})"),
            ("Endereço", f"{client.street}, {client.number} · {client.city}"),
            ("Valor", f"R$ {core.brl(draft.amount)}"),
            (f"Valor em {core.CURRENCIES[client.currency]}", core.brl(draft.foreign_amount)),
            ("Competência", f"{draft.date:%d/%m/%Y}"),
            ("Descrição", draft.description),
        ]
        if app.gateway is None:  # simulation: nothing below is real
            lines += [("Código do serviço", app.cfg.nfse.service_code), ("Tributação", app.cfg.nfse.taxation)]
            warning = Text("Simulação: a nota não será emitida de verdade.", style=WAITING)
        else:
            try:
                preview = app.gateway.preview()
            except core.BlockedError as blocked:
                self.static("#body").update(blocked_panel(blocked))
                return
            except httpx.HTTPError as offline:
                self.static("#error").update(
                    Text(f"Sem resposta do Emissor Nacional ({type(offline).__name__}).", style=ERROR)
                )
                return
            self.number, model = preview.number, preview.template
            lines += [
                ("Série e número da DPS", f"{model.series} · {preview.number}"),
                ("Código do serviço", f"{model.service_code} · NBS {model.nbs_code}"),
                ("Tributação", f"{app.cfg.nfse.taxation} · ISSQN não retido"),
                ("Demais campos", f"iguais aos da NFS-e {model.model_number}"),
            ]
            if preview.environment is nfse.Environment.PRODUCTION:
                warning = Text("Confira cada campo: a emissão não pode ser desfeita.", style=AMBER)
            else:
                warning = Text("Homologação: nota de teste, sem efeito fiscal.", style=WAITING)
        self.draft = draft
        self.set_editing(False)
        self.static("#body").update(pairs(lines))
        self.static("#error").update(warning)
        self.query_one("#edit", Button).focus()

    def issue(self, draft: core.InvoiceDraft) -> None:
        app, client = self.fiscal, self.chosen_client()
        if client is None:
            return
        if app.gateway is None:
            period = core.period_of(draft.date)
            db.save_invoice(
                app.db,
                db.Invoice(
                    key=f"SIMULATED-{datetime.now():%Y%m%d%H%M%S}",
                    period=period,
                    number=f"{len(db.invoices(app.db, period)) + 1:04d}",
                    date=draft.date,
                    client=client.name,
                    amount=draft.amount,
                    description=draft.description,
                    status="ISSUED",
                ),
            )
        else:
            self.query_one("#issue", Button).disabled = True  # one send per review, whatever the outcome
            try:
                app.gateway.issue(draft, self.number, client)
            except core.BlockedError as blocked:
                self.static("#body").update(blocked_panel(blocked))
                self.static("#error").update(Text("A nota não foi emitida. Corrija e revise de novo.", style=ERROR))
                self.query_one("#edit", Button).focus()
                return
        app.pop_screen()


class NewClient(FiscalScreen):
    """Register a client abroad. The fields are the ones the official NFS-e layout asks for."""

    BINDINGS = [Binding("escape", "app.pop_screen", "voltar")]
    FIELDS = {
        "name": ("Nome empresarial", "como deve sair na nota"),
        "country": ("País (2 letras)", "IT, US, DE…"),
        "tax_id": ("NIF (identificação fiscal)", "vazio se o país não exige"),
        "street": ("Logradouro", ""),
        "number": ("Número", ""),
        "complement": ("Complemento", "opcional"),
        "district": ("Bairro", ""),
        "city": ("Cidade", ""),
        "region": ("Estado, província ou região", ""),
        "postal_code": ("Código postal", ""),
        "contract_amount": ("Valor do contrato na moeda", "opcional, só os números"),
    }

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="page", can_focus=False):
            yield Static(Text("Novo tomador", style="bold").append(" · exterior", style=f"not bold {WAITING}"))
            for field, (name, placeholder) in self.FIELDS.items():
                with Horizontal(classes="row"):
                    yield Static(name, classes="label")
                    kind = MoneyInput if field == "contract_amount" else Input
                    yield kind(id=field, placeholder=placeholder)
            with Horizontal(classes="row"):
                yield Static("Moeda do contrato", classes="label")
                yield Select[str](
                    [(name, code) for code, name in core.CURRENCIES.items()],
                    prompt="escolha a moeda",
                    compact=True,
                    id="currency",
                )
            yield Static(id="error")
            with Horizontal(id="actions"):
                yield Button("Voltar", id="back")
                yield Button("Cadastrar", id="save")
        yield Static(key_hints(("tab", "próximo campo"), ("enter", "acionar"), ("esc", "voltar")), id="keys")

    def on_input_submitted(self) -> None:
        self.focus_next()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "back":
            self.app.pop_screen()
            return
        typed: dict[str, object] = {field.id or "": field.value for field in self.query(Input)}
        currency = self.query_one("#currency", Select).value
        typed["currency"] = currency if isinstance(currency, str) else None
        try:
            db.save_client(self.fiscal.db, core.Client.model_validate(typed))
        except ValidationError as invalid:
            labels = {**{k: v[0] for k, v in self.FIELDS.items()}, "currency": "Moeda do contrato"}
            problems = [
                f"{labels.get(str(e['loc'][0]) if e['loc'] else '', 'Tomador')}: {core.message(e)}."
                for e in invalid.errors()
            ]
            self.static("#error").update(Text("\n".join(problems), style=ERROR))
        except core.BlockedError as blocked:
            self.static("#error").update(blocked_panel(blocked))
        else:
            self.app.pop_screen()


class AuditLog(FiscalScreen):
    BINDINGS = [Binding("escape", "app.pop_screen", "voltar")]
    ACTIONS = {
        "SENDING": "envio iniciado",
        "DONE": "concluída",
        "BLOCKED": "bloqueada",
        "USER_CONFIRMED": "confirmado pelo usuário",
        "ISSUED": "emitida",
    }
    SUBJECTS = {"revenue": "Faturamento", "paid_on": "Data do pagamento", "nfse": "NFS-e"}

    def compose(self) -> ComposeResult:
        app = self.fiscal
        events = db.audit_trail(app.db, app.period)
        body: RenderableType
        if events:
            body = Table.grid(padding=(0, 2))
            body.add_column(style=WAITING, no_wrap=True, min_width=14)
            body.add_column(no_wrap=True, min_width=17)
            body.add_column(no_wrap=True, min_width=23)
            body.add_column(style=WAITING, overflow="ellipsis", no_wrap=True)
            for event in events:
                step = core.STEP_BY_ID.get(event.subject)
                subject = step.name if step else self.SUBJECTS.get(event.subject, event.subject)
                proof = event.detail.get("receipt") or event.detail.get("value") or event.detail.get("number") or ""
                body.add_row(
                    f"{event.at:%d/%m %H:%M:%S}",
                    subject,
                    self.ACTIONS.get(event.action, event.action),
                    Text(str(proof)),
                )
        else:
            body = Text(
                "Nada registrado neste período ainda. Cada confirmação e cada envio aparecem aqui.", style=WAITING
            )
        with VerticalScroll(id="page", can_focus=False):
            yield Static(Group(Text(f"O que foi feito em {core.label(app.period)}", style="bold"), Text(""), body))
        yield Static(key_hints(("esc", "voltar")), id="keys")


class Documents(FiscalScreen):
    """Every file kept for the period, with the hash that proves it was not altered."""

    BINDINGS = [Binding("escape", "app.pop_screen", "voltar")]

    def compose(self) -> ComposeResult:
        app = self.fiscal
        filed = db.documents(app.db, app.period)
        body: RenderableType
        if filed:
            body = Table.grid(padding=(0, 2))
            body.add_column(no_wrap=True, min_width=10)
            body.add_column(justify="right", no_wrap=True)
            body.add_column(style=WAITING, no_wrap=True)
            body.add_column(style=WAITING, overflow="ellipsis", no_wrap=True)
            for document in filed:
                body.add_row(
                    document.kind,
                    core.brl(document.amount) if document.amount else "",
                    f"{document.origin} {document.saved_at:%d/%m %H:%M} · sha256 {document.sha256[:12]}",
                    Text(document.path),
                )
        else:
            body = Text("Nenhum documento guardado neste período ainda.", style=WAITING)
        with VerticalScroll(id="page", can_focus=False):
            yield Static(Group(Text(f"Documentos de {core.label(app.period)}", style="bold"), Text(""), body))
            yield Static(Text(f"Pasta: {app.data_dir}", style=WAITING), id="notice")
        yield Static(key_hints(("esc", "voltar")), id="keys")


class FiscalApp(App[None]):
    CSS = CSS

    def __init__(
        self,
        session: Session,
        cfg: core.Config,
        today: date | None = None,
        simulate: bool = False,
        gateway: Gateway | None = None,
        data_dir: Path = db.DATA_DIR,
    ) -> None:
        super().__init__()
        self.db, self.cfg, self.simulate, self.gateway, self.data_dir = session, cfg, simulate, gateway, data_dir
        self.today = today or date.today()
        self.period = core.shift(core.period_of(self.today), -1)  # the closed period on screen

    def on_mount(self) -> None:
        self.push_screen(Home())
