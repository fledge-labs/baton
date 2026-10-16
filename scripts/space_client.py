# SPDX-License-Identifier: Apache-2.0
"""File-authenticated client for disposable Baton acceptance runs."""
import ssl
import uuid
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]


class Client:
    def __init__(self, args, name):
        self.name = name
        self.args = args
        self.token = (args.credentials_dir / (name + '.token')).read_text().strip()
        self.tls = ssl.create_default_context(cafile=str(args.ca_file)) if args.ca_file else True

    def call(self, operation, label=None, *, expected=200, **arguments):
        if label is not None:
            arguments['operation_id'] = str(uuid.uuid5(uuid.NAMESPACE_URL,
                'baton-space-acceptance/' + self.args.key + '/' + self.name + '/' + label))
        with httpx.Client(verify=self.tls, trust_env=False, timeout=30) as http:
            response = http.post(self.args.url.rstrip('/') + '/v1/space/' + operation,
                headers={'Authorization': 'Bearer ' + self.token}, json=arguments)
        allowed = (expected,) if isinstance(expected, int) else expected
        assert response.status_code in allowed, f'{operation}: expected {allowed}, got {response.status_code}; response suppressed'
        if response.status_code != 200:
            return {'http_status': response.status_code}
        payload = response.json()
        assert payload['x_protocol'] == 'baton.space.v1'
        return payload['result']

