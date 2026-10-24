"""Real Shell entry, exact owned groups; all files and processes are synthetic."""
import asyncio
import os
import shlex
import signal
import sys
from pathlib import Path

import pytest
from lantai.agents.tools import shell

BEAT_CODE = '''import os, signal, sys, time
from pathlib import Path
signal.signal(signal.SIGTERM, signal.SIG_IGN)
beat, pidfile = map(Path, sys.argv[1:3])
pidfile.write_text(str(os.getpid()))
while True:
    with beat.open('a') as f: f.write('x')
    time.sleep(.02)
'''

async def ready(paths, timeout=5):
    deadline = asyncio.get_running_loop().time() + timeout
    while not all(p.exists() and p.stat().st_size for p in paths):
        assert asyncio.get_running_loop().time() < deadline, 'synthetic process not ready'
        await asyncio.sleep(.01)

async def kill_exact(proc):
    if proc is None:
        return
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    await asyncio.wait_for(proc.wait(), 5)

async def unrelated_process(tmp_path):
    beat = tmp_path / 'unrelated.beat'
    pidfile = tmp_path / 'unrelated.pid'
    proc = await asyncio.create_subprocess_exec(
        sys.executable, '-c', BEAT_CODE, str(beat), str(pidfile),
        stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL, start_new_session=True,
    )
    await ready([beat, pidfile])
    return proc, beat

def draining_output(task):
    current = task.get_coro()
    for _ in range(15):
        frame = getattr(current, 'cr_frame', None)
        if frame is not None and frame.f_code.co_name == '_collect_limited_output_unix':
            awaited = getattr(current, 'cr_await', None)
            return getattr(awaited, '__name__', '') == 'wait_for'
        current = getattr(current, 'cr_await', None)
        if current is None:
            return None
    return None

@pytest.mark.asyncio
@pytest.mark.parametrize('repeat_cancel', [False, True])
async def test_cancel_parent_exited_while_draining_pipe_settles_owned_child_only(tmp_path, monkeypatch, repeat_cancel):
    beat, childpid = tmp_path / 'owned.beat', tmp_path / 'owned.pid'
    parent_code = '''import subprocess, sys
subprocess.Popen([sys.executable, '-c', sys.argv[1], *sys.argv[2:]], stdin=subprocess.DEVNULL)
'''
    command = 'exec ' + ' '.join(shlex.quote(p) for p in (
        sys.executable, '-c', parent_code, BEAT_CODE, str(beat), str(childpid),
    ))
    real_spawn = asyncio.create_subprocess_shell
    launched = []
    async def spawn_after_leader_exit(*args, **kwargs):
        proc = await real_spawn(*args, **kwargs)
        launched.append(proc)
        # Expose a legitimate scheduling boundary: leader exit before collection.
        deadline = asyncio.get_running_loop().time() + 5
        while proc.returncode is None:
            assert asyncio.get_running_loop().time() < deadline
            await asyncio.sleep(.01)
        await ready([beat, childpid])
        assert proc.returncode == 0
        assert os.getpgid(int(childpid.read_text())) == proc.pid
        return proc
    monkeypatch.setattr(shell.asyncio, 'create_subprocess_shell', spawn_after_leader_exit)
    unrelated, unrelated_beat = await unrelated_process(tmp_path)
    task = asyncio.create_task(shell.execute_shell_command(command, timeout=10, cwd=tmp_path))
    try:
        deadline = asyncio.get_running_loop().time() + 3
        while not (draining_output(task)):
            if task.done():
                response = task.result()
                pytest.fail('real Shell did not reach pipe drain: ' + str(response.is_error))
            assert asyncio.get_running_loop().time() < deadline
            await asyncio.sleep(.005)
        task.cancel()
        if repeat_cancel:
            await asyncio.sleep(.03)
            task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        owned_at, unrelated_at = beat.stat().st_size, unrelated_beat.stat().st_size
        await asyncio.sleep(.25)
        assert unrelated_beat.stat().st_size > unrelated_at, 'unrelated same executable was killed'
        assert beat.stat().st_size == owned_at, 'CancelledError escaped pipe drain with owned descendant still executing'
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        for proc in launched:
            await kill_exact(proc)
        await kill_exact(unrelated)

@pytest.mark.asyncio
@pytest.mark.parametrize('boundary', ['cancel', 'timeout'])
async def test_childfirst_stop_settles_descendant_only(tmp_path, boundary):
    beat, childpid = tmp_path / 'owned.beat', tmp_path / 'owned.pid'
    parent_code = '''import subprocess, sys, time
subprocess.Popen([sys.executable, '-c', sys.argv[1], *sys.argv[2:]], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
time.sleep(30)
'''
    command = 'exec ' + ' '.join(shlex.quote(p) for p in (
        sys.executable, '-c', parent_code, BEAT_CODE, str(beat), str(childpid),
    ))
    unrelated, unrelated_beat = await unrelated_process(tmp_path)
    task = asyncio.create_task(shell.execute_shell_command(command, timeout=.3 if boundary == 'timeout' else 10, cwd=tmp_path))
    pgid = None
    try:
        await ready([beat, childpid])
        pgid = os.getpgid(int(childpid.read_text()))
        if boundary == 'cancel':
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            result = await asyncio.wait_for(task, 8)
            assert result.is_error and result.metadata['exit_code'] == -1
        owned_at, unrelated_at = beat.stat().st_size, unrelated_beat.stat().st_size
        await asyncio.sleep(.2)
        assert beat.stat().st_size == owned_at
        assert unrelated_beat.stat().st_size > unrelated_at
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        if pgid is not None:
            try: os.killpg(pgid, signal.SIGKILL)
            except ProcessLookupError: pass
        await kill_exact(unrelated)
