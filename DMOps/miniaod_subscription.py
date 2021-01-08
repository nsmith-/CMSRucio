#!/usr/bin/env python
from __future__ import print_function
from rucio.client.client import Client

client = Client(account='transfer_ops')


filter = {
    'pattern': '^/.*/MINIAOD(|SIM)$',
    'did_type': 'CONTAINER',
    'scope': ['cms'],
}

rules = [
    {
        "copies": 1,
        "rse_expression": "ddm_quota>0&rse_type=DISK&country=US",
        "activity": "Data rebalancing",
        "grouping": "ALL",
        "weight": "ddm_quota",
    },
]

res = client.update_subscription(
    name='USMiniAOD',
    account='transfer_ops',
    filter=filter,
    replication_rules=rules,
    comments='Ensures a replica of MiniAOD in US',
    lifetime=False,
    retroactive=False,  # Not a supported feature
    dry_run=False,  # BUG: this is ignored
)
print(res)
