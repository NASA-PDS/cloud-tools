"""Lambda interface to initiate password expiration checking."""

import json
import sys

from common import close_smtp
from common import get_ssm_parameters_by_path
from common import open_smtp
from enforce_password_expiration import password_expiration_check


def lambda_handler(event, context):
    """Lambda handler function."""
    # Pull config values from SSM parameter store. This utilizes the ssm model from Scott Collins' excellent work on the data upload manager
    config_ssm_path = event["config_ssm_path"]

    expected_fields = (
        "user_pool_id",
        "cognito_login_url",
        "valid_period",
        "warn_window",
        "smtp_username",
        "smtp_password",
        "smtp_server",
        "smtp_sender",
        "expired_message_template",
        "expired_subject_template",
        "warning_message_template",
        "warning_subject_template",
    )
    # optional_fields are ("apply_changes", "develop_mode")

    ssm_parameters = get_ssm_parameters_by_path(config_ssm_path)

    config_params = {}
    # Strip off parameter prefixes and map to values
    for ssm_parameter_name, ssm_parameter_value in ssm_parameters.items():
        config_params[ssm_parameter_name.split("/")[-1]] = ssm_parameter_value

    if not all(field in config_params for field in expected_fields):
        raise RuntimeError(
            f"Unexpected SMTP configuration from SSM, expected {expected_fields}, got {list(config_params.keys())}"
        )

    user_pool_id = config_params["user_pool_id"]
    cognito_login_url = config_params["cognito_login_url"]
    valid_period = int(config_params["valid_period"])
    warn_window = int(config_params["warn_window"])
    smtp_username = config_params["smtp_username"]
    smtp_password = config_params["smtp_password"]
    smtp_server = config_params["smtp_server"]
    sender = config_params["smtp_sender"]
    expired_message_template = config_params["expired_message_template"]
    warning_message_template = config_params["warning_message_template"]
    expired_subject_template = config_params["expired_subject_template"]
    warning_subject_template = config_params["warning_subject_template"]
    apply_changes = eval(config_params.get("apply_changes", "True"))
    develop_mode = eval(config_params.get("develop_mode", "False"))

    print(f"apply_changes : {apply_changes}")
    print(f"develop_mode : {develop_mode}")
    if develop_mode:
        print(json.dumps(config_params, indent=4))

    smtp_host, smtp_port = smtp_server.split(":")
    smtp_endpoint = None
    if apply_changes:
        smtp_endpoint = open_smtp(smtp_username, smtp_password, smtp_host, int(smtp_port))

    try:
        password_expiration_check(
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
            apply_changes,
            develop_mode,
        )
    finally:
        if smtp_endpoint is not None:
            close_smtp(smtp_endpoint)


"""For testing"""
if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(f"Usage:\n\t{sys.argv[0]} <ssm_path>")
        sys.exit(0)

    event = {"config_ssm_path": sys.argv[1]}

    lambda_handler(event, None)
