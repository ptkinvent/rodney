import os

import aws_cdk as cdk

from stack import RodneyStack

app = cdk.App()
users = os.environ.get("RODNEY_USERS")
if not users:
    raise SystemExit('Set RODNEY_USERS, e.g. RODNEY_USERS=\'{"ptkinvent":"you@gmail.com","friend":"them@gmail.com"}\'')

# The stack id stays "Spin": CloudFormation cannot rename a stack, only replace it.
RodneyStack(
    app,
    "Spin",
    users=users,
    domain_name=app.node.get_context("domainName"),
    hosted_zone_id=app.node.get_context("hostedZoneId"),
    google_client_id=app.node.get_context("googleClientId"),
    base_url=app.node.try_get_context("baseUrl"),
)
app.synth()
