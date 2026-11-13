"""Aggregation: turn cached documents into the Report shape the renderer eats.

Pure functions, no IO. Every number a report prints is computed here, so the
sleep-type filter and the unit discipline live in one place.
"""
from __future__ import annotations

import statistics
from datetime import date, datetime, timedelta
from typing import Dict, List, Optional, Tuple

# Sleep types that must never reach a statistic:
#   deleted = the user removed this sleep in the app
#   rest    = falsely detected, rejected by the user
IGNORED_SLEEP_TYPES = ("deleted", "rest")
MAIN_SLEEP_TYPE = "long_sleep"
NAP_SLEEP_TYPES = ("sleep", "late_nap")

# domain key -> label used in Report["missing_domains"]
DAILY_DOMAINS = {
    "daily_sleep": "sleep",
    "daily_readiness": "readiness",
    "daily_activity": "activity",
    "daily_spo2": "spo2",
    "daily_stress": "stress",
    "daily_resilience": "resilience",
    "daily_cardiovascular_age": "cardio_age",
    "vO2_max": "vo2max",
    "sleep_time": "sleep_time",
    "sleep": "sleep_detail",
}


def _docs(docs: Dict[str, List[dict]], key: str) -> List[dict]:
    return list(docs.get(key) or [])


def _one(docs: Dict[str, List[dict]], key: str, day: date) -> Optional[dict]:
    for doc in _docs(docs, key):
        if doc.get("day") == str(day):
            return doc
    return None


def _local_tz():
    return datetime.now().astimezone().tzinfo


def _parse_iso(text: object) -> Optional[datetime]:
    """Parse an API timestamp defensively across Python versions.

    Python 3.9's ``fromisoformat`` only accepts 3 or 6 fractional digits and
    rejects ``Z``; 3.11+ accepts any count. Normalise instead of requiring a
    recent interpreter.
    """
    if not text:
        return None
    raw = str(text).strip().replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        pass
    # Normalise the fractional part to 6 digits and retry once.
    import re

    match = re.match(r"^(.*?\.)(\d+)(.*)$", raw)
    if not match:
        return None
    head, digits, tail = match.groups()
    padded = (digits + "000000")[:6]
    try:
        return datetime.fromisoformat(head + padded + tail)
    except ValueError:
        return None


def local_day_of(timestamp: str) -> Optional[date]:
    """Convert an API UTC timestamp to a date in the machine's local timezone."""
    moment = _parse_iso(timestamp)
    if moment is None:
        return None
    return moment.astimezone(_local_tz()).date()


def filter_sleep_docs(docs: List[dict]) -> Tuple[Optional[dict], List[dict]]:
    """Split sleep documents into (main sleep, naps).

    ``deleted`` and ``rest`` never enter either bucket. Naps are shown for
    information only: Oura already counts them in the daily score, so merging
    them into the main duration would double-count.
    """
    usable = [d for d in docs if d.get("type") not in IGNORED_SLEEP_TYPES]
    main = None
    naps: List[dict] = []
    for doc in usable:
        if doc.get("type") == MAIN_SLEEP_TYPE and main is None:
            main = doc
        elif doc.get("type") in NAP_SLEEP_TYPES:
            naps.append(doc)
    if main is None:
        # No long_sleep today: fall back to the longest usable document so a
        # day whose only sleep was a confirmed nap still shows something.
        candidates = [d for d in usable if d.get("type") in NAP_SLEEP_TYPES]
        if candidates:
            candidates.sort(key=lambda d: d.get("total_sleep_duration") or 0,
                            reverse=True)
            main = candidates[0]
            naps = [d for d in candidates[1:]]
    return main, naps


def _sleep_main(main: Optional[dict]) -> Optional[dict]:
    if not main:
        return None
    return {
        "total_sleep_duration": main.get("total_sleep_duration"),
        "time_in_bed": main.get("time_in_bed"),
        "efficiency": main.get("efficiency"),
        "deep": main.get("deep_sleep_duration"),
        "light": main.get("light_sleep_duration"),
        "rem": main.get("rem_sleep_duration"),
        "awake": main.get("awake_time"),
        "latency": main.get("latency"),
        "hrv": main.get("average_hrv"),
        "avg_hr": main.get("average_heart_rate"),
        "lowest_hr": main.get("lowest_heart_rate"),
        "breath": main.get("average_breath"),
        "bedtime_start": main.get("bedtime_start"),
        "bedtime_end": main.get("bedtime_end"),
        "phases": main.get("sleep_phase_5_min") or "",
    }


def _naps(naps: List[dict]) -> List[dict]:
    return [{"type": n.get("type"), "duration": n.get("total_sleep_duration") or 0}
            for n in naps]


def _scores(docs: Dict[str, List[dict]], day: date) -> dict:
    ds = _one(docs, "daily_sleep", day)
    dr = _one(docs, "daily_readiness", day)
    da = _one(docs, "daily_activity", day)
    return {
        "sleep": {
            "value": ds.get("score") if ds else None,
            "contributors": dict(ds.get("contributors") or {}) if ds else {},
        },
        "readiness": {
            "value": dr.get("score") if dr else None,
            "contributors": dict(dr.get("contributors") or {}) if dr else {},
            "temperature_deviation": dr.get("temperature_deviation") if dr else None,
        },
        "activity": {
            "value": da.get("score") if da else None,
            "steps": da.get("steps") if da else None,
            "active_calories": da.get("active_calories") if da else None,
            "contributors": dict(da.get("contributors") or {}) if da else {},
        },
    }


def _rest_mode(docs: Dict[str, List[dict]], day: date) -> List[dict]:
    hits = []
    for doc in _docs(docs, "rest_mode_period"):
        start = doc.get("start_day")
        end = doc.get("end_day") or start
        if not start:
            continue
        try:
            if date.fromisoformat(start) <= day <= date.fromisoformat(end):
                hits.append(doc)
        except ValueError:
            continue
    return hits


def _timeseries(docs: Dict[str, List[dict]], key: str, day: date,
                value_field: str) -> List[Tuple[str, float]]:
    rows = []
    for doc in _docs(docs, key):
        if local_day_of(doc.get("timestamp")) != day:
            continue
        value = doc.get(value_field)
        if value is None:
            continue
        rows.append((str(doc.get("timestamp")), float(value)))
    # Cache merging can append out-of-order rows when a window is backfilled;
    # an unsorted series draws a zig-zag line.
    rows.sort(key=lambda pair: pair[0])
    return rows


def _heartrate_block(rows: List[Tuple[str, float]]) -> dict:
    if not rows:
        return {"min": [], "avg": [], "max": [], "curve": []}
    values = [v for _ts, v in rows]
    curve = []
    for stamp, value in rows:
        moment = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
        curve.append((moment.astimezone(_local_tz()).strftime("%H:%M"), value))
    return {
        "min": [min(values)],
        "avg": [round(statistics.fmean(values), 1)],
        "max": [max(values)],
        "curve": curve,
    }


def _battery_block(rows: List[Tuple[str, float]]) -> dict:
    values = [v for _ts, v in rows]
    return {"level": values, "lowest": min(values) if values else None}


def _enhanced_tags(docs: Dict[str, List[dict]], day: date) -> List[dict]:
    """Enhanced tags span start_day..end_day, so they are not day-keyed."""
    hits = []
    for doc in _docs(docs, "enhanced_tag"):
        start = doc.get("start_day")
        if not start:
            continue
        end = doc.get("end_day") or start
        try:
            if date.fromisoformat(start) <= day <= date.fromisoformat(end):
                hits.append(doc)
        except ValueError:
            continue
    return hits


def _duration_between(start: object, end: object):
    a = _parse_iso(start)
    b = _parse_iso(end)
    if a is None or b is None:
        return None
    return (b - a).total_seconds()


def _events_summary(daily_reports: List[dict]) -> dict:
    """Period view of the event families: counts, totals, distributions."""
    workouts = [w for r in daily_reports for w in r["events"]["workout"]]
    sessions = [s for r in daily_reports for s in r["events"]["session"]]
    tags = [t for r in daily_reports for t in r["events"]["tag"]]
    enhanced = [t for r in daily_reports for t in r["events"]["enhanced_tag"]]

    intensities: Dict[str, int] = {}
    for workout in workouts:
        key = str(workout.get("intensity") or "unknown")
        intensities[key] = intensities.get(key, 0) + 1

    tag_counts: Dict[str, int] = {}
    for tag in tags:
        key = str(tag.get("text") or "")
        tag_counts[key] = tag_counts.get(key, 0) + 1
    top = sorted(tag_counts.items(), key=lambda pair: pair[1], reverse=True)[:5]

    return {
        "workout": {
            "count": len(workouts),
            "total_duration": int(sum(_duration_between(w.get("start_datetime"),
                                                        w.get("end_datetime")) or 0
                                      for w in workouts)),
            "total_calories": int(sum(w.get("calories") or 0 for w in workouts)),
            "intensities": intensities,
        },
        "session": {
            "count": len(sessions),
            "total_duration": int(sum(_duration_between(s.get("start_datetime"),
                                                        s.get("end_datetime")) or 0
                                      for s in sessions)),
        },
        "tag": {"count": len(tags), "top": [{"text": k, "n": v} for k, v in top]},
        "enhanced_tag": {"count": len(enhanced)},
    }


def _previous_scores(docs: Dict[str, List[dict]], day: date) -> dict:
    """Yesterday's headline scores, so a report can show a day-over-day delta."""
    previous_day = day - timedelta(days=1)
    scores = _scores(docs, previous_day)
    return {
        "date": str(previous_day),
        "scores": {
            "sleep": (scores.get("sleep") or {}).get("value"),
            "readiness": (scores.get("readiness") or {}).get("value"),
            "activity": (scores.get("activity") or {}).get("value"),
        },
    }


def _context(docs: Dict[str, List[dict]]) -> Optional[dict]:
    """Report header context. email and biological_sex are excluded as PII."""
    info = _docs(docs, "personal_info")
    if not info:
        return None
    source = info[0]
    context = {}
    for field in ("age", "height", "weight"):
        if source.get(field) is not None:
            context[field] = source[field]
    return context or None


def _device(docs: Dict[str, List[dict]]) -> Optional[dict]:
    rings = _docs(docs, "ring_configuration")
    if not rings:
        return None
    ring = rings[0]
    return {k: ring.get(k) for k in ("design", "color", "hardware_type", "size", "firmware_version")
            if ring.get(k) is not None}


def build_daily(docs: Dict[str, List[dict]], day: date) -> dict:
    """Aggregate one calendar day into a Report."""
    day_docs = {k: [d for d in v if d.get("day") == str(day)]
                for k, v in docs.items()}
    day_enhanced_tags = _enhanced_tags(docs, day)

    has_heartrate = bool(_timeseries(docs, "heartrate", day, "bpm"))
    has_battery = bool(_timeseries(docs, "ring_battery_level", day, "level"))
    has_any = any(day_docs.get(k) for k in
                  ("daily_sleep", "daily_readiness", "daily_activity", "sleep",
                   "daily_spo2", "daily_stress", "workout", "session", "tag")) \
        or has_heartrate or has_battery or bool(day_enhanced_tags)
    if not has_any:
        _empty = {
            "period": "daily", "start": str(day), "end": str(day),
            "is_partial": False, "elapsed_days": 1,
            "scores": {"sleep": {"value": None, "contributors": {}},
                       "readiness": {"value": None, "contributors": {},
                                     "temperature_deviation": None},
                       "activity": {"value": None, "steps": None,
                                    "active_calories": None, "contributors": {}}},
            "sleep_main": None, "naps": [],
            "spo2": None, "stress": None, "resilience": None,
            "cardio_age": None, "vo2max": None, "sleep_time": None,
            "rest_mode": [],
            "events": {"workout": [], "session": [], "tag": [],
                       "enhanced_tag": day_enhanced_tags},
            "timeseries": {"heartrate": {"min": [], "avg": [], "max": [], "curve": []},
                           "battery": {"level": [], "lowest": None}},
            "previous": _previous_scores(docs, day),
            "missing_days": [str(day)], "missing_domains": [],
            "context": _context(docs), "device": _device(docs),
        }
        return _empty

    _sp = _one(docs, "daily_spo2", day)
    _st = _one(docs, "daily_stress", day)
    _rs = _one(docs, "daily_resilience", day)
    _cv = _one(docs, "daily_cardiovascular_age", day)
    _vo = _one(docs, "vO2_max", day)
    _slt = _one(docs, "sleep_time", day)

    main, naps = filter_sleep_docs(day_docs.get("sleep") or [])

    missing_domains = []
    for key, label in DAILY_DOMAINS.items():
        if not day_docs.get(key):
            missing_domains.append(label)

    report = {
        "period": "daily",
        "start": str(day),
        "end": str(day),
        "is_partial": False,
        "elapsed_days": 1,
        "scores": _scores(docs, day),
        "sleep_main": _sleep_main(main),
        "naps": _naps(naps),
        "spo2": {"average": (_sp.get("spo2_percentage") or {}).get("average"),
                 "bdi": _sp.get("breathing_disturbance_index")} if _sp else None,
        "stress": {"stress_high": _st.get("stress_high"),
                   "recovery_high": _st.get("recovery_high"),
                   "day_summary": _st.get("day_summary")} if _st else None,
        "resilience": {"level": _rs.get("level"),
                       "contributors": _rs.get("contributors") or {}} if _rs else None,
        "cardio_age": {"vascular_age": _cv.get("vascular_age"),
                       "pulse_wave_velocity": _cv.get("pulse_wave_velocity")} if _cv else None,
        "vo2max": {"vo2_max": _vo.get("vo2_max")} if _vo else None,
        "sleep_time": {"status": _slt.get("status"),
                       "recommendation": _slt.get("recommendation")} if _slt else None,
        "rest_mode": _rest_mode(docs, day),
        "events": {
            "workout": day_docs.get("workout") or [],
            "session": day_docs.get("session") or [],
            "tag": day_docs.get("tag") or [],
            "enhanced_tag": day_enhanced_tags,
        },
        "timeseries": {
            "heartrate": _heartrate_block(_timeseries(docs, "heartrate", day, "bpm")),
            "battery": _battery_block(
                _timeseries(docs, "ring_battery_level", day, "level")),
        },
        "previous": _previous_scores(docs, day),
        "missing_days": [],
        "missing_domains": missing_domains,
        "context": _context(docs),
        "device": _device(docs),
    }
    return report

# ------------------------------------------------------------ period reports

import calendar  # noqa: E402 - kept next to its only user

SUMMARY_FIELDS = {
    "sleep_score": ("scores", "sleep", "value"),
    "readiness_score": ("scores", "readiness", "value"),
    "activity_score": ("scores", "activity", "value"),
    "efficiency": ("sleep_main", "efficiency"),
    "hrv": ("sleep_main", "hrv"),
    "resting_hr": ("sleep_main", "lowest_hr"),
    "breath": ("sleep_main", "breath"),
    # Steps are a count, not a score: averaging them is the meaningful period view.
    "steps_avg": ("scores", "activity", "steps"),
    "sleep_duration_avg": ("sleep_main", "total_sleep_duration"),
}

# Chinese labels for the summary table: the report is Chinese, and the internal
# snake_case names are an implementation detail.
SUMMARY_LABELS = {
    "sleep_score": "睡眠分数",
    "readiness_score": "准备度",
    "activity_score": "活动分数",
    "efficiency": "睡眠效率",
    "hrv": "平均 HRV",
    "resting_hr": "最低心率",
    "breath": "平均呼吸率",
    "steps_avg": "日均步数",
    "sleep_duration_avg": "平均睡眠时长",
}


def week_bounds(anchor: date) -> Tuple[date, date]:
    """ISO week: Monday through Sunday."""
    start = anchor - timedelta(days=anchor.weekday())
    return start, start + timedelta(days=6)


def month_bounds(anchor: date) -> Tuple[date, date]:
    last = calendar.monthrange(anchor.year, anchor.month)[1]
    return date(anchor.year, anchor.month, 1), date(anchor.year, anchor.month, last)


def _metric_value(report: dict, path: Tuple[str, ...]):
    node = report
    for step in path:
        if not isinstance(node, dict):
            return None
        node = node.get(step)
    return node


def _summarize(values: List[float]) -> dict:
    if not values:
        return {"mean": None, "min": None, "max": None, "std": None, "n": 0}
    return {
        "mean": round(statistics.fmean(values), 2),
        "min": min(values),
        "max": max(values),
        # Population standard deviation: the period's days are the whole
        # population under discussion, not a sample of it.
        "std": round(statistics.pstdev(values), 2),
        "n": len(values),
    }


def build_period(docs: Dict[str, List[dict]], start: date, end: date,
                 prev_docs: Optional[Dict[str, List[dict]]] = None,
                 today: Optional[date] = None) -> dict:
    """Aggregate a date range into a weekly/monthly Report."""
    today = today or date.today()
    days = [start + timedelta(days=i) for i in range((end - start).days + 1)]
    daily_reports = [build_daily(docs, d) for d in days]

    def _trend_row(r: dict) -> dict:
        main = r.get("sleep_main") or {}
        activity = r["scores"].get("activity") or {}
        return {
            "day": r["start"],
            "sleep": r["scores"]["sleep"]["value"],
            "readiness": r["scores"]["readiness"]["value"],
            "activity": activity.get("value"),
            "sleep_duration": main.get("total_sleep_duration"),
            "deep": main.get("deep"),
            "light": main.get("light"),
            "rem": main.get("rem"),
            "awake": main.get("awake"),
            "efficiency": main.get("efficiency"),
            "steps": activity.get("steps"),
            "active_calories": activity.get("active_calories"),
            "lowest_hr": main.get("lowest_hr"),
            "hrv": main.get("hrv"),
            "spo2": (r.get("spo2") or {}).get("average"),
            "vascular_age": (r.get("cardio_age") or {}).get("vascular_age"),
            "bedtime_start": main.get("bedtime_start"),
            "bedtime_end": main.get("bedtime_end"),
            "missing": bool(r.get("missing_days")),
        }

    trend = [_trend_row(r) for r in daily_reports]
    sleep_totals = [{"day": r["start"],
                     "total_sleep_duration": (r["sleep_main"] or {}).get("total_sleep_duration")}
                    for r in daily_reports]

    summaries = {}
    for name, path in SUMMARY_FIELDS.items():
        values = [_metric_value(r, path) for r in daily_reports]
        summaries[name] = _summarize([v for v in values if v is not None])

    scored = [(r["start"], r["scores"]["sleep"]["value"]) for r in daily_reports
              if r["scores"]["sleep"]["value"] is not None]
    best = max(scored, key=lambda pair: pair[1]) if scored else None
    worst = min(scored, key=lambda pair: pair[1]) if len(scored) > 1 else None

    elapsed = (min(today, end) - start).days + 1
    elapsed = max(1, min(elapsed, len(days)))
    is_partial = today < end

    comparison = None
    if prev_docs is not None:
        prev_end = start - timedelta(days=1)
        # Align to the START of the previous period, not its tail: comparing
        # Mon-Wed against last Fri-Sun would mix weekdays with weekends and
        # make every mid-week report look like a decline.
        if (end - start).days == 6:
            prev_start = start - timedelta(days=7)
        else:
            prev_start = month_bounds(prev_end)[0]
        prev_span = (prev_end - prev_start).days + 1
        prev_days = [prev_start + timedelta(days=i)
                     for i in range(min(elapsed, prev_span))]
        prev_reports = [build_daily(prev_docs, d) for d in prev_days]
        deltas = {}
        for name, path in SUMMARY_FIELDS.items():
            prev_values = [_metric_value(r, path) for r in prev_reports]
            prev_values = [v for v in prev_values if v is not None]
            current = summaries[name]["mean"]
            if prev_values and current is not None:
                deltas[name] = round(current - statistics.fmean(prev_values), 2)
        if is_partial:
            note = ("本期截至 %s（%d 天），与上期同天数对比" % (min(today, end), elapsed))
        else:
            note = "完整周期，与上一周期同口径对比"
        compared_end = prev_days[-1] if prev_days else prev_start
        comparison = {"prev_start": str(prev_start), "prev_end": str(compared_end),
                      "prev_days": len(prev_days), "deltas": deltas, "note": note}

    # Days that have not happened yet are not "missing": reporting tomorrow as
    # missing reads as data loss to anyone looking at a mid-week report.
    horizon = min(today, end)
    missing_days = [r["start"] for r in daily_reports
                    if r["missing_days"] and date.fromisoformat(r["start"]) <= horizon]
    pending_days = [r["start"] for r in daily_reports
                    if date.fromisoformat(r["start"]) > horizon]

    augmented = dict(daily_reports[0]) if daily_reports else {}
    # Score cards must describe the period, not its first day: a weekly report
    # whose Monday has no sleep score would otherwise show an empty card.
    base_scores = dict(augmented.get("scores") or {})
    for key, field in (("sleep", "sleep_score"), ("readiness", "readiness_score"),
                       ("activity", "activity_score")):
        block = dict(base_scores.get(key) or {})
        block["value"] = summaries[field]["mean"]
        base_scores[key] = block
    augmented["scores"] = base_scores
    augmented.update({
        "period": "weekly" if (end - start).days == 6 else "monthly",
        "start": str(start),
        "end": str(end),
        "is_partial": is_partial,
        "elapsed_days": elapsed,
        "trend": trend,
        "sleep_totals": sleep_totals,
        "summaries": summaries,
        "events_summary": _events_summary(daily_reports),
        "best_worst": {
            "best": {"day": best[0], "value": best[1]} if best else None,
            "worst": {"day": worst[0], "value": worst[1]} if worst else None,
        },
        "comparison": comparison,
        "missing_days": missing_days,
        "pending_days": pending_days,
        "days": [r["start"] for r in daily_reports],
    })
    return augmented
