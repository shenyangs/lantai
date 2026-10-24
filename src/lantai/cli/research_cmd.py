"""Explicit local evidence workflows and public metadata search."""
import json
from pathlib import Path

import click

from .http import print_json
from ..research.evidence import create_packet, audit_packet, load_record
from ..research.literature import search_literature


@click.group('research')
def research_group():
    """Create local evidence packets, audit citations, search public literature."""


@research_group.command('packet')
@click.option('--workspace', required=True, type=click.Path(exists=True, file_okay=False, path_type=Path))
@click.option('--source', 'sources', multiple=True, required=True)
def packet_cmd(workspace, sources):
    """Snapshot explicitly selected imported knowledge sources. No network."""
    try:
        print_json(create_packet(workspace, list(sources)))
    except (ValueError, OSError) as exc:
        raise click.ClickException(str(exc)) from exc


@research_group.command('audit')
@click.option('--workspace', required=True, type=click.Path(exists=True, file_okay=False, path_type=Path))
@click.option('--packet', required=True)
@click.option('--claims', required=True, type=click.Path(exists=True, dir_okay=False, path_type=Path))
def audit_cmd(workspace, packet, claims):
    """Validate {claims: [...], open_issues: [...]} JSON against a saved packet."""
    try:
        if claims.stat().st_size > 4 * 1024 * 1024:
            raise ValueError('claims input exceeds size limit')
        payload = json.loads(claims.read_text(encoding='utf-8'))
        if not isinstance(payload, dict) or not isinstance(payload.get('claims'), list):
            raise ValueError('expected a JSON object with a claims list')
        result = audit_packet(workspace, packet, payload['claims'], payload.get('open_issues', []))
        print_json(result)
    except (ValueError, OSError) as exc:
        raise click.ClickException(str(exc)) from exc
    if result['status'] == 'blocked':
        raise click.exceptions.Exit(2)


@research_group.command('show')
@click.option('--workspace', required=True, type=click.Path(exists=True, file_okay=False, path_type=Path))
@click.argument('record_id')
def show_cmd(workspace, record_id):
    """Read a stored packet or audit, checking record integrity."""
    try:
        print_json(load_record(workspace, record_id))
    except (ValueError, OSError) as exc:
        raise click.ClickException(str(exc)) from exc


@research_group.command('search')
@click.argument('query')
@click.option('--limit', type=click.IntRange(1, 20), default=5)
def search_cmd(query, limit):
    """Send QUERY to Crossref/OpenAlex; returns metadata, never claims full-text access."""
    try:
        result = search_literature(query, limit)
        print_json(result)
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
    if result['status'] != 'ok':
        raise click.exceptions.Exit(2)
