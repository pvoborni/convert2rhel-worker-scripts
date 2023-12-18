import json
import os
import re
import shutil
import subprocess
import copy
from time import gmtime, strftime

from urllib2 import urlopen

STATUS_CODE = {
    "SUCCESS": 0,
    "INFO": 25,
    "WARNING": 51,
    "SKIP": 101,
    "OVERRIDABLE": 152,
    "ERROR": 202,
}
# Revert the `STATUS_CODE` dictionary to map number: name instead of name:
# number as used originally.
STATUS_CODE_NAME = {number: name for name, number in STATUS_CODE.items()}
# Log folder path for convert2rhel
C2R_LOG_FOLDER = "/var/log/convert2rhel"
# Log file for convert2rhel
C2R_LOG_FILE = "%s/convert2rhel.log" % C2R_LOG_FOLDER
# Path to the convert2rhel report json file.
C2R_REPORT_FILE = "%s/convert2rhel-pre-conversion.json" % C2R_LOG_FOLDER
# Path to the convert2rhel report textual file.
C2R_REPORT_TXT_FILE = "%s/convert2rhel-pre-conversion.txt" % C2R_LOG_FOLDER
# Path to the archive folder for convert2rhel.
C2R_ARCHIVE_DIR = "%s/archive" % C2R_LOG_FOLDER
# Set of yum transactions that will be rolled back after the operation is done.
YUM_TRANSACTIONS_TO_UNDO = set()

# Define regex to look for specific errors in the rollback phase in
# convert2rhel.
DETECT_ERROR_IN_ROLLBACK_PATTERN = re.compile(
    r".*(error|fail|denied|traceback|couldn't find a backup)",
    flags=re.MULTILINE | re.I,
)
# Detect the last transaction id in yum.
LATEST_YUM_TRANSACTION_PATTERN = re.compile(r"^(\s+)?(\d+)", re.MULTILINE)


class RequiredFile(object):
    """Holds data about files needed to download convert2rhel"""

    def __init__(self, path="", host="", keep=False):
        self.path = path
        self.host = host
        self.keep = keep


class ProcessError(Exception):
    """Custom exception to report errors during setup and run of conver2rhel"""

    def __init__(self, message, report):
        super(ProcessError, self).__init__(report)
        self.message = message
        self.report = report


class OutputCollector(object):
    """Wrapper class for script expected stdout"""

    # pylint: disable=too-many-instance-attributes
    # pylint: disable=too-many-arguments
    # Eight and five is reasonable in this case.

    def __init__(
        self, status="", message="", report="", entries=None, alert=False, error=False
    ):
        self.status = status
        self.alert = alert  # true if error true or if conversion inhibited
        self.error = error  # true if the script wasn't able to finish, otherwise false
        self.message = message
        self.report = report
        self.tasks_format_version = "1.0"
        self.tasks_format_id = "oamg-format"
        self.entries = entries
        self.report_json = None

    def to_dict(self):
        # If we have entries, then we change report_json to be a dictionary
        # with the needed values, otherwise, we leave it as `None` to be
        # transformed to `null` in json.
        if self.entries:
            self.report_json = {
                "tasks_format_version": self.tasks_format_version,
                "tasks_format_id": self.tasks_format_id,
                "entries": self.entries,
            }

        return {
            "status": self.status,
            "alert": self.alert,
            "error": self.error,
            "message": self.message,
            "report": self.report,
            "report_json": self.report_json,
        }


def check_for_inhibitors_in_rollback():
    """Returns lines with errors in rollback section of c2r log file, or empty string."""
    print("Checking content of '%s' for possible rollback problems ..." % C2R_LOG_FILE)
    matches = ""
    start_of_rollback_section = "WARNING - Abnormal exit! Performing rollback ..."
    try:
        with open(C2R_LOG_FILE, mode="r") as handler:
            lines = [line.strip() for line in handler.readlines()]
            # Find index of first string in the logs that we care about.
            start_index = lines.index(start_of_rollback_section)
            # Find index of last string in the logs that we care about.
            end_index = [
                i for i, s in enumerate(lines) if "Pre-conversion analysis report" in s
            ][0]

            actual_data = lines[start_index + 1 : end_index]
            matches = list(filter(DETECT_ERROR_IN_ROLLBACK_PATTERN.match, actual_data))
            matches = "\n".join(matches)
    except ValueError:
        print(
            "Failed to find rollback section ('%s') in '%s' file."
            % (start_of_rollback_section, C2R_LOG_FILE)
        )
    except IOError:
        print("Failed to read '%s' file.")

    return matches


def _check_ini_file_modified():
    rpm_va_output, ini_file_not_modified = run_subprocess(
        ["/usr/bin/rpm", "-Va", "convert2rhel"]
    )

    # No modifications at all
    if not ini_file_not_modified:
        return False

    lines = rpm_va_output.strip().split("\n")
    for line in lines:
        line = line.strip().split()
        status = line[0].replace(".", "").replace("?", "")
        path = line[-1]

        default_ini_modified = path == "/etc/convert2rhel.ini"
        md5_hash_mismatch = "5" in status

        if default_ini_modified and md5_hash_mismatch:
            return True
    return False


def check_convert2rhel_inhibitors_before_run():
    """
    Conditions that must be True in order to run convert2rhel command.
    """
    default_ini_path = "/etc/convert2rhel.ini"
    custom_ini_path = os.path.expanduser("~/.convert2rhel.ini")

    if os.path.exists(custom_ini_path):
        raise ProcessError(
            message="Custom %s was found." % custom_ini_path,
            report=(
                "Remove the %s file by running "
                "'rm -f %s' before running the Task again."
            )
            % (custom_ini_path, custom_ini_path),
        )

    if _check_ini_file_modified():
        raise ProcessError(
            message="According to 'rpm -Va' command %s was modified."
            % default_ini_path,
            report=(
                "Either remove the %s file by running "
                "'rm -f %s' or uninstall convert2rhel by running "
                "'yum remove convert2rhel' before running the Task again."
            )
            % (default_ini_path, default_ini_path),
        )


def get_system_distro_version():
    """Currently we execute the task only for RHEL 7 or 8"""
    print("Checking OS distribution and version ID ...")
    distribution_id = None
    version_id = None
    try:
        with open("/etc/system-release", "r") as system_release_file:
            data = system_release_file.readline()
            match = re.search(r"(.+?)\s?(?:release\s?)?\d", data)
            if match:
                # Split and get the first position, which will contain the system
                # name.
                distribution_id = match.group(1).lower()

            match = re.search(r".+?(\d+)\.(\d+)\D?", data)
            if match:
                version_id = "%s.%s" % (match.group(1), match.group(2))
    except IOError:
        print("Couldn't read /etc/system-release")

    print("Detected distribution='%s' in version='%s'" % (distribution_id, version_id))
    return distribution_id, version_id


def is_eligible_releases(release):
    eligible_releases = "7.9"
    return release == eligible_releases if release else False


def archive_analysis_report(file):
    """Archive previous json and textual report from convert2rhel"""
    stat = os.stat(file)
    # Get the last modified time in UTC
    last_modified_at = gmtime(stat.st_mtime)

    # Format time to a human-readable format
    formatted_time = strftime("%Y%m%dT%H%M%SZ", last_modified_at)

    # Create the directory if it don't exist
    if not os.path.exists(C2R_ARCHIVE_DIR):
        os.makedirs(C2R_ARCHIVE_DIR)

    file_name, suffix = tuple(os.path.basename(file).rsplit(".", 1))
    archive_log_file = "%s/%s-%s.%s" % (
        C2R_ARCHIVE_DIR,
        file_name,
        formatted_time,
        suffix,
    )
    shutil.move(file, archive_log_file)


def find_highest_report_level(actions):
    """
    Gather status codes from messages and result. We are not seeking for
    differences between them as we want all the results, no matter from where
    they come.
    """
    print("Collecting and combining report status.")
    action_level_combined = []
    for value in actions.values():
        action_level_combined.append(value["result"]["level"])
        for message in value["messages"]:
            action_level_combined.append(message["level"])

    valid_action_levels = [
        level for level in action_level_combined if level in STATUS_CODE
    ]
    valid_action_levels.sort(key=lambda status: STATUS_CODE[status], reverse=True)
    return valid_action_levels[0]


def gather_json_report():
    """Collect the json report generated by convert2rhel."""
    print("Collecting JSON report.")

    if not os.path.exists(C2R_REPORT_FILE):
        return {}

    try:
        with open(C2R_REPORT_FILE, "r") as handler:
            data = json.load(handler)

            if not data:
                return {}
    except ValueError:
        # In case it is not a valid JSON content.
        return {}

    return data


def gather_textual_report():
    """
    Collect the textual report generated by convert2rhel.

        .. note::
            We are checking if file exists here as the textual report is not
            that important as the JSON report for the script and for Insights.
            It's fine if the textual report does not exist, but the JSON one is
            required.
    """
    print("Collecting TXT report.")
    data = ""
    if os.path.exists(C2R_REPORT_TXT_FILE):
        with open(C2R_REPORT_TXT_FILE, mode="r") as handler:
            data = handler.read()
    return data


def generate_report_message(highest_status):
    """Generate a report message based on the status severity."""
    message = ""
    alert = False

    if STATUS_CODE[highest_status] <= STATUS_CODE["WARNING"]:
        message = (
            "No problems found. The system was converted successfully. Please,"
            " reboot your system at your earliest convenience to make sure that"
            " the system is using the RHEL Kernel."
        )

    if STATUS_CODE[highest_status] > STATUS_CODE["WARNING"]:
        message = "The conversion cannot proceed. You must resolve existing issues to perform the conversion."
        alert = True

    return message, alert


def setup_convert2rhel(required_files):
    """Setup convert2rhel tool by downloading the required files."""
    print("Downloading required files.")
    for required_file in required_files:
        _create_or_restore_backup_file(required_file)
        response = urlopen(required_file.host)
        data = response.read()

        directory = os.path.dirname(required_file.path)
        if not os.path.exists(directory):
            print("Creating directory at '%s'" % directory)
            os.makedirs(directory, mode=0o755)

        print("Writing file to destination: '%s'" % required_file.path)
        with open(required_file.path, mode="w") as handler:
            handler.write(data)
            os.chmod(required_file.path, 0o644)


# Code taken from
# https://github.com/oamg/convert2rhel/blob/v1.4.1/convert2rhel/utils.py#L345
# and modified to adapt the needs of the tools that are being executed in this
# script.
def run_subprocess(cmd, print_cmd=True, env=None):
    """
    Call the passed command and optionally log the called command
    (print_cmd=True) and environment variables in form of dictionary(env=None).
    Switching off printing the command can be useful in case it contains a
    password in plain text.

    The cmd is specified as a list starting with the command and followed by a
    list of arguments. Example: ["/usr/bin/yum", "install", "<package>"]
    """
    # This check is here because we passed in strings in the past and changed
    # to a list for security hardening.  Remove this once everyone is
    # comfortable with using a list instead.
    if isinstance(cmd, str):
        raise TypeError("cmd should be a list, not a str")

    if print_cmd:
        print("Calling command '%s'" % " ".join(cmd))

    process = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, bufsize=1, env=env
    )
    output = ""
    for line in iter(process.stdout.readline, b""):
        line = line.decode("utf8")
        output += line

    # Call wait() to wait for the process to terminate so that we can
    # get the return code.
    process.wait()

    return output, process.returncode


def _get_last_yum_transaction_id(pkg_name):
    output, return_code = run_subprocess(["/usr/bin/yum", "history", "list", pkg_name])
    if return_code:
        # NOTE: There is only print because list will exit with 1 when no such transaction exist
        print(
            "Listing yum transaction history for '%s' failed with exit status '%s' and output '%s'"
            % (pkg_name, return_code, output),
            "\nThis may cause clean up function to not remove '%s' after Task run."
            % pkg_name,
        )
        return None

    matches = LATEST_YUM_TRANSACTION_PATTERN.findall(output)
    return matches[-1][1] if matches else None


def _check_if_package_installed(pkg_name):
    _, return_code = run_subprocess(["/usr/bin/rpm", "-q", pkg_name])
    return return_code == 0


def install_convert2rhel():
    """
    Install the convert2rhel tool to the system.
    Returns True and transaction ID if the c2r pkg was installed, otherwise False, None.
    """
    print("Installing & updating Convert2RHEL package.")

    c2r_pkg_name = "convert2rhel"
    c2r_installed = _check_if_package_installed(c2r_pkg_name)

    if not c2r_installed:
        output, returncode = run_subprocess(
            ["/usr/bin/yum", "install", c2r_pkg_name, "-y"],
        )
        if returncode:
            raise ProcessError(
                message="Failed to install convert2rhel RPM.",
                report="Installing convert2rhel with yum exited with code '%s' and output:\n%s"
                % (returncode, output.rstrip("\n")),
            )
        transaction_id = _get_last_yum_transaction_id(c2r_pkg_name)
        return True, transaction_id

    output, returncode = run_subprocess(["/usr/bin/yum", "update", c2r_pkg_name, "-y"])
    if returncode:
        raise ProcessError(
            message="Failed to update convert2rhel RPM.",
            report="Updating convert2rhel with yum exited with code '%s' and output:\n%s"
            % (returncode, output.rstrip("\n")),
        )
    # NOTE: If we would like to undo update we could use _get_last_yum_transaction_id(c2r_pkg_name)
    return False, None


def run_convert2rhel():
    """
    Run the convert2rhel tool assigning the correct environment variables.
    """
    print("Running Convert2RHEL Conversion")
    env = {"PATH": os.environ["PATH"]}

    if "RHC_WORKER_CONVERT2RHEL_DISABLE_TELEMETRY" in os.environ:
        env["CONVERT2RHEL_DISABLE_TELEMETRY"] = os.environ[
            "RHC_WORKER_CONVERT2RHEL_DISABLE_TELEMETRY"
        ]

    return run_subprocess(["/usr/bin/convert2rhel", "-y"], env=env)


def configure_host_metering():
    """
    Install, enable and start host-metering on the system.

    When RHC_WORKER_CONVERT2RHEL_PAYG env var is set to "yes".

    Raises ProcessError if any of the steps fails.
    """

    error_msg = "Conversion succeeded but host-metering configuration failed."
    payg = os.environ.get("RHC_WORKER_CONVERT2RHEL_PAYG", "no")
    if payg != "yes":
        print("Skipping host-metering configuration.")
        return

    print("Installing host-metering rpms.")
    output, ret_install = run_subprocess(
        ["yum", "install", "-y", "host-metering"], True
    )
    print("Output of yum call: %s" % output)
    if ret_install:
        raise ProcessError(
            message=error_msg,
            report="Failed to install host-metering rpms: \n%s\n" % output,
        )

    print("Enabling host-metering service.")
    output, ret_enable = run_subprocess(
        ["systemctl", "enable", "host-metering.service"], True
    )
    print("Output of systemctl call: %s" % output)
    if ret_enable:
        raise ProcessError(
            message=error_msg,
            report="Failed to enable host-metering service: \n%s\n" % output,
        )

    print("Starting host-metering service.")
    output, ret_start = run_subprocess(
        ["systemctl", "start", "host-metering.service"], True
    )
    print("Output of systemctl call: %s" % output)
    if ret_start:
        raise ProcessError(
            message=error_msg,
            report="Failed to start host-metering service: \n%s\n" % output,
        )


def cleanup(required_files):
    """
    Cleanup the downloaded files downloaded in previous steps in this script.

    If any of the required files was already present on the system, the script
    will not remove that file, as it understand that it is a system file and
    not something that was downloaded by the script.
    """
    for required_file in required_files:
        if required_file.keep:
            continue
        if os.path.exists(required_file.path):
            print(
                "Removing the file '%s' as it was previously downloaded."
                % required_file.path
            )
            os.remove(required_file.path)
        _create_or_restore_backup_file(required_file)

    for transaction_id in YUM_TRANSACTIONS_TO_UNDO:
        output, returncode = run_subprocess(
            ["/usr/bin/yum", "history", "undo", "-y", transaction_id],
        )
        if returncode:
            print(
                "Undo of yum transaction with ID %s failed with exit status '%s' and output:\n%s"
                % (transaction_id, returncode, output)
            )


def _create_or_restore_backup_file(required_file):
    """
    Either creates or restores backup files (rename in both cases).
    """
    suffix = ".backup"
    if os.path.exists(required_file.path + suffix):
        print("Restoring backed up file %s." % (required_file.path))
        os.rename(required_file.path + suffix, required_file.path)
        return
    if os.path.exists(required_file.path):
        print(
            "File %s already present on system, backing up to %s."
            % (required_file.path, required_file.path + suffix)
        )
        os.rename(required_file.path, required_file.path + ".backup")


def _generate_message_key(message, action_id):
    """
    Helper method to generate a key field in the message composed by action_id
    and message_id.
    Returns modified copy of original message.
    """
    new_message = copy.deepcopy(message)

    new_message["key"] = "%s::%s" % (action_id, message["id"])
    del new_message["id"]

    return new_message


def _generate_detail_block(message):
    """
    Helper method to generate the detail key that is composed by the
    remediations and diagnosis fields.
    Returns modified copy of original message.
    """
    new_message = copy.deepcopy(message)
    detail_block = {
        "remediations": [],
        "diagnosis": [],
    }

    remediation_key = "remediations" if "remediations" in new_message else "remediation"
    detail_block["remediations"].append(
        {"context": new_message.pop(remediation_key, "")}
    )
    detail_block["diagnosis"].append({"context": new_message.pop("diagnosis", "")})
    new_message["detail"] = detail_block
    return new_message


def _rename_dictionary_key(message, new_key, old_key):
    """Helper method to rename keys in a flatten dictionary."""
    new_message = copy.deepcopy(message)
    new_message[new_key] = new_message.pop(old_key)
    return new_message


def _filter_message_level(message, level):
    """
    Filter for messages with specific level. If any of the message matches the
    level, return None, otherwise, if it is different from what is expected,
    return the message received to continue with the other transformations.
    """
    if message["level"] != level:
        return message

    return {}


def apply_message_transform(message, action_id):
    """Apply the necessary data transformation to the given messages."""
    if not _filter_message_level(message, level="SUCCESS"):
        return {}

    new_message = _generate_message_key(message, action_id)
    new_message = _rename_dictionary_key(new_message, "severity", "level")
    new_message = _rename_dictionary_key(new_message, "summary", "description")
    new_message = _generate_detail_block(new_message)

    # Appending the `modifiers` key to the message here for now. Once we have
    # this feature in the frontend, we can populate the data with it.
    new_message["modifiers"] = []

    return new_message


def transform_raw_data(raw_data):
    """
    Method that will transform the raw data given and output in the expected
    format.

    The expected format will be a flattened version of both results and
    messages into a single
    """
    new_data = []
    for action_id, result in raw_data["actions"].items():
        # Format the results as a single list
        for message in result["messages"]:
            new_data.append(apply_message_transform(message, action_id))

        new_data.append(apply_message_transform(result["result"], action_id))

    # Filter out None values before returning
    return [data for data in new_data if data]


def update_insights_inventory():
    """Call insights-client to update insights inventory."""
    print("Updating system status in Red Hat Insights.")
    output, returncode = run_subprocess(cmd=["/usr/bin/insights-client"])

    if returncode:
        raise ProcessError(
            message="Conversion succeeded but update of Insights Inventory by registering the system again failed.",
            report="insights-client execution exited with code '%s' and output:\n%s"
            % (returncode, output.rstrip("\n")),
        )

    print("System registered with insights-client successfully.")


# pylint: disable=too-many-branches
# pylint: disable=too-many-statements
# pylint: disable=too-many-locals
def main():
    """Main entrypoint for the script."""
    if os.path.exists(C2R_REPORT_FILE):
        archive_analysis_report(C2R_REPORT_FILE)

    if os.path.exists(C2R_REPORT_TXT_FILE):
        archive_analysis_report(C2R_REPORT_TXT_FILE)

    output = OutputCollector()
    gpg_key_file = RequiredFile(
        path="/etc/pki/rpm-gpg/RPM-GPG-KEY-redhat-release",
        host="https://www.redhat.com/security/data/fd431d51.txt",
    )
    c2r_repo = RequiredFile(
        path="/etc/yum.repos.d/convert2rhel.repo",
        host="https://ftp.redhat.com/redhat/convert2rhel/7/convert2rhel.repo",
    )
    required_files = [
        gpg_key_file,
        c2r_repo,
    ]

    convert2rhel_installed = False
    # Flag that indicate if the conversion was successful or not.
    conversion_successful = False
    # String to hold any errors that happened during rollback.
    rollback_errors = ""

    try:
        # Exit if not CentOS 7.9
        dist, version = get_system_distro_version()
        if not dist.startswith("centos") or not is_eligible_releases(version):
            raise ProcessError(
                message="Conversion is only supported on CentOS 7.9 distributions.",
                report='Exiting because distribution="%s" and version="%s"'
                % (dist.title(), version),
            )

        # Setup Convert2RHEL to be executed.
        setup_convert2rhel(required_files)
        check_convert2rhel_inhibitors_before_run()
        convert2rhel_installed, transaction_id = install_convert2rhel()
        if convert2rhel_installed:
            YUM_TRANSACTIONS_TO_UNDO.add(transaction_id)

        stdout, returncode = run_convert2rhel()
        conversion_successful = returncode == 0
        rollback_errors = check_for_inhibitors_in_rollback()

        # Returncode other than 0 can happen in two states in analysis mode:
        #  1. In case there is another instance of convert2rhel running
        #  2. In case of KeyboardInterrupt, SystemExit (misplaced by mistaked),
        #     Exception not catched before.
        # In any case, we should treat this as separate and give it higher
        # priority. In case the returncode was non zero, we don't care about
        # the rest and we should jump to the exception handling immediatly
        if not conversion_successful:
            # Check if there are any inhibitors in the rollback logging. This is
            # necessary in the case where the analysis was done successfully, but
            # there was an error in the rollback log.
            if rollback_errors:
                raise ProcessError(
                    message=(
                        "A rollback of changes performed by convert2rhel failed. The system is in an undefined state. "
                        "Recover the system from a backup or contact Red Hat support."
                    ),
                    report=(
                        "\nFor details, refer to the convert2rhel log file on the host at "
                        "/var/log/convert2rhel/convert2rhel.log. Relevant lines from log file: \n%s\n"
                    )
                    % rollback_errors,
                )

            raise ProcessError(
                message=(
                    "An error occurred during the pre-conversion analysis. For details, refer to "
                    "the convert2rhel log file on the host at /var/log/convert2rhel/convert2rhel.log"
                ),
                report=(
                    "convert2rhel exited with code %s.\n"
                    "Output of the failed command: %s"
                    % (returncode, stdout.rstrip("\n"))
                ),
            )

        if conversion_successful:
            configure_host_metering()

        # Only call insights to update inventory on successful conversion.
        update_insights_inventory()

        print("Conversion script finished successfully!")
    except ProcessError as exception:
        print(exception.report)
        output = OutputCollector(
            status="ERROR",
            alert=True,
            error=False,
            message=exception.message,
            report=exception.report,
        )
    except Exception as exception:
        print(str(exception))
        output = OutputCollector(
            status="ERROR",
            alert=True,
            error=False,
            message="An unexpected error occurred. Expand the row for more details.",
            report=str(exception),
        )
    finally:
        # Gather JSON & Textual report
        data = gather_json_report()
        if data and not rollback_errors:
            highest_level = find_highest_report_level(actions=data["actions"])
            # Set the first position of the list as being the final status,
            # that's needed because `find_highest_report_level` will sort out
            # the list with the highest priority first.
            output.status = highest_level

            # At this point we know JSON report exists and no rollback errors occured
            # we can rewrite possible previous message with more specific one and set alert
            output.message, output.alert = generate_report_message(highest_level)

            # Alert not present for successfull conversion
            if not output.alert:
                gpg_key_file.keep = True

                # NOTE: When c2r statistics on insights are not reliant on rpm being installed
                # remove below line (=decide only based on install_convert2rhel() result)
                if convert2rhel_installed:
                    YUM_TRANSACTIONS_TO_UNDO.remove(transaction_id)
                # NOTE: Keep always because added/updated pkg is also kept
                # (if repo existed, the .backup file will remain on system)
                c2r_repo.keep = True

            if not output.report and not conversion_successful:
                # Try to attach the textual report in the report if we have
                # json report, otherwise, we would overwrite the report raised
                # by the exception.
                output.report = gather_textual_report()

            # Only add entries (report_json = Insights colorful report)
            # if the returncode is not 0 and here are no rollback errors.
            if not conversion_successful and not rollback_errors:
                output.entries = transform_raw_data(data)

        print("Cleaning up modifications to the system.")
        cleanup(required_files)

        print("### JSON START ###")
        print(json.dumps(output.to_dict(), indent=4))
        print("### JSON END ###")


if __name__ == "__main__":
    main()
