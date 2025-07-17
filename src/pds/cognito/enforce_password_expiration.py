"""Determine which users in the identified user pool have expired passwords."""
import sys
from datetime import datetime, timedelta
import json

import boto3


"""
This script accepts a user pool id, a period of days in which passwords are considered valid, and a period
prior to expiration at which warnings of imminent expirations are issued.

Key Assumptions:
  - User auth event history is enabled for the pool.
  - The pool has self-service account recovery configured.

Given that Cognito user pools support active (re)configuration, there are circumstances in which a user may
not have a history of authentication events. In these (hopefully rare) cases, the password validity will be
measured based on the user's creation date.

By default, no status changes are applied to user records and no email notifications are issued. To have these
changes applied, '--apply' must be specified as the last argument on the command line. Additionally, another
optional argument to this script of --dev which considers the units of validity and warning periods from days 
to minutes (to more easily simulate account event states).
"""


# List of states which constitute an inactive user - Note that ARCHIVED is no longer used
inactive_user_statuses = { 'RESET_REQUIRED', 'FORCE_CHANGE_PASSWORD', 'EXTERNAL_PROVIDER' }


def datetime_serializable(obj):
    """Support JSON serializaion of datetimes"""
    if isinstance(obj,datetime):
        return obj.isoformat()
    return obj


def get_userpool_users(client, user_pool_id):
    """Obtain the ids of the users in the identified user pool."""
    has_next_page = True
    next_token = None

    users = []
    while has_next_page: 
        response = (
            client.list_users(UserPoolId=user_pool_id, AttributesToGet=[])
            if next_token is None
            else client.list_users(UserPoolId=user_pool_id, AttributesToGet=[], PaginationToken=next_token)
        )
        users.extend(response['Users'])

        next_token = response.get('PaginationToken')
        if next_token is None:
            has_next_page = False

    return users


def validate_user_creation(user_record, valid_datetime, warn_datetime):
    """Validate user's password validity based on the user's creation date."""
    continue_on = False
    password_change_required = True
    issue_warning = False

    user_create_date = user_record['UserCreateDate'].replace(tzinfo=None)
    if user_create_date > valid_datetime:
        print("User creation date preceeds validity datetime, password change required.")
        password_change_required = False
    elif warn_datetime is not None and warn_datetime < user_create_date:
        print("User creation date preceeds warning datetime, warning required.")
        issue_warning = True

    return password_change_required, issue_warning


def validate_user_password(client, user_pool_id, user_record, valid_datetime, warn_datetime):
    """Based on the authentication events history of the identified user/user-pool prior to valid_datetime, determine if a 
       password change occurred or if a warning message is merited (according to warn_datetime). If the user is not in an 
       active state, it is ignored."""

    print(f"{json.dumps(user_record, indent=4, default=datetime_serializable)}")

    password_change_required = False
    issue_warning = False

    if user_record['UserStatus'] in inactive_user_statuses:
        print("User is not active, skipping.")
    else:
        password_change_required = True
        issue_warning = False

        continue_on = True
        next_token = None
        while continue_on:
            response = (
                client.admin_list_user_auth_events(UserPoolId=user_pool_id, Username=user_name) 
                if next_token is None
                else client.admin_list_user_auth_events(UserPoolId=user_pool_id, Username=user_name, NextToken=next_token)   
            )
    
            authEvents = response.get('AuthEvents')
            for user_event in response['AuthEvents']:
                event_datetime = user_event['CreationDate'].replace(tzinfo=None)
                if event_datetime < valid_datetime:
                    # event is before the validity window, done
                    print("Change password not identified within validity period, password change required.")
                    continue_on = False
                    break
                elif user_event['EventType'] == 'PasswordChange' and user_event['EventResponse'] == 'Pass':
                    print("Password change has been found within validity period.")
                    continue_on = False
                    password_change_required = False
                    if warn_datetime is not None and event_datetime < warn_datetime:
                        print("Password change is within warning period, warning required.")
                        issue_warning = True
                    break
            
            next_token = response.get('NextToken')
            if continue_on and next_token is None:
                print("User has no auth event history before validity window - checking user creation.")
                password_change_required, issue_warning = validate_user_creation(user_record, valid_datetime, warn_datetime)
                continue_on = False

    return password_change_required, issue_warning


user_pool_id = sys.argv[1]
valid_days = int(sys.argv[2])
warn_window_days = int(sys.argv[3])

apply_changes = False
develop = False
for i in range(4, len(sys.argv)):
    if sys.argv[i] == '--dev':
        # dev purposes, shifts units for validity and warning to minutes
        develop = True
    elif sys.argv[i] == '--apply':
        # make changes
        apply_changes = True

# datetime at which passwords expire from now
valid_time_diff = timedelta(days=valid_days)
if develop: valid_time_diff = timedelta(minutes=valid_days)
valid_datetime = datetime.now() - valid_time_diff
 
# datetime at which imminent password expiration warnings are issued
warn_time_diff = timedelta(days=warn_window_days)
if develop: warn_time_diff = timedelta(minutes=warn_window_days)
warn_datetime = valid_datetime + warn_time_diff

print(f"Password expiration datetime {valid_datetime}")
print(f"Password warning datetime {warn_datetime}")

client = boto3.client("cognito-idp")

users = get_userpool_users(client, user_pool_id)
for user in users:
    user_name = user['Username']
    password_change_required, issue_warning = validate_user_password(client, user_pool_id, user, valid_datetime, warn_datetime)

    if password_change_required:
        print(f"Hey {user_name}, resetting your password. I SAY, YOUR PASSWORD IS WILL BE RESET.")
        if apply_changes:
            # force a password change, this will automatically send out a notification message
            client.admin_reset_user_password(user_pool_id, user_name)
    elif issue_warning:
        # send out a message indicating that the user's password is about to expire
        print(f"Hey {user_name}, your password is about to expire. I SAY, IT'S ABOUT TO EXPIRE.")
