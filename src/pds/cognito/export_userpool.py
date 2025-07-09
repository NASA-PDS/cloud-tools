"""Extract the list of users from the identified Cognito user pool."""
import json
import sys

import boto3

from pds.cognito import common_cognito_defs


# Process the cognito user pool

if len(sys.argv) > 4 or len(sys.argv) < 2:
    common_cognito_defs.cognito_tool_usage(exit_status=1)

user_pool_id = sys.argv[1]

page_size, region = common_cognito_defs.get_args(sys.argv[2:], exit_status=1)

cognito_client = boto3.client("cognito-idp", region)


has_next_page = True
next_page_token = None
users = []
while has_next_page:
    response = (
        cognito_client.list_users(UserPoolId=user_pool_id, Limit=page_size, PaginationToken=next_page_token)
        if next_page_token
        else cognito_client.list_users(UserPoolId=user_pool_id, Limit=page_size)
    )

    users.extend(response["Users"])

    next_page_token = response.get("PaginationToken")

    if next_page_token is None:
        has_next_page = False

user_pool = {"UserPoolId": f"{user_pool_id}", "Users": users}
print(json.dumps(user_pool, indent=4, default=common_cognito_defs.datetimeconverter))
