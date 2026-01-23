#!/usr/bin/env python3
"""Generate IOL license based on hostname and hostid."""
import hashlib
import struct
import socket
import subprocess


def get_hostid():
    """Get the host ID using the hostid command."""
    result = subprocess.run(["hostid"], capture_output=True, text=True)
    return result.stdout.strip()


def generate_iourc():
    """Generate the IOL license file (.iourc) based on CiscoKeyGen algorithm."""
    hostname = socket.gethostname()
    hostid = get_hostid()
    
    # Convert hostid to integer
    ioukey = int(hostid, 16)
    
    # Add ASCII values of hostname characters
    for char in hostname:
        ioukey += ord(char)
    
    # CiscoKeyGen padding constants
    iouPad1 = b"\x4B\x58\x21\x81\x56\x7B\x0D\xF3\x21\x43\x9B\x7E\xAC\x1D\xE6\x8A"
    iouPad2 = b"\x80" + (39 * b"\x00")
    
    # Create MD5 input and generate license key
    md5input = iouPad1 + iouPad2 + struct.pack("!I", ioukey) + iouPad1
    license_key = hashlib.md5(md5input).hexdigest()[:16]
    
    # Write license file
    with open("/iol/.iourc", "w") as f:
        f.write("[license]\n")
        f.write(f"{hostname} = {license_key};\n")
    
    print(f"Generated license for {hostname}: {license_key}")


if __name__ == "__main__":
    generate_iourc()
