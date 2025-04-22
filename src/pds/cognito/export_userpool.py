"""Extract the list of users from the identified Cognito user pool."""

import datetime
import sys

import boto3


def get_cognito_csv_header(cognito_client, user_pool_id):
    """Retrieve the csv header of the user pool."""
    result = None
    csv_header_record = cognito_client.get_csv_header(UserPoolId=user_pool_id)
    if "CSVHeader" in csv_header_record:
        result = csv_header_record["CSVHeader"]

    return result


def get_cognito_users(cognito_client, user_pool_id, next_pagination_token, limit):
    """Get next page of users from the user pool based on the given pagination token."""
    return (
        cognito_client.list_users(UserPoolId=user_pool_id, Limit=limit, PaginationToken=next_pagination_token)
        if next_pagination_token
        else cognito_client.list_users(UserPoolId=user_pool_id, Limit=limit)
    )


def extract_values(header_list, user_record):
    """Extract attributes according to the given header list."""
    # put the Username and Attributes in a form that's easier to search across
    user_dict = {}
    user_dict["cognito:username"] = user_record["Username"]
    for attr in user_record["Attributes"]:
        user_dict[attr["Name"].lower()] = attr["Value"]

    result = []
    for header_name in header_list:
        if header_name in user_dict:
            result.append(user_dict[header_name])
        else:
            result.append("")

    return result


def datetimeconverter(o):
    """Ensure that datetimes are handled as strings."""
    if isinstance(o, datetime.datetime):
        return str(o)


# Process the cognito user pool

if len(sys.argv) <= 1 or len(sys.argv) != 4:
    print(f"Usage:\n\t{sys.argv[0]} <cognito_user_pool_id> <aws_region> <page_size>")
    print("\n\tpage_size has a ceiling set by AWS, which is currently 60")
    sys.exit(1)

user_pool_id = sys.argv[1]
region = sys.argv[2]
page_size = int(sys.argv[3])

cognito_client = boto3.client("cognito-idp", region)

csv_header = get_cognito_csv_header(cognito_client, user_pool_id)
print(",".join(csv_header))

pagination_token = ""
while pagination_token is not None:
    user_records = get_cognito_users(cognito_client, user_pool_id, pagination_token, page_size)

    pagination_token = user_records.get("PaginationToken")

    if "Users" in user_records:
        for user in user_records["Users"]:
            print(",".join(extract_values(csv_header, user)))
