#!/usr/bin/env python3
r"""Kiro CLI credit audit - read-only, stdlib only, works on existing session files.

Ground truth: ~/.kiro/sessions/cli/<session_id>.json
  session_state.conversation_metadata.user_turn_metadatas[].metering_usage[{value,unit:"credit"}]

Does NOT touch data.sqlite3 (that file also holds credentials). Never writes anything.

Platform notes
  Linux/macOS : ~/.kiro/sessions/cli/
  Windows     : %USERPROFILE%\.kiro\sessions\cli\      (invoke as `python`, not `python3`)
  WSL         : If kiro-cli runs INSIDE WSL, session files live in the WSL home.
                /mnt/c/Users/*/.kiro/sessions/cli is probed as well, so a native-Windows
                install on the same machine is merged into the same report.
                Override everything with KIRO_SESSIONS_DIR.

Requires Python 3.7 or newer.

Usage
  python3 kiro_credit_audit.py                          # everything on disk
  python3 kiro_credit_audit.py --since 2026-09-01T00:00:00Z --tz-offset 8
  python3 kiro_credit_audit.py --tools        # + per-tool attribution (parses .jsonl, slower)
"""
import sys

if sys.version_info < (3, 7):
    sys.exit("error: Python 3.7+ required (datetime.fromisoformat); found %s"
             % sys.version.split()[0])

import argparse
import base64
import calendar
import glob
import hashlib
import json
import math
import os
import re
import statistics
from collections import defaultdict
from datetime import datetime, timezone, timedelta

# Windows zh-CN defaults stdout to cp936 and dies on the CJK text below.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# Per-turn model -> rate multiplier. Display only; credits always come from metering_usage.
# Do NOT join the session-level rate_multiplier onto every turn: users switch models
# mid-session and every turn would inherit the session's FINAL selection.
MULT = {"auto": 1.0, "claude-haiku-4.5": 0.4, "claude-sonnet-4": 1.3,
        "claude-sonnet-4.5": 1.3, "claude-sonnet-4.6": 1.3, "claude-opus-4.5": 2.2,
        "claude-opus-4.6": 2.2, "claude-opus-4.7": 2.2, "claude-opus-4.8": 2.2,
        "claude-opus-5": 2.2, "claude-fable-5": 4.4, "gpt-5.6-sol": 2.4,
        "gpt-5.6-terra": 1.0, "gpt-5.6-luna": 0.1, "glm-5": 0.5,
        "deepseek-3.2": 0.25, "minimax-m2.5": 0.25, "minimax-m2.1": 0.15,
        "qwen3-coder-next": 0.05}

MIN_COV = 0.5

_TS = re.compile(r"^(?P<head>\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2})?)?)"
                 r"(?:\.(?P<frac>\d+))?(?P<tz>Z|[+-]\d{2}:?\d{2})?$")


def num(x, default=0):
    """x if it is a finite int/float (not bool), else default."""
    if isinstance(x, bool) or not isinstance(x, (int, float)):
        return default
    try:
        if not math.isfinite(x):
            return default
    except OverflowError:  # int too large for float
        return default
    return x


def pct(a, b):
    return 100.0 * a / b if b else 0.0


def parse_ts(s):
    """ISO 8601 -> aware datetime (UTC if no offset). Handles Z, +HH:MM, +HHMM,
    any number of fractional digits, negative offsets, and date-only input."""
    if not isinstance(s, str):
        return None
    m = _TS.match(s.strip())
    if not m:
        return None
    head, frac, tz = m.group("head"), m.group("frac") or "", m.group("tz") or ""
    if frac and len(head) != 19:  # fractions are only valid after HH:MM:SS
        return None
    if len(head) == 10:
        head += "T00:00:00"
    if tz == "Z":
        tz = "+00:00"
    elif tz and ":" not in tz:
        tz = tz[:3] + ":" + tz[3:]
    s = head + (("." + frac[:6].ljust(6, "0")) if frac else "") + tz
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    try:
        return dt.astimezone(timezone.utc)
    except OverflowError:  # year 1 / 9999 with a non-zero offset
        return dt


def session_roots():
    """Every plausible session dir: $HOME, %USERPROFILE%, ~, plus /mnt/c/Users/* under WSL.
    Roots are de-duplicated by real path so HOME and USERPROFILE pointing at the same
    directory are not scanned twice."""
    roots, seen = [], set()

    def add(p):
        if not p or not os.path.isdir(p):
            return
        k = os.path.normcase(os.path.realpath(p))
        if k not in seen:
            seen.add(k)
            roots.append(p)

    if os.environ.get("KIRO_SESSIONS_DIR"):
        add(os.environ["KIRO_SESSIONS_DIR"])
        return roots
    for h in (os.environ.get("HOME"), os.environ.get("USERPROFILE"), os.path.expanduser("~")):
        if h:
            add(os.path.join(h, ".kiro", "sessions", "cli"))
    if os.path.isdir("/mnt/c/Users"):
        for d in sorted(glob.glob("/mnt/c/Users/*/.kiro/sessions/cli")):
            parts = d.split("/")
            user = parts[4] if len(parts) > 4 else ""
            if user not in ("Public", "Default", "Default User", "All Users"):
                add(d)
    return roots


def parse_turn(t, f, stem, i, sess_model, sess_rate, tz_off):
    """One user_turn_metadatas entry -> row dict, or None if it is not a dict."""
    if not isinstance(t, dict):
        return None
    raw_mids = t.get("message_ids")
    raw_mids = raw_mids if isinstance(raw_mids, list) else []
    mids = [str(x) for x in raw_mids
            if isinstance(x, (str, int)) and not isinstance(x, bool) and x != ""]
    # Dedupe key = session file name + turn index + message ids. Only the same session
    # file copied into several roots (WSL home + /mnt/c) counts as a duplicate; two turns
    # of one session that happen to share all message ids are both kept.
    key = (stem, i, tuple(sorted(set(mids))))
    ts = parse_ts(t.get("end_timestamp"))
    mu = t.get("metering_usage")
    mu = mu if isinstance(mu, list) else []
    # float() keeps sums on the float path: huge ints would otherwise overflow later formatting
    entries = [float(u["value"]) for u in mu
               if isinstance(u, dict) and u.get("unit") == "credit"
               and num(u.get("value"), None) is not None]
    m = t.get("model")
    inferred = False
    if not (isinstance(m, str) and m):
        m, inferred = (sess_model, True) if sess_model else ("unknown", False)
    mult = MULT.get(m)
    if mult is None and sess_rate is not None and m == sess_model:
        mult = sess_rate
    td = t.get("turn_duration")
    dur = (num(td.get("secs")) + num(td.get("nanos")) / 1e9) if isinstance(td, dict) else num(td)
    try:
        loc = (ts + timedelta(hours=tz_off)) if ts else None
    except OverflowError:
        loc = None
    return dict(
        key=key, ts=ts, model=m, inferred=inferred, mult=mult, cr=sum(entries),
        ctx=num(t.get("context_usage_percentage"), None),
        cyc=int(min(num(t.get("number_of_cycles")), 10**12)),
        req=int(min(num(t.get("total_request_count")), 10**12)),
        dur=dur, entries=entries, mids=mids,
        utc=ts.strftime("%Y-%m-%d") if ts else "?",
        local=loc.strftime("%Y-%m-%d") if loc else "?",
        hour=loc.strftime("%m-%d %Hh") if loc else "?")


def load(since=None, tz_off=8.0):
    seen, rows = set(), []
    st = dict(files=0, sessions=0, dupes=0, skipped=0, denied=0, bad_turns=0, no_ts=0,
              turn_stems=set())
    files, seen_real = [], set()
    for r in session_roots():
        for f in sorted(glob.glob(os.path.join(glob.escape(r), "*.json"))):
            rp = os.path.realpath(f)
            if rp not in seen_real:
                seen_real.add(rp)
                files.append(f)
    for f in files:
        st["files"] += 1
        try:
            with open(f, "r", encoding="utf-8-sig", errors="replace") as fh:
                d = json.load(fh)
        except PermissionError:
            st["denied"] += 1
            continue
        except Exception:
            st["skipped"] += 1
            continue
        if not isinstance(d, dict):
            st["skipped"] += 1
            continue
        cwd = d.get("cwd")
        cwd = cwd if isinstance(cwd, str) and cwd else "?"
        proj = cwd.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1] or cwd
        ss = d.get("session_state")
        ss = ss if isinstance(ss, dict) else {}
        cm = ss.get("conversation_metadata")
        cm = cm if isinstance(cm, dict) else {}
        rms = ss.get("rts_model_state")
        mi = rms.get("model_info") if isinstance(rms, dict) else None
        mi = mi if isinstance(mi, dict) else {}
        sess_model = mi.get("model_id") if isinstance(mi.get("model_id"), str) else None
        sess_rate = num(mi.get("rate_multiplier"), None)
        turns = cm.get("user_turn_metadatas")
        turns = turns if isinstance(turns, list) else []
        stem = os.path.basename(f)[:-5]
        if turns and stem not in st["turn_stems"]:
            st["sessions"] += 1
            st["turn_stems"].add(stem)
        for i, t in enumerate(turns):
            try:
                row = parse_turn(t, f, stem, i, sess_model, sess_rate, tz_off)
            except Exception:
                row = None
            if row is None:
                st["bad_turns"] += 1
                continue
            if row["key"] in seen:
                st["dupes"] += 1
                continue
            seen.add(row["key"])
            if row["ts"] is None:
                st["no_ts"] += 1  # counted in totals, but cannot be placed on any date
                if since:
                    continue
            elif since and row["ts"] < since:
                continue
            row["sid"], row["stem"], row["proj"] = stem[:8], stem, proj
            rows.append(row)
    return rows, st


def _contents(content):
    """Yield content items, descending one level into toolResult payloads
    (images returned by tools live in toolResult.data.content[])."""
    if not isinstance(content, list):
        return
    for c in content:
        if not isinstance(c, dict):
            continue
        yield c
        cd = c.get("data")
        if c.get("kind") == "toolResult" and isinstance(cd, dict):
            inner = cd.get("content")
            for cc in (inner if isinstance(inner, list) else []):
                if isinstance(cc, dict):
                    yield cc


def _image_bytes(payload):
    try:
        if isinstance(payload, list):
            return bytes(payload)
        if isinstance(payload, str) and payload:
            try:
                return base64.b64decode(payload, validate=True)
            except (ValueError, TypeError):
                return None  # URL / path / data-URL: not raw image bytes
    except (TypeError, ValueError):
        pass
    return None


def tool_index(roots, stems_with_turns):
    """message_id -> [tool names] from the .jsonl transcripts beside each session file.
    Also counts transcripts that contain assistant messages but whose .json has no
    turn metadata (their credits are invisible to this script)."""
    idx, imgs, image_ids, nfile, orphans = {}, [], set(), 0, 0
    seen_stem = set()  # same transcript copied into several roots is read once
    for r in roots:
        for f in sorted(glob.glob(os.path.join(glob.escape(r), "*.jsonl"))):
            stem = os.path.basename(f)[:-6]
            if stem in seen_stem:
                continue
            has_assistant = False
            try:
                with open(f, "r", encoding="utf-8-sig", errors="replace") as fh:
                    seen_stem.add(stem)
                    nfile += 1
                    for ln in fh:
                        if '"kind": "AssistantMessage"' in ln or '"kind":"AssistantMessage"' in ln:
                            has_assistant = True
                        if '"toolUse"' not in ln and '"image"' not in ln:
                            continue
                        try:
                            rec = json.loads(ln)
                        except Exception:
                            continue
                        d = rec.get("data") if isinstance(rec, dict) else None
                        if not isinstance(d, dict):
                            continue
                        mid = d.get("message_id")
                        mid = str(mid) if isinstance(mid, (str, int)) and not isinstance(mid, bool) else None
                        for c in _contents(d.get("content")):
                            cd = c.get("data") if isinstance(c.get("data"), dict) else {}
                            if c.get("kind") == "toolUse" and isinstance(cd.get("name"), str) and mid:
                                idx.setdefault(mid, []).append(cd["name"])
                            elif c.get("kind") == "image":
                                src = cd.get("source") if isinstance(cd.get("source"), dict) else {}
                                raw = _image_bytes(src.get("data"))
                                if raw is not None:
                                    imgs.append(len(raw))
                                    image_ids.add(hashlib.sha256(raw).digest())
            except OSError:
                # A readable copy with the same stem under another root may follow.
                has_assistant = False
            if has_assistant and stem not in stems_with_turns:
                orphans += 1
    return idx, imgs, len(image_ids), nfile, orphans


def report_images(imgs, unique_imgs):
    if not imgs:
        return
    imgs = sorted(imgs)
    print(f"\n  [图片] 转写记录中出现 {len(imgs)} 个 image 项（含工具返回的图片，不含压缩快照中的历史副本），"
          f"涉及 {unique_imgs} 张不同图片；"
          f"原始字节中位 {imgs[len(imgs)//2]/1024:.0f} KB，最大 {imgs[-1]/1024:.0f} KB。")
    print("  解读：这里统计的是转写中的原始图像字节；同一图片可能随历史上下文重复出现。")
    print("        API 传输采用 base64 时体积约增加 1/3，此处统计的是解码后的原始字节。")


def report_tools(rows, tot, roots, turn_stems, since=None):
    idx, imgs, unique_imgs, nfile, orphans = tool_index(roots, turn_stems)
    # A message_id can appear in two adjacent turns; credit its tool calls once, to the
    # earliest turn, so nothing is double counted.
    floor = datetime.min.replace(tzinfo=timezone.utc)
    claimed, hit = set(), []
    # turns without a timestamp go last so they cannot steal a shared message id
    for r in sorted(rows, key=lambda x: (x["ts"] is None, x["ts"] or floor)):
        names = []
        for m in r["mids"]:
            if m in claimed:
                continue
            claimed.add(m)
            names += idx.get(m, [])
        if names:
            hit.append((r, names))
    total_calls = sum(len(v) for v in idx.values())
    matched_calls = sum(len(n) for _, n in hit)
    print("\n--- 工具调用归因（估算） ---")
    if orphans:
        print(f"  注意：另有 {orphans} 个会话的 .jsonl 含模型回复，但对应 .json 缺失或没有轮次元数据；"
              f"这些会话自身的轮次额度未计入本报表，是本机结果低于 /usage 的一个可能原因。")
    if not hit:
        if total_calls:
            why = "（--since 过滤后常见）" if since else "（对应会话的 .json 可能缺失或没有轮次元数据）"
            print(f"  已扫描 {nfile} 个 .jsonl，含 {total_calls} 次 toolUse，"
                  f"但当前统计范围内的轮次都没有关联到工具调用{why}。")
        else:
            print(f"  已扫描 {nfile} 个 .jsonl，未找到任何工具调用记录；"
                  "对应 .jsonl 可能缺失、为空或已被截断。")
        report_images(imgs, unique_imgs)
        return
    covcr = sum(r["cr"] for r, _ in hit)
    print(f"  关联覆盖：{len(hit)}/{len(rows)} 轮，{covcr:.2f}/{tot:.2f} credits"
          f"（{pct(covcr, tot):.1f}%）；已扫描 {nfile} 个 .jsonl，"
          f"toolUse {matched_calls}/{total_calls} 次已归到具体轮次"
          f"（分母为全部转写中的 toolUse，不受 --since 限制）。")
    print("  口径：将一轮的 credits 平均分配给该轮所有 toolUse；用于识别高频/高额度工具，")
    print("        不代表某个工具本身的真实边际价格，也不证明因果关系。")
    g = defaultdict(lambda: [0, 0.0])
    for r, names in hit:
        share = r["cr"] / len(names)
        for t in names:
            g[t][0] += 1
            g[t][1] += share
    print(f"  {'tool':26s} {'calls':>6s} {'credits':>10s} {'cr/call':>10s} {'share':>8s}")
    for t, (n, c) in sorted(g.items(), key=lambda x: -x[1][1])[:18]:
        print(f"  {t[:26]:26s} {n:6d} {c:10.2f} {c/n:10.3f} {pct(c, covcr):7.1f}%")
    print(f"  合计：{sum(n for n, _ in g.values())} 次调用，涉及 {len(g)} 个工具。"
          f"（credits=归因额度，cr/call=每次调用平均归因额度，share=占已覆盖额度比例）")
    report_images(imgs, unique_imgs)


def grp(title, keyf, rows, chrono=False, top=40):
    print(f"\n--- {title} ---")
    g = defaultdict(lambda: {"n": 0, "cr": 0.0, "cyc": 0, "nw": 0, "crw": 0.0})
    for r in rows:
        e = g[keyf(r)]
        e["n"] += 1
        e["cr"] += r["cr"]
        if r["cyc"] > 0:
            e["cyc"] += r["cyc"]
            e["nw"] += 1
            e["crw"] += r["cr"]
    items = sorted(g.items()) if chrono else sorted(g.items(), key=lambda x: -x[1]["cr"])
    print(f"  {'key':24s} {'turns':>6s} {'credits':>10s} {'cr/turn':>9s} "
          f"{'cr/cycle':>10s} {'cyc/turn':>9s} {'cyc cov':>9s}")
    starred = False
    for k, e in items[:top]:
        k = str(k)
        if len(k) > 24:
            k = k[:11] + ".." + k[-11:]
        cov = e["nw"] / e["n"] if e["n"] else 0
        if e["cyc"]:
            low = cov < MIN_COV
            starred = starred or low
            crc = f"{e['crw']/e['cyc']:10.3f}" + ("*" if low else "")
            cyt = f"{e['cyc']/e['nw']:9.2f}"
        else:
            crc, cyt = "       n/a", "      n/a"
        print(f"  {k:24s} {e['n']:6d} {e['cr']:10.2f} {e['cr']/e['n']:9.3f} "
              f"{crc:>10s} {cyt} {e['nw']:4d}/{e['n']:<4d}")
    if starred:
        print(f"  * cycle coverage < {MIN_COV:.0%}：该行 cr/cycle 样本覆盖不足，不宜横向比较。")


def main():
    p = argparse.ArgumentParser(
        prog="kiro_credit_audit.py",  # keep a readable name when run as `curl ... | python3 -`
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--since", metavar="TIMESTAMP",
                   help="only count turns that ended at or after this time "
                        "(ISO 8601, e.g. 2026-09-01 or 2026-09-01T00:00:00Z; UTC if no offset)")
    p.add_argument("--tz-offset", metavar="HOURS", type=float, default=8.0,
                   help="local UTC offset in hours, may be fractional (default: 8)")
    p.add_argument("--tools", action="store_true",
                   help="also attribute credits to tool calls (parses .jsonl, slower)")
    a = p.parse_args()

    since = None
    if a.since:
        since = parse_ts(a.since)
        if not since:
            p.error(f"--since {a.since!r} is not a parsable timestamp "
                    f"(want e.g. 2026-09-01 or 2026-09-01T00:00:00Z)")
    tz = a.tz_offset + 0.0  # normalises -0.0 to 0.0
    if not -12 <= tz <= 14:
        p.error(f"--tz-offset {tz:g} out of range (-12..14)")
    tzlabel = f"UTC{tz:+g}"

    roots = session_roots()
    rows, st = load(since, tz)
    print("=== Kiro CLI 额度审计 ===")
    print("数据目录（仅本机 CLI；跨主机需分别运行后汇总）:", *(roots or ["(无)"]), sep="\n  ")
    if not rows:
        print("\nno sidecar turns found（未找到可审计的 CLI 会话记录）。")
        env_dir = os.environ.get("KIRO_SESSIONS_DIR")
        if env_dir and not os.path.isdir(env_dir):
            print(f"KIRO_SESSIONS_DIR={env_dir!r} 不是目录，已忽略；请检查路径是否拼错。")
        if st["skipped"]:
            print(f"有 {st['skipped']} 个文件无法解析为 JSON 对象（可能写入中途被截断）。")
        if st["denied"]:
            print(f"有 {st['denied']} 个文件无读取权限。")
        if st["bad_turns"]:
            print(f"有 {st['bad_turns']} 条轮次记录结构异常，已跳过。")
        if st["no_ts"]:
            print(f"有 {st['no_ts']} 轮没有时间戳，因 --since 无法判断时间而被跳过。")
        if since:
            print(f"注意：已按 --since {a.since} 过滤，可能是该时间之后没有用量。")
        print("Windows 用户请确认 kiro-cli 是否运行在 WSL 中；若是，请进入 WSL 后重跑脚本。")
        return

    tot = sum(r["cr"] for r in rows)
    print("\n--- 汇总 ---")
    warn = "".join(f"  ! {k}={st[k]}" for k in ("skipped", "denied", "bad_turns", "no_ts") if st[k])
    print(f"credits={tot:.2f}  turns={len(rows)}  sessions={st['sessions']}/{st['files']}  "
          f"deduped={st['dupes']}  tz={tzlabel}{warn}")
    print("说明：sessions=有轮次记录的会话数/会话文件总数（子代理会话通常没有轮次，不受 --since 影响）；"
          "deduped=同一会话文件在多个目录重复出现时被排除的轮次数。")
    if st["skipped"]:
        print(f"注意：有 {st['skipped']} 个文件无法解析，以下总额可能偏低；其余文件仍已正常统计。")
    if st["denied"]:
        print(f"注意：有 {st['denied']} 个文件无读取权限（通常是其它 Windows 用户的目录），已跳过。")
    if st["bad_turns"]:
        print(f"注意：有 {st['bad_turns']} 条轮次记录结构异常，已跳过。")
    if st["no_ts"]:
        print(f"注意：有 {st['no_ts']} 轮没有时间戳，" + ("因 --since 无法判断时间而被跳过。" if since else
              "已计入总额，在按日期的表中归入 ? 行，不计入统计范围、活跃日均和当前计费周期。"))
    if tot <= 0:
        print("\n本机记录中的 credits 计量总额为 0，跳过额度分析。")
        return

    by_day = defaultdict(float)
    for r in rows:
        by_day[r["utc"]] += r["cr"]
    active = [d for d, c in by_day.items() if c > 0 and d != "?"]  # "?" = no timestamp
    span = [r["ts"] for r in rows if r["ts"]]
    if span:
        lo, hi = min(span), max(span)
        print(f"统计范围：{lo:%Y-%m-%d %H:%MZ} → {hi:%Y-%m-%d %H:%MZ}"
              f"（跨度 {(hi-lo).total_seconds()/86400:.1f} 天，其中 {len(active)} 天有用量）")
    if active:
        dated = sum(by_day[d] for d in active)
        print(f"活跃日均：{dated/len(active):.0f} credits/活跃日（只对有用量的日期取平均）")

    now = datetime.now(timezone.utc)
    per_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    # --since later than the month start truncates this section; say so instead of
    # pretending the number covers the whole billing month.
    limited = since is not None and since > per_start
    start = since if limited else per_start
    per = sum(r["cr"] for r in rows if r["ts"] and r["ts"] >= start)
    pdays = len({r["utc"] for r in rows if r["ts"] and r["ts"] >= start})
    el = max((now - start).total_seconds() / 86400, 1e-9)
    mlen = calendar.monthrange(now.year, now.month)[1]
    local_boundary = per_start + timedelta(hours=tz)
    print("\n--- 当前计费周期（用于和 /usage 对账） ---")
    if limited:
        print(f"自 --since {start:%Y-%m-%d %H:%M} UTC 起：{per:.2f} credits"
              f"（已过 {el:.2f} 天，其中 {pdays} 天有用量）。")
        print(f"注意：--since 晚于本计费月起点 {per_start:%Y-%m-%d} 00:00 UTC，"
              "此数字只是本月的一部分，不能与 /usage 直接对账；去掉 --since 可查看整月。")
    else:
        print(f"当前计费周期：{per:.2f} credits（自 {per_start:%Y-%m-%d} 00:00 UTC 起；"
              f"已过 {el:.2f} 天，其中 {pdays} 天有用量）")
        projection = per / el * mlen
        if el < 5:
            print(f"按当前速度估算整月约 {projection:.0f} credits"
                  f"（本月 {mlen} 天；样本不足 5 天，波动很大，请勿作为预测结论）。")
        else:
            print(f"按当前速度估算整月约 {projection:.0f} credits（本月 {mlen} 天；仅作趋势参考）。")
    print(f"计费边界：每月 1 日 00:00 UTC（{tzlabel} 为 {local_boundary:%m-%d %H:%M}）。")
    print("对账提示：")
    print("  1. 本脚本只统计本机 CLI 会话记录；同一账号在多台主机使用时，请分别运行后汇总。")
    print("  2. 本机结果低于 /usage，常见原因是其它主机或 headless/CI 记录未保留在本机，")
    print("     或会话文件缺失、轮次元数据未落盘（--tools 会报告这类会话的数量）。")
    print(f"  3. 本机结果高于 /usage，可检查面板更新延迟及 deduped={st['dupes']} 是否提示重复记录。")
    print(f"  4. 本地时间（{tzlabel}）{local_boundary:%m-%d %H:%M} 起的用量才计入新的 UTC 计费月。")

    print("\n字段说明：cr/turn=每轮平均额度；cr/cycle=有工具循环的轮次的每循环平均额度；")
    print("          cyc/turn=有工具循环的轮次的每轮平均循环数；cyc cov=有工具循环的轮次占比。")
    grp("按 UTC 日期（与 /usage 对账）", lambda r: r["utc"], rows, chrono=True)
    grp(f"按本地日期（工作日视角，{tzlabel}）", lambda r: r["local"], rows, chrono=True)
    grp("按模型（倍率为静态参考值）",
        lambda r: f"{r['model']} x{r['mult']}" if r["mult"] is not None else f"{r['model']} x?",
        rows)
    grp("按会话（额度最高优先）", lambda r: r["sid"], rows, top=15)
    grp("按项目目录（额度最高优先）", lambda r: r["proj"], rows, top=15)

    W = [r for r in rows if r["cyc"] > 0 and r["ctx"] is not None]
    if W:
        print(f"\n--- 上下文水位与每循环额度（n={len(W)}） ---")
        b = defaultdict(list)
        for r in W:
            b[min(max(int(r["ctx"] // 10) * 10, 0), 90)].append(r)
        print(f"  {'ctx':>9s} {'turns':>6s} {'med cr/cyc':>11s} {'pooled':>9s} {'cyc/turn':>9s}")
        for k in sorted(b):
            v = b[k]
            cyc = sum(x["cyc"] for x in v)
            per_cyc = [x["cr"] / x["cyc"] for x in v]
            lbl = f"{k:3d}-{k+9:3d}%" if k < 90 else " 90-100%"
            print(f"  {lbl} {len(v):6d} {statistics.median(per_cyc):11.3f} "
                  f"{sum(x['cr'] for x in v)/cyc:9.3f} {cyc/len(v):9.2f}")
        print("  解读：ctx=该轮开始时的上下文占用；优先比较中位数 med cr/cyc；")
        print("        pooled=该区间总额度/总循环数，会受循环深度差异影响。")
        print("        此表用于观察相关性，不宜单独据此判断因果；样本少或模型构成不同会使曲线波动。")

    Wc = [r for r in rows if r["cyc"] > 0]
    if Wc:
        print(f"\n--- 循环深度与额度占比（有工具循环的轮次 {len(Wc)}/{len(rows)}） ---")
        z = [r for r in rows if r["cyc"] == 0 and r["cr"] > 0]
        if z:
            zc = sum(r["cr"] for r in z)
            z0 = len(rows) - len(Wc)
            print(f"  口径说明：另有 {z0} 轮没有工具循环（cycle=0，没有工具调用），"
                  f"其中 {len(z)} 轮产生 {zc:.2f} credits（占总额 {pct(zc, tot):.1f}%）"
                  + (f"，{z0 - len(z)} 轮计量额度为 0；" if z0 > len(z) else "；")
                  + "它们计入总额和 cr/turn，不计入 cr/cycle。")
        h = defaultdict(lambda: [0, 0.0])
        lbl = {1: "1", 5: "2-5", 10: "6-10", 20: "11-20", 99: "21+"}
        for r in Wc:
            k = 1 if r["cyc"] <= 1 else 5 if r["cyc"] <= 5 else 10 if r["cyc"] <= 10 \
                else 20 if r["cyc"] <= 20 else 99
            h[k][0] += 1
            h[k][1] += r["cr"]
        t2 = sum(v[1] for v in h.values())
        for k in sorted(h):
            n, c = h[k]
            print(f"  cycles {lbl[k]:>6s}  turns={n:4d} ({pct(n, len(Wc)):4.1f}%)  "
                  f"credits={c:9.2f} ({pct(c, t2):4.1f}%)")

    D = [r for r in rows if r["dur"] > 0]
    if D:
        print(f"\n--- 轮次耗时与额度（n={len(D)}） ---")
        print("  提示：耗时是任务复杂度和循环深度的代理指标，不表示系统按时间计费。")
        for lo, hi, lb in ((0, 60, "<1min"), (60, 300, "1-5min"), (300, 900, "5-15min"),
                           (900, float("inf"), ">15min")):
            v = [r for r in D if lo <= r["dur"] < hi]
            if v:
                c = sum(x["cr"] for x in v)
                print(f"  {lb:>8s}  turns={len(v):4d}  credits={c:9.2f} "
                      f"({pct(c, tot):4.1f}%)  cr/turn={c/len(v):7.2f}")

    E = sorted(v for r in rows for v in r["entries"])
    if E:
        n = len(E)

        def q(f):  # nearest-rank percentile
            return E[min(max(math.ceil(n * f) - 1, 0), n - 1)]

        k = max(1, round(n * 0.01))
        top = sum(E[-k:])
        print(f"\n--- 模型请求额度分布（n={n}） ---")
        print("  口径：每条 metering_usage 对应一次已计费的模型请求；被取消或失败的请求没有计量条目。")
        print(f"  p50={q(.50):.4f}  p90={q(.90):.4f}  p99={q(.99):.4f}  max={E[-1]:.4f}  "
              f"mean={sum(E)/n:.4f}")
        print(f"  额度最高的 {k} 条请求（约占 {pct(k, n):.1f}%）占总额 {pct(top, sum(E)):.1f}%"
              f"（直接读取计量记录，不依赖 cr/cycle 推算）")

    if a.tools:
        report_tools(rows, tot, roots, st["turn_stems"], since)

    print("\n--- 额度最高的 10 轮 ---")
    for r in sorted(rows, key=lambda x: -x["cr"])[:10]:
        flag = " (model inferred)" if r["inferred"] else ""
        ctx = f"{r['ctx']:.1f}" if r["ctx"] is not None else "n/a"
        print(f"  {r['hour']} {r['sid']} {r['model'][:16]:16s} cr={r['cr']:8.3f} "
              f"cyc={r['cyc']:3d} req={r['req']:3d} ctx={ctx}{flag}")


if __name__ == "__main__":
    main()
