"""Common functions."""
import datetime
import random
import smtplib
import string
import sys
from email.mime.text import MIMEText

import boto3


def datetimeconverter(o):
    """Ensure that datetimes are handled as strings."""
    if isinstance(o, datetime.datetime):
        return str(o)


def get_args(arg_sub_list, exit_status=1):
    """Extract common AWS options from given list."""
    page_size = 60  # max allowable
    region = "us-west-2"
    for arg in arg_sub_list:
        if arg.startswith("--page-size"):
            page_size = int(arg.split("=")[1])
        elif arg.startswith("--region"):
            region = arg.split("=")[1]
        else:
            cognito_tool_usage(exit_status)

    return page_size, region


random_punct_set = "-.,*!?"
random_character_set = string.ascii_letters + string.digits + random_punct_set


def generate_random_string(length=8):
    """Generate a random (password) string.

    The password will be composed of alphanumeric + limited punctuation. Note that the minimum length is 4.
    """
    # Ensure at least one each upper and lower characters, one digit and one punctuation mark
    random_string_list = [
        random.choice(string.ascii_lowercase),
        random.choice(string.ascii_uppercase),
        random.choice(string.digits),
        random.choice(random_punct_set),
    ]
    if length > 4:
        random_string_list = random_string_list + random.choices(random_character_set, k=length - 3)
    return "".join(random_string_list)


def cognito_tool_usage(exit_status=None):
    """Provide command line instructions."""
    print(f"Usage:\n\t{sys.argv[0]} <cognito_user_pool_id> {{--page-size=<page_size>}} {{--region=<aws_region>}}")
    if exit_status is not None:
        sys.exit(exit_status)


def get_ssm_parameters_by_path(ssm_path):
    """Return the set of parameters provided by the indicated path. Only the leaf of the names are returned."""
    result_params = {}
    has_next_page = True
    next_token = None

    ssm_client = boto3.client("ssm")

    while has_next_page:
        response = (
            ssm_client.get_parameters_by_path(Path=ssm_path, Recursive=True)
            if next_token is None
            else ssm_client.get_parameters_by_path(Path=ssm_path, Recursive=True, NextToken=next_token)
        )

        # Strip off parameter prefixes and map to values
        for ssm_parameter in response["Parameters"]:
            result_params[ssm_parameter["Name"].split("/")[-1]] = ssm_parameter["Value"]

        next_token = response.get("NextToken")
        has_next_page = next_token is not None

    return result_params


def open_smtp(smtp_user, smtp_password, smtp_endpoint_host, smtp_endpoint_port):
    """Open a TLS-mode connection to the indicated smtp host."""
    smtp_endpoint = smtplib.SMTP(smtp_endpoint_host, smtp_endpoint_port)
    smtp_endpoint.starttls()
    smtp_endpoint.login(smtp_user, smtp_password)

    return smtp_endpoint


def send_mail(smtp_endpoint, sender, to_email, message_subject, message_body):
    """Send email via the specified AWS SMTP endpoint."""
    message = MIMEText(message_body)
    message["Subject"] = message_subject
    message["From"] = sender
    message["To"] = to_email

    smtp_endpoint.sendmail(sender, to_email, message.as_string())


def close_smtp(smtp_endpoint):
    """End the given endpoint connection. Very straight forward but provided to adhere to the smtplib abstraction."""
    smtp_endpoint.quit()
