// Übersicht desktop widget — "החבילות שלי"
// Runs the local tracker every minute and paints pickup cards on the desktop.
// Each card has a "נאסף ✓" button that marks the parcel collected and hides it.

import { run } from "uebersicht";

// Edit TOOL if you cloned the repo somewhere other than the default location.
const PY = "/usr/bin/python3";
const TOOL = "$HOME/Documents/Claude/package-tracker/package_tracker.py";

export const command = `${PY} "${TOOL}" --json`;

export const refreshFrequency = 60000; // 60s

export const className = `
  bottom: 120px; right: 40px; width: 340px;
  font-family: -apple-system, Heebo, Arial, sans-serif;
  direction: rtl; color: #e6edf3; z-index: 0;
  .pt-title { font-size: 20px; font-weight: 700; margin-bottom: 10px;
    text-shadow: 0 1px 6px rgba(0,0,0,.6); }
  .pt-card { background: rgba(22,27,34,.92); border: 1px solid #30363d;
    border-radius: 12px; padding: 12px 14px; margin-bottom: 10px;
    box-shadow: 0 6px 20px rgba(0,0,0,.35); backdrop-filter: blur(6px);
    transition: opacity .25s ease; }
  .pt-head { display:flex; justify-content:space-between; align-items:center;
    margin-bottom:6px; }
  .pt-courier { font-size:16px; font-weight:700; }
  .pt-left { font-weight:700; font-size:13px; }
  .pt-row { font-size:13px; color:#c9d1d9; padding:2px 0; }
  .pt-code { font-family:monospace; font-size:15px; color:#f0b90b; letter-spacing:1px; }
  .pt-link { color:#58a6ff; text-decoration:none; cursor:pointer; }
  .pt-link:hover { text-decoration:underline; }
  .pt-foot { display:flex; justify-content:space-between; align-items:center;
    margin-top:6px; border-top:1px solid #21262d; padding-top:6px; }
  .pt-meta { font-size:11px; color:#6e7681; }
  .pt-collect { font-size:12px; font-weight:700; color:#3ddc84; cursor:pointer;
    background:rgba(61,220,132,.12); border:1px solid rgba(61,220,132,.4);
    border-radius:8px; padding:3px 10px; user-select:none; }
  .pt-collect:hover { background:rgba(61,220,132,.25); }
  .pt-empty { background: rgba(22,27,34,.85); border:1px solid #30363d;
    border-radius:12px; padding:22px; text-align:center; color:#8b949e; font-size:14px; }
`;

const COLOR = { soon:"#ff5c5c", ok:"#3ddc84", expired:"#8a8a8a", none:"#f0b90b" };

function collect(key, ev) {
  // hide the card immediately, then record it so it never comes back
  const card = ev.currentTarget.closest(".pt-card");
  if (card) card.style.opacity = "0";
  run(`${PY} "${TOOL}" --collect ${key}`).then(() => {
    if (card) card.style.display = "none";
  });
}

export const render = ({ output }) => {
  let items = [];
  try { items = JSON.parse(output || "[]"); } catch (e) { items = []; }

  return (
    <div>
      <div className="pt-title">📦 החבילות שלי</div>
      {items.length === 0 ? (
        <div className="pt-empty">אין חבילות ממתינות 📭</div>
      ) : (
        items.map((r, i) => {
          const c = COLOR[r.urgency] || COLOR.none;
          return (
            <div className="pt-card" key={r.key || i} style={{ borderRight: `4px solid ${c}` }}>
              <div className="pt-head">
                <span className="pt-courier">{r.courier}</span>
                {r.time_left && <span className="pt-left" style={{ color: c }}>⏳ {r.time_left}</span>}
              </div>
              {r.location && <div className="pt-row">📍 {r.location}</div>}
              {r.pickup_code && <div className="pt-row">🔑 קוד: <span className="pt-code">{r.pickup_code}</span></div>}
              {r.tracking && <div className="pt-row">🔎 {r.tracking}</div>}
              {r.deadline && <div className="pt-row">📅 עד {r.deadline}</div>}
              {r.link && <div className="pt-row">🔗 <a className="pt-link" href={r.link} onClick={(e) => { e.preventDefault(); run(`open '${r.link}'`); }}>פתח קישור</a></div>}
              <div className="pt-foot">
                <span className="pt-meta">🕒 {r.received} · {r.sender_raw}</span>
                <span className="pt-collect" onClick={(e) => collect(r.key, e)}>נאסף ✓</span>
              </div>
            </div>
          );
        })
      )}
    </div>
  );
};
