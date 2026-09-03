#!/usr/bin/env python3
"""Render docs/architecture.{svg,png} using official AWS architecture icons.

Regenerate with:  <venv>/python docs/architecture.py
Requires:         pip install diagrams && apt install graphviz
"""
from diagrams import Cluster, Diagram, Edge
from diagrams.aws.compute import Lambda
from diagrams.aws.integration import SimpleQueueServiceSqsQueue
from diagrams.aws.ml import Bedrock
from diagrams.aws.network import APIGateway
from diagrams.aws.security import Cognito, WAF
from diagrams.onprem.vcs import Github

graph_attr = {"pad": "0.4", "nodesep": "0.7", "ranksep": "0.9"}

with Diagram(
    "AI Code Reviewer",
    filename="docs/architecture",
    show=False,
    direction="LR",
    curvestyle="curved",
    outformat=["png", "svg"],
    graph_attr=graph_attr,
):
    github = Github("GitHub\npull_request webhook")

    with Cluster("AWS  (us-east-1)"):
        cognito = Cognito("Cognito User Pool\n+ Lambda Authorizer\nBearer token / GitHub exempt")
        waf = WAF("AWS WAF\nrate rule:\n200 req/IP/5min")
        apigw = APIGateway("API Gateway\nthrottle:\n10 rps / burst 20")
        ingest = Lambda("Ingest Lambda\nverify\nX-Hub-Signature-256")
        queue = SimpleQueueServiceSqsQueue("SQS\nPR-Review-Queue")
        dlq = SimpleQueueServiceSqsQueue("DLQ\nPR-Review-Queue-dlq")
        worker = Lambda("Worker Lambda\nfetch diff -> review")
        bedrock = Bedrock("Amazon Bedrock\namazon.nova-lite-v1:0")

    github >> Edge(label="POST /webhook") >> waf >> apigw
    apigw >> Edge(label="auth check", style="dashed", color="purple") >> cognito
    apigw >> Edge(label="proxy (if authorized)") >> ingest
    ingest >> Edge(label="enqueue if valid") >> queue
    queue >> worker
    queue >> Edge(
        label="maxReceiveCount=5", color="red", style="dashed"
    ) >> dlq
    worker >> Edge(label="Converse API") >> bedrock
    worker >> Edge(
        label="post review comment", color="#2e7d32", style="dotted"
    ) >> github
