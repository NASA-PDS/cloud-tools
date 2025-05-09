"""Extract the list of users from the identified Cognito user pool."""
import datetime
import json
import sys

import boto3


page_size = 60  # max allowable
region = "us-west-2"  # default


def datetimeconverter(o):
    """Ensure that datetimes are handled as strings."""
    if isinstance(o, datetime.datetime):
        return str(o)


# Process the cognito user pool

if len(sys.argv) > 3 or len(sys.argv) < 2:
    print(f"Usage:\n\t{sys.argv[0]} <cognito_user_pool_id> {{<aws_region>}}")
    sys.exit(1)

user_pool_id = sys.argv[1]
if len(sys.argv) > 2:
    region = sys.argv[2]

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
print(json.dumps(user_pool, indent=4, default=datetimeconverter))
