#!/usr/bin/env python3
"""Render docs/architecture.{svg,png} using official AWS architecture icons.

Regenerate with:  <venv>/python docs/architecture.py
Requires:         pip install diagrams && apt install graphviz
"""
from diagrams import Cluster, Diagram, Edge
from diagrams.aws.compute import Lambda
from diagrams.aws.database import Dynamodb
from diagrams.aws.integration import SimpleQueueServiceSqsQueue
from diagrams.aws.ml import Bedrock
from diagrams.aws.network import APIGateway
from diagrams.aws.security import WAF, Cognito, SecretsManager
from diagrams.onprem.vcs import Github, Gitlab

graph_attr = {"pad": "0.4", "nodesep": "0.7", "ranksep": "1.0"}

with Diagram(
    "AI Code Reviewer",
    filename="docs/architecture",
    show=False,
    direction="LR",
    curvestyle="curved",
    outformat=["png", "svg"],
    graph_attr=graph_attr,
):
    with Cluster("Git providers"):
        github = Github("GitHub")
        gitlab = Gitlab("GitLab")

    with Cluster("AWS  (us-east-1)"):
        with Cluster("Edge / AuthN"):
            waf = WAF("AWS WAF\nrate rule")
            apigw = APIGateway("API Gateway\n10 rps / burst 20")
            cognito = Cognito("Cognito\nLambda Authorizer")
        secrets = SecretsManager("Secrets Manager\nwebhook secrets")
        ingest = Lambda("Ingest Lambda\ndetect provider\nverify HMAC")
        with Cluster("Queue"):
            queue = SimpleQueueServiceSqsQueue("SQS\nPR-Review-Queue")
            dlq = SimpleQueueServiceSqsQueue("DLQ")
        worker = Lambda("Worker Lambda\nfetch diff -> review")
        bedrock = Bedrock("Amazon Bedrock\nNova")
        ddb = Dynamodb("DynamoDB\ncost attribution")

    github >> Edge(label="webhook") >> waf
    gitlab >> Edge(label="webhook", style="dotted") >> waf
    waf >> apigw
    apigw >> Edge(label="auth?", style="dashed", color="purple") >> cognito
    apigw >> Edge(label="authorized") >> ingest
    secrets >> Edge(label="HMAC key", style="dashed", color="gray") >> ingest
    ingest >> Edge(label="enqueue if valid") >> queue
    queue >> worker
    queue >> Edge(label="maxReceiveCount=5", color="red", style="dashed") >> dlq
    worker >> Edge(label="Converse API") >> bedrock
    worker >> Edge(label="token usage", style="dashed", color="orange") >> ddb
    worker >> Edge(label="review comment", color="#2e7d32", style="dotted") >> github
