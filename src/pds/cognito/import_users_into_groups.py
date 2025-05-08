"""Using a json file produced by export_groups.py, populate the groups w/ the indicated users."""
import json
import sys

import boto3


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

# check to make user all groups are present in the user pool
group_counter = 0
failure = False
print(f"User Pool Id: {user_pool_id}")
print("Verifying groups...")
for group in user_groups:
    try:
        print(f"Group {group_counter} : {group['GroupName']}", end=" ")
        response = client.get_group(GroupName=group["GroupName"], UserPoolId=user_pool_id)
    except client.exceptions.ResourceNotFoundException:
        print("does not exist")
        failure = True
    print("OK")
    group_counter += 1

if failure:
    print("Missing groups have been detected, please create them or edit the json file appropriately")
    sys.exit(1)

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
