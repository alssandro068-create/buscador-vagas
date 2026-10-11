"""Núcleo: banco (SQLAlchemy → PostgreSQL na nuvem / SQLite local), matching, fontes e ingestão."""
import io
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import requests
from sqlalchemy import (JSON, DateTime, Float, ForeignKey, Integer, String, Text,
                        UniqueConstraint, create_engine, or_, select)
from sqlalchemy.orm import (DeclarativeBase, Mapped, joinedload, mapped_column,
                            relationship, sessionmaker)

STATUSES = ["Currículo Enviado", "Em Análise", "Entrevista Agendada", "Teste Técnico",
            "Proposta Recebida", "Não Selecionado", "Desistido"]
MODES = ["", "presencial", "hibrido", "remoto"]


def now() -> datetime:
    return datetime.now(timezone.utc)


# ───────────────────────────── Banco de dados ─────────────────────────────
class Base(DeclarativeBase):
    pass


class Profile(Base):
    __tablename__ = "user_profile"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)  # sempre 1 (uso pessoal)
    name: Mapped[str] = mapped_column(String(200), default="")
    resume_text: Mapped[str] = mapped_column(Text, default="")
    skills: Mapped[list] = mapped_column(JSON, default=list)
    search_terms: Mapped[list] = mapped_column(JSON, default=list)
    location: Mapped[str] = mapped_column(String(200), default="")
    work_mode: Mapped[str] = mapped_column(String(30), default="")
    current_salary: Mapped[float | None] = mapped_column(Float)
    benefits: Mapped[list] = mapped_column(JSON, default=list)
    salary_floor: Mapped[float | None] = mapped_column(Float)


class Company(Base):
    __tablename__ = "companies"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(300), unique=True)
    rating: Mapped[float | None] = mapped_column(Float)


class Job(Base):
    __tablename__ = "jobs"
    __table_args__ = (UniqueConstraint("source", "external_id"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source: Mapped[str] = mapped_column(String(50))
    external_id: Mapped[str] = mapped_column(String(200))
    title: Mapped[str] = mapped_column(String(300))
    company_id: Mapped[int | None] = mapped_column(ForeignKey("companies.id"))
    location: Mapped[str] = mapped_column(String(300), default="")
    work_mode: Mapped[str] = mapped_column(String(30), default="")
    salary_min: Mapped[float | None] = mapped_column(Float)
    salary_max: Mapped[float | None] = mapped_column(Float)
    salary_estimated: Mapped[bool] = mapped_column(default=False)
    benefits: Mapped[list] = mapped_column(JSON, default=list)
    requirements: Mapped[list] = mapped_column(JSON, default=list)
    url: Mapped[str] = mapped_column(Text)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    match_score: Mapped[float] = mapped_column(Float, default=0)
    missing: Mapped[list] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String(20), default="active", index=True)  # active|applied|discarded
    company = relationship("Company")


class Application(Base):
    __tablename__ = "applications"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id"), unique=True)
    status: Mapped[str] = mapped_column(String(40), default=STATUSES[0])
    applied_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    notes: Mapped[str] = mapped_column(Text, default="")


class ApplicationEvent(Base):
    __tablename__ = "application_events"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    application_id: Mapped[int] = mapped_column(ForeignKey("applications.id"))
    status: Mapped[str] = mapped_column(String(40))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


_engine = None
_Session = None


def _db_url() -> str:
    url = os.environ.get("DATABASE_URL", "sqlite:///jobs.db")
    for prefix in ("postgres://", "postgresql://"):
        if url.startswith(prefix):
            return "postgresql+psycopg2://" + url[len(prefix):]
    return url


def _S():
    global _engine, _Session
    if _engine is None:
        _engine = create_engine(_db_url(), pool_pre_ping=True, pool_recycle=300)
        _Session = sessionmaker(_engine, expire_on_commit=False)
    return _Session()


def init_db() -> None:
    _S().close()
    Base.metadata.create_all(_engine)


# ───────────────────────────── Matching ─────────────────────────────
SKILL_VOCAB = [
    "python", "sql", "postgresql", "mysql", "fastapi", "django", "flask", "javascript", "typescript",
    "react", "next.js", "node.js", "java", "c#", ".net", "php", "docker", "kubernetes", "aws", "azure",
    "gcp", "git", "power bi", "excel", "tableau", "scrum", "ingles", "inglês", "pandas", "airflow",
    "spark", "linux", "ci/cd", "rest", "api", "machine learning", "etl", "sap", "vendas", "marketing",
]


def extract_skills(text: str) -> list[str]:
    text = (text or "").lower()
    return sorted({s for s in SKILL_VOCAB if re.search(rf"(?<!\w){re.escape(s)}(?!\w)", text)})


def match_score(requirements: list[str], mine: list[str]) -> tuple[float, list[str]]:
    reqs, mine = {r.lower() for r in requirements}, {m.lower() for m in mine}
    if not reqs:
        return 0.0, []
    missing = sorted(reqs - mine)
    return round(100 * (len(reqs) - len(missing)) / len(reqs), 1), missing


def meets_floor(lo, hi, floor) -> bool:
    """Vagas sem salário informado passam (o app mostra 'não informado')."""
    top = hi or lo
    return True if not floor or top is None else top >= floor


def infer_mode(text: str) -> str:
    t = (text or "").lower()
    if re.search(r"h[ií]brid|hybrid", t):
        return "hibrido"
    if re.search(r"remot|home office|work from home|anywhere", t):
        return "remoto"
    return ""


def extract_resume_text(filename: str, data: bytes) -> str:
    name = filename.lower()
    if name.endswith(".pdf"):
        import pdfplumber
        with pdfplumber.open(io.BytesIO(data)) as pdf:
            return "\n".join(p.extract_text() or "" for p in pdf.pages)
    if name.endswith(".docx"):
        import docx
        return "\n".join(p.text for p in docx.Document(io.BytesIO(data)).paragraphs)
    raise ValueError("Formato não suportado (use PDF ou DOCX).")


# ───────────────────────────── Fontes de vagas ─────────────────────────────
@dataclass
class RawJob:
    source: str
    external_id: str
    title: str
    company: str
    url: str
    published_at: datetime
    location: str = ""
    work_mode: str = ""
    salary_min: float | None = None
    salary_max: float | None = None
    salary_estimated: bool = False
    benefits: list[str] = field(default_factory=list)
    requirements: list[str] = field(default_factory=list)


def _dt(s: str) -> datetime:
    d = datetime.fromisoformat(s.replace("Z", "+00:00"))
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def _plain(html: str) -> str:
    return re.sub(r"<[^>]+>", " ", html or "")


class AdzunaSource:
    """API oficial (grátis, com chave) – cobre o Brasil. https://developer.adzuna.com"""
    name = "adzuna"

    def available(self) -> bool:
        return bool(os.environ.get("ADZUNA_APP_ID") and os.environ.get("ADZUNA_APP_KEY"))

    def fetch(self, terms, where):
        out = []
        for term in terms:
            params = {"app_id": os.environ["ADZUNA_APP_ID"], "app_key": os.environ["ADZUNA_APP_KEY"],
                      "what": term, "results_per_page": 50, "sort_by": "date", "max_days_old": 2}
            if where:
                params["where"] = where
            r = requests.get("https://api.adzuna.com/v1/api/jobs/br/search/1", params=params, timeout=25)
            r.raise_for_status()
            for j in r.json().get("results", []):
                text = f"{j.get('title', '')} {_plain(j.get('description', ''))}"
                out.append(RawJob(
                    "adzuna", str(j["id"]), j.get("title", ""), (j.get("company") or {}).get("display_name", "—"),
                    j.get("redirect_url", ""), _dt(j["created"]), (j.get("location") or {}).get("display_name", ""),
                    infer_mode(text), j.get("salary_min"), j.get("salary_max"),
                    str(j.get("salary_is_predicted")) == "1", [], extract_skills(text)))
        return out


class RemotiveSource:
    """API pública sem chave, só vagas remotas (maioria internacional). https://remotive.com/api"""
    name = "remotive"

    def available(self) -> bool:
        return True

    def fetch(self, terms, where):
        out = []
        for term in terms[:3]:  # a API pede uso moderado
            r = requests.get("https://remotive.com/api/remote-jobs", params={"search": term, "limit": 50}, timeout=25)
            r.raise_for_status()
            for j in r.json().get("jobs", []):
                text = f"{j.get('title', '')} {_plain(j.get('description', ''))}"
                out.append(RawJob(
                    "remotive", str(j["id"]), j.get("title", ""), j.get("company_name", "—"), j.get("url", ""),
                    _dt(j["publication_date"]), j.get("candidate_required_location", ""), "remoto",
                    requirements=extract_skills(text)))
        return out


class MockSource:
    """Vagas fictícias só para demonstrar o fluxo."""
    name = "mock"

    def available(self) -> bool:
        return True

    def fetch(self, terms, where):
        n = now()
        return [
            RawJob("mock", "1", "Desenvolvedor Python Pleno", "Acme Tech", "https://example.com/1", n - timedelta(hours=5),
                   "Rio de Janeiro, RJ", "hibrido", 9000, 12000, False, ["VR", "Plano de saúde", "PLR"],
                   ["python", "sql", "fastapi", "docker", "aws"]),
            RawJob("mock", "2", "Engenheiro de Dados", "DataCorp", "https://example.com/2", n - timedelta(hours=30),
                   "Remoto", "remoto", 11000, 15000, False, ["VR", "Home office"], ["python", "sql", "airflow", "spark"]),
            RawJob("mock", "3", "Analista Jr", "Pequena LTDA", "https://example.com/3", n - timedelta(hours=10),
                   "Niterói, RJ", "presencial", 3000, 4000, False, ["VT"], ["sql", "excel"]),
            RawJob("mock", "4", "Dev Full-Stack", "Startup X", "https://example.com/5", n - timedelta(hours=2),
                   "Remoto", "remoto", None, None, False, ["Stock options"], ["python", "react", "typescript", "sql"]),
        ]


def default_sources():
    return [s for s in (AdzunaSource(), RemotiveSource()) if s.available()]


# ───────────────────────────── Ingestão ─────────────────────────────
def run_ingest(sources=None, max_age_hours: int = 48) -> dict:
    prof = get_profile()
    if not prof:
        raise RuntimeError("Cadastre seu perfil na aba Perfil antes de buscar vagas.")
    if not prof["search_terms"] and sources is None:
        raise RuntimeError("Informe ao menos um termo de busca na aba Perfil.")
    stats = {"fetched": 0, "inserted": 0, "too_old": 0, "below_floor": 0, "duplicates": 0, "errors": []}
    cutoff = now() - timedelta(hours=max_age_hours)

    raws = []
    for src in sources or default_sources():
        try:
            raws += src.fetch(prof["search_terms"], prof["location"])
        except Exception as e:  # uma fonte fora do ar não derruba as outras
            stats["errors"].append(f"{src.name}: {e}")

    with _S() as s, s.begin():
        for raw in raws:
            stats["fetched"] += 1
            if raw.published_at < cutoff:
                stats["too_old"] += 1
                continue
            if not meets_floor(raw.salary_min, raw.salary_max, prof["salary_floor"]):
                stats["below_floor"] += 1
                continue
            if s.scalar(select(Job.id).where(Job.source == raw.source, Job.external_id == raw.external_id)):
                stats["duplicates"] += 1
                continue
            comp = s.scalar(select(Company).where(Company.name == raw.company))
            if not comp:
                comp = Company(name=raw.company)
                s.add(comp)
                s.flush()
            score, missing = match_score(raw.requirements, prof["skills"])
            s.add(Job(source=raw.source, external_id=raw.external_id, title=raw.title, company_id=comp.id,
                      location=raw.location, work_mode=raw.work_mode, salary_min=raw.salary_min,
                      salary_max=raw.salary_max, salary_estimated=raw.salary_estimated, benefits=raw.benefits,
                      requirements=raw.requirements, url=raw.url, published_at=raw.published_at,
                      match_score=score, missing=missing))
            stats["inserted"] += 1
    return stats


# ───────────────────────────── Consultas usadas pela interface ─────────────────────────────
def get_profile() -> dict | None:
    with _S() as s:
        p = s.get(Profile, 1)
        return {c.name: getattr(p, c.name) for c in Profile.__table__.columns} if p else None


def save_profile(**data) -> None:
    with _S() as s, s.begin():
        p = s.get(Profile, 1) or Profile(id=1)
        for k, v in data.items():
            setattr(p, k, v)
        if not p.salary_floor:
            p.salary_floor = p.current_salary
        s.add(p)


def _job_dict(j: Job) -> dict:
    d = {c.name: getattr(j, c.name) for c in Job.__table__.columns}
    d["company"] = j.company.name if j.company else "—"
    d["company_rating"] = j.company.rating if j.company else None
    return d


def get_feed(min_score: float = 0) -> list[dict]:
    with _S() as s:
        q = (select(Job).options(joinedload(Job.company))
             .where(Job.status == "active", Job.match_score >= min_score)
             .order_by(Job.match_score.desc(), Job.published_at.desc()))
        return [_job_dict(j) for j in s.scalars(q).all()]


def mark_applied(job_id: int) -> bool:
    """Check-off: tira do feed e cria a candidatura (uma única transação)."""
    with _S() as s, s.begin():
        job = s.get(Job, job_id)
        if not job or job.status != "active":
            return False
        job.status = "applied"
        app = Application(job_id=job.id)
        s.add(app)
        s.flush()
        s.add(ApplicationEvent(application_id=app.id, status=app.status))
    return True


def discard_job(job_id: int) -> None:
    with _S() as s, s.begin():
        job = s.get(Job, job_id)
        if job and job.status == "active":
            job.status = "discarded"


def set_company_rating(company_id: int, rating: float | None) -> None:
    with _S() as s, s.begin():
        c = s.get(Company, company_id)
        if c:
            c.rating = rating or None


def get_applications(status: str | None = None, q: str | None = None) -> list[dict]:
    with _S() as s:
        stmt = (select(Application, Job, Company)
                .join(Job, Application.job_id == Job.id)
                .outerjoin(Company, Job.company_id == Company.id)
                .order_by(Application.updated_at.desc()))
        if status:
            stmt = stmt.where(Application.status == status)
        if q:
            like = f"%{q}%"
            stmt = stmt.where(or_(Job.title.ilike(like), Company.name.ilike(like), Application.notes.ilike(like)))
        return [{"id": a.id, "status": a.status, "notes": a.notes, "applied_at": a.applied_at,
                 "title": j.title, "url": j.url, "match_score": j.match_score, "company": c.name if c else "—"}
                for a, j, c in s.execute(stmt).all()]


def update_application(app_id: int, status: str, notes: str) -> None:
    with _S() as s, s.begin():
        a = s.get(Application, app_id)
        if not a:
            return
        if status != a.status:
            s.add(ApplicationEvent(application_id=a.id, status=status))
        a.status, a.notes, a.updated_at = status, notes, now()
