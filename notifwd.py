#!/usr/bin/env python3
# notifwd for macOS
# Original author: Jordan Mann,
# with credit to contributors on GitHub:
# https://github.com/jrmann100/notifwd/pulls
# https://github.com/gabeklavans/notifwd/pulls

__version__ = "0.6"

import argparse
import plistlib
import sched
import sqlite3
import subprocess
import time
from datetime import UTC, datetime
from itertools import cycle
from os import environ
from pathlib import Path
from sys import exit, stdout
from typing import Any

import apprise
import requests
from apprise.apprise import Apprise


class Notification:

    @staticmethod
    def coredata_now():
        """
        Create current Cocoa Core Data Timestamp (seconds since Jan 1 2001)
        and subtract notification date to find how many seconds ago it was.
        https://www.epochconverter.com/coredata
        """
        return (datetime.now(UTC) - datetime(2001, 1, 1, tzinfo=UTC)).total_seconds()

    @staticmethod
    def lookup_display_name(identifier):
        """
        Get an application name like "Messages" from an identifier like "com.apple.Messages"
        that comes with the notification.
        """
        return subprocess.run(["mdfind", "kMDItemCFBundleIdentifier", "=",
                               identifier.strip(), "-attr", "kMDItemDisplayName"],
                              stdout=subprocess.PIPE).stdout.decode("utf-8").split(" = ")[-1].strip()

    # Inititialize nonstatic Notification attributes.
    def __init__(self):
        self.identifier = ""
        self.app = ""
        self.title = ""
        self.subtitle = ""
        self.body = ""
        # Combined body and subtitle.
        self.text = ""
        self.ago = 0.0
        self.date = 0.0
        self.xml = ""

    # Display notification info, for logging.
    def __str__(self):
        return ("%d minutes ago from %s: \"%s\"" % (
            (int(self.ago/60)), self.app, self.title.strip()))

    # Send a notification to the Prowl API.
    def send(self, silent: bool, apobj: Apprise):
        if not silent:
            print("\nSending notification from", self)

        try:
            apobj.notify(title=f"{self.title} ({self.app})", body=self.text)
        except Exception as e:
            print(f"Failed to send notification via apprise: {e}")


def parse_notification(raw_plist):
    """
    Create a notification from raw plist data. The returned notification can then be sent.
    """

    notif = Notification()
    # Parse raw database data, which is an Apple plist.
    data = plistlib.loads(raw_plist)
    for key, value in data.items():
        if key == "app":
            notif.identifier = value or ""
            notif.app = Notification.lookup_display_name(value) or ""
        elif key == "date":
            notif.date = float(value)
            notif.ago = Notification.coredata_now() - float(value)
        elif key == "req":
            for subkey, subvalue in value.items():
                if subkey == "titl":
                    notif.title = subvalue or ""
                if subkey == "subt":
                    notif.subtitle = subvalue or ""
                if subkey == "body":
                    notif.body = subvalue or ""
    # Merge subtitle and body - yes, notifications have three lines.
    notif.text = notif.subtitle + ("\u2014" if notif.subtitle else "") + notif.body
    return notif


def get_notification_data(n, cursor: sqlite3.Cursor):
    """
    Fetch data for a specific notification from the database.
    """
    try:
        # I know there is a better way to do this, but I've spent an hour with my limited SQLite knowledge and it isn't enough.
        return cursor.execute("SELECT * FROM (SELECT * FROM record ORDER BY rec_id DESC LIMIT %d) ORDER BY rec_id LIMIT 1" % (n + 1)).fetchone()
    except Exception as e:
        print(f"failed to get notif data: {e}")
        return None


def check(last_id: Any | None, last_date: Any | None, cursor: sqlite3.Cursor, silent: bool, apobj: Apprise) -> tuple[Any | None, Any | None]:
    """
    Collect recent notifications.
    """

    # Oh, I've figured it out. We need to cross-check by timestamps, or dismissed notifications cause the system to never encounter into last_id.
    n = 0
    sql_data = get_notification_data(n, cursor)
    if not sql_data:
        return last_id, last_date

    newest_id = sql_data[0]
    # Either delivered_date or request_date will be filled in. Don't yet want to peek into what those mean.
    newest_date = (sql_data[6] if sql_data[6] is not None else sql_data[4])
    while sql_data[0] != last_id and (sql_data[6] if sql_data[6] != None else sql_data[4]) >= last_date:
        parse_notification(sql_data[3]).send(silent, apobj)
        n += 1
        sql_data = get_notification_data(n, cursor)

    return newest_id, newest_date


def setup() -> tuple[bool, int, sqlite3.Connection, sqlite3.Cursor, Any | None, Any | None, Apprise]:
    parser = argparse.ArgumentParser(
        description="notifwd v%s - macOS notification forwarder" % __version__,
        prog="notifwd")
    parser.add_argument("--notif-url", "-k",
                        help="Apprise Notification URL",
                        default=environ.get("NOTIF_URL"))
    parser.add_argument("--frequency", "-f", type=int,
                        help="Frequency, in seconds, to check for new notifications.",
                        default=60)
    parser.add_argument("--version", action="store_true",
                        help="Get program version")
    parser.add_argument("--silent", "-s",
                        help="Don't display the splash screen or verbose logging.", action="store_true")
    parser.add_argument("--test", "-t",
                        help="Display a test notification on startup.", action="store_true")
    args = parser.parse_args()
    if args.version:
        print("notifwd v%s" % __version__)
        raise SystemExit()
    if args.notif_url is None:
        parser.error("no notification URL is specified. Is $NOTIF_URL defined?")
    if args.frequency <= 0:
        parser.error("frequency must be a positive integer.")

    if not args.silent:
        print("""
  _   _       _   _ _____             _ 
 | \\ | | ___ | |_(_)  ___|_      ____| |
 |  \\| |/ _ \\| __| | |_  \\ \\ /\\ / / _` |
 | |\\  | (_) | |_| |  _|  \\ V  V / (_| |
 |_| \\_|\\___/ \\__|_|_|     \\_/\\_/ \\__,_|

Starting up... """, end="")
    # Locate the database; start SQLite.
    db_path = Path.home() / "Library" / "Group Containers" / "group.com.apple.usernoted" / "db2" / "db"
    connection = sqlite3.connect(db_path)
    cursor = connection.cursor()

    # Set the most recent notification ID to the ID of the last-displayed notification.
    last_data = get_notification_data(0, cursor)
    last_id = None
    last_date = None
    if last_data:
        last_id = last_data[0]
        last_date = last_data[6]

    if args.test:
        print("Sending test notification... ", end="")
        subprocess.run(
            ["osascript", "-e", "display notification time string of (current date) with title \"The time is\" subtitle \"Most definitely\""])
    if not args.silent:
        print("setup done.")

    apobj = apprise.Apprise()
    apobj.add(args.notif_url)

    return args.silent, args.frequency, connection, cursor, last_id, last_date, apobj


def main():
    silent, freq, connection, cursor, last_id, last_date, apobj = setup()

    s = sched.scheduler(time.time, time.sleep)
    # https://stackoverflow.com/a/22616059/9068081
    spinner = cycle(['*', '-', '/', '|', '\\', '-', '*'])

    def scheduled_update(s: sched.scheduler, last_id: Any | None, last_date: Any | None, cursor: sqlite3.Cursor):
        if not silent:
            for _ in range(0, 7):
                time.sleep(0.1)
                stdout.write(next(spinner))
                stdout.flush()
                stdout.write('\b')

        last_id, last_date = check(last_id, last_date, cursor, silent, apobj)
        # Schedule to run periodically.
        s.enter(freq - 0.7, 1, scheduled_update, (s, last_id, last_date, cursor))

    # Schedule to run on start.
    s.enter(0, 1, scheduled_update, (s, last_id, last_date, cursor))
    try:
        print("Starting scheduler. Update frequency is %d second%s. " %
              (freq, ("s" if freq != 1 else "")), end="")
        stdout.flush()  # See note above.
        s.run()
    except KeyboardInterrupt:
        print("\nQuitting...")
        connection.close()
        exit(0)
    except Exception as e:
        raise (e)


if __name__ == "__main__":
    main()
