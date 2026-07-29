"""Publishing calendar and social performance tracking.

The calendar is the operating rhythm of an organic-traffic business: reach is
bought with consistency, and a week with no posts costs more than a week with
mediocre ones. So this module schedules, tracks what was actually posted, and
measures how each post did.

The important discipline here is about **posting times**. Every guide on the
internet publishes a "best time to post on TikTok" table, and all of them are
someone else's audience in someone else's timezone. This module will not ship
one. `recommend_posting_times` reads *this account's own* published history and
says one of three things: here is what has worked for you, here is what has
worked but on too little data to trust, or we do not know yet — post on a
consistent schedule and we will know in a few weeks. The third answer is the
honest one for a new account, and it is the one a borrowed table hides.

The second discipline is about **engagement rate denominators**. A video with
1,000 views and 100 likes is not "10% engagement" in any sense comparable to a
video with 1,000,000 views and 100,000 likes — the distribution mechanics
differ by an order of magnitude. Rates here are always reported next to their
denominator, and a video below the view floor is excluded from averages rather
than allowed to swing them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any

# Below this many views a video has not been distributed enough for its rates
# to mean anything. Including a 300-view post in an engagement average lets a
# video nobody saw dominate the number.
MIN_VIEWS_FOR_RATES = 1000

# A post needs time before it can be judged. TikTok distributes in waves and a
# video can double its views on day three, so an early reading is a different
# measurement, not a worse one.
JUDGEMENT_HOURS = 48

# Below this many posts in a slot, a "best time" is noise. Deliberately strict:
# there are 7*24 possible slots and a handful of posts will always produce an
# apparent winner.
MIN_POSTS_PER_SLOT = 3
MIN_POSTS_FOR_TIMING = 15

WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday",
            "Saturday", "Sunday")


@dataclass
class ScheduledPost:
    scheduled_for: str          # ISO datetime
    weekday: str
    slot: str
    package_id: str
    sku: str
    angle: str
    objective: str
    ready: bool
    blockers: list[str] = field(default_factory=list)


@dataclass
class PublishingPlan:
    starts: str
    ends: str
    posts: list[ScheduledPost]
    cadence_per_day: float
    warnings: list[str] = field(default_factory=list)

    @property
    def shootable(self) -> list[ScheduledPost]:
        return [p for p in self.posts if p.ready]

    def summary(self) -> dict[str, Any]:
        return {
            "scheduled": len(self.posts),
            "ready_to_publish": len(self.shootable),
            "blocked": len(self.posts) - len(self.shootable),
            "cadence_per_day": self.cadence_per_day,
            "window": f"{self.starts} to {self.ends}",
        }


@dataclass
class VideoPerformance:
    package_id: str
    sku: str
    angle: str
    hook_archetype: str
    published_at: str
    hours_since_post: float
    views: int
    engagement_rate_pct: float | None
    completion_pct: float | None
    click_rate_pct: float | None
    share_rate_pct: float | None
    mature: bool
    rateable: bool
    note: str = ""


def _rate(numerator: float | None, denominator: float) -> float | None:
    """None when there is no denominator — never zero.

    Zero means measured and bad. A click rate of zero on a video with no view
    data is not a bad video, it is no data, and averaging the two together is
    how a channel gets abandoned for the wrong reason.
    """
    if numerator is None or denominator <= 0:
        return None
    return round(numerator / denominator * 100, 3)


# ---------------------------------------------------------------------------
# Scheduling
# ---------------------------------------------------------------------------
def build_publishing_plan(packages: list[Any], *, days: int = 14,
                          posts_per_day: int = 2,
                          slots: list[str] | None = None,
                          start: date | None = None,
                          recommended_slots: list[str] | None = None
                          ) -> PublishingPlan:
    """Schedule production packages across a window.

    Blocked packages are still scheduled, marked not ready and carrying their
    blockers. Dropping them would silently shrink the calendar and hide the
    fact that the pipeline is short — which is the thing a content operation
    most needs to see coming.
    """
    start = start or date.today()
    slots = slots or recommended_slots or ["11:00", "19:00", "15:00"]
    slots = slots[:max(posts_per_day, 1)]
    objectives = ["Test hook retention", "Test angle conversion",
                  "Re-cut of the current best performer", "Widen the audience"]

    posts: list[ScheduledPost] = []
    warnings: list[str] = []
    if not packages:
        return PublishingPlan(start.isoformat(),
                              (start + timedelta(days=days - 1)).isoformat(),
                              [], 0.0,
                              ["No production packages supplied — there is "
                               "nothing to schedule. An organic channel with a "
                               "gap in the calendar loses distribution faster "
                               "than it regains it."])

    index = 0
    for offset in range(days):
        day = start + timedelta(days=offset)
        for slot in slots:
            package = packages[index % len(packages)]
            blockers = list(getattr(package, "blockers", []) or [])
            posts.append(ScheduledPost(
                scheduled_for=f"{day.isoformat()}T{slot}:00",
                weekday=WEEKDAYS[day.weekday()],
                slot=slot,
                package_id=getattr(package, "package_id", f"PKG-{index}"),
                sku=getattr(package, "sku", ""),
                angle=getattr(package, "angle", ""),
                objective=objectives[index % len(objectives)],
                ready=bool(getattr(package, "ready", False)),
                blockers=blockers,
            ))
            index += 1

    needed = days * len(slots)
    if len(packages) < needed:
        reuse = needed / len(packages)
        warnings.append(
            f"{len(packages)} package(s) scheduled across {needed} slots — each "
            f"is posted about {reuse:.1f} times. Re-posting the same cut trains "
            "the algorithm on an audience that has already seen it; generate "
            "more angles before extending the window.")
    blocked = [p for p in posts if not p.ready]
    if blocked:
        distinct = len({p.package_id for p in blocked})
        warnings.append(
            f"{distinct} distinct package(s) are scheduled but not shootable. "
            "They occupy calendar slots that will go empty unless their "
            "blockers are cleared first.")

    return PublishingPlan(
        starts=start.isoformat(),
        ends=(start + timedelta(days=days - 1)).isoformat(),
        posts=posts,
        cadence_per_day=round(len(slots), 2),
        warnings=warnings,
    )


# ---------------------------------------------------------------------------
# Performance
# ---------------------------------------------------------------------------
def summarise_video(row: dict[str, Any]) -> VideoPerformance:
    """One video's rates, with the denominators that make them meaningful."""
    views = int(row.get("views") or 0)
    rateable = views >= MIN_VIEWS_FOR_RATES
    hours = float(row.get("hours_since_post") or 0.0)
    mature = hours >= JUDGEMENT_HOURS

    engagement = None
    if rateable:
        interactions = sum(int(row.get(k) or 0)
                           for k in ("likes", "comments", "shares", "saves"))
        engagement = _rate(interactions, views)

    notes = []
    if not rateable:
        notes.append(
            f"{views:,} views is below the {MIN_VIEWS_FOR_RATES:,} floor — rates "
            "are omitted rather than computed, because a handful of interactions "
            "on a video nobody saw produces a percentage that means nothing.")
    if not mature:
        notes.append(
            f"Measured {hours:.0f}h after posting. TikTok distributes in waves "
            f"and a video can double by day three; treat as provisional until "
            f"{JUDGEMENT_HOURS}h.")

    return VideoPerformance(
        package_id=str(row.get("package_id", "")),
        sku=str(row.get("sku", "")),
        angle=str(row.get("angle", "")),
        hook_archetype=str(row.get("hook_archetype", "")),
        published_at=str(row.get("published_at", "")),
        hours_since_post=hours,
        views=views,
        engagement_rate_pct=engagement,
        completion_pct=(float(row["avg_watch_pct"])
                        if row.get("avg_watch_pct") is not None else None),
        click_rate_pct=_rate(row.get("link_clicks"), views) if rateable else None,
        share_rate_pct=_rate(row.get("shares"), views) if rateable else None,
        mature=mature,
        rateable=rateable,
        note=" ".join(notes),
    )


def channel_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Account-level view: volume, reach, and what is actually measurable."""
    videos = [summarise_video(r) for r in rows]
    judgeable = [v for v in videos if v.rateable and v.mature]

    total_views = sum(v.views for v in videos)
    engagements = [v.engagement_rate_pct for v in judgeable
                   if v.engagement_rate_pct is not None]
    clicks = [v.click_rate_pct for v in judgeable if v.click_rate_pct is not None]
    completions = [v.completion_pct for v in judgeable if v.completion_pct is not None]

    def mean(values: list[float]) -> float | None:
        return round(sum(values) / len(values), 3) if values else None

    return {
        "videos_published": len(videos),
        "videos_judgeable": len(judgeable),
        "videos_too_fresh": sum(1 for v in videos if not v.mature),
        "videos_below_view_floor": sum(1 for v in videos if not v.rateable),
        "total_views": total_views,
        "median_views": _median([v.views for v in videos]),
        "mean_engagement_rate_pct": mean(engagements),
        "mean_click_rate_pct": mean(clicks),
        "mean_completion_pct": mean(completions),
        "note": (
            f"Averages are computed over {len(judgeable)} of {len(videos)} videos "
            f"— those with at least {MIN_VIEWS_FOR_RATES:,} views and at least "
            f"{JUDGEMENT_HOURS}h since posting. The rest are counted, not averaged."
            if videos else
            "No published videos recorded yet. Log posts with `publish-log` so "
            "performance can be attributed to creative choices."
        ),
    }


def _median(values: list[int]) -> float | None:
    """Median rather than mean for views.

    View counts on organic short-form are heavily skewed — one video that
    breaks out drags the mean above every other post, so a mean describes a
    channel that does not exist.
    """
    if not values:
        return None
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return float(ordered[mid])
    return round((ordered[mid - 1] + ordered[mid]) / 2, 1)


def follower_growth(readings: list[dict[str, Any]]) -> dict[str, Any]:
    """Growth rate from dated follower readings.

    Takes readings rather than computing from videos, because follower count is
    an account-level number typed in from the app — there is no organic API for
    it. Two readings are the minimum and are reported as such.
    """
    dated = sorted(
        (r for r in readings if r.get("observed_at") and r.get("followers") is not None),
        key=lambda r: r["observed_at"])
    if len(dated) < 2:
        return {
            "readings": len(dated),
            "growth_per_day": None,
            "note": ("At least two dated follower readings are needed to compute "
                     "growth. One reading is a level, not a rate."),
        }
    first, last = dated[0], dated[-1]
    start = datetime.fromisoformat(str(first["observed_at"]).replace("Z", "+00:00"))
    end = datetime.fromisoformat(str(last["observed_at"]).replace("Z", "+00:00"))
    days = max((end - start).total_seconds() / 86400.0, 1e-9)
    delta = int(last["followers"]) - int(first["followers"])
    return {
        "readings": len(dated),
        "window_days": round(days, 1),
        "followers_start": int(first["followers"]),
        "followers_now": int(last["followers"]),
        "net_change": delta,
        "growth_per_day": round(delta / days, 2),
        "note": ("Followers are a lagging indicator on a commerce account — "
                 "click rate and conversion move first. Track it, do not steer "
                 "by it."),
    }


# ---------------------------------------------------------------------------
# Posting times
# ---------------------------------------------------------------------------
def recommend_posting_times(rows: list[dict[str, Any]], *,
                            top_n: int = 3) -> dict[str, Any]:
    """Recommend posting slots from this account's own history — or refuse to.

    Deliberately has no built-in "best times" table. Every published one is a
    different audience in a different timezone, and following it produces a
    schedule optimised for somebody else's followers. When there is not enough
    of our own data, the answer is "post consistently and ask again", which is
    both true and actionable.
    """
    judgeable = [r for r in rows
                 if float(r.get("hours_since_post") or 0) >= JUDGEMENT_HOURS
                 and int(r.get("views") or 0) > 0]

    if len(judgeable) < MIN_POSTS_FOR_TIMING:
        return {
            "confident": False,
            "recommendations": [],
            "posts_analysed": len(judgeable),
            "posts_needed": MIN_POSTS_FOR_TIMING,
            "note": (
                f"Only {len(judgeable)} mature post(s) with view data — not "
                f"enough to distinguish a good slot from a good video. This "
                "system deliberately ships no default 'best times to post' "
                "table: every published one is someone else's audience in "
                "someone else's timezone. Post on a consistent schedule, log "
                f"each post, and ask again at {MIN_POSTS_FOR_TIMING} posts."),
        }

    buckets: dict[tuple[int, int], list[int]] = {}
    for row in judgeable:
        key = (int(row.get("weekday") or 0), int(row.get("hour") or 0))
        buckets.setdefault(key, []).append(int(row.get("views") or 0))

    overall = _median([int(r.get("views") or 0) for r in judgeable]) or 0.0
    scored = []
    for (weekday, hour), views in buckets.items():
        if len(views) < MIN_POSTS_PER_SLOT:
            continue
        median = _median(views) or 0.0
        scored.append({
            "weekday": WEEKDAYS[weekday % 7],
            "hour": f"{hour:02d}:00",
            "posts": len(views),
            "median_views": median,
            "vs_account_median_pct": (round((median / overall - 1) * 100, 1)
                                      if overall else None),
        })

    scored.sort(key=lambda s: s["median_views"], reverse=True)
    if not scored:
        return {
            "confident": False,
            "recommendations": [],
            "posts_analysed": len(judgeable),
            "note": (
                f"{len(judgeable)} mature posts, but no single slot has the "
                f"{MIN_POSTS_PER_SLOT} posts needed to be more than noise. There "
                "are 168 possible slots; a handful of posts spread across them "
                "will always produce an apparent winner. Concentrate posting on "
                "fewer slots to learn faster."),
        }

    return {
        "confident": True,
        "recommendations": scored[:top_n],
        "posts_analysed": len(judgeable),
        "account_median_views": overall,
        "note": (
            "Derived from this account's own posts only. Slots with fewer than "
            f"{MIN_POSTS_PER_SLOT} posts are excluded. Median rather than mean, "
            "because one breakout video would otherwise elect its own slot."),
    }


def cadence_report(rows: list[dict[str, Any]], *, days: int = 28,
                   today: date | None = None) -> dict[str, Any]:
    """Posting frequency over a trailing window, and the gaps in it.

    Gaps matter more than the average. An account that posts fourteen times in
    two days and nothing for a fortnight has the same weekly average as one
    posting daily, and materially worse distribution.
    """
    today = today or datetime.now(timezone.utc).date()
    cutoff = today - timedelta(days=days - 1)
    by_day: dict[str, int] = {}
    for row in rows:
        raw = str(row.get("published_at", ""))
        if not raw:
            continue
        try:
            when = datetime.fromisoformat(raw.replace("Z", "+00:00")).date()
        except ValueError:
            continue
        if when < cutoff or when > today:
            continue
        by_day[when.isoformat()] = by_day.get(when.isoformat(), 0) + 1

    posted = len(by_day)
    total = sum(by_day.values())
    longest_gap, current_gap = 0, 0
    for offset in range(days):
        day = (cutoff + timedelta(days=offset)).isoformat()
        if by_day.get(day):
            current_gap = 0
        else:
            current_gap += 1
            longest_gap = max(longest_gap, current_gap)

    warnings = []
    if longest_gap >= 4:
        warnings.append(
            f"Longest silence in the window was {longest_gap} days. Organic "
            "distribution decays through a gap and takes longer to recover than "
            "the gap itself — consistency outperforms volume here.")
    if total and posted and total / posted >= 4:
        warnings.append(
            f"{total} posts across only {posted} active day(s). Batching posts "
            "into a single day makes them compete with each other for the same "
            "audience rather than reaching new ones.")

    return {
        "window_days": days,
        "posts": total,
        "active_days": posted,
        "posts_per_active_day": round(total / posted, 2) if posted else 0.0,
        "posts_per_day": round(total / days, 2),
        "longest_gap_days": longest_gap,
        "warnings": warnings,
    }
