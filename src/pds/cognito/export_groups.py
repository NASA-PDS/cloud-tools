"""Extract the user groups and members from the identified Cognito user pool."""
import datetime
import json
import sys
from typing import Union

import boto3


# NOTE: The following attributes, while not required from an AWS point of view, must appear
# in the output from the perspective of using the generated JSON as terraform input. If they
# are absent from a group definition, they will be added w/ the indicated 'empty' values.
# For precedence, this is 0.
#
# See https://github.com/nasa-pds/pds-tf-modules/terraform/modules/cognito/cognito_groups.tf
# for how the JSON can be consumed to create (empty) groups. The default set of groups follows
# this same format.
#
# While this could be considered a list of magic strings, exposing them as an external config
# introduces a bit too much and likely unnecessary flexibility.
#
# jdy: 20250522 - since we aren't using the generated group JSON w/ terraform, we don't need
#                 to establish default values for any fields.
mandatory_attrs: dict[str, Union[str, int]] = {}


page_size = 60  # max allowable
region = "us-west-2"  # default


def datetimeconverter(o):
    """Ensure that datetimes are handled as strings."""
    if isinstance(o, datetime.datetime):
        return str(o)


def usage():
    """Provide command line instructions."""
    print(f"Usage:\n\t{sys.argv[0]} <cognito_user_pool_id> {{--page-size=<page_size>}} {{--region=<aws_region>}}")


# Process the groups for the indicated cognito user pool

if len(sys.argv) > 4 or len(sys.argv) < 2:
    usage()
    sys.exit(1)

# Replace with your Cognito User Pool ID
user_pool_id = sys.argv[1]

for arg in sys.argv[2:]:
    if arg.startswith("--page-size"):
        page_size = int(arg.split("=")[1])
    elif arg.startswith("--region"):
        region = arg.split("=")[1]
    else:
        usage()
        sys.exit(1)

client = boto3.client("cognito-idp", region)

# Get a list of group names
groups = []
has_next_page = True
next_page_token = None
while has_next_page:
    try:
        response = (
            client.list_groups(UserPoolId=user_pool_id, Limit=page_size, NextToken=next_page_token)
            if next_page_token
            else client.list_groups(UserPoolId=user_pool_id, Limit=page_size)
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

    # Add in the mandatory attributes if not present
    for attr, def_value in mandatory_attrs.items():
        if group.get(attr) is None:
            group[attr] = def_value

    group_name = group["GroupName"]
    next_page_token = None
    has_next_page = True
    while has_next_page:
        try:
            response = (
                client.list_users_in_group(
                    UserPoolId=user_pool_id, GroupName=group_name, Limit=page_size, NextToken=next_page_token
                )
                if next_page_token
                else client.list_users_in_group(UserPoolId=user_pool_id, GroupName=group_name, Limit=page_size)
            )
            group["Users"].extend(response["Users"])
            next_page_token = response.get("NextToken")
            has_next_page = bool(next_page_token)
        except client.exceptions.ClientError as e:
            print(f"Error listing users for {user_pool_id}/{group_name}: {e}")

user_pool = {"UserPoolId": f"{user_pool_id}", "Groups": groups}
print(json.dumps(user_pool, indent=4, default=datetimeconverter))
