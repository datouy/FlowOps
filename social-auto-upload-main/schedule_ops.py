import publish_jobs
import json
from flask import jsonify, request
from datetime import datetime, timedelta

def _week_schedule():
    """Compute scheduled publish dates for jobs with timer enabled."""
    items = []
    # Prefer concrete expanded rows (per account + per file) when available.
    with publish_jobs.get_conn() as c:
        try:
            rows = c.execute(
                '''SELECT s.id, s.job_id, s.account, s.file, s.scheduled_at, s.status,
                          j.type, j.title
                   FROM scheduled_items s LEFT JOIN jobs j ON j.id = s.job_id
                   WHERE s.scheduled_at IS NOT NULL
                   ORDER BY s.scheduled_at''').fetchall()
        except Exception:
            rows = []
    if rows:
        for r in rows:
            items.append({
                'job_id': r['job_id'],
                'title': r['title'],
                'type': r['type'],
                'account': r['account'],
                'file': r['file'],
                'status': r['status'] or 'pending',
                'scheduled_at': r['scheduled_at'],
                'date': r['scheduled_at'][:10],
            })
        return items
    # Fallback: derive from job extra (legacy jobs before expansion existed).
    with publish_jobs.get_conn() as c:
        rows = c.execute('SELECT * FROM jobs ORDER BY id DESC').fetchall()
    for job in rows:
        extra = json.loads(job['extra'] or '{}')
        if not extra.get('enableTimer'):
            continue
        videos_per_day = int(extra.get('videosPerDay') or 1)
        daily_times = extra.get('dailyTimes') or ['10:00']
        start_days = int(extra.get('startDays') or 0)
        n_files = len(json.loads(job['file_list'] or '[]'))
        total = max(n_files, 1)
        for video in range(total):
            day_offset = video // max(videos_per_day,1) + start_days + 1
            time_idx = video % max(videos_per_day,1)
            hhmm = str(daily_times[time_idx]) if time_idx < len(daily_times) else '10:00'
            try:
                hh, mm = hhmm.split(':')[:2]
                hh, mm = int(hh), int(mm)
            except Exception:
                hh, mm = 10, 0
            dt = (datetime.now() + timedelta(days=day_offset)).replace(hour=hh, minute=mm, second=0, microsecond=0)
            items.append({
                'job_id': job['id'],
                'title': job['title'],
                'type': job['type'],
                'accounts': job['account_list'],
                'files': job['file_list'],
                'status': job['status'],
                'scheduled_at': dt.strftime('%Y-%m-%d %H:%M'),
                'date': dt.strftime('%Y-%m-%d'),
            })
    return items

def register(app):
    @app.route('/material-status', methods=['GET'])
    def material_status():
        """Return set of file names that have been used in publish records."""
        used = set()
        with publish_jobs.get_conn() as c:
            rows = c.execute('SELECT files FROM publish_records').fetchall()
        for r in rows:
            for f in (r['files'] or '').split(','):
                f = f.strip()
                if f:
                    used.add(f)
        return jsonify({'code': 200, 'data': sorted(used), 'msg': None}), 200

    @app.route('/schedule/week', methods=['GET'])
    def schedule_week():
        items = _week_schedule()
        return jsonify({'code': 200, 'data': items, 'msg': None}), 200

    @app.route('/stats/unmatched', methods=['GET'])
    def stats_unmatched():
        """Return latest collected contents that were not linked to a publish record."""
        with publish_jobs.get_conn() as c:
            rows = c.execute('''
                SELECT s.* FROM content_stats s
                INNER JOIN (
                    SELECT content_key, MAX(id) AS max_id
                    FROM content_stats
                    WHERE content_key IS NOT NULL AND content_key != ''
                    GROUP BY content_key
                ) latest ON latest.max_id = s.id
                WHERE s.record_id IS NULL OR s.record_id = 0
                ORDER BY s.id DESC
            ''').fetchall()
        return jsonify({'code': 200, 'data': [dict(r) for r in rows], 'msg': None}), 200

    @app.route('/stats/link', methods=['POST'])
    def stats_link():
        """Manually link a collected content row to a publish record."""
        data = request.get_json(force=True) or {}
        stats_id = data.get('stats_id')
        record_id = data.get('record_id')
        if not stats_id or not record_id:
            return jsonify({'code': 400, 'msg': 'stats_id and record_id are required'}), 400
        with publish_jobs.get_conn() as c:
            row = c.execute('SELECT content_key FROM content_stats WHERE id=?', (stats_id,)).fetchone()
            rec = c.execute('SELECT id FROM publish_records WHERE id=?', (record_id,)).fetchone()
            if not row or not rec:
                return jsonify({'code': 404, 'msg': 'stats or record not found'}), 404
            c.execute('UPDATE content_stats SET record_id=? WHERE id=?', (record_id, stats_id))
            if row['content_key']:
                c.execute('UPDATE content_stats SET record_id=? WHERE content_key=?', (record_id, row['content_key']))
        return jsonify({'code': 200, 'msg': 'linked'}), 200
