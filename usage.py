"""Usage log for Chatbox: one line per reply in usage.jsonl, plus summaries for the Usage screen.

Only what Chatbox itself sends is counted. Subscription limits are not visible to the
command-line tools, so they can't be shown here.
"""
import datetime
import json
import os
import threading
import time

BACKENDS = ["claude", "cursor", "codex", "chatgpt", "ollama"]  # fixed order = chart order


class Log:
    def __init__(self, data_dir):
        self.path = os.path.join(data_dir, "usage.jsonl")
        self.dir = data_dir
        self.lock = threading.Lock()

    def record(self, backend, mode, model, ok, seconds, usage):
        u = usage or {}
        entry = {"ts": round(time.time()), "backend": backend, "mode": mode,
                 "model": u.get("model") or model or "Auto", "ok": bool(ok),
                 "seconds": round(seconds, 1),
                 "input": int(u.get("input", 0)) + int(u.get("cache_read", 0)) + int(u.get("cache_write", 0)),
                 "output": int(u.get("output", 0)) + int(u.get("reasoning", 0))}
        if isinstance(u.get("cost"), (int, float)):
            entry["cost"] = round(u["cost"], 6)
        with self.lock:
            os.makedirs(self.dir, exist_ok=True)
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry) + "\n")
        return entry

    def clear(self):
        with self.lock:
            try:
                os.remove(self.path)
            except FileNotFoundError:
                pass

    def _entries(self, since):
        try:
            with open(self.path, encoding="utf-8") as f:
                for line in f:
                    try:
                        e = json.loads(line)
                    except ValueError:
                        continue
                    if e.get("ts", 0) >= since and e.get("backend") in BACKENDS:
                        yield e
        except OSError:
            return

    def summary(self, days=30):
        """Totals per assistant, per model, per mode, and a per-day series."""
        today = datetime.date.today()
        entries = list(self._entries(0 if not days else
                                     time.mktime((today - datetime.timedelta(days=days - 1)).timetuple())))
        blank = lambda: {"messages": 0, "errors": 0, "input": 0, "output": 0, "cost": 0.0,
                         "seconds": 0.0, "has_cost": False}
        totals = {b: blank() for b in BACKENDS}
        models, modes, per_day = {}, {"ask": 0, "plan": 0, "agent": 0}, {}
        for e in entries:
            b = e["backend"]
            for bucket in (totals[b], models.setdefault((b, e.get("model") or "Auto"), blank())):
                bucket["messages"] += 1
                bucket["errors"] += 0 if e.get("ok", True) else 1
                bucket["input"] += e.get("input", 0)
                bucket["output"] += e.get("output", 0)
                bucket["seconds"] += e.get("seconds", 0)
                if "cost" in e:
                    bucket["cost"] += e["cost"]
                    bucket["has_cost"] = True
            if e.get("mode") in modes:
                modes[e["mode"]] += 1
            day = datetime.date.fromtimestamp(e["ts"]).isoformat()
            d = per_day.setdefault(day, {b: {"messages": 0, "tokens": 0} for b in BACKENDS})
            d[b]["messages"] += 1
            d[b]["tokens"] += e.get("input", 0) + e.get("output", 0)
        # every day in the range, including quiet ones
        if days:
            start = today - datetime.timedelta(days=days - 1)
        else:
            start = min((datetime.date.fromisoformat(d) for d in per_day), default=today)
        series, d = [], start
        while d <= today:
            k = d.isoformat()
            series.append({"date": k, **per_day.get(k, {b: {"messages": 0, "tokens": 0} for b in BACKENDS})})
            d += datetime.timedelta(days=1)
        model_rows = sorted(({"backend": b, "model": m, **v} for (b, m), v in models.items()),
                            key=lambda r: (-r["messages"], r["backend"]))
        return {"days": days, "totals": totals, "models": model_rows, "modes": modes,
                "series": series, "count": len(entries)}
