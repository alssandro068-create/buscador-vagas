import hmac
import os
from datetime import datetime, timezone

import streamlit as st

st.set_page_config(page_title="Job Hunter", page_icon="💼", layout="centered")


def secret(name, default=None):
    if name in os.environ:
        return os.environ[name]
    try:
        return st.secrets[name]
    except Exception:
        return default


for _k in ("DATABASE_URL", "ADZUNA_APP_ID", "ADZUNA_APP_KEY"):  # repassa os Secrets ao core
    if secret(_k):
        os.environ[_k] = str(secret(_k))

import core  # noqa: E402  (precisa vir depois de repassar os Secrets)

# ── Acesso protegido por senha (o link é público, os dados são seus) ──
_pw = secret("APP_PASSWORD")
if not _pw:
    st.error("Defina `APP_PASSWORD` nos Secrets do app para proteger seus dados.")
    st.stop()
if not st.session_state.get("auth"):
    st.title("💼 Job Hunter")
    typed = st.text_input("Senha", type="password")
    if typed:
        if hmac.compare_digest(typed, str(_pw)):
            st.session_state.auth = True
            st.rerun()
        st.error("Senha incorreta.")
    st.stop()


@st.cache_resource
def _init():
    core.init_db()
    return True


try:
    _init()
except Exception as e:
    st.error(f"Não consegui conectar ao banco: {e}")
    st.stop()

MODE = {"presencial": "🏢 Presencial", "hibrido": "🔀 Híbrido", "remoto": "🏠 Remoto", "": "—"}
ICON = {"Currículo Enviado": "📨", "Em Análise": "🔍", "Entrevista Agendada": "📅", "Teste Técnico": "🧪",
        "Proposta Recebida": "🎉", "Não Selecionado": "❌", "Desistido": "🚪"}


def money(j):
    lo, hi = j["salary_min"], j["salary_max"]
    if not (lo or hi):
        return "Salário não informado"
    txt = f"R$ {lo:,.0f} – {hi:,.0f}" if lo and hi else f"R$ {(lo or hi):,.0f}"
    return txt.replace(",", ".") + (" (estimado)" if j["salary_estimated"] else "")


def ago(dt):
    if not dt:
        return "—"
    dt = dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    h = (datetime.now(timezone.utc) - dt).total_seconds() / 3600
    return f"há {int(h)}h" if h < 48 else f"há {int(h // 24)}d"


st.title("💼 Job Hunter")
tab_feed, tab_hist, tab_prof = st.tabs(["🔎 Vagas do Dia", "📋 Histórico", "👤 Perfil"])

# ───────────── Aba 1: Vagas do Dia ─────────────
with tab_feed:
    c1, c2 = st.columns([2, 1])
    if c1.button("🔄 Buscar vagas agora", use_container_width=True):
        with st.spinner("Buscando vagas..."):
            try:
                st.session_state.stats = core.run_ingest()
            except Exception as e:
                st.error(str(e))
    min_score = c2.slider("Match mín. (%)", 0, 100, 0, step=10)
    if "stats" in st.session_state:
        s = st.session_state.stats
        st.caption(f"Última busca: {s['inserted']} novas · {s['below_floor']} abaixo do piso · "
                   f"{s['too_old']} antigas · {s['duplicates']} repetidas")
        for err in s["errors"]:
            st.warning(err)

    jobs = core.get_feed(min_score)
    if not jobs:
        st.info("Nenhuma vaga ativa. Preencha o Perfil e clique em **Buscar vagas agora**.")
    for j in jobs:
        with st.container(border=True):
            st.markdown(f"#### {j['title']}")
            rating = f"⭐ {j['company_rating']:.1f}" if j["company_rating"] else "sem nota"
            st.markdown(f"**{j['company']}** · {rating}")
            st.caption(f"📍 {j['location'] or '—'} · {MODE.get(j['work_mode'], '—')} · {money(j)} · "
                       f"publicada {ago(j['published_at'])}")
            if j["benefits"]:
                st.write("🎁 " + ", ".join(j["benefits"]))
            st.progress(int(j["match_score"]), text=f"Match {j['match_score']:.0f}%")
            have = [r for r in j["requirements"] if r not in j["missing"]]
            if have:
                st.write("✅ " + ", ".join(have))
            if j["missing"]:
                st.write("⚠️ **Falta no currículo:** " + ", ".join(j["missing"]))
            b1, b2 = st.columns(2)
            b1.link_button("Ir para a Vaga ↗", j["url"], use_container_width=True)
            b2.button("✔ Marcar como Enviado", key=f"ap{j['id']}", use_container_width=True,
                      type="primary", on_click=core.mark_applied, args=(j["id"],))
            with st.expander("Mais opções"):
                r = st.number_input("Nota da empresa (0–5)", 0.0, 5.0, float(j["company_rating"] or 0),
                                    step=0.1, key=f"r{j['id']}")
                st.button("Salvar nota", key=f"sr{j['id']}", on_click=core.set_company_rating,
                          args=(j["company_id"], r))
                st.button("🗑 Descartar vaga", key=f"d{j['id']}", on_click=core.discard_job, args=(j["id"],))

    with st.expander("🧪 Demonstração"):
        st.caption("Carrega 4 vagas fictícias para você testar o fluxo (precisa ter perfil salvo).")
        if st.button("Carregar vagas de demonstração"):
            try:
                core.run_ingest([core.MockSource()])
                st.rerun()
            except Exception as e:
                st.error(str(e))

# ───────────── Aba 2: Histórico / CRM ─────────────
with tab_hist:
    f1, f2 = st.columns([1, 1])
    status = f1.selectbox("Status", ["Todos"] + core.STATUSES)
    q = f2.text_input("Buscar", placeholder="cargo, empresa, anotação")
    apps = core.get_applications(None if status == "Todos" else status, q or None)
    st.caption(f"{len(apps)} candidatura(s)")
    for a in apps:
        with st.expander(f"{ICON[a['status']]} {a['title']} — {a['company']}"):
            st.caption(f"Enviada em {a['applied_at']:%d/%m/%Y} · match {a['match_score']:.0f}%")
            with st.form(f"f{a['id']}"):
                new_status = st.selectbox("Status do processo", core.STATUSES, index=core.STATUSES.index(a["status"]))
                notes = st.text_area("Anotações", a["notes"], placeholder="Datas de reunião, recrutador, feedback...")
                if st.form_submit_button("Salvar", type="primary"):
                    core.update_application(a["id"], new_status, notes)
                    st.toast("Salvo!")
            st.link_button("Abrir vaga original ↗", a["url"])

# ───────────── Perfil ─────────────
with tab_prof:
    prof = core.get_profile() or {}
    if not prof:
        st.info("Comece por aqui: envie o currículo e informe seu salário atual (piso das vagas).")
    up = st.file_uploader("Currículo (PDF ou DOCX)", type=["pdf", "docx"])
    if up and st.session_state.get("last_up") != (up.name, up.size):
        try:
            text = core.extract_resume_text(up.name, up.getvalue())
            st.session_state.update(last_up=(up.name, up.size), resume_text=text,
                                    skills_default=core.extract_skills(text))
        except ValueError as e:
            st.error(str(e))
    with st.form("perfil"):
        name = st.text_input("Nome", prof.get("name", ""))
        resume_text = st.text_area("Texto do currículo", st.session_state.get("resume_text", prof.get("resume_text", "")), height=150)
        default_skills = st.session_state.get("skills_default", prof.get("skills", []))
        skills = st.multiselect("Competências (edite se precisar)",
                                sorted(set(core.SKILL_VOCAB) | set(default_skills)), default=default_skills)
        terms = st.text_input("Cargos/termos de busca (separe por vírgula)", ", ".join(prof.get("search_terms", [])))
        location = st.text_input("Localização", prof.get("location", ""))
        work_mode = st.selectbox("Modalidade atual", core.MODES, index=core.MODES.index(prof.get("work_mode", "")))
        salary = st.number_input("Salário bruto atual (R$)", 0.0, step=100.0, value=float(prof.get("current_salary") or 0))
        benefits = st.text_input("Benefícios atuais (vírgula)", ", ".join(prof.get("benefits", [])))
        floor = st.number_input("Piso mínimo aceitável (R$) — 0 = usar salário atual", 0.0, step=100.0,
                                value=float(prof.get("salary_floor") or 0))
        if st.form_submit_button("Salvar perfil", type="primary"):
            split = lambda t: [x.strip() for x in t.split(",") if x.strip()]
            core.save_profile(name=name, resume_text=resume_text, skills=skills or core.extract_skills(resume_text),
                              search_terms=split(terms), location=location, work_mode=work_mode,
                              current_salary=salary or None, benefits=split(benefits), salary_floor=floor or None)
            st.success("Perfil salvo!")
