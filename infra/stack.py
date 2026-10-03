from aws_cdk import CfnOutput, Duration, RemovalPolicy, Stack
from aws_cdk import aws_certificatemanager as acm
from aws_cdk import aws_cloudfront as cloudfront
from aws_cdk import aws_cloudfront_origins as origins
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_route53 as route53
from aws_cdk import aws_route53_targets as route53_targets
from aws_cdk import aws_s3 as s3
from constructs import Construct

# Runs at CloudFront's edge on every viewer request, before S3 is asked for anything.
#   /slug   -> 301 to /slug/   (so relative links like css/style.css resolve correctly)
#   /slug/  -> fetch /slug/index.html from S3
REWRITE_FUNCTION = """
function handler(event) {
  var r = event.request;
  var u = r.uri;
  if (u.endsWith('/')) { r.uri = u + 'index.html'; return r; }
  var last = u.substring(u.lastIndexOf('/') + 1);
  if (last.indexOf('.') === -1) {
    return { statusCode: 301, statusDescription: 'Moved Permanently', headers: { location: { value: u + '/' } } };
  }
  return r;
}"""


class RodneyStack(Stack):
    def __init__(
        self,
        scope: Construct,
        id: str,
        *,
        users: str,  # JSON: author -> password (temporary stand-in for real auth)
        domain_name: str | None = None,  # e.g. "ptkinvent.com" (needs hosted_zone_id)
        hosted_zone_id: str | None = None,  # Route 53 zone for domain_name; must already exist
        base_url: str | None = None,  # public URL used in tool results
        **kwargs,
    ) -> None:
        super().__init__(scope, id, **kwargs)

        if bool(domain_name) != bool(hosted_zone_id):
            raise ValueError("Provide both domain_name and hosted_zone_id, or neither.")
        base_url = base_url or (f"https://{domain_name}" if domain_name else "")

        # 1. Storage: a private bucket holding every app's files.
        bucket = s3.Bucket(
            self,
            "Apps",
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            encryption=s3.BucketEncryption.S3_MANAGED,
            enforce_ssl=True,
            removal_policy=RemovalPolicy.RETAIN,  # deleting the stack must not delete your apps
        )

        # 2. Compute: the Python MCP server. `src/` is zipped as-is; boto3 is already
        #    in the Lambda runtime, so there is nothing to build or install.
        mcp = lambda_.Function(
            self,
            "Mcp",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="rodney.handler.handler",  # module rodney/handler.py, function handler()
            code=lambda_.Code.from_asset("src", exclude=["**/__pycache__"]),
            timeout=Duration.seconds(30),
            memory_size=256,
            environment={
                "BUCKET_NAME": bucket.bucket_name,
                "RODNEY_USERS": users,
                "RODNEY_BASE_URL": base_url,
            },
        )
        bucket.grant_read_write(mcp)  # IAM: lets this Lambda (and only it) read/write the bucket
        mcp_url = mcp.add_function_url(auth_type=lambda_.FunctionUrlAuthType.NONE)

        # The domains: one certificate covering the apps site and the MCP host, validated
        # through DNS records CDK adds to the Route 53 zone.
        mcp_domain_name = f"mcp.{domain_name}" if domain_name else None
        zone = certificate = None
        if domain_name:
            zone = route53.HostedZone.from_hosted_zone_attributes(
                self, "Zone", hosted_zone_id=hosted_zone_id, zone_name=domain_name
            )
            # CloudFront only accepts certificates from us-east-1, which is where this stack lives.
            certificate = acm.Certificate(
                self,
                "Cert",
                domain_name=domain_name,
                subject_alternative_names=[mcp_domain_name],
                validation=acm.CertificateValidation.from_dns(zone),
            )

        # 3. Front door: CloudFront routes /mcp to the Lambda and everything else to S3.
        rewrite = cloudfront.Function(
            self,
            "Rewrite",
            code=cloudfront.FunctionCode.from_inline(REWRITE_FUNCTION),
            runtime=cloudfront.FunctionRuntime.JS_2_0,
        )

        dist = cloudfront.Distribution(
            self,
            "Dist",
            domain_names=[domain_name] if domain_name else None,
            certificate=certificate,
            default_behavior=cloudfront.BehaviorOptions(
                # Origin Access Control: S3 only answers CloudFront, never the public internet.
                origin=origins.S3BucketOrigin.with_origin_access_control(bucket),
                viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
                # No caching so edits show up immediately; fine at personal scale.
                cache_policy=cloudfront.CachePolicy.CACHING_DISABLED,
                function_associations=[
                    cloudfront.FunctionAssociation(
                        function=rewrite, event_type=cloudfront.FunctionEventType.VIEWER_REQUEST
                    )
                ],
            ),
            additional_behaviors={
                # Kept so the current connector keeps working; remove once clients use the MCP host.
                "/mcp": cloudfront.BehaviorOptions(
                    origin=origins.FunctionUrlOrigin(mcp_url),
                    viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.HTTPS_ONLY,
                    allowed_methods=cloudfront.AllowedMethods.ALLOW_ALL,
                    cache_policy=cloudfront.CachePolicy.CACHING_DISABLED,
                    origin_request_policy=cloudfront.OriginRequestPolicy.ALL_VIEWER_EXCEPT_HOST_HEADER,
                )
            },
        )

        if zone:
            # A separate front door for the MCP server, so untrusted app JavaScript (served from
            # the apps host) is never on the same origin as the MCP/login endpoints.
            mcp_dist = cloudfront.Distribution(
                self,
                "McpDist",
                domain_names=[mcp_domain_name],
                certificate=certificate,
                default_behavior=cloudfront.BehaviorOptions(
                    origin=origins.FunctionUrlOrigin(mcp_url),
                    viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.HTTPS_ONLY,
                    allowed_methods=cloudfront.AllowedMethods.ALLOW_ALL,
                    cache_policy=cloudfront.CachePolicy.CACHING_DISABLED,
                    origin_request_policy=cloudfront.OriginRequestPolicy.ALL_VIEWER_EXCEPT_HOST_HEADER,
                ),
            )
            # Alias records: Route 53's way of pointing a domain at CloudFront.
            for name, record_id, d in [
                (domain_name, "Apps", dist),
                (mcp_domain_name, "Mcp", mcp_dist),
            ]:
                target = route53.RecordTarget.from_alias(route53_targets.CloudFrontTarget(d))
                route53.ARecord(self, f"{record_id}AliasA", zone=zone, record_name=name, target=target)
                route53.AaaaRecord(self, f"{record_id}AliasAAAA", zone=zone, record_name=name, target=target)

        CfnOutput(self, "CloudFrontDomain", value=dist.distribution_domain_name)
        CfnOutput(self, "McpUrl", value=f"https://{mcp_domain_name or dist.distribution_domain_name}/mcp")
        CfnOutput(self, "BucketName", value=bucket.bucket_name)
