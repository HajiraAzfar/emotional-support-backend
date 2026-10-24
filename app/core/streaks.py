"""
The heatmap and streak every journal shares (the Saving Journal first, the
Thought Journal next): a day's points, its level, and the streak with one
grace day per rolling week. Pure functions of dates, so each journal decides
only what earns a point.
"""
from datetime import date, timedelta


def level(points: int) -> int:
    """0 none, 1 one point, 2 two, 3 three or more."""
    return min(points, 3)


def streaks(active: set[date], today: date) -> tuple[set[date], int, int]:
    """
    (grace days, current streak, best streak). A day with any point counts;
    the streak is the number of such days in a row. One missed day between two
    counted ones is bridged automatically, at most once in any seven days, and
    shown as a grace day. Today is never a miss while it lasts, and a missed
    yesterday stays bridged until today is over, so the streak does not drop
    before she has had the chance to keep it.
    """
    grace: set[date] = set()
    run = best = 0
    last_grace = None
    day = min(active) if active else today
    one = timedelta(days=1)
    while day <= today:
        if day in active:
            run += 1
        elif day == today:
            pass
        elif (day - one in active and (day + one in active or day + one == today)
              and (last_grace is None or (day - last_grace).days >= 7)):
            grace.add(day)
            last_grace = day
        else:
            run = 0
        best = max(best, run)
        day += one
    return grace, run, best


def heatmap(points: dict[date, int], start: date, end: date, today: date) -> dict:
    """[{date, points, level, grace_day}] from start to end, with the streak beside it."""
    grace, current, best = streaks(set(points), today)
    days = []
    day = start
    while day <= end:
        days.append({"date": day.isoformat(), "points": points.get(day, 0), "level": level(points.get(day, 0)),
                     "grace_day": day in grace})
        day += timedelta(days=1)
    return {"days": days, "streak": {"current": current, "best": best, "total_days": len(points),
                                     "today_counted": today in points}}
