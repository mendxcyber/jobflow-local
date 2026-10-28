from __future__ import annotations

import hashlib
import html
import json
import os
import re
import sqlite3
import time
import unicodedata
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import quote, urljoin, urlparse
from email.utils import parsedate_to_datetime
import xml.etree.ElementTree as ET

import requests
from bs4 import BeautifulSoup
from fastapi import FastAPI, Form, HTTPException, Request, Response
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from pydantic import BaseModel
from pypdf import PdfReader
from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import HRFlowable, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle


APP_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.getenv("DATA_DIR", "/app/data"))
OUTPUT_DIR = Path(os.getenv("OUTPUT_DIR", "/app/output"))
CONFIG_DIR = Path(os.getenv("CONFIG_DIR", "/app/config"))
DB_PATH = DATA_DIR / "riovagas.db"
MAX_PAGES = int(os.getenv("MAX_PAGES", "3"))
REQUEST_DELAY = float(os.getenv("REQUEST_DELAY_SECONDS", "0.8"))
TEMPLATE_VERSION = "ats-v2.2-compactnames"
SOURCE_COOLDOWNS: dict[str, datetime] = {}

app = FastAPI(title="Automação RioVagas — JobFlow")


def load_json(name: str) -> dict[str, Any]:
    path = CONFIG_DIR / name
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def normalize(value: str) -> str:
    value = unicodedata.normalize("NFKD", value or "")
    value = "".join(ch for ch in value if not unicodedata.combining(ch))
    return re.sub(r"\s+", " ", value).strip().lower()


def safe_filename(value: str, max_len: int = 95) -> str:
    value = re.sub(r'[<>:"/\\|?*]', " ", value)
    value = re.sub(r"\s+", " ", value).strip(" .")
    return value[:max_len].rstrip()


def short_job_filename(value: str, max_role_len: int = 16) -> str:
    value = value.replace("–", "-").replace("—", "-").replace("‑", "-")
    value = re.split(r"\s+-\s+|\s+\|\s+", value, maxsplit=1)[0]
    value = re.sub(r"(?i)^\s*(?:vaga|oportunidade)\s+(?:de|para)\s+", "", value)
    value = re.sub(r"(?i)\bR\$\s*[\d.,]+.*$", "", value)
    value = re.sub(r"(?i)\((?:a|o|as|os|m|f|mf|m/f)\)", "", value)
    value = re.sub(r"(?i)\b(?:home\s*office|remoto|remota|presencial|h[ií]brido|h[ií]brida)\b", "", value)
    value = re.sub(r"(?i)\b(?:rio de janeiro|zona oeste|rj)\b", "", value)
    value = unicodedata.normalize("NFKD", value)
    value = "".join(ch for ch in value if not unicodedata.combining(ch))
    value = re.sub(r"[^A-Za-z0-9]+", " ", value).strip()
    normalized = value.lower()
    categories = [
        (("ciberseguranca", "cyber", "seguranca da informacao"), "Ciberseguranca"),
        (("backoffice", "back office"), "Backoffice"),
        (("telemarketing", "call center"), "Telemarketing"),
        (("administrativo", "administrativa", "administracao"), "Administrativo"),
        (("suporte", "help desk", "service desk"), "Suporte"),
        (("atendimento", "atendente", "sac"), "Atendimento"),
        (("vendedor", "vendedora", "vendas", "comercial", "promotor"), "Vendas"),
        (("informatica", "tecnologia", "sistemas"), "TI"),
    ]
    for terms, category in categories:
        if any(term in normalized for term in terms):
            return f"CV_{category}.pdf"

    stopwords = {"de", "da", "do", "das", "dos", "para", "em", "no", "na", "e"}
    words: list[str] = []
    for word in (item for item in value.split() if item.lower() not in stopwords):
        candidate = "_".join(words + [word])
        if len(candidate) > max_role_len:
            break
        words.append(word)
    role = "_".join(words) or "Vaga"
    return f"CV_{role}.pdf"


def get_db() -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS jobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            url TEXT UNIQUE NOT NULL,
            source TEXT NOT NULL DEFAULT 'RioVagas',
            company TEXT,
            location TEXT,
            title TEXT NOT NULL,
            cargo TEXT,
            published_at TEXT,
            salary REAL,
            contract_type TEXT,
            work_mode TEXT,
            decision TEXT NOT NULL,
            reason TEXT NOT NULL,
            score INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'encontrada',
            pdf_path TEXT,
            description TEXT,
            discovered_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    existing_columns = {row[1] for row in conn.execute("PRAGMA table_info(jobs)").fetchall()}
    for column, definition in {
        "source": "TEXT NOT NULL DEFAULT 'RioVagas'",
        "company": "TEXT",
        "location": "TEXT",
        "template_version": "TEXT",
        "hidden": "INTEGER NOT NULL DEFAULT 0",
        "apply_state": "TEXT NOT NULL DEFAULT 'ready'",
        "apply_message": "TEXT",
        "apply_attempted_at": "TEXT",
    }.items():
        if column not in existing_columns:
            conn.execute(f"ALTER TABLE jobs ADD COLUMN {column} {definition}")
    conn.commit()
    return conn


def request_session() -> requests.Session:
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 Chrome/129 Safari/537.36"
            ),
            "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.7",
        }
    )
    return session


def is_riovagas_job_url(url: str) -> bool:
    parsed = urlparse(url)
    if "riovagas.com.br" not in parsed.netloc:
        return False
    path = parsed.path.rstrip("/")
    return bool(re.match(r"^/(riovagas|vagas-externas)/[^/]+$", path))


def collect_riovagas_links(session: requests.Session, source: dict[str, Any]) -> list[dict[str, str]]:
    found: dict[str, str] = {}
    base_url = source["url"]
    pages = min(int(source.get("paginas", MAX_PAGES)), MAX_PAGES)
    for page in range(1, pages + 1):
        page_url = base_url if page == 1 else urljoin(base_url, f"page/{page}/")
        try:
            response = session.get(page_url, timeout=30)
            response.raise_for_status()
        except requests.RequestException:
            continue
        soup = BeautifulSoup(response.text, "html.parser")
        for anchor in soup.select("a[href]"):
            url = urljoin(page_url, anchor.get("href", ""))
            if not is_riovagas_job_url(url):
                continue
            title = " ".join(anchor.stripped_strings)
            if len(title) < 8:
                parent = anchor.find_parent(["h2", "h3", "article"])
                title = " ".join(parent.stripped_strings) if parent else title
            if len(title) >= 8:
                found[url.rstrip("/") + "/"] = title[:300]
        time.sleep(REQUEST_DELAY)
    return [{"url": url, "title": title, "source": source["nome"], "source_type": source["tipo"]} for url, title in found.items()]


HTML_PATTERNS = {
    "vagas_html": re.compile(r"/vagas/v\d+", re.IGNORECASE),
    "infojobs_html": re.compile(r"/vaga-de-.+__\d+\.aspx", re.IGNORECASE),
    "trabalhabrasil_html": re.compile(r"/vagas-de-emprego-em-rio-de-janeiro-rj/.+/\d+", re.IGNORECASE),
    "glassdoor_html": re.compile(r"/job-listing/[^\"'?#\s]+\.htm", re.IGNORECASE),
    "geekhunter_html": re.compile(r"/pt/[^/]+/jobs/[^/?#]+", re.IGNORECASE),
}


CYBER_TERMS = re.compile(r"\b(cyber\w*|security|seguranca|soc|siem|appsec|devsecops|pentest\w*|dfir|iam|help\s?desk|service\s?desk|suporte|support|infrastructure|infraestrutura|splunk|sentinel|crowdstrike|fortinet)\b", re.I)


def collect_remote_feed(session: requests.Session, source: dict[str, Any]) -> list[dict[str, Any]]:
    """Read public feeds once per run; retain original URLs and foreign salary units."""
    response = session.get(source["url"], timeout=35)
    response.raise_for_status()
    if source["tipo"] == "remoteok_api":
        records = [dict(title=x.get("position", ""), url=x.get("url", ""),
                        description=x.get("description", ""), company=x.get("company", ""),
                        location=x.get("location", ""), date=x.get("date", ""))
                   for x in response.json() if x.get("position")]
    else:
        records = [dict(title=x.findtext("title", ""), url=x.findtext("link", ""),
                        description=x.findtext("description", ""), date=x.findtext("pubDate", ""),
                        company="", location=x.findtext("region", ""))
                   for x in ET.fromstring(response.content).findall("./channel/item")]
    jobs = []
    for record in records:
        title = record["title"]
        if not CYBER_TERMS.search(normalize(title)):
            continue
        if not record["url"].startswith("https://"):
            continue
        try:
            date = datetime.fromisoformat(record["date"].replace("Z", "+00:00"))
        except ValueError:
            try:
                date = parsedate_to_datetime(record["date"])
            except (ValueError, TypeError):
                continue
        date = datetime.fromtimestamp(date.timestamp())
        description = BeautifulSoup(record["description"], "html.parser").get_text("\n", strip=True)
        jobs.append(dict(url=record["url"], source=source["nome"], title=title, cargo=title,
                         company=record["company"], location=record["location"] or "Internacional — verificar elegibilidade",
                         published_at=date, salary=None, salary_text="Consultar anúncio original (moeda e periodicidade)",
                         contract_type=infer_contract(description), work_mode="Remoto", description=description[:30000],
                         international_review=True))
    return jobs[:int(source.get("max_detalhes_por_execucao", 30))]


def collect_html_links(session: requests.Session, source: dict[str, Any]) -> list[dict[str, str]]:
    pattern = HTML_PATTERNS[source["tipo"]]
    found: dict[str, str] = {}
    for page_url in source.get("urls", []):
        try:
            response = session.get(page_url, timeout=30)
            response.raise_for_status()
        except requests.RequestException:
            continue
        soup = BeautifulSoup(response.text, "html.parser")
        for anchor in soup.select("a[href]"):
            href = anchor.get("href", "")
            if not pattern.search(href):
                continue
            title = " ".join(anchor.stripped_strings)
            if source["tipo"] == "geekhunter_html" and not CYBER_TERMS.search(normalize(title)):
                continue
            if title:
                found[urljoin(page_url, href)] = title[:300]
        time.sleep(REQUEST_DELAY)
    return [{"url": url, "title": title, "source": source["nome"], "source_type": source["tipo"]} for url, title in found.items()]


def gupy_contract(value: str) -> str:
    return {
        "vacancy_type_effective": "CLT – Efetivo",
        "vacancy_type_internship": "Estágio",
        "vacancy_type_apprentice": "Aprendiz",
        "vacancy_type_temporary": "Temporário",
        "vacancy_legal_entity": "Pessoa Jurídica/PJ",
        "vacancy_type_autonomous": "Autônomo",
        "vacancy_type_freelancer": "Freelancer",
    }.get(value, value or "Não informado")


def collect_gupy_jobs(session: requests.Session, source: dict[str, Any]) -> list[dict[str, Any]]:
    found_links: dict[str, dict[str, str]] = {}
    base_url = source["url"].rstrip("/")
    for term in source.get("termos", []):
        encoded_term = quote(str(term), safe="")
        search_urls = [
            f"{base_url}/term={encoded_term}&state=Rio%20de%20Janeiro",
            f"{base_url}/term={encoded_term}&workplaceTypes%5B%5D=remote",
        ]
        for search_url in search_urls:
            try:
                response = session.get(search_url, timeout=45)
                response.raise_for_status()
            except requests.RequestException:
                continue
            soup = BeautifulSoup(response.text, "html.parser")
            for anchor in soup.select("a[href]"):
                href = anchor.get("href", "")
                absolute = urljoin(search_url, href)
                parsed = urlparse(absolute)
                if not parsed.netloc.endswith(".gupy.io") or "/job/" not in parsed.path:
                    continue
                card_text = " ".join(anchor.stripped_strings)
                card_norm = normalize(card_text)
                if not any(marker in card_norm for marker in ["rio de janeiro", "remoto", "remote", "home office"]):
                    continue
                heading = anchor.select_one("h1, h2, h3, [data-testid*='title']")
                title = " ".join(heading.stripped_strings) if heading else card_text
                title = re.split(r"\b(?:Modelo de trabalho|Local de trabalho|Publicada em)\s*:", title, maxsplit=1, flags=re.IGNORECASE)[0]
                if len(title) < 3:
                    title = f"Vaga Gupy - {term}"
                canonical = absolute.split("#", 1)[0]
                found_links[canonical] = {
                    "url": canonical,
                    "title": title[:300],
                    "source": source["nome"],
                    "source_type": source["tipo"],
                }
            time.sleep(REQUEST_DELAY)

    max_details = int(source.get("max_detalhes_por_execucao", 12))
    detail_delay = float(source.get("intervalo_detalhes_segundos", REQUEST_DELAY))
    jobs: list[dict[str, Any]] = []
    for item in list(found_links.values())[:max_details]:
        try:
            job = fetch_html_job(session, item)
            company_host = urlparse(item["url"]).netloc.split(".")[0]
            if not job.get("company"):
                job["company"] = company_host.replace("-", " ").title()
            jobs.append(job)
            time.sleep(detail_delay)
        except requests.RequestException:
            continue
    return jobs


FIELD_LABELS = [
    "Cargo", "Regime de Contratação", "Número de Vagas", "Descrição do Cargo / Responsabilidades",
    "Formação e experiências desejáveis", "Forma de Trabalho", "Bairro", "Cidade", "Benefícios",
    "Horário de Expediente", "Salário", "Bolsa Auxílio", "Informações Adicionais",
]


def extract_field(text: str, label: str) -> str:
    label_pattern = re.escape(label)
    alternatives = "|".join(re.escape(item) for item in FIELD_LABELS if item != label)
    match = re.search(
        rf"{label_pattern}\s*:\s*(.+?)(?=\n\s*(?:{alternatives})\s*:|\Z)",
        text,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if not match:
        return ""
    value = re.sub(r"\s+", " ", match.group(1)).strip()
    return value[:1000]


def parse_date(text: str, soup: BeautifulSoup) -> datetime | None:
    for tag in soup.select("time[datetime]"):
        raw = tag.get("datetime", "")
        try:
            return datetime.fromisoformat(raw.replace("Z", "+00:00")).replace(tzinfo=None)
        except ValueError:
            pass
    match = re.search(r"\b([0-3]?\d/[01]?\d/20\d{2})\b", text)
    if match:
        try:
            return datetime.strptime(match.group(1), "%d/%m/%Y")
        except ValueError:
            return None
    normalized = normalize(text)
    if re.search(r"\b(hoje|ultimas 24 horas)\b", normalized):
        return datetime.now()
    if re.search(r"\bontem\b", normalized):
        return datetime.now() - timedelta(days=1)
    match = re.search(r"\bha\s+(\d+)\s+dias?\b", normalized)
    if match:
        return datetime.now() - timedelta(days=int(match.group(1)))
    match = re.search(r"\bultimos\s+(\d+)\s+dias?\b", normalized)
    if match:
        return datetime.now() - timedelta(days=int(match.group(1)))
    return None


def money_to_float(raw: str) -> float | None:
    match = re.search(r"R\$\s*([0-9.]+(?:,[0-9]{1,2})?)", raw, flags=re.IGNORECASE)
    if not match:
        return None
    number = match.group(1).replace(".", "").replace(",", ".")
    try:
        return float(number)
    except ValueError:
        return None


def infer_contract(text: str) -> str:
    normalized = normalize(text)
    for term, label in [
        ("clt", "CLT – Efetivo"), ("efetivo", "CLT – Efetivo"), ("estagio", "Estágio"),
        ("temporario", "Temporário"), ("pessoa juridica", "Pessoa Jurídica/PJ"),
        ("pj", "Pessoa Jurídica/PJ"), ("mei", "Pessoa Jurídica/MEI"),
        ("autonomo", "Autônomo"), ("aprendiz", "Aprendiz"),
    ]:
        if re.search(rf"\b{re.escape(term)}\b", normalized):
            return label
    return "Não informado"


def infer_work_mode(text: str) -> str:
    normalized = normalize(text)
    explicit = re.search(
        r"\b(?:modalidade|modelo de trabalho|forma de trabalho)\s*:?\s*(presencial|hibrido|remoto|home office)\b",
        normalized,
    )
    if explicit:
        value = explicit.group(1)
        if value == "hibrido":
            return "Híbrido"
        if value in {"remoto", "home office"}:
            return "Remoto"
        return "Presencial"
    if "hibrid" in normalized or re.search(r"\d+\s+dias?\s+(?:presenciais?|no escritorio)", normalized):
        return "Híbrido"
    if any(term in normalized for term in ["100% remoto", "100% home office", "home-office", "home office", "trabalho remoto", "remoto"]):
        return "Remoto"
    return "Presencial"


def infer_location(text: str) -> str:
    for pattern in [r"Bairro\s*:\s*([^\n]+)", r"Cidade\s*:\s*([^\n]+)", r"([A-Za-zÀ-ÿ ]+)\s*/\s*RJ"]:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            return re.sub(r"\s+", " ", match.group(1)).strip()[:180]
    return "Rio de Janeiro/RJ" if "rio de janeiro" in normalize(text) else "Não informado"


def structured_job_text(soup: BeautifulSoup) -> str:
    parts: list[str] = []

    def visit(value: Any) -> None:
        if isinstance(value, list):
            for item in value:
                visit(item)
            return
        if not isinstance(value, dict):
            return
        item_type = value.get("@type")
        types = item_type if isinstance(item_type, list) else [item_type]
        if "JobPosting" in types and value.get("description"):
            raw = html.unescape(str(value["description"]))
            parts.append(BeautifulSoup(raw, "html.parser").get_text("\n", strip=True))
        for nested in value.values():
            if isinstance(nested, (dict, list)):
                visit(nested)

    for script in soup.select('script[type="application/ld+json"]'):
        try:
            visit(json.loads(script.string or script.get_text() or "{}"))
        except (json.JSONDecodeError, TypeError):
            continue
    return "\n".join(parts)


def fetch_html_job(session: requests.Session, item: dict[str, str]) -> dict[str, Any]:
    response = session.get(item["url"], timeout=30)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, "html.parser")
    h1 = soup.find("h1")
    title = " ".join(h1.stripped_strings) if h1 else item["title"]
    content = soup.find("article") or soup.find("main") or soup.body
    text = content.get_text("\n", strip=True) if content else soup.get_text("\n", strip=True)
    structured = structured_job_text(soup)
    if structured:
        text = f"{text}\n{structured}"
    text = re.sub(r"\n{3,}", "\n\n", text)
    salary_text = extract_field(text, "Salário") or extract_field(text, "Bolsa Auxílio") or f"{title}\n{text[:6000]}"
    contract = extract_field(text, "Regime de Contratação") or infer_contract(text)
    work_mode = extract_field(text, "Forma de Trabalho") or infer_work_mode(text)
    return {
        "url": item["url"],
        "source": item.get("source", "Outra fonte"),
        "company": "",
        "location": infer_location(text),
        "title": title,
        "cargo": extract_field(text, "Cargo") or title.split(" – ")[0].split(" - ")[0],
        "published_at": parse_date(text, soup),
        "salary": money_to_float(salary_text),
        "salary_text": salary_text,
        "contract_type": contract,
        "work_mode": work_mode,
        "description": text[:30000],
    }


def excluded_title_reason(title: str, filters: dict[str, Any]) -> str | None:
    title_norm = normalize(title)
    for term in filters.get("palavras_excluidas", []):
        if normalize(term) in title_norm:
            return term
    return None


def link_priority(item: dict[str, str], filters: dict[str, Any]) -> int:
    title = normalize(item.get("title", ""))
    return sum(1 for term in filters.get("prioridades", []) if normalize(term) in title)


def collect_all_jobs(
    session: requests.Session,
    sources: dict[str, Any],
    filters: dict[str, Any],
    known_urls: set[str],
) -> tuple[list[dict[str, Any]], list[dict[str, str]], list[dict[str, str]]]:
    jobs: dict[str, dict[str, Any]] = {}
    errors: list[dict[str, str]] = []
    warnings: list[dict[str, str]] = []
    for source in sources.get("automaticas", []):
        source_name = source.get("nome", "desconhecida")
        cooldown_until = SOURCE_COOLDOWNS.get(source_name)
        if cooldown_until and cooldown_until > datetime.now():
            warnings.append({
                "fonte": source_name,
                "aviso": f"fonte pausada até {cooldown_until.strftime('%d/%m/%Y %H:%M')} após limite de requisições",
            })
            continue
        try:
            if source["tipo"] == "gupy":
                collected = collect_gupy_jobs(session, source)
            elif source["tipo"] in {"remoteok_api", "wwr_rss"}:
                collected = collect_remote_feed(session, source)
            else:
                links = collect_riovagas_links(session, source) if source["tipo"] == "riovagas" else collect_html_links(session, source)
                links = [
                    item for item in links
                    if item["url"] not in known_urls
                    and not is_pcd_only_job(item.get("title", ""), "")
                    and not excluded_title_reason(item.get("title", ""), filters)
                ]
                links.sort(key=lambda item: link_priority(item, filters), reverse=True)
                max_details = int(source.get("max_detalhes_por_execucao", len(links)))
                links = links[:max_details]
                detail_delay = float(source.get("intervalo_detalhes_segundos", REQUEST_DELAY))
                collected = []
                for item in links:
                    try:
                        collected.append(fetch_html_job(session, item))
                        time.sleep(detail_delay)
                    except requests.HTTPError as exc:
                        response = exc.response
                        if response is not None and response.status_code == 429:
                            retry_raw = response.headers.get("Retry-After", "21600")
                            try:
                                retry_seconds = max(3600, min(int(retry_raw), 86400))
                            except ValueError:
                                retry_seconds = 21600
                            blocked_until = datetime.now() + timedelta(seconds=retry_seconds)
                            SOURCE_COOLDOWNS[source_name] = blocked_until
                            warnings.append({
                                "fonte": source_name,
                                "aviso": f"site limitou acessos; coleta pausada até {blocked_until.strftime('%d/%m/%Y %H:%M')}",
                            })
                            break
                        errors.append({"fonte": source_name, "url": item["url"], "erro": str(exc)[:250]})
                    except Exception as exc:
                        errors.append({"fonte": source_name, "url": item["url"], "erro": str(exc)[:250]})
            for job in collected:
                jobs[job["url"]] = job
        except Exception as exc:
            errors.append({"fonte": source_name, "url": source.get("url", ""), "erro": str(exc)[:250]})
    return list(jobs.values()), errors, warnings


def is_pcd_only_job(title: str, description: str) -> bool:
    title_norm = normalize(title)
    text_norm = normalize(f"{title} {description}")
    if re.search(r"\bpcd\b|pessoas? com deficiencia", title_norm):
        return True
    if re.search(r"\bvaga\s+para\s+pcd\s*:", text_norm):
        return True
    exclusive_signals = [
        "vaga exclusiva para pcd",
        "vaga exclusiva pcd",
        "exclusiva para pessoas com deficiencia",
        "exclusivo para pessoas com deficiencia",
        "vaga afirmativa para pcd",
        "vaga afirmativa para pessoas com deficiencia",
        "somente para pcd",
    ]
    return any(signal in text_norm for signal in exclusive_signals)


def classify(job: dict[str, Any], filters: dict[str, Any]) -> tuple[str, str, int]:
    combined = normalize(" ".join([
        job.get("title", ""), job.get("cargo", ""), job.get("contract_type", ""),
        job.get("work_mode", ""), job.get("description", "")
    ]))
    title_norm = normalize(job.get("title", ""))
    reasons: list[str] = []
    pending: list[str] = []

    if job.get("international_review"):
        pending.append("confirmar país elegível, autorização de trabalho, contrato e remuneração na moeda original")
        if re.search(r"\b(senior|sr|staff|principal|director|head|lead|manager)\b", title_norm):
            reasons.append("vaga internacional exige senioridade acima do perfil júnior")

    if filters.get("rejeitar_vagas_exclusivas_pcd") and is_pcd_only_job(job.get("title", ""), job.get("description", "")):
        reasons.append("vaga exclusiva ou afirmativa para PCD")

    published = job.get("published_at")
    if job.get("source") in {"GeekHunter", "Remote OK", "We Work Remotely"} and not published:
        pending.append("confirmar data de publicação: limite de 7 dias")
    if published and published < datetime.now() - timedelta(days=int(filters["dias_maximos"])):
        reasons.append(f"publicada há mais de {filters['dias_maximos']} dias")

    is_hybrid = "hibrid" in normalize(job.get("work_mode", "")) or "hibrid" in title_norm
    if filters.get("rejeitar_hibrido") and is_hybrid:
        reasons.append("trabalho híbrido")

    is_remote = normalize(job.get("work_mode", "")) == "remoto" or any(term in combined for term in ["forma de trabalho: remoto", "100% home office", "100% remoto", "trabalho remoto"])
    if filters.get("somente_home_office_nesta_automacao") and not is_remote:
        reasons.append("não é uma vaga remota")

    if filters.get("rejeitar_pj_mei") and any(term in normalize(job.get("contract_type", "")) for term in ["pessoa juridica", "mei", "pj"]):
        reasons.append("contratação PJ/MEI")

    if filters.get("rejeitar_temporario") and "temporar" in normalize(job.get("contract_type", "")):
        reasons.append("contrato temporário")

    excluded_term = excluded_title_reason(job.get("title", ""), filters)
    if excluded_term:
        reasons.append(f"área excluída: {excluded_term}")

    for term in filters.get("requisitos_incompativeis", []):
        if normalize(term) in combined:
            reasons.append(f"requisito obrigatório incompatível: {term}")
            break

    for term in filters.get("palavras_suspeitas", []):
        if normalize(term) in title_norm:
            pending.append(f"revisar possível anúncio genérico ou comissionado: {term}")
            break

    salary = job.get("salary")
    if is_remote:
        if salary is not None and salary < float(filters["salario_minimo_remoto"]):
            reasons.append(f"remuneração abaixo de R$ {filters['salario_minimo_remoto']:.2f}")
        if salary is None and not filters.get("aceitar_salario_nao_informado"):
            reasons.append("salário não informado")
    elif not is_hybrid:
        if salary is None:
            pending.append("vaga presencial sem salário informado")
        elif salary <= float(filters.get("salario_minimo_presencial", 2300)):
            reasons.append(f"vaga presencial exige salário acima de R$ {filters.get('salario_minimo_presencial', 2300):.2f}")
        location_text = normalize(f"{job.get('location', '')} {job.get('title', '')} {job.get('description', '')[:2500]}")
        west_match = any(normalize(area) in location_text for area in filters.get("bairros_zona_oeste", []))
        if not west_match:
            reasons.append("vaga presencial fora da Zona Oeste ou local não identificado")
        elif not filters.get("bairro_residencia"):
            pending.append(f"confirmar se fica a até {filters.get('distancia_maxima_km', 15)} km da residência")

    commission_signals = ["100% comission", "exclusivamente comission", "salario: 20%", "salario: 5%", "comissionado"]
    has_currency_base = money_to_float(job.get("salary_text", "")) is not None
    if filters.get("rejeitar_comissao_sem_salario_base") and any(s in combined for s in commission_signals) and not has_currency_base:
        reasons.append("remuneração somente por comissão")

    if reasons:
        return "descartada", "; ".join(dict.fromkeys(reasons)), 0

    score = 50 if is_remote else 25
    if any(term in normalize(job.get("contract_type", "")) for term in ["clt", "efetivo"]):
        score += 15
    if salary and salary >= float(filters["salario_minimo_remoto"]):
        score += min(15, int((salary - float(filters["salario_minimo_remoto"])) / 150))
    for priority in filters.get("prioridades", []):
        if normalize(priority) in combined:
            score += 4
    if pending:
        return "pendente", "; ".join(dict.fromkeys(pending)), min(score, 100)
    return "aprovada", "compatível com os filtros automáticos; revisar antes de enviar", min(score, 100)


TRACKS = {'tecnologia': {'terms': ['tecnologia', 'suporte', 'ti', 'informatica', 'sistema', 'cyber', 'seguranca'], 'summary': '', 'order': []}, 'administrativo': {'terms': ['administrativo', 'backoffice', 'planilha', 'documento', 'operacional', 'relatorio'], 'summary': '', 'order': []}, 'comercial': {'terms': ['venda', 'comercial', 'prospeccao', 'negociacao', 'lead', 'meta'], 'summary': '', 'order': []}, 'atendimento': {'terms': ['atendimento', 'cliente', 'sac', 'chat', 'whatsapp', 'telemarketing'], 'summary': '', 'order': []}}


def choose_track(job_text: str) -> str:
    normalized = normalize(job_text)
    scores = {name: sum(1 for term in cfg["terms"] if term in normalized) for name, cfg in TRACKS.items()}
    return max(scores, key=scores.get) if max(scores.values()) > 0 else "atendimento"


def ranked_skills(job_text: str, skills: list[str]) -> list[str]:
    normalized = normalize(job_text)
    synonyms = {
        "Pacote Office": ["office", "word", "excel", "powerpoint"],
        "CRM": ["crm", "cliente", "relacionamento"],
        "Atendimento digital": ["chat", "whatsapp", "digital", "remoto", "cliente"],
        "Atendimento telefônico": ["telefone", "telemarketing", "call center", "cliente"],
        "Organização de dados": ["dados", "cadastro", "controle", "informacao"],
        "Planilhas": ["planilha", "excel", "relatorio"],
        "Suporte técnico básico": ["suporte", "tecnico", "tecnologia", "ti"],
        "Sistemas informatizados": ["sistema", "plataforma", "software"],
        "Comunicação": ["comunicacao", "cliente", "atendimento"],
        "Organização": ["organizacao", "prazo", "rotina"],
        "Trabalho remoto": ["remoto", "home office"],
    }
    scored = []
    for index, skill in enumerate(skills):
        terms = synonyms.get(skill, [normalize(skill)])
        points = sum(5 for term in terms if normalize(term) in normalized)
        scored.append((points, -index, skill))
    scored.sort(reverse=True)
    return [skill for _, _, skill in scored[:12]]


def paragraph(text: str, style: ParagraphStyle) -> Paragraph:
    text = text.replace("–", "-").replace("—", "-").replace("‑", "-")
    return Paragraph(html.escape(text), style)


def rich_paragraph(text: str, style: ParagraphStyle) -> Paragraph:
    text = text.replace("–", "-").replace("—", "-").replace("‑", "-")
    return Paragraph(text, style)


def ats_section(title: str, style: ParagraphStyle) -> Table:
    table = Table([[Paragraph(html.escape(title.upper()), style)]], colWidths=[184 * mm])
    table.setStyle(TableStyle([
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ("TOPPADDING", (0, 0), (-1, -1), 1.0 * mm),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 1.0 * mm),
        ("LINEBELOW", (0, 0), (-1, -1), 0.45, colors.HexColor("#777777")),
        ("VALIGN", (0, 0), (-1, -1), "BOTTOM"),
    ]))
    return table


def skill_lines(track_name: str) -> list[tuple[str, str]]:
    groups = {
        "atendimento": [
            ("Atendimento e CRM", "Atendimento digital e telefônico, registro de informações, resolução de demandas, escuta ativa e comunicação."),
            ("Sistemas e canais digitais", "CRM, sistemas informatizados, plataformas digitais e boa digitação."),
            ("Organização", "Acompanhamento de solicitações, organização de dados, cumprimento de procedimentos e prazos."),
            ("Trabalho remoto", "Experiência em atendimento home office, com disciplina e organização da rotina."),
        ],
        "comercial": [
            ("Atendimento comercial", "Atendimento ativo e receptivo, relacionamento com clientes, retenção e atuação orientada a metas."),
            ("CRM e registros", "Registro e organização de informações de clientes em CRM e sistemas informatizados."),
            ("Comunicação", "Comunicação assertiva, escuta ativa, negociação e resolução de demandas."),
            ("Ferramentas", "Pacote Office, planilhas, plataformas digitais e trabalho remoto."),
        ],
        "administrativo": [
            ("Rotinas administrativas", "Organização e controle de documentos, dados, planilhas e relatórios; suporte operacional e contato com fornecedores."),
            ("Pacote Office", "Word, Excel, PowerPoint e uso de planilhas para organização de informações."),
            ("Sistemas", "Sistemas informatizados, plataformas digitais, CRM e organização de dados."),
            ("Atendimento e suporte", "Atendimento digital e telefônico, comunicação, escuta ativa e suporte técnico básico."),
        ],
        "tecnologia": [
            ("Suporte técnico", "Suporte básico a hardware e software, sistemas informatizados e plataformas digitais."),
            ("Dados e ferramentas", "Organização de dados, planilhas, relatórios e Pacote Office: Word, Excel e PowerPoint."),
            ("Atendimento ao usuário", "Atendimento digital e telefônico, resolução de demandas, comunicação e escuta ativa."),
            ("Formação em tecnologia", "Graduação em Segurança Cibernética em andamento e estudos contínuos em tecnologia e informática."),
        ],
    }
    return groups[track_name]


def build_pdf(job: sqlite3.Row, candidate: dict[str, Any], target: Path, font_size: float = 9.2) -> None:
    styles = getSampleStyleSheet()
    black = colors.HexColor("#111111")
    gray = colors.HexColor("#555555")
    body = ParagraphStyle(
        "ats_body", parent=styles["BodyText"], fontName="Helvetica", fontSize=font_size,
        leading=font_size + 2.0, textColor=black, alignment=TA_LEFT, spaceAfter=1.0 * mm,
    )
    small = ParagraphStyle(
        "ats_small", parent=body, fontSize=font_size - 0.15, leading=font_size + 1.65,
        spaceAfter=0.45 * mm,
    )
    bullet = ParagraphStyle(
        "ats_bullet", parent=small, leftIndent=3.4 * mm, firstLineIndent=-2.6 * mm,
        bulletIndent=0, spaceAfter=0.35 * mm,
    )
    heading = ParagraphStyle(
        "ats_heading", parent=body, fontName="Helvetica-Bold", fontSize=10.2,
        leading=12, textColor=black, spaceBefore=2.0 * mm, spaceAfter=0,
    )
    name_style = ParagraphStyle(
        "ats_name", parent=body, fontName="Helvetica-Bold", fontSize=17.5,
        leading=20, alignment=TA_LEFT, textColor=black, spaceAfter=0.7 * mm,
    )
    contact_style = ParagraphStyle(
        "ats_contact", parent=small, fontSize=8.6, leading=10.5,
        alignment=TA_LEFT, textColor=gray, spaceAfter=1.4 * mm,
    )
    experience_style = ParagraphStyle(
        "ats_experience", parent=body, fontName="Helvetica-Bold", fontSize=font_size,
        leading=font_size + 2, spaceAfter=0.2 * mm,
    )

    job_text = f"{job['title']} {job['description']}"
    track_name = choose_track(job_text)
    track = TRACKS[track_name]
    summary_override = job.get("summary_override") if isinstance(job, dict) else None
    professional_summary = summary_override or candidate.get("resumos", {}).get(track_name, candidate.get("resumo", ""))
    experiences_by_id = {exp["id"]: exp for exp in candidate["experiencias"]}
    order = candidate.get("ordem_experiencias", {}).get(track_name, list(experiences_by_id))
    experiences = [experiences_by_id[item] for item in order if item in experiences_by_id]

    story = [
        paragraph(candidate["nome_completo"], name_style),
        paragraph(f"{candidate['telefone']}  |  {candidate['email']}  |  {candidate.get("localidade", "Localidade não informada")}", contact_style),
        HRFlowable(width="100%", thickness=0.6, color=colors.HexColor("#777777"), spaceBefore=0, spaceAfter=1.0 * mm),
        ats_section("Resumo profissional", heading),
        Spacer(1, 0.9 * mm),
        paragraph(professional_summary, body),
        ats_section("Experiência profissional", heading),
        Spacer(1, 0.9 * mm),
    ]

    for exp in experiences:
        story.append(paragraph(exp["empresa"], experience_style))
        story.append(rich_paragraph(f"<b>{html.escape(exp['cargo'])}</b>", small))
        for activity in exp["atividades"][:3]:
            story.append(Paragraph(html.escape(activity), bullet, bulletText="-"))
        story.append(Spacer(1, 0.55 * mm))

    story.extend([ats_section("Competências técnicas", heading), Spacer(1, 0.9 * mm)])
    for label, description in [("Competências declaradas", ", ".join(candidate.get("competencias", [])))]:
        story.append(rich_paragraph(f"<b>{html.escape(label)}:</b> {html.escape(description)}", small))

    story.extend([
        ats_section("Formação acadêmica", heading),
        Spacer(1, 0.9 * mm),
        rich_paragraph(f"<b>{html.escape(candidate['formacao'])}</b> - {html.escape(candidate['situacao_formacao'])}<br/>{html.escape(candidate['ensino'])}", small),
        ats_section("Cursos complementares", heading),
        Spacer(1, 0.9 * mm),
        paragraph(" | ".join(candidate["cursos"]), small),
        ats_section("Idiomas", heading),
        Spacer(1, 0.9 * mm),
        paragraph(" | ".join(candidate.get("idiomas", [])), small),
    ])

    target.parent.mkdir(parents=True, exist_ok=True)
    doc = SimpleDocTemplate(
        str(target), pagesize=A4, leftMargin=13 * mm, rightMargin=13 * mm,
        topMargin=10 * mm, bottomMargin=9 * mm,
        title=f"{candidate['nome_arquivos']} - {job['cargo']}", author=candidate["nome_completo"],
    )
    doc.build(story)


def generate_pdf(conn: sqlite3.Connection, job_id: int) -> str:
    row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    if not row:
        raise KeyError(job_id)
    candidate = load_json("candidato.json")
    previous_relative = row["pdf_path"]
    short_id = hashlib.sha1(row["url"].encode("utf-8")).hexdigest()[:8]
    folder = OUTPUT_DIR / short_id
    filename = short_job_filename(row["cargo"] or row["title"])
    target = folder / filename
    build_pdf(row, candidate, target, 9.2)
    if len(PdfReader(str(target)).pages) > 1:
        build_pdf(row, candidate, target, 8.55)
    relative = target.relative_to(OUTPUT_DIR).as_posix()
    if previous_relative and previous_relative != relative:
        previous_path = (OUTPUT_DIR / previous_relative).resolve()
        output_root = OUTPUT_DIR.resolve()
        if output_root in previous_path.parents and previous_path.suffix.lower() == ".pdf" and previous_path.is_file():
            previous_path.unlink()
    conn.execute(
        "UPDATE jobs SET pdf_path = ?, template_version = ?, status = ?, updated_at = ? WHERE id = ?",
        (relative, TEMPLATE_VERSION, "material preparado", datetime.now().isoformat(timespec="seconds"), job_id),
    )
    conn.commit()
    return relative


def save_job(conn: sqlite3.Connection, job: dict[str, Any], decision: str, reason: str, score: int) -> tuple[int, bool]:
    now = datetime.now().isoformat(timespec="seconds")
    existing = conn.execute("SELECT id FROM jobs WHERE url = ?", (job["url"],)).fetchone()
    published = job["published_at"].date().isoformat() if job.get("published_at") else None
    if existing:
        conn.execute(
            """UPDATE jobs SET source=?, company=?, location=?, title=?, cargo=?, published_at=?, salary=?, contract_type=?, work_mode=?,
            decision=?, reason=?, score=?, description=?, updated_at=? WHERE id=?""",
            (job.get("source", "Outra fonte"), job.get("company", ""), job.get("location", ""), job["title"], job["cargo"], published,
             job["salary"], job["contract_type"], job["work_mode"], decision, reason, score, job["description"], now, existing["id"]),
        )
        conn.commit()
        return int(existing["id"]), False
    cursor = conn.execute(
        """INSERT INTO jobs (url,source,company,location,title,cargo,published_at,salary,contract_type,work_mode,decision,reason,score,status,description,discovered_at,updated_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (job["url"], job.get("source", "Outra fonte"), job.get("company", ""), job.get("location", ""), job["title"], job["cargo"], published,
         job["salary"], job["contract_type"], job["work_mode"], decision, reason, score,
         "encontrada" if decision == "aprovada" else ("pendente de informação" if decision == "pendente" else "descartada"),
         job["description"], now, now),
    )
    conn.commit()
    return int(cursor.lastrowid), True


class ApplicationResult(BaseModel):
    state: str
    message: str = ""
    current_url: str = ""


def application_payload(request: Request, row: sqlite3.Row) -> dict[str, Any]:
    candidate = load_json("candidato.json")
    try:
        answers = load_json("respostas.json")
    except FileNotFoundError:
        answers = {"respostas": []}
    return {
        "id": row["id"],
        "url": row["url"],
        "title": row["title"],
        "source": row["source"],
        "company": row["company"] or "",
        "pdf_url": str(request.base_url).rstrip("/") + f"/pdf/{row['id']}",
        "pdf_name": Path(row["pdf_path"]).name if row["pdf_path"] else f"CV_{row['id']}.pdf",
        "candidate": candidate,
        "answers": answers.get("respostas", []),
    }


@app.get("/api/applications/next")
def next_application(request: Request) -> dict[str, Any]:
    conn = get_db()
    try:
        stale_before = (datetime.now() - timedelta(minutes=30)).isoformat(timespec="seconds")
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            """
            SELECT * FROM jobs
            WHERE decision='aprovada' AND status IN ('encontrada', 'material preparado') AND pdf_path IS NOT NULL
              AND COALESCE(hidden, 0)=0
              AND (COALESCE(apply_state, 'ready') IN ('ready', 'retry')
                   OR (apply_state='processing' AND COALESCE(apply_attempted_at, '') < ?))
            ORDER BY score DESC, discovered_at DESC
            LIMIT 1
            """,
            (stale_before,),
        ).fetchone()
        if not row:
            conn.commit()
            return {"job": None}
        now = datetime.now().isoformat(timespec="seconds")
        conn.execute(
            "UPDATE jobs SET apply_state='processing', apply_message='', apply_attempted_at=?, updated_at=? WHERE id=?",
            (now, now, row["id"]),
        )
        conn.commit()
        return {"job": application_payload(request, row)}
    finally:
        conn.close()


@app.get("/api/applications/by-url")
def application_by_url(request: Request, url: str) -> dict[str, Any]:
    normalized_url = url.rstrip("/")
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT * FROM jobs WHERE rtrim(url, '/')=? AND decision='aprovada' AND pdf_path IS NOT NULL LIMIT 1",
            (normalized_url,),
        ).fetchone()
        return {"job": application_payload(request, row) if row else None}
    finally:
        conn.close()


@app.post("/api/applications/{job_id}/result")
def application_result(job_id: int, result: ApplicationResult) -> dict[str, Any]:
    allowed = {"sent", "action_required", "retry", "failed", "processing"}
    if result.state not in allowed:
        raise HTTPException(400, "Estado de candidatura inválido")
    conn = get_db()
    try:
        row = conn.execute("SELECT id FROM jobs WHERE id=?", (job_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Vaga não encontrada")
        status = {
            "sent": "enviada",
            "action_required": "aguardando ação humana",
            "failed": "aguardando ação humana",
        }.get(result.state, "material preparado")
        now = datetime.now().isoformat(timespec="seconds")
        detail = result.message.strip()[:1000]
        if result.current_url:
            detail = f"{detail} | Página: {result.current_url}".strip(" |")[:1000]
        conn.execute(
            "UPDATE jobs SET apply_state=?, apply_message=?, status=?, apply_attempted_at=?, updated_at=? WHERE id=?",
            (result.state, detail, status, now, now, job_id),
        )
        conn.commit()
        return {"ok": True, "state": result.state}
    finally:
        conn.close()


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/run")
def run_search() -> dict[str, Any]:
    filters = load_json("filtros.json")
    sources = load_json("fontes.json")
    session = request_session()
    conn = get_db()
    result = {"encontradas": 0, "novas": 0, "aprovadas": 0, "pendentes": 0, "descartadas": 0, "erros": [], "avisos": []}
    try:
        known_urls = {row[0] for row in conn.execute("SELECT url FROM jobs").fetchall()}
        jobs, source_errors, source_warnings = collect_all_jobs(session, sources, filters, known_urls)
        result["erros"].extend(source_errors)
        result["avisos"].extend(source_warnings)
        result["encontradas"] = len(jobs)
        for job in jobs:
            try:
                if filters.get("rejeitar_vagas_exclusivas_pcd") and is_pcd_only_job(job.get("title", ""), job.get("description", "")):
                    conn.execute(
                        "UPDATE jobs SET hidden=1, decision='descartada', reason='vaga exclusiva ou afirmativa para PCD', "
                        "score=0, status='descartada', updated_at=? WHERE url=?",
                        (datetime.now().isoformat(timespec="seconds"), job["url"]),
                    )
                    conn.commit()
                    result["descartadas"] += 1
                    continue
                decision, reason, score = classify(job, filters)
                job_id, is_new = save_job(conn, job, decision, reason, score)
                result["novas"] += int(is_new)
                result[{"aprovada": "aprovadas", "pendente": "pendentes", "descartada": "descartadas"}[decision]] += 1
                row = conn.execute("SELECT pdf_path, template_version FROM jobs WHERE id = ?", (job_id,)).fetchone()
                if decision == "aprovada" and (not row["pdf_path"] or row["template_version"] != TEMPLATE_VERSION):
                    generate_pdf(conn, job_id)
            except Exception as exc:
                result["erros"].append({"fonte": job.get("source", ""), "url": job["url"], "erro": str(exc)[:250]})
    finally:
        conn.close()
    result["painel"] = "http://localhost:8090"
    return result


def badge(value: str) -> str:
    cls = {
        "aprovada": "good", "material preparado": "good", "enviada": "sent",
        "descartada": "bad", "ignorada": "ignored", "pendente": "wait",
        "pendente de informação": "wait", "aguardando ação humana": "wait",
    }.get(value, "neutral")
    return f'<span class="badge {cls}">{html.escape(value)}</span>'


def dashboard_tab_for(row: sqlite3.Row) -> str:
    if row["status"] == "enviada":
        return "enviadas"
    if row["status"] == "ignorada":
        return "ignoradas"
    if row["status"] == "aguardando ação humana":
        return "acao"
    if row["decision"] == "pendente":
        return "pendentes"
    return "novas"


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request, response: Response, tab: str = "novas") -> str:
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    conn = get_db()
    all_rows = conn.execute(
        "SELECT * FROM jobs WHERE COALESCE(hidden, 0)=0 AND decision<>'descartada' AND lower(reason) NOT LIKE '%pcd%' "
        "ORDER BY CASE decision WHEN 'aprovada' THEN 0 WHEN 'pendente' THEN 1 ELSE 2 END, score DESC, discovered_at DESC"
    ).fetchall()
    conn.close()
    tab_labels = {
        "novas": "Novas vagas",
        "pendentes": "Pendentes",
        "acao": "Ação necessária",
        "enviadas": "Enviadas",
        "ignoradas": "Ignoradas",
    }
    if tab not in tab_labels:
        tab = "novas"
    tab_counts = {name: 0 for name in tab_labels}
    for item in all_rows:
        tab_counts[dashboard_tab_for(item)] += 1
    rows = [item for item in all_rows if dashboard_tab_for(item) == tab]
    tabs_html = "".join(
        f'<a class="tab {"active" if name == tab else ""}" href="/?tab={name}">'
        f'{label}<span class="tab-count">{tab_counts[name]}</span></a>'
        for name, label in tab_labels.items()
    )
    sources = load_json("fontes.json")
    access_host = request.url.hostname or "localhost"
    if ":" in access_host and not access_host.startswith("["):
        access_host = f"[{access_host}]"
    n8n_url = f"{request.url.scheme}://{access_host}:5678"
    manual_links = "".join(
        f'<a class="button secondary" href="{html.escape(item["url"])}" target="_blank" rel="noopener">{html.escape(item["nome"])}</a>'
        for item in sources.get("buscas_manuais", [])
    )
    table_rows = []
    for row in rows:
        salary = f"R$ {row['salary']:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".") if row["salary"] else "Não informado"
        pdf = f'<a class="button secondary" href="/pdf/{row["id"]}">Baixar currículo</a>' if row["pdf_path"] else ""
        actions = ""
        if row["status"] == "enviada":
            actions = f'''
                {pdf}
                <a class="button" href="{html.escape(row['url'])}" target="_blank" rel="noopener">Abrir vaga</a>
                <form method="post" action="/jobs/{row['id']}/restore"><button class="button secondary">Voltar para novas</button></form>
            '''
        elif row["status"] == "ignorada":
            actions = f'''
                <a class="button" href="{html.escape(row['url'])}" target="_blank" rel="noopener">Abrir vaga</a>
                <form method="post" action="/jobs/{row['id']}/restore"><button class="button secondary">Restaurar</button></form>
            '''
        elif row["status"] == "aguardando ação humana":
            note = html.escape(row["apply_message"] or "A candidatura automática precisa da sua intervenção.")
            actions = f'''
                {pdf}
                <a class="button" href="{html.escape(row['url'])}" target="_blank" rel="noopener">Resolver agora</a>
                <div class="apply-note">{note}</div>
                <form method="post" action="/jobs/{row['id']}/restore"><button class="button secondary">Voltar para novas</button></form>
            '''
        elif row["decision"] == "aprovada":
            actions = f'''
                {pdf}
                <form method="post" action="/jobs/{row['id']}/regenerate?return_tab={tab}"><button class="button secondary">Regenerar currículo ATS</button></form>
                <a class="button" href="{html.escape(row['url'])}" target="_blank" rel="noopener">Abrir vaga</a>
                <form method="post" action="/jobs/{row['id']}/status?return_tab={tab}"><input type="hidden" name="status" value="enviada"><button class="button success">Marcar enviada</button></form>
                <form method="post" action="/jobs/{row['id']}/status?return_tab={tab}"><input type="hidden" name="status" value="ignorada"><button class="linkbutton">Ignorar</button></form>
            '''
        elif row["decision"] == "pendente":
            actions = f'''
                <a class="button" href="{html.escape(row['url'])}" target="_blank" rel="noopener">Abrir vaga</a>
                <form method="post" action="/jobs/{row['id']}/approve"><button class="button success">Aprovar e gerar PDF</button></form>
                <form method="post" action="/jobs/{row['id']}/status?return_tab={tab}"><input type="hidden" name="status" value="ignorada"><button class="linkbutton">Ignorar</button></form>
            '''
        else:
            actions = f'<a class="button" href="{html.escape(row["url"])}" target="_blank" rel="noopener">Abrir vaga</a>'
        table_rows.append(f'''
            <tr>
              <td>{badge(row['decision'])}<br>{badge(row['status'])}</td>
              <td><strong>{html.escape(row['cargo'] or row['title'])}</strong><div class="muted">{html.escape(row['company'] or row['title'])}</div></td>
              <td><strong>{html.escape(row['source'])}</strong><div class="muted">{html.escape(row['location'] or 'Local não identificado')} · {html.escape(row['work_mode'] or 'Modalidade não informada')}</div></td>
              <td>{salary}<br><span class="muted">{html.escape(row['contract_type'] or 'Contrato não informado')}</span></td>
              <td>{html.escape(row['published_at'] or 'Data não identificada')}</td>
              <td><strong>{row['score']}/100</strong><div class="muted">{html.escape(row['reason'])}</div></td>
              <td class="actions">{actions}</td>
            </tr>
        ''')
    rows_html = "".join(table_rows) or f'<tr><td colspan="7" class="empty">Nenhuma vaga em <strong>{html.escape(tab_labels[tab])}</strong>.</td></tr>'
    return f'''<!doctype html>
<html lang="pt-BR"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Painel de vagas — JobFlow</title>
<style>
:root{{color-scheme:dark}} *{{box-sizing:border-box}} body{{margin:0;background:#090e18;color:#e5e7eb;font-family:Inter,Segoe UI,Arial,sans-serif}}
.top{{background:linear-gradient(120deg,#0f172a,#172554);color:#f8fafc;padding:30px 5vw;border-bottom:1px solid #25324a}} .top h1{{margin:0 0 7px;font-size:29px}} .top p{{margin:0;color:#b8c4d6}}
.wrap{{max-width:1500px;margin:22px auto;padding:0 24px}} .toolbar{{display:flex;gap:12px;align-items:center;margin-bottom:18px;flex-wrap:wrap}}
.button,button.button{{display:inline-block;border:1px solid #3b82f6;border-radius:8px;padding:9px 13px;background:#2563eb;color:#fff;text-decoration:none;cursor:pointer;font:inherit;white-space:nowrap}}
.button:hover,button.button:hover{{background:#1d4ed8}} .button.secondary{{background:#1e293b;color:#dbeafe;border-color:#3a4a61}} .button.secondary:hover{{background:#293548}} .button.success{{background:#15803d;border-color:#22c55e}} .button.success:hover{{background:#166534}} .linkbutton{{background:none;border:0;color:#a7b4c8;text-decoration:underline;cursor:pointer}}
.card{{background:#111827;border:1px solid #273449;border-radius:12px;overflow:auto;box-shadow:0 12px 28px rgba(0,0,0,.28)}} table{{border-collapse:collapse;width:100%;min-width:1080px}}
th{{background:#172033;text-align:left;color:#aebbd0;font-size:12px;text-transform:uppercase;letter-spacing:.04em}} th,td{{padding:13px;border-bottom:1px solid #263247;vertical-align:top}} tbody tr:hover{{background:#141f31}}
.muted{{font-size:12px;color:#93a3b8;margin-top:5px;max-width:430px}} .apply-note{{max-width:300px;margin-top:7px;color:#fde68a;font-size:12px;line-height:1.35}} .badge{{display:inline-block;border-radius:999px;padding:3px 8px;margin:1px;font-size:11px;background:#273449;color:#d4dbe7}}
.badge.good{{background:#123923;color:#86efac}} .badge.bad{{background:#441a20;color:#fca5a5}} .badge.sent{{background:#172c55;color:#93c5fd}} .badge.wait{{background:#44340d;color:#fde68a}}
.badge.ignored{{background:#293241;color:#cbd5e1}}
.actions{{display:flex;gap:7px;align-items:flex-start;flex-wrap:wrap}} form{{margin:0}} .empty{{text-align:center;padding:50px;color:#93a3b8}}
.note{{background:#2b230f;border:1px solid #66551c;border-radius:9px;padding:11px 14px;margin-bottom:16px;color:#fde68a}}
.tabs{{display:flex;gap:8px;align-items:center;overflow-x:auto;margin:4px 0 16px;padding:4px 0}}
.tab{{display:flex;gap:8px;align-items:center;padding:10px 14px;border:1px solid #2b3a50;border-radius:9px;background:#111827;color:#b8c5d8;text-decoration:none;font-weight:650;white-space:nowrap}}
.tab:hover{{border-color:#52729a;background:#162033}} .tab.active{{background:#1d4ed8;color:#fff;border-color:#60a5fa}}
.tab-count{{display:inline-flex;align-items:center;justify-content:center;min-width:24px;height:22px;padding:0 7px;border-radius:999px;background:#263449;color:#dbeafe;font-size:12px}}
.tab.active .tab-count{{background:#dbeafe;color:#173b7a}}
</style></head>
<body><div class="top"><h1>Vagas e currículos de JobFlow</h1><p>RioVagas · Gupy · Glassdoor · Vagas.com · InfoJobs · Trabalha Brasil · GeekHunter · Remote OK · We Work Remotely · últimos 7 dias</p></div>
<main class="wrap"><div class="note">A extensão envia automaticamente as candidaturas completas. CAPTCHA, login vencido, testes e perguntas sem resposta segura aparecem em “Ação necessária”.</div>
<div class="toolbar"><form method="post" action="/run-browser"><button class="button">Pesquisar agora</button></form><a class="button secondary" href="{html.escape(n8n_url)}" target="_blank">Abrir n8n</a></div>
<div class="toolbar"><span class="muted">Buscas adicionais que exigem navegação manual:</span>{manual_links}</div>
<nav class="tabs" aria-label="Organização das vagas">{tabs_html}</nav>
<div class="card"><table><thead><tr><th>Decisão</th><th>Vaga</th><th>Fonte e local</th><th>Remuneração</th><th>Publicação</th><th>Aderência</th><th>Ações</th></tr></thead><tbody>{rows_html}</tbody></table></div></main></body></html>'''


@app.post("/run-browser")
def run_browser() -> RedirectResponse:
    run_search()
    return RedirectResponse("/", status_code=303)


@app.post("/jobs/{job_id}/status")
def update_status(job_id: int, status: str = Form(...), return_tab: str = "novas") -> RedirectResponse:
    allowed = {"enviada", "ignorada", "aguardando ação humana", "material preparado"}
    if status not in allowed:
        raise HTTPException(400, "Status inválido")
    conn = get_db()
    apply_state = {"enviada": "sent", "aguardando ação humana": "action_required"}.get(status, "ready")
    conn.execute(
        "UPDATE jobs SET status=?, apply_state=?, updated_at=? WHERE id=?",
        (status, apply_state, datetime.now().isoformat(timespec="seconds"), job_id),
    )
    conn.commit()
    conn.close()
    if return_tab not in {"novas", "pendentes", "acao", "enviadas", "ignoradas"}:
        return_tab = "novas"
    return RedirectResponse(f"/?tab={return_tab}", status_code=303)


@app.post("/jobs/{job_id}/restore")
def restore_job(job_id: int) -> RedirectResponse:
    conn = get_db()
    row = conn.execute("SELECT decision, pdf_path FROM jobs WHERE id=?", (job_id,)).fetchone()
    if not row:
        conn.close()
        raise HTTPException(404, "Vaga não encontrada")
    if row["decision"] == "aprovada":
        status = "material preparado" if row["pdf_path"] else "encontrada"
        destination = "novas"
    elif row["decision"] == "pendente":
        status = "pendente de informação"
        destination = "pendentes"
    else:
        status = "descartada"
        destination = "novas"
    conn.execute(
        "UPDATE jobs SET status=?, apply_state='ready', apply_message='', updated_at=? WHERE id=?",
        (status, datetime.now().isoformat(timespec="seconds"), job_id),
    )
    conn.commit()
    conn.close()
    return RedirectResponse(f"/?tab={destination}", status_code=303)


@app.post("/jobs/{job_id}/approve")
def approve_job(job_id: int) -> RedirectResponse:
    conn = get_db()
    row = conn.execute("SELECT id FROM jobs WHERE id=?", (job_id,)).fetchone()
    if not row:
        conn.close()
        raise HTTPException(404, "Vaga não encontrada")
    conn.execute(
        "UPDATE jobs SET decision='aprovada', reason='aprovada manualmente após revisão', status='encontrada', apply_state='ready', apply_message='', updated_at=? WHERE id=?",
        (datetime.now().isoformat(timespec="seconds"), job_id),
    )
    conn.commit()
    generate_pdf(conn, job_id)
    conn.close()
    return RedirectResponse("/", status_code=303)


@app.post("/jobs/{job_id}/regenerate")
def regenerate_job_pdf(job_id: int, return_tab: str = "novas") -> RedirectResponse:
    conn = get_db()
    row = conn.execute("SELECT id FROM jobs WHERE id=?", (job_id,)).fetchone()
    if not row:
        conn.close()
        raise HTTPException(404, "Vaga não encontrada")
    generate_pdf(conn, job_id)
    conn.close()
    if return_tab not in {"novas", "pendentes", "acao", "enviadas", "ignoradas"}:
        return_tab = "novas"
    return RedirectResponse(f"/?tab={return_tab}", status_code=303)


@app.get("/pdf/{job_id}")
def download_pdf(job_id: int) -> FileResponse:
    conn = get_db()
    row = conn.execute("SELECT pdf_path FROM jobs WHERE id=?", (job_id,)).fetchone()
    conn.close()
    if not row or not row["pdf_path"]:
        raise HTTPException(404, "Currículo ainda não foi gerado")
    path = (OUTPUT_DIR / row["pdf_path"]).resolve()
    if OUTPUT_DIR.resolve() not in path.parents or not path.exists():
        raise HTTPException(404, "Arquivo não encontrado")
    return FileResponse(path, media_type="application/pdf", filename=path.name)
