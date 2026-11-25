#!/usr/bin/env python3
"""
makawan_web.py - a local website for Makawan, the BTC tracker
==============================================================
Runs the same engine as makawan_bot.py and serves a live dashboard at
http://127.0.0.1:8765  (this computer only). Times show in US Eastern.

    python makawan_web.py                 # live
    python makawan_web.py --simulate      # offline demo
    python makawan_web.py --port 9000 --open

Paper trading only. It never places a real order and needs no API key.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import makawan_bot as tb

REQUIRES = "2026.11.13"
if getattr(tb, "__version__", None) != REQUIRES:
    sys.exit("{} and makawan_bot.py come from different downloads. Put ALL the files from the latest download in one "
             "folder, replacing the old ones (makawan_bot.py reports version {}, this file needs {}).".format(
                 os.path.basename(__file__), getattr(tb, "__version__", "older than 2026.11.08.4"), REQUIRES))


# --------------------------------------------------------------------------
# State -> JSON
# --------------------------------------------------------------------------
def snapshot(eng: tb.Engine, clock, mode: str) -> dict:
    cfg, now = eng.cfg, clock.time()
    c_trade = cfg.trade
    wins = []
    for h in cfg.horizons:
        w = eng.active.get(h)
        if not w:
            wins.append({"horizon": h, "slug": None})
            continue
        i = w.info
        v = cfg.view(i.horizon)
        hist = w.hist
        if len(hist) > 300:
            hist = hist[::math.ceil(len(hist) / 300)] + [w.hist[-1]]
        pos = None
        if w.position:
            pos = {"side": w.position.side, "shares": w.position.shares, "avg": w.position.avg_px}
        wins.append({
            "horizon": h, "slug": i.slug, "ws": i.ws, "we": i.we, "tau": w.tau, "ptb": i.ptb,
            "index": i.index_symbol, "warm_s": max(v.warmup_min_s, v.warmup_frac * (i.we - i.ws)), "model": w.p_model, "market": w.p_mkt, "chance": w.p_final,
            "call": w.call, "first_call": w.first_call, "lean": tb.lean_side(w), "book_error": w.book_error,
            "lean_why": "holding" if w.position else ("call" if w.call else "leaning"), "bid": w.book.bid if w.book else None,
            "ask": w.book.ask if w.book else None, "note": w.note, "value": w.value_note,
            "position": pos, "trade_last": v.trade_last_s.get(i.horizon) if c_trade else None, "hist": hist})
    stats = {}
    for h, st in eng.stats.items():
        stats[h] = {"windows": st.windows, "called": st.called, "correct": st.correct, "trades": st.trades,
                    "staked": round(st.staked, 2), "pnl": round(st.pnl, 2), "fees": round(st.fees, 2),
                    "gross": round(st.pnl + st.fees, 2), "orders": st.orders, "fills": st.fills,
                    "brier": (st.brier / st.brier_n) if st.brier_n else None}
    c = eng.cfg
    return {
        "ready": True, "mode": mode, "now": now, "tz": tb.DISPLAY_TZ_NAME,
        "brti": eng.S, "print_ts": eng.last_print_ts,
        "sigma_min": None if eng.sigma_sec is None else eng.sigma_sec * math.sqrt(60.0),
        "cash": round(eng.cash, 2), "equity": round(eng.equity(), 2), "bankroll": c.bankroll,
        "account": eng.real_status(),
        "errors": eng.errors, "last_error": eng.last_error, "warnings": list(eng.warnings) + ([eng.sleep_note()] if eng.sleep_note() else []),
        "guard": eng.guard_status(),
        "real": "rehearsal" if getattr(eng.broker, "rehearse", False) else ("real" if eng.broker is not None else ""), "last_tick": eng._last_tick, "sleep_gaps": eng.sleep_gaps, "version": tb.__version__, "notes": list(eng.notes),
        "out_dir": eng.cfg.out_dir, "books": eng.stream_status(), "spot": eng.spot_status(),
        "pending": [w.info.horizon for w in eng.pending],
        "thresholds": {"call_up": c.call_up, "keep_up": c.keep_up, "keep_down": c.keep_down, "call_down": c.call_down},
        "trade": c.trade, "exec_mode": c.exec_mode, "model": tb.describe_model(c), "windows": wins, "stats": stats,
        "events": [t for _, t in eng.events], "history": list(eng.history)[:15]}


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------
class Shared:
    latest = json.dumps({"ready": False}).encode()
    out_dir = "makawan_out"


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, body: bytes, ctype: str, code=200, extra=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?")[0]
        if path in ("/", "/index.html"):
            return self._send(PAGE.encode("utf-8"), "text/html; charset=utf-8")
        if path == "/api/state":
            return self._send(Shared.latest, "application/json")
        if path in ("/events.csv", "/windows.csv"):
            f = os.path.join(Shared.out_dir, path[1:])
            if os.path.exists(f):
                with open(f, "rb") as fh:
                    return self._send(fh.read(), "text/csv; charset=utf-8",
                                      extra={"Content-Disposition": f'attachment; filename="{path[1:]}"'})
        self._send(b"not found", "text/plain", 404)


# --------------------------------------------------------------------------
# The page (one self-contained file; follows your light/dark setting)
# --------------------------------------------------------------------------
PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Makawan BTC tracker</title>
<style>
:root{--bg:#f6f3ea;--card:#fffdf7;--ink:#1d2a22;--mute:#6a7068;--line:#e3ddcc;--up:#1f7a4d;--down:#b4473a;
--warn:#8f6400;--warnbg:#fbf0cf;--grid:#cdc6b3;--chip:#efeadb}
@media (prefers-color-scheme:dark){:root{--bg:#111512;--card:#192019;--ink:#e7ece4;--mute:#97a297;--line:#2a332b;
--up:#4cc38a;--down:#e6806f;--warn:#e3b44f;--warnbg:#3a3014;--grid:#3a463c;--chip:#232c24}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif}
.wrap{max-width:1180px;margin:0 auto;padding:18px 16px 40px}
header{display:flex;flex-wrap:wrap;gap:10px 22px;align-items:baseline;justify-content:space-between;margin-bottom:14px}
h1{font:600 22px Georgia,"Times New Roman",serif;margin:0}
.mono{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}
.mute{color:var(--mute)} .up{color:var(--up)} .down{color:var(--down)}
.chip{display:inline-block;padding:2px 9px;border-radius:99px;background:var(--chip);font-size:12px;letter-spacing:.04em}
.dot{display:inline-block;width:9px;height:9px;border-radius:50%;margin-right:6px;background:var(--mute)}
.dot.ok{background:var(--up)} .dot.bad{background:var(--down)}
.top{display:flex;flex-wrap:wrap;gap:6px 22px;align-items:baseline}
.big{font:600 26px ui-monospace,Menlo,Consolas,monospace}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(340px,1fr));gap:14px}
.card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:14px 16px}
.card h2{margin:0;font:600 18px Georgia,serif;display:flex;justify-content:space-between;align-items:baseline}
.sub{font-size:12.5px;color:var(--mute)}
.row{display:flex;justify-content:space-between;gap:10px;padding:3px 0;border-bottom:1px dotted var(--line)}
.row:last-child{border-bottom:0}
.badge{font-weight:700;padding:3px 12px;border-radius:8px;border:1.5px solid var(--mute);color:var(--mute)}
.badge.UP{border-color:var(--up);color:var(--up)} .badge.DOWN{border-color:var(--down);color:var(--down)}
.chance{display:flex;align-items:center;gap:12px;margin:8px 0 4px}
.chance .n{font:600 40px ui-monospace,Menlo,Consolas,monospace;min-width:104px}
.gauge{position:relative;height:12px;border-radius:6px;background:var(--chip)}
.gl{display:flex;justify-content:space-between;font-size:11px;color:var(--mute);margin-top:7px;letter-spacing:.05em}
.gauge i{position:absolute;top:-3px;width:2px;height:18px;background:var(--grid)}
.gauge b{position:absolute;top:-4px;width:6px;height:20px;border-radius:3px;background:var(--ink);transform:translateX(-3px)}
canvas{width:100%;height:170px;display:block;margin:6px 0 8px}
table{width:100%;border-collapse:collapse;font-size:14px}
th,td{text-align:right;padding:5px 8px;border-bottom:1px solid var(--line)}
th{font-weight:600;color:var(--mute);font-size:12.5px} th:first-child,td:first-child{text-align:left}
.panel{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:12px 16px;margin-top:14px}
.panel h3{margin:0 0 6px;font:600 16px Georgia,serif}
.note{background:var(--chip);border-radius:10px;padding:8px 12px;margin:0 0 12px;font-size:14px}
.warn{background:var(--warnbg);color:var(--warn);border-radius:10px;padding:8px 12px;margin:0 0 12px;font-size:14px}
.log{font:13px/1.55 ui-monospace,Menlo,Consolas,monospace;white-space:pre-wrap;max-height:260px;overflow:auto}
footer{margin-top:16px;font-size:12.5px;color:var(--mute)} a{color:var(--up)}
</style></head><body><div class="wrap">
<header>
  <h1>Makawan <span class="sub">Bitcoin Up/Down tracker &middot; Polymarket US &middot; paper money</span></h1>
  <div class="top"><span id="mode" class="chip">-</span><span id="clock" class="mono">-</span></div>
</header>
<div class="sub" id="model" style="margin:-6px 0 10px"></div>
<div id="real" style="display:none;margin:10px 0;padding:10px 14px;border-radius:8px;background:#8a5a00;color:#fff;font-weight:600"></div>
<div id="halt" style="display:none;margin:10px 0;padding:12px 14px;border-radius:8px;background:#b3261e;color:#fff;font-weight:600"></div>
<div id="warn"></div>
<div class="top" style="margin-bottom:12px">
  <div><span class="sub">BRTI (official index)</span><div class="big" id="brti">-</div></div>
  <div><span class="sub">typical minute</span><div class="big" id="sigma">-</div></div>
  <div><span class="sub" id="eqlabel">paper equity</span><div class="big" id="equity">-</div>
    <div class="sub mono" id="eqnote"></div></div>
  <div><span class="sub">feed</span><div><span class="dot" id="fdot"></span><span id="feed" class="mono">-</span></div></div>
  <div><span class="sub">order books</span><div class="mono" id="books">-</div></div>
  <div><span class="sub">Coinbase price</span><div class="mono" id="spot">-</div></div>
</div>
<div class="cards" id="cards"></div>
<div class="panel"><h3>Scorecard</h3><table id="score"></table>
  <div class="sub" style="margin-top:6px" id="liqnote"></div><div class="sub">Net = gross &minus; fees. Counts each window's <b>first</b> call. Brier at 50% of the window: 0.250 = coin flip, lower is better.</div></div>
<div class="panel"><h3>Recent windows</h3><table id="hist"></table></div>
<div class="panel"><h3>Calls and trades</h3><div class="log" id="log"></div></div>
<footer>Example sizing, paper money only. Not financial advice. Chances are model estimates, not predictions.
Window slugs on Polymarket use UTC ("z"). Downloads: <a href="/events.csv">events.csv</a> - <a href="/windows.csv">windows.csv</a>. History is kept in <span class="mono" id="outdir">-</span>/ and picked up on restart (start over with --fresh).</footer>
</div>
<script>
const $=s=>document.querySelector(s), byId=i=>document.getElementById(i);
const esc=s=>String(s==null?'':s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
let S=null, got=0, skew=0, built=false;
const pct=x=>x==null?'-':Math.round(x*100)+'%';
const num=(x,d=2)=>x==null?'-':x.toLocaleString('en-US',{minimumFractionDigits:d,maximumFractionDigits:d});
const usd=x=>x==null?'-':(x<0?'-':'')+'$'+num(Math.abs(x));
const shr=x=>x==null?'':(Math.abs(x-Math.round(x))<0.005?String(Math.round(x)):num(x,2));
const cents=x=>x==null?'':String(Math.round(x*1000)/10)+'\u00A2';
const sgn=x=>x==null?'-':(x>=0?'+':'-')+num(Math.abs(x));
const tf=(ts,o)=>new Intl.DateTimeFormat('en-US',Object.assign({timeZone:S.tz},o)).format(new Date(ts*1000));
const tHMS=t=>tf(t,{hour:'numeric',minute:'2-digit',second:'2-digit'});
const tHM=t=>tf(t,{hour:'numeric',minute:'2-digit'});
const tDay=t=>tf(t,{month:'short',day:'numeric'});
const tzn=t=>new Intl.DateTimeFormat('en-US',{timeZone:S.tz,timeZoneName:'short'}).formatToParts(new Date(t*1000)).find(p=>p.type==='timeZoneName').value;
const mmss=s=>{s=Math.max(0,Math.floor(s));const h=Math.floor(s/3600),m=Math.floor(s%3600/60),r=s%60;
  return (h?h+':'+String(m).padStart(2,'0'):m)+':'+String(r).padStart(2,'0');};

function build(){
  $('#cards').innerHTML=S.windows.map(w=>`<div class="card" id="c-${w.horizon}">
    <h2><span>${w.horizon} window</span><span class="badge" id="${w.horizon}-call">-</span></h2>
    <div class="sub" id="${w.horizon}-when">-</div>
    <div class="chance"><div class="n" id="${w.horizon}-n">-</div>
      <div style="flex:1"><div class="gauge" id="${w.horizon}-g"></div><div class="gl"><span>&#9664; DOWN</span><span>UP &#9654;</span></div></div></div>
    <div class="sub" id="${w.horizon}-cs">-</div>
    <canvas id="${w.horizon}-cv"></canvas>
    <div id="${w.horizon}-rows"></div></div>`).join('');
  S.windows.forEach(w=>{const t=S.thresholds;
    byId(w.horizon+'-g').innerHTML=[t.call_down,t.keep_down,.5,t.keep_up,t.call_up].map(v=>`<i style="left:${v*100}%"></i>`).join('')+`<b id="${w.horizon}-m" style="left:50%"></b>`;});
  built=true;
}
function chart(w){
  const cv=byId(w.horizon+'-cv'), dpr=window.devicePixelRatio||1, W=cv.clientWidth, H=cv.clientHeight;
  cv.width=W*dpr; cv.height=H*dpr; const g=cv.getContext('2d'); g.scale(dpr,dpr); g.clearRect(0,0,W,H);
  const css=getComputedStyle(document.documentElement), c=n=>css.getPropertyValue(n).trim();
  const L=4,R=58,T=8,B=8, X=t=>L+(t-w.ws)/(w.we-w.ws)*(W-L-R), Y=p=>T+(1-p)*(H-T-B), th=S.thresholds;
  g.globalAlpha=.10; g.fillStyle=c('--up'); g.fillRect(L,Y(1),W-L-R,Y(th.call_up)-Y(1));
  g.fillStyle=c('--down'); g.fillRect(L,Y(th.call_down),W-L-R,Y(0)-Y(th.call_down)); g.globalAlpha=1;
  const wu=w.ws+(w.warm_s!=null?w.warm_s:(w.we-w.ws)/15); g.fillStyle=c('--chip'); g.fillRect(X(w.ws),T,X(wu)-X(w.ws),H-T-B);
  g.font='11px ui-monospace,Menlo,Consolas,monospace'; g.textBaseline='middle'; g.lineWidth=1;
  [[th.call_up,'57% UP',c('--up'),1],[th.keep_up,'',c('--grid'),0],[.5,'50%',c('--mute'),1],
   [th.keep_down,'',c('--grid'),0],[th.call_down,'43% DOWN',c('--down'),1]].forEach(([v,lab,col,solid])=>{
    g.strokeStyle=solid?col:c('--grid'); g.setLineDash(solid?[5,4]:[2,4]);
    g.beginPath(); g.moveTo(L,Y(v)); g.lineTo(W-R,Y(v)); g.stroke(); g.setLineDash([]);
    if(lab){g.fillStyle=col; g.fillText(Math.round(v*100)+'%'===lab?lab:lab.replace(/^\d+/,Math.round(v*100)),W-R+5,Y(v));}});
  if(w.trade_last){g.fillStyle=c('--ink'); g.globalAlpha=.55; g.fillRect(X(w.we-w.trade_last),H-B-3,X(w.we)-X(w.we-w.trade_last),3); g.globalAlpha=1;
    g.fillStyle=c('--mute'); g.fillText('trades',W-R+5,H-B-6);}
  g.fillStyle=c('--mute'); g.font='10px ui-monospace,Menlo,Consolas,monospace'; g.textBaseline='top';
  g.fillText('\u25B2 UP more likely',L+4,T+2); g.textBaseline='bottom'; g.fillText('\u25BC DOWN more likely',L+4,H-B-6);
  g.font='11px ui-monospace,Menlo,Consolas,monospace'; g.textBaseline='middle';
  if(!w.hist||!w.hist.length) return;
  g.strokeStyle=c('--ink'); g.lineWidth=1.8; g.lineJoin='round'; g.beginPath();
  w.hist.forEach(([t,p],i)=>{const x=X(t),y=Y(p); i?g.lineTo(x,y):g.moveTo(x,y);}); g.stroke();
}
function render(){
  if(!S||!S.ready) return;
  if(!built) build();
  // live: count from this computer's own clock (the bot runs on the same Mac), not from the moment the last update
  // arrived; that update can be a second or two old, which made the countdown jump back up. 'skew' only matters if
  // the page is opened on a device whose clock is off by more than 5 s.
  const live=S.mode==='live', nowS=live? Date.now()/1000+(Math.abs(skew)>5?skew:0) : S.now;
  $('#mode').textContent=live?'LIVE DATA':'SIMULATION';
  $('#model').textContent='Model: '+S.model;
  $('#outdir').textContent=S.out_dir||'-';
  $('#books').textContent=S.books||'-';
  byId('spot').textContent=S.spot||'-';
  $('#liqnote').innerHTML=S.exec_mode==='maker'?'<b>Maker</b> (paper, live data only): posts a limit bid and waits, paying no fee. It counts as filled when the ask drops to the bid, or a trade prints at or through it. Bids that never fill show as &ldquo;unfilled&rdquo;.':'<b>Taker</b>: buys at the ask straight away and pays the fee, about 0.095 &times; price &times; (1 &minus; price) per share (rate measured on a real trade).';
  $('#clock').textContent=tHMS(nowS)+' '+tzn(nowS)+' - '+tDay(nowS);
  $('#brti').textContent=S.brti==null?'-':'$'+num(S.brti);
  $('#sigma').textContent=S.sigma_min==null?'-':'$'+num(S.sigma_min,0);
  const A=S.account;
  if(A){
    const eqa=A.equity-S.bankroll;
    byId('eqlabel').textContent='Polymarket account';
    byId('equity').innerHTML='$'+num(A.equity)+` <span class="sub ${eqa>=0?'up':'down'}">${sgn(eqa)}</span>`;
    byId('eqnote').textContent='cash $'+num(A.balance)+(A.open_positions?' + open $'+num(A.open_positions):'')+
      ' \u00b7 bot\u2019s books $'+num(A.books)+(Math.abs(A.diff)>=0.5?' ('+sgn(A.diff)+')':'')+
      ' \u00b7 '+A.age+'s ago'+(A.error?' \u00b7 refresh failed':'');
  } else {
    const eq=S.equity-S.bankroll;
    byId('eqlabel').textContent=S.real==='real'?'equity (bot\u2019s books; reading Polymarket...)':'paper equity';
    byId('equity').innerHTML='$'+num(S.equity)+` <span class="sub ${eq>=0?'up':'down'}">${sgn(eq)}</span>`;
    byId('eqnote').textContent='';
  }
  const age=S.print_ts?nowS-S.print_ts:null, okf=age!=null&&age<10&&!S.last_error;
  $('#fdot').className='dot '+(okf?'ok':'bad');
  $('#feed').textContent=age==null?'no data yet':(S.last_error?S.last_error:'last print '+Math.round(age)+'s ago');
  const rb=byId('real');
  if(S.real){ rb.style.display='block';
    rb.textContent=S.real==='real'?'REAL MONEY \u2014 this bot sends real orders to your Polymarket account.'
                                  :'REHEARSAL \u2014 no orders are sent. Fills shown here are paper.'; }
  else rb.style.display='none';
  const G=S.guard||{}, hb=byId('halt');
  if(G.halted||G.paused_today){ hb.style.display='block';
    hb.textContent=G.halted?('TRADING HALTED \u2014 '+G.halted+'. Still tracking; no new trades. Restart with --resume to trade again.')
                           :('PAUSED UNTIL TOMORROW \u2014 daily loss limit reached (today '+sgn(G.today_pnl)+').'); }
  else hb.style.display='none';
  $('#warn').innerHTML=(S.notes||[]).map(x=>`<div class="note">${esc(x)}</div>`).join('')+S.warnings.map(x=>`<div class="warn">${esc(x)}</div>`).join('');
  S.windows.forEach(w=>{
    const id=w.horizon, el=s=>byId(id+'-'+s);
    if(!w.slug){el('when').textContent='waiting for the next window to be listed...';return;}
    const tau=live? Math.max(0,w.we-nowS) : w.tau;
    el('when').textContent=tHM(w.ws)+' - '+tHM(w.we)+' '+tzn(w.ws)+'   closes in '+mmss(tau)+(w.trade_last?'   -   trades in the last '+mmss(w.trade_last):'');
    const lean=w.lean, sc=x=>(x==null||lean==null)?x:(lean==='DOWN'?1-x:x), side=lean||'UP';
    el('n').textContent=pct(sc(w.chance));
    el('cs').innerHTML=lean?`chance BTC finishes <b>${lean==='UP'?'above':'below'}</b> the line (${lean}, ${w.lean_why}) &middot; UP ${pct(w.chance)} / DOWN ${pct(w.chance==null?null:1-w.chance)}`:'-';
    el('m').style.left=(Math.min(1,Math.max(0,w.chance==null?.5:w.chance))*100)+'%';
    const b=el('call'); b.textContent=w.call?'call '+w.call:'no call'; b.className='badge '+(w.call||'');
    const vs=(S.brti!=null&&w.ptb!=null)?S.brti-w.ptb:null;
    const pos=w.position?`${w.position.side} ${num(w.position.shares)} @ ${num(w.position.avg*100,1)}c`:"-";
    const rows=[['Price to beat',w.ptb==null?'waiting...':'$'+num(w.ptb)],
      ['BTC vs the line',vs==null?'-':`<span class="${(lean==='DOWN'?vs<0:vs>=0)?'up':'down'}">${sgn(vs)}</span> ${vs>=0?'above':'below'}`],
      ['Makawan\u2019s maths / the market ('+side+')',pct(sc(w.model))+' / '+(w.market==null?'<span class="mute">no quotes</span>':pct(sc(w.market)))],
      [side+' bid / ask',(w.bid==null&&w.ask==null)?(w.book_error?'<span class="down">'+esc(w.book_error)+'</span>':'<span class="mute">nobody quoting (logged to book_debug.jsonl)</span>'):
        (()=>{const c=x=>x==null?'none':Math.round(x*100)+'c'; return side==='DOWN'?c(w.ask==null?null:1-w.ask)+' / '+c(w.bid==null?null:1-w.bid):c(w.bid)+' / '+c(w.ask);})()],
      ['Value check',esc(w.note||w.value||'-')],['Paper position',esc(pos)]];
    el('rows').innerHTML=rows.map(r=>`<div class="row"><span class="mute">${r[0]}</span><span class="mono">${r[1]}</span></div>`).join('');
    chart(w);
  });
  const hs=Object.keys(S.stats);
  $('#score').innerHTML='<tr><th>Window</th><th>Settled</th><th>1st calls</th><th>Right</th><th>Hit rate</th><th>Trades</th>'+(S.exec_mode==='maker'?'<th>Bids filled</th>':'')+'<th>Staked</th><th>Gross P&amp;L</th><th>Fees</th><th>Net P&amp;L</th><th>Brier @50%</th></tr>'+
    hs.map(h=>{const s=S.stats[h];return `<tr><td>${h}</td><td>${s.windows}</td><td>${s.called}</td><td>${s.correct}</td><td>${s.called?Math.round(100*s.correct/s.called)+'%':'-'}</td><td>${s.trades}</td>${S.exec_mode==='maker'?`<td>${s.fills}/${s.orders}${s.orders?' ('+Math.round(100*s.fills/s.orders)+'%)':''}</td>`:''}<td>${usd(s.staked)}</td><td class="${s.gross>=0?'up':'down'}">${usd(s.gross)}</td><td>${usd(s.fees)}</td><td class="${s.pnl>=0?'up':'down'}">${usd(s.pnl)}</td><td>${s.brier==null?'-':s.brier.toFixed(3)}</td></tr>`}).join('');
  $('#hist').innerHTML='<tr><th>Window</th><th>Length</th><th>Result</th><th>1st call</th><th>Position</th><th>Gross</th><th>Fee</th><th>Net P&amp;L</th></tr>'+
    (S.history.length?S.history.map(r=>`<tr><td>${tDay(r.ws)} ${tHM(r.ws)}-${tHM(r.we)} ${tzn(r.ws)}</td><td>${r.horizon}</td><td class="${r.outcome==='UP'?'up':'down'}">${r.outcome}</td>
      <td>${r.first_call?r.first_call+(r.first_call_correct===1?' \u2713':' \u2717'):'-'}</td><td>${r.side?`<span class="${r.side==='UP'?'up':'down'}">${r.side}</span> ${shr(r.shares)} @ ${cents(r.avg)}`:(r.order_status?`<span class="mute">bid ${cents(r.order_px)} ${r.order_status}</span>`:'-')}</td>
      <td class="${(r.gross||0)>=0?'up':'down'}">${r.gross==null?'-':usd(r.gross)}</td>
      <td>${r.fee==null?'-':usd(r.fee)+(r.liquidity?' <span class="mute">'+r.liquidity+'</span>':'')}</td>
      <td class="${(r.pnl||0)>=0?'up':'down'}">${r.pnl==null?'-':usd(r.pnl)}</td></tr>`).join(''):'<tr><td colspan="8" class="mute">no windows have settled yet</td></tr>');
  $('#log').textContent=S.events.length?S.events.join('\n'):'no calls yet - each window waits before its first call';
}
async function poll(){
  try{const r=await fetch('/api/state',{cache:'no-store'}); S=await r.json(); got=Date.now(); if(S&&S.now) skew=S.now-got/1000;}catch(e){}
  render();
}
poll(); setInterval(poll,1000); setInterval(()=>{if(S&&S.mode==='live')render()},250);
window.addEventListener('resize',render);
</script></body></html>
"""


# --------------------------------------------------------------------------
def main(argv=None, live=None, overrides=None):
    """live: used only by makawan_live.py. A function (cfg, args) -> broker that sends REAL orders."""
    ap = argparse.ArgumentParser(description="Local web dashboard for Makawan, the BTC tracker")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--host", default="127.0.0.1", help="leave as 127.0.0.1 so only this computer can open it")
    ap.add_argument("--simulate", action="store_true")
    ap.add_argument("--speed", type=float, default=30.0, help="simulation speed (virtual seconds per real second)")
    ap.add_argument("--horizons", default="5m,15m,1h")
    ap.add_argument("--bankroll", type=float, default=None,
                    help="starting paper bankroll (default: what this folder started with, else 1000)")
    ap.add_argument("--w-market", type=float, default=None)
    ap.add_argument("--warmup-s", type=float, default=None, help="seconds to wait into each window before its first call")
    ap.add_argument("--trade-last", default=None, help="only open trades in the last N seconds, e.g. 5m=120,15m=300,1h=0")
    ap.add_argument("--stake", type=float, default=None, help="flat dollars per trade (default 20; 0 = quarter-Kelly)")
    ap.add_argument("--stop-loss", type=float, default=None,
                    help="halt trading once this share of the starting bankroll is lost (default 0.5; 0 = off)")
    ap.add_argument("--daily-loss", type=float, default=None,
                    help="pause until tomorrow after losing this many dollars in a day (default off)")
    ap.add_argument("--resume", action="store_true", help="clear a stop-loss halt and trade again")
    ap.add_argument("--max-gap", type=float, default=None, help="skip when the model is this far above the market (default 0.20; 1 = off)")
    ap.add_argument("--no-trade-on", dest="no_trade_on", default=None, help="window lengths to track but not trade (default 1h; 'none')")
    ap.add_argument("--exec", dest="exec_mode", choices=("taker", "maker"), default=None,
                    help="taker = buy at the ask (default); maker = paper limit bids, no fee (live paper only)")
    ap.add_argument("--confirm-s", type=float, default=None)
    ap.add_argument("--preset", choices=sorted(tb.PRESETS), default="pro")
    ap.add_argument("--params", default=None, help="params.json from makawan_tune.py")
    ap.add_argument("--no-ticks", action="store_true")
    ap.add_argument("--no-trade", action="store_true")
    ap.add_argument("--out", default=None, help="log folder (default makawan_out, or makawan_out_sim with --simulate)")
    ap.add_argument("--fresh", action="store_true", help="archive the old history in the log folder and start from scratch")
    ap.add_argument("--cut", action="store_true",
                    help="sell a LOSING position when buyers pay more for it (after the fee) than the maths says it is worth")
    ap.add_argument("--spot", choices=("off", "watch", "use"), default="watch",
                    help="Coinbase live price: watch = log it next to the index (default); use = also use it in the "
                         "model; off")
    ap.add_argument("--stream", action="store_true",
                    help="live order-book stream instead of polling (needs your API key; read-only)")
    ap.add_argument("--allow-sleep", action="store_true", help="do not keep the Mac awake while the bot runs")
    ap.add_argument("--tz", default="America/New_York")
    ap.add_argument("--open", action="store_true", help="open the page in your browser")
    a = ap.parse_args(argv)
    if not tb.set_display_tz(a.tz):
        print(f"Time zone '{a.tz}' not available, showing UTC. On Windows run: pip install tzdata")

    a.out = a.out or ("makawan_out_sim" if a.simulate else "makawan_out")
    if a.fresh:
        moved = tb.archive_outputs(a.out)
        if moved:
            print(f"Old history moved to {moved}/ - starting from scratch.")
    try:
        cfg = tb.build_config(a)
    except (ValueError, OSError) as e:
        sys.exit(f"Settings problem: {e}")
    for k, v in (overrides or {}).items():
        setattr(cfg, k, v)
    if a.simulate and cfg.exec_mode == "maker":
        sys.exit(tb.SIM_MAKER_MSG)
    if a.simulate:
        start, hist = tb.sim_start(cfg.out_dir)    # resume the fake clock after the last run in this folder
        clock = tb.SimClock(start)
        api = tb.Api(tb.SimTransport(clock, history_s=hist), cfg)
    else:
        clock = tb.RealClock()
        api = tb.Api(tb.HttpTransport(cfg.gateway), cfg)
    mode = "simulation" if a.simulate else "live"
    Shared.out_dir = cfg.out_dir
    rec = tb.Recorder(cfg.out_dir)
    tb.write_state(cfg.out_dir, cfg.bankroll)
    tb.clear_halt(cfg.out_dir, rec, a.resume)
    stream = tb.make_stream() if (a.stream and not a.simulate) else None
    if a.stream and a.simulate:
        print("--stream is ignored in a simulation (there is nothing to stream).")
    if not a.simulate and not a.allow_sleep:
        tb.keep_awake()
    broker = live(cfg, a) if live else None
    spot = tb.make_spot("off" if a.simulate else a.spot)
    eng = tb.Engine(cfg, api, clock, rec, stream=stream, broker=broker, spot=spot)
    try:
        for oid, what in eng.resolve_orphans():
            print(f"Resting order {oid} left from the last run: {what}.")
        eng.restore()
    except Exception as e:                          # never let old logs stop the dashboard
        eng.warn(f"Could not read the earlier history ({type(e).__name__}: {e}); continuing without it.")
    for n in eng.notes:
        print(n)

    def loop():
        try:
            eng.bootstrap()
        except Exception as e:                      # keep the page up and show the problem
            eng.last_error = f"{type(e).__name__}: {e}"
        while True:
            eng.tick()
            Shared.latest = json.dumps(snapshot(eng, clock, mode), default=str).encode()
            if a.simulate:
                clock.advance(1.0)
                if a.speed > 0:
                    time.sleep(1.0 / a.speed)
            else:
                time.sleep(max(0.05, math.floor(time.time()) + 1.15 - time.time()))

    threading.Thread(target=loop, daemon=True).start()
    srv = ThreadingHTTPServer((a.host, a.port), Handler)
    url = f"http://{'localhost' if a.host == '127.0.0.1' else a.host}:{a.port}"
    what = "REAL ORDERS" if (broker is not None and not getattr(broker, "rehearse", False)) else mode
    print(f"Dashboard: {url}   ({what}, times in {tb.DISPLAY_TZ_NAME})   Ctrl+C to stop")
    if a.host not in ("127.0.0.1", "localhost"):
        print("WARNING: other devices on your network can open this page.")
    if a.open:
        webbrowser.open(url)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
