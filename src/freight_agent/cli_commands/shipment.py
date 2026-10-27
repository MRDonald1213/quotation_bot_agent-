from __future__ import annotations

from decimal import Decimal
from typing import Optional

import typer
from rich.prompt import Prompt

from ..domain.enums import DeliveryScope, ShipmentStatus
from ..services import shipments as shipment_service
from .common import Table, console, err_console, settings_or_exit, typed_errors


@typed_errors
def create(
    reference: Optional[str] = typer.Option(None, "--reference", help="Unique human-readable reference."),
    name: Optional[str] = typer.Option(None, "--name"),
    origin: Optional[str] = typer.Option(None, "--origin"),
    destination: Optional[str] = typer.Option(None, "--destination"),
    cargo: Optional[str] = typer.Option(None, "--cargo", help="Cargo description."),
    weight_kg: Optional[str] = typer.Option(None, "--weight-kg"),
    volume_m3: Optional[str] = typer.Option(None, "--volume-m3"),
    delivery_scope: DeliveryScope = typer.Option(DeliveryScope.unknown, "--delivery-scope"),
    currency: str = typer.Option("EUR", "--currency", help="Target ISO-4217 currency."),
    notes: Optional[str] = typer.Option(None, "--notes"),
) -> None:
    """Create a shipment. Interactive when flags are omitted, scriptable with flags."""
    settings = settings_or_exit()

    def ask(value: Optional[str], prompt: str, required: bool = True) -> str:
        if value is not None:
            return value
        if not required:
            return ""
        return Prompt.ask(prompt)

    shipment = shipment_service.create_shipment(
        settings,
        reference=ask(reference, "Shipment reference"),
        name=ask(name, "Shipment name"),
        origin=ask(origin, "Origin"),
        destination=ask(destination, "Destination"),
        cargo_description=ask(cargo, "Cargo description"),
        weight_kg=Decimal(weight_kg) if weight_kg else None,
        volume_m3=Decimal(volume_m3) if volume_m3 else None,
        required_delivery_scope=delivery_scope if delivery_scope != DeliveryScope.unknown or reference is not None
        else DeliveryScope(Prompt.ask("Required delivery scope",
                                      choices=[str(s) for s in DeliveryScope], default="unknown")),
        target_currency=currency if currency != "EUR" or reference is not None
        else Prompt.ask("Target currency", default="EUR"),
        notes=notes,
    )
    console.print(f"[green]shipment created[/green] id={shipment.id} reference={shipment.reference}")


@typed_errors
def list_() -> None:
    settings = settings_or_exit()
    table = Table(title="shipments")
    for col in ("id", "reference", "name", "origin", "destination", "route", "scope", "currency", "status"):
        table.add_column(col)
    for s in shipment_service.list_shipments(settings):
        table.add_row(str(s.id)[:8], s.reference, s.name, s.origin, s.destination,
                      s.route_category or "-", str(s.required_delivery_scope),
                      s.target_currency, str(s.status))
    console.print(table)


@typed_errors
def show(id_or_reference: str) -> None:
    settings = settings_or_exit()
    s = shipment_service.get_shipment(settings, id_or_reference)
    console.print_json(s.model_dump_json(indent=2))


@typed_errors
def close(id_or_reference: str) -> None:
    settings = settings_or_exit()
    s = shipment_service.close_shipment(settings, id_or_reference)
    console.print(f"[green]closed[/green] shipment {s.reference} ({s.id})")


@typed_errors
def review() -> None:
    """List shipment proposals the agent created from documents, awaiting approval."""
    settings = settings_or_exit()
    proposals = [s for s in shipment_service.list_shipments(settings)
                 if s.status == ShipmentStatus.needs_review]
    if not proposals:
        console.print("[green]no shipment proposals pending review[/green]")
        return
    table = Table(title="shipment proposals awaiting approval")
    for col in ("id", "reference", "route", "origin", "destination", "cargo",
                "weight kg", "volume m3", "currency", "updated"):
        table.add_column(col)
    for s in proposals:
        table.add_row(str(s.id)[:8], s.reference, s.route_category or "?", s.origin,
                      s.destination, s.cargo_description or "-",
                      str(s.weight_kg) if s.weight_kg is not None else "-",
                      str(s.volume_m3) if s.volume_m3 is not None else "-",
                      s.target_currency, s.updated_at.strftime("%Y-%m-%d %H:%M:%S"))
    console.print(table)
    err_console.print("Check details with `freight shipment show <id>`, correct with "
                      "`freight shipment edit <id>`, then `freight shipment approve <id>`.")


@typed_errors
def edit(
    id_or_reference: str,
    origin: Optional[str] = typer.Option(None, "--origin"),
    destination: Optional[str] = typer.Option(None, "--destination"),
    origin_country: Optional[str] = typer.Option(None, "--origin-country"),
    destination_country: Optional[str] = typer.Option(None, "--destination-country"),
    cargo: Optional[str] = typer.Option(None, "--cargo"),
    weight_kg: Optional[str] = typer.Option(None, "--weight-kg"),
    volume_m3: Optional[str] = typer.Option(None, "--volume-m3"),
    name: Optional[str] = typer.Option(None, "--name"),
    currency: Optional[str] = typer.Option(None, "--currency"),
) -> None:
    """Correct a shipment proposal before approving it (proposals only)."""
    changes = {
        "origin": origin, "destination": destination,
        "origin_country": origin_country, "destination_country": destination_country,
        "cargo_description": cargo, "name": name, "target_currency": currency,
        "weight_kg": Decimal(weight_kg) if weight_kg else None,
        "volume_m3": Decimal(volume_m3) if volume_m3 else None,
    }
    settings = settings_or_exit()
    s = shipment_service.edit_proposed_shipment(settings, id_or_reference,
                                                **{k: v for k, v in changes.items() if v is not None})
    console.print(f"[green]proposal corrected[/green] {s.reference} status={s.status} "
                  f"route={s.route_category or '-'} updated={s.updated_at}")


@typed_errors
def approve(
    id_or_reference: str,
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the confirmation prompt."),
) -> None:
    """Approve a document-derived shipment proposal, making it an active shipment."""
    settings = settings_or_exit()
    s = shipment_service.get_shipment(settings, id_or_reference)
    if s.status != ShipmentStatus.needs_review:
        err_console.print(f"[yellow]shipment {s.reference} is {s.status}; only proposals can be approved[/yellow]")
        raise typer.Exit(1)
    console.print(f"route: {s.origin} ({s.origin_country or '?'}) -> "
                  f"{s.destination} ({s.destination_country or '?'})")
    console.print(f"cargo: {s.cargo_description or '-'}  weight: {s.weight_kg or '-'} kg  "
                  f"volume: {s.volume_m3 or '-'} m3  currency: {s.target_currency}")
    if not yes:
        if Prompt.ask("Approve this shipment?", choices=["y", "n"], default="n") != "y":
            console.print("not approved — correct with `freight shipment edit`, re-run approve later")
            return
    approved = shipment_service.approve_proposed_shipment(settings, str(s.id), s.updated_at)
    console.print(f"[green]shipment approved[/green] {approved.reference} is now open "
                  f"(category {approved.route_category or '-'})")
    console.print(f"[dim]next: freight questions generate {approved.reference}[/dim]")
