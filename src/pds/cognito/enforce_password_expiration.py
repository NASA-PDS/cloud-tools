"""Determine which users in the identified user pool have expired passwords."""
import json
from datetime import datetime
from datetime import timedelta

import boto3
from common import generate_random_string  # type: ignore[import]
from common import send_mail  # type: ignore[import]


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
inactive_user_statuses = {"RESET_REQUIRED", "FORCE_CHANGE_PASSWORD", "EXTERNAL_PROVIDER"}

# List of User events which if result is passed, constitute a password change
password_change_events = {"PasswordChange", "ForgotPassword"}

# Temporary password length
temporary_password_length = 8


def datetime_serializable(obj):
    """Support JSON serializaion of datetimes."""
    if isinstance(obj, datetime):
        return obj.isoformat()
    return obj


def get_userpool_users(client, user_pool_id):
    """Obtain the ids of the users in the identified user pool."""
    has_next_page = True
    next_token = None

    users = []
    while has_next_page:
        response = (
            client.list_users(UserPoolId=user_pool_id)
            if next_token is None
            else client.list_users(UserPoolId=user_pool_id, PaginationToken=next_token)
        )
        users.extend(response["Users"])

        next_token = response.get("PaginationToken")
        if next_token is None:
            has_next_page = False

    return users


def validate_user_creation(user_record, valid_datetime, warn_datetime):
    """Validate user's password validity based on the user's creation date."""
    password_change_required = True
    issue_warning = False
    last_event_date = None

    user_create_date = user_record["UserCreateDate"].replace(tzinfo=None)
    if user_create_date > valid_datetime:
        print("User creation date preceeds validity datetime, password change required.")
        password_change_required = False
    elif warn_datetime is not None and warn_datetime < user_create_date:
        print("User creation date preceeds warning datetime, warning required.")
        issue_warning = True
        last_event_date = user_create_date

    return password_change_required, issue_warning, last_event_date


def validate_user_password(client, user_pool_id, user_record, valid_datetime, warn_datetime):
    """Validate user's password state.

    Based on the authentication events history of the identified user/user-pool prior to valid_datetime, determine if a
    password change occurred or if a warning message is merited (according to warn_datetime). If the user is not in an
    active state, it is ignored.
    """
    print(f"{json.dumps(user_record, indent=4, default=datetime_serializable)}")

    password_change_required = False
    issue_warning = False
    last_event_date = None
    username = user_record["Username"]

    if user_record["UserStatus"] in inactive_user_statuses:
        print(f"User {username} is not active, skipping.")
    else:
        password_change_required = True

        continue_on = True
        next_token = None
        while continue_on:
            response = (
                client.admin_list_user_auth_events(UserPoolId=user_pool_id, Username=username)
                if next_token is None
                else client.admin_list_user_auth_events(
                    UserPoolId=user_pool_id, Username=username, NextToken=next_token
                )
            )

            for user_event in response["AuthEvents"]:
                event_datetime = user_event["CreationDate"].replace(tzinfo=None)
                if event_datetime < valid_datetime:
                    # event is before the validity window, done
                    print("Change password not identified within validity period, password change required.")
                    continue_on = False
                    break
                elif user_event["EventType"] in password_change_events and user_event["EventResponse"] == "Pass":
                    print("An event constituting a password change has been found within validity period.")
                    continue_on = False
                    password_change_required = False
                    if warn_datetime is not None and event_datetime < warn_datetime:
                        print("Password change is within warning period, warning required.")
                        issue_warning = True
                        last_event_date = event_datetime
                    break

            next_token = response.get("NextToken")
            if continue_on and next_token is None:
                print("User has no auth event history before validity window - checking user creation.")
                password_change_required, issue_warning, last_event_date = validate_user_creation(
                    user_record, valid_datetime, warn_datetime
                )
                continue_on = False

    return password_change_required, issue_warning, last_event_date


def extract_user_email(user):
    """Extract email address from the user record.

    A None value can be returned.
    """
    result = None
    for attr in user["Attributes"]:
        if attr["Name"] == "email":
            result = attr["Value"]
    return result


def password_expiration_check(
    user_pool_id,
    cognito_login_url,
    valid_period,
    warn_window,
    smtp_endpoint,
    sender,
    expired_message_template,
    expired_subject_template,
    warning_message_template,
    warning_subject_template,
    apply_changes=True,
    develop_mode=False,
):
    """Run through the given pool to handle expired passwords and issue warnings as necessary."""
    current_date = datetime.now()
    # datetime at which passwords expire from now
    valid_time_diff = timedelta(days=valid_period)
    if develop_mode:
        valid_time_diff = timedelta(minutes=valid_period)
    valid_datetime = current_date - valid_time_diff

    # datetime at which imminent password expiration warnings are issued
    warn_time_diff = timedelta(days=warn_window)
    if develop_mode:
        warn_time_diff = timedelta(minutes=warn_window)
    warn_datetime = valid_datetime + warn_time_diff

    print(f"Password expiration datetime {valid_datetime}")
    print(f"Password warning datetime {warn_datetime}")

    client = boto3.client("cognito-idp")

    user_pool_info = client.describe_user_pool(UserPoolId=user_pool_id)
    if develop_mode:
        print(user_pool_info)

    user_pool = user_pool_info.get("UserPool", {})
    user_pool_name = user_pool.get("Name", "Undefined")
    temp_password_validity_days = (
        user_pool.get("Policies", {}).get("PasswordPolicy", {}).get("TemporaryPasswordValidityDays")
    )

    message_data = {
        "user_pool_name": user_pool_name,
        "user_pool_id": user_pool_id,
        "cognito_login_url": cognito_login_url,
        "valid_date": valid_datetime.strftime("%Y/%m/%d"),
        "warn_date": warn_datetime.strftime("%Y/%m/%d"),
        "valid_period": valid_period,
        "warn_window": warn_window,
        "temp_password_validity_days": temp_password_validity_days,
    }

    users = get_userpool_users(client, user_pool_id)
    for user in users:
        username = user["Username"]
        user_email = extract_user_email(user)

        password_change_required, issue_warning, last_event_date = validate_user_password(
            client, user_pool_id, user, valid_datetime, warn_datetime
        )

        if develop_mode:
            print(f"User : {username}")

        if last_event_date is not None:
            # calculate actual expiration date based on last qualifying event
            actual_expire_date = last_event_date + timedelta(days=valid_period)
            message_data["expiration_date"] = actual_expire_date.strftime("%D")
        else:
            message_data["expiration_date"] = "N/A"
        message_data["username"] = username
        message_data["user_email"] = user_email
        if password_change_required:
            temp_password = generate_random_string(temporary_password_length)
            message_data["temp_password"] = temp_password
            expired_message = expired_message_template.format(**message_data)
            expired_subject = expired_subject_template.format(**message_data)
            if develop_mode:
                print(expired_message)
            if user_email is None:
                print(
                    f"WARNING: {username} does not have an assigned email address in user pool "
                    f"{user_pool_id}/{user_pool_name}. Account password has been reset but an email "
                    "message will not be sent."
                )
            elif smtp_endpoint is not None:
                send_mail(smtp_endpoint, sender, user_email, expired_subject, expired_message)
            if apply_changes:
                """
                Change the user password.
                admin_set_user_password is used because it is functionally cleaner. The alternative of
                admin_reset_user_password requires construction of a web-app that performs the
                confirm_user_password portion of that process.
                """
                client.admin_set_user_password(
                    UserPoolId=user_pool_id, Username=username, Password=temp_password, Permanent=False
                )
        elif issue_warning:
            # send out a message indicating that the user's password is about to expire
            warning_message = warning_message_template.format(**message_data)
            warning_subject = warning_subject_template.format(**message_data)
            if develop_mode:
                print(warning_message)
            if user_email is None:
                print(
                    f"WARNING: {username} does not have an assigned email address in user pool "
                    f"{user_pool_id}/{user_pool_name}. A password expiration imminent warning email "
                    "message will not be sent."
                )
            elif smtp_endpoint is not None:
                send_mail(smtp_endpoint, sender, user_email, warning_subject, warning_message)
