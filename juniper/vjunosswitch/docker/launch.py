#!/usr/bin/env python3
import datetime
import logging
import os
import re
import signal
import subprocess
import sys

import vrnetlab

# loadable startup config
STARTUP_CONFIG_FILE = "/config/startup-config.cfg"
# initial configuration rendered from the init.conf template on every start
JUNIPER_CONFIG_FILE = "juniper.conf"
# the console login banner of a Junos that booted without applying juniper.conf:
# "FreeBSD/amd64 (Amnesiac) (ttyu0)" instead of the configured host name
AMNESIAC_BANNER = re.compile(rb"\(Amnesiac\)\s*\(tty")
AMNESIAC_ERROR = "Junos booted Amnesiac: the startup configuration was not applied"


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


class VJUNOSSWITCH_vm(vrnetlab.VM):
    def __init__(self, hostname, username, password, conn_mode):
        disk_image = vrnetlab.find_disk_image()
        super(VJUNOSSWITCH_vm, self).__init__(
            username,
            password,
            disk_image=disk_image,
            ram=5120,
            driveif="virtio",
            cpu="host",
            smp="4,sockets=1,cores=4,threads=1",
            mgmt_passthrough=False,
            # no data link → no NIC → the FPC never boots (900 s BOOT_TIMEOUT loop): one dummy NIC (clab#507)
            min_dp_nics=1,
        )
        # device hostname
        self.hostname = hostname

        self.amnesiac_retried = False
        self.console_since_banner = b""
        self.console_tail = b""
        self.render_config()

        # these QEMU cmd line args are translated from the shipped libvirt XML file
        self.qemu_args.extend(["-overcommit", "mem-lock=off"])

        # extend QEMU args with device USB details, xhci is most virtualization-friendly
        self.qemu_args.extend(["-device", "qemu-xhci,id=usb,bus=pci.0,addr=0x1.0x2"])

        # mount config disk with juniper.conf base configs
        self.qemu_args.extend(
            [
                "-drive",
                "file=/config.img,format=raw,if=none,id=config_disk",
                "-device",
                "usb-storage,drive=config_disk,id=usb-disk0,removable=off,write-cache=on",
            ]
        )

        self.qemu_args.extend(["-no-user-config", "-nodefaults", "-boot", "strict=on"])
        self.nic_type = "virtio-net-pci"
        # 1 management port + 48 front ports + 8 uplink ports to match most dense 1U Juniper switch
        self.num_nics = 57
        self.smbios = ["type=1,product=VM-VEX"]
        self.conn_mode = conn_mode

    def render_config(self):
        """Render juniper.conf from the init.conf template plus the user
        startup config, and build the config disk from it."""
        # read init.conf configuration file to replace hostname placehodler
        # with given hostname
        with open("init.conf", "r") as file:
            cfg = file.read()

        ipv6_address_config = self.render_optional_mgmt_config(
            "family inet6 {\n                address {address};\n            }",
            address=self.mgmt_address_ipv6,
        )
        ipv6_route_config = self.render_optional_mgmt_config(
            "rib mgmt_junos.inet6.0 {\n"
            "                static {\n"
            "                    route ::/0 next-hop {gateway};\n"
            "                }\n"
            "            }",
            address=self.mgmt_address_ipv6,
            gateway=self.mgmt_gw_ipv6,
        )
        cfg = (
            cfg.replace("{MGMT_IP_IPV4}", self.mgmt_address_ipv4)
            .replace("{MGMT_GW_IPV4}", self.mgmt_gw_ipv4)
            .replace("{MGMT_IPV6_ADDRESS_CONFIG}", ipv6_address_config)
            .replace("{MGMT_IPV6_ROUTE_CONFIG}", ipv6_route_config)
            .replace("{HOSTNAME}", self.hostname)
        )

        # write the rendered config to juniper.conf, keep init.conf intact
        # so that the container can be restarted
        with open(JUNIPER_CONFIG_FILE, "w") as file:
            file.write(cfg)

        # pass in user startup config
        self.startup_config()

    def startup_config(self):
        """Load additional config provided by user and append initial
        configurations set by vrnetlab."""
        # juniper.conf is (re)rendered from the pristine init.conf template on
        # every start, so append the user startup config to it if it exists
        if os.path.exists(STARTUP_CONFIG_FILE):
            self.logger.trace(
                f"Startup config file {STARTUP_CONFIG_FILE} found, appending to initial configuration"
            )
            with open(STARTUP_CONFIG_FILE, "r") as file:
                startup_cfg = file.read()
            with open(JUNIPER_CONFIG_FILE, "a") as file:
                file.write(startup_cfg)

        # generate mountable config disk based on juniper.conf file with base vrnetlab configs
        subprocess.run(["./make-config.sh", JUNIPER_CONFIG_FILE, "config.img"], check=True)

    def bootstrap_spin(self):
        """This function should be called periodically to do work."""
        # Junos 26.x sends its boot output to /boot/console.log, so the serial
        # console stays silent for ~10 min under nested virtualization — 300
        # silent spins restarted a VM that was still booting, every time
        # (golden-setup-vjunos-bench, 2026-10-28). Allow ~30 min of silence.
        if self.spins > 1800:
            # too many spins with no result ->  give up
            self.banner_seen = False
            self.stop()
            self.start()
            return

        # lets wait for the OS/platform log to determine if VM is booted,
        # login prompt can get lost in boot logs
        # Once the banner is consumed, keep waiting for login: on later spins
        # instead of expecting the banner again (a slow nested boot prints the
        # prompt more than a second after it; Codex vrnetlab#4).
        if not getattr(self, "banner_seen", False):
            (ridx, match, res) = self.tn.expect([b"FreeBSD/amd64"], 1)
            # expect() drops what it read on a timeout, so a banner split
            # across two reads ("FreeBSD/amd" | "64 (r1) (ttyu0)") never
            # matched and the boot waited for the silence restart (bench,
            # containerlab-mcp#493): search a rolling tail as well
            tail = (self.console_tail + (res or b""))[-1024:]
            if match or b"FreeBSD/amd64" in tail:
                self.logger.info("VM started")
                self.banner_seen = True
                self.console_since_banner = (
                    b"" if match else tail[tail.rfind(b"FreeBSD/amd64") :]
                )
                self.console_tail = b""
                self.wait_write("\r", None)
            else:
                self.console_tail = tail
        else:
            res = b""
        if getattr(self, "banner_seen", False):
            (_, loginMatch, login_res) = self.tn.expect([b"login:"], 1)
            res = res + (login_res or b"")
            if not loginMatch:
                # keep the tail: the "(Amnesiac)" banner can arrive a spin
                # before the login: prompt it belongs to
                self.console_since_banner = (
                    self.console_since_banner + (login_res or b"")
                )[-4096:]
            if not loginMatch and login_res == b"" and self.spins % 30 == 29:
                # nudge the console in case the prompt was printed before
                # we started waiting for it
                self.wait_write("\r", None)
            if loginMatch:
                self.logger.info("Login prompt found")
                if AMNESIAC_BANNER.search(self.console_since_banner + login_res):
                    if not self.amnesiac_retried:
                        self.logger.error(
                            f"{AMNESIAC_ERROR}; restarting the VM once with a freshly rendered config disk"
                        )
                        self.amnesiac_retried = True
                        self.tn.close()
                        self.retry_fresh_boot()
                        return
                    # a startup file Junos rejects stays Amnesiac on every boot
                    self.logger.error(
                        f"{AMNESIAC_ERROR} (again after one retry): check the startup configuration "
                        "for a statement Junos rejects; the node has no management address"
                    )

                # close telnet connection
                self.tn.close()
                # startup time?
                startup_time = datetime.datetime.now() - self.start_time
                self.logger.info("Startup complete in: %s" % startup_time)
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

    def retry_fresh_boot(self):
        """Boot again as a first boot: a freshly rendered config disk and a
        new overlay, the same as a redeploy of the node."""
        self.stop()
        self.render_config()
        overlay = self.overlay_disk_image()
        if overlay:
            os.remove(overlay)
            subprocess.run(
                [
                    "qemu-img",
                    "create",
                    "-f",
                    "qcow2",
                    "-F",
                    self._overlay_disk_image_format(),
                    "-b",
                    self.image,
                    overlay,
                ],
                check=True,
            )
        self.banner_seen = False
        self.console_tail = b""
        self.spins = 0
        self.start()

    def overlay_disk_image(self):
        """The overlay qcow2 the -drive argument boots, or None."""
        for arg in self.qemu_args:
            m = re.match(r"if=\w+,file=(\S+-overlay\.qcow2)$", arg)
            if m and os.path.exists(m.group(1)):
                return m.group(1)
        return None


class VJUNOSSWITCH(vrnetlab.VR):
    def __init__(self, hostname, username, password, conn_mode):
        super(VJUNOSSWITCH, self).__init__(username, password)
        self.vms = [VJUNOSSWITCH_vm(hostname, username, password, conn_mode)]


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="")
    parser.add_argument(
        "--trace", action="store_true", help="enable trace level logging"
    )
    parser.add_argument(
        "--hostname", default="vr-vjunosswitch", help="vJunos-switch hostname"
    )
    parser.add_argument("--username", default="vrnetlab", help="Username")
    parser.add_argument("--password", default="VR-netlab9", help="Password")
    parser.add_argument(
        "--connection-mode", default="tc", help="Connection mode to use in the datapath"
    )
    args = parser.parse_args()

    LOG_FORMAT = "%(asctime)s: %(module)-10s %(levelname)-8s %(message)s"
    logging.basicConfig(format=LOG_FORMAT)
    logger = logging.getLogger()

    logger.setLevel(logging.DEBUG)
    if args.trace:
        logger.setLevel(1)

    vr = VJUNOSSWITCH(
        args.hostname,
        args.username,
        args.password,
        conn_mode=args.connection_mode,
    )
    vr.start()
