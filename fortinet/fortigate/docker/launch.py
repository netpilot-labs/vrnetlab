#!/usr/bin/env python3
import datetime
import logging
import os
import re
import shutil
import signal
import sys
import telnetlib
import time
import uuid

import vrnetlab


STARTUP_CONFIG_FILE = "/config/startup-config.cfg"


def handle_SIGCHLD(_unused_signal, _unused_frame):
    os.waitpid(-1, os.WNOHANG)


def handle_SIGTERM(_unused_signal, _unused_frame):
    sys.exit(0)


signal.signal(signal.SIGINT, handle_SIGTERM)
signal.signal(signal.SIGTERM, handle_SIGTERM)
signal.signal(signal.SIGCHLD, handle_SIGCHLD)

TRACE_LEVEL_NUM = 9
logging.addLevelName(TRACE_LEVEL_NUM, "TRACE")


def trace(self, message, *args, **kws):
    # Yes, logger takes its '*args' as 'args'.
    if self.isEnabledFor(TRACE_LEVEL_NUM):
        self.log(TRACE_LEVEL_NUM, message, *args, **kws)


logging.Logger.trace = trace


class FortiOS_vm(vrnetlab.VM):
    def __init__(self, hostname, username, password, conn_mode):
        for e in os.listdir("."):
            if re.search(".qcow2$", e):
                disk_image = "./" + e
        # call parents __init__ function here
        super(FortiOS_vm, self).__init__(
            username,
            password,
            disk_image=disk_image,
            ram=2048,
            driveif="virtio",
            # fortios fails to respond to network requests if the pci bus is setup :D
            provision_pci_bus=False,
        )
        self.conn_mode = conn_mode
        self.hostname = hostname
        self.num_nics = 12
        self.nic_type = "virtio-net-pci"
        self.highest_port = 0
        self.qemu_args.extend(["-uuid", os.getenv("FORTIGATE_UUID") or str(uuid.uuid4())])
        self.spins = 0
        self.running = None

        # set up the extra empty disk image
        # for fortigate logs
        vrnetlab.run_command(
            ["qemu-img", "create", "-f", "qcow2", "empty.qcow2", "30G"]
        )

        self.qemu_args.extend(
            [
                "-drive",
                "if=virtio,format=qcow2,file=empty.qcow2,index=1",
            ]
        )


    def bootstrap_spin(self):
        """This function should be called periodically to do work.

        returns False when it has failed and given up, otherwise True
        """
        if self.spins > 300:
            # too many spins with no result -> restart
            self.logger.warning("no output from serial console, restarting VCP")
            self.stop()
            self.start()
            self.spins = 0
            return

        # FortiOS 7.6.5+ requires password change on first boot
        # IMPORTANT: Order matters! More specific patterns must come BEFORE shorter ones
        # because "Password:" is a substring of "New Password:" and "Confirm Password:"
        # Prompt patterns:
        #   0: login: - initial login prompt
        #   1: FortiGate-VM64-KVM # - CLI prompt (bootstrap complete)
        #   2: New Password: - first-boot password change prompt (must be before Password:)
        #   3: Confirm Password: - password confirmation prompt (must be before Password:)
        #   4: Password: - password prompt during login (last to avoid substring match)
        (ridx, match, res) = self.tn.expect(
            [
                b"login:",
                b"FortiGate-VM64-KVM #",
                b"New Password:",
                b"Confirm Password:",
                b"Password:",
            ],
            1,
        )
        if match:  # got a match!
            if ridx == 0:  # matched login prompt
                self.logger.info("matched login prompt, sending username")
                self.wait_write(self.username, wait=None)

            elif ridx == 1:  # matched CLI prompt - bootstrap complete
                self.logger.info("matched CLI prompt, configuring hostname")
                self.wait_write("config system global", wait=None)
                hostname_command = "set hostname " + self.hostname
                self.wait_write(hostname_command, wait="global")
                self.wait_write("end", wait=hostname_command)
                # Apply startup config if it exists
                self.startup_config()
                self.running = True
                self.tn.close()
                startup_time = datetime.datetime.now() - self.start_time
                self.logger.info(f"Startup complete in {startup_time}")
                return

            elif ridx == 2:  # New Password prompt - FortiOS 7.6.5+ first boot
                self.logger.info(f"matched New Password prompt, setting password")
                self.wait_write(self.password, wait=None)

            elif ridx == 3:  # Confirm Password prompt
                self.logger.info("matched Confirm Password prompt")
                self.wait_write(self.password, wait=None)

            elif ridx == 4:  # Password prompt - send empty for first boot
                self.logger.info("matched Password prompt")
                # For first boot, admin has no password - send empty
                self.wait_write("", wait=None)

        else:
            # no match, if we saw some output from the router it's probably
            # booting, so let's give it some more time
            if res != b"":
                self.logger.trace(f"OUTPUT FORTIGATE: {res.decode()}")
                # reset spins if we saw some output
                self.spins = 0

        self.spins += 1

    def startup_config(self):
        """Load user config from /config/startup-config.cfg.

        ContainerLab mounts the startup config at /config/startup-config.cfg.
        Two shapes are supported:

        - A FULL FortiOS backup (first line ``#config-version=...``) is
          restored atomically via ``execute restore config tftp`` — FortiOS
          downloads it, validates it, applies the whole file and reboots.
          Blind serial replay silently truncates such a file (thousands of
          lines) so it must NOT be typed line by line.
        - A PARTIAL config (no header) is applied line by line over the
          serial console — reliable for the small deltas the NetPilot agent
          authors, and needs no reboot.
        """
        if not os.path.exists(STARTUP_CONFIG_FILE):
            self.logger.trace(f"Startup config file {STARTUP_CONFIG_FILE} not found, skipping")
            return

        self.logger.info(f"Found startup config file {STARTUP_CONFIG_FILE}")

        with open(STARTUP_CONFIG_FILE) as file:
            config_lines = [line.rstrip() for line in file if line.strip()]

        if not config_lines:
            self.logger.trace("Startup config file is empty, skipping")
            return

        # Full backup → native restore (atomic, verified by FortiOS itself).
        if config_lines[0].startswith("#config-version="):
            self.restore_full_config()
            return

        self.logger.info(f"Applying {len(config_lines)} lines from startup config")

        # Apply each config line
        for line in config_lines:
            self.logger.trace(f"Applying config: {line}")
            self.wait_write(line, wait=None)
            # Small delay to let FortiOS process each command
            time.sleep(0.1)

        # Wait a bit for config to settle
        time.sleep(1)
        self.logger.info("Startup config applied successfully")

    TFTP_SERVER = "10.0.0.2"

    def _wait_mgmt_ready(self, attempts=40):
        """Wait until the mgmt interface can reach the TFTP server.

        FortiOS reaches the CLI prompt BEFORE port1 finishes DHCP and
        installs its route, so an immediate ``execute restore ... tftp``
        fails with "Network is unreachable". Ping-gate on the QEMU user-net
        gateway until it answers before attempting the transfer.
        """
        marker = b"bytes from " + self.TFTP_SERVER.encode()
        for _ in range(attempts):
            self.wait_write(f"execute ping {self.TFTP_SERVER}", wait=None)
            (ridx, match, _res) = self.tn.expect(
                [marker, b"100% packet loss", b"unreachable"], 8
            )
            if match and ridx == 0:
                self.logger.info("mgmt interface reachable; TFTP server pingable")
                return True
            time.sleep(3)
        self.logger.error("mgmt interface never became reachable for TFTP")
        return False

    def restore_full_config(self):
        """Restore a full FortiOS backup via QEMU's built-in TFTP server.

        QEMU serves ``/tftpboot`` to the guest at the user-mode-net gateway
        ``10.0.0.2`` (the ``tftp=/tftpboot`` netdev option in
        ``common/vrnetlab.py``). ``execute restore config`` downloads the
        file, validates it, applies it transactionally and reboots — so we
        wait for the box to come back before declaring startup complete.
        """
        os.makedirs("/tftpboot", exist_ok=True)
        shutil.copy(STARTUP_CONFIG_FILE, "/tftpboot/restore.conf")

        if not self._wait_mgmt_ready():
            self.logger.error(
                "TFTP server unreachable; cannot restore full config"
            )
            return

        self.logger.info(
            "Restoring full config via TFTP (execute restore config tftp)"
        )
        self.wait_write(
            f"execute restore config tftp restore.conf {self.TFTP_SERVER}",
            wait=None,
        )
        # FortiOS warns it will overwrite + reboot and asks to confirm.
        self.wait_write("y", wait="(y/n)")
        # On success FortiOS validates the file then reboots ("Please stand
        # by while rebooting the system."); on failure it prints an error and
        # stays at the prompt.
        (ridx, match, _res) = self.tn.expect(
            [
                b"rebooting the system",
                b"Can not get file",
                b"Invalid",
                b"command parse error",
            ],
            120,
        )
        if not match or ridx != 0:
            self.logger.error(
                "restore did not confirm reboot; full config may not have applied"
            )
            return
        self.logger.info("Restore accepted; waiting for reboot to complete")
        if self._wait_reset():
            self.logger.info("Full config restored successfully")
        else:
            self.logger.error("Timed out waiting for reboot after restore")

    def _wait_reset(self):
        """
        This function waits for the login prompt after the VM was resetted.
        If commands are issued that enforce a reboot this comes in hand.
        e.g factoryreset or factoryreset2
        """
        self.logger.debug("waiting for reset")
        wait_spins = 0
        while wait_spins < 90:
            _, match, data = self.tn.expect([b"login: "], timeout=10)
            self.logger.trace(data.decode("UTF-8"))
            if match:
                self.logger.debug("reset finished")
                return True
            wait_spins += 1
        self.logger.error("Reset took to long")
        return False


class FortiOS(vrnetlab.VR):
    def __init__(self, hostname, username, password, conn_mode):
        super(FortiOS, self).__init__(username, password)
        self.logger.debug("Hostname")
        self.logger.debug(hostname)
        self.vms = [FortiOS_vm(hostname, username, password, conn_mode)]


def validate_fortios_password(password):
    """
    Validate password meets FortiOS 7.6.5+ policy requirements:
    - Minimum 8 characters
    - At least one uppercase letter
    - At least one lowercase letter
    - At least one digit
    - At least one special character (!, #, $, %, ^, &, *, (, ))
    """
    if len(password) < 8:
        return False
    if not any(c.isupper() for c in password):
        return False
    if not any(c.islower() for c in password):
        return False
    if not any(c.isdigit() for c in password):
        return False
    if not any(c in "!#$%^&*()" for c in password):
        return False
    return True


# Default password that meets FortiOS 7.6.5+ policy
DEFAULT_FORTIOS_PASSWORD = "Fortinet!1234"


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="")
    parser.add_argument(
        "--trace", action="store_true", help="enable trace level logging"
    )
    parser.add_argument("--hostname", default="vr-fortinet", help="Fortinet hostname")
    parser.add_argument("--username", default="admin", help="Username")
    parser.add_argument(
        "--password",
        default=os.getenv("PASSWORD", DEFAULT_FORTIOS_PASSWORD),
        help="Password (must meet FortiOS policy: 8+ chars, upper, lower, number, special)",
    )
    parser.add_argument(
        "--connection-mode",
        default="tc",
        help="Connection mode to use in the datapath",
    )
    args = parser.parse_args()

    # Validate password meets FortiOS policy, use default if not
    if not validate_fortios_password(args.password):
        logging.getLogger().warning(
            f"Password '{args.password}' does not meet FortiOS policy. "
            f"Using default: {DEFAULT_FORTIOS_PASSWORD}"
        )
        args.password = DEFAULT_FORTIOS_PASSWORD

    LOG_FORMAT = "%(asctime)s: %(module)-10s %(levelname)-8s %(message)s"
    logging.basicConfig(format=LOG_FORMAT)
    logger = logging.getLogger()

    logger.setLevel(logging.DEBUG)
    if args.trace:
        logger.setLevel(1)
    vrnetlab.boot_delay()
    vr = FortiOS(
        args.hostname, args.username, args.password, conn_mode=args.connection_mode
    )
    vr.start()
