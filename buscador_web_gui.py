# -*- coding: utf-8 -*-
# Buscador Inteligente v2.5 — consultas atómicas + reranking + exports robustos
# Build: 2025-08-12

import threading, re, math, random, webbrowser, tkinter as tk, csv
from datetime import datetime
from urllib.parse import urlparse
import requests, pandas as pd
import ttkbootstrap as tb
from ttkbootstrap.constants import PRIMARY, SUCCESS, WARNING, OUTLINE, INFO
from ttkbootstrap.scrolled import ScrolledFrame

# Toast
try:
    from ttkbootstrap.toast import ToastNotification
    TOAST_AVAILABLE = True
except Exception:
    from tkinter import messagebox
    ToastNotification = None
    TOAST_AVAILABLE = False

# ddg
try:
    from ddgs import DDGS
except ImportError:
    DDGS = None

from bs4 import BeautifulSoup
import trafilatura
from urllib import robotparser

# ---------- Red con reintentos ----------
try:
    from requests.adapters import HTTPAdapter
    from urllib3.util.retry import Retry
    _HAS_RETRY = True
except Exception:
    _HAS_RETRY = False

USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
              "AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/124.0.0.0 Safari/537.36")

DEFAULT_ACADEMIC_DOMAINS = [
    "scielo.org","redalyc.org","dialnet.unirioja.es","researchgate.net",
    "ieeexplore.ieee.org","springer.com","link.springer.com","mdpi.com",
    "nature.com","jstor.org","tandfonline.com","sciencedirect.com",
    "elsevier.com","dl.acm.org","sagepub.com","cambridge.org","oup.com","wiley.com"
]

SPECIAL_SOURCES = {
    "YouTube": ["youtube.com","youtu.be"],
    "GitHub": ["github.com"],
    "Stack Overflow": ["stackoverflow.com"],
    "Reddit": ["reddit.com"],
    "PDFs": ["filetype:pdf"],
}

THEME_PRESETS = ["cosmo","morph","flatly","journal","superhero","darkly","vapor"]

SYN_BASE = {
    "aplicaciones móviles": ['"aplicaciones móviles"','"apps móviles"','"mobile apps"','"mobile applications"'],
    "fidelización": ['"fidelización"','"retención de clientes"','"customer loyalty"','"customer retention"'],
    "inteligencia artificial": ['"inteligencia artificial"','"IA"','"AI"','"artificial intelligence"'],
    "aprendizaje automático": ['"aprendizaje automático"','"machine learning"','"ML"'],
    "transformación digital": ['"transformación digital"','"digitalización"','"digital transformation"'],
    "ciberseguridad": ['"ciberseguridad"','"seguridad informática"','"cybersecurity"'],
    "erp": ['"ERP"','"enterprise resource planning"'],
    "odoo": ['"odoo"','"odoo erp"'],
    "educación": ['"educación"','"education"'],
    "agricultura": ['"agricultura"','"agriculture"','"farming"'],
    "minería": ['"minería"','"mining industry"','"metallurgy"'],
}

def build_session():
    s = requests.Session()
    s.headers.update({"User-Agent": USER_AGENT, "Accept-Language": "es-ES,es;q=0.9"})
    if _HAS_RETRY:
        retry = Retry(total=3, backoff_factor=0.4,
                      status_forcelist=(429,500,502,503,504),
                      allowed_methods=False)
        adapter = HTTPAdapter(max_retries=retry, pool_connections=10, pool_maxsize=20)
        s.mount("http://", adapter); s.mount("https://", adapter)
    return s

def robots_allowed(url: str, user_agent: str = USER_AGENT) -> bool:
    try:
        p = urlparse(url); rp = robotparser.RobotFileParser()
        rp.set_url(f"{p.scheme}://{p.netloc}/robots.txt"); rp.read()
        return rp.can_fetch(user_agent, url)
    except Exception:
        return True

def clean_text(t: str) -> str:
    return re.sub(r"\s+", " ", t or "").strip()

def _norm_host(h: str) -> str:
    if not h: return ""
    h = h.split(":")[0].strip().lower()
    return h[4:] if h.startswith("www.") else h

def url_host(u: str) -> str:
    try: return _norm_host(urlparse(u).netloc)
    except Exception: return ""

def is_probably_xml(html: str, content_type: str) -> bool:
    ct = (content_type or "").lower()
    if "xml" in ct or "rss" in ct or "atom" in ct: return True
    head = (html or "").lstrip()[:80].lower()
    return head.startswith("<?xml") or "<rss" in head or "<feed" in head

def extract_main_text(html: str, url: str, content_type: str) -> str:
    if not html: return ""
    try:
        if not is_probably_xml(html, content_type):
            ex = trafilatura.extract(html, include_comments=False, include_tables=False, url=url)
            if ex: return clean_text(ex)
    except Exception: pass
    try:
        parser = "lxml-xml" if is_probably_xml(html, content_type) else "lxml"
        soup = BeautifulSoup(html, parser)
        if parser != "lxml-xml":
            for t in soup(["script","style","noscript","header","footer","form","nav","aside"]): t.decompose()
            return clean_text(soup.get_text(" "))
        items = soup.find_all(["item","entry"]) or soup.find_all()
        texts = []
        for it in items:
            title = getattr(it.find("title"), "text", None) or ""
            desc = getattr(it.find("description") or it.find("summary"), "text", None) or ""
            if title or desc: texts.append(clean_text(f"{title} {desc}"))
        return clean_text(" ".join(texts)) if texts else clean_text(soup.get_text(" "))
    except Exception:
        return ""

def normalize(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip().lower())

def themed_synonyms(theme_text: str) -> list[str]:
    t = normalize(theme_text); out = set()
    for k, vals in SYN_BASE.items():
        if k in t: out.update(vals)
    if not out and t:
        out.add(f'"{t}"'); 
        if not t.endswith("s"): out.add(f'"{t}s"')
        hints = {"tesis":"tesis","pregrado":"undergraduate","posgrado":"postgraduate","cliente":"customer",
                 "clientes":"customers","empresas":"companies","moviles":"mobile","móviles":"mobile"}
        for es,en in hints.items():
            if es in t: out.add(f'"{en}"')
    return list(out)

# --------- consultas atómicas + scoring ----------
def build_atomic_queries(theme_text: str, extra: str, site_host: str|None, domains: list[str]) -> list[str]:
    syns = themed_synonyms(theme_text) or [f'"{theme_text}"']
    extras = extra.strip()
    base_terms = []
    for s in syns:
        base_terms.append(s if not extras else f"{s} ({extras})")
    queries = []

    # sitio específico: 1 consulta por sinónimo
    if site_host:
        for t in base_terms:
            queries.append(f"{t} site:{site_host}")
        return queries

    # sin sitio: también probamos .edu/.org
    for t in base_terms:
        queries.append(t)
        queries.append(f"{t} site:.edu OR site:.org")
    # dominios elegidos por el usuario (si los hay)
    if domains:
        site_filter = " OR ".join(f"site:{_norm_host(d)}" for d in domains if d)
        for t in base_terms:
            queries.append(f"{t} ({site_filter})")
    # dedup conservando orden
    seen, out = set(), []
    for q in queries:
        if q not in seen: out.append(q); seen.add(q)
    return out

def ddg_text_search(query: str, max_results: int = 20, region="es-es", safesearch="moderate"):
    items = []
    if DDGS is None: return items
    try:
        with DDGS() as ddgs:
            for r in ddgs.text(query, region=region, safesearch=safesearch, max_results=max_results):
                url = r.get("href") or r.get("url")
                if not url or not url.startswith("http"): continue
                items.append({"title": r.get("title") or "", "url": url, "snippet": r.get("body") or ""})
    except Exception:
        pass
    return items

def tokenize_for_score(theme_text: str, extra: str):
    txt = f"{theme_text} {extra}".lower()
    tokens = [w.strip('"\':,.()[]') for w in re.split(r"[\s/|]+", txt) if len(w.strip('"\':,.()[]'))>=3]
    return list(dict.fromkeys(tokens))  # únicos y ordenados

def score_item(item, tokens, preferred_host=None):
    t = (item.get("title","") or "").lower()
    s = (item.get("snippet","") or "").lower()
    u = (item.get("url","") or "").lower()
    sc = 0.0
    for k in tokens:
        sc += 3*t.count(k) + 1.5*s.count(k) + 1.0*u.count(k)
    h = url_host(item.get("url",""))
    if preferred_host and (h == preferred_host or h.endswith("."+preferred_host)): sc += 3
    if h.endswith(".edu") or h.endswith(".org"): sc += 1
    return sc

def search_bundle(queries: list[str], n: int, site_only: str|None, domains: list[str], specials: dict):
    # traigo MUCHO y luego reranqueo
    pool = []
    for q in queries:
        # pido 3x lo solicitado para tener margen de filtrado
        for r in ddg_text_search(q, max_results=max(n*3, 60)):
            r["source"] = "Sitio específico" if site_only else ("Académico/Filtrado" if domains else "General")
            pool.append(r)

    # especiales
    for name, enabled in specials.items():
        if not enabled: continue
        if name == "PDFs":
            for q in queries:
                pool += [{"title":x.get("title",""),"url":x["url"],"snippet":x.get("body",""),"source":"PDFs"}
                         for x in ddg_text_search(f"{q} filetype:pdf", max_results=max(n*2, 40))]
        else:
            sites = " OR ".join(f"site:{d}" for d in SPECIAL_SOURCES.get(name, []))
            for q in queries:
                pool += [{"title":x.get("title",""),"url":x["url"],"snippet":x.get("body",""),"source":name}
                         for x in ddg_text_search(f"{q} ({sites})", max_results=max(n*2, 40))]

    # dedup global
    dedup = {}
    for it in pool:
        base = it["url"].split("#")[0]
        if base not in dedup: dedup[base] = it
    items = list(dedup.values())

    # scoring
    tokens = tokenize_for_score(queries[0], "")  # usa tema inicial implícito
    # si queries[0] no trae todo, igual añadimos tokens del resto
    extra_tokens = set()
    for q in queries[1:]:
        extra_tokens.update(tokenize_for_score(q, ""))
    tokens = list(dict.fromkeys(tokens + list(extra_tokens)))

    preferred = site_only
    scored = [(score_item(it, tokens, preferred), it) for it in items]
    scored.sort(key=lambda x: x[0], reverse=True)
    top = [it for sc,it in scored]

    # filtro: quita ruido extremo, pero asegura mínimo n resultados base
    if top:
        top_score = score_item(top[0], tokens, preferred)
        thr = max(1.0, 0.18*top_score)   # 18% del top o 1.0
        filtered = [it for it in top if score_item(it, tokens, preferred) >= thr]
        if len(filtered) < n: filtered = top  # garantía de cantidad
    else:
        filtered = []

    # quota final por fuente
    out = []
    def take(source, k):
        taken = [it for it in filtered if it.get("source")==source][:k]
        out.extend(taken)
    if site_only:
        take("Sitio específico", n)
    else:
        take("Académico/Filtrado" if domains else "General", n)
        for name in ["YouTube","GitHub","Stack Overflow","Reddit","PDFs"]:
            if specials.get(name): take(name, n)
    return out

# ---------- Extracción ----------
def fetch_and_extract(url: str, session: requests.Session, keywords: list[str]) -> dict:
    data = {
        "ok": False, "url": url, "host": url_host(url), "title": "",
        "summary": "", "text_length": 0, "reason": "", "source": "",
        "content_type": "", "status_code": None, "snippet": "",
        "fetched_at": datetime.utcnow().isoformat(timespec="seconds")+"Z"
    }
    try:
        if not robots_allowed(url, USER_AGENT):
            data["reason"] = "Bloqueado por robots.txt"; return data
        resp = session.get(url, timeout=20, allow_redirects=True)
        data["status_code"] = resp.status_code
        ctype = (resp.headers.get("Content-Type") or "").lower()
        data["content_type"] = ctype
        if "pdf" in ctype or url.lower().endswith(".pdf"):
            data["title"] = url.split("/")[-1]; data["summary"] = "(Documento PDF)"
            data["ok"] = True; return data

        html = resp.text
        parser = "lxml-xml" if is_probably_xml(html, ctype) else "lxml"
        try:
            soup_title = BeautifulSoup(html, parser)
            tt = soup_title.find("title")
            if tt and tt.text: data["title"] = clean_text(tt.text)
        except Exception: pass

        main_text = extract_main_text(html, url, ctype)
        data["text_length"] = len(main_text)
        data["summary"] = summarize_text(main_text, keywords, max_sentences=3)
        if not data["title"]: data["title"] = clean_text(urlparse(url).netloc)
        data["ok"] = True; return data
    except requests.RequestException as e:
        data["reason"] = f"HTTP: {e.__class__.__name__}"; return data
    except Exception as e:
        data["reason"] = f"Error: {e.__class__.__name__}"; return data

def summarize_text(text: str, keywords: list[str], max_sentences: int = 3) -> str:
    if not text: return ""
    sents = re.split(r'(?<=[\.\!\?])\s+', text)
    kw = [k.lower() for k in keywords if k]
    scored = []
    for s in sents:
        low = s.lower()
        score = sum(2 if f" {k} " in f" {low} " else (1 if k in low else 0) for k in kw)
        ln = len(s.split()); 
        if ln < 6 or ln > 60: score *= 0.6
        if score > 0: scored.append((score, s))
    scored.sort(key=lambda x: x[0], reverse=True)
    chosen = [s for _, s in scored[:max_sentences]]
    if not chosen:
        for s in sents:
            if 8 <= len(s.split()) <= 40:
                chosen.append(s); 
                if len(chosen)==max_sentences: break
    return " ".join(chosen)

# ---------- UI ----------
class ConfettiCanvas(tk.Canvas):
    def __init__(self, master, width=400, height=80, **kwargs):
        super().__init__(master, width=width, height=height, highlightthickness=0, **kwargs)
        self.particles = []; 
        try: self._colors = master.winfo_toplevel().style.colors
        except Exception: self._colors = None
    def start(self):
        self.delete("all"); self.particles = []
        for _ in range(48):
            x = random.randint(10, int(self["width"])-10)
            y = random.randint(-60, -10); r = random.randint(2, 5)
            vy = random.uniform(1.5, 3.8); vx = random.uniform(-1.2, 1.2)
            self.particles.append([x,y,r,vx,vy])
        self.animate()
    def animate(self):
        self.delete("all"); alive = False
        for p in self.particles:
            p[0]+=p[3]; p[1]+=p[4]
            if p[1] < int(self["height"])+10: alive = True
            color = self._colors.success if self._colors else ""
            self.create_oval(p[0],p[1],p[0]+p[2],p[1]+p[2], outline=color)
        if alive: self.after(16, self.animate)

class ScraperGUI:
    def __init__(self):
        self.app = tb.Window(title="Buscador Inteligente • Web Scraping", themename="darkly", size=(1120, 780))
        self.style = self.app.style
        self.results, self.raw_links = [], []
        self.stop_flag = threading.Event()

        # Header
        header = tb.Frame(self.app, padding=12); header.pack(fill=tk.X)
        tb.Label(header, text="🔎 Buscador Inteligente", font=("Segoe UI", 20, "bold")).pack(side=tk.LEFT)
        self.status_label = tb.Label(header, text="Listo", bootstyle="secondary"); self.status_label.pack(side=tk.RIGHT)

        # Toolbar
        toolbar = tb.Frame(self.app, padding=(12,4)); toolbar.pack(fill=tk.X)
        tb.Label(toolbar, text="Tema (lo que buscas):").grid(row=0,column=0,sticky=tk.E,padx=6,pady=6)
        self.entry_theme = tb.Entry(toolbar, width=48); self.entry_theme.grid(row=0,column=1,sticky=tk.EW,padx=6,pady=6)
        tb.Label(toolbar, text="Palabras extra (opcional):").grid(row=0,column=2,sticky=tk.E,padx=6)
        self.entry_keywords = tb.Entry(toolbar, width=34); self.entry_keywords.grid(row=0,column=3,sticky=tk.EW,padx=6,pady=6)

        self.academic_var = tk.BooleanVar(value=True)
        tb.Checkbutton(toolbar, text="Modo académico (dominios .edu/.org y editoriales)",
                       variable=self.academic_var, command=self._on_academic_toggle).grid(row=0,column=4,sticky=tk.W,padx=6)

        tb.Label(toolbar, text="Dominios (coma)").grid(row=1,column=0,sticky=tk.E,padx=6)
        self.entry_domains = tb.Entry(toolbar); self.entry_domains.grid(row=1,column=1,sticky=tk.EW,padx=6)
        tb.Label(toolbar, text="Resultados").grid(row=1,column=2,sticky=tk.E,padx=6)
        self.spin_results = tb.Spinbox(toolbar, from_=10, to=100, increment=10, width=8)
        self.spin_results.insert(0, "30"); self.spin_results.grid(row=1,column=3,sticky=tk.W,padx=6)

        self.theme_choice = tk.StringVar(value="darkly")
        tb.Label(toolbar, text="Apariencia").grid(row=1,column=4,sticky=tk.E,padx=(12,6))
        theme_box = tb.Combobox(toolbar, state="readonly", values=THEME_PRESETS, textvariable=self.theme_choice, width=12)
        theme_box.grid(row=1,column=5,sticky=tk.W)
        theme_box.bind("<<ComboboxSelected>>", lambda e: self._switch_theme())
        for c in range(6): toolbar.grid_columnconfigure(c, weight=1 if c in (1,3) else 0)

        # Sitio específico
        sitebar = tb.Frame(self.app, padding=(12,0)); sitebar.pack(fill=tk.X)
        tb.Label(sitebar, text="Sitio específico").grid(row=0,column=0,sticky=tk.E,padx=6,pady=6)
        self.entry_site = tb.Entry(sitebar, width=40); self.entry_site.insert(0, ""); self.entry_site.grid(row=0,column=1,sticky=tk.EW,padx=6,pady=6)
        tb.Button(sitebar, text="Buscar solo este sitio", bootstyle=PRIMARY,
                  command=lambda: self.on_search(site_only=True)).grid(row=0,column=2,padx=6)
        sitebar.grid_columnconfigure(1, weight=1)

        # Botones
        btns = tb.Frame(self.app, padding=(12,0)); btns.pack(fill=tk.X)
        self.btn_buscar = tb.Button(btns, text="Buscar", bootstyle=PRIMARY, command=self.on_search); self.btn_buscar.pack(side=tk.LEFT,padx=6)
        self.btn_scrap = tb.Button(btns, text="Rastrear y extraer", bootstyle=SUCCESS, command=self.on_scrape, state=tk.DISABLED); self.btn_scrap.pack(side=tk.LEFT,padx=6)
        self.btn_stop = tb.Button(btns, text="Detener", bootstyle=(WARNING, OUTLINE), command=self.on_stop, state=tk.DISABLED); self.btn_stop.pack(side=tk.LEFT,padx=6)

        # Especiales
        special = tb.Labelframe(self.app, text="Búsquedas especiales (puedes marcar varias)", padding=10, bootstyle="info")
        special.pack(fill=tk.X, padx=12, pady=(8,0))
        self.var_special = {name: tk.BooleanVar(value=False) for name in SPECIAL_SOURCES.keys()}
        for i,name in enumerate(["YouTube","GitHub","Stack Overflow","Reddit","PDFs"]):
            tb.Checkbutton(special, text=name, variable=self.var_special[name]).grid(row=0,column=i,padx=8,pady=4,sticky=tk.W)

        # Progreso
        prog = tb.Frame(self.app, padding=(12,0)); prog.pack(fill=tk.X)
        self.progress = tb.Progressbar(prog, mode="determinate"); self.progress.pack(fill=tk.X,padx=4,pady=4)
        self.marquee = tb.Progressbar(prog, mode="indeterminate")
        self.confetti = ConfettiCanvas(prog, width=1024, height=60); self.confetti.pack(fill=tk.X,padx=4,pady=(2,6))

        # Resultados (cards)
        body = ScrolledFrame(self.app, padding=12, autohide=True); body.pack(fill=tk.BOTH, expand=True)
        self.cards_container = tb.Frame(body); self.cards_container.pack(fill=tk.BOTH, expand=True)

        # Footer
        footer = tb.Frame(self.app, padding=12); footer.pack(fill=tk.X)
        self.btn_table = tb.Button(footer, text="Ver tabla", bootstyle=INFO, command=self.show_table, state=tk.DISABLED)
        self.btn_export_csv = tb.Button(footer, text="Exportar CSV (;)", bootstyle=INFO, command=self.export_csv, state=tk.DISABLED)
        self.btn_export_xlsx = tb.Button(footer, text="Exportar Excel (.xlsx)", bootstyle=INFO, command=self.export_xlsx, state=tk.DISABLED)
        self.btn_export_jsonl = tb.Button(footer, text="Exportar JSONL", bootstyle=INFO, command=self.export_jsonl, state=tk.DISABLED)
        for b in (self.btn_table,self.btn_export_csv,self.btn_export_xlsx,self.btn_export_jsonl): b.pack(side=tk.LEFT,padx=4)

        self._pulse = 0; self._animate_header(); self._on_academic_toggle(first_time=True)

    # ---- helpers UI ----
    def _switch_theme(self):
        try: self.app.style.theme_use(self.theme_choice.get())
        except Exception: pass

    def _animate_header(self):
        self._pulse += 0.08
        try:
            base = self.style.colors
            col = base.primary if (math.sin(self._pulse)+1)/2 > 0.5 else base.info
            self.status_label.configure(bootstyle=col)
        except Exception: pass
        self.app.after(480, self._animate_header)

    def _on_academic_toggle(self, first_time=False):
        if self.academic_var.get():
            if first_time or not self.entry_domains.get().strip():
                self.entry_domains.delete(0, tk.END)
                self.entry_domains.insert(0, ", ".join(DEFAULT_ACADEMIC_DOMAINS))
        else:
            if self.entry_domains.get().strip() == ", ".join(DEFAULT_ACADEMIC_DOMAINS):
                self.entry_domains.delete(0, tk.END)

    def set_status(self, text, style="secondary"):
        try: self.status_label.configure(text=text, bootstyle=style)
        except Exception: self.status_label.configure(text=text)

    def toast(self, title, message):
        if TOAST_AVAILABLE and ToastNotification:
            try:
                ToastNotification(title=title, message=message, duration=2800, position=(20, 80, "ne")).show_toast(); return
            except Exception: pass
        try: messagebox.showinfo(title, message)
        except Exception: print(f"[{title}] {message}")

    def clear_cards(self):
        for w in self.cards_container.winfo_children(): w.destroy()

    def _mk_copy_button(self, parent, url):
        def _copy():
            try:
                self.app.clipboard_clear(); self.app.clipboard_append(url)
                self.toast("Copiado", "URL copiada al portapapeles")
            except Exception: pass
        return tb.Button(parent, text="Copiar URL", bootstyle=OUTLINE, command=_copy)

    def create_card(self, idx, title, url, snippet="", summary="", length=0, ok=True, reason="", source="General"):
        card = tb.Frame(self.cards_container, padding=12, bootstyle="light"); card.pack(fill=tk.X, pady=8)
        head = tb.Frame(card); head.pack(fill=tk.X)
        tb.Label(head, text=f"{idx}. {title or '(Sin título)'}", font=("Segoe UI", 12, "bold")).pack(side=tk.LEFT)
        tb.Label(head, text=url, bootstyle="secondary").pack(side=tk.RIGHT)
        meta = tb.Frame(card); meta.pack(fill=tk.X, pady=(2,6))
        tb.Label(meta, text=source, bootstyle="inverse-info").pack(side=tk.LEFT, padx=(0,8))
        if ok: tb.Label(meta, text=f"Extraído ✓ • {length} chars", bootstyle="success").pack(side=tk.LEFT)
        else:  tb.Label(meta, text=f"Falló ✗ • {reason}", bootstyle="danger").pack(side=tk.LEFT)
        actions = tb.Frame(card); actions.pack(fill=tk.X, pady=(2,6))
        tb.Button(actions, text="Abrir", bootstyle=PRIMARY, command=lambda u=url: webbrowser.open(u)).pack(side=tk.LEFT, padx=(0,6))
        self._mk_copy_button(actions, url).pack(side=tk.LEFT)
        if snippet: tb.Label(card, text=snippet, wraplength=980).pack(fill=tk.X, pady=(2,4))
        if summary:
            tb.Label(card, text="Resumen:", bootstyle="secondary").pack(anchor=tk.W)
            tb.Label(card, text=summary, wraplength=980).pack(fill=tk.X)

    # ---- Acciones ----
    def on_search(self, site_only=False):
        if DDGS is None:
            self.toast("Falta dependencia", "Instala: pip install ddgs"); return
        theme_text = self.entry_theme.get().strip()
        extras_text = self.entry_keywords.get().strip()
        if not theme_text and not extras_text:
            self.toast("Falta tema", "Escribe un tema o palabra clave."); return

        user_domains = [d.strip() for d in self.entry_domains.get().split(",") if d.strip()]
        try: n = int(self.spin_results.get())
        except Exception: n = 30

        site_host = None
        if site_only:
            raw = self.entry_site.get().strip()
            if not raw:
                self.toast("Falta sitio", "Ej.: repositorio.uncp.edu.pe"); return
            site_host = urlparse("http://" + raw if "://" not in raw else raw).netloc or raw
            site_host = _norm_host(site_host)

        domains = user_domains if (self.academic_var.get() or user_domains) else []
        specials = {k: v.get() for k,v in self.var_special.items()}

        self.set_status("Buscando…", "info")
        self.btn_buscar.configure(state=tk.DISABLED)
        self.btn_scrap.configure(state=tk.DISABLED)
        self.clear_cards(); self.progress.configure(value=0, maximum=100)
        self.marquee.start(9)

        def run():
            queries = build_atomic_queries(theme_text, extras_text, site_host, domains)
            links = search_bundle(queries, n=n, site_only=site_host, domains=domains, specials=specials)

            # dedup final
            dedup = {}
            for it in links:
                base = it["url"].split("#")[0]; dedup.setdefault(base, it)
            self.raw_links = list(dedup.values())

            self.results = []
            self.app.after(0, self.render_links)
            self.app.after(0, lambda: self.set_status(f"{len(self.raw_links)} enlaces tras filtrado", "success"))
            self.app.after(0, lambda: self.btn_scrap.configure(state=tk.NORMAL if self.raw_links else tk.DISABLED))
            self.app.after(0, lambda: self.btn_buscar.configure(state=tk.NORMAL))
            self.app.after(0, self.marquee.stop)
            if not self.raw_links:
                self.app.after(0, lambda: self.toast("Sin resultados", "Prueba menos comillas o más sinónimos."))
        threading.Thread(target=run, daemon=True).start()

    def render_links(self):
        self.clear_cards()
        for i,it in enumerate(self.raw_links, start=1):
            self.create_card(i, it.get("title",""), it["url"], snippet=it.get("snippet",""), ok=True, length=0, source=it.get("source","General"))

    def on_stop(self):
        self.stop_flag.set(); self.set_status("Deteniendo…", "warning")

    def on_scrape(self):
        if not self.raw_links:
            self.toast("Nada que extraer", "Primero realiza una búsqueda."); return
        self.stop_flag.clear(); self.results = []
        for b in (self.btn_scrap,self.btn_buscar): b.configure(state=tk.DISABLED)
        self.btn_stop.configure(state=tk.NORMAL)
        for b in (self.btn_table,self.btn_export_csv,self.btn_export_xlsx,self.btn_export_jsonl): b.configure(state=tk.DISABLED)
        self.progress.configure(value=0, maximum=len(self.raw_links))
        self.marquee.start(10); self.set_status("Extrayendo contenido…", "primary")

        syn = themed_synonyms(self.entry_theme.get())
        kw = syn + [k for k in re.split(r"[,\s]+", self.entry_keywords.get()) if k]

        def run():
            session = build_session(); out = []
            for idx,it in enumerate(self.raw_links, start=1):
                if self.stop_flag.is_set(): break
                d = fetch_and_extract(it["url"], session, kw)
                d["source"] = it.get("source","General"); d["snippet"] = it.get("snippet","")
                if not d.get("title"): d["title"] = it.get("title","")
                out.append(d)
                self.app.after(0, lambda i=idx, data=d: self.update_card(i, data))
                self.app.after(0, lambda v=idx: self.progress.configure(value=v))

            self.results = out
            self.app.after(0, self.marquee.stop)
            self.app.after(0, lambda: self.btn_stop.configure(state=tk.DISABLED))
            self.app.after(0, lambda: self.btn_buscar.configure(state=tk.NORMAL))
            self.app.after(0, lambda: self.btn_scrap.configure(state=tk.NORMAL))
            if not self.stop_flag.is_set():
                self.app.after(0, lambda: self.set_status("Listo", "success"))
                self.app.after(0, self.confetti.start)
                for b in (self.btn_table,self.btn_export_csv,self.btn_export_xlsx,self.btn_export_jsonl):
                    self.app.after(0, lambda bb=b: bb.configure(state=tk.NORMAL))
                self.app.after(100, lambda: self.toast("Completado", "Extracción finalizada."))
            else:
                self.app.after(0, lambda: self.set_status("Proceso detenido por el usuario.", "warning"))
        threading.Thread(target=run, daemon=True).start()

    def update_card(self, idx, data):
        self.clear_cards()
        for i,it in enumerate(self.raw_links, start=1):
            if i < idx:
                dmatch = next((r for r in self.results if r.get("url")==it["url"]), None)
                if dmatch:
                    self.create_card(i, dmatch.get("title",""), dmatch["url"], snippet=dmatch.get("snippet",""),
                                     summary=dmatch.get("summary",""), length=dmatch.get("text_length",0),
                                     ok=dmatch.get("ok",False), reason=dmatch.get("reason",""), source=dmatch.get("source","General"))
                else:
                    self.create_card(i, it.get("title",""), it["url"], snippet=it.get("snippet",""), ok=True, length=0, source=it.get("source","General"))
            elif i == idx:
                self.create_card(i, data.get("title",""), data["url"], snippet=data.get("snippet",""),
                                 summary=data.get("summary",""), length=data.get("text_length",0),
                                 ok=data.get("ok",False), reason=data.get("reason",""), source=data.get("source","General"))
            else:
                self.create_card(i, it.get("title",""), it["url"], snippet=it.get("snippet",""), ok=True, length=0, source=it.get("source","General"))

    # ---- Tabla y export ----
    def _results_dataframe(self) -> pd.DataFrame:
        cols = ["title","url","host","source","content_type","status_code","fetched_at","snippet","summary","text_length"]
        return pd.DataFrame([{c: r.get(c,"") for c in cols} for r in self.results], columns=cols)

    def show_table(self):
        if not self.results: return
        try:
            df = self._results_dataframe()
            top = tb.Toplevel(self.app, title="Vista en tabla", size=(1100, 600))
            top.transient(self.app); top.grab_set(); top.lift(); 
            try:
                top.attributes("-topmost", True); top.after(200, lambda: top.attributes("-topmost", False))
            except Exception: pass
            container = tb.Frame(top, padding=8); container.pack(fill=tk.BOTH, expand=True)
            cols = list(df.columns)
            tree = tb.Treeview(container, columns=cols, show="headings")
            vs = tb.Scrollbar(container, orient="vertical", command=tree.yview)
            hs = tb.Scrollbar(container, orient="horizontal", command=tree.xview)
            tree.configure(yscrollcommand=vs.set, xscrollcommand=hs.set)
            tree.grid(row=0,column=0,sticky="nsew"); vs.grid(row=0,column=1,sticky="ns"); hs.grid(row=1,column=0,sticky="ew")
            container.grid_rowconfigure(0, weight=1); container.grid_columnconfigure(0, weight=1)
            for c in cols: tree.heading(c, text=c); tree.column(c, width=160, stretch=True)
            for _,row in df.iterrows(): tree.insert("", tk.END, values=[row[c] for c in cols])
            tb.Label(top, text="Sugerencia: usa Exportar Excel para un informe limpio.").pack(pady=6)
        except Exception as e:
            self.toast("Error al abrir tabla", str(e))

    def export_csv(self):
        if not self.results:
            self.toast("Nada que exportar", "Primero realiza una extracción."); return
        try:
            ts = datetime.now().strftime("%Y%m%d-%H%M%S")
            fname = f"scraping_resultados_{ts}.csv"
            df = self._results_dataframe()
            df.to_csv(fname, index=False, encoding="utf-8-sig", sep=";", quoting=csv.QUOTE_ALL, quotechar='"', line_terminator="\n")
            self.toast("CSV exportado", f"Guardado como {fname}")
        except Exception as e:
            self.toast("Error al exportar CSV", str(e))

    def export_xlsx(self):
        if not self.results:
            self.toast("Nada que exportar", "Primero realiza una extracción."); return
        ts = datetime.now().strftime("%Y%m%d-%H%M%S"); fname = f"scraping_resultados_{ts}.xlsx"
        df_links = pd.DataFrame(self.raw_links, columns=["title","url","snippet","source"])
        df_data  = self._results_dataframe()
        try:
            engine = None
            try:
                import openpyxl  # noqa
                engine = "openpyxl"
            except Exception:
                try:
                    import xlsxwriter  # noqa
                    engine = "xlsxwriter"
                except Exception:
                    raise RuntimeError("Instala openpyxl o xlsxwriter para exportar Excel.")
            with pd.ExcelWriter(fname, engine=engine) as writer:
                df_links.to_excel(writer, index=False, sheet_name="Enlaces")
                df_data.to_excel(writer, index=False, sheet_name="Extracciones")
                if engine == "xlsxwriter":
                    for sheet_name, df_tmp in [("Enlaces", df_links), ("Extracciones", df_data)]:
                        ws = writer.sheets[sheet_name]
                        for i,col in enumerate(df_tmp.columns):
                            maxlen = max([len(str(col))] + [len(str(x)) for x in df_tmp[col].astype(str).head(120)])
                            ws.set_column(i, i, min(max(12, maxlen+2), 80))
            self.toast("Excel exportado", f"Guardado como {fname}")
        except Exception as e:
            self.toast("Error al exportar Excel", str(e))

    def export_jsonl(self):
        if not self.results:
            self.toast("Nada que exportar", "Primero realiza una extracción."); return
        try:
            import json
            ts = datetime.now().strftime("%Y%m%d-%H%M%S"); fname = f"scraping_resultados_{ts}.jsonl"
            with open(fname, "w", encoding="utf-8") as f:
                for r in self.results: f.write(json.dumps(r, ensure_ascii=False) + "\n")
            self.toast("JSONL exportado", f"Guardado como {fname}")
        except Exception as e:
            self.toast("Error al exportar JSONL", str(e))

    def run(self): self.app.mainloop()

# ---- Main ----
if __name__ == "__main__":
    print("Buscador Inteligente v2.5 (ddgs) • listo")
    ScraperGUI().run()
