from mock import patch

from scripts.conversion_script import run_convert2rhel


def test_run_convert2rhel():
    mock_env = {"PATH": "/fake/path", "RHC_WORKER_CONVERT2RHEL_DISABLE_TELEMETRY": "1"}

    with patch("os.environ", mock_env), patch(
        "scripts.conversion_script.run_subprocess", return_value=(b"", 0)
    ) as mock_popen:
        run_convert2rhel()

    mock_popen.assert_called_once_with(
        ["/usr/bin/convert2rhel", "-y"],
        env={"PATH": "/fake/path", "CONVERT2RHEL_DISABLE_TELEMETRY": "1"},
    )


def test_run_convert2rhel_payg():
    """
    Test that convert2rhel is called with payg option when "RHC_WORKER_CONVERT2RHEL_PAYG"
    env var is set to 'yes' - i.e. when that task receives this option.
    """
    mock_env = {"PATH": "/fake/path", "RHC_WORKER_CONVERT2RHEL_PAYG": "yes"}

    with patch("os.environ", mock_env), patch(
        "scripts.conversion_script.run_subprocess", return_value=(b"", 0)
    ) as mock_popen:
        run_convert2rhel()

    mock_popen.assert_called_once_with(
        ["/usr/bin/convert2rhel", "-y", "--payg"],
        env={"PATH": "/fake/path"},
    )


def test_run_convert2rhel_nopayg():
    """
    Test that convert2rhel is not call with payg option.
    Even when the env var is set to something else than 'yes'
    """
    mock_env = {"PATH": "/fake/path", "RHC_WORKER_CONVERT2RHEL_PAYG": "no"}

    with patch("os.environ", mock_env), patch(
        "scripts.conversion_script.run_subprocess", return_value=(b"", 0)
    ) as mock_popen:
        run_convert2rhel()

    mock_popen.assert_called_once_with(
        ["/usr/bin/convert2rhel", "-y"],
        env={"PATH": "/fake/path"},
    )
