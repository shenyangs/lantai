#!/usr/bin/env python3
"""Isolated, opt-in two-turn Native/Legacy dispatch smoke; no model text is saved."""
from __future__ import annotations
import argparse
import asyncio
import importlib.util
import json
import logging
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]


async def evaluate(working, *, auxiliary=False):
    from lantai.agents.model_factory import create_model_client, create_model_and_formatter
    from lantai.config.config import load_agent_config
    from lantai.providers import ProviderManager
    from lantai.research.chat import bind_chat
    from lantai.research.context import research_turn
    from lantai.research.projects import ResearchService
    from lantai.runtime.models import ModelMessage, ModelRequest
    from lantai.runtime.converters import LegacyMsg as Msg
    slot = load_agent_config('default').active_model
    if not ProviderManager().is_chat_model_slot_selectable(slot):
        raise RuntimeError('Configured provider not selectable')
    workspace = working / 'workspaces/default'
    service = ResearchService(workspace)
    results = []
    prompts = ('Reason about a chain where A precedes B and B precedes C; answer briefly.',
               'Continue the same reasoning: which prerequisites must finish before C? Answer briefly.')
    for engine in ('native', 'legacy'):
        project = service.create_project('Isolated inference budget verification', [], idempotency_key=engine,
                                         budget={'model_calls': 2, 'model_seconds': 180})
        binding = bind_chat(SimpleNamespace(meta={'research_project_id': project['project_id']}), workspace)
        history, turns = [], []
        if engine == 'native':
            client = create_model_client('default', model_slot=slot,
                dispatch_wrapper=None if auxiliary else binding.wrap_native)
        else:
            raw, formatter = create_model_and_formatter('default')
            client = raw if auxiliary else binding.wrap_legacy(raw)
        # Models above are constructed before entering the research turn.
        with research_turn(binding):
            for prompt in prompts:
                if engine == 'native':
                    history.append(ModelMessage.text('user', prompt))
                    kinds, response = {}, None
                    async for event in client.stream(ModelRequest(messages=history, generation={'max_tokens':1024})):
                        kinds[event.kind] = kinds.get(event.kind, 0) + 1
                        if event.response is not None:
                            response = event.response
                    if response is None or not response.message.text_content():
                        raise AssertionError('Native response incomplete')
                    history.append(response.message)
                    turns.append({'completed':True, 'event_counts':kinds})
                else:
                    history.append(Msg('user', prompt, 'user'))
                    formatted = await formatter.format(history)
                    response = await client(messages=formatted, max_tokens=1024)
                    chunks = 0
                    if hasattr(response, '__aiter__'):
                        async for chunk in response:
                            response = chunk
                            chunks += 1
                    if not getattr(response, 'content', None):
                        raise AssertionError('Legacy response incomplete')
                    history.append(Msg('assistant', response.content, 'assistant'))
                    turns.append({'completed':True, 'chunks':chunks})
            stopped = False
            try:
                if engine == 'native':
                    await client.complete(ModelRequest(messages=history))
                else:
                    await client(messages=await formatter.format(history))
            except ValueError as exc:
                stopped = 'budget' in str(exc)
        summary = service.project(project['project_id'])['inference']
        results.append({'engine':engine, 'turns':turns, 'third_call_blocked':stopped,
                        'inference':summary})
        if not stopped or summary['calls'] != 2:
            raise AssertionError('Budget did not block next dispatch')
    return {'passed': True, 'provider':slot.provider_id, 'model':slot.model,
            'model_text_persisted':False, 'credential_store':'temporary_encrypted_copy',
            'scope':'cached auxiliary text dispatch' if auxiliary else 'explicit main dispatch',
            'full_console_sse':False, 'results':results}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--working-dir', type=Path, required=True)
    parser.add_argument('--secret-dir', type=Path, required=True)
    parser.add_argument('--agent-id', default='default')
    parser.add_argument('--provider-id')
    parser.add_argument('--model')
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--auxiliary', action='store_true')
    args = parser.parse_args()
    if bool(args.provider_id) != bool(args.model):
        parser.error('Provider and model overrides must be supplied together')
    spec = importlib.util.spec_from_file_location('provider_smoke', ROOT / 'scripts/source_configured_provider_stream_smoke.py')
    smoke = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(smoke)
    sys.path.insert(0, str(ROOT / 'src'))
    logging.disable(logging.CRITICAL)
    with tempfile.TemporaryDirectory(prefix='lantai-research-chat-smoke-') as temporary:
        override = {'provider_id':args.provider_id, 'model':args.model} if args.provider_id else None
        working, secret, _ = smoke._stage_profile(source_working_dir=args.working_dir, source_secret_dir=args.secret_dir,
            agent_id=args.agent_id, target_root=Path(temporary), active_model_override=override)
        os.environ.update(LANTAI_WORKING_DIR=str(working), LANTAI_SECRET_DIR=str(secret),
            LANTAI_DISABLE_KEYRING='1', LANTAI_SKIP_KEYRING='1', LANTAI_REMINDER_DELIVERY_WORKER='0')
        try:
            result = asyncio.run(evaluate(working, auxiliary=args.auxiliary))
        except Exception as exc:
            result = {'passed':False, 'error_type':type(exc).__name__, 'model_text_persisted':False}
        args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2)+'\n')
        print(json.dumps(result))
        return 0 if result['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
