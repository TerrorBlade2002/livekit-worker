"""
Setup SIP Trunk and Dispatch Rules for LiveKit Cloud.

Run this ONCE to configure LiveKit Cloud to accept inbound SIP calls
from TCN and route them to the VTA agent.

Usage:
    python setup_sip.py create-trunk
    python setup_sip.py create-dispatch-rule
    python setup_sip.py list-trunks
    python setup_sip.py list-rules
"""

import asyncio
import os
import sys

from dotenv import load_dotenv
from livekit import api

load_dotenv()

LIVEKIT_URL = os.getenv("LIVEKIT_URL", "").replace("wss://", "https://")
LIVEKIT_API_KEY = os.getenv("LIVEKIT_API_KEY")
LIVEKIT_API_SECRET = os.getenv("LIVEKIT_API_SECRET")


async def create_inbound_trunk():
    """Create an inbound SIP trunk that accepts calls from TCN."""
    lk = api.LiveKitAPI(LIVEKIT_URL, LIVEKIT_API_KEY, LIVEKIT_API_SECRET)
    try:
        trunk = await lk.sip.create_inbound_trunk(
            api.CreateSIPInboundTrunkRequest(
                trunk=api.SIPInboundTrunkInfo(
                    name="TCN-VTA-Inbound",
                    numbers=[],
                    allowed_addresses=["0.0.0.0/0"],
                )
            )
        )
        print("Created inbound SIP trunk:")
        print(f"  Trunk ID:   {trunk.sip_trunk_id}")
        print(f"  Name:       {trunk.name}")
        print("\nNote the Trunk ID — you'll need it for the dispatch rule.")
        print("\nNext steps:")
        print("  1. In LiveKit Cloud dashboard, find the SIP URI for this trunk")
        print("  2. Configure TCN's Linkback to point to that SIP URI instead of Retell's number")
        print("  3. Run: python setup_sip.py create-dispatch-rule")
        return trunk
    finally:
        await lk.aclose()


async def create_dispatch_rule(trunk_id: str = ""):
    """Create a dispatch rule that routes inbound SIP calls to the VTA agent."""
    lk = api.LiveKitAPI(LIVEKIT_URL, LIVEKIT_API_KEY, LIVEKIT_API_SECRET)
    try:
        rule = await lk.sip.create_sip_dispatch_rule(
            api.CreateSIPDispatchRuleRequest(
                name="VTA-Dispatch",
                trunk_ids=[trunk_id] if trunk_id else [],
                rule=api.SIPDispatchRule(
                    dispatch_rule_individual=api.SIPDispatchRuleIndividual(
                        room_prefix="vta-call-",
                        pin="",
                    )
                ),
                room_config=api.RoomConfiguration(
                    agents=[
                        api.RoomAgentDispatch(
                            agent_name="vta-emma",
                            metadata="",
                        )
                    ]
                ),
            )
        )
        print("Created SIP dispatch rule:")
        print(f"  Rule ID:    {rule.sip_dispatch_rule_id}")
        print(f"  Name:       {rule.name}")
        print("  Room prefix: vta-call-")
        print("  Agent:       vta-emma")
        return rule
    finally:
        await lk.aclose()


async def create_phone_number_dispatch_rule(e164: str = ""):
    """Create a dispatch rule scoped to a specific LiveKit Phone Number."""
    lk = api.LiveKitAPI(LIVEKIT_URL, LIVEKIT_API_KEY, LIVEKIT_API_SECRET)
    try:
        rule = await lk.sip.create_sip_dispatch_rule(
            api.CreateSIPDispatchRuleRequest(
                name="VTA-Dispatch-PhoneNumber",
                trunk_ids=[],
                inbound_numbers=[e164] if e164 else [],
                rule=api.SIPDispatchRule(
                    dispatch_rule_individual=api.SIPDispatchRuleIndividual(
                        room_prefix="vta-call-",
                        pin="",
                    )
                ),
                room_config=api.RoomConfiguration(
                    agents=[api.RoomAgentDispatch(agent_name="vta-emma", metadata="")]
                ),
            )
        )
        print("Created phone-number dispatch rule:")
        print(f"  Rule ID: {rule.sip_dispatch_rule_id}")
        print(f"  Name:    {rule.name}")
        print(f"  Numbers: {list(rule.inbound_numbers)}")
        return rule
    finally:
        await lk.aclose()


async def delete_dispatch_rule(rule_id: str):
    lk = api.LiveKitAPI(LIVEKIT_URL, LIVEKIT_API_KEY, LIVEKIT_API_SECRET)
    try:
        await lk.sip.delete_sip_dispatch_rule(
            api.DeleteSIPDispatchRuleRequest(sip_dispatch_rule_id=rule_id)
        )
        print(f"Deleted dispatch rule {rule_id}")
    finally:
        await lk.aclose()


async def list_trunks():
    lk = api.LiveKitAPI(LIVEKIT_URL, LIVEKIT_API_KEY, LIVEKIT_API_SECRET)
    try:
        resp = await lk.sip.list_sip_inbound_trunk(api.ListSIPInboundTrunkRequest())
        if not resp.items:
            print("No inbound SIP trunks found.")
            return
        for t in resp.items:
            print(f"  Trunk ID: {t.sip_trunk_id}  Name: {t.name}  Numbers: {t.numbers}")
    finally:
        await lk.aclose()


async def list_rules():
    lk = api.LiveKitAPI(LIVEKIT_URL, LIVEKIT_API_KEY, LIVEKIT_API_SECRET)
    try:
        resp = await lk.sip.list_sip_dispatch_rule(api.ListSIPDispatchRuleRequest())
        if not resp.items:
            print("No dispatch rules found.")
            return
        for r in resp.items:
            print(f"  Rule ID: {r.sip_dispatch_rule_id}  Name: {r.name}  Trunks: {r.trunk_ids}")
    finally:
        await lk.aclose()


async def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return

    cmd = sys.argv[1]
    if cmd == "create-trunk":
        await create_inbound_trunk()
    elif cmd == "create-dispatch-rule":
        trunk_id = sys.argv[2] if len(sys.argv) > 2 else ""
        await create_dispatch_rule(trunk_id)
    elif cmd == "list-trunks":
        await list_trunks()
    elif cmd == "list-rules":
        await list_rules()
    elif cmd == "create-phone-dispatch":
        e164 = sys.argv[2] if len(sys.argv) > 2 else ""
        await create_phone_number_dispatch_rule(e164)
    elif cmd == "delete-rule":
        await delete_dispatch_rule(sys.argv[2])
    else:
        print(f"Unknown command: {cmd}")
        print(__doc__)


if __name__ == "__main__":
    asyncio.run(main())
