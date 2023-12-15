import pytest
from mock import patch, call

from scripts.conversion_script import (
    configure_host_metering,
    ProcessError,
)

install_call = [["yum", "install", "-y", "host-metering"], True]
enable_call = [["systemctl", "enable", "host-metering.service"], True]
start_call = [["systemctl", "start", "host-metering.service"], True]
failure = (b"failed", 1)
success = (b"success", 0)


@patch(
    "scripts.conversion_script.run_subprocess", return_value=success
)
def test_host_metering_no_runl(mock_run_subprocess):
    mock_env = {"RHC_WORKER_CONVERT2RHEL_PAYG": "no"}
    with patch("os.environ", mock_env):
        configure_host_metering()
    mock_run_subprocess.assert_not_called()

    mock_env = {}
    with patch("os.environ", mock_env):
        configure_host_metering()
    mock_run_subprocess.assert_not_called()


@patch(
    "scripts.conversion_script.run_subprocess", side_effect=[failure, success, success]
)
def test_host_metering_error_install(mock_run_subprocess):
    mock_env = {"RHC_WORKER_CONVERT2RHEL_PAYG": "yes"}
    with patch("os.environ", mock_env), pytest.raises(
        ProcessError,
        match="Failed to install host-metering rpms: \nfailed\n",
    ):
        configure_host_metering()

    mock_run_subprocess.assert_any_call(*install_call)


@patch(
    "scripts.conversion_script.run_subprocess", side_effect=[success, failure, success]
)
def test_host_metering_error_enable(mock_run_subprocess):
    mock_env = {"RHC_WORKER_CONVERT2RHEL_PAYG": "yes"}
    with patch("os.environ", mock_env),pytest.raises(
        ProcessError,
        match="Failed to enable host-metering service: \nfailed\n",
    ):
        configure_host_metering()

    mock_run_subprocess.assert_any_call(*install_call)
    mock_run_subprocess.assert_any_call(*enable_call)


@patch(
    "scripts.conversion_script.run_subprocess", side_effect=[success, success, failure]
)
def test_host_metering_error_start(mock_run_subprocess):
    mock_env = {"RHC_WORKER_CONVERT2RHEL_PAYG": "yes"}
    with patch("os.environ", mock_env),pytest.raises(
        ProcessError,
        match="Failed to start host-metering service: \nfailed\n",
    ):
        configure_host_metering()

    mock_run_subprocess.assert_any_call(*install_call)
    mock_run_subprocess.assert_any_call(*enable_call)
    mock_run_subprocess.assert_any_call(*start_call)


@patch("scripts.conversion_script.run_subprocess", return_value=success)
def test_host_metering_success(mock_run_subprocess):
    mock_env = {"RHC_WORKER_CONVERT2RHEL_PAYG": "yes"}
    with patch("os.environ", mock_env):
        configure_host_metering()

    mock_run_subprocess.assert_any_call(*install_call)
    mock_run_subprocess.assert_any_call(*enable_call)
    mock_run_subprocess.assert_any_call(*start_call)
