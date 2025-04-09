import sys

import boto3
import datetime
import json

page_size = 60
region = 'us-west-2'


# function to get user pool csv header
def get_cognito_csv_header(cognito_client, user_pool_id):

    result = None
    csv_header_record = cognito_client.get_csv_header(UserPoolId = user_pool_id)
    if 'CSVHeader' in csv_header_record:
        result = csv_header_record['CSVHeader']

    return result


# function to get next page of users from the user pool
def get_cognito_users(cognito_client, user_pool_id, next_pagination_token ='', limit = page_size):  

    return cognito_client.list_users(
        UserPoolId = user_pool_id,
        Limit = limit,
        PaginationToken = next_pagination_token
    ) if next_pagination_token else cognito_client.list_users(
        UserPoolId = user_pool_id,
        Limit = limit
    )
  

# extract attributes according to the given header list
def extract_values(header_list, user_record):
    # put the Username and Attributes in a form that's easier to search across
    user_dict = {}
    user_dict['cognito:username'] = user_record['Username']
    for attr in user_record['Attributes']:
        user_dict[attr['Name'].lower()] = attr['Value']

    result = []
    for header_name in header_list:
        if header_name in user_dict:
            result.append(user_dict[header_name])
        else: 
            result.append("")

    return result


# ensures that datetimes are handled as strings
def datetimeconverter(o):
    if isinstance(o, datetime.datetime):
        return str(o)

  
#------------------------------
# Process the cognito user pool
#------------------------------

if len(sys.argv) <= 1:
    print("Need a cognito user pool id as an argument.")
    print(f"\t{sys.argv[0]} <cognito_user_pool_id> <aws_region>=us=west=2")
    sys.exit(1)

user_pool_id = sys.argv[1]

if len(sys.argv) >2:
    region = sys.argv[2]

#undocumented, supported for dev/debugging
if len(sys.argv) > 3:
    page_size = int(sys.argv[3])

cognito_client = boto3.client('cognito-idp', region)
pagination_token = ""

csv_header = get_cognito_csv_header(cognito_client, user_pool_id)
print(','.join(csv_header))

while(True):
    user_records = get_cognito_users(
        cognito_client, 
        user_pool_id, 
        pagination_token,
        page_size
    )

    pagination_token = None
    if 'PaginationToken' in user_records:
        pagination_token = user_records['PaginationToken']
        print(f"{pagination_token}")

    if 'Users' in user_records:
        for user in user_records['Users']:
            print(",".join(extract_values(csv_header, user)))

    if pagination_token is None:
        break
