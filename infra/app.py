import aws_cdk as cdk

from stack import RodneyStack

app = cdk.App()

# The stack id stays "Spin": CloudFormation cannot rename a stack, only replace it.
RodneyStack(
    app,
    "Spin",
    domain_name=app.node.get_context("domainName"),
    hosted_zone_id=app.node.get_context("hostedZoneId"),
    google_client_id=app.node.get_context("googleClientId"),
    blocked_authors=app.node.try_get_context("blockedAuthors") or "",
    base_url=app.node.try_get_context("baseUrl"),
)
app.synth()
