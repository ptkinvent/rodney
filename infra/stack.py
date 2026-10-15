from aws_cdk import CfnOutput, Duration, RemovalPolicy, Stack
from aws_cdk import aws_certificatemanager as acm
from aws_cdk import aws_cloudfront as cloudfront
from aws_cdk import aws_cloudfront_origins as origins
from aws_cdk import aws_dynamodb as dynamodb
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_route53 as route53
from aws_cdk import aws_route53_targets as route53_targets
from aws_cdk import aws_s3 as s3
from constructs import Construct

# Runs at CloudFront's edge on every viewer request, before S3 is asked for anything.
#   /slug   -> 301 to /slug/   (so relative links like css/style.css resolve correctly)
#   /slug/  -> fetch /slug/index.html from S3
#   /_...   -> 404, always. Names starting with an underscore are reserved for internals and
#              can never be app names, so nothing public should ever live there.
REWRITE_FUNCTION = """
function handler(event) {
  var r = event.request;
  var u = r.uri;
  if (u.charAt(1) === '_') { return { statusCode: 404, statusDescription: 'Not Found' }; }
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
        domain_name: str,  # e.g. "ptkinvent.com"; apps live here, the MCP server at mcp.<domain>
        hosted_zone_id: str,  # Route 53 zone for domain_name; must already exist
        google_client_id: str,  # OAuth client from Google Cloud Console (not a secret)
        blocked_authors: str = "",  # comma-separated author handles that may not use Rodney
        base_url: str | None = None,  # public URL used in tool results
        **kwargs,
    ) -> None:
        super().__init__(scope, id, **kwargs)

        mcp_domain_name = f"mcp.{domain_name}"
        app_domain_name = f"app.{domain_name}"  # the website: sign in and see your apps
        base_url = base_url or f"https://{domain_name}"

        # 1. Storage: a private bucket holding every app's files.
        bucket = s3.Bucket(
            self,
            "Apps",
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            encryption=s3.BucketEncryption.S3_MANAGED,
            enforce_ssl=True,
            removal_policy=RemovalPolicy.RETAIN,  # deleting the stack must not delete your apps
            # Direct uploads wait under _uploads/ until finalize-upload publishes them. Anything
            # left behind (abandoned or failed uploads) is deleted after a day.
            lifecycle_rules=[
                s3.LifecycleRule(
                    prefix="_uploads/",
                    expiration=Duration.days(1),
                    abort_incomplete_multipart_upload_after=Duration.days(1),
                )
            ],
        )

        # The registry of apps: who owns what. Durable (kept if the stack is deleted, with
        # point-in-time recovery), and reachable only by the Lambda, unlike the public bucket.
        apps_table = dynamodb.Table(
            self,
            "AppRegistry",
            partition_key=dynamodb.Attribute(name="slug", type=dynamodb.AttributeType.STRING),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            point_in_time_recovery_specification=dynamodb.PointInTimeRecoverySpecification(
                point_in_time_recovery_enabled=True
            ),
            removal_policy=RemovalPolicy.RETAIN,
        )
        apps_table.add_global_secondary_index(
            index_name="by-author",
            partition_key=dynamodb.Attribute(name="author", type=dynamodb.AttributeType.STRING),
            sort_key=dynamodb.Attribute(name="slug", type=dynamodb.AttributeType.STRING),
            projection_type=dynamodb.ProjectionType.ALL,
        )

        # Data written by full-stack apps: one table shared by all apps, kept apart by key (see
        # datastore.py). This is other people's data, so it is kept if the stack is deleted, and
        # point-in-time recovery lets us rewind accidental or malicious changes.
        data_table = dynamodb.Table(
            self,
            "AppData",
            partition_key=dynamodb.Attribute(name="pk", type=dynamodb.AttributeType.STRING),
            sort_key=dynamodb.Attribute(name="sk", type=dynamodb.AttributeType.STRING),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            point_in_time_recovery_specification=dynamodb.PointInTimeRecoverySpecification(
                point_in_time_recovery_enabled=True
            ),
            removal_policy=RemovalPolicy.RETAIN,
        )

        # Sign-in state that must be single-use: authorization codes and refresh tokens.
        # Rows expire on their own via the `ttl` attribute. Never put this in the apps
        # bucket, which CloudFront serves publicly.
        auth_table = dynamodb.Table(
            self,
            "Auth",
            partition_key=dynamodb.Attribute(name="id", type=dynamodb.AttributeType.STRING),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            time_to_live_attribute="ttl",
            removal_policy=RemovalPolicy.DESTROY,  # only short-lived login state lives here
        )

        # 2. Compute: the Python MCP + OAuth server. `src/` is zipped as-is; boto3 is already
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
                "AUTH_TABLE": auth_table.table_name,
                "APPS_TABLE": apps_table.table_name,
                "DATA_TABLE": data_table.table_name,
                "RODNEY_BLOCKED": blocked_authors,
                "RODNEY_BASE_URL": base_url,
                "RODNEY_ISSUER": f"https://{mcp_domain_name}",
                "RODNEY_APP_URL": f"https://{app_domain_name}",
                "GOOGLE_CLIENT_ID": google_client_id,
            },
        )
        bucket.grant_read_write(mcp)  # IAM: lets this Lambda (and only it) read/write the bucket
        auth_table.grant_read_write_data(mcp)
        apps_table.grant_read_write_data(mcp)  # includes the by-author index
        data_table.grant_read_write_data(mcp)  # transactions are covered by the item permissions
        # The Google client secret and token-signing key live in SSM Parameter Store as
        # SecureStrings (created by hand, never in this repo or in CloudFormation).
        mcp.add_to_role_policy(
            iam.PolicyStatement(
                actions=["ssm:GetParameters"],
                resources=[self.format_arn(service="ssm", resource="parameter", resource_name="rodney/*")],
            )
        )
        mcp_url = mcp.add_function_url(auth_type=lambda_.FunctionUrlAuthType.NONE)

        # The domains: one certificate covering the apps site and the MCP host, validated
        # through DNS records CDK adds to the Route 53 zone.
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

        # The website gets its own certificate, so adding it never touches the others.
        app_certificate = acm.Certificate(
            self, "AppCert", domain_name=app_domain_name, validation=acm.CertificateValidation.from_dns(zone)
        )

        # 3. Front doors. The apps host serves S3; the MCP host and the website serve the Lambda.
        #    All three are separate origins, so untrusted app JavaScript never shares an origin
        #    with the login endpoints or with a signed-in session.
        rewrite = cloudfront.Function(
            self,
            "Rewrite",
            code=cloudfront.FunctionCode.from_inline(REWRITE_FUNCTION),
            runtime=cloudfront.FunctionRuntime.JS_2_0,
        )

        dist = cloudfront.Distribution(
            self,
            "Dist",
            domain_names=[domain_name],
            certificate=certificate,
            additional_behaviors={
                # The data API for full-stack apps. Same origin as the apps, so their pages can call it
                # with a plain relative fetch() and no CORS.
                "/api/*": cloudfront.BehaviorOptions(
                    origin=origins.FunctionUrlOrigin(mcp_url, custom_headers={"x-rodney-surface": "data"}),
                    viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.HTTPS_ONLY,
                    allowed_methods=cloudfront.AllowedMethods.ALLOW_ALL,
                    cache_policy=cloudfront.CachePolicy.CACHING_DISABLED,
                    # Forwards the browser's Sec-Fetch-Site label, which the API uses to refuse other websites.
                    origin_request_policy=cloudfront.OriginRequestPolicy.ALL_VIEWER_EXCEPT_HOST_HEADER,
                )
            },
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
        )

        mcp_dist = cloudfront.Distribution(
            self,
            "McpDist",
            domain_names=[mcp_domain_name],
            certificate=certificate,
            default_behavior=cloudfront.BehaviorOptions(
                # The surface header tells the Lambda which host a request arrived on. CloudFront
                # sets it, so a viewer can't choose it.
                origin=origins.FunctionUrlOrigin(mcp_url, custom_headers={"x-rodney-surface": "mcp"}),
                viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.HTTPS_ONLY,
                allowed_methods=cloudfront.AllowedMethods.ALLOW_ALL,
                cache_policy=cloudfront.CachePolicy.CACHING_DISABLED,
                # Forwards Authorization, cookies and query strings to the Lambda.
                origin_request_policy=cloudfront.OriginRequestPolicy.ALL_VIEWER_EXCEPT_HOST_HEADER,
            ),
        )

        app_dist = cloudfront.Distribution(
            self,
            "AppDist",
            domain_names=[app_domain_name],
            certificate=app_certificate,
            default_behavior=cloudfront.BehaviorOptions(
                origin=origins.FunctionUrlOrigin(mcp_url, custom_headers={"x-rodney-surface": "app"}),
                viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
                allowed_methods=cloudfront.AllowedMethods.ALLOW_ALL,
                cache_policy=cloudfront.CachePolicy.CACHING_DISABLED,
                origin_request_policy=cloudfront.OriginRequestPolicy.ALL_VIEWER_EXCEPT_HOST_HEADER,
            ),
        )

        # Alias records: Route 53's way of pointing a domain at CloudFront.
        for name, record_id, d in [
            (domain_name, "Apps", dist),
            (mcp_domain_name, "Mcp", mcp_dist),
            (app_domain_name, "Website", app_dist),
        ]:
            target = route53.RecordTarget.from_alias(route53_targets.CloudFrontTarget(d))
            route53.ARecord(self, f"{record_id}AliasA", zone=zone, record_name=name, target=target)
            route53.AaaaRecord(self, f"{record_id}AliasAAAA", zone=zone, record_name=name, target=target)

        CfnOutput(self, "CloudFrontDomain", value=dist.distribution_domain_name)
        CfnOutput(self, "McpUrl", value=f"https://{mcp_domain_name}/mcp")
        CfnOutput(self, "WebsiteUrl", value=f"https://{app_domain_name}/")
        CfnOutput(self, "BucketName", value=bucket.bucket_name)
        CfnOutput(self, "AppsTableName", value=apps_table.table_name)
        CfnOutput(self, "DataTableName", value=data_table.table_name)
