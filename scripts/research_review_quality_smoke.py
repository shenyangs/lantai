#!/usr/bin/env python3
"""Opt-in review quality checks; encrypted profile copies and model results stay isolated.

Only engineering annotations, assessment labels, counts and timing are written.
Prompts, responses, reasons and copied credentials are never printed or saved.
"""
from __future__ import annotations

import argparse
import asyncio
from contextlib import contextmanager
import hashlib
import importlib.util
import json
import logging
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]


async def evaluate(args, working: Path, corpus: dict):
    if any(type(case.get('require_critical_recheck', False)) is not bool for case in corpus['cases']):
        raise ValueError('require_critical_recheck must be a boolean annotation')
    from lantai.config.config import load_agent_config
    from lantai.providers import ProviderManager
    from lantai.research.projects import ResearchService
    from lantai.research.review import configured_review
    from lantai.research.store import ResearchStore

    class MemoryStore(ResearchStore):
        def __init__(self, workspace):
            self.db = sqlite3.connect(':memory:')
            self.db.row_factory = sqlite3.Row
            super().__init__(workspace)

        @contextmanager
        def transaction(self):
            try:
                self.db.execute('BEGIN IMMEDIATE')
                yield self.db
                self.db.commit()
            except BaseException:
                self.db.rollback()
                raise

    slot = load_agent_config('default').active_model
    if not ProviderManager().is_chat_model_slot_selectable(slot):
        raise RuntimeError('Configured model is not selectable')
    service = ResearchService(working / 'workspaces/default')
    service.store = MemoryStore(service.workspace)
    semaphore = asyncio.Semaphore(args.concurrency)

    async def judge(case, trial):
        async with semaphore:
            case_service = service
            relative = f'wiki/raw/sources/{case["id"]}-{trial}.txt'
            path = service.workspace / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(case['source'], encoding='utf-8')
            project = service.create_project('Check evidence boundaries', [relative],
                idempotency_key=f'p:{case["id"]}:{trial}')
            source = service.source(project['source_versions'][0])
            if case.get('note_provider'):
                from lantai.research.library import LibraryService
                case_service = ResearchService(working / f'note-cases/{case["id"]}-{trial}')
                paper_path = case_service.workspace / relative
                paper_path.parent.mkdir(parents=True, exist_ok=True)
                paper_path.write_text('Synthetic bibliographic attachment; no study result supplied.', encoding='utf-8')
                project = case_service.create_project('Check note evidence boundaries', [relative], idempotency_key='p')
                library = LibraryService(case_service.workspace)
                work = library.import_items([{'title': 'Synthetic study'}], origin={'provider': 'manual'})['items'][0]
                library.link_source(work['work_id'], project['source_versions'][0])
                note = library.annotate(work['work_id'], case['source'], origin={'provider': case['note_provider']})
                imported = library.add_note_source(note['id'], project['project_id'], 1, idempotency_key='note')
                project = imported['project']
                source = case_service.source(imported['source_version_id'])
                # Only synthetic inputs reach disk; all model output remains in RAM.
                memory = MemoryStore(case_service.workspace)
                with sqlite3.connect(case_service.store.path) as disk:
                    disk.backup(memory.db)
                case_service.store = memory
            revision = project['revision']
            for source_id in project['source_versions']:
                manifest = case_service.source_manifest(source_id)
                for block in range(len(manifest['blocks'])):
                    case_service.read_source(project['project_id'], revision, source_id, block)
            report_text = case['report']
            if case.get('figure_caption'):
                import base64
                from io import BytesIO
                from PIL import Image
                from lantai.research.figures import FigureService
                raster = BytesIO()
                Image.new('RGB', (60, 30), 'white').save(raster, format='PNG')
                figure = FigureService(case_service.workspace, store=case_service.store).upload(
                    project['project_id'], revision, base64.b64encode(raster.getvalue()).decode(),
                    idempotency_key=f'figure:{case["id"]}:{trial}')
                report_text += f'\n\n![{case["figure_caption"]}](research-figure:{figure["id"]})'
            report = case_service.save_report(project['project_id'], revision, report_text,
                [{'text': case.get('claim_text', case['report']), 'evidence': [{'source_version_id': source['id'], 'quote': case['source']}]}],
                idempotency_key=f'r:{case["id"]}:{trial}')
            started = time.monotonic()
            result = {'scenario': case['id'], 'trial': trial + 1, 'expected_assessment': case['expected_assessment'],
                'critical_recheck_required': case.get('require_critical_recheck', False),
                'critical_recheck_complete': False}
            try:
                review = await asyncio.wait_for(configured_review(case_service, project['project_id'], revision,
                    report['id'], 'default', model_slot=slot), timeout=args.timeout)
                assessments = [claim['assessment'] for claim in review['claims']]
                expected_status = case.get('expected_status', 'reviewed' if case['expected_assessment'] in {'supports', 'nonfactual'} else 'needs_revision')
                secondary = review.get('critical_review', {})
                recheck_complete = (secondary.get('required_claims', 0) > 0
                    and bool(secondary.get('checkpoint_ids')) and bool(secondary.get('claims')))
                allowed = case.get('allowed_assessments', [case['expected_assessment']])
                reference_assessments = [r['assessment'] for r in review.get('reference_review', {}).get('references', [])]
                # Rechecks may repeat a reference; compare the relation set and require a result.
                references_correct = ('expected_reference_assessments' not in case or
                    bool(reference_assessments) and set(reference_assessments) == set(case['expected_reference_assessments']))
                result.update(review_status=review['status'], assessments=assessments,
                    critical_claims=secondary.get('required_claims', 0),
                    critical_recheck_calls=len(secondary.get('checkpoint_ids', [])),
                    critical_recheck_complete=recheck_complete,
                    critical_assessments=[c['assessment'] for c in secondary.get('claims', [])],
                    critical_correct=all(c['assessment'] in allowed for c in secondary.get('claims', [])),
                    reference_assessments=reference_assessments, references_correct=references_correct,
                    dispositions=[c.get('disposition') for c in review['claims']],
                    review_calls=case_service.project(project['project_id'])['budget_usage']['review_calls'],
                    correct=bool(assessments) and all(a in allowed for a in assessments) and references_correct
                    and review['status'] == expected_status,
                    full_coverage=review['coverage'] == [[0, len(report_text)]],
                    unsafe_approval=expected_status == 'needs_revision' and review['status'] == 'reviewed')
            except Exception as exc:
                result.update(review_status='failed', error_type=type(exc).__name__, correct=False, full_coverage=False)
            finally:
                inference = case_service.project(project['project_id'])['inference']
                result['inference'] = {key: inference[key] for key in ('calls', 'known_tokens', 'unknown_token_calls',
                    'known_cost_cny', 'unknown_cost_calls', 'seconds')}
                result['configuration_versions'] = [item['version'] for item in inference['configurations']]
                if case_service is not service:
                    case_service.store.db.close()
            result['duration_seconds'] = round(time.monotonic() - started, 3)
            print(json.dumps(result), flush=True)
            return result

    try:
        results = await asyncio.gather(*(judge(case, trial) for case in corpus['cases'] for trial in range(args.trials)))
    finally:
        service.store.db.close()
    correct = sum(result['correct'] for result in results)
    minimum = corpus['minimum_accuracy']
    critical = [r for r in results if r.get('critical_claims', 0)]
    critical_accuracy = sum(r.get('critical_correct', False) for r in critical) / len(critical) if critical else None
    required = [r for r in results if r['critical_recheck_required']]
    missing = [{'scenario': r['scenario'], 'trial': r['trial']} for r in required
               if not r['critical_recheck_complete']]
    return {'provider': slot.provider_id, 'model': slot.model, 'trials': results, 'correct': correct,
        'total': len(results), 'accuracy': correct / len(results), 'minimum_accuracy': minimum,
        'critical_accuracy': critical_accuracy, 'critical_reviews': len(critical),
        'required_critical_reviews': len(required), 'missing_critical_reviews': missing,
        'passed': correct / len(results) >= minimum and (critical_accuracy is None or critical_accuracy >= minimum) and all(result['full_coverage'] for result in results)
        and not missing
        and not any(result.get('unsafe_approval', False) for result in results),
        'model_text_persisted': False, 'annotation_source': corpus['label_source']}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--working-dir', type=Path, required=True)
    parser.add_argument('--secret-dir', type=Path, required=True)
    parser.add_argument('--agent-id', default='default')
    parser.add_argument('--provider-id')
    parser.add_argument('--model')
    parser.add_argument('--corpus', type=Path, default=ROOT / 'tests/fixtures/research/review-quality-v2.json')
    parser.add_argument('--trials', type=int, default=3, choices=range(1, 6))
    parser.add_argument('--concurrency', type=int, default=3, choices=range(1, 5))
    parser.add_argument('--timeout', type=float, default=180)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if bool(args.provider_id) != bool(args.model):
        parser.error('--provider-id and --model must be supplied together')
    raw = args.corpus.read_bytes()
    corpus = json.loads(raw)
    if corpus.get('schema_version') != 1 or not corpus.get('cases'):
        parser.error('Unsupported or empty corpus')
    spec = importlib.util.spec_from_file_location('provider_smoke', ROOT / 'scripts/source_configured_provider_stream_smoke.py')
    smoke = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(smoke)
    sys.path.insert(0, str(ROOT / 'src'))
    logging.disable(logging.CRITICAL)
    with tempfile.TemporaryDirectory(prefix='lantai-research-quality-') as temporary:
        override = {'provider_id': args.provider_id, 'model': args.model} if args.provider_id else None
        working, secret, _ = smoke._stage_profile(source_working_dir=args.working_dir,
            source_secret_dir=args.secret_dir, agent_id=args.agent_id, target_root=Path(temporary),
            active_model_override=override)
        os.environ.update(LANTAI_WORKING_DIR=str(working), LANTAI_SECRET_DIR=str(secret),
            LANTAI_DISABLE_KEYRING='1', LANTAI_SKIP_KEYRING='1', LANTAI_REMINDER_DELIVERY_WORKER='0')
        try:
            result = asyncio.run(evaluate(args, working, corpus))
        except Exception as exc:
            result = {'passed': False, 'error_type': type(exc).__name__, 'model_text_persisted': False}
        result['corpus_sha256'] = hashlib.sha256(raw).hexdigest()
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        print(json.dumps({key: value for key, value in result.items() if key != 'trials'}), flush=True)
        return 0 if result['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
