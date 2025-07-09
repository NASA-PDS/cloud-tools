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


def usage():
    """Provide command line instructions."""
    print(f"Usage:\n\t{sys.argv[0]} <cognito_user_pool_id> {{--page-size=<page_size>}} {{--region=<aws_region>}}")


# Process the cognito user pool

if len(sys.argv) > 4 or len(sys.argv) < 2:
    usage()
    sys.exit(1)

user_pool_id = sys.argv[1]

for arg in sys.argv[2:]:
    if arg.startswith("--page-size"):
        page_size = int(arg.split("=")[1])
    elif arg.startswith("--region"):
        region = arg.split("=")[1]
    else:
        usage()
        sys.exit(1)

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
