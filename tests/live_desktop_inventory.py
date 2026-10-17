"""Read-only native-desktop MCP acceptance. Never positions any desktop item."""
import asyncio
import json
from pathlib import Path
import sys
import time

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def main():
    params=StdioServerParameters(command=sys.executable,args=[str(Path(__file__).resolve().parents[1]/"clickwarden_mcp.py")])
    report={}
    async with stdio_client(params) as (reader,writer):
        async with ClientSession(reader,writer) as client:
            await client.initialize()
            async def call(name,**args):
                result=await client.call_tool(name,args)
                assert not getattr(result,"is_error",False),result
                assert all(c.type=="text" for c in result.content)
                return json.loads(result.content[0].text)
            sid=None
            try:
                started=time.monotonic()
                start=await call("use_my_computer",objective="Read-only desktop inventory and layout dry run",app_or_url="desktop")
                sid=start["session_id"]
                for _ in range(10):
                    await asyncio.sleep(.2)
                    state=await call("fast_session_status",session_id=sid)
                    if state["status"]!="preparing":
                        break
                assert not state.get("error"),state
                report["ready_ms"]=round((time.monotonic()-started)*1000)
                await call("fast_session_control",session_id=sid,command="pause")
                inventory=await call("fast_desktop_inventory",session_id=sid)
                assert inventory["status"]=="observed",inventory
                plan=await call("fast_desktop_arrange",session_id=sid,shape="C",dry_run=True)
                assert plan["status"]=="planned",plan
                after=await call("fast_desktop_inventory",session_id=sid)
                assert after["revision"]==inventory["revision"],"Read-only test changed desktop state"
                state=await call("fast_session_status",session_id=sid)
                assert state["requests"]==0
                assert state["status"]=="paused"
                report.update(count=inventory["count"],spacing=inventory["spacing"],planned=plan["count"],
                              unchanged=True,remained_paused=True,vision_requests=state["requests"])
            finally:
                if sid:
                    report["end"]=await call("fast_session_end",session_id=sid,result="completed")
            print(json.dumps(report),flush=True)


if __name__=="__main__":
    asyncio.run(main())
