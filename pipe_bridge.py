"""Windows Obsidian highlight pipe: off-click connect, framed persistent requests.

Only highlight creation uses this channel. A pipe not ready before sending safely
falls back to HTTP; any error after a write is uncertain and must retain capture ID.
"""
from __future__ import annotations

import hashlib
import json
import os
import struct
import threading
import time


class PipeUncertain(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class PipeReplyError(Exception):
    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status = status


def _exchange(handle, request: dict, timeout: float) -> tuple[int, dict, int]:
    import _winapi
    body = json.dumps(request, ensure_ascii=False).encode('utf-8')
    if not 0 < len(body) <= 64 * 1024:
        raise ValueError('bridge request too large')
    deadline = time.monotonic() + timeout

    def finish(overlapped, error):
        if error == _winapi.ERROR_IO_PENDING:
            left = max(0, int((deadline - time.monotonic()) * 1000))
            if _winapi.WaitForMultipleObjects([overlapped.event], False, left) != _winapi.WAIT_OBJECT_0:
                overlapped.cancel()
                raise PipeUncertain('timeout')
        size, error = overlapped.GetOverlappedResult(False)
        if error:
            raise OSError(error, 'pipe I/O failed')
        return size

    message = struct.pack('!I', len(body)) + body
    try:
        written, error = _winapi.WriteFile(handle, message, overlapped=True)
        if finish(written, error) != len(message):
            raise PipeUncertain('write_failed')
    except PipeUncertain:
        raise
    except OSError as exc:
        raise PipeUncertain('write_failed') from exc
    write_wall_ms = time.time_ns() // 1_000_000
    data = bytearray()
    target = 4
    try:
        while len(data) < target:
            read, error = _winapi.ReadFile(handle, min(target - len(data), 65536), overlapped=True)
            count = finish(read, error)
            if not count:
                raise PipeUncertain('read_failed')
            data.extend(read.getbuffer()[:count])
            if len(data) == 4:
                length, = struct.unpack('!I', data)
                if not 0 < length <= 1024 * 1024:
                    raise PipeUncertain('invalid_response')
                target = length + 4
        reply = json.loads(data[4:].decode('utf-8'))
        status, result = reply['status'], reply['body']
        if not isinstance(status, int) or not isinstance(result, dict):
            raise PipeUncertain('invalid_response')
        return status, result, write_wall_ms
    except PipeUncertain:
        raise
    except (OSError, UnicodeError, ValueError, KeyError, TypeError) as exc:
        raise PipeUncertain('read_failed') from exc


class _Session:
    def __init__(self, token: str, port: int):
        self.token, self.port = token, port
        self.handle = None
        self.connect_ms = None
        self.ready = threading.Event()
        self.wake = threading.Event()
        self.stopped = threading.Event()
        self.lock = threading.Lock()
        self.thread = threading.Thread(target=self._connector, daemon=True,
                                       name='highlight-pipe-connector')
        self.thread.start()

    def _drop_locked(self):
        self.ready.clear()
        if self.handle is not None:
            import _winapi
            _winapi.CloseHandle(self.handle)
            self.handle = None
        self.wake.set()

    def _connector(self):
        import _winapi
        address = (r'\\.\pipe\noting-helper-'
                   + hashlib.sha256(f'{self.token}:{self.port}'.encode('utf-8')).hexdigest()[:20])
        while not self.stopped.is_set():
            if not self.ready.is_set():
                handle = None
                try:
                    start = time.perf_counter()
                    handle = _winapi.CreateFile(address,
                        _winapi.GENERIC_READ | _winapi.GENERIC_WRITE, 0,
                        _winapi.NULL, _winapi.OPEN_EXISTING, _winapi.FILE_FLAG_OVERLAPPED,
                        _winapi.NULL)
                    connect_ms = round((time.perf_counter() - start) * 1000, 2)
                    status, health, _ = _exchange(handle, {
                        'route': '/health', 'method': 'GET', 'payload': {},
                        'token': self.token}, timeout=2.0)
                    # Old bridge closes after one response. Never use its stale handle.
                    if status == 200 and health.get('pipe_persistent') is True:
                        with self.lock:
                            self.handle, self.connect_ms = handle, connect_ms
                            self.ready.set()
                        handle = None
                except (OSError, PipeUncertain, ValueError):
                    pass  # Not loaded yet, or incompatible runtime: retry in background.
                finally:
                    if handle is not None:
                        _winapi.CloseHandle(handle)
            self.wake.wait(0.8 if not self.ready.is_set() else 30.0)
            self.wake.clear()

    def request(self, route: str, payload: dict, timeout: float) -> dict | None:
        if not self.ready.is_set():
            return None
        queued_wall_ms = time.time_ns() // 1_000_000
        with self.lock:
            if not self.ready.is_set() or self.handle is None:
                return None
            start_wall_ms = time.time_ns() // 1_000_000
            try:
                status, result, written_ms = _exchange(self.handle, {
                    'route': route, 'method': 'POST', 'payload': payload,
                    'token': self.token}, timeout)
            except (PipeUncertain, OSError):
                self._drop_locked()
                raise
            if status >= 400:
                raise PipeReplyError(status, str(result.get('error') or
                                                  f'bridge returned {status}'))
            if payload.get('trace'):
                result['_bridge_channel'] = 'pipe'
                result['_bridge_connect_reused'] = True
                result['_bridge_pipe_connect_ms'] = self.connect_ms
                result['_bridge_queue_wait_ms'] = max(0, start_wall_ms - queued_wall_ms)
                result['_bridge_send_ms'] = max(0, written_ms - start_wall_ms)
                result['_bridge_wait_response_ms'] = max(0, time.time_ns() // 1_000_000 - written_ms)
                result['_pipe_written_wall_ms'] = written_ms
            return result

    def close(self):
        self.stopped.set()
        with self.lock:
            self._drop_locked()


_sessions: dict[tuple[str, int], _Session] = {}
_sessions_lock = threading.Lock()


def warm(token: str, port: int) -> _Session:
    if os.name != 'nt':
        raise RuntimeError('Windows named pipe only')
    with _sessions_lock:
        key = (token, port)
        if key not in _sessions:
            _sessions[key] = _Session(token, port)
        return _sessions[key]


def close_all():
    with _sessions_lock:
        sessions = list(_sessions.values())
        _sessions.clear()
    for session in sessions:
        session.close()


import atexit
atexit.register(close_all)
