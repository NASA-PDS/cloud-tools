"""Common functions and values."""
import datetime
import sys


def datetimeconverter(o):
    """Ensure that datetimes are handled as strings."""
    if isinstance(o, datetime.datetime):
        return str(o)


def get_args(arg_sub_list, exitStatus=1):
    """Extract common AWS options from given list."""
    page_size = 60  # max allowable
    region = "us-west-2"
    for arg in arg_sub_list:
        if arg.startswith("--page-size"):
            page_size = int(arg.split("=")[1])
        elif arg.startswith("--region"):
            region = arg.split("=")[1]
        else:
            cognito_tool_usage(exitStatus)

    return page_size, region


def cognito_tool_usage(exitStatus=None):
    """Provide command line instructions."""
    print(f"Usage:\n\t{sys.argv[0]} <cognito_user_pool_id> {{--page-size=<page_size>}} {{--region=<aws_region>}}")
    if exitStatus is not None:
        sys.exit(exitStatus)
