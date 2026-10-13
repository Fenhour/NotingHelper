"""Offline Windows pipe interoperability smoke test; never connects to a real Vault."""
from __future__ import annotations

import json
from pathlib import Path
import os
import queue
import statistics
import subprocess
import sys
import threading
import time
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from word_picker import ObsidianBridgeClient, ObsidianBridgeTransportError  # noqa: E402
import pipe_bridge  # noqa: E402

SERVER = r"""
const Module=require('module');const original=Module._load;
Module._load=function(n,p,m){if(n==='obsidian')return {Plugin:class{},ItemView:class{},setIcon:()=>{},MarkdownView:class{},TFile:class{},Notice:class{}};return original.call(this,n,p,m)};
const Plugin=require(process.argv[1]);
const host=Object.create(Plugin.prototype);
host.settings={token:process.argv[2],port:Number(process.argv[3]),highlightJournal:'test.jsonl'};
host.app={vault:{getName:()=> 'isolated-test'}};
host.manifest={version:'pipe-test'};
host.highlightInFlight=new Map();
host.applyHighlight=async(payload,response)=>{
  response.writeHead(200);response.end(JSON.stringify({ok:true,capture_id:payload.capture_id,
    connections:host.connections,request_received_wall_ms:payload._receivedWallMs}));
};
host.startPipeServer();
host.connections=0;
host.pipeServer.on('connection',()=>{host.connections++});
host.pipeServer.once('listening',()=>process.stdout.write('ready\n'));
process.stdin.on('data',()=>{
  for(const socket of host.pipeSockets)socket.destroy();
  process.stdout.write('dropped\n');
});
"""


def main() -> None:
    if os.name != 'nt':
        print('SKIP: Windows named pipe only')
        return
    plugin = ROOT / 'obsidian-plugin/noting-helper-bridge/main.js'
    token = f'pipe-offline-{os.getpid()}'
    port = 25000 + os.getpid() % 1000
    process = subprocess.Popen(['node', '-e', SERVER, str(plugin), token, str(port)],
                               cwd=str(ROOT), stdin=subprocess.PIPE,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               text=True, bufsize=1)
    ready: queue.Queue[str] = queue.Queue()
    threading.Thread(target=lambda: ready.put(process.stdout.readline()), daemon=True).start()
    try:
        if ready.get(timeout=6).strip() != 'ready':
            raise RuntimeError(f'pipe server failed: {process.stderr.read()}')
        client = ObsidianBridgeClient.__new__(ObsidianBridgeClient)
        client.port, client.token = port, token
        session = pipe_bridge.warm(token, port)
        deadline = time.monotonic() + 5
        while not session.ready.wait(.01) and time.monotonic() < deadline:
            pass
        assert session.ready.is_set(), 'pipe did not preconnect'
        samples = []
        with mock.patch('word_picker.urllib.request.urlopen',
                        side_effect=AssertionError('HTTP fallback was not expected')):
            for i in range(8):
                t = time.perf_counter()
                result = client._request('/highlight', {'capture_id': f'isolate-{i}',
                                                         'color': 'yellow', 'trace': True}, timeout=3)
                samples.append((time.perf_counter() - t) * 1000)
                assert result['capture_id'] == f'isolate-{i}' and result['ok'] is True
                assert result['_bridge_channel'] == 'pipe'
                assert result['_bridge_connect_reused'] is True
                assert result['_bridge_pipe_connect_ms'] >= 0
                assert result['_bridge_send_ms'] >= 0
                assert result['_bridge_wait_response_ms'] >= 0
                assert result['connections'] == 1, 'all requests should reuse one pipe'
        process.stdin.write('drop\n')
        process.stdin.flush()
        assert process.stdout.readline().strip() == 'dropped'
        with mock.patch('word_picker.urllib.request.urlopen',
                        side_effect=AssertionError('uncertain write must not fallback HTTP')):
            try:
                client._request('/highlight', {'capture_id': 'after-drop', 'trace': True}, timeout=2)
            except ObsidianBridgeTransportError:
                pass  # Broken pipe: no cross-protocol blind replay.
        deadline = time.monotonic() + 5
        while not session.ready.wait(.02) and time.monotonic() < deadline:
            pass
        assert session.ready.is_set(), 'background reconnection did not recover'
        with mock.patch('word_picker.urllib.request.urlopen',
                        side_effect=AssertionError('recovered pipe should not use HTTP')):
            recovered = client._request('/highlight', {'capture_id': 'recovered',
                         'trace': True}, timeout=2)
        assert recovered['capture_id'] == 'recovered' and recovered['connections'] >= 2
        print(f'pipe: {len(samples)} reused requests, median {statistics.median(samples):.1f} ms '
              f'(min {min(samples):.1f}, max {max(samples):.1f}); reconnect verified; '
              'no Vault data accessed')
    finally:
        pipe_bridge.close_all()
        process.terminate()
        try:
            process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate()


if __name__ == '__main__':
    main()
