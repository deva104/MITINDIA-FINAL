"""Print a PBKDF2 hash for a console user's password, for CONSOLE_USERS.

    python scripts/hash_password.py            (asks for the password, no echo)
    python scripts/hash_password.py --json deva "Deva H" Pune
        (asks for the password, prints a whole CONSOLE_USERS entry)

The password is read without echo and never printed or written anywhere.
"""
import getpass
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from auth import hash_password  # noqa: E402


def read_password():
    first = getpass.getpass("Password: ")
    if not first:
        sys.exit("No password entered.")
    if getpass.getpass("Again: ") != first:
        sys.exit("The passwords did not match.")
    return first


def main(argv):
    if argv[:1] == ["--json"]:
        if len(argv) != 4:
            sys.exit("usage: hash_password.py --json <username> <display name> <city>")
        username, display_name, city = argv[1:]
        print(json.dumps({"username": username, "display_name": display_name, "city": city,
                          "password_hash": hash_password(read_password())}))
        return
    if argv:
        sys.exit("usage: hash_password.py [--json <username> <display name> <city>]")
    print(hash_password(read_password()))


if __name__ == "__main__":
    main(sys.argv[1:])
