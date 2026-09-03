"""
Pulls early ERC-20 Transfer events for a set of Robinhood Chain tokens using
the Blockscout PRO API (REST for address/tx lookups, JSON-RPC for eth_getLogs).

Requires env var BLOCKSCOUT_PRO_API_KEY (set as a GitHub Actions secret).
Writes results to data/early_transfers.json in the repo.

This intentionally avoids the account-based get_token_transfers_by_address
pagination pattern (newest-first, expensive to page back to launch time).
Instead it fetches deployment block, then queries Transfer logs directly
for a block-range window starting at deployment -- far fewer calls.
"""

import os
import json
import time
import requests

CHAIN_ID = 4663  # Robinhood Chain
BASE_URL = f"https://api.blockscout.com/{CHAIN_ID}"
API_KEY = os.environ["BLOCKSCOUT_PRO_API_KEY"]

# keccak256("Transfer(address,address,uint256)")
TRANSFER_TOPIC0 = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"

TOKENS = {
    "PONS": "0x39dBED3a2bd333467115dE45665cC57F813C4571",
    "CASHCAT": "0x020bfC650A365f8BB26819deAAbF3E21291018b4",
    "AI": "0x2e8c31162b855a2ffa90f6f8634643ad6f111e18",
}

# How many blocks past deployment counts as "early window" for buyer
# discovery. Tune this once you know the chain's block time / typical
# time-to-100x. Start conservative and widen if you're missing known
# early wallets from the handoff notes.
EARLY_WINDOW_BLOCKS = 100_000

# Keep eth_getLogs ranges modest -- some providers cap block spans per call
# regardless of PRO tier, and the free PRO tier is 5 requests/second.
LOG_CHUNK_BLOCKS = 2000
RATE_LIMIT_SLEEP_SEC = 0.25


def rest_get(path, params=None):
    params = dict(params or {})
    params["apikey"] = API_KEY
    resp = requests.get(f"{BASE_URL}{path}", params=params, timeout=30)
    resp.raise_for_status()
    return resp.json()


def json_rpc(method, params):
    payload = {"id": 0, "jsonrpc": "2.0", "method": method, "params": params}
    resp = requests.post(
        f"{BASE_URL}/json-rpc",
        json=payload,
        headers={
            "content-type": "application/json",
            "authorization": f"Bearer {API_KEY}",
        },
        timeout=60,
    )
    resp.raise_for_status()
    data = resp.json()
    if "error" in data:
        raise RuntimeError(f"{method} error: {data['error']}")
    return data["result"]


def get_deployment_block(token_address):
    info = rest_get(f"/api/v2/addresses/{token_address}")
    tx_hash = info.get("creation_transaction_hash") or info.get("creation_tx_hash")
    if not tx_hash:
        raise RuntimeError(f"No creation tx recorded for {token_address}")
    tx = rest_get(f"/api/v2/transactions/{tx_hash}")
    return int(tx["block_number"])


def get_transfer_logs(token_address, from_block, to_block):
    logs = []
    start = from_block
    while start <= to_block:
        end = min(start + LOG_CHUNK_BLOCKS - 1, to_block)
        params = [
            {
                "fromBlock": hex(start),
                "toBlock": hex(end),
                "address": token_address,
                "topics": [TRANSFER_TOPIC0],
            }
        ]
        chunk = json_rpc("eth_getLogs", params)
        logs.extend(chunk)
        start = end + 1
        time.sleep(RATE_LIMIT_SLEEP_SEC)
    return logs


def decode_transfer_log(log):
    from_addr = "0x" + log["topics"][1][-40:]
    to_addr = "0x" + log["topics"][2][-40:]
    value = int(log["data"], 16)
    return {
        "tx_hash": log["transactionHash"],
        "block_number": int(log["blockNumber"], 16),
        "from": from_addr,
        "to": to_addr,
        "value": value,
    }


def main():
    results = {}
    for name, address in TOKENS.items():
        print(f"[{name}] resolving deployment block...")
        deploy_block = get_deployment_block(address)
        to_block = deploy_block + EARLY_WINDOW_BLOCKS
        print(f"[{name}] pulling Transfer logs, blocks {deploy_block}-{to_block}...")
        raw_logs = get_transfer_logs(address, deploy_block, to_block)
        decoded = [decode_transfer_log(log) for log in raw_logs]
        results[name] = {
            "address": address,
            "deployment_block": deploy_block,
            "window_end_block": to_block,
            "transfer_count": len(decoded),
            "transfers": decoded,
        }
        print(f"[{name}] {len(decoded)} early transfers captured")

    os.makedirs("data", exist_ok=True)
    out_path = "data/early_transfers.json"
    with open(out_path, "w") as f:
        json.dump(results, f, indent=2)
    print(f"Wrote {out_path}")


if __name__ == "__main__":
    main()
