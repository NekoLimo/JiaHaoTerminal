import platform
import getpass
import socket
from platform import uname_result


def sysinfo():
    u = platform.uname()
    return {
        "system": f"{platform.system()}",
        "userHost": f"{getpass.getuser()}@localhost",
        "unameRelease": f"{u.release}",
        "hardware": f"{u.machine}",
        "kernelName": f"{u.system}",
    }
