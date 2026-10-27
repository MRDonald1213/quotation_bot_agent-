from __future__ import annotations

import typer

from .logging_config import setup_logging

app = typer.Typer(
    name="freight",
    help=("AI Freight Agent — Workflows A+B+C: local quotation intake, clarification "
          "drafts, supplier replies, and deterministic cross-carrier comparison. "
          "Email approval/export only; never sends email."),
    no_args_is_help=True,
)
shipment_app = typer.Typer(help="Shipment context for quotation workflows.", no_args_is_help=True)
intake_app = typer.Typer(help="Import and process intake documents.", no_args_is_help=True)
quote_app = typer.Typer(help="Inspect quotations and version history.", no_args_is_help=True)
questions_app = typer.Typer(help="Generate shipment-wide clarification questions and drafts.",
                            no_args_is_help=True)
email_app = typer.Typer(help="Review, approve, and export local email drafts. Never sends email.",
                        no_args_is_help=True)
reply_app = typer.Typer(help="Import supplier replies and automatically update quotations.",
                        no_args_is_help=True)
config_app = typer.Typer(help="Show configuration.", no_args_is_help=True)
db_app = typer.Typer(help="Database utilities.", no_args_is_help=True)
report_app = typer.Typer(help="Export deterministic Workflow C comparison reports.", no_args_is_help=True)

app.add_typer(shipment_app, name="shipment")
app.add_typer(intake_app, name="intake")
app.add_typer(quote_app, name="quote")
app.add_typer(questions_app, name="questions")
app.add_typer(email_app, name="email")
app.add_typer(reply_app, name="reply")
app.add_typer(config_app, name="config")
app.add_typer(db_app, name="db")
app.add_typer(report_app, name="report")


@app.callback()
def main_callback(
    verbose: bool = typer.Option(False, "--verbose", "-v",
                                 help="Info-level logs (email addresses stay masked)."),
) -> None:
    setup_logging(verbose)


from .cli_commands import comparison as comparison_cmd  # noqa: E402
from .cli_commands import config as config_cmd  # noqa: E402
from .cli_commands import db as db_cmd  # noqa: E402
from .cli_commands import (  # noqa: E402
    email, gaps, health, intake, questions, quote, reply, report, shipment,
)

app.command(name="init")(health.cmd_init)
config_app.command(name="show")(config_cmd.show)
app.command(name="health")(health.cmd_health)
shipment_app.command(name="create")(shipment.create)
shipment_app.command(name="list")(shipment.list_)
shipment_app.command(name="show")(shipment.show)
shipment_app.command(name="close")(shipment.close)
shipment_app.command(name="review")(shipment.review)
shipment_app.command(name="edit")(shipment.edit)
shipment_app.command(name="approve")(shipment.approve)
intake_app.command(name="add")(intake.add)
intake_app.command(name="classify")(intake.classify_)
intake_app.command(name="process")(intake.process)
intake_app.command(name="list")(intake.list_)
intake_app.command(name="show")(intake.show)
intake_app.command(name="delete")(intake.delete)
quote_app.command(name="list")(quote.list_)
quote_app.command(name="show")(quote.show)
quote_app.command(name="reprocess")(quote.reprocess)
quote_app.command(name="history")(reply.history)
questions_app.command(name="generate")(questions.generate)
questions_app.command(name="list")(questions.list_)
questions_app.command(name="edit")(questions.edit)
email_app.command(name="draft")(email.draft)
email_app.command(name="list")(email.list_)
email_app.command(name="show")(email.show)
email_app.command(name="edit")(email.edit)
email_app.command(name="approve")(email.approve)
email_app.command(name="export")(email.export)
reply_app.command(name="import")(reply.import_)
reply_app.command(name="list")(reply.list_)
reply_app.command(name="show")(reply.show)
reply_app.command(name="reprocess")(reply.reprocess)
db_app.command(name="backup")(db_cmd.backup)
db_app.command(name="path")(db_cmd.path)
db_app.command(name="open")(db_cmd.open_db)
app.command(name="comparison", help="Deterministic cross-carrier comparison for a shipment (Workflow C).")(
    comparison_cmd.comparison)
report_app.command(name="export")(report.export)


class _GapsGroup(typer.core.TyperGroup):
    """`freight gaps <shipment-id>` is shorthand for `freight gaps show <shipment-id>`."""

    def resolve_command(self, ctx, args):
        if args and args[0] not in self.commands and not args[0].startswith("-"):
            args = ["show", *args]
        return super().resolve_command(ctx, args)


gaps_app = typer.Typer(cls=_GapsGroup, help="Show data gaps per quote; waive a gap with an audited reason.",
                       no_args_is_help=True)
gaps_app.command(name="show")(gaps.gaps)
gaps_app.command(name="waive")(gaps.waive)
app.add_typer(gaps_app, name="gaps")

__all__ = ["app", "main"]


def main() -> None:
    app()
