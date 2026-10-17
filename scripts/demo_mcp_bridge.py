"""Interactive text-only client for a fresh Clickwarden MCP process.

Accept one JSON {tool, arguments} per stdin line. This uses the developed MCP
tools unchanged; it does not provide an alternate desktop input implementation.
EOF or {"exit":true} stops any session owned by this client before closing MCP.
"""
import asyncio
import json
import os
from pathlib import Path
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def main():
    params = StdioServerParameters(command=sys.executable,
        args=[str(Path(__file__).resolve().parents[1] / 'clickwarden_mcp.py')],
        env={**os.environ, 'CLICKWARDEN_TEXT_ONLY': '1'})
    session_id = None
    async with stdio_client(params) as (reader, writer):
        async with ClientSession(reader, writer) as client:
            await client.initialize()
            print('CLICKWARDEN_BRIDGE_READY', flush=True)
            try:
                while True:
                    line = await asyncio.to_thread(sys.stdin.readline)
                    if not line:
                        break
                    try:
                        command = json.loads(line)
                        if command.get('exit'):
                            break
                        name = command['tool']
                        if name not in ('use_my_computer', 'list_windows') and not name.startswith('fast_'):
                            raise ValueError('Only Clickwarden session tools are accepted')
                        result = await client.call_tool(name, command.get('arguments', {}))
                        if any(c.type != 'text' for c in result.content):
                            raise RuntimeError('Non-text result refused')
                        text = '\n'.join(c.text for c in result.content)
                        print(text, flush=True)
                        if name == 'use_my_computer':
                            session_id = json.loads(text).get('session_id', session_id)
                        elif name == 'fast_session_end':
                            session_id = None
                    except Exception as exc:
                        print(json.dumps({'bridge_error':str(exc)}), flush=True)
            finally:
                if session_id:
                    await client.call_tool('fast_session_end', {'session_id':session_id, 'result':'stopped'})


if __name__ == '__main__':
    asyncio.run(main())
