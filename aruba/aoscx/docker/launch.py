#!/usr/bin/env python3

import datetime
import logging
import os
import re
import signal
import sys
import time

import vrnetlab

STARTUP_CONFIG_FILE = "/config/startup-config.cfg"
# Seconds a startup-config line may take to return the prompt before the
# injection gives up with an error line instead of hanging the boot.
STARTUP_LINE_TIMEOUT = 120
# AOS-CX confirmation questions: "Do you want to create (y/n)? ",
# "Do you want to continue (y/n)? ", "Do you want to continue? (y/n)? ".
YN_QUESTION = rb"\(y/n\)\??\s*$"

def handle_SIGCHLD(signal, frame):
    os.waitpid(-1, os.WNOHANG)


def handle_SIGTERM(signal, frame):
    sys.exit(0)


signal.signal(signal.SIGINT, handle_SIGTERM)
signal.signal(signal.SIGTERM, handle_SIGTERM)
signal.signal(signal.SIGCHLD, handle_SIGCHLD)

TRACE_LEVEL_NUM = 9
logging.addLevelName(TRACE_LEVEL_NUM, "TRACE")


def trace(self, message, *args, **kws):
    # Yes, logger takes its '*args' as 'args'.
    if self.isEnabledFor(TRACE_LEVEL_NUM):
        self._log(TRACE_LEVEL_NUM, message, args, **kws)


logging.Logger.trace = trace


class AOSCX_vm(vrnetlab.VM):
    def __init__(self, hostname, username, password, conn_mode):
        disk_image = ""
        for e in os.listdir("/"):
            if re.search(".vmdk$", e):
                disk_image = "/" + e
        if disk_image == "":
            logging.getLogger().info("Disk image was not found")
            exit(1)
        super(AOSCX_vm, self).__init__(
            username, password, disk_image=disk_image, ram=8192, cpu="host", smp="4"
        )
        self.hostname = hostname
        self.conn_mode = conn_mode
        self.num_nics = 20
        self.nic_type = "virtio-net-pci"

    def bootstrap_spin(self):
        """This function should be called periodically to do work."""

        if self.spins > 300:
            # too many spins with no result ->  give up
            self.logger.info("To many spins with no result, restarting")
            self.stop()
            self.start()
            return

        (ridx, match, res) = self.tn.expect([b"switch login:"], 1)
        if match:  # got a match!
            if ridx == 0:  # login
                self.logger.debug("trying to log in with 'admin'")
                self.wait_write("\r", wait=None)
                self.logger.debug("sent newline")
                self.wait_write("admin", wait="switch login:")
                self.logger.debug("sent username")
                self.wait_write("\r", wait="Password:")
                self.logger.debug("sent empty password")
                self.logger.debug("resetting password")
                self.wait_write("admin", wait="Enter new password:")
                self.wait_write("admin", wait="Confirm new password:")

                # run main config!
                self.bootstrap_config()
                applied = self.startup_config()
                # close telnet connection
                self.tn.close()
                # startup time?
                startup_time = datetime.datetime.now() - self.start_time
                if applied:
                    self.logger.info("Startup complete in: %s" % startup_time)
                else:
                    self.logger.error(
                        f"Startup complete in: {startup_time}, startup-config NOT "
                        "fully applied (see 'startup-config stalled' above)"
                    )
                # mark as running
                self.running = True
                return

        # no match, if we saw some output from the router it's probably
        # booting, so let's give it some more time
        if res != b"":
            self.logger.trace("OUTPUT: %s" % res.decode())
            # reset spins if we saw some output
            self.spins = 0

        self.spins += 1

        return

    def bootstrap_config(self):
        """Do the actual bootstrap config"""
        self.logger.info("applying bootstrap configuration")
        self.wait_write("", None)
        self.wait_write("configure")
        self.wait_write(
            "user %s group administrators password plaintext %s"
            % (self.username, self.password)
        )

        # configure mgmt interface
        self.wait_write("interface mgmt")
        self.wait_write("ip static 10.0.0.15/24")
        self.wait_write("default-gateway 10.0.0.2")
        self.wait_write("no shutdown")
        self.wait_write("exit")
        self.wait_write("ssh server vrf mgmt")
        self.wait_write("https-server vrf mgmt")
        self.wait_write("ntp vrf mgmt")

        self.wait_write(f"hostname {self.hostname}")

        self.wait_write("end")
        self.wait_write("write memory")
        self.wait_write("")

    def startup_config(self):
        """Load additional config provided by user.

        Returns False when the injection stalled before every line landed.
        """

        if not os.path.exists(STARTUP_CONFIG_FILE):
            self.logger.trace(f"Startup config file {STARTUP_CONFIG_FILE} is not found")
            return True

        self.logger.trace(f"Startup config file {STARTUP_CONFIG_FILE} exists")
        with open(STARTUP_CONFIG_FILE) as file:
            config_lines = file.readlines()
            config_lines = [line.rstrip() for line in config_lines]
            self.logger.trace(f"Parsed startup config file {STARTUP_CONFIG_FILE}")

        self.logger.info(f"Writing lines from {STARTUP_CONFIG_FILE}")

        self.wait_write("configure")
        # Apply lines from file. Each line waits for the prompt of the one
        # before it; a (y/n) question in between (e.g. an interface naming an
        # OSPF area that does not exist yet) is answered "y".
        sent = "configure"
        for num, line in enumerate(config_lines + [None], start=1):
            if not self.wait_prompt(STARTUP_LINE_TIMEOUT):
                self.logger.error(
                    f"startup-config stalled: no prompt within {STARTUP_LINE_TIMEOUT}s "
                    f"after '{sent}'; {len(config_lines) - num + 1} later line(s) "
                    "not applied"
                )
                return False
            if line is None:
                break
            self.logger.debug(f"writing to serial console: '{line}'")
            self.tn.write(f"{line}\r".encode())
            sent = line
        # End and Save
        self.tn.write(b"end\r")
        self.wait_write("write memory")
        return True

    def wait_prompt(self, timeout):
        """Wait for the '#' prompt, answering any (y/n) question with 'y'.

        Returns False when neither shows within `timeout` seconds.
        """
        while True:
            self.logger.info("waiting for '#' or a (y/n) question on serial console")
            ridx, _, res = self.tn.expect([YN_QUESTION, b"#"], timeout)
            self.logger.info(f"read from serial console: '{res.decode(errors='replace')}'")
            if ridx == 0:
                self.logger.info("answering 'y' to the (y/n) question")
                self.tn.write(b"y\r")
                continue
            return ridx == 1


class AOSCX(vrnetlab.VR):
    def __init__(self, hostname, username, password, conn_mode):
        super(AOSCX, self).__init__(username, password)
        self.vms = [AOSCX_vm(hostname, username, password, conn_mode)]


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="")
    parser.add_argument(
        "--trace", action="store_true", help="enable trace level logging"
    )
    parser.add_argument("--hostname", default="vr-aoscx", help="Router hostname")
    parser.add_argument("--username", default="vrnetlab", help="Username")
    parser.add_argument("--password", default="VR-netlab9", help="Password")
    parser.add_argument(
        "--connection-mode",
        default="vrxcon",
        help="Connection mode to use in the datapath",
    )
    args = parser.parse_args()

    LOG_FORMAT = "%(asctime)s: %(module)-10s %(levelname)-8s %(message)s"
    logging.basicConfig(format=LOG_FORMAT)
    logger = logging.getLogger()

    logger.setLevel(logging.DEBUG)
    if args.trace:
        logger.setLevel(1)

    logger.debug(f"Environment variables: {os.environ}")
    vrnetlab.boot_delay()

    vr = AOSCX(args.hostname, args.username, args.password, args.connection_mode)
    vr.start()

