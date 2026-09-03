"""Shared fixtures: put the Lambda source dirs on sys.path and stub the
env vars the authorizer reads at import time."""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

os.environ.setdefault('USER_POOL_ID', 'us-east-1_TESTPOOL')
os.environ.setdefault('CLIENT_ID', 'testclient000')
os.environ.setdefault('AWS_DEFAULT_REGION', 'us-east-1')

sys.path.insert(0, str(ROOT / 'lambda'))
sys.path.insert(0, str(ROOT / 'authorizer'))
