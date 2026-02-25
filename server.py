#!/usr/bin/env -S uv run
# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "mcp[cli]>=1.0.0",
#     "pydantic>=2.0.0",
# ]
# ///
"""
Apple Mail + Calendar MCP Server (Python).

Manages Apple Mail and Apple Calendar safely via scoped osascript commands.
Does NOT expose raw AppleScript execution - only a fixed set of parameterized tools.
User input is passed as argv to constant scripts, preventing injection.

Write actions (send email, move message, create event) require a two-step
confirmation flow with a token.
"""

import asyncio
import json
import os
import re
import secrets
import subprocess
import time
from datetime import datetime, timedelta
from typing import Any, Optional, Union

from mcp.server.fastmcp import FastMCP
from pydantic import BaseModel, ConfigDict, Field, field_validator

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

OSASCRIPT_BIN = "/usr/bin/osascript"
_raw_timeout = os.environ.get("OSASCRIPT_MCP_TIMEOUT_MS")
if _raw_timeout and _raw_timeout.isdigit():
    OSASCRIPT_TIMEOUT_S = min(300, max(5, int(_raw_timeout) // 1000))
else:
    OSASCRIPT_TIMEOUT_S = 30
OSASCRIPT_MAX_BUFFER_BYTES = 1024 * 1024
DEBUG = os.environ.get("OSASCRIPT_MCP_DEBUG") == "1"

# ---------------------------------------------------------------------------
# Token store for two-step confirmation
# ---------------------------------------------------------------------------

_TOKEN_TTL_S = 10 * 60  # 10 minutes


class _TokenStore:
    """In-memory store for confirmation tokens with TTL."""

    def __init__(self, ttl_s: int = _TOKEN_TTL_S) -> None:
        self._ttl_s = ttl_s
        self._items: dict[str, dict[str, Any]] = {}

    def put(self, value: dict[str, Any]) -> str:
        token = f"confirm_{secrets.token_hex(16)}"
        self._items[token] = {"value": value, "expires_at": time.time() + self._ttl_s}
        return token

    def take(self, token: str) -> Optional[dict[str, Any]]:
        entry = self._items.pop(token, None)
        if entry is None:
            return None
        if time.time() > entry["expires_at"]:
            return None
        return entry["value"]


_confirmation_tokens = _TokenStore()

# ---------------------------------------------------------------------------
# osascript execution
# ---------------------------------------------------------------------------


async def _run_osascript(script: str, argv: list[str] | None = None) -> str:
    """Execute an AppleScript via osascript, passing user data as argv only."""
    args = [OSASCRIPT_BIN, "-e", script, *(argv or [])]
    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            limit=OSASCRIPT_MAX_BUFFER_BYTES,
        )
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(), timeout=OSASCRIPT_TIMEOUT_S
        )
        if DEBUG and stderr:
            import sys

            print(
                f"osascript stderr: {stderr.decode(errors='replace')}", file=sys.stderr
            )
        if proc.returncode != 0:
            stderr_text = stderr.decode(errors="replace").strip()
            stderr_short = "\n".join(stderr_text.splitlines()[:3])
            detail = f": {stderr_short}" if stderr_short else ""
            raise RuntimeError(
                f"osascript execution failed (code={proc.returncode}){detail}"
            )
        return stdout.decode(errors="replace").strip()
    except asyncio.TimeoutError:
        raise RuntimeError("osascript execution timed out")


# ---------------------------------------------------------------------------
# AppleScript templates (constant scripts - user input via argv only)
# ---------------------------------------------------------------------------

AS_JSON_UTILS = r"""
on _json_escape(s)
  if s is missing value then return ""
  set s to s as string
  set s to my _replace_text(s, "\\", "\\\\")
  set s to my _replace_text(s, "\"", "\\\"")
  set s to my _replace_text(s, return, "\\n")
  set s to my _replace_text(s, linefeed, "\\n")
  set s to my _replace_text(s, tab, "\\t")
  return s
end _json_escape

on _replace_text(s, find, repl)
  set AppleScript's text item delimiters to find
  set parts to text items of s
  set AppleScript's text item delimiters to repl
  set s2 to parts as string
  set AppleScript's text item delimiters to ""
  return s2
end _replace_text
"""

AS_MAIL_LIST_ACCOUNTS = (
    AS_JSON_UTILS
    + r"""
on run argv
  tell application "Mail"
    set out to "["
    set firstItem to true
    repeat with a in accounts
      if firstItem then
        set firstItem to false
      else
        set out to out & ","
      end if
      set out to out & "\"" & my _json_escape(name of a) & "\""
    end repeat
    set out to out & "]"
    return out
  end tell
end run
"""
)

AS_MAIL_LIST_MAILBOXES = (
    AS_JSON_UTILS
    + r"""
on _walk_mailboxes(mboxList, prefix)
  set out to ""
  repeat with mb in mboxList
    set mbName to name of mb as string
    if prefix is "" then
      set fullName to mbName
    else
      set fullName to prefix & "/" & mbName
    end if
    set out to out & fullName & linefeed
    try
      set children to mailboxes of mb
      if (count of children) > 0 then
        set out to out & my _walk_mailboxes(children, fullName)
      end if
    end try
  end repeat
  return out
end _walk_mailboxes

on run argv
  set accountName to item 1 of argv
  tell application "Mail"
    set a to first account whose name is accountName
    set rawList to my _walk_mailboxes(mailboxes of a, "")
    set AppleScript's text item delimiters to linefeed
    set itemsList to text items of rawList
    set AppleScript's text item delimiters to ""
    set out to "["
    set firstItem to true
    repeat with s in itemsList
      if (s as string) is not "" then
        if firstItem then
          set firstItem to false
        else
          set out to out & ","
        end if
        set out to out & "\"" & my _json_escape(s) & "\""
      end if
    end repeat
    set out to out & "]"
    return out
  end tell
end run
"""
)

AS_MAIL_LIST_MESSAGES = (
    AS_JSON_UTILS
    + r"""
on run argv
  set accountName to item 1 of argv
  set mailboxPath to item 2 of argv
  set limitCount to (item 3 of argv) as integer
  set unreadOnly to item 4 of argv

  set AppleScript's text item delimiters to "/"
  set pathParts to text items of mailboxPath
  set AppleScript's text item delimiters to ""

  tell application "Mail"
    set a to first account whose name is accountName
    set mb to mailboxes of a
    set currentBox to missing value
    repeat with p in pathParts
      set partName to p as string
      if currentBox is missing value then
        set currentBox to first mailbox of a whose name is partName
      else
        set currentBox to first mailbox of currentBox whose name is partName
      end if
    end repeat
    set mbx to currentBox

    set out to "["
    set firstItem to true
    set i to 1
    repeat with m in (messages of mbx)
      if i > limitCount then exit repeat

      set includeMsg to true
      if unreadOnly is "true" then
        if (read status of m) is true then
          set includeMsg to false
        end if
      end if

      if includeMsg is false then
        set i to i + 1
      else
      set subj to ""
      try
        set subj to subject of m
      end try
      set snd to ""
      try
        set snd to sender of m
      end try
      set dr to ""
      try
        set dr to (date received of m) as string
      end try

      if firstItem then
        set firstItem to false
      else
        set out to out & ","
      end if
      set out to out & "{" & "\"index\":" & (i as string) & "," & "\"subject\":\"" & my _json_escape(subj) & "\"," & "\"from\":\"" & my _json_escape(snd) & "\"," & "\"date_received\":\"" & my _json_escape(dr) & "\"" & "}"
      set i to i + 1
      end if
    end repeat
    set out to out & "]"
    return out
  end tell
end run
"""
)

AS_MAIL_GET_MESSAGE = (
    AS_JSON_UTILS
    + r"""
on run argv
  set accountName to item 1 of argv
  set mailboxPath to item 2 of argv
  set msgIndex to (item 3 of argv) as integer
  set includeBody to item 4 of argv
  set maxChars to (item 5 of argv) as integer

  set AppleScript's text item delimiters to "/"
  set pathParts to text items of mailboxPath
  set AppleScript's text item delimiters to ""

  tell application "Mail"
    set a to first account whose name is accountName
    set currentBox to missing value
    repeat with p in pathParts
      set partName to p as string
      if currentBox is missing value then
        set currentBox to first mailbox of a whose name is partName
      else
        set currentBox to first mailbox of currentBox whose name is partName
      end if
    end repeat
    set mbx to currentBox

    set m to item msgIndex of (messages of mbx)

    set subj to ""
    try
      set subj to subject of m
    end try
    set snd to ""
    try
      set snd to sender of m
    end try
    set dr to ""
    try
      set dr to (date received of m) as string
    end try

    set bodyText to ""
    if includeBody is "true" then
      try
        set bodyText to content of m
      end try
      if (count of characters of bodyText) > maxChars then
        set bodyText to (text 1 thru maxChars of bodyText) & "..."
      end if
    end if

    set out to "{" & "\"index\":" & (msgIndex as string) & "," & "\"subject\":\"" & my _json_escape(subj) & "\"," & "\"from\":\"" & my _json_escape(snd) & "\"," & "\"date_received\":\"" & my _json_escape(dr) & "\"," & "\"body\":\"" & my _json_escape(bodyText) & "\"" & "}"
    return out
  end tell
end run
"""
)

AS_MAIL_SEND = r"""
on run argv
  set toList to item 1 of argv
  set ccList to item 2 of argv
  set bccList to item 3 of argv
  set subjectText to item 4 of argv
  set bodyText to item 5 of argv

  set AppleScript's text item delimiters to ","
  set toItems to {}
  if toList is not "" then set toItems to text items of toList
  set ccItems to {}
  if ccList is not "" then set ccItems to text items of ccList
  set bccItems to {}
  if bccList is not "" then set bccItems to text items of bccList
  set AppleScript's text item delimiters to ""

  tell application "Mail"
    set newMessage to make new outgoing message with properties {subject:subjectText, content:bodyText, visible:false}
    tell newMessage
      repeat with addr in toItems
        if (addr as string) is not "" then
          make new to recipient at end of to recipients with properties {address:addr as string}
        end if
      end repeat
      repeat with addr in ccItems
        if (addr as string) is not "" then
          make new cc recipient at end of cc recipients with properties {address:addr as string}
        end if
      end repeat
      repeat with addr in bccItems
        if (addr as string) is not "" then
          make new bcc recipient at end of bcc recipients with properties {address:addr as string}
        end if
      end repeat
    end tell
    send newMessage
  end tell
  return "sent"
end run
"""

AS_MAIL_MOVE_MESSAGE = r"""
on run argv
  set accountName to item 1 of argv
  set srcMailboxPath to item 2 of argv
  set msgIndex to (item 3 of argv) as integer
  set dstMailboxPath to item 4 of argv

  set AppleScript's text item delimiters to "/"
  set srcParts to text items of srcMailboxPath
  set dstParts to text items of dstMailboxPath
  set AppleScript's text item delimiters to ""

  tell application "Mail"
    set a to first account whose name is accountName

    -- resolve source mailbox
    set srcBox to missing value
    repeat with p in srcParts
      set partName to p as string
      if srcBox is missing value then
        set srcBox to first mailbox of a whose name is partName
      else
        set srcBox to first mailbox of srcBox whose name is partName
      end if
    end repeat

    -- resolve destination mailbox
    set dstBox to missing value
    repeat with p in dstParts
      set partName to p as string
      if dstBox is missing value then
        set dstBox to first mailbox of a whose name is partName
      else
        set dstBox to first mailbox of dstBox whose name is partName
      end if
    end repeat

    set m to item msgIndex of (messages of srcBox)
    move m to dstBox
  end tell
  return "moved"
end run
"""

AS_CALENDAR_LIST_CALENDARS = (
    AS_JSON_UTILS
    + r"""
on run argv
  tell application "Calendar"
    set out to "["
    set firstItem to true
    repeat with c in calendars
      if firstItem then
        set firstItem to false
      else
        set out to out & ","
      end if
      set out to out & "\"" & my _json_escape(name of c) & "\""
    end repeat
    set out to out & "]"
    return out
  end tell
end run
"""
)

AS_CALENDAR_LIST_CALENDARS_DETAILED = (
    AS_JSON_UTILS
    + r"""
on run argv
  tell application "Calendar"
    set out to "["
    set firstItem to true
    set i to 1
    repeat with c in calendars
      set calName to ""
      try
        set calName to name of c
      end try
      set srcName to ""
      try
        set srcName to name of source of c
      end try

      if firstItem then
        set firstItem to false
      else
        set out to out & ","
      end if

      set out to out & "{" & "\"index\":" & (i as string) & "," & "\"name\":\"" & my _json_escape(calName) & "\"," & "\"source\":\"" & my _json_escape(srcName) & "\"" & "}"
      set i to i + 1
    end repeat
    set out to out & "]"
    return out
  end tell
end run
"""
)

AS_CALENDAR_LIST_EVENTS = (
    AS_JSON_UTILS
    + r"""
on _month_num(m)
  set monthsList to {January, February, March, April, May, June, July, August, September, October, November, December}
  repeat with i from 1 to 12
    if item i of monthsList is m then return i
  end repeat
  return 0
end _month_num

on _date_parts(dt)
  return "{" & "\"y\":" & (year of dt as integer) & "," & "\"m\":" & (my _month_num(month of dt)) & "," & "\"d\":" & (day of dt as integer) & "," & "\"hh\":" & (hours of dt as integer) & "," & "\"mm\":" & (minutes of dt as integer) & "," & "\"ss\":" & (seconds of dt as integer) & "}"
end _date_parts

on _make_date(y, m, d, hh, mm, ss)
  set monthsList to {January, February, March, April, May, June, July, August, September, October, November, December}
  set dt to current date
  set year of dt to y
  set month of dt to item m of monthsList
  set day of dt to d
  set hours of dt to hh
  set minutes of dt to mm
  set seconds of dt to ss
  return dt
end _make_date

on run argv
  set calendarIndex to (item 1 of argv) as integer
  set calendarName to item 2 of argv
  set sy to (item 3 of argv) as integer
  set sm to (item 4 of argv) as integer
  set sd to (item 5 of argv) as integer
  set sh to (item 6 of argv) as integer
  set smin to (item 7 of argv) as integer
  set ss to (item 8 of argv) as integer
  set ey to (item 9 of argv) as integer
  set em to (item 10 of argv) as integer
  set ed to (item 11 of argv) as integer
  set eh to (item 12 of argv) as integer
  set emin to (item 13 of argv) as integer
  set es to (item 14 of argv) as integer
  set limitCount to (item 15 of argv) as integer

  set startDate to my _make_date(sy, sm, sd, sh, smin, ss)
  set endDate to my _make_date(ey, em, ed, eh, emin, es)

  tell application "Calendar"
    if calendarIndex > 0 then
      set cal to item calendarIndex of calendars
    else
      if calendarName is "" then
        set cal to first calendar
      else
        set cal to first calendar whose name is calendarName
      end if
    end if

    set evs to every event of cal whose start date is not less than startDate and start date is less than endDate

    set candidateLimit to limitCount * 20
    if candidateLimit < 50 then set candidateLimit to 50
    if candidateLimit > 500 then set candidateLimit to 500

    set out to "["
    set firstItem to true
    set i to 0
    repeat with e in evs
      set i to i + 1
      if i > candidateLimit then exit repeat

      set t to ""
      try
        set t to summary of e
      end try
      set loc to ""
      try
        set loc to location of e
      end try
      set rs to ""
      try
        set rs to recurrence of e
      end try

      set sdt to start date of e
      set edt to end date of e

      if firstItem then
        set firstItem to false
      else
        set out to out & ","
      end if
      set out to out & "{" & "\"title\":\"" & my _json_escape(t) & "\"," & "\"location\":\"" & my _json_escape(loc) & "\"," & "\"recurrence\":\"" & my _json_escape(rs) & "\"," & "\"start\":" & my _date_parts(sdt) & "," & "\"end\":" & my _date_parts(edt) & "}"
    end repeat
    set out to out & "]"
    return out
  end tell
end run
"""
)

AS_CALENDAR_CREATE_EVENT = r"""
on _make_date(y, m, d, hh, mm, ss)
  set monthsList to {January, February, March, April, May, June, July, August, September, October, November, December}
  set dt to current date
  set year of dt to y
  set month of dt to item m of monthsList
  set day of dt to d
  set hours of dt to hh
  set minutes of dt to mm
  set seconds of dt to ss
  return dt
end _make_date

on run argv
  set calendarName to item 1 of argv
  set titleText to item 2 of argv
  set locationText to item 3 of argv
  set notesText to item 4 of argv
  set sy to (item 5 of argv) as integer
  set sm to (item 6 of argv) as integer
  set sd to (item 7 of argv) as integer
  set sh to (item 8 of argv) as integer
  set smin to (item 9 of argv) as integer
  set ss to (item 10 of argv) as integer
  set ey to (item 11 of argv) as integer
  set em to (item 12 of argv) as integer
  set ed to (item 13 of argv) as integer
  set eh to (item 14 of argv) as integer
  set emin to (item 15 of argv) as integer
  set es to (item 16 of argv) as integer

  set startDate to my _make_date(sy, sm, sd, sh, smin, ss)
  set endDate to my _make_date(ey, em, ed, eh, emin, es)

  tell application "Calendar"
    if calendarName is "" then
      set cal to first calendar
    else
      set cal to first calendar whose name is calendarName
    end if
    make new event at end of events of cal with properties {summary:titleText, start date:startDate, end date:endDate, location:locationText, description:notesText}
  end tell
  return "created"
end run
"""

# ---------------------------------------------------------------------------
# Recurrence expansion helpers (mirrors Node.js logic)
# ---------------------------------------------------------------------------

BYDAY_MAP = {"SU": 0, "MO": 1, "TU": 2, "WE": 3, "TH": 4, "FR": 5, "SA": 6}


def _date_from_parts(p: dict) -> datetime:
    return datetime(p["y"], p["m"], p["d"], p["hh"], p["mm"], p["ss"])


def _start_of_week_monday(d: datetime) -> datetime:
    dow = d.weekday()  # Monday=0 in Python
    return datetime(d.year, d.month, d.day) - timedelta(days=dow)


def _days_between(a: datetime, b: datetime) -> int:
    da = datetime(a.year, a.month, a.day)
    db = datetime(b.year, b.month, b.day)
    return (db - da).days


def _weeks_between_monday(a: datetime, b: datetime) -> int:
    wa = _start_of_week_monday(a)
    wb = _start_of_week_monday(b)
    return (wb - wa).days // 7


def _parse_rrule(rrule: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for part in rrule.split(";"):
        if "=" not in part:
            continue
        k, v = part.split("=", 1)
        out[k.upper()] = v
    return out


def _parse_until(until: Optional[str]) -> Optional[datetime]:
    if not until:
        return None
    m = re.match(r"^(\d{4})(\d{2})(\d{2})(?:T(\d{2})(\d{2})(\d{2})Z)?$", until)
    if not m:
        return None
    y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
    if m.group(4):
        hh, mm, ss = int(m.group(4)), int(m.group(5)), int(m.group(6))
        # UTC time
        return datetime(y, mo, d, hh, mm, ss)
    return datetime(y, mo, d, 23, 59, 59)


def _expand_occurrences(
    master_start: datetime,
    master_end: datetime,
    rrule: dict[str, str],
    window_start: datetime,
    window_end: datetime,
) -> list:
    occurrences: list[dict[str, datetime]] = []
    duration = master_end - master_start
    freq = rrule.get("FREQ", "").upper()
    interval = int(rrule.get("INTERVAL", "1") or "1")
    until = _parse_until(rrule.get("UNTIL"))
    time_of_day = (master_start.hour, master_start.minute, master_start.second)

    day = datetime(window_start.year, window_start.month, window_start.day)
    while day < window_end:
        cand = datetime(day.year, day.month, day.day, *time_of_day)

        if until and cand > until:
            day += timedelta(days=1)
            continue
        if cand < master_start:
            day += timedelta(days=1)
            continue

        if freq == "DAILY":
            diff_days = _days_between(master_start, cand)
            if diff_days >= 0 and diff_days % interval == 0:
                occurrences.append({"start": cand, "end": cand + duration})

        elif freq == "WEEKLY":
            byday_str = rrule.get("BYDAY", "")
            byday = [s.strip() for s in byday_str.split(",") if s.strip()]
            if not byday:
                # JS getDay: 0=Sun, Python weekday: 0=Mon
                # Convert master_start to JS-style day
                js_dow = (master_start.weekday() + 1) % 7
                byday = [k for k, v in BYDAY_MAP.items() if v == js_dow]

            # Convert candidate's day to JS-style
            cand_js_dow = (cand.weekday() + 1) % 7
            allowed_dows = [BYDAY_MAP[x] for x in byday if x in BYDAY_MAP]
            if cand_js_dow not in allowed_dows:
                day += timedelta(days=1)
                continue

            wdiff = _weeks_between_monday(master_start, cand)
            if wdiff >= 0 and wdiff % interval == 0:
                occurrences.append({"start": cand, "end": cand + duration})

        day += timedelta(days=1)

    return occurrences


def _parse_iso_to_parts(iso_string: str) -> dict[str, int]:
    """Parse ISO-8601 string to year/month/day/hour/minute/second dict."""
    dt = datetime.fromisoformat(iso_string)
    return {
        "year": dt.year,
        "month": dt.month,
        "day": dt.day,
        "hour": dt.hour,
        "minute": dt.minute,
        "second": dt.second,
    }


# ---------------------------------------------------------------------------
# MCP Server
# ---------------------------------------------------------------------------

mcp = FastMCP("mail_calendar_macos_mcp")

# ---------------------------------------------------------------------------
# Pydantic input models
# ---------------------------------------------------------------------------


class MailListMailboxesInput(BaseModel):
    """Input for listing mailboxes of a Mail account."""

    model_config = ConfigDict(extra="forbid")
    accountName: str = Field(..., description="Apple Mail account name.")


class MailListMessagesInput(BaseModel):
    """Input for listing messages in a mailbox."""

    model_config = ConfigDict(extra="forbid")
    accountName: str = Field(..., description="Apple Mail account name.")
    mailboxPath: str = Field(
        ...,
        description="Mailbox path from mail_list_mailboxes (e.g. 'Inbox' or 'Archive/2025').",
    )
    limit: Optional[int] = Field(
        default=20, description="Max messages to return.", ge=1, le=50
    )
    unreadOnly: Optional[bool] = Field(
        default=False, description="Only return unread messages."
    )


class MailGetMessageInput(BaseModel):
    """Input for getting a single message."""

    model_config = ConfigDict(extra="forbid")
    accountName: str = Field(..., description="Apple Mail account name.")
    mailboxPath: str = Field(..., description="Mailbox path.")
    index: int = Field(..., description="Message index (1-based).", ge=1)
    includeBody: Optional[bool] = Field(
        default=False, description="Include message body."
    )
    maxBodyChars: Optional[int] = Field(
        default=4000,
        description="Max body characters to return.",
        ge=1,
        le=20000,
    )


class MailPrepareSendInput(BaseModel):
    """Input for preparing an email to send."""

    model_config = ConfigDict(extra="forbid")
    to: list[str] = Field(..., description="Recipient email addresses.", min_length=1)
    cc: Optional[list[str]] = Field(default_factory=list, description="CC recipients.")
    bcc: Optional[list[str]] = Field(
        default_factory=list, description="BCC recipients."
    )
    subject: str = Field(..., description="Email subject line.")
    body: str = Field(..., description="Email body text.")


class ConfirmationTokenInput(BaseModel):
    """Input for confirmation-based actions."""

    model_config = ConfigDict(extra="forbid")
    confirmationToken: str = Field(
        ..., description="Confirmation token from prepare step."
    )


class MailPrepareMoveInput(BaseModel):
    """Input for preparing to move a message."""

    model_config = ConfigDict(extra="forbid")
    accountName: str = Field(..., description="Apple Mail account name.")
    mailboxPath: str = Field(..., description="Source mailbox path (e.g. 'Inbox').")
    index: int = Field(
        ...,
        description=(
            "Message index in the source mailbox (from mail_list_messages). "
            "Indexes shift when messages are moved or deleted; re-list to get "
            "current indexes. When batch-moving, process highest index first "
            "to avoid index shifting."
        ),
        ge=1,
    )
    destinationMailboxPath: str = Field(
        ...,
        description=(
            "Destination mailbox path. For Gmail accounts use 'All Mail' to "
            "archive (removes Inbox label). For Exchange/Outlook accounts use "
            "'Archive'. Do not use 'Trash'."
        ),
    )


class CalendarListEventsInput(BaseModel):
    """Input for listing calendar events."""

    model_config = ConfigDict(extra="forbid")
    calendarIndex: Optional[int] = Field(
        default=None,
        description="Optional calendar index from calendar_list_calendars_detailed.",
        ge=1,
    )
    calendarName: Optional[str] = Field(
        default=None,
        description="Optional calendar name. If omitted, uses the first calendar.",
    )
    startISO: str = Field(..., description="ISO-8601 date-time for range start.")
    endISO: str = Field(..., description="ISO-8601 date-time for range end.")
    limit: Optional[int] = Field(
        default=25, description="Maximum events to return.", ge=1, le=50
    )


class CalendarPrepareCreateEventInput(BaseModel):
    """Input for preparing a new calendar event."""

    model_config = ConfigDict(extra="forbid")
    calendarName: Optional[str] = Field(
        default=None,
        description="Optional calendar name. If omitted, uses the first calendar.",
    )
    title: str = Field(..., description="Event title.")
    location: Optional[str] = Field(default=None, description="Event location.")
    notes: Optional[str] = Field(default=None, description="Event notes/description.")
    startISO: str = Field(..., description="ISO-8601 start date-time.")
    endISO: str = Field(..., description="ISO-8601 end date-time.")


# ---------------------------------------------------------------------------
# Mail tools
# ---------------------------------------------------------------------------


@mcp.tool(
    name="mail_list_accounts",
    annotations={
        "title": "List Apple Mail Accounts",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def mail_list_accounts() -> str:
    """List Apple Mail accounts by name."""
    return await _run_osascript(AS_MAIL_LIST_ACCOUNTS)


@mcp.tool(
    name="mail_list_mailboxes",
    annotations={
        "title": "List Mail Mailboxes",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def mail_list_mailboxes(params: MailListMailboxesInput) -> str:
    """List mailboxes for an Apple Mail account. Mailbox names are returned as paths like 'Inbox' or 'Archive/2025'."""
    return await _run_osascript(AS_MAIL_LIST_MAILBOXES, [params.accountName])


@mcp.tool(
    name="mail_list_messages",
    annotations={
        "title": "List Mail Messages",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def mail_list_messages(params: MailListMessagesInput) -> str:
    """List recent messages from a mailbox (metadata only)."""
    limit = params.limit or 20
    unread_only = params.unreadOnly or False
    return await _run_osascript(
        AS_MAIL_LIST_MESSAGES,
        [
            params.accountName,
            params.mailboxPath,
            str(limit),
            "true" if unread_only else "false",
        ],
    )


@mcp.tool(
    name="mail_get_message",
    annotations={
        "title": "Get Mail Message",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def mail_get_message(params: MailGetMessageInput) -> str:
    """Get a message by index from a mailbox. Body is optional and truncated."""
    include_body = params.includeBody or False
    max_body_chars = params.maxBodyChars or 4000
    return await _run_osascript(
        AS_MAIL_GET_MESSAGE,
        [
            params.accountName,
            params.mailboxPath,
            str(params.index),
            "true" if include_body else "false",
            str(max_body_chars),
        ],
    )


@mcp.tool(
    name="mail_prepare_send",
    annotations={
        "title": "Prepare Email to Send",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def mail_prepare_send(params: MailPrepareSendInput) -> str:
    """Prepare an email to send via Apple Mail. Returns a confirmation token; does not send."""
    payload = {
        "kind": "mail_send",
        "to": params.to,
        "cc": params.cc or [],
        "bcc": params.bcc or [],
        "subject": params.subject,
        "body": params.body,
    }
    token = _confirmation_tokens.put(payload)

    body_preview = params.body
    if len(body_preview) > 500:
        body_preview = body_preview[:500] + "..."

    return json.dumps(
        {
            "confirmationToken": token,
            "preview": {
                "to": params.to,
                "cc": params.cc or [],
                "bcc": params.bcc or [],
                "subject": params.subject,
                "bodyPreview": body_preview,
            },
            "note": "Call mail_send with confirmationToken to send.",
        },
        indent=2,
    )


@mcp.tool(
    name="mail_send",
    annotations={
        "title": "Send Prepared Email",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    },
)
async def mail_send(params: ConfirmationTokenInput) -> str:
    """Send a previously prepared email using its confirmation token."""
    payload = _confirmation_tokens.take(params.confirmationToken)
    if payload is None:
        return "Error: Invalid or expired confirmationToken"
    if payload.get("kind") != "mail_send":
        return "Error: confirmationToken is not valid for mail_send"

    to_csv = ",".join(payload["to"])
    cc_csv = ",".join(payload["cc"])
    bcc_csv = ",".join(payload["bcc"])
    result = await _run_osascript(
        AS_MAIL_SEND,
        [to_csv, cc_csv, bcc_csv, payload["subject"], payload["body"]],
    )
    return json.dumps({"status": result}, indent=2)


@mcp.tool(
    name="mail_prepare_move",
    annotations={
        "title": "Prepare Move Message",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def mail_prepare_move(params: MailPrepareMoveInput) -> str:
    """Prepare moving an email to another mailbox. Returns a confirmation token; does not move."""
    dest_lower = params.destinationMailboxPath.lower()
    if dest_lower in ("trash", "deleted items"):
        return (
            "Error: Moving to Trash is not allowed. Use 'All Mail' (Gmail) or "
            "'Archive' (Exchange/Outlook) to archive messages."
        )

    payload = {
        "kind": "mail_move",
        "accountName": params.accountName,
        "mailboxPath": params.mailboxPath,
        "index": params.index,
        "destinationMailboxPath": params.destinationMailboxPath,
    }
    token = _confirmation_tokens.put(payload)

    return json.dumps(
        {
            "confirmationToken": token,
            "preview": {
                "accountName": params.accountName,
                "mailboxPath": params.mailboxPath,
                "index": params.index,
                "destinationMailboxPath": params.destinationMailboxPath,
            },
            "note": "Call mail_move_message with confirmationToken to move.",
        },
        indent=2,
    )


@mcp.tool(
    name="mail_move_message",
    annotations={
        "title": "Move Prepared Message",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": False,
    },
)
async def mail_move_message(params: ConfirmationTokenInput) -> str:
    """Move a previously prepared email using its confirmation token."""
    payload = _confirmation_tokens.take(params.confirmationToken)
    if payload is None:
        return "Error: Invalid or expired confirmationToken"
    if payload.get("kind") != "mail_move":
        return "Error: confirmationToken is not valid for mail_move_message"

    result = await _run_osascript(
        AS_MAIL_MOVE_MESSAGE,
        [
            payload["accountName"],
            payload["mailboxPath"],
            str(payload["index"]),
            payload["destinationMailboxPath"],
        ],
    )
    return json.dumps({"status": result}, indent=2)


# ---------------------------------------------------------------------------
# Calendar tools
# ---------------------------------------------------------------------------


@mcp.tool(
    name="calendar_list_calendars",
    annotations={
        "title": "List Calendars",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def calendar_list_calendars() -> str:
    """List Apple Calendar calendars by name."""
    return await _run_osascript(AS_CALENDAR_LIST_CALENDARS)


@mcp.tool(
    name="calendar_list_calendars_detailed",
    annotations={
        "title": "List Calendars (Detailed)",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def calendar_list_calendars_detailed() -> str:
    """List Apple Calendar calendars with index + source account (helps disambiguate duplicates)."""
    return await _run_osascript(AS_CALENDAR_LIST_CALENDARS_DETAILED)


@mcp.tool(
    name="calendar_list_events",
    annotations={
        "title": "List Calendar Events",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def calendar_list_events(params: CalendarListEventsInput) -> str:
    """List events in a calendar between startISO and endISO."""
    calendar_index = params.calendarIndex or 0
    calendar_name = params.calendarName or ""
    limit = params.limit or 25
    start = _parse_iso_to_parts(params.startISO)
    end = _parse_iso_to_parts(params.endISO)

    raw = await _run_osascript(
        AS_CALENDAR_LIST_EVENTS,
        [
            str(calendar_index),
            calendar_name,
            str(start["year"]),
            str(start["month"]),
            str(start["day"]),
            str(start["hour"]),
            str(start["minute"]),
            str(start["second"]),
            str(end["year"]),
            str(end["month"]),
            str(end["day"]),
            str(end["hour"]),
            str(end["minute"]),
            str(end["second"]),
            str(limit),
        ],
    )

    try:
        candidates = json.loads(raw)
    except json.JSONDecodeError:
        return "Error: Calendar returned non-JSON output"

    window_start = datetime(
        start["year"],
        start["month"],
        start["day"],
        start["hour"],
        start["minute"],
        start["second"],
    )
    window_end = datetime(
        end["year"],
        end["month"],
        end["day"],
        end["hour"],
        end["minute"],
        end["second"],
    )

    items: list[dict[str, Any]] = []
    for c in candidates:
        title = c.get("title", "")
        location = c.get("location", "")
        recurrence = (c.get("recurrence") or "").strip()

        if not c.get("start") or not c.get("end"):
            continue
        master_start = _date_from_parts(c["start"])
        master_end = _date_from_parts(c["end"])

        if not recurrence:
            if window_start <= master_start < window_end:
                items.append(
                    {
                        "title": title,
                        "location": location,
                        "start": master_start,
                        "end": master_end,
                    }
                )
            continue

        rrule = _parse_rrule(recurrence)
        occs = _expand_occurrences(
            master_start, master_end, rrule, window_start, window_end
        )
        for o in occs:
            items.append(
                {
                    "title": title,
                    "location": location,
                    "start": o["start"],
                    "end": o["end"],
                }
            )

    items.sort(key=lambda e: e["start"])
    sliced = items[:limit]
    out = [
        {
            "index": idx + 1,
            "title": e["title"],
            "startISO": e["start"].isoformat(),
            "endISO": e["end"].isoformat(),
            "location": e["location"],
        }
        for idx, e in enumerate(sliced)
    ]

    return json.dumps(out, indent=2)


@mcp.tool(
    name="calendar_prepare_create_event",
    annotations={
        "title": "Prepare Create Event",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def calendar_prepare_create_event(
    params: CalendarPrepareCreateEventInput,
) -> str:
    """Prepare creating an Apple Calendar event. Returns a confirmation token; does not create."""
    calendar_name = params.calendarName or ""
    location = params.location or ""
    notes = params.notes or ""
    start = _parse_iso_to_parts(params.startISO)
    end = _parse_iso_to_parts(params.endISO)

    payload = {
        "kind": "calendar_create_event",
        "calendarName": calendar_name,
        "title": params.title,
        "location": location,
        "notes": notes,
        "start": start,
        "end": end,
    }
    token = _confirmation_tokens.put(payload)

    notes_preview = notes
    if len(notes_preview) > 300:
        notes_preview = notes_preview[:300] + "..."

    return json.dumps(
        {
            "confirmationToken": token,
            "preview": {
                "calendarName": calendar_name or "(default)",
                "title": params.title,
                "location": location,
                "notesPreview": notes_preview,
                "startISO": params.startISO,
                "endISO": params.endISO,
            },
            "note": "Call calendar_create_event with confirmationToken to create.",
        },
        indent=2,
    )


@mcp.tool(
    name="calendar_create_event",
    annotations={
        "title": "Create Prepared Event",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": False,
    },
)
async def calendar_create_event(params: ConfirmationTokenInput) -> str:
    """Create a previously prepared Apple Calendar event using its confirmation token."""
    payload = _confirmation_tokens.take(params.confirmationToken)
    if payload is None:
        return "Error: Invalid or expired confirmationToken"
    if payload.get("kind") != "calendar_create_event":
        return "Error: confirmationToken is not valid for calendar_create_event"

    start = payload["start"]
    end = payload["end"]
    result = await _run_osascript(
        AS_CALENDAR_CREATE_EVENT,
        [
            payload["calendarName"],
            payload["title"],
            payload["location"],
            payload["notes"],
            str(start["year"]),
            str(start["month"]),
            str(start["day"]),
            str(start["hour"]),
            str(start["minute"]),
            str(start["second"]),
            str(end["year"]),
            str(end["month"]),
            str(end["day"]),
            str(end["hour"]),
            str(end["minute"]),
            str(end["second"]),
        ],
    )
    return json.dumps({"status": result}, indent=2)


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    mcp.run()
