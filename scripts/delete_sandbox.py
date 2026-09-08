import os
import requests
from sandbox_api import SandboxAccountAPI

BASE_URL = "https://csp.infoblox.com/v2"
TOKEN = os.environ.get('Infoblox_Token')
SANDBOX_ID_FILE = "sandbox_id.txt"

# Read sandbox ID from file
try:
    with open(SANDBOX_ID_FILE, "r") as f:
        sandbox_id = f.read().strip()
except FileNotFoundError:
    print(f"ERROR: {SANDBOX_ID_FILE} not found. You must run create_sandbox.py first.")
    exit(1)

if not sandbox_id:
    print("WARNING: Empty sandbox ID. File is corrupted or empty.")
    exit(1)


# Updated deletion logic
def delete_sandbox(api: SandboxAccountAPI, sandbox_id: str) -> bool:
    endpoint = f"{api.base_url}/sandbox/accounts/{sandbox_id}"
    try:
        print(f"Sending DELETE request to: {endpoint}")
        response = requests.delete(endpoint, headers=api._headers())

        if response.status_code in [200, 204]:
            print(f"Sandbox {sandbox_id} deleted successfully.")
            return True
        else:
            print(f"ERROR: Failed to delete sandbox. Status: {response.status_code}")
            print(f"Response: {response.text}")
            return False
    except Exception as e:
        print(f"ERROR: Error deleting sandbox: {e}")
        return False


# Run delete
api = SandboxAccountAPI(base_url=BASE_URL, token=TOKEN)
deleted = delete_sandbox(api, sandbox_id)

if deleted:
    try:
        os.remove(SANDBOX_ID_FILE)
        print(f"Removed file: {SANDBOX_ID_FILE}")
    except OSError as e:
        print(f"WARNING: Could not remove file: {e}")
else:
    print(f"WARNING: Sandbox {sandbox_id} may still exist. Please verify manually.")
