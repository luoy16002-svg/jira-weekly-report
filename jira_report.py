"""Weekly Jira developer report as a PDF with charts.

Pulls the board's current sprint, the issues completed in the reporting week and every worklog
logged that week, then renders a one-glance PDF: headline numbers, completed tasks per developer,
sprint progress and burndown, and hours logged per developer per day.

    python jira_report.py                    # last full week (Mon-Sun) from Jira, settings in .env
    python jira_report.py --week 2026-09-21  # a specific week (any date inside it)
    python jira_report.py --demo             # sample data, no Jira needed

Settings (.env or environment): JIRA_URL, JIRA_EMAIL, JIRA_API_TOKEN, JIRA_BOARD_ID,
optional JIRA_STORY_POINTS_FIELD (e.g. customfield_10016) and REPORT_TITLE.
"""
from __future__ import annotations

import argparse
import os
import random
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt  # noqa: E402
import requests  # noqa: E402
from dateutil import parser as dtparse  # noqa: E402
from reportlab.lib import colors  # noqa: E402
from reportlab.lib.pagesizes import A4  # noqa: E402
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet  # noqa: E402
from reportlab.lib.units import mm  # noqa: E402
from reportlab.platypus import Image, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle  # noqa: E402

INK, ACCENT, MUTED, GRID = '#1f2a37', '#2f6fdf', '#8a94a3', '#e6e9ee'
STATUS_COLORS = {'Done': '#2f9e6a', 'In Progress': '#2f6fdf', 'To Do': '#c9ced6'}


# ---------------------------------------------------------------- data model

@dataclass
class Issue:
    key: str
    summary: str
    assignee: str
    status_category: str          # To Do / In Progress / Done
    points: float
    resolved: datetime | None


@dataclass
class Worklog:
    issue: str
    author: str
    started: datetime
    hours: float


@dataclass
class WeekData:
    week_start: date
    sprint_name: str
    sprint_start: date
    sprint_end: date
    sprint_issues: list[Issue]
    completed_in_week: list[Issue]
    worklogs: list[Worklog] = field(default_factory=list)


# ---------------------------------------------------------------- Jira access

class Jira:
    def __init__(self, url: str, email: str, token: str, points_field: str | None):
        self.url, self.points_field = url.rstrip('/'), points_field
        self.s = requests.Session()
        self.s.auth = (email, token)
        self.s.headers['Accept'] = 'application/json'

    def get(self, path: str, **params) -> dict:
        r = self.s.get(self.url + path, params=params, timeout=30)
        r.raise_for_status()
        return r.json()

    def paged(self, path: str, key: str, **params):
        start = 0
        while True:
            page = self.get(path, startAt=start, maxResults=100, **params)
            items = page.get(key, [])
            yield from items
            start += len(items)
            if not items or page.get('isLast') or start >= page.get('total', start):
                return

    def _issue(self, raw: dict) -> Issue:
        f = raw['fields']
        pts = f.get(self.points_field) if self.points_field else None
        return Issue(raw['key'], f.get('summary', ''), (f.get('assignee') or {}).get('displayName', 'Unassigned'),
                     f['status']['statusCategory']['name'], float(pts or 0),
                     dtparse.isoparse(f['resolutiondate']) if f.get('resolutiondate') else None)

    def week(self, board_id: str, week_start: date) -> WeekData:
        fields = 'summary,assignee,status,resolutiondate' + (f',{self.points_field}' if self.points_field else '')
        sprints = list(self.paged(f'/rest/agile/1.0/board/{board_id}/sprint', 'values', state='active,closed'))
        sprint = next((s for s in reversed(sprints) if s.get('state') == 'active'), sprints[-1] if sprints else None)
        issues = [self._issue(i) for i in self.paged(f"/rest/agile/1.0/sprint/{sprint['id']}/issue", 'issues', fields=fields)] if sprint else []
        end = week_start + timedelta(days=7)
        jql = f'resolved >= "{week_start}" AND resolved < "{end}" AND statusCategory = Done'
        done = [self._issue(i) for i in self.paged(f'/rest/agile/1.0/board/{board_id}/issue', 'issues', jql=jql, fields=fields)]
        logs: list[Worklog] = []
        worked = self.paged(f'/rest/agile/1.0/board/{board_id}/issue', 'issues', fields='key',
                            jql=f'worklogDate >= "{week_start}" AND worklogDate < "{end}"')
        for raw in worked:
            for w in self.paged(f"/rest/api/3/issue/{raw['key']}/worklog", 'worklogs'):
                started = dtparse.isoparse(w['started'])
                if week_start <= started.date() < end:
                    logs.append(Worklog(raw['key'], w['author']['displayName'], started, w['timeSpentSeconds'] / 3600))
        return WeekData(week_start, sprint['name'] if sprint else 'No sprint',
                        dtparse.isoparse(sprint['startDate']).date() if sprint and sprint.get('startDate') else week_start,
                        dtparse.isoparse(sprint['endDate']).date() if sprint and sprint.get('endDate') else end,
                        issues, done, logs)


def demo_week(week_start: date) -> WeekData:
    """Realistic sample data so the report can be previewed without a Jira account."""
    rng = random.Random(7)
    devs = ['Amira Haddad', 'Lucas Moreau', 'Priya Nair', 'Tom Becker', 'Chen Wei']
    words = ['checkout', 'login', 'invoice export', 'search', 'profile page', 'webhooks', 'rate limiter', 'dashboard', 'push alerts', 'CSV import']
    verbs = ['Fix', 'Add', 'Refactor', 'Speed up', 'Test', 'Document']
    sprint_start = week_start - timedelta(days=4)
    issues = []
    for n in range(46):
        state = rng.choices(['Done', 'In Progress', 'To Do'], [0.55, 0.25, 0.2])[0]
        resolved = None
        if state == 'Done':
            resolved = datetime.combine(sprint_start + timedelta(days=rng.randint(0, 10)), datetime.min.time(), timezone.utc) + timedelta(hours=rng.randint(9, 18))
        issues.append(Issue(f'APP-{400 + n}', f'{rng.choice(verbs)} {rng.choice(words)}', rng.choice(devs), state, rng.choice([1, 2, 3, 5, 8]), resolved))
    done = [i for i in issues if i.resolved and week_start <= i.resolved.date() < week_start + timedelta(days=7)]
    logs = []
    for d in range(5):
        day = datetime.combine(week_start + timedelta(days=d), datetime.min.time(), timezone.utc)
        for dev in devs:
            for _ in range(rng.randint(1, 3)):
                logs.append(Worklog(rng.choice(issues).key, dev, day + timedelta(hours=9), round(rng.uniform(0.5, 3.5) * 4) / 4))
    return WeekData(week_start, 'Sprint 42', sprint_start, sprint_start + timedelta(days=13), issues, done, logs)


# ---------------------------------------------------------------- charts

def _style(ax, title: str) -> None:
    ax.set_title(title, loc='left', fontsize=11, color=INK, fontweight='bold', pad=10)
    for side in ('top', 'right'):
        ax.spines[side].set_visible(False)
    ax.spines['left'].set_color(GRID); ax.spines['bottom'].set_color(GRID)
    ax.tick_params(colors=MUTED, labelsize=8)
    ax.grid(axis='x' if ax.get_ylabel() == 'barh' else 'y', color=GRID, linewidth=0.8)
    ax.set_axisbelow(True)


def charts(data: WeekData, out: Path) -> dict[str, Path]:
    out.mkdir(parents=True, exist_ok=True)
    paths = {}

    per_dev = Counter(i.assignee for i in data.completed_in_week)
    fig, ax = plt.subplots(figsize=(6.2, 2.9), dpi=200)
    names = sorted(per_dev, key=per_dev.get)
    ax.barh(names, [per_dev[n] for n in names], color=ACCENT, height=0.55)
    for y, n in enumerate(names):
        ax.text(per_dev[n] + 0.1, y, str(per_dev[n]), va='center', fontsize=8, color=INK)
    ax.set_ylabel('barh'); _style(ax, 'Tasks completed this week'); ax.set_ylabel('')
    fig.tight_layout(); paths['done'] = out / 'done.png'; fig.savefig(paths['done']); plt.close(fig)

    status = Counter(i.status_category for i in data.sprint_issues)
    fig, ax = plt.subplots(figsize=(3.0, 2.9), dpi=200)
    labels = [s for s in ('Done', 'In Progress', 'To Do') if status[s]]
    ax.pie([status[s] for s in labels], colors=[STATUS_COLORS[s] for s in labels], startangle=90, counterclock=False,
           wedgeprops={'width': 0.36, 'edgecolor': 'white'})
    pct = 100 * status['Done'] / max(sum(status.values()), 1)
    ax.text(0, 0.06, f'{pct:.0f}%', ha='center', va='center', fontsize=16, fontweight='bold', color=INK)
    ax.text(0, -0.2, 'done', ha='center', va='center', fontsize=8, color=MUTED)
    ax.legend(labels, loc='lower center', bbox_to_anchor=(0.5, -0.2), ncol=3, frameon=False, fontsize=7)
    ax.set_title(f'{data.sprint_name} progress', loc='left', fontsize=11, color=INK, fontweight='bold')
    fig.tight_layout(); paths['sprint'] = out / 'sprint.png'; fig.savefig(paths['sprint']); plt.close(fig)

    total_pts = sum(i.points for i in data.sprint_issues)
    days = [(data.sprint_start + timedelta(days=d)) for d in range((data.sprint_end - data.sprint_start).days + 1)]
    remaining = []
    for d in days:
        burned = sum(i.points for i in data.sprint_issues if i.resolved and i.resolved.date() <= d)
        remaining.append(total_pts - burned if d <= data.week_start + timedelta(days=6) else None)
    fig, ax = plt.subplots(figsize=(6.2, 2.6), dpi=200)
    ax.plot(days, [total_pts * (1 - k / (len(days) - 1)) for k in range(len(days))], color=MUTED, linestyle='--', linewidth=1, label='Ideal')
    ax.plot([d for d, r in zip(days, remaining) if r is not None], [r for r in remaining if r is not None], color=ACCENT, linewidth=2, marker='o', markersize=3, label='Remaining')
    ax.axvspan(data.week_start, data.week_start + timedelta(days=6), color=ACCENT, alpha=0.06)
    ax.xaxis.set_major_formatter(matplotlib.dates.DateFormatter('%d %b'))
    ax.set_ylabel('story points', fontsize=8, color=MUTED); ax.legend(frameon=False, fontsize=7)
    _style(ax, 'Sprint burndown (reporting week shaded)')
    fig.tight_layout(); paths['burndown'] = out / 'burndown.png'; fig.savefig(paths['burndown']); plt.close(fig)

    hours = defaultdict(lambda: [0.0] * 7)
    for w in data.worklogs:
        hours[w.author][(w.started.date() - data.week_start).days] += w.hours
    fig, ax = plt.subplots(figsize=(6.2, 2.8), dpi=200)
    daynames = [(data.week_start + timedelta(days=d)).strftime('%a') for d in range(7)]
    bottom = [0.0] * 7
    palette = ['#2f6fdf', '#2f9e6a', '#e0a33a', '#8b5cf6', '#e0605b', '#14a3b8']
    for k, (dev, hs) in enumerate(sorted(hours.items())):
        ax.bar(daynames, hs, bottom=bottom, color=palette[k % len(palette)], width=0.6, label=dev)
        bottom = [b + h for b, h in zip(bottom, hs)]
    ax.legend(frameon=False, fontsize=7, ncol=3, loc='upper right')
    ax.set_ylabel('hours', fontsize=8, color=MUTED)
    _style(ax, 'Time logged per day')
    fig.tight_layout(); paths['hours'] = out / 'hours.png'; fig.savefig(paths['hours']); plt.close(fig)
    return paths


# ---------------------------------------------------------------- PDF

def build_pdf(data: WeekData, pdf: Path, title: str) -> None:
    imgs = charts(data, pdf.parent / f'{pdf.stem}_charts')
    ss = getSampleStyleSheet()
    h = ParagraphStyle('h', parent=ss['Title'], fontSize=20, textColor=colors.HexColor(INK), alignment=0, spaceAfter=2)
    sub = ParagraphStyle('s', parent=ss['Normal'], fontSize=9, textColor=colors.HexColor(MUTED))
    cell = ParagraphStyle('c', parent=ss['Normal'], fontSize=8, leading=10)
    week_end = data.week_start + timedelta(days=6)
    hours_total = sum(w.hours for w in data.worklogs)
    sprint_done = sum(1 for i in data.sprint_issues if i.status_category == 'Done')
    points_done = sum(i.points for i in data.completed_in_week)

    def tile(value: str, label: str) -> Table:
        t = Table([[Paragraph(f'<font size=18 color="{INK}"><b>{value}</b></font>', ss['Normal'])],
                   [Paragraph(f'<font size=8 color="{MUTED}">{label}</font>', ss['Normal'])]], colWidths=[42 * mm])
        t.setStyle(TableStyle([('BACKGROUND', (0, 0), (-1, -1), colors.HexColor('#f4f6f9')), ('LEFTPADDING', (0, 0), (-1, -1), 8),
                               ('TOPPADDING', (0, 0), (-1, 0), 8), ('BOTTOMPADDING', (0, -1), (-1, -1), 8)]))
        return t

    story = [
        Paragraph(title, h),
        Paragraph(f'Week of {data.week_start:%d %b %Y} to {week_end:%d %b %Y} · {data.sprint_name} '
                  f'({data.sprint_start:%d %b} to {data.sprint_end:%d %b}) · generated {datetime.now():%d %b %Y %H:%M}', sub),
        Spacer(1, 8 * mm),
        Table([[tile(str(len(data.completed_in_week)), 'tasks completed this week'), tile(f'{points_done:g}', 'story points delivered'),
                tile(f'{hours_total:.1f} h', 'time logged this week'), tile(f'{sprint_done}/{len(data.sprint_issues)}', 'sprint issues done')]],
              colWidths=[45 * mm] * 4, style=[('LEFTPADDING', (0, 0), (-1, -1), 0)]),
        Spacer(1, 6 * mm),
        Table([[Image(str(imgs['done']), 118 * mm, 55 * mm), Image(str(imgs['sprint']), 57 * mm, 55 * mm)]], colWidths=[120 * mm, 60 * mm]),
        Image(str(imgs['burndown']), 180 * mm, 75 * mm),
        Image(str(imgs['hours']), 180 * mm, 81 * mm),
    ]
    rows = [['Key', 'Completed task', 'Developer', 'Points', 'Resolved']]
    for i in sorted(data.completed_in_week, key=lambda i: i.resolved):
        rows.append([i.key, Paragraph(i.summary, cell), i.assignee, f'{i.points:g}', f'{i.resolved:%a %d %b}'])
    story += [Spacer(1, 4 * mm), Paragraph('<b>Completed this week</b>', ss['Heading3']),
              Table(rows, colWidths=[20 * mm, 75 * mm, 38 * mm, 15 * mm, 25 * mm], repeatRows=1, style=TableStyle([
                  ('FONTSIZE', (0, 0), (-1, -1), 8), ('TEXTCOLOR', (0, 0), (-1, 0), colors.HexColor(MUTED)),
                  ('LINEBELOW', (0, 0), (-1, 0), 0.6, colors.HexColor(GRID)), ('LINEBELOW', (0, 1), (-1, -1), 0.3, colors.HexColor(GRID)),
                  ('VALIGN', (0, 0), (-1, -1), 'MIDDLE')]))]
    SimpleDocTemplate(str(pdf), pagesize=A4, leftMargin=15 * mm, rightMargin=15 * mm, topMargin=14 * mm, bottomMargin=14 * mm,
                      title=title).build(story)


# ---------------------------------------------------------------- CLI

def load_env(path: Path = Path('.env')) -> None:
    if path.exists():
        for line in path.read_text(encoding='utf-8').splitlines():
            if '=' in line and not line.lstrip().startswith('#'):
                k, v = line.split('=', 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"'))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--week', help='any date inside the week to report (default: last full week)')
    ap.add_argument('--demo', action='store_true', help='use sample data instead of Jira')
    ap.add_argument('--out', default='reports', help='output folder')
    a = ap.parse_args()
    load_env()
    ref = dtparse.parse(a.week).date() if a.week else date.today() - timedelta(days=7)
    week_start = ref - timedelta(days=ref.weekday())
    if a.demo:
        data = demo_week(week_start)
    else:
        jira = Jira(os.environ['JIRA_URL'], os.environ['JIRA_EMAIL'], os.environ['JIRA_API_TOKEN'], os.environ.get('JIRA_STORY_POINTS_FIELD'))
        data = jira.week(os.environ['JIRA_BOARD_ID'], week_start)
    out = Path(a.out); out.mkdir(parents=True, exist_ok=True)
    pdf = out / f'jira-weekly-{week_start:%Y-%m-%d}.pdf'
    build_pdf(data, pdf, os.environ.get('REPORT_TITLE', 'Weekly development report'))
    print(f'{pdf}  ({len(data.completed_in_week)} completed, {sum(w.hours for w in data.worklogs):.1f} h logged)')


if __name__ == '__main__':
    main()
