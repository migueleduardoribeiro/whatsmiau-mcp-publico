"""Read-only capability check. Never print keys, contacts, or message data."""
import json
import os
from pathlib import Path
from mcp_contracts import inspect_contracts
from server import Bridge


def main():
    config = json.loads(Path(os.environ['LYLA_MCP_CONFIG']).read_text())
    credentials = Path(os.environ['CREDENTIALS_DIRECTORY']) / 'whatsmiau.env'
    key = next(line.split('=', 1)[1].strip() for line in credentials.read_text().splitlines()
               if line.startswith('API_KEY='))
    report = inspect_contracts(Bridge(key, 'http://127.0.0.1:8080', 15), config['instance_id'])
    print(json.dumps(report, sort_keys=True))


if __name__ == '__main__':
    main()
