"""Using a json file produced by export_groups.py, populate the groups w/ the indicated users."""
import json
import sys

import boto3


def createGroup(client, user_pool_id, group):
    """Create a group given the indicated user pool id and group data"""
    role_arn = group.get("RoleArn")
    precedence = 0 if group.get("Precedence") is None else group.get("Precedence")

    if role_arn is None or len(role_arn) == 0:
        client.create_group(GroupName=group["GroupName"],
                            UserPoolId=user_pool_id,
                            Description=group.get("Description"),
                            Precedence=precedence)
    else:
        client.create_group(GroupName=group["GroupName"],
                            UserPoolId=user_pool_id,
                            Description=group.get("Description"),
                            RoleArn=group.get("RoleArn"),
                            Precedence=precedence)


# Populate the groups from the indicated json file, first checking that all groups exist.

if len(sys.argv) != 2:
    print(f"Usage:\n\t{sys.argv[0]} <export_groups_json_file>")
    sys.exit(1)

user_groups_json_file = sys.argv[1]
client = boto3.client("cognito-idp")

with open(user_groups_json_file, "r") as json_file:
    user_pool = json.load(json_file)

user_pool_id = user_pool["UserPoolId"]
user_groups = user_pool["Groups"]

# ensure all groups are present in the user pool, creating them if necessary
group_counter = 0
print(f"User Pool Id: {user_pool_id}")
print("Verifying groups...")
groups_to_create = []
for group in user_groups:
    group_exists = True
    try:
        print(f"Group {group_counter} : {group['GroupName']}", end=" ")
        response = client.get_group(GroupName=group["GroupName"], UserPoolId=user_pool_id)

        print("Exists.")
    except client.exceptions.ResourceNotFoundException:
        print("Does not exist.")
        groups_to_create.append(group)

    group_counter += 1

if len(groups_to_create) > 0:
    print(f"Creating {len(groups_to_create)} missing groups.")
    for group in groups_to_create:
        # while the user pool id is duplicated in each group record, we'll use the one
        # retrieved from the root of the JSON - just for absolute consistency
        createGroup(client, user_pool_id, group)
        print(f"\t{group['GroupName']}")
else:
    print("All groups are present.")

# Now that the groups have all been verified, add the users for each
print("Adding users to groups.")
for group in user_groups:
    print(f"Group:{group['GroupName']}")
    group_users = group["Users"]
    for user in group_users:
        print(f"User:{user['Username']}")
        response = client.admin_add_user_to_group(
            UserPoolId=user_pool_id, Username=user["Username"], GroupName=group["GroupName"]
        )
