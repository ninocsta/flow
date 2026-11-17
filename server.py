#!/usr/bin/env python3
"""Flow: painel local das sessões do Claude Code + agenda. Só stdlib.

Lê ~/.claude*/sessions/<pid>.json (sessões vivas) e os transcripts .jsonl,
guarda título/assunto manual em ~/.local/share/flow/tags.json,
foca a janela via hyprctl e abre sessão nova no kitty.
"""
import json
import os
import re
import subprocess
import threading
import shutil
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# Não herdar o ambiente de uma sessão Claude (senão o claude aberto pelo painel vira "sessão filha").
for _k in list(os.environ):
    if _k.startswith(("CLAUDE", "ANTHROPIC_")) or _k in ("AI_AGENT", "NODE_AUTH_TOKEN"):
        del os.environ[_k]

os.environ["PATH"] = f"{Path.home()}/.local/bin:" + os.environ.get("PATH", "/usr/bin:/bin")
PORT = int(os.environ.get("PAINEL_PORT", "8765"))
HOME = Path.home()
CLAUDE_DIRS = [d for d in (HOME / ".claude-trabalho", HOME / ".claude") if d.is_dir()]
DATA = HOME / ".local/share/flow"
DATA.mkdir(parents=True, exist_ok=True)
TAGS_FILE = DATA / "tags.json"
HERE = Path(__file__).resolve().parent
RECENT_DAYS = 3

GH_RE = re.compile(r"(?:/home/[^/]+|~)/github/([A-Za-z0-9_-]+)(?![A-Za-z0-9_.-]*\.md)")
CODE_RE = re.compile(r"\b(DEM|TAR|MSW|USI|MULTI|INF|QA)-\d+\b")
ASSUNTOS = ["Demanda", "Tarefa", "Revisão de PR", "Criar tarefas", "Deploy", "Infra", "Outro"]
SKILL_ASSUNTO = {
    "revisar-entrega": "Revisão de PR", "code-review": "Revisão de PR",
    "workspace-gestao": "Criar tarefas", "migrar-redmine-workspace": "Criar tarefas",
    "redmine-api": "Criar tarefas", "deploy-producao": "Deploy", "core-gfi-subir": "Deploy",
    "usi-subir-app": "Deploy", "salao-deploy-check": "Deploy", "coolify-upgrade": "Infra",
    "coolify-servidor-novo": "Infra", "we-serve-servidores": "Infra", "dia": "Outro",
}

PRICES = {"claude-opus-5-5": (4, 20, 0.2), "claude-opus-5": (5, 25, 0.5), "claude-sonnet-5-5": (2, 10, 0.1),
          "claude-sonnet-5": (2, 10, 0.2), "claude-haiku-5-5": (0.1, 0.5, 0.01), "claude-fable-5-2": (8, 40, 0.2)}
CTX_WINDOW = 1_000_000


def msg_cost(model, u):
    pin, pout, pread = PRICES.get((model or "").split("[")[0], PRICES["claude-opus-5-5"])
    cc = u.get("cache_creation") or {}
    w5, w1 = cc.get("ephemeral_5m_input_tokens"), cc.get("ephemeral_1h_input_tokens")
    if w5 is None and w1 is None:
        w5, w1 = u.get("cache_creation_input_tokens", 0), 0
    return ((u.get("input_tokens", 0) * pin + (w5 or 0) * pin * 1.25 + (w1 or 0) * pin * 2
             + u.get("cache_read_input_tokens", 0) * pread + u.get("output_tokens", 0) * pout) / 1e6)


def usage_of(path, seen=None):
    """Custo equivalente na API (US$) e contexto atual (tokens da última resposta). Respostas repetidas no
    jsonl (uma linha por bloco) contam uma vez, pelo id da mensagem."""
    seen = set() if seen is None else seen
    cost, ctx = 0.0, 0
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if '"usage"' not in line or '"type":"assistant"' not in line:
                    continue
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                m = d.get("message") or {}
                u, mid = m.get("usage"), m.get("id")
                if not u or mid in seen or m.get("model") == "<synthetic>":
                    continue
                seen.add(mid)
                cost += msg_cost(m.get("model"), u)
                if not d.get("isSidechain"):
                    ctx = u.get("input_tokens", 0) + u.get("cache_creation_input_tokens", 0) + u.get("cache_read_input_tokens", 0)
    except OSError:
        pass
    return cost, ctx


def resumo_of(last):
    """Uma linha de onde a sessão parou: a pergunta final do Claude, senão a última frase dele."""
    lines = [re.sub(r"^[#>*\-\s\d.)]+|[*`_]", "", l).strip() for l in (last or "").splitlines()]
    lines = [l for l in lines if len(l) > 3]
    if not lines:
        return ""
    pick = next((l for l in reversed(lines[-4:]) if l.endswith("?")), lines[-1])
    return pick if len(pick) <= 140 else pick[:139] + "…"


_lock = threading.Lock()
_cache = {}  # path -> (mtime, info)
_last_status = {}
_last_pid_status = {}
_busy_since = {}  # sid -> início do turno atual (modo serviço)
_shown_last = {"kitty": None, "fixa": False}  # fixa = você escolheu essa sessão no painel


def load_tags():
    try:
        return json.loads(TAGS_FILE.read_text())
    except Exception:
        return {}


def save_tags(tags):
    tmp = TAGS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(tags, ensure_ascii=False, indent=1))
    tmp.replace(TAGS_FILE)


def pid_alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def ppid(pid):
    try:
        for line in open(f"/proc/{pid}/status"):
            if line.startswith("PPid:"):
                return int(line.split()[1])
    except OSError:
        pass
    return 0


def comm(pid):
    try:
        return open(f"/proc/{pid}/comm").read().strip()
    except OSError:
        return ""


def kitty_pid_of(pid):
    p = pid
    for _ in range(10):
        p = ppid(p)
        if p <= 1:
            return None
        if comm(p) == "kitty":
            return p
    return None


def find_transcript(session_id):
    for cd in CLAUDE_DIRS:
        for f in (cd / "projects").glob(f"*/{session_id}.jsonl"):
            return f
    return None


def text_of(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(c.get("text", "") for c in content if isinstance(c, dict) and c.get("type") == "text")
    return ""


def parse_transcript(path):
    """Extrai título, primeira pergunta, última resposta, códigos e skills. Cache por mtime."""
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return {}
    hit = _cache.get(str(path))
    if hit and hit[0] == mtime:
        return hit[1]
    info = {"ai_title": None, "custom_title": None, "first": None, "last": None,
            "last_user": None, "codes": [], "skills": [], "mtime": mtime}
    codes, skills = {}, {}
    hits = {"github": 0, "projetos": 0}
    projs = {}
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if line.startswith('{"type":"api-request') or '"type":"file-history' in line[:60]:
                    continue
                if '"type":"assistant"' in line:  # só o que o Claude mexeu (tool_use), não contexto injetado
                    if "/github/" in line:
                        for m in GH_RE.finditer(line):
                            projs["_last"] = m.group(1)
                    hits["github"] += line.count(f"{HOME}/github/") + line.count("~/github/")
                    hits["projetos"] += line.count(f"{HOME}/projetos/") + line.count("~/projetos/")
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                typ = d.get("type")
                if typ == "ai-title":
                    info["ai_title"] = d.get("aiTitle")
                elif typ == "custom-title":
                    info["custom_title"] = d.get("customTitle")
                elif typ in ("user", "assistant"):
                    msg = d.get("message") or {}
                    content = msg.get("content")
                    if typ == "assistant" and isinstance(content, list):
                        for c in content:
                            if isinstance(c, dict) and c.get("type") == "tool_use" and c.get("name") == "Skill":
                                s = (c.get("input") or {}).get("skill")
                                if s:
                                    skills[s] = 1
                    txt = text_of(content).strip()
                    if not txt or txt.startswith("<") or d.get("isMeta"):
                        continue
                    for m in CODE_RE.finditer(txt):
                        codes[m.group(0)] = codes.get(m.group(0), 0) + 1
                    info["last_kind"] = (typ, msg.get("stop_reason"))
                    if typ == "user":
                        if info["first"] is None:
                            info["first"] = txt[:300]
                        info["last_user"] = txt[:300]
                    else:
                        info["last"] = txt[-600:]
    except OSError:
        pass
    info["hits"] = hits
    info["proj"] = projs.get("_last")
    seen = set()
    cost, ctx = usage_of(path, seen)
    for sub in path.with_suffix("").glob("subagents/*.jsonl"):  # agentes que a sessão abriu
        cost += usage_of(sub, seen)[0]
    info["cost"], info["ctx"] = round(cost, 2), ctx
    info["codes"] = sorted(codes, key=lambda k: -codes[k])[:6]
    info["skills"] = list(skills)[:8]
    _cache[str(path)] = (mtime, info)
    return info


def info_of(sid):
    tp = find_transcript(sid) if sid else None
    return parse_transcript(tp) if tp else {}


def espaco_of(cwd, conf="", info=None):
    """trabalho = ~/projetos, Paperclip e conta .claude-trabalho; pessoal = ~/github e conta .claude.
    Se a conversa mexe bem mais em ~/github do que em ~/projetos, é pessoal mesmo aberta em ~/projetos."""
    cwd = cwd or ""
    if cwd.startswith(str(HOME / "github")) or conf == ".claude":
        return "pessoal"
    h = (info or {}).get("hits") or {}
    if h.get("github", 0) >= 5 and h.get("github", 0) > 2 * h.get("projetos", 0):
        return "pessoal"
    return "trabalho"


def projetos_pessoais():
    gh = HOME / "github"
    return sorted((d.name for d in gh.iterdir() if d.is_dir() and not d.name.startswith(".")), key=str.lower) if gh.is_dir() else []


def guess_assunto(info, cwd):
    if espaco_of(cwd, info=info) == "pessoal":  # pessoal: assunto = projeto (tasks, salao, capta…)
        rel = os.path.relpath(cwd, HOME / "github") if cwd.startswith(str(HOME / "github") + "/") else ""
        return rel.split("/")[0] if rel else (info.get("proj") or "Outro")
    for s in info.get("skills", []):
        if s in SKILL_ASSUNTO:
            return SKILL_ASSUNTO[s]
    codes = info.get("codes", [])
    if any(c.startswith("DEM-") for c in codes):
        return "Demanda"
    if codes:
        return "Tarefa"
    blob = " ".join(filter(None, [info.get("first"), info.get("ai_title")])).lower()
    for word, a in (("pr ", "Revisão de PR"), ("deploy", "Deploy"), ("coolify", "Infra"), ("tarefa", "Criar tarefas")):
        if word in blob:
            return a
    return "Outro"


def live_sessions():
    out = []
    for cd in CLAUDE_DIRS:
        for f in (cd / "sessions").glob("*.json"):
            try:
                d = json.loads(f.read_text())
            except Exception:
                continue
            pid = d.get("pid")
            if not pid or not pid_alive(pid) or d.get("kind") != "interactive":
                continue
            d["_conf"] = cd.name
            normalize_status(d)
            out.append(d)
    out += orphan_claudes({d["pid"] for d in out}, out)
    return out


def normalize_status(d):
    """'shell' = terminou de responder e deixou comando em segundo plano -> esperando você.
    'busy' com a última mensagem do Claude encerrada (end_turn) há >10 s também é esperando você."""
    st = d.get("status")
    if st == "shell":
        d["status"], d["_shell"] = "idle", True
    elif st == "busy":
        tp = find_transcript(d.get("sessionId"))
        info = parse_transcript(tp) if tp else {}
        if info.get("last_kind") == ("assistant", "end_turn") and time.time() - info.get("mtime", 0) > 10:
            d["status"], d["_shell"] = "idle", True
            d["statusUpdatedAt"] = info.get("mtime", 0) * 1000


def orphan_claudes(known, registered):
    """Processos `claude` interativos sem arquivo em sessions/ (ex.: 2 janelas com --resume do mesmo id)."""
    by_sid = {d.get("sessionId"): d for d in registered}
    extra = []
    for e in os.listdir("/proc"):
        if not e.isdigit() or int(e) in known:
            continue
        pid = int(e)
        if comm(pid) != "claude" or comm(ppid(pid)) == "claude":
            continue
        try:
            argv = open(f"/proc/{pid}/cmdline").read().split("\0")
            cwd = os.readlink(f"/proc/{pid}/cwd")
        except OSError:
            continue
        if "-p" in argv or "--print" in argv:
            continue
        sid = None
        for i, a in enumerate(argv):
            if a in ("--resume", "-r", "--session-id") and i + 1 < len(argv):
                sid = argv[i + 1]
        twin = by_sid.get(sid)
        extra.append({"pid": pid, "sessionId": sid or f"sem-id-{pid}", "cwd": cwd, "status": "duplicada" if twin else "sem registro",
                      "kind": "interactive", "_conf": "", "_dup_of": twin["pid"] if twin else None})
    return extra


def build_card(sid, cwd, info, tags, live=None, conf=""):
    tag = tags.get(sid, {})
    title = tag.get("titulo") or info.get("custom_title") or info.get("ai_title") or (info.get("first") or "")[:70] or "(sem título)"
    card = {
        "id": sid, "cwd": cwd, "conf": conf, "title": title,
        "assunto": tag.get("assunto") or guess_assunto(info, cwd),
        "assunto_auto": not tag.get("assunto"),
        "espaco": tag.get("espaco") or espaco_of(cwd, conf, info),
        "espaco_auto": not tag.get("espaco"),
        "nota": tag.get("nota", ""),
        "codes": info.get("codes", []), "skills": info.get("skills", []),
        "first": info.get("first"), "last": info.get("last"), "last_user": info.get("last_user"),
        "updated": info.get("mtime", 0), "live": bool(live), "arquivada": tag.get("arquivada", False),
        "espera": tag.get("espera", False), "espera_desde": tag.get("espera_desde", 0),
        "espera_ate": tag.get("espera_ate", 0), "voltou": tag.get("voltou", 0),
        "servico": tag.get("servico", False), "chamando": tag.get("chamando", 0),
        "custo": info.get("cost", 0), "ctx": info.get("ctx", 0), "ctx_pct": round(100 * info.get("ctx", 0) / CTX_WINDOW),
        "resumo": resumo_of(info.get("last")),
    }
    if live:
        card.update(pid=live["pid"], status=live.get("status", "?"), dup_of=live.get("_dup_of"),
                    status_since=(live.get("statusUpdatedAt") or live.get("updatedAt") or 0) / 1000,
                    waiting_for=live.get("waitingFor"), shell=live.get("_shell", False),
                    updated=max(card["updated"], (live.get("updatedAt") or 0) / 1000),
                    started=(live.get("startedAt") or 0) / 1000,
                    kitty=kitty_pid_of(live["pid"]))
        if card["status"] == "waiting" and card.get("waiting_for"):
            wf = card["waiting_for"]
            card["resumo"] = "Pede confirmação: " + (wf if isinstance(wf, str) else json.dumps(wf, ensure_ascii=False))[:120]
    return card


def snapshot():
    tags = load_tags()
    cards, seen = [], set()
    for s in live_sessions():
        sid = s.get("sessionId")
        tp = find_transcript(sid) if sid else None
        info = parse_transcript(tp) if tp else {}
        cards.append(build_card(sid, s.get("cwd", ""), info, tags, live=s, conf=s["_conf"]))
        seen.add(sid)
    cutoff = time.time() - RECENT_DAYS * 86400
    for cd in CLAUDE_DIRS:
        for f in (cd / "projects").glob("*/*.jsonl"):
            sid = f.stem
            if sid in seen:
                continue
            try:
                if f.stat().st_mtime < cutoff or f.stat().st_size < 2000:
                    continue
            except OSError:
                continue
            info = parse_transcript(f)
            if not info.get("first"):
                continue
            cwd = cwd_of(f)
            cards.append(build_card(sid, cwd, info, tags, conf=cd.name))
            seen.add(sid)
    cards.sort(key=lambda c: (not c["live"], -c["updated"]))
    try:  # qual sessão está aberta ao lado do painel / com foco
        cs = clients()
        pw = painel_win(cs)
        pws = pw["workspace"]["name"] if pw else None
        act = json.loads(subprocess.run(["hyprctl", "activewindow", "-j"], capture_output=True, text=True).stdout or "{}").get("pid")
        ws_of = {w["pid"]: w["workspace"]["name"] for w in cs}
        for c in cards:
            k = c.get("kitty")
            c["aberta"] = bool(k and pws and ws_of.get(k) == pws)
            c["foco"] = bool(k and k == act)
            seen = _last_focus.get(k, 0)
            c["nao_visto"] = bool(c.get("status") in ("idle", "waiting") and seen < c.get("status_since", 0) and not c["foco"])
            if c.get("voltou") and seen > c["voltou"]:
                c["voltou"] = 0
            c["rc"] = bool(k and os.path.exists(kitty_sock(k)))
    except Exception:
        pass
    return {"conf": load_conf(), "cards": cards, "assuntos": ASSUNTOS, "assuntos_pessoal": projetos_pessoais() + ["Outro"], "agenda": sorted(load_agenda(), key=lambda i: i["quando"]), "gcal": _gcal["status"], "now": time.time(), "v": (HERE / "index.html").stat().st_mtime}


_cwd_cache = {}


def cwd_of(path):
    k = str(path)
    if k in _cwd_cache:
        return _cwd_cache[k]
    cwd = ""
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for i, line in enumerate(fh):
                m = re.search(r'"cwd":"([^"]+)"', line)
                if m:
                    cwd = m.group(1)
                    break
                if i > 50:
                    break
    except OSError:
        pass
    _cwd_cache[k] = cwd
    return cwd


def notify(title, body, fixa=False):
    try:
        subprocess.Popen(["notify-send", "-a", "Claude", "-i", "dialog-information"] + (["-u", "critical"] if fixa else []) + [title, body[:200]],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except FileNotFoundError:
        pass


_last_focus = {}  # kitty pid -> último instante com foco


def track_focus():
    try:
        act = json.loads(subprocess.run(["hyprctl", "activewindow", "-j"], capture_output=True, text=True).stdout or "{}").get("pid")
        if act:
            _last_focus[act] = time.time()
    except ValueError:
        pass


def wake_reminders(tags):
    """Espera com hora marcada venceu: volta pras ativas e avisa."""
    now, changed = time.time(), False
    for sid, t in tags.items():
        if t.get("espera") and t.get("espera_ate") and t["espera_ate"] <= now:
            t["espera"] = False
            t.pop("espera_ate", None)
            t.pop("espera_desde", None)
            t["voltou"] = now
            changed = True
            notify(f"Voltou da espera: {t.get('titulo') or sid[:8]}", t.get("nota") or "")
    if changed:
        with _lock:
            fresh = load_tags()
            for sid, t in tags.items():
                if t.get("voltou") and fresh.get(sid, {}).get("espera"):
                    fresh[sid].update(espera=False, voltou=t["voltou"])
                    fresh[sid].pop("espera_ate", None)
                    fresh[sid].pop("espera_desde", None)
            save_tags(fresh)


def kitty_sock(kp):
    return os.path.join(os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}"), f"kitty-{kp}")


# ---------------------------------------------------------------- agenda
# Itens em agenda.json: aviso (só notifica), retomar (digita/reabre uma sessão pelo id) ou nova (abre sessão com prompt).
# Rodam no watcher; vencidos com o Flow fechado rodam ao voltar se o atraso <= atraso_max_h, senão viram "perdido".
AGENDA_FILE = DATA / "agenda.json"
_agenda_lock = threading.Lock()
_agenda_wait_note = {}  # id -> já avisou que a sessão está ocupada
DIAS = {"seg": 0, "ter": 1, "qua": 2, "qui": 3, "sex": 4, "sab": 5, "sáb": 5, "dom": 6}


def load_agenda():
    try:
        return json.loads(AGENDA_FILE.read_text())
    except Exception:
        return []


def save_agenda(items):
    tmp = AGENDA_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(items, ensure_ascii=False, indent=1))
    tmp.replace(AGENDA_FILE)


def _hhmm(s):
    m = re.fullmatch(r"(\d{1,2})(?::|h)?(\d{2})?", s.strip())
    if not m:
        raise ValueError(f"hora inválida: {s}")
    h, mi = int(m.group(1)), int(m.group(2) or 0)
    if h > 23 or mi > 59:
        raise ValueError(f"hora inválida: {s}")
    return h, mi


def parse_quando(q, now=None):
    """epoch | 'AAAA-MM-DD HH:MM' | 'DD/MM HH:MM' | 'HH:MM' (hoje, ou amanhã se já passou) | 'amanhã HH:MM' | '+30m' '+2h' '+1d'."""
    import datetime as dt
    now = now or time.time()
    if isinstance(q, (int, float)):
        return float(q)
    q = str(q).strip().lower()
    base = dt.datetime.fromtimestamp(now)
    if re.fullmatch(r"\d{9,11}(\.\d+)?", q):
        return float(q)
    m = re.fullmatch(r"\+\s*(\d+)\s*(m|min|h|d)", q)
    if m:
        return now + int(m.group(1)) * {"m": 60, "min": 60, "h": 3600, "d": 86400}[m.group(2)]
    m = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})[ t](\S+)", q)
    if m:
        h, mi = _hhmm(m.group(4))
        return dt.datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)), h, mi).timestamp()
    m = re.fullmatch(r"(\d{1,2})/(\d{1,2})(?:/(\d{4}))?\s+(\S+)", q)
    if m:
        h, mi = _hhmm(m.group(4))
        return dt.datetime(int(m.group(3) or base.year), int(m.group(2)), int(m.group(1)), h, mi).timestamp()
    m = re.fullmatch(r"(hoje|amanh[ãa])\s+(\S+)", q)
    if m:
        h, mi = _hhmm(m.group(2))
        d = base + dt.timedelta(days=0 if m.group(1) == "hoje" else 1)
        return d.replace(hour=h, minute=mi, second=0, microsecond=0).timestamp()
    h, mi = _hhmm(q)
    t = base.replace(hour=h, minute=mi, second=0, microsecond=0)
    if t.timestamp() <= now:
        t += dt.timedelta(days=1)
    return t.timestamp()


def proxima(rec, depois):
    """rec: 'diario HH:MM' | 'seg-sex HH:MM' | 'seg,qua,sex HH:MM'. Próxima ocorrência > depois."""
    import datetime as dt
    m = re.fullmatch(r"\s*(\S+)\s+(\S+)\s*", (rec or "").lower())
    if not m:
        raise ValueError(f"recorrência inválida: {rec}")
    dias_s, (h, mi) = m.group(1), _hhmm(m.group(2))
    if dias_s in ("diario", "diário", "todo-dia"):
        dias = set(range(7))
    elif "-" in dias_s:
        a, b = dias_s.split("-")
        dias = set(range(DIAS[a], DIAS[b] + 1))
    else:
        dias = {DIAS[d] for d in dias_s.split(",")}
    d = dt.datetime.fromtimestamp(depois).replace(hour=h, minute=mi, second=0, microsecond=0)
    for _ in range(8):
        if d.weekday() in dias and d.timestamp() > depois:
            return d.timestamp()
        d += dt.timedelta(days=1)
    raise ValueError(f"recorrência inválida: {rec}")


ICONES = [  # palpite pelo texto quando quem agenda não manda --icone
    (r"reuni|meet|daily|call|alinhamento|1:1", "📅"), (r"cabel|barb", "✂️"), (r"m[eé]dic|consulta|exame|dentist", "🩺"),
    (r"rem[eé]dio", "💊"), (r"deploy|subir|publicar", "🚀"), (r"pagar|boleto|fatura|pix", "💸"), (r"anivers", "🎂"),
    (r"academia|treino|corrida", "🏋️"), (r"ligar|telefon", "📞"), (r"compra|mercado", "🛒"), (r"almo[cç]|jantar|caf[eé]", "🍽️"),
    (r"pr\b|pull request|revis", "🔎"), (r"d[eé]bito|cobran|banco|prod", "🏦"), (r"viagem|voo|aeroporto", "✈️"),
]


def icone_de(data, tipo):
    if data.get("icone"):
        return data["icone"]
    txt = f"{data.get('titulo') or ''} {data.get('nota') or ''} {data.get('prompt') or ''}".lower()
    for rx, ic in ICONES:
        if re.search(rx, txt):
            return ic
    return {"aviso": "⏰", "nova": "✨", "retomar": "↻"}[tipo]


def novo_item(data):
    tipo = data.get("tipo") or ("retomar" if data.get("sessao") else "aviso")
    if tipo not in ("aviso", "retomar", "nova"):
        raise ValueError("tipo: aviso | retomar | nova")
    if tipo == "retomar" and not data.get("sessao"):
        raise ValueError("retomar precisa de sessao")
    if tipo in ("retomar", "nova") and not (data.get("prompt") or "").strip():
        raise ValueError("prompt vazio")
    rec = data.get("recorrencia") or None
    quando = parse_quando(data["quando"]) if data.get("quando") else proxima(rec, time.time()) if rec else None
    if not quando:
        raise ValueError("quando vazio")
    if rec:
        proxima(rec, time.time())  # valida
    return {
        "id": uuid.uuid4().hex[:8], "quando": quando, "tipo": tipo, "icone": icone_de(data, tipo),
        "titulo": (data.get("titulo") or (data.get("prompt") or "")[:60] or "Lembrete").strip(),
        "sessao": data.get("sessao"), "cwd": data.get("cwd"), "conf": data.get("conf"),
        "prompt": data.get("prompt") or "", "nota": data.get("nota") or "",
        "recorrencia": rec, "atraso_max_h": float(data.get("atraso_max_h", 12)),
        "status": "pendente", "criado": time.time(), "criado_por": data.get("criado_por"),
        "origem": data.get("origem") or "manual", "chave": data.get("chave"), "link": data.get("link"), "log": [],
    }


def agenda_add(data):
    item = novo_item(data)
    with _agenda_lock:
        items = load_agenda()
        if item.get("chave"):  # importado (Google/Workspace): atualiza em vez de duplicar
            for it in items:
                if it.get("chave") == item["chave"]:
                    if it["status"] == "pendente":
                        it.update({k: item[k] for k in ("quando", "titulo", "nota", "link", "atraso_max_h")})
                    save_agenda(items)
                    return it
        items.append(item)
        save_agenda(items)
    return item


def agenda_update(iid, **kw):
    with _agenda_lock:
        items = load_agenda()
        for it in items:
            if it["id"] == iid:
                msg = kw.pop("_log", None)
                it.update(kw)
                if msg:
                    it["log"] = (it.get("log") or [])[-9:] + [{"t": time.time(), "msg": msg}]
                save_agenda(items)
                return it
    return None


def agenda_prune(items):
    """Mantém 7 dias de histórico (feitos/cancelados/perdidos)."""
    lim = time.time() - 7 * 86400
    return [i for i in items if i["status"] in ("pendente", "rodando", "tocando") or (i.get("fim") or i["quando"]) > lim]


def session_meta(sid):
    tp = find_transcript(sid)
    if not tp:
        return None, None
    conf = next((cd.name for cd in CLAUDE_DIRS if str(tp).startswith(str(cd))), ".claude-trabalho")
    return cwd_of(tp) or None, conf


def fmt_hora(t):
    return time.strftime("%d/%m %H:%M", time.localtime(t))


def notify_link(title, body, link):
    """Notificação com botão Abrir (mako/swaync); clique abre o link no Chrome."""
    def run():
        try:
            r = subprocess.run(["notify-send", "-a", "Claude", "-i", "x-office-calendar", "-u", "critical",
                                "-A", "default=Abrir", "-w", title, body[:200]], capture_output=True, text=True, timeout=600)
            if r.stdout.strip() == "default":
                subprocess.Popen(["xdg-open", link], start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except (OSError, subprocess.TimeoutExpired):
            pass
    threading.Thread(target=run, daemon=True).start()


def agenda_exec(it, live):
    """Executa um item vencido. Devolve (status, msg) ou None se tem que tentar de novo depois."""
    now = time.time()
    atraso = now - it["quando"]
    cab = f"[Agenda do Flow: {it['titulo']} · marcado p/ {fmt_hora(it['quando'])}"
    cab += f", rodando com {int(atraso // 60)} min de atraso]" if atraso > 300 else "]"
    if it["tipo"] == "aviso":
        corpo = it.get("nota") or ""
        if it.get("link"):
            notify_link(f"⏰ {it['titulo']}", corpo or it["link"], it["link"])
        else:
            notify(f"⏰ {it['titulo']}", corpo, fixa=True)
        if it.get("origem") == "google":  # aviso de 1 min substitui o de 10 min da mesma reunião
            ev = (it.get("chave") or "").rsplit(":", 1)[0]
            with _agenda_lock:
                items = load_agenda()
                for o in items:
                    if o["id"] != it["id"] and o["status"] == "tocando" and (o.get("chave") or "").startswith(ev + ":"):
                        o.update(status="feito", fim=now)
                save_agenda(items)
        tocar_alarme()
        return "tocando", "avisado"
    prompt = f"{cab} {it['prompt']}"
    if it["tipo"] == "retomar":
        s = next((x for x in live if x.get("sessionId") == it["sessao"] and not x.get("_shell")), None)
        if s:
            kp = kitty_pid_of(s["pid"])
            pronta = s.get("status") == "idle" and kp and os.path.exists(kitty_sock(kp)) and not input_has_text(kp)
            if not pronta:
                if not _agenda_wait_note.get(it["id"]):
                    _agenda_wait_note[it["id"]] = True
                    notify(f"Agenda esperando sessão livre: {it['titulo']}", "a sessão está ocupada ou com texto digitado")
                return None
            ok, err = send_text(s["pid"], prompt)
            return ("feito", "digitado na sessão aberta") if ok else ("erro", f"send_text: {err}")
        cwd, conf = session_meta(it["sessao"])
        cwd, conf = it.get("cwd") or cwd, it.get("conf") or conf
        ok = launch(cwd, it["titulo"], resume=it["sessao"], prompt=prompt, conf=conf)
        return ("feito", "sessão reaberta") if ok else ("erro", f"não abriu (cwd {cwd})")
    sid = str(uuid.uuid4())
    ok = launch(it.get("cwd"), it["titulo"], prompt=prompt, session_id=sid, conf=it.get("conf"))
    if ok:
        with _lock:
            tags = load_tags()
            tags.setdefault(sid, {})["titulo"] = it["titulo"]
            save_tags(tags)
        agenda_update(it["id"], sessao_criada=sid)
        return "feito", f"sessão nova {sid[:8]}"
    return "erro", f"não abriu (cwd {it.get('cwd')})"


SOM_ALARME = "/usr/share/sounds/freedesktop/stereo/alarm-clock-elapsed.oga"
SOM_REPETE = 120  # s entre repetições enquanto não der OK


def tocar_alarme():
    try:
        subprocess.Popen(["paplay", SOM_ALARME], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    except FileNotFoundError:
        pass


def agenda_tick(live):
    now = time.time()
    tocando = [i for i in load_agenda() if i["status"] == "tocando"]
    if tocando and now - max(i.get("som", 0) for i in tocando) >= SOM_REPETE:
        tocar_alarme()
        for i in tocando:
            agenda_update(i["id"], som=now)
    for it in load_agenda():
        if it["status"] != "pendente" or it["quando"] > now:
            continue
        atraso = now - it["quando"]
        if atraso > it.get("atraso_max_h", 12) * 3600 + 120:
            nxt = proxima(it["recorrencia"], now) if it.get("recorrencia") else None
            if nxt:
                agenda_update(it["id"], quando=nxt, _log=f"pulado ({fmt_hora(it['quando'])}, atraso grande)")
            else:
                agenda_update(it["id"], status="perdido", fim=now, _log="atraso maior que o limite")
                if it["tipo"] != "aviso":
                    notify(f"Agenda perdida: {it['titulo']}", f"era {fmt_hora(it['quando'])}; rode pelo Flow se ainda fizer sentido")
            continue
        agenda_update(it["id"], status="rodando")
        try:
            r = agenda_exec(it, live)
        except Exception as e:
            r = ("erro", str(e)[:200])
        if r is None:
            agenda_update(it["id"], status="pendente")
            continue
        _agenda_wait_note.pop(it["id"], None)
        st, msg = r
        if st == "erro":
            notify(f"Agenda falhou: {it['titulo']}", msg)
        if st == "tocando":
            agenda_update(it["id"], status="tocando", tocou=now, som=now, _log=msg)
            if it.get("recorrencia"):  # o próximo nasce como item novo; este fica tocando até o OK
                nxt = dict(it, id=uuid.uuid4().hex[:8], quando=proxima(it["recorrencia"], now), status="pendente", log=[])
                with _agenda_lock:
                    items = load_agenda(); items.append(nxt); save_agenda(items)
            continue
        if it.get("recorrencia") and st != "erro":
            agenda_update(it["id"], status="pendente", quando=proxima(it["recorrencia"], now), ultimo=now,
                          _log=msg + (" (atrasado)" if atraso > 300 else ""))
        else:
            agenda_update(it["id"], status="atrasado" if st == "feito" and atraso > 300 else st, fim=now, _log=msg)
    with _agenda_lock:
        items = load_agenda()
        pr = agenda_prune(items)
        if len(pr) != len(items):
            save_agenda(pr)



# ---------------------------------------------------------------- reuniões (Google Agenda via Workspace)
# A cada 5 min lê GET /me/calendar/events do Workspace (msw-api, chave pessoal) e mantém avisos 10 e 1 min antes.
# Avisos dessas reuniões nunca rodam depois do início (atraso_max_h = até a hora da reunião).
GCAL_EVERY = 300
GCAL_MINUTOS = (10, 1)
_gcal = {"last": 0.0, "erro_aviso": 0.0, "status": "nunca rodou"}


def _iso(t):
    import datetime as dt
    return dt.datetime.fromtimestamp(t, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _ts(iso):
    import datetime as dt
    return dt.datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()


def gcal_fetch(now):
    path = f"/me/calendar/events?from={_iso(now - 600)}&to={_iso(now + 36 * 3600)}"
    r = subprocess.run(["msw-api", "GET", path], capture_output=True, text=True, timeout=30)
    code = (re.search(r"HTTP (\d+)", r.stderr) or [None, "?"])[1]
    if code != "200":
        raise RuntimeError(f"HTTP {code}")
    d = json.loads(r.stdout)
    return d.get("data", d)  # API v1 responde em {data: ...}


def gcal_sync():
    now = time.time()
    try:
        d = gcal_fetch(now)
    except Exception as e:
        _gcal["status"] = f"erro: {e}"[:120]
        return
    if not d.get("connected") or d.get("problem"):
        _gcal["status"] = "Google desconectado no Workspace" if not d.get("connected") else f"problema: {d.get('problem')}"
        if now - _gcal["erro_aviso"] > 6 * 3600:
            _gcal["erro_aviso"] = now
            notify("Flow: agenda sem Google", "Reconecte o Google Agenda no Workspace (/agenda) para receber avisos de reunião")
        return
    vistos = set()
    for ev in d.get("events", []):
        if ev.get("allDay") or ev.get("declined"):
            continue
        ini = _ts(ev["start"])
        if ini <= now:
            continue
        link = ev.get("meetUrl") or ev.get("link")
        for m in GCAL_MINUTOS:
            q = ini - m * 60
            chave = f"gcal:{ev['id']}:{m}"
            vistos.add(chave)
            if q < now - 60:
                continue
            agenda_add({"tipo": "aviso", "icone": "📅", "quando": q, "titulo": f"{ev['title']} · {'agora' if m <= 1 else f'em {m} min'}",
                        "nota": f"Começa às {time.strftime('%H:%M', time.localtime(ini))}" + (" · clique para entrar" if ev.get("meetUrl") else ""),
                        "link": link, "chave": chave, "origem": "google", "atraso_max_h": max(0.0, (ini - q) / 3600)})
    # evento cancelado/movido pra fora da janela: some da agenda
    with _agenda_lock:
        items = load_agenda()
        mudou = False
        for it in items:
            if it.get("origem") == "google" and it["status"] == "pendente" and it.get("chave") not in vistos and it["quando"] < now + 36 * 3600:
                it.update(status="cancelado", fim=now)
                mudou = True
        if mudou:
            save_agenda(items)
    _gcal["status"] = f"ok {time.strftime('%H:%M')} · {len(d.get('events', []))} eventos"


def gcal_loop():
    while True:
        try:
            if load_conf().get("agenda_google", True):
                gcal_sync()
        except Exception as e:
            print("gcal:", e, flush=True)
        _gcal["last"] = time.time()
        time.sleep(GCAL_EVERY)


KEYMAP = {" ": "space", "-": "minus", ".": "period", ",": "comma", "/": "slash"}


def send_text(claude_pid, text):
    """Digita text + Enter na sessão. Socket do kitty se existir; senão tecla a tecla pelo Hyprland (só ASCII simples)."""
    kp = kitty_pid_of(claude_pid)
    if not kp:
        return False, "janela não achada"
    sock = kitty_sock(kp)
    if os.path.exists(sock):
        # texto longo vira "colagem" no Claude e engole o \r: manda o Enter separado depois
        r = subprocess.run(["kitty", "@", "--to", f"unix:{sock}", "send-text", "--", text], capture_output=True, text=True)
        if r.returncode == 0:
            time.sleep(0.4)
            r = subprocess.run(["kitty", "@", "--to", f"unix:{sock}", "send-key", "enter"], capture_output=True, text=True)
        if r.returncode == 0:
            _last_focus[kp] = time.time()
            return True, None
    keys = []
    for ch in text:
        if ch.isascii() and (ch.isalnum() or ch in KEYMAP):
            keys.append(("SHIFT" if ch.isupper() else "", KEYMAP.get(ch, ch.lower())))
        else:
            return False, "texto com acento/símbolo: reabra a sessão pelo painel pra liberar"
    keys.append(("", "Return"))
    lua = "\n".join(f'hl.dispatch(hl.dsp.send_shortcut({{ mods = "{m}", key = "{k}", window = "pid:{kp}" }}))' for m, k in keys)
    ok = hypr_eval(lua)
    if ok:
        _last_focus[kp] = time.time()
    return ok, None if ok else "falhou"


def servico_step(sid, prev, st, tags):
    """Modo serviço (ex.: criador-de-tarefas): fica em espera; só aparece quando chama você (/api/chamar)
    ou pede confirmação; terminou um turno sem chamar -> volta pra espera sozinha."""
    t = tags[sid]
    now = time.time()
    if st == "busy" and prev != "busy":
        _busy_since[sid] = now
    mudou = False
    if st == "waiting" and prev != "waiting" and t.get("espera"):
        t["espera"] = False; t["chamando"] = now; mudou = True
    elif st == "idle" and prev == "busy":
        chamou = t.get("chamando", 0) >= _busy_since.get(sid, now)
        if not chamou and not t.get("espera"):
            t["espera"] = True; t["espera_desde"] = now; t.pop("chamando", None); mudou = True
    if mudou:
        with _lock:
            fresh = load_tags()
            fresh.setdefault(sid, {}).update({k: t[k] for k in ("espera", "espera_desde", "chamando") if k in t})
            if "chamando" not in t:
                fresh[sid].pop("chamando", None)
            if not t.get("espera"):
                fresh[sid].pop("espera_desde", None)
            save_tags(fresh)


_agenda_last = [0.0]


def watcher():
    """Avisa quando sessão viva passa de busy -> idle."""
    while True:
        try:
            tags = load_tags()
            live = live_sessions()
            track_focus()
            wake_reminders(tags)
            if time.time() - _agenda_last[0] >= 5:
                _agenda_last[0] = time.time()
                agenda_tick(live)
            prev_all = dict(_last_pid_status)
            for s in live:
                _last_pid_status[s["pid"]] = s.get("status")
            for s in live:
                sid, st = s.get("sessionId"), s.get("status")
                prev = _last_status.get(sid)
                if tags.get(sid, {}).get("servico"):
                    servico_step(sid, prev, st, tags)
                if prev == "busy" and st == "idle" and not tags.get(sid, {}).get("espera"):
                    tp = find_transcript(sid)
                    info = parse_transcript(tp) if tp else {}
                    title = tags.get(sid, {}).get("titulo") or info.get("custom_title") or info.get("ai_title") or s.get("name")
                    notify(f"Claude terminou: {title}", (info.get("last") or "")[-200:])
                if st == "waiting" and prev != "waiting" and not tags.get(sid, {}).get("espera"):
                    tp = find_transcript(sid)
                    info = parse_transcript(tp) if tp else {}
                    title = tags.get(sid, {}).get("titulo") or info.get("custom_title") or info.get("ai_title") or s.get("name")
                    notify(f"Claude pede confirmação: {title}", str(s.get("waitingFor") or "aguardando seu Enter"))
                _last_status[sid] = st
            auto_advance(live, tags, prev_all)
        except Exception as e:
            print("watcher:", e, flush=True)
        time.sleep(1)


CONF_FILE = DATA / "config.json"


def load_conf():
    try:
        return {"auto": True, **json.loads(CONF_FILE.read_text())}
    except Exception:
        return {"auto": True}


def set_conf(**kw):
    c = load_conf()
    c.update(kw)
    CONF_FILE.write_text(json.dumps(c))


def mic_active():
    """Algum app gravando do microfone (modo voz do Claude, ditado…). O Chrome não conta: é o Meet, não ditado."""
    try:
        r = subprocess.run(["pactl", "-f", "json", "list", "source-outputs"], capture_output=True, text=True, timeout=2)
        outs = json.loads(r.stdout or "[]")
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return False
    return any(o.get("properties", {}).get("application.process.binary") not in ("chrome", "google-chrome", "chromium")
               for o in outs)


def user_idle():
    """hypridle cria o arquivo após 8 s sem teclado/mouse e apaga quando você mexe."""
    return os.path.exists(os.path.join(os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}"), "painel-ocioso"))


def input_has_text(kp):
    """Lê a tela do kitty (se tiver socket) e vê se a caixa de mensagem do Claude tem texto digitado."""
    sock = kitty_sock(kp)
    if not os.path.exists(sock):
        return False
    try:
        r = subprocess.run(["kitty", "@", "--to", f"unix:{sock}", "get-text"], capture_output=True, text=True, timeout=2)
    except (OSError, subprocess.TimeoutExpired):
        return False
    lines = r.stdout.splitlines()
    rules = [i for i, l in enumerate(lines) if l.strip().startswith("─" * 10)]
    for a, b in zip(rules[-2::-1], rules[:0:-1]):  # pares de régua, de baixo pra cima
        if b - a >= 2:
            txt = " ".join(lines[a + 1:b]).replace("\xa0", " ").strip()
            txt = txt.lstrip("❯>").strip()
            return bool(txt) and not txt.startswith('Try "')
    return False


def auto_advance(live, tags, prev):
    """Sessão aberta ao lado do painel passou de esperando -> trabalhando (você respondeu): mostra a próxima que espera você."""
    if not load_conf().get("auto"):
        return
    cs = clients()
    pw = painel_win(cs)
    if not pw:
        return
    pws = pw["workspace"]["name"]
    ws_of = {w["pid"]: w["workspace"]["name"] for w in cs}
    by_kitty = {}
    for x in live:
        k = kitty_pid_of(x["pid"])
        if k:
            by_kitty[k] = x
    shown = [(k, x) for k, x in by_kitty.items() if ws_of.get(k) == pws]
    pin = load_conf().get("pin")
    if pin and pin not in {x.get("sessionId") for x in live}:  # sessão fixada fechou: solta o pino
        set_conf(pin=None)
        pin = None
    if pin and len(shown) == 1 and shown[0][1].get("sessionId") == pin:
        return  # fixada: não troca sozinho
    last = _shown_last.get("kitty")
    if len(shown) == 1:
        _shown_last["kitty"] = shown[0][0]
    if not shown and last and last not in ws_of:
        # a sessão que estava aberta foi fechada (Super+Q ou botão Fechar): abre a próxima, mesmo que esteja trabalhando
        _shown_last["kitty"] = None
        cur, rank = None, {"waiting": 0, "idle": 1, "busy": 2}
    elif len(shown) == 1:
        cur = shown[0][1]
        respondeu = prev.get(cur["pid"]) in ("idle", "waiting") and cur.get("status") == "busy"
        if respondeu:
            _shown_last["fixa"] = False
        # você está falando/digitando nela: não troca
        if mic_active() or input_has_text(shown[0][0]):
            return
        # troca se: você acabou de responder, ou a aberta está só trabalhando (não foi escolhida por você)
        # — nesse segundo caso, só depois de 8 s sem teclado/mouse
        if not (respondeu or (cur.get("status") == "busy" and not _shown_last.get("fixa") and user_idle())):
            return
        rank = {"waiting": 0, "idle": 1}
    else:
        return
    cands = [(rank[x["status"]], (x.get("statusUpdatedAt") or 0) * (-1 if x["status"] == "busy" else 1), k)
             for k, x in by_kitty.items()
             if x is not cur and x.get("status") in rank and not x.get("_dup_of")
             and not tags.get(x.get("sessionId"), {}).get("espera") and not tags.get(x.get("sessionId"), {}).get("arquivada")
             and (tags.get(x.get("sessionId"), {}).get("espaco") or espaco_of(x.get("cwd"), x.get("_conf"), info_of(x.get("sessionId")))) == load_conf().get("espaco", "trabalho")]
    if cands:
        cands.sort()
        show(cands[0][2])
        _shown_last["kitty"] = cands[0][2]
        _shown_last["fixa"] = False


SPLIT = float(os.environ.get("PAINEL_SPLIT", "0.6"))  # modo lado: 0.6 => painel ~30% da largura
BAR = float(os.environ.get("PAINEL_BAR", "0.16"))      # modo topo: barra ~8% da altura (~110 px no 1440p)
BAR_OPEN = 0.9                                         # barra expandida: ~45% da altura
PARK_WS = os.environ.get("PAINEL_PARK_WS", "10")  # pra onde vai a sessão que sai do lado do painel


def hypr(expr):
    r = subprocess.run(["hyprctl", "dispatch", expr], capture_output=True, text=True)
    return r.returncode == 0 and "error" not in (r.stdout + r.stderr).lower()


def clients():
    try:
        return json.loads(subprocess.run(["hyprctl", "clients", "-j"], capture_output=True, text=True).stdout)
    except ValueError:
        return []


def painel_win(cs):
    for w in cs:
        if w.get("class", "").startswith("chrome-127.0.0.1__") or w.get("title") == "Flow":
            return w
    return None


def move(pid, ws):
    return hypr(f'hl.dsp.window.move({{ workspace = "{ws}", window = "pid:{pid}", follow = false }})')


def hypr_eval(code):
    r = subprocess.run(["hyprctl", "eval", code], capture_output=True, text=True)
    return r.returncode == 0 and "error" not in (r.stdout + r.stderr).lower()


def painel_ratio(conf):
    if conf.get("layout", "topo") == "topo":
        return BAR_OPEN if conf.get("aberta") else BAR
    return SPLIT


def arranged(pw, ws_wins, want, topo):
    """Janelas do workspace do painel já são exatamente `want` e estão no lugar (abaixo / à direita do painel)."""
    if [w["pid"] for w in ws_wins] != want:  # ws_wins vem ordenado da esquerda pra direita
        return False
    if topo:
        ok = all(w["at"][1] > pw["at"][1] and w["at"][0] >= pw["at"][0] - 2 for w in ws_wins)
        return ok and pw["size"][0] >= sum(w["size"][0] for w in ws_wins) - 2
    return all(w["at"][0] > pw["at"][0] for w in ws_wins)


def show(kp, lado=None):
    """Modo topo: painel como barra fina em cima, sessão(ões) embaixo.
    lado=None: só ela (1 tela; se já está aberta, só foca). lado="esq"/"dir": entra nesse lado,
    substituindo quem estava lá (ou trocando de lado, se já estava aberta), e a outra fica.
    Modo lado: painel à esquerda, sessão à direita. Outras janelas do workspace vão pro PARK_WS.
    Tudo num único eval Lua (sem frame intermediário)."""
    cs = clients()
    pw = painel_win(cs)
    if not pw:
        return hypr(f'hl.dsp.focus({{ window = "pid:{kp}" }})')
    if not any(w["pid"] == kp for w in cs):
        return False
    conf = load_conf()
    topo = conf.get("layout", "topo") == "topo"
    pws = pw["workspace"]["name"]
    ws_wins = sorted((w for w in cs if w["workspace"]["name"] == pws and w["pid"] != pw["pid"]),
                     key=lambda w: (w["at"][0], w["at"][1]))
    cur = [w["pid"] for w in ws_wins if w["class"] == "kitty"][:2]
    others = [p for p in cur if p != kp]
    if lado in ("esq", "dir") and others:
        if len(cur) == 2 and kp not in cur:  # substitui a do lado escolhido
            other = cur[1] if lado == "esq" else cur[0]
        else:
            other = others[0]
        want = [kp, other] if lado == "esq" else [other, kp]
    elif kp in cur:
        want = cur
    else:
        want = [kp]
    if arranged(pw, ws_wins, want, topo):
        _last_focus[kp] = time.time()
        return hypr(f'hl.dsp.focus({{ window = "pid:{kp}" }})')
    lua = ['local fs = hl.get_config("dwindle.force_split")',
           'local sr = hl.get_config("dwindle.default_split_ratio")']
    lua += [f'hl.dispatch(hl.dsp.window.move({{ workspace = "{PARK_WS}", window = "pid:{w["pid"]}", follow = false }}))' for w in ws_wins]
    lua += [f'hl.dispatch(hl.dsp.focus({{ window = "pid:{pw["pid"]}" }}))',
            f'hl.config({{ dwindle = {{ force_split = 2, default_split_ratio = {painel_ratio(conf)} }} }})',
            f'hl.dispatch(hl.dsp.window.move({{ workspace = "{pws}", window = "pid:{want[0]}", follow = false }}))']
    if len(want) > 1:
        lua += [f'hl.dispatch(hl.dsp.focus({{ window = "pid:{want[0]}" }}))',
                'hl.config({ dwindle = { default_split_ratio = 1.0 } })',
                f'hl.dispatch(hl.dsp.window.move({{ workspace = "{pws}", window = "pid:{want[1]}", follow = false }}))']
    lua += ['hl.config({ dwindle = { force_split = fs, default_split_ratio = sr } })']
    if topo:  # o dwindle divide o painel (largo) lado a lado: vira em cima/embaixo
        lua += [f'hl.dispatch(hl.dsp.focus({{ window = "pid:{pw["pid"]}" }}))', 'hl.dispatch(hl.dsp.layout("togglesplit"))']
    lua.append(f'hl.dispatch(hl.dsp.focus({{ window = "pid:{kp}" }}))')
    ok = hypr_eval("\n".join(lua))
    _last_focus[kp] = time.time()
    if topo:  # rede de segurança: se o split já veio em cima/embaixo, o toggle deixou lado a lado
        cs = clients()
        pw = painel_win(cs)
        k = next((w for w in cs if w["pid"] == want[0]), None)
        if pw and k and k["at"][1] <= pw["at"][1]:
            hypr_eval(f'hl.dispatch(hl.dsp.focus({{ window = "pid:{pw["pid"]}" }}))\nhl.dispatch(hl.dsp.layout("togglesplit"))\n'
                      f'hl.dispatch(hl.dsp.focus({{ window = "pid:{kp}" }}))')
    return ok


def set_bar(aberta):
    """Expande/recolhe a barra do topo sem refazer o layout."""
    set_conf(aberta=bool(aberta))
    cs = clients()
    pw = painel_win(cs)
    if not pw or load_conf().get("layout", "topo") != "topo":
        return False
    act = json.loads(subprocess.run(["hyprctl", "activewindow", "-j"], capture_output=True, text=True).stdout or "{}").get("pid")
    lua = [f'hl.dispatch(hl.dsp.focus({{ window = "pid:{pw["pid"]}" }}))',
           f'hl.dispatch(hl.dsp.layout("splitratio {BAR_OPEN if aberta else BAR} exact"))']
    if act and act != pw["pid"] and not aberta:
        lua.append(f'hl.dispatch(hl.dsp.focus({{ window = "pid:{act}" }}))')
    return hypr_eval("\n".join(lua))


def relayout():
    """Reaplica o layout atual (trocou topo/lado ou desligou as 2 telas)."""
    cs = clients()
    pw = painel_win(cs)
    if not pw:
        return False
    ks = [w for w in cs if w["workspace"]["name"] == pw["workspace"]["name"] and w["class"] == "kitty"]
    if not ks:
        return False
    ks.sort(key=lambda w: w["at"][0])
    if len(ks) >= 2:  # refaz mantendo esquerda/direita
        show(ks[0]["pid"])
        return show(ks[1]["pid"], lado="dir")
    return show(ks[0]["pid"])


def focus(card_pid, lado=None):
    _shown_last["fixa"] = True  # escolhida à mão: não trocar enquanto ela só estiver trabalhando
    kp = kitty_pid_of(int(card_pid))
    if not kp:
        return False
    if load_conf().get("aberta"):
        set_conf(aberta=False)
    return show(kp, lado)


def show_new_kitty(before, timeout=8):
    """Espera o kitty novo aparecer e coloca ao lado do painel."""
    end = time.time() + timeout
    while time.time() < end:
        time.sleep(0.4)
        for w in clients():
            if w["class"] == "kitty" and w["pid"] not in before:
                time.sleep(0.3)
                show(w["pid"])
                return


def launch(cwd, title, resume=None, prompt=None, session_id=None, conf=None):
    cwd = os.path.expanduser(cwd or str(HOME / "projetos"))
    if not os.path.isdir(cwd):
        return False
    cmd = "claude"
    if resume:
        cmd += f" --resume {shlex_quote(resume)}"
    else:
        if session_id:
            cmd += f" --session-id {session_id}"
        if title:
            cmd += f" -n {shlex_quote(title)}"
    if prompt:
        cmd += " " + shlex_quote(prompt)
    env = dict(os.environ)
    if resume and conf == ".claude":
        # conta pessoal está sem plano: copia a conversa pra conta trabalho e retoma lá (original fica intacto)
        src = next((HOME / ".claude" / "projects").glob(f"*/{resume}.jsonl"), None)
        if src:
            dst = HOME / ".claude-trabalho" / "projects" / src.parent.name / src.name
            if not dst.exists():
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)
        conf = ".claude-trabalho"
    env["CLAUDE_CONFIG_DIR"] = str(HOME / (conf if conf in (".claude", ".claude-trabalho") else ".claude-trabalho"))
    args = ["kitty", "--directory", cwd]
    if title:
        args += ["--title", title]
    args += ["bash", "-ic", f"{cmd}; exec bash"]
    before = {w["pid"] for w in clients()}
    threading.Thread(target=show_new_kitty, args=(before,), daemon=True).start()
    subprocess.Popen(args, env=env, start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return True


def shlex_quote(s):
    import shlex
    return shlex.quote(s)


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            body = (HERE / "index.html").read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path.startswith("/api/agenda"):
            self._json({"agenda": sorted(load_agenda(), key=lambda i: i["quando"]), "now": time.time()})
        elif self.path.startswith("/api/sessions"):
            with _lock:
                self._json(snapshot())
        else:
            self._json({"erro": "404"}, 404)

    def do_POST(self):
        # só aceita da própria página
        if self.headers.get("X-Painel") != "1":
            return self._json({"erro": "proibido"}, 403)
        n = int(self.headers.get("Content-Length") or 0)
        try:
            data = json.loads(self.rfile.read(n) or b"{}")
        except ValueError:
            return self._json({"erro": "json"}, 400)
        if self.path == "/api/tag":
            sid = data.get("id")
            if not sid:
                return self._json({"erro": "id"}, 400)
            with _lock:
                tags = load_tags()
                t = tags.setdefault(sid, {})
                for k in ("titulo", "assunto", "nota", "arquivada", "espera", "espaco", "espera_ate", "servico"):
                    if k in data:
                        v = data[k]
                        if v in ("", None):
                            t.pop(k, None)
                        else:
                            t[k] = v
                if t.get("espera") and not t.get("espera_desde"):
                    t["espera_desde"] = time.time()
                if not t.get("espera"):
                    t.pop("espera_desde", None)
                    t.pop("espera_ate", None)
                t.pop("voltou", None) if "espera" in data else None
                save_tags(tags)
            return self._json({"ok": True})
        if self.path == "/api/config":
            c = load_conf()
            if "auto" in data:
                c["auto"] = bool(data["auto"])
            if data.get("espaco") in ("trabalho", "pessoal"):
                c["espaco"] = data["espaco"]
            if "pin" in data:
                c["pin"] = data["pin"] or None
            relay = False
            if data.get("layout") in ("topo", "lado"):
                c["layout"] = data["layout"]; c["aberta"] = False; relay = True
            CONF_FILE.write_text(json.dumps(c))
            if relay:
                relayout()
            if "aberta" in data:
                set_bar(data["aberta"])
            return self._json(load_conf())
        if self.path == "/api/send":
            ok, err = send_text(int(data.get("pid") or 0), data.get("text") or "")
            return self._json({"ok": ok, "erro": err})
        if self.path == "/api/close":
            kp = kitty_pid_of(int(data.get("pid") or 0))
            return self._json({"ok": bool(kp) and hypr(f'hl.dsp.window.close({{ window = "pid:{kp}" }})')})
        if self.path == "/api/focus":
            return self._json({"ok": focus(data.get("pid", 0), data.get("lado"))})
        if self.path == "/api/chamar":
            # sessão em modo serviço pede você: sai da espera e avisa; volta sozinha quando terminar sem chamar de novo
            sid = data.get("id")
            if not sid:
                return self._json({"erro": "id"}, 400)
            with _lock:
                tags = load_tags()
                t = tags.setdefault(sid, {})
                t["chamando"] = time.time()
                t["espera"] = False
                t.pop("espera_desde", None); t.pop("espera_ate", None)
                if data.get("motivo"):
                    t["nota"] = data["motivo"]
                save_tags(tags)
            notify(f"Pede você: {t.get('titulo') or sid[:8]}", data.get("motivo") or "")
            return self._json({"ok": True})
        if self.path == "/api/agenda":
            try:
                return self._json({"ok": True, "item": agenda_add(data)})
            except (ValueError, KeyError) as e:
                return self._json({"erro": str(e)}, 400)
        if self.path == "/api/agenda/ok":
            return self._json({"ok": bool(agenda_update(data.get("id"), status="feito", fim=time.time(), _log="ok"))})
        if self.path == "/api/agenda/soneca":
            q = time.time() + 60 * int(data.get("min") or 10)
            return self._json({"ok": bool(agenda_update(data.get("id"), status="pendente", quando=q, atraso_max_h=12, _log="soneca"))})
        if self.path in ("/api/agenda/cancelar", "/api/agenda/rodar", "/api/agenda/hora"):
            iid = data.get("id")
            if self.path.endswith("cancelar"):
                it = agenda_update(iid, status="cancelado", fim=time.time(), _log="cancelado")
            elif self.path.endswith("rodar"):
                it = agenda_update(iid, status="pendente", quando=time.time(), atraso_max_h=12, _log="rodar agora")
            else:
                try:
                    it = agenda_update(iid, status="pendente", quando=parse_quando(data.get("quando")), _log="hora alterada")
                except ValueError as e:
                    return self._json({"erro": str(e)}, 400)
            return self._json({"ok": bool(it), "item": it})
        if self.path == "/api/launch":
            sid = data.get("resume") or str(uuid.uuid4())
            ok = launch(data.get("cwd"), data.get("titulo"), data.get("resume"), data.get("prompt"),
                        None if data.get("resume") else sid, data.get("conf"))
            if ok:
                with _lock:
                    tags = load_tags()
                    t = tags.setdefault(sid, {})
                    t.pop("arquivada", None)
                    for k in ("titulo", "assunto"):
                        if data.get(k):
                            t[k] = data[k]
                    save_tags(tags)
            return self._json({"ok": ok, "id": sid})
        self._json({"erro": "404"}, 404)


def main():
    # item que estava rodando quando o Flow caiu volta pra fila (pode repetir 1x; melhor que perder)
    items = load_agenda()
    if any(i["status"] == "rodando" for i in items):
        for i in items:
            if i["status"] == "rodando":
                i["status"] = "pendente"
        save_agenda(items)
    threading.Thread(target=watcher, daemon=True).start()
    threading.Thread(target=gcal_loop, daemon=True).start()
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), H)
    print(f"painel em http://127.0.0.1:{PORT}")
    srv.serve_forever()


if __name__ == "__main__":
    main()
