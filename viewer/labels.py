"""The label shown for a drive: start date and time in local time, then its duration."""
import datetime

from django.utils import dateformat, timezone

LABEL_FORMAT = "M j, Y - g:i A"  # Oct 20, 2026 - 5:05 PM


def format_duration(seconds):
    """'45 sec', '10 min 26 sec', '1 hr 5 min 26 sec'; None when the duration is not known.

    The largest unit that is not zero comes first and every smaller unit follows (also when it
    is zero); larger units that would be zero are left out.
    """
    if seconds is None:
        return None
    total = round(seconds)
    if total < 1:
        return None
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours} hr {minutes} min {secs} sec"
    if minutes:
        return f"{minutes} min {secs} sec"
    return f"{secs} sec"


def drive_label(start_time, duration_s=None):
    """'Oct 20, 2026 - 5:05 PM (10 min 26 sec)'.

    start_time is what the sync wrote: a naive time in UTC. It is shown in the viewer's time zone
    (the TIME_ZONE setting). Without a known duration there are no brackets.
    """
    if timezone.is_naive(start_time):
        start_time = start_time.replace(tzinfo=datetime.timezone.utc)
    text = dateformat.format(timezone.localtime(start_time), LABEL_FORMAT)
    duration = format_duration(duration_s)
    return f"{text} ({duration})" if duration else text
