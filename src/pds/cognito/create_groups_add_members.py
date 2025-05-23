"""Using a json file produced by export_groups.py, populate the groups w/ the indicated users."""
import json
import sys

import boto3


def create_group(client, user_pool_id, group):
    """Create a group given the indicated user pool id and group data."""
    role_arn = group.get("RoleArn")
    precedence = group.get("Precedence")

    args = {"GroupName": group["GroupName"], "UserPoolId": user_pool_id, "Description": group.get("Description")}

    if role_arn is not None and len(role_arn) > 0:
        args["RoleArn"] = role_arn
    if precedence is not None:
        args["Precedence"] = precedence

    client.create_group(**args)


# Populate the groups from the indicated json file, first checking that all groups exist.

test_only = False
if len(sys.argv) == 3:
    if sys.argv[1] == "-n":
        test_only = True
        del sys.argv[1:2]

if len(sys.argv) != 2:
    print(f"Usage:\n\t{sys.argv[0]} {{-n}} <export_groups_json_file>")
    print("\t-n only tests group existance, it does not create groups or add members.")
    sys.exit(1)

user_groups_json_file = sys.argv[1]
client = boto3.client("cognito-idp")

with open(user_groups_json_file, "r") as json_file:
    user_pool = json.load(json_file)

user_pool_id = user_pool["UserPoolId"]
user_groups = user_pool["Groups"]

# ensure all groups are present in the user pool, creating them if necessary unless we're in
# test only mode in which we just check groups
group_counter = 0
print(f"User Pool Id: {user_pool_id}")
print("Verifying groups...")
groups_to_create = []
for group in user_groups:
    group_exists = True
    try:
        print(f"\tGroup {group_counter} : {group['GroupName']}", end=" ")
        response = client.get_group(GroupName=group["GroupName"], UserPoolId=user_pool_id)

        print("Exists.")
    except client.exceptions.ResourceNotFoundException:
        print("Does not exist.")
        groups_to_create.append(group)

    group_counter += 1

if len(groups_to_create) > 0 and not test_only:
    print(f"\nCreating {len(groups_to_create)} missing groups.")
    for group in groups_to_create:
        # while the user pool id is duplicated in each group record, we'll use the one
        # retrieved from the root of the JSON - just for absolute consistency
        create_group(client, user_pool_id, group)
        print(f"\t{group['GroupName']}")
elif not test_only:
    print("All groups are present.")

# Now that the groups have all been verified, add the users for each unless we are in
# test only mode in which case just display memberships.
if not test_only:
    print("\nAdding users to groups.")
else:
    print("\nGroup memberships:")

for group in user_groups:
    print(f"\tGroup:{group['GroupName']}")
    group_users = group["Users"]
    for user in group_users:
        print(f"\t\tUser:{user['Username']}")
        if not test_only:
            # note that it is not an error to add user to a group in which he/she is already
            # a member, but there are no side-effects to doing this
            response = client.admin_add_user_to_group(
                UserPoolId=user_pool_id, Username=user["Username"], GroupName=group["GroupName"]
            )
    print("")
