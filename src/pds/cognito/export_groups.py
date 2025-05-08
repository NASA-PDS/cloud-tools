"""Extract the user groups and members from the identified Cognito user pool."""
import datetime
import json
import sys

import boto3


def datetimeconverter(o):
    """Ensure that datetimes are handled as strings."""
    if isinstance(o, datetime.datetime):
        return str(o)


# Process the groups for the indicated cognito user pool

if len(sys.argv) != 2:
    print(f"Usage:\n\t{sys.argv[0]} <cognito_user_pool_id>")
    sys.exit(1)

# Replace with your Cognito User Pool ID
user_pool_id = sys.argv[1]
client = boto3.client("cognito-idp")

# Get a list of group names
groups = []
has_next_page = True
next_page_token = None
while has_next_page:
    try:
        response = (
            client.list_groups(UserPoolId=user_pool_id, NextToken=next_page_token)
            if next_page_token
            else client.list_groups(UserPoolId=user_pool_id)
        )
        groups.extend([group for group in response["Groups"]])
        next_page_token = response.get("NextToken")
        has_next_page = bool(next_page_token)
    except client.exceptions.ClientError as e:
        print(f"Error listing groups: {e}")
        sys.exit(1)

# Get details for each group
for group in groups:
    group["Users"] = []
    group_name = group["GroupName"]
    next_page_token = None
    has_next_page = True
    while has_next_page:
        try:
            response = (
                client.list_users_in_group(UserPoolId=user_pool_id, GroupName=group_name, NextToken=next_page_token)
                if next_page_token
                else client.list_users_in_group(UserPoolId=user_pool_id, GroupName=group_name)
            )
            group["Users"].extend(response["Users"])
            next_page_token = response.get("NextToken")
            has_next_page = bool(next_page_token)
        except client.exceptions.ClientError as e:
            print(f"Error listing users for {user_pool_id}/{group_name}: {e}")

user_pool = {"UserPoolId": f"{user_pool_id}", "Groups": groups}
print(json.dumps(user_pool, indent=4, default=datetimeconverter))
