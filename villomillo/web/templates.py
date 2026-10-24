"""Page templates, kept in code so there are no data files to lose when the package is moved."""

BASE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{% block title %}VilloMillo{% endblock %}</title>
<style>
:root { --bg:#f6f7f9; --card:#fff; --ink:#15181d; --mute:#5b6572; --line:#dfe3e8; --accent:#0b6bcb;
        --good:#14804a; --warn:#a15c00; --bad:#b3261e; --chip:#eef1f5; }
@media (prefers-color-scheme: dark) { :root { --bg:#0f1318; --card:#171c23; --ink:#e8ecf1; --mute:#9aa6b4;
        --line:#2a323d; --accent:#6db3ff; --good:#4cc38a; --warn:#e3a54a; --bad:#ff7b72; --chip:#222a34; } }
* { box-sizing:border-box } body { margin:0; background:var(--bg); color:var(--ink);
  font:15px/1.45 system-ui, "Segoe UI", sans-serif }
header { display:flex; gap:16px; align-items:center; padding:12px 20px; background:var(--card);
  border-bottom:1px solid var(--line); flex-wrap:wrap }
header b { font-size:17px } header a { color:var(--mute); text-decoration:none } header a.on { color:var(--ink); font-weight:600 }
main { max-width:1100px; margin:0 auto; padding:20px 16px 60px } h1 { font-size:20px; margin:0 0 12px }
.cards { display:grid; grid-template-columns:repeat(auto-fit,minmax(210px,1fr)); gap:12px; margin-bottom:18px }
.card { background:var(--card); border:1px solid var(--line); border-radius:10px; padding:14px }
.card .n { font-size:26px; font-weight:650 } .card .l { color:var(--mute); font-size:13px }
table { width:100%; border-collapse:collapse } td, th { padding:10px 8px; text-align:left; vertical-align:top }
tr.item { background:var(--card); border-bottom:1px solid var(--line) }
img.th { width:128px; height:72px; object-fit:cover; border-radius:6px; background:var(--chip) }
.chip { display:inline-block; background:var(--chip); border-radius:99px; padding:1px 8px; margin:2px 3px 0 0; font-size:12px; color:var(--mute) }
.chip.flag { color:var(--bad) } .gate { font-weight:650; font-size:12px; text-transform:uppercase }
.gate.pass { color:var(--good) } .gate.escalate { color:var(--warn) } .gate.fail { color:var(--mute) }
.score { font-size:22px; font-weight:700 } .mute { color:var(--mute); font-size:13px }
button.t { border:1px solid var(--line); background:var(--card); color:var(--ink); border-radius:8px; font-size:18px;
  padding:6px 10px; cursor:pointer; margin-right:4px } button.t[aria-pressed=true] { background:var(--accent); color:#fff; border-color:var(--accent) }
a { color:var(--accent) } .filters a { margin-right:12px } .filters a.on { font-weight:700; color:var(--ink); text-decoration:none }
.bar { height:8px; background:var(--chip); border-radius:99px; overflow:hidden; margin-top:6px } .bar i { display:block; height:100%; background:var(--accent) }
.ev { font-size:13px; border-top:1px solid var(--line); padding:6px 0 } .ev.error { color:var(--bad) } .ev.warn { color:var(--warn) }
@media (max-width: 720px) { img.th { width:96px; height:54px } td.hide { display:none } }
</style></head><body>
<header><b>VilloMillo</b>
<a href="/?profile={{ current.id }}" class="{{ 'on' if active == 'overview' }}">Overview</a>
<a href="/research?profile={{ current.id }}" class="{{ 'on' if active == 'research' }}">Research</a>
<span class="mute">channel: {{ current.display_name }}</span></header>
<main>{% block body %}{% endblock %}</main></body></html>"""

OVERVIEW = """{% extends "base.html" %}{% set active = 'overview' %}
{% block body %}
<h1>Overview <span class="mute">for {{ current.display_name }}, quota day {{ day }}</span></h1>
<div class="cards">
 <div class="card"><div class="l">Top-20 picks you liked</div>
  <div class="n">{% if precision.precision is none %}no ratings yet{% else %}{{ (precision.precision * 100)|round|int }}%{% endif %}</div>
  <div class="mute">{{ precision.up }} of {{ precision.rated }} rated ({{ precision.n }} in the top list). Goal: 70%</div></div>
 <div class="card"><div class="l">Ready to clip (pass the gate)</div><div class="n">{{ gates.pass }}</div>
  <div class="mute">{{ gates.escalate }} need a second opinion, {{ gates.fail }} fell short</div></div>
 <div class="card"><div class="l">YouTube allowance used today</div>
  <div class="n">{{ quota.general[0] }} <span class="mute">/ {{ quota.general[1] }}</span></div>
  <div class="bar"><i style="width:{{ (100 * quota.general[0] / quota.general[1])|round|int }}%"></i></div>
  <div class="mute">searches: {{ quota.search[0] }} / {{ quota.search[1] }}</div></div>
 <div class="card"><div class="l">Jev spend today</div><div class="n">${{ '%.4f'|format(jev[0]) }}</div>
  <div class="mute">cap ${{ '%.2f'|format(jev[1]) }} a day</div></div>
</div>
<div class="card"><b>Videos by stage</b>
 <p>{% for state, n in states.items() %}<span class="chip">{{ state }}: {{ n }}</span>{% else %}<span class="mute">nothing found yet</span>{% endfor %}</p>
 <b>Background tasks</b>
 <p>{% for state, n in tasks.items() %}<span class="chip">{{ state }}: {{ n }}</span>{% else %}<span class="mute">none</span>{% endfor %}</p></div>
<h1 style="margin-top:20px">Recent events</h1>
<div class="card">{% for e in events %}<div class="ev {{ e.level }}"><span class="mute">{{ e.ts[5:16] }}</span> {{ e.component }}: {{ e.message }}</div>
{% else %}<span class="mute">Nothing yet.</span>{% endfor %}</div>
{% endblock %}"""

RESEARCH = """{% extends "base.html" %}{% set active = 'research' %}
{% block title %}Research | VilloMillo{% endblock %}
{% block body %}
<h1>Research <span class="mute">best matches first. Thumbs up what you would clip, thumbs down what you wouldn't.</span></h1>
<p class="mute" id="prec">Top-20 picks you liked:
 {% if precision.precision is none %}no ratings yet{% else %}{{ (precision.precision * 100)|round|int }}% ({{ precision.up }} of {{ precision.rated }} rated){% endif %}</p>
<p class="filters">{% for g in gates %}<a href="/research?profile={{ current.id }}&gate={{ g }}" class="{{ 'on' if g == gate }}">{{ g }}</a>{% endfor %}</p>
<table>{% for r in rows %}
<tr class="item" data-profile="{{ current.id }}" data-video="{{ r.video_id }}">
 <td style="width:136px"><a href="{{ r.url }}" target="_blank" rel="noopener noreferrer"><img class="th" src="{{ r.thumb }}" alt="" loading="lazy"></a></td>
 <td><a href="{{ r.url }}" target="_blank" rel="noopener noreferrer"><b>{{ r.title }}</b></a>
  <div class="mute">{{ r.channel_title }} · {{ r.minutes }} min · {{ r.age }} ago · {{ '{:,}'.format(r.views or 0) }} views · found by {{ r.found_by }}</div>
  <div>{% for x in r.reasons %}<span class="chip">{{ x }}</span>{% endfor %}{% for f in r.flags %}<span class="chip flag">{{ f }}</span>{% endfor %}</div></td>
 <td style="width:120px"><div class="score">{{ '%.0f'|format(r.score) }}</div>
  <div class="gate {{ r.gate }}">{{ r.gate }}</div><div class="mute">sure {{ '%.0f'|format(r.certainty * 100) }}%</div></td>
 <td style="width:110px">
  <button class="t" data-rating="1" aria-label="Thumbs up" aria-pressed="{{ 'true' if r.rating == 1 else 'false' }}">&#128077;</button><button class="t" data-rating="-1" aria-label="Thumbs down" aria-pressed="{{ 'true' if r.rating == -1 else 'false' }}">&#128078;</button></td>
</tr>{% else %}<tr><td class="mute">Nothing here yet. Run <code>python -m villomillo once</code> or start the worker.</td></tr>{% endfor %}</table>
<script>
document.addEventListener('click', async (ev) => {
  const btn = ev.target.closest('button.t'); if (!btn) return;
  const row = btn.closest('tr'); const wanted = btn.dataset.rating;
  const rating = btn.getAttribute('aria-pressed') === 'true' ? '0' : wanted;   // pressing again clears it
  const res = await fetch('/api/rate', { method: 'POST', body: new URLSearchParams({ profile: row.dataset.profile, video_id: row.dataset.video, rating }) });
  if (!res.ok) return;
  row.querySelectorAll('button.t').forEach(b => b.setAttribute('aria-pressed', String(b.dataset.rating === rating)));
  const p = (await res.json()).precision;
  document.getElementById('prec').textContent = 'Top-20 picks you liked: ' +
    (p.precision === null ? 'no ratings yet' : Math.round(p.precision * 100) + '% (' + p.up + ' of ' + p.rated + ' rated)');
});
</script>
{% endblock %}"""

TEMPLATES = {"base.html": BASE, "overview.html": OVERVIEW, "research.html": RESEARCH}
