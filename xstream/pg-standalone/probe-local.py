#!/opt/gateway-venv/bin/python
# Licensed under the Apache License, Version 2.0.
"""Verify a real API route plus candidate-specific response header."""
import os
import sys
import time
from urllib.request import urlopen
import yaml

config = yaml.safe_load(open(sys.argv[1]))
expected = next(route['plugins']['response-rewrite']['headers']['set']['X-Gateway-Config']
                for route in config['routes'] if route['id'] == 'accounts-api')
for _ in range(10):
    try:
        with urlopen(os.getenv('GATEWAY_PROBE_URL', 'http://127.0.0.1:19080/api/ping'), timeout=2) as response:
            if response.status == 200 and response.headers.get('X-Gateway-Config') == expected:
                sys.exit(0)
    except Exception:
        pass
    time.sleep(0.5)
sys.exit(1)
