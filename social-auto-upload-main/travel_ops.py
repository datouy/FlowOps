# -*- coding: utf-8 -*-
import sqlite3
from pathlib import Path
from flask import Blueprint, request, jsonify
from conf import BASE_DIR

bp = Blueprint('travel_ops', __name__)
DB = Path(BASE_DIR / 'db' / 'travel_ops.db')

def get_conn():
    conn = sqlite3.connect(DB, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with get_conn() as c:
        c.execute('''CREATE TABLE IF NOT EXISTS tours (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL, destination TEXT DEFAULT '', days INTEGER DEFAULT 1,
            price REAL DEFAULT 0, tags TEXT DEFAULT '', highlights TEXT DEFAULT '',
            itinerary TEXT DEFAULT '', status TEXT DEFAULT 'active',
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP)''')
        c.execute('''CREATE TABLE IF NOT EXISTS content_templates (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL, platform TEXT DEFAULT 'all', category TEXT DEFAULT '',
            title_tpl TEXT DEFAULT '', body_tpl TEXT DEFAULT '',
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP)''')
        c.execute('''CREATE TABLE IF NOT EXISTS leads (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            platform TEXT DEFAULT '', source_content TEXT DEFAULT '',
            customer_contact TEXT DEFAULT '', intent_tour TEXT DEFAULT '',
            follow_status TEXT DEFAULT 'new', note TEXT DEFAULT '',
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP)''')

init_db()

@bp.route('/tours', methods=['GET'])
def list_tours():
    with get_conn() as c:
        rows = [dict(r) for r in c.execute('SELECT * FROM tours ORDER BY id DESC').fetchall()]
    return jsonify({'code':200,'data':rows,'msg':None})

@bp.route('/tours', methods=['POST'])
def add_tour():
    d = request.get_json(force=True)
    if not d.get('name'):
        return jsonify({'code':400,'msg':'name required'}), 400
    with get_conn() as c:
        cur = c.execute('INSERT INTO tours (name,destination,days,price,tags,highlights,itinerary,status) VALUES (?,?,?,?,?,?,?,?)',
            (d.get('name'), d.get('destination',''), int(d.get('days') or 1), float(d.get('price') or 0),
             d.get('tags',''), d.get('highlights',''), d.get('itinerary',''), d.get('status','active')))
        tid = cur.lastrowid
    return jsonify({'code':200,'data':{'id':tid},'msg':'ok'})

@bp.route('/tours/<int:tid>', methods=['PUT'])
def update_tour(tid):
    d = request.get_json(force=True)
    with get_conn() as c:
        c.execute('UPDATE tours SET name=?,destination=?,days=?,price=?,tags=?,highlights=?,itinerary=?,status=? WHERE id=?',
            (d.get('name'), d.get('destination',''), int(d.get('days') or 1), float(d.get('price') or 0),
             d.get('tags',''), d.get('highlights',''), d.get('itinerary',''), d.get('status','active'), tid))
    return jsonify({'code':200,'msg':'ok'})

@bp.route('/tours/<int:tid>', methods=['DELETE'])
def delete_tour(tid):
    with get_conn() as c:
        c.execute('DELETE FROM tours WHERE id=?', (tid,))
    return jsonify({'code':200,'msg':'ok'})

@bp.route('/templates', methods=['GET'])
def list_templates():
    with get_conn() as c:
        rows = [dict(r) for r in c.execute('SELECT * FROM content_templates ORDER BY id DESC').fetchall()]
    return jsonify({'code':200,'data':rows,'msg':None})

@bp.route('/templates', methods=['POST'])
def add_template():
    d = request.get_json(force=True)
    if not d.get('name'):
        return jsonify({'code':400,'msg':'name required'}), 400
    with get_conn() as c:
        cur = c.execute('INSERT INTO content_templates (name,platform,category,title_tpl,body_tpl) VALUES (?,?,?,?,?)',
            (d.get('name'), d.get('platform','all'), d.get('category',''), d.get('title_tpl',''), d.get('body_tpl','')))
        tid = cur.lastrowid
    return jsonify({'code':200,'data':{'id':tid},'msg':'ok'})

@bp.route('/templates/<int:tid>', methods=['DELETE'])
def delete_template(tid):
    with get_conn() as c:
        c.execute('DELETE FROM content_templates WHERE id=?', (tid,))
    return jsonify({'code':200,'msg':'ok'})

@bp.route('/generate-content', methods=['POST'])
def generate_content():
    d = request.get_json(force=True)
    with get_conn() as c:
        tour = c.execute('SELECT * FROM tours WHERE id=?', (d.get('tour_id'),)).fetchone()
        tpl = c.execute('SELECT * FROM content_templates WHERE id=?', (d.get('template_id'),)).fetchone()
    if not tour or not tpl:
        return jsonify({'code':404,'msg':'tour or template not found'}), 404
    mapping = {
        '{name}': tour['name'], '{destination}': tour['destination'],
        '{days}': str(tour['days']), '{price}': str(tour['price']),
        '{highlights}': tour['highlights'], '{itinerary}': tour['itinerary'], '{tags}': tour['tags'],
    }
    title = tpl['title_tpl']; body = tpl['body_tpl']
    for k, v in mapping.items():
        title = title.replace(k, v); body = body.replace(k, v)
    return jsonify({'code':200,'data':{'title':title,'body':body,'tags':tour['tags'],'platform':tpl['platform']},'msg':'ok'})

@bp.route('/leads', methods=['GET'])
def list_leads():
    with get_conn() as c:
        rows = [dict(r) for r in c.execute('SELECT * FROM leads ORDER BY id DESC').fetchall()]
    return jsonify({'code':200,'data':rows,'msg':None})

@bp.route('/leads', methods=['POST'])
def add_lead():
    d = request.get_json(force=True)
    with get_conn() as c:
        cur = c.execute('INSERT INTO leads (platform,source_content,customer_contact,intent_tour,follow_status,note) VALUES (?,?,?,?,?,?)',
            (d.get('platform',''), d.get('source_content',''), d.get('customer_contact',''),
             d.get('intent_tour',''), d.get('follow_status','new'), d.get('note','')))
        lid = cur.lastrowid
    return jsonify({'code':200,'data':{'id':lid},'msg':'ok'})

@bp.route('/leads/<int:lid>', methods=['PUT'])
def update_lead(lid):
    d = request.get_json(force=True)
    with get_conn() as c:
        c.execute('UPDATE leads SET follow_status=?, note=? WHERE id=?', (d.get('follow_status','new'), d.get('note',''), lid))
    return jsonify({'code':200,'msg':'ok'})

@bp.route('/leads/<int:lid>', methods=['DELETE'])
def delete_lead(lid):
    with get_conn() as c:
        c.execute('DELETE FROM leads WHERE id=?', (lid,))
    return jsonify({'code':200,'msg':'ok'})

@bp.route('/lead-stats', methods=['GET'])
def lead_stats():
    """按来源平台统计线索量与转化率"""
    with get_conn() as c:
        rows = c.execute('SELECT platform, COUNT(*) total, '
                         "SUM(CASE WHEN follow_status='converted' THEN 1 ELSE 0 END) converted, "
                         "SUM(CASE WHEN follow_status='new' THEN 1 ELSE 0 END) new_count, "
                         "SUM(CASE WHEN follow_status='contacted' THEN 1 ELSE 0 END) contacted "
                         'FROM leads GROUP BY platform ORDER BY total DESC').fetchall()
    stats = []
    for r in rows:
        total = r['total']; converted = r['converted'] or 0
        stats.append({
            'platform': r['platform'] or '未知',
            'total': total,
            'new': r['new_count'] or 0,
            'contacted': r['contacted'] or 0,
            'converted': converted,
            'conversion_rate': round(converted * 100.0 / total, 1) if total else 0.0,
        })
    return jsonify({'code':200,'data':stats,'msg':None})