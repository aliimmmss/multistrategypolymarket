import logging
import time
import requests
from web3 import Web3
from eth_account import Account
from typing import List, Dict, Any, Optional

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Constants
FASTLANE_RELAY_URL = "https://tx-gateway.polygon.fastlane.xyz"  # FastLane Polygon Relay
POLYGON_RPC_URL = "https://polygon-rpc.com" # Standard Polygon RPC

class FastLaneClient:
    """
    Client for submitting MEV bundles to FastLane on Polygon.
    """
    def __init__(self, private_key: str, rpc_url: str = POLYGON_RPC_URL, relay_url: str = FASTLANE_RELAY_URL):
        self.w3 = Web3(Web3.HTTPProvider(rpc_url))
        self.relay_url = relay_url
        self.account = Account.from_key(private_key)
        self.address = self.account.address
        
        if not self.w3.is_connected():
            logger.error("Failed to connect to Polygon RPC")
            raise ConnectionError("Could not connect to Polygon RPC")
            
        logger.info(f"FastLaneClient initialized for address: {self.address}")

    def create_bundle(self, txs: List[Dict[str, Any]]) -> List[str]:
        """
        Signs a list of transaction dictionaries.
        """
        signed_txs = []
        nonce = self.w3.eth.get_transaction_count(self.address)
        
        for i, tx in enumerate(txs):
            # precise nonce management
            tx['nonce'] = nonce + i
            
            # Sign the transaction
            signed = self.w3.eth.account.sign_transaction(tx, self.account.key)
            signed_txs.append(signed.rawTransaction.hex())
            
        return signed_txs

    def submit_bundle(self, signed_txs: List[str], target_block: int, min_timestamp: Optional[int] = None, max_timestamp: Optional[int] = None) -> bool:
        """
        Submits a bundle of signed transactions to the FastLane relay.
        Uses the `eth_sendBundle` JSON-RPC method.
        """
        
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "eth_sendBundle",
            "params": [
                {
                    "txs": signed_txs,
                    "blockNumber": hex(target_block),
                    "minTimestamp": min_timestamp,
                    "maxTimestamp": max_timestamp,
                }
            ]
        }
        
        # Remove None values from params
        if min_timestamp is None:
            del payload["params"][0]["minTimestamp"]
        if max_timestamp is None:
            del payload["params"][0]["maxTimestamp"]
            
        try:
            logger.info(f"Submitting bundle to {self.relay_url} for block {target_block}")
            headers = {
                "Content-Type": "application/json",
                "User-Agent": "FastLane-Python-Client/1.0"
            }
            response = requests.post(self.relay_url, json=payload, headers=headers, timeout=5)
            response.raise_for_status()
            
            result = response.json()
            
            if "error" in result:
                logger.error(f"Bundle submission error: {result['error']}")
                return False
                
            logger.info(f"Bundle submitted successfully: {result}")
            return True
            
        except requests.exceptions.RequestException as e:
            logger.error(f"Failed to send bundle request: {e}", exc_info=True)
            return False

    def get_next_block(self):
        return self.w3.eth.block_number + 1

# Example usage (for testing only)
if __name__ == "__main__":
    # Dummy key for testing structure
    print("FastLaneClient module loaded.")
